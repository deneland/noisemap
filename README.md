# noisemap

A static, serverless map of **modelled aircraft noise** around Brussels Airport
(Zaventem, EBBR), built from open historical ADS-B flight tracks. Live site:
https://deneland.github.io/noisemap/

It's a house-hunting aid: runway use at EBBR rotates with wind and a legally
imposed preferential system, so the paths over any given spot swing wildly day
to day — only aggregating a long window gives an honest picture. The map shows
long-term **Lden**, **Lnight** and **peak flyover LAmax**; click anywhere for a
per-location readout.

> The levels are **modelled physics estimates, not calibrated** to the official
> noise monitors — best for *comparing* locations, not as exact dB.

## How it works (fully serverless)

A daily GitHub Action does everything; there is no server:

```
restore accumulator (release asset)
  → download one day of adsb.lol global history (~3 GB, transient on the runner)
  → filter to the Brussels bbox            (ingest/ingest_day.py)
  → fold that day into the accumulator     (ingest/accumulate.py)
  → re-publish the accumulator
  → rebuild the Lden/Lnight/LAmax grids    (ingest/build_static.py)
  → deploy web_static/ to GitHub Pages
```

The trick is the **accumulator**: the grid is built from per-cell quantities
that are all associative — energy sums (additive), peak energy (max), day counts
(set union) — so each new day folds into a **fixed-size (~MB) store** and history
is never re-read. Storage is O(1) no matter how many years accumulate. See
`ingest/accumulate.py`; its equivalence to the whole-dataset computation is
proven by the tests in the dev repo.

## Data & attribution

Flight tracks: **[adsb.lol](https://www.adsb.lol/) open historical ADS-B**,
collected by community receivers and licensed under the
**[Open Database License (ODbL) v1.0](https://opendatacommons.org/licenses/odbl/1-0/)**.
The grids published here are a Produced Work derived from that database; the
underlying data remains © adsb.lol contributors under ODbL.

## Local use

```bash
pip install -r requirements.txt
python -m ingest.backfill 2026-06-15 2026-07-14   # fetch + filter a date range
python -m ingest.accumulate                        # seed accumulator from data/bbox/
python -m ingest.build_static                      # write web_static/data/*.json
python -m http.server -d web_static 8000
```

## Repo layout

```
ingest/ingest_day.py    download one global day, keep only the Brussels bbox
ingest/noise.py         uncalibrated parametric noise model (physics helpers)
ingest/accumulate.py    incremental per-cell accumulator (+ finalize -> grids)
ingest/build_static.py  accumulator -> web_static/data/*.json + meta.json
web_static/             the Leaflet site served by Pages
.github/workflows/      the daily ingest + deploy Action
```
