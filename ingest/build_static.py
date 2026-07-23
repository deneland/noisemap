"""Build the static noise site from the incremental accumulator.

Reads the fixed-size accumulator produced by ingest.accumulate (accumulator.npz)
and writes the Lden / Lnight / peak-LAmax grids + provenance meta into
web_static/data/. No DuckDB, no re-reading the day archive — O(1) in history.

  python -m ingest.build_static
"""
import datetime as _dt
import json

from ingest import accumulate as acc
from ingest import config as C

OUT = C.ROOT / "web_static"
DATA = OUT / "data"
VARIANTS = {"all": "all", "weekday": "wd", "weekend": "we"}


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    accp = C.DATA / "accumulator.npz"
    if not accp.exists():
        raise SystemExit(f"no accumulator at {accp} — run `python -m ingest.accumulate` first")
    a = acc.load_acc(accp)

    for name, vk in VARIANTS.items():
        grid = acc.finalize(a, vk)
        grid["variant"] = name
        path = DATA / f"noise_{name}.json"
        path.write_text(json.dumps(grid, separators=(",", ":")))
        print(f"  -> {path.name}  ({len(grid['cells']):,} cells, {grid['days']} day(s), "
              f"{path.stat().st_size / 1e6:.1f} MB)")

    to_date = lambda t: _dt.datetime.fromtimestamp(t, _dt.UTC).strftime("%Y-%m-%d")
    (DATA / "meta.json").write_text(json.dumps({
        "airport": [C.EBBR_LAT, C.EBBR_LON],
        "bbox": C.BBOX,
        "night_hours": [C.NIGHT_H[0], C.NIGHT_H[1]],
        "loud_threshold": C.NOISE["loud_threshold_db"],
        "variants": list(VARIANTS),
        "source": "adsb.lol open historical ADS-B (ODbL)",
        "date_from": to_date(a["ts_min"]),
        "date_to": to_date(a["ts_max"]),
        "samples": int(a["n_samples"]),
        "max_alt_ft": C.MAX_ALT_FT,
    }))
    print(f"static site ready: {OUT}")


if __name__ == "__main__":
    main()
