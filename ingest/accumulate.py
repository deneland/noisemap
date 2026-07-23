"""Incremental noise-grid accumulator.

Folds one (or many) day(s) of bbox samples into a fixed-size per-cell store of
sufficient statistics — period energy sums (Ed/Ee/En), peak energy (Emax) and a
set of distinct local dates — kept separately for weekday and weekend. Because
those are all associative (sum / max / set-union), the long-term Lden/Lnight/
LAmax grids can be rebuilt from the store alone, WITHOUT re-reading history. The
store is a constant ~2 MB regardless of how many years accumulate.

`finalize()` energy-integrates the store into the grid (Lden/Lnight/LAmax); the
"all" variant is just weekday+weekend combined. This is proven equivalent, to
within one rounding step, to a whole-dataset batch computation by a red/green
test in the dev repo.

  seed:        python -m ingest.accumulate                 # all of data/bbox/
  fold a day:  python -m ingest.accumulate DAY.parquet --per-file
"""
import argparse
import json
import math
from pathlib import Path

import numpy as np

from ingest import config as C
from ingest import noise
from ingest.noise import (FT_M, NPD_LOGD, NPD_POWER, curve_matrix, lateral_atten_db,
                          npd_keys, power_fraction, type_adj, _l0, _mode_char)

N = C.NOISE
# Grid geometry — must match what the static site expects; kept here so the
# accumulator and build_static agree on a single source of truth.
CELL_DEG = 0.005
RADIUS_M = 5000.0
FLOOR_DB = 35.0
VAR_KEYS = ("wd", "we")
_METRICS = ("Ed", "Ee", "En", "Emax")


def grid_dims():
    lat0, lat1, lon0, lon1 = C.BBOX
    latc = (lat0 + lat1) / 2
    my = CELL_DEG * 111320.0
    mx = CELL_DEG * 111320.0 * math.cos(math.radians(latc))
    ny = int(math.ceil((lat1 - lat0) / CELL_DEG)) + 1
    nx = int(math.ceil((lon1 - lon0) / CELL_DEG)) + 1
    return lat0, lon0, my, mx, nx, ny


def _stencil(my, mx):
    ri, rj = int(RADIUS_M / my), int(RADIUS_M / mx)
    return [(di, dj, math.hypot(di * my, dj * mx))
            for di in range(-ri, ri + 1) for dj in range(-rj, rj + 1)
            if math.hypot(di * my, dj * mx) <= RADIUS_M]


def empty_acc():
    lat0, lon0, my, mx, nx, ny = grid_dims()
    def _v():
        return {m: np.zeros(nx * ny) for m in _METRICS} | {"dates": set()}
    return {"nx": nx, "ny": ny, "lat0": lat0, "lon0": lon0, "cell_deg": CELL_DEG,
            "ts_min": None, "ts_max": None, "n_samples": 0, "ingested": set(),
            "wd": _v(), "we": _v()}


def _query_rows(paths):
    """One DuckDB pass over the given parquet file(s): per-sample kinematics plus
    the clamped time-weight dt and along-track accel, and isodow so we can split
    weekday/weekend. The dt/accel window runs over the full airborne track (before
    any dow split) so a subset can't inflate the gap to the next sample."""
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL icu; LOAD icu;")
    lst = "[" + ",".join("'" + str(p) + "'" for p in paths) + "]"
    return con.execute(f"""
        WITH f AS (
          SELECT lat, lon, alt_ft, vert_rate, gs, type, "desc", icao, ts,
                 hour(timezone('Europe/Brussels', to_timestamp(ts))) AS lh,
                 isodow(timezone('Europe/Brussels', to_timestamp(ts))) AS dow,
                 cast(timezone('Europe/Brussels', to_timestamp(ts)) AS DATE) AS ld,
                 (lead(ts) OVER (PARTITION BY icao ORDER BY ts) - ts) AS gap,
                 (lead(gs) OVER (PARTITION BY icao ORDER BY ts) - gs) AS dgs
          FROM read_parquet({lst}) WHERE NOT on_ground
        )
        SELECT lat, lon, alt_ft, vert_rate, type, "desc", icao, ts, lh, dow, ld,
               greatest(least(CASE WHEN gap > 0 AND gap < 60 THEN gap ELSE 4.0 END, 30.0), 1.0) AS dt,
               gs,
               CASE WHEN gap > 0 AND gap < 60 THEN dgs * 0.514444 / gap ELSE 0.0 END AS accel
        FROM f
    """).fetchall()


def add_samples(acc, rows):
    """Scatter one batch of rows onto the accumulator (weekday/weekend split)."""
    if not rows:
        return acc
    lat0, lon0, my, mx, nx, ny = grid_dims()
    col = lambda i, d=None, t=float: np.array(
        [r[i] if r[i] is not None else d for r in rows], t) if t else \
        np.array([r[i] for r in rows])
    lat = np.array([r[0] for r in rows]); lon = np.array([r[1] for r in rows])
    alt = col(2, 0); vr = col(3, 0)
    icao = np.array([r[6] for r in rows]); ts = np.array([r[7] for r in rows], float)
    lh = np.array([r[8] for r in rows], int)
    dow = np.array([r[9] for r in rows], int)
    ld = np.array([str(r[10]) for r in rows])
    adj = np.array([type_adj(r[4], r[5]) for r in rows])
    dt = np.array([r[11] for r in rows], float)
    gs_mps = col(12, 0) * 0.514444
    accel = col(13, 0)
    keys = npd_keys(np.array([r[4] for r in rows], dtype=object))
    mode = _mode_char(vr)
    e_src = np.power(10.0, (_l0(vr) + adj) / 10.0)
    h = np.maximum(alt * FT_M - C.GROUND_ELEV_M, 10.0)
    use_npd = N["use_npd"] and bool(NPD_POWER)
    has_npd = keys != ""
    if use_npd and has_npd.any():
        Cmat = curve_matrix(keys, mode, power_fraction(vr, gs_mps, accel))
        arange = np.arange(len(keys))
    else:
        use_npd = False
    lat_on = N["lateral_attenuation"]
    dref, alpha = N["dref_m"], N["alpha_db_per_m"]
    cap_e = 10.0 ** (N["la_cap_db"] / 10.0)
    dmin2 = N["dmin_m"] ** 2
    ci = np.clip(((lat - lat0) / CELL_DEG).astype(int), 0, ny - 1)
    cj = np.clip(((lon - lon0) / CELL_DEG).astype(int), 0, nx - 1)
    period = {"Ed": (lh >= C.DAY_H[0]) & (lh < C.DAY_H[1]),
              "Ee": (lh >= C.EVE_H[0]) & (lh < C.EVE_H[1]),
              "En": (lh >= C.NIGHT_H[0]) | (lh < C.NIGHT_H[1])}
    var_mask = {"wd": (dow >= 1) & (dow <= 5), "we": dow >= 6}

    acc["n_samples"] += len(ts)
    tmn, tmx = float(ts.min()), float(ts.max())
    acc["ts_min"] = tmn if acc["ts_min"] is None else min(acc["ts_min"], tmn)
    acc["ts_max"] = tmx if acc["ts_max"] is None else max(acc["ts_max"], tmx)
    for vk in VAR_KEYS:
        acc[vk]["dates"].update(np.unique(ld[var_mask[vk]]).tolist())

    for di, dj, g in _stencil(my, mx):
        d2 = np.maximum(g * g + h * h, dmin2)
        d = np.sqrt(d2)
        inst = e_src * (dref * dref) / d2 * np.power(10.0, -alpha * (d - dref) / 10.0)
        if use_npd:
            logd = np.log10(np.maximum(d, 1.0))
            seg = np.clip(np.searchsorted(NPD_LOGD, logd) - 1, 0, len(NPD_LOGD) - 2)
            w = (logd - NPD_LOGD[seg]) / (NPD_LOGD[seg + 1] - NPD_LOGD[seg])
            la_npd = Cmat[arange, seg] * (1 - w) + Cmat[arange, seg + 1] * w
            inst = np.where(has_npd, np.power(10.0, la_npd / 10.0), inst)
        if lat_on:
            inst = inst * np.power(10.0, -lateral_atten_db(g, h) / 10.0)
        inst_c = np.minimum(inst, cap_e)
        contrib = inst_c * dt
        ti, tj = ci + di, cj + dj
        ok = (ti >= 0) & (ti < ny) & (tj >= 0) & (tj < nx)
        tgt = ti * nx + tj
        for vk in VAR_KEYS:
            A, okv = acc[vk], ok & var_mask[vk]
            if okv.any():
                np.maximum.at(A["Emax"], tgt[okv], inst_c[okv])
            for pk, pm in period.items():
                sel = okv & pm
                if sel.any():
                    A[pk] += np.bincount(tgt[sel], weights=contrib[sel], minlength=nx * ny)
    return acc


def add_files(acc, paths, combined=True):
    """Fold parquet file(s) in, skipping any already folded (idempotent by stem).

    combined=True runs one query over all new files (used when seeding from a
    backfilled range, so the dt window spans the whole set); combined=False folds
    each file on its own (production daily mode)."""
    paths = [Path(p) for p in paths]
    new = [p for p in paths if p.stem not in acc["ingested"]]
    if not new:
        return acc
    if combined:
        add_samples(acc, _query_rows(new))
    else:
        for p in new:
            add_samples(acc, _query_rows([p]))
    acc["ingested"].update(p.stem for p in new)
    return acc


def _combine_all(acc):
    out = {m: acc["wd"][m] + acc["we"][m] for m in ("Ed", "Ee", "En")}
    out["Emax"] = np.maximum(acc["wd"]["Emax"], acc["we"]["Emax"])
    out["dates"] = acc["wd"]["dates"] | acc["we"]["dates"]
    return out


def finalize(acc, variant, floor_db=FLOOR_DB):
    """Build a grid dict (the shape the static site consumes) for variant in
    {'all','wd','we'}."""
    A = _combine_all(acc) if variant == "all" else acc[variant]
    nx, ny = acc["nx"], acc["ny"]
    days = len(A["dates"]) or 1
    rate_d = A["Ed"] / (days * 12 * 3600)
    rate_e = A["Ee"] / (days * 4 * 3600)
    rate_n = A["En"] / (days * 8 * 3600)
    emax = A["Emax"]
    s = N.get("grid_blur_sigma", 0)
    if s:
        rate_d, rate_e, rate_n, emax = (
            noise._gaussian_blur(a.reshape(ny, nx), s).ravel()
            for a in (rate_d, rate_e, rate_n, emax))
    with np.errstate(divide="ignore"):
        lnight = 10.0 * np.log10(np.where(rate_n > 0, rate_n, np.nan))
        den = 12 * rate_d + 4 * rate_e * 10 ** 0.5 + 8 * rate_n * 10 ** 1.0
        lden = 10.0 * np.log10(np.where(den > 0, den / 24, np.nan))
        lamax = 10.0 * np.log10(np.where(emax > 0, emax, np.nan))
    idx = np.where(np.nan_to_num(lden) >= floor_db)[0]
    gi, gj = idx // nx, idx % nx
    return {"lat0": acc["lat0"], "lon0": acc["lon0"], "cell_deg": acc["cell_deg"],
            "nx": nx, "ny": ny, "days": days, "floor_db": floor_db,
            "cells": [[int(gi[k]), int(gj[k]),
                       round(float(lden[idx[k]]), 1),
                       round(float(np.nan_to_num(lnight[idx[k]])), 1),
                       round(float(np.nan_to_num(lamax[idx[k]])), 1)]
                      for k in range(len(idx))]}


def save_acc(acc, path):
    meta = {k: acc[k] for k in ("nx", "ny", "lat0", "lon0", "cell_deg",
                                "ts_min", "ts_max", "n_samples")}
    meta["ingested"] = sorted(acc["ingested"])
    meta["dates"] = {vk: sorted(acc[vk]["dates"]) for vk in VAR_KEYS}
    arrs = {f"{vk}_{m}": acc[vk][m] for vk in VAR_KEYS for m in _METRICS}
    np.savez_compressed(path, meta=np.array(json.dumps(meta)), **arrs)


def load_acc(path):
    z = np.load(path, allow_pickle=False)
    meta = json.loads(str(z["meta"]))
    acc = {k: meta[k] for k in ("nx", "ny", "lat0", "lon0", "cell_deg",
                                "ts_min", "ts_max", "n_samples")}
    acc["ingested"] = set(meta["ingested"])
    for vk in VAR_KEYS:
        acc[vk] = {m: z[f"{vk}_{m}"] for m in _METRICS}
        acc[vk]["dates"] = set(meta["dates"][vk])
    return acc


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("days", nargs="*", help="parquet file(s); default all of data/bbox/")
    ap.add_argument("--acc", default=str(C.DATA / "accumulator.npz"))
    ap.add_argument("--per-file", action="store_true",
                    help="fold each file independently (production daily mode)")
    args = ap.parse_args()
    accp = Path(args.acc)
    acc = load_acc(accp) if accp.exists() else empty_acc()
    files = [Path(p) for p in args.days] or sorted(C.BBOX_DIR.glob("*.parquet"))
    before = len(acc["ingested"])
    add_files(acc, files, combined=not args.per_file)
    save_acc(acc, accp)
    print(f"folded {len(acc['ingested']) - before} new day(s); {acc['n_samples']:,} "
          f"samples total, wd={len(acc['wd']['dates'])}d we={len(acc['we']['dates'])}d "
          f"-> {accp}")


if __name__ == "__main__":
    main()
