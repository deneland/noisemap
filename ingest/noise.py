"""Uncalibrated physics noise model — per-sample helpers.

Per flight sample the instantaneous A-weighted level at a ground receiver is

    LA = L0(phase) + type_adj - 20*log10(d/dref) - alpha*(d - dref)

with d the slant distance (m) and phase from the sample's vertical rate
(climbing -> departing/loud, descending -> arriving, else overflight). Where an
ANP NPD curve exists for the type it overrides the parametric distance term.

This module holds only the per-sample helpers; ingest.accumulate scatters them
onto the grid and energy-integrates to Lden/Lnight/LAmax. (The whole-dataset
reference implementation compute_grid() and its equivalence test live in the
private dev repo.)

The constants in config.NOISE are ballpark values; calibrating them against the
batc.be station measurements is the intended next step. All outputs are labelled
as modelled estimates in the UI.
"""
import numpy as np

from ingest import config as C

N = C.NOISE
FT_M = 0.3048

_HEAVY = {
    "A332", "A333", "A338", "A339", "A342", "A343", "A345", "A346", "A359",
    "A35K", "A388", "B742", "B744", "B748", "B762", "B763", "B764", "B772",
    "B773", "B77L", "B77W", "B788", "B789", "B78X", "MD11", "IL76", "A124",
    "B752", "B753",  # 757 is loud on departure
}
_REGIONAL = {
    "E170", "E75L", "E75S", "E190", "E195", "E290", "E295", "CRJ2", "CRJ7",
    "CRJ9", "CRJX", "AT43", "AT45", "AT72", "AT76", "DH8A", "DH8B", "DH8C",
    "DH8D", "SF34", "E145", "E135",
}


def type_adj(type_code, desc):
    """Aircraft-type adjustment to L0 (dB)."""
    t = (type_code or "").upper()
    d = (desc or "").upper()
    if t in _HEAVY:
        return 6.0
    if t in _REGIONAL:
        return -4.0
    ga = ("CESSNA", "PIPER", "ROBINSON", "DIAMOND", "CIRRUS", "BEECH", "SOCATA")
    if any(k in d for k in ga) or t[:2] in ("C1", "C2", "PA", "SR", "DA", "P2", "DV"):
        return -10.0
    heli = ("ROBINSON", "AIRBUS HELI", "EUROCOPTER", "AGUSTA", "BELL", "SIKORSKY")
    if t and t[0] in ("H",) or any(k in d for k in heli):
        return -6.0
    return 0.0  # narrowbody jet baseline (A320/B738/A220 family etc.)


def _l0(vrate):
    """Vectorised L0 (dB) by phase from vertical rate (ft/min)."""
    out = np.full(vrate.shape, N["L0_over"], dtype=float)
    out[vrate > N["vrate_dep"]] = N["L0_dep"]
    out[vrate < N["vrate_arr"]] = N["L0_arr"]
    return out


# --- ANP NPD curves + SAE-AIR-5662 lateral attenuation ----------------------
NPD_DIST_FT = np.array([200, 400, 630, 1000, 2000, 4000, 6300, 10000, 16000, 25000], float)
NPD_LOGD = np.log10(NPD_DIST_FT * FT_M)   # log10 slant distance in metres


def _load_npd():
    """{(ICAO_TYPE, mode): (powers[k], lamax[k,10])} — all power rows, delta-applied.

    Reads the ANP v2.3 NPD database from C.ANP_DIR (mapped to ADS-B ICAO type
    codes). Returns {} if the CSVs are absent, in which case the parametric
    distance model is used for every type."""
    import csv
    npd_p, acft_p, map_p = (C.ANP_DIR / n for n in
                            ("ANP2.3_NPD_data.csv", "ANP2.3_Aircraft.csv", "icao_to_anp.csv"))
    if not (npd_p.exists() and acft_p.exists() and map_p.exists()):
        return {}
    rows_by = {}
    with open(npd_p) as f:
        for r in csv.DictReader(f, delimiter=";"):
            if r.get("Noise Metric") != "LAmax":
                continue
            lv = [float(r[f"L_{int(d)}ft"]) for d in NPD_DIST_FT]
            rows_by.setdefault((r["NPD_ID"], r["Op Mode"]), []).append((float(r["Power Setting"]), lv))
    npd = {}
    for key, rows in rows_by.items():
        rows.sort(key=lambda x: x[0])
        npd[key] = (np.array([p for p, _ in rows]), np.array([lv for _, lv in rows]))
    with open(acft_p) as f:
        acft = {r["ACFT_ID"]: r["NPD_ID"] for r in csv.DictReader(f, delimiter=";")}
    out = {}
    with open(map_p) as f:
        for r in csv.DictReader(f):
            nid = acft.get(r["ANP_PROXY"])
            if nid is None:
                continue
            delta = {"A": float(r["DELTA_APP"]), "D": float(r["DELTA_DEP"])}
            for mode in ("A", "D"):
                pm = npd.get((nid, mode))
                if pm is not None:
                    out[(r["ICAO"].upper(), mode)] = (pm[0], pm[1] + delta[mode])
    return out


NPD_POWER = _load_npd()
NPD_TYPES = {t for (t, _m) in NPD_POWER}


def npd_keys(types):
    """ICAO type codes -> themselves where an NPD curve exists, else ''."""
    return np.array([(t or "").upper() if (t or "").upper() in NPD_TYPES else ""
                     for t in types], dtype=object)


def _mode_char(vrate):
    """'D' for climbing (departure thrust), else 'A' (approach/level)."""
    m = np.where(vrate > N["vrate_dep"], "D", "A")
    return m.astype("U1")


def lateral_atten_db(g_m, h_m):
    """SAE-AIR-5662 ground + refraction-scattering lateral attenuation (dB).

    beta = elevation angle atan(h/g); g_m = horizontal (lateral) distance.
    Engine-installation term omitted (needs per-type engine mounting).
    """
    beta = np.degrees(np.arctan2(h_m, np.maximum(g_m, 1.0)))
    agrs = 1.137 - 0.0229 * beta + 9.72 * np.exp(-0.142 * beta)
    agrs = np.where(beta < 50.0, np.maximum(agrs, 0.0), 0.0)
    frac = np.minimum(11.83 * (1.0 - np.exp(-0.00274 * g_m)), 10.86) / 10.86
    return agrs * frac


def power_fraction(vrate_fpm, v_mps, accel_mps2):
    """Estimate NPD power fraction f in [0,1] from specific excess power
    SEP = climb_rate + (V/g)*acceleration (m/s), a thrust proxy from kinematics."""
    climb = np.asarray(vrate_fpm, float) * FT_M / 60.0
    a = np.clip(np.asarray(accel_mps2, float), -N["accel_clamp_mps2"], N["accel_clamp_mps2"])
    sep = climb + np.asarray(v_mps, float) / 9.80665 * a
    return np.clip(sep / N["sep_ref_mps"], 0.0, 1.0)


def curve_matrix(keys, mode, pfrac):
    """Per-sample NPD LAmax curve (n x 10) at each sample's estimated power; 0 where uncovered."""
    C = np.zeros((len(keys), len(NPD_DIST_FT)))
    for (k, mo), (powers, mat) in NPD_POWER.items():
        sel = (keys == k) & (mode == mo)
        if not sel.any():
            continue
        if len(powers) == 1:
            C[sel] = mat[0]
            continue
        P = powers[0] + pfrac[sel] * (powers[-1] - powers[0])
        i = np.clip(np.searchsorted(powers, P) - 1, 0, len(powers) - 2)
        w = (P - powers[i]) / (powers[i + 1] - powers[i])
        C[sel] = mat[i] * (1 - w)[:, None] + mat[i + 1] * w[:, None]
    return C


def _gaussian_blur(a, sigma):
    """Separable Gaussian blur of a 2-D array (edge-extended). sigma in cells."""
    if sigma <= 0:
        return a
    r = max(1, int(round(3 * sigma)))
    k = np.exp(-(np.arange(-r, r + 1) ** 2) / (2 * sigma ** 2))
    k /= k.sum()

    def conv(arr, axis):
        pad = [(0, 0), (0, 0)]
        pad[axis] = (r, r)
        ap = np.pad(arr, pad, mode="edge")
        out = np.zeros_like(arr)
        for j, kk in enumerate(k):
            sl = [slice(None), slice(None)]
            sl[axis] = slice(j, j + arr.shape[axis])
            out = out + kk * ap[tuple(sl)]
        return out
    return conv(conv(a, 1), 0)
