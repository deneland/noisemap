"""Shared configuration for the noisemap ingest pipeline."""
from pathlib import Path

# --- Geographic area of interest -------------------------------------------
# Generous box around Brussels Airport (Zaventem, EBBR) capturing its full
# arrival/departure funnels and holding stacks — ~78 km N-S x ~90 km E-W,
# centred on the airport. Covers Brussels, Leuven, Mechelen, Aalst, Wavre and
# the southern Antwerp approaches. Measured cost: ~3.3 MB/day (~1.3 GB/year).
# (min_lat, max_lat, min_lon, max_lon) in WGS84 decimal degrees.
BBOX = (50.55, 51.25, 3.90, 5.15)

# Brussels Airport (EBBR) reference point + field elevation, for AGL estimates.
EBBR_LAT = 50.9014
EBBR_LON = 4.4844
EBBR_ELEV_M = 56.0  # ~184 ft

# Only keep samples at or below this barometric altitude (feet). 20000 ft keeps
# the full climb/descent funnels into EBBR while dropping pure high-level cruise
# clutter. The UI altitude slider still defaults to a low band for the noise
# view. None = keep all altitudes.
MAX_ALT_FT = 20000

# --- Paths ------------------------------------------------------------------
# --- Noise model ------------------------------------------------------------
# Uncalibrated physics defaults. LA = L0(phase) + type_adj
#   - 20*log10(d/dref) - alpha*(d - dref), d = slant distance (m).
# These constants are what a future calibration against the batc.be station
# measurements would tune. Values are ballpark A-weighted dB.
NOISE = {
    "dref_m": 305.0,            # reference slant distance (~1000 ft)
    "dmin_m": 120.0,            # near-field floor: point-source spreading is invalid closer
    "la_cap_db": 100.0,         # hard cap on modelled instantaneous level
    "alpha_db_per_m": 0.002,    # atmospheric + ground absorption (~2 dB/km)
    "L0_dep": 88.0,             # LAmax at dref, departing narrowbody jet (high thrust)
    "L0_arr": 83.0,             # ... arriving (reduced thrust, but flaps/gear)
    "L0_over": 84.0,            # ... level / overflight
    "vrate_dep": 128.0,         # ft/min climb above which we treat as departing
    "vrate_arr": -128.0,        # ft/min descent below which we treat as arriving
    "event_gap_s": 1800,        # >30 min gap splits one aircraft into separate flyovers
    "loud_threshold_db": 60.0,  # default LAmax threshold for a "loud event"
    "lateral_attenuation": True, # SAE-AIR-5662 ground/refraction lateral attenuation
    "use_npd": False,           # parametric model only (public build ships no ANP database)
    # Per-sample engine thrust is estimated from specific excess power
    # SEP = climb_rate + (V/g)*acceleration (m/s), then mapped to a fraction of the
    # aircraft's NPD power range: f = clip(SEP / sep_ref_mps, 0, 1). ~20 m/s ≈ full
    # takeoff/initial-climb thrust; low/negative SEP (level, descent) -> lowest power.
    "sep_ref_mps": 20.0,
    "accel_clamp_mps2": 4.0,    # clamp noisy ADS-B-derived acceleration
    "grid_blur_sigma": 0.0,     # grid smoothing (cells) in energy space; 0 = off
                                # (full-sampling removes the per-cell noise at source,
                                # so no blur is needed — raise this only if you re-sample)
}
GROUND_ELEV_M = 30.0            # nominal terrain elevation for AGL (region ~10-120 m)
# EU Lden periods (local time): day 07-19, evening 19-23 (+5 dB), night 23-07 (+10 dB)
DAY_H, EVE_H, NIGHT_H = (7, 19), (19, 23), (23, 7)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"          # transient downloaded tar parts (deleted after use)
BBOX_DIR = DATA / "bbox"    # one parquet of Brussels-only rows per day (kept)
NOISE_GRID = DATA / "noise_grid.parquet"  # precomputed noise heatmap grid
ANP_DIR = DATA / "anp"      # ANP NPD curve CSVs (seed A320; drop full DB here)

for _p in (DATA, RAW, BBOX_DIR, ANP_DIR):
    _p.mkdir(parents=True, exist_ok=True)


def release_urls(date_str: str, variant: str = "prod"):
    """Return the download URLs for a given day 'YYYY.MM.DD'.

    adsb.lol splits each day into .tar.aa / .tar.ab under a per-year repo.
    """
    year = date_str[:4]
    tag = f"v{date_str}-planes-readsb-{variant}-0"
    base = (
        f"https://github.com/adsblol/globe_history_{year}"
        f"/releases/download/{tag}/{tag}"
    )
    return [f"{base}.tar.aa", f"{base}.tar.ab"]
