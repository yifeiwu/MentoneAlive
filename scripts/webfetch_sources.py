"""Webfetch sources from Python (runs in GHA).

One module per source lives in scripts/webfetch_<name>.py; this file is
the thin orchestrator (config + normalize + write snapshots).

Plain urllib/requests gets 403 from the Granicus WAF (Kingston Council,
Kingston Arts). Fetching uses curl-cffi with Chrome TLS impersonation.

Usage:

    python scripts/webfetch_sources.py                  # all sources
    python scripts/webfetch_sources.py --source bayside_live   # one source
    python scripts/webfetch_sources.py --source bayside_live --max-pages 2 --detail-cap 5
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

import yaml

from fetch_events import _fmt_dt, _price_sort
from jsonio import read_json
from jsonio import write_json as _write_json_atomic
from webfetch_bayside import fetch_bayside
from webfetch_ccc import fetch_ccc
from webfetch_granicus import fetch_granicus
from webfetch_http import (PartialFetch, make_session, parse_day_month_year,
                           report, set_reporting_source)
from webfetch_seniors import fetch_kingston_seniors

ROOT = Path(__file__).resolve().parent.parent
SNAP_DIR = str(ROOT / "scripts" / "webfetch_snapshots")


# One entry per `type:` in sources.yaml, and the arity is part of the entry.
# The type used to be switched on twice -- once here for dispatch and once
# again in main() to decide whether the fetcher wanted a detail cap -- so
# adding a source meant editing two places and the failure mode was a
# TypeError from fetcher(*args) rather than a config error naming the type.
FETCHERS = {
    "bayside": (fetch_bayside, True),
    "granicus": (fetch_granicus, True),
    "ccc": (fetch_ccc, True),
    # The seniors PDF arrives whole, so it has no detail pages and takes no
    # cap.
    "kingston_seniors_pdf": (fetch_kingston_seniors, False),
}

# Config keys each `type:` must have before any network I/O. These used to be
# unguarded lookups inside the fetchers, so a missing key surfaced as a bare
# KeyError('url') from three frames down, naming neither the file nor the
# entry -- and only after the earlier sources had already been fetched.
REQUIRED_KEYS = {
    "bayside": ("url",),
    "granicus": ("url",),
    "ccc": (),  # `pages` or `url`; checked separately
    "kingston_seniors_pdf": ("pdf_url", "year"),
}


def validate_config(entries):
    """Config faults, as a list of readable strings. Empty means usable.

    Runs before a single request, so a typo is reported once, up front, and
    nothing is half-fetched on the strength of it.
    """
    errors = []
    seen_ids, seen_snapshots = set(), {}
    for cfg in entries:
        sid = cfg.get("id")
        where = f"source {sid!r}" if sid else "a source with no 'id'"
        if not sid:
            errors.append(f"{where}: every webfetch entry needs an 'id'")
            continue
        if sid in seen_ids:
            errors.append(f"source {sid!r}: duplicate id in sources.yaml")
        seen_ids.add(sid)
        if not cfg.get("name"):
            errors.append(f"source {sid!r}: no 'name'")
        # Snapshot checks are type-independent, and they run before the type is
        # dispatched -- so one pass reports everything fixable at once, rather
        # than making the operator re-run to discover the next fault.
        snap = cfg.get("snapshot")
        if not snap:
            errors.append(f"source {sid!r}: no 'snapshot' configured, so its "
                          f"rows would be fetched and then discarded")
        elif not str(snap).endswith(".json"):
            errors.append(f"source {sid!r}: snapshot {snap!r} should end in "
                          f"'.json'")
        else:
            # A snapshot name is the only thing tying a config entry to a file
            # on disk; a renamed one orphans the old file with nothing to say
            # so.
            if snap in seen_snapshots:
                errors.append(f"source {sid!r}: snapshot {snap!r} is already "
                              f"claimed by {seen_snapshots[snap]!r}")
            seen_snapshots[snap] = sid
        ftype = cfg.get("type")
        if ftype not in FETCHERS:
            errors.append(f"source {sid!r}: unknown type {ftype!r} "
                          f"(known: {', '.join(sorted(FETCHERS))}); its "
                          f"required keys cannot be checked")
            continue
        for key in REQUIRED_KEYS[ftype]:
            if not cfg.get(key):
                errors.append(f"source {sid!r} (type {ftype}): missing "
                              f"required key {key!r}")
        if ftype == "ccc" and not (cfg.get("pages") or cfg.get("url")):
            # This one silently produced 0 rows and a "returned 0 events"
            # failure, which reads like a dead scraper rather than a typo.
            errors.append(f"source {sid!r} (type ccc): needs 'pages' or 'url'")
    return errors


def _previous_count(path):
    """Row count of the snapshot this run is about to replace, or None.

    Must be read BEFORE the write: afterwards the file is the new one, and the
    comparison would always report "was N" against an N it had just written.
    """
    try:
        existing = read_json(path, default=None)
    except (ValueError, OSError):
        return None
    if isinstance(existing, dict):
        existing = existing.get("rows", [])
    return len(existing) if isinstance(existing, list) else None


def _count_change(previous, new_count):
    """How this write compares with the snapshot it replaced, for the log.

    A fetch can succeed and still be truncated -- a starved --detail-cap, a
    markup change that drops one section, a site that quietly stops listing
    half its events. None of those is a fetch *error*, so PartialFetch does not
    fire, and the snapshot is replaced with a fraction of the real data. The
    calendar then shrinks by exactly that fraction, which dedupe.py will
    believe: reconcile_store() drops the rows no source still justifies, and
    the source that stopped justifying them is this one.

    So the previous size is printed beside the new one. It is a warning, not a
    refusal: a term genuinely ending does shrink a source, and blocking that
    would be worse than reporting it.
    """
    if not previous:
        return f"{new_count} rows (no previous snapshot)"
    if new_count >= previous:
        return f"{new_count} rows (was {previous})"
    lost = previous - new_count
    return (f"{new_count} rows (was {previous}: DOWN {lost}, "
            f"{100 * lost // previous}% smaller than the last run -- check this "
            f"source is not truncated before trusting the calendar)")


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
    ap.add_argument("--check-config", action="store_true",
                    help="validate sources.yaml and exit without fetching")
    args = ap.parse_args()

    with open(ROOT / "scripts" / "sources.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    webfetch_cfg = config.get("webfetch", [])

    # Every config fault at once, before any request. A typo should be one
    # message naming the entry, not a KeyError from inside a fetcher after
    # three sources have already been fetched.
    config_errors = validate_config(webfetch_cfg)
    if config_errors:
        print(f"FAIL: sources.yaml has {len(config_errors)} webfetch config "
              f"fault(s):")
        for e in config_errors:
            print(f"  - {e}")
        if not args.check_config:
            print("\nnothing fetched; fix sources.yaml and re-run")
        sys.exit(1)
    if args.check_config:
        print(f"webfetch config ok: {len(webfetch_cfg)} source(s), types "
              f"{', '.join(sorted(FETCHERS))}")
        return

    if args.source:
        # A typo used to match nothing, skip the whole loop and exit 0 -- a
        # "successful" run that fetched no data. Validate against the real ids
        # so a miss is loud, and list them to make the correct id obvious.
        known = [c.get("id") for c in webfetch_cfg]
        if args.source not in known:
            print(f"No webfetch source with id {args.source!r}. "
                  f"Available: {', '.join(str(i) for i in known)}")
            sys.exit(2)

    session = make_session()
    failures = []
    for cfg in webfetch_cfg:
        if args.source and cfg.get("id") != args.source:
            continue
        sid = cfg["id"]
        snapshot = cfg["snapshot"]
        set_reporting_source(sid)
        report(f"fetching {cfg['name']}...")
        run_cfg = dict(cfg)
        if args.max_pages is not None:
            run_cfg["max_pages"] = args.max_pages
        # detail_cap bounds per-detail-page fetches, so it only means anything
        # for a source that opens one page per event.
        fetcher, takes_cap = FETCHERS[cfg["type"]]
        fetcher_args = (session, run_cfg, args.detail_cap
                        if args.detail_cap is not None
                        else run_cfg.get("detail_cap", 15)) if takes_cap \
            else (session, run_cfg)
        path = f"{SNAP_DIR}/{snapshot}"
        try:
            rows = fetcher(*fetcher_args)
        except PartialFetch as e:
            # The ONE signal that means "do not publish". A fetch that stopped
            # early, was blocked, or was misconfigured yields a subset or
            # nothing, and writing it would replace a good snapshot with less
            # data than the last run published.
            report(f"PARTIAL FETCH ({e.reason}); keeping existing {path} "
                   f"({len(e.rows)} rows discarded)", level="error")
            failures.append((sid, f"partial fetch: {e.reason}"))
            continue
        except Exception as e:
            report(f"FAILED {repr(e)[:150]}", level="error")
            failures.append((sid, repr(e)[:150]))
            continue

        rows = normalize(rows)
        report(f"-> {len(rows)} events")
        if not rows:
            # The fetcher returned cleanly and found nothing, which is the one
            # case that genuinely means an empty source. The snapshot is still
            # kept: a source that goes quiet for a season must not erase the
            # season it already published.
            report(f"keeping existing {path} (refusing to clobber with empty)",
                   level="warn")
            failures.append((sid, "returned 0 rows"))
            continue
        # Read the old size before the write; after it, the file is the new one.
        previous = _previous_count(path)
        _write_json_atomic(path, rows)
        report(f"wrote {path} ({_count_change(previous, len(rows))})")

    set_reporting_source(None)
    if failures:
        print(f"\n{len(failures)} source(s) did not fetch cleanly:")
        for sid, reason in failures:
            print(f"  FAIL: {sid}: {reason}")
        sys.exit(1)


if __name__ == "__main__":
    main()
