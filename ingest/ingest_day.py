"""Ingest one day of adsb.lol global history, keeping only Brussels-bbox samples.

Strategy: stream-and-discard. Download the day's split tar parts, stream through
them extracting per-aircraft trace files, filter each trace point to the bbox +
altitude ceiling, and write the survivors to a compact per-day Parquet file.
The raw ~3 GB tar is deleted afterwards unless --keep-raw is given.

Usage:  python -m ingest.ingest_day 2026-07-14 [--keep-raw]
"""
import argparse
import gzip
import subprocess
import sys
import tarfile
from pathlib import Path

import orjson
import pyarrow as pa
import pyarrow.parquet as pq

from ingest import config as C


class ChainedFile:
    """Read several files as one continuous byte stream (for split tars)."""

    def __init__(self, paths):
        self._paths = list(paths)
        self._idx = 0
        self._fh = open(self._paths[0], "rb")

    def read(self, size=-1):
        if self._fh is None:
            return b""
        buf = self._fh.read(size)
        while (size < 0 or len(buf) < size) and self._idx + 1 < len(self._paths):
            self._fh.close()
            self._idx += 1
            self._fh = open(self._paths[self._idx], "rb")
            more = self._fh.read(size if size < 0 else size - len(buf))
            buf += more
        return buf

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None


def download_parts(date_dot: str) -> list[Path]:
    """Download the day's tar parts into data/raw (resumable). Returns paths."""
    paths = []
    for url in C.release_urls(date_dot):
        dest = C.RAW / url.rsplit("/", 1)[-1]
        paths.append(dest)
        # Always run wget -c: it completes a partial file or no-ops if the file
        # is already whole. (Skipping on "file exists" once treated a truncated
        # partial download as complete and produced a corrupt tar.)
        print(f"  fetching {dest.name} (resume if partial) ...")
        rc = subprocess.call(
            ["wget", "-q", "--show-progress", "-c", "-O", str(dest), url]
        )
        if rc != 0:
            raise RuntimeError(f"download failed ({rc}) for {url}")
    return paths


# trace-point array indices (readsb trace_full format)
I_SECS, I_LAT, I_LON, I_ALT, I_GS, I_TRK, I_FLAGS, I_VRATE = 0, 1, 2, 3, 4, 5, 6, 7
I_SRC, I_GEOM = 9, 10


def ingest_day(date_iso: str, keep_raw: bool = False) -> Path:
    date_dot = date_iso.replace("-", ".")
    out = C.BBOX_DIR / f"{date_iso}.parquet"
    if out.exists():
        print(f"{date_iso}: already ingested -> {out.name}")
        return out

    print(f"{date_iso}: fetching parts")
    parts = download_parts(date_dot)

    lat0, lat1, lon0, lon1 = C.BBOX
    cols = {k: [] for k in (
        "ts", "icao", "reg", "type", "desc", "lat", "lon",
        "alt_ft", "geom_alt_ft", "on_ground", "gs", "track", "vert_rate",
    )}
    n_files = n_pts = n_kept = 0

    reader = ChainedFile(parts)
    tar = tarfile.open(fileobj=reader, mode="r|")
    try:
        for m in tar:
            if not (m.isfile() and "trace_full_" in m.name):
                continue
            n_files += 1
            if n_files % 5000 == 0:
                print(f"  ...{n_files} aircraft scanned, {n_kept} samples kept")
            f = tar.extractfile(m)
            if f is None:
                continue
            try:
                raw = f.read()
                try:
                    raw = gzip.decompress(raw)  # trace files are gzipped JSON
                except (OSError, gzip.BadGzipFile):
                    pass  # tolerate plain JSON too
                obj = orjson.loads(raw)
            except Exception:
                continue
            base = obj.get("timestamp")
            trace = obj.get("trace")
            if base is None or not trace:
                continue
            icao = obj.get("icao")
            reg = obj.get("r")
            typ = obj.get("t")
            desc = obj.get("desc")  # human-readable model, e.g. "BOEING 747-8"
            for p in trace:
                n_pts += 1
                lat = p[I_LAT]
                lon = p[I_LON]
                if lat is None or lon is None:
                    continue
                if not (lat0 <= lat <= lat1 and lon0 <= lon <= lon1):
                    continue
                alt = p[I_ALT]
                on_ground = alt == "ground"
                alt_ft = 0 if on_ground else alt
                if (C.MAX_ALT_FT is not None and alt_ft is not None
                        and alt_ft > C.MAX_ALT_FT):
                    continue
                geom = p[I_GEOM] if len(p) > I_GEOM else None
                cols["ts"].append(base + p[I_SECS])
                cols["icao"].append(icao)
                cols["reg"].append(reg)
                cols["type"].append(typ)
                cols["desc"].append(desc)
                cols["lat"].append(lat)
                cols["lon"].append(lon)
                cols["alt_ft"].append(alt_ft)
                cols["geom_alt_ft"].append(geom)
                cols["on_ground"].append(on_ground)
                cols["gs"].append(p[I_GS])
                cols["track"].append(p[I_TRK])
                cols["vert_rate"].append(p[I_VRATE] if len(p) > I_VRATE else None)
                n_kept += 1
    finally:
        tar.close()
        reader.close()

    table = pa.table({
        "ts": pa.array(cols["ts"], pa.float64()),
        "icao": pa.array(cols["icao"], pa.string()),
        "reg": pa.array(cols["reg"], pa.string()),
        "type": pa.array(cols["type"], pa.string()),
        "desc": pa.array(cols["desc"], pa.string()),
        "lat": pa.array(cols["lat"], pa.float64()),
        "lon": pa.array(cols["lon"], pa.float64()),
        "alt_ft": pa.array(cols["alt_ft"], pa.float64()),
        "geom_alt_ft": pa.array(cols["geom_alt_ft"], pa.float64()),
        "on_ground": pa.array(cols["on_ground"], pa.bool_()),
        "gs": pa.array(cols["gs"], pa.float64()),
        "track": pa.array(cols["track"], pa.float64()),
        "vert_rate": pa.array(cols["vert_rate"], pa.float64()),
    })
    tmp = out.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp, compression="zstd")
    tmp.rename(out)
    print(f"{date_iso}: {n_files} aircraft, {n_pts:,} points scanned, "
          f"{n_kept:,} kept -> {out.name} ({out.stat().st_size/1e6:.1f} MB)")

    if not keep_raw:
        for p in parts:
            p.unlink(missing_ok=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", help="ISO date YYYY-MM-DD")
    ap.add_argument("--keep-raw", action="store_true")
    args = ap.parse_args()
    ingest_day(args.date, keep_raw=args.keep_raw)


if __name__ == "__main__":
    sys.exit(main())
