"""Backfill a date range by ingesting each day into data/bbox/.

Days already present are skipped, so the run is resumable — just re-run it.
Missing/failed days are logged and skipped (some days may not be released).

Usage:
  python -m ingest.backfill 2026-06-15 2026-07-14        # inclusive range
  python -m ingest.backfill 2026-06-15 2026-07-14 --keep-raw
"""
import argparse
import sys
import traceback
from datetime import date, timedelta

from ingest.ingest_day import ingest_day


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("start", help="YYYY-MM-DD (inclusive)")
    ap.add_argument("end", help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--keep-raw", action="store_true")
    args = ap.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    days = list(daterange(start, end))
    print(f"Backfill {len(days)} day(s): {args.start} .. {args.end}\n")

    ok, failed = 0, []
    for d in days:
        iso = d.isoformat()
        try:
            ingest_day(iso, keep_raw=args.keep_raw)
            ok += 1
        except Exception as e:  # keep going on a bad/missing day
            failed.append((iso, str(e)))
            print(f"{iso}: FAILED -> {e}")
            traceback.print_exc()

    print(f"\nDone. {ok} ok, {len(failed)} failed.")
    for iso, e in failed:
        print(f"  - {iso}: {e}")
    return 1 if failed and ok == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
