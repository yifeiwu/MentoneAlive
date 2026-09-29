"""Webfetch sources from Python (runs in GHA).

One module per source lives in scripts/webfetch_<name>.py; this file is
the thin orchestrator (config + normalize + write snapshots).

Plain urllib/requests gets 403 from the Granicus WAF (Kingston Council,
Kingston Arts). Fetching uses curl-cffi with Chrome TLS impersonation.

Usage:

    python scripts/webfetch_sources.py                  # all sources
    python scripts/webfetch_sources.py --source bayside # one source
    python scripts/webfetch_sources.py --source bayside --max-pages 2 --detail-cap 5
"""
import argparse
import sys
from datetime import datetime

import yaml

from fetch_events import _fmt_dt, _price_sort
from jsonio import write_json as _write_json_atomic
from webfetch_bayside import fetch_bayside
from webfetch_ccc import fetch_ccc
from webfetch_granicus import fetch_granicus
from webfetch_http import PartialFetch, make_session, parse_day_month_year
from webfetch_seniors import fetch_kingston_seniors

SNAP_DIR = "scripts/webfetch_snapshots"


FETCHERS = {"bayside": fetch_bayside, "granicus": fetch_granicus,
            "ccc": fetch_ccc, "kingston_seniors_pdf": fetch_kingston_seniors}


def normalize(rows):
    out = []
    for r in rows:
        dt = None
        if r.get("datetime_iso"):
            try:
                dt = datetime.fromisoformat(r["datetime_iso"])
            except (ValueError, TypeError):
                dt = None
        if dt is None and r.get("datetime_text"):
            # Listing day-blocks without detail enrichment stay midnight;
            # still a real calendar day supplied by the source. Do NOT try to
            # pull a time out of the free-text field: a dotted "9.30am"
            # mis-parses to 23:00, and unrelated times ("bookings close
            # 5pm") would be picked up. Times come from labelled fields only.
            try:
                dt = parse_day_month_year(r["datetime_text"])
            except Exception:
                dt = None
        if dt is None:
            r["datetime_iso"] = None
        else:
            r["datetime_iso"] = dt.isoformat()
        # A date is "real" when the source itself supplied one (via either
        # field). Never infer this from the value we just wrote back --
        # that field is non-empty by construction at this point.
        r["has_real_date"] = dt is not None
        r["datetime_display"] = _fmt_dt(dt) if dt else (r.get("datetime_text") or "")
        r["price_sort"] = _price_sort(r.get("price_text"))
        r["source_label"] = r.get("source_id", "unknown")
        out.append(r)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=None)
    ap.add_argument("--max-pages", type=int, default=None)
    ap.add_argument("--detail-cap", type=int, default=None)
    args = ap.parse_args()

    with open("scripts/sources.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    session = make_session()
    failures = []
    for cfg in config.get("webfetch", []):
        if args.source and cfg.get("id") != args.source:
            continue
        snapshot = cfg.get("snapshot")
        print(f"Webfetching {cfg.get('name')} ({cfg.get('id')})...")
        if not snapshot:
            print(f"  {cfg.get('id')}: no snapshot configured, skipped")
            continue
        if args.max_pages is not None:
            cfg = dict(cfg, max_pages=args.max_pages)
        cap = args.detail_cap if args.detail_cap is not None else cfg.get("detail_cap", 15)
        fetcher = FETCHERS.get(cfg.get("type"))
        if fetcher is None:
            msg = f"unknown fetcher type {cfg.get('type')!r}"
            print(f"  {cfg['id']}: FAILED {msg}")
            failures.append((cfg["id"], msg))
            continue
        path = f"{SNAP_DIR}/{snapshot}"
        try:
            rows = fetcher(session, cfg, cap)
        except PartialFetch as e:
            # A crawl that stopped early yields a *subset*. Writing it would
            # replace a good snapshot with partial data, so keep the old one.
            print(f"  {cfg['id']}: PARTIAL CRAWL ({e.reason}); keeping existing "
                  f"{path} ({len(e.rows)} rows discarded)")
            failures.append((cfg["id"], f"partial crawl: {e.reason}"))
            continue
        except Exception as e:
            print(f"  {cfg['id']}: FAILED {repr(e)[:150]}")
            failures.append((cfg["id"], repr(e)[:150]))
            continue

        rows = normalize(rows)
        print(f"  -> {len(rows)} events")
        if not rows:
            print(f"  keeping existing {path} (refusing to clobber with empty)")
            failures.append((cfg["id"], "returned 0 rows"))
            continue
        _write_json_atomic(path, rows)
        print(f"  wrote {path}")

    if failures:
        print(f"\n{len(failures)} source(s) did not fetch cleanly:")
        for sid, reason in failures:
            print(f"  FAIL: {sid}: {reason}")
        sys.exit(1)


if __name__ == "__main__":
    main()
