"""Fetch every source and write its snapshot.

One entry point for all sources, because a source's transport is a property of
its *host*, not of the script that happens to fetch it. This used to be two:
`fetch_events.py` (plain urllib) and `webfetch_sources.py` (curl-cffi Chrome
impersonation), split on the claim that "plain urllib gets 403 from the Granicus
WAF". `kingston_council` and `kingston_arts` need impersonation for listings,
and `greater_dandenong` + `gd_libraries` need it for detail pages only;
the other webfetch hosts answer plain HTTP. So a source now says
`impersonate: true` in sources.yaml and everything else shares one dispatch, one
config validation pass and one set of failure rules.

    python scripts/fetch_sources.py                  # all sources
    python scripts/fetch_sources.py --check-config   # validate and exit
    python scripts/fetch_sources.py --source kingston_hubs
    python scripts/fetch_sources.py --source bayside_live --max-pages 2 --detail-cap 5

Output is one snapshot per source, and that boundary is the point: dedupe.py
reads files, never a fetcher.

- webfetch sources -> scripts/webfetch_snapshots/<snapshot>
- plain sources    -> data/raw_events.json (merged into one file)

Failure behaviour, in full:

- `raise PartialFetch` is the ONLY "do not publish" signal. It means the fetch
  was cut short, blocked, or misconfigured, and the existing snapshot survives.
- returning [] means the source genuinely has nothing. Both exit non-zero.
- 0 rows never overwrites a snapshot: a festival out of season must not erase
  the season it already published.
- config faults are found before any request, all at once.
- a fetch that succeeds but returns a fraction of the real data prints the
  previous size beside the new one and calls out the shrink. A warning, not a
  refusal, because a term genuinely ending does shrink a source.
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

import config as _config  # noqa: E402

from fetch_urllib_sources import (fetch_chatty_cafe,  # noqa: E402
                                  fetch_gd_libraries, fetch_greater_dandenong,
                                  fetch_kingston_hubs)
from jsonio import read_json, write_json  # noqa: E402
from webfetch_bayside import fetch_bayside  # noqa: E402
from webfetch_ccc import fetch_ccc  # noqa: E402
from webfetch_directory import fetch_directory  # noqa: E402
from webfetch_everi import fetch_everi  # noqa: E402
from webfetch_frankston_libraries import (  # noqa: E402
    fetch_frankston_libraries)
from webfetch_granicus import fetch_granicus  # noqa: E402
from webfetch_http import (PartialFetch, parse_day_month_year,  # noqa: E402
                           price_sort as _price_sort, report,
                           set_reporting_source)
from webfetch_seniors import fetch_kingston_seniors  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SNAP_DIR = ROOT / "scripts" / "webfetch_snapshots"

# Which file a source's rows land in. Snapshot-per-source for the webfetch
# sources, because each one's rows are also committed and a single overwritten
# file would lose the per-source view. The plain sources share raw_events.json,
# which is gitignored and is a scratch file for dedupe.py.
SNAPSHOT_TYPES = {"bayside", "granicus", "ccc", "kingston_seniors_pdf",
                 "oc_directory", "everi", "frankston_libraries"}

# One entry per `type:` in sources.yaml. The type is the ONLY dispatch key --
# it used to be dispatched on here and on `id` in the other script, so adding a
# source meant editing two places and the failure mode was a TypeError from
# fetcher(*args) rather than a config error naming the type.
FETCHERS = {
    "api": (fetch_kingston_hubs, False),
    "greater_dandenong": (fetch_greater_dandenong, False),
    "gd_libraries": (fetch_gd_libraries, False),
    "venues": (fetch_chatty_cafe, False),
    "bayside": (fetch_bayside, True),
    "granicus": (fetch_granicus, True),
    "ccc": (fetch_ccc, True),
    "oc_directory": (fetch_directory, True),
    "everi": (fetch_everi, True),
    "frankston_libraries": (fetch_frankston_libraries, True),
    # The seniors PDF arrives whole, so it has no detail pages and takes no cap.
    "kingston_seniors_pdf": (fetch_kingston_seniors, False),
}

# Config keys each `type:` must have before any network I/O. These used to be
# unguarded lookups inside the fetchers, so a missing key surfaced as a bare
# KeyError('url') from three frames down, naming neither the file nor the
# entry -- and only after the earlier sources had already been fetched.
REQUIRED_KEYS = {
    "api": ("calendars", "calendar_venues"),
    "greater_dandenong": ("url",),
    "gd_libraries": ("url",),
    "venues": ("venues",),
    "bayside": ("url",),
    "frankston_libraries": ("url",),
    "granicus": ("url",),
    "ccc": (),  # `pages` or `url`; checked separately
    "oc_directory": ("url",),
    "everi": ("sitemap",),
    "kingston_seniors_pdf": ("pdf_url", "year"),
}


def validate_config(entries):
    """Config faults, as a list of readable strings. Empty means usable.

    Runs before a single request, so a typo is reported once, up front, and
    nothing is half-fetched on the strength of it.

    Every check is independent of every other, and in particular the snapshot
    checks do not depend on the `type` being recognised: one pass reports
    everything fixable at once, rather than making the operator re-run to
    discover the next fault. Whether a source owns a snapshot is decided by the
    config list it came from (`group`), not by its type -- a mistyped type would
    otherwise silently skip the snapshot check too.
    """
    errors = []
    seen_ids, seen_snapshots = set(), {}
    for cfg in entries:
        sid = cfg.get("id")
        where = f"source {sid!r}" if sid else "a source with no 'id'"
        if not sid:
            errors.append(f"{where}: every entry needs an 'id'")
            continue
        if sid in seen_ids:
            errors.append(f"source {sid!r}: duplicate id in sources.yaml")
        seen_ids.add(sid)
        if not cfg.get("name"):
            errors.append(f"source {sid!r}: no 'name'")
        ftype = cfg.get("type")
        if ftype not in FETCHERS:
            errors.append(f"source {sid!r}: unknown type {ftype!r} "
                          f"(known: {', '.join(sorted(FETCHERS))}); its "
                          f"required keys cannot be checked")
        else:
            for key in REQUIRED_KEYS[ftype]:
                if not cfg.get(key):
                    errors.append(f"source {sid!r} (type {ftype}): missing "
                                  f"required key {key!r}")
            if ftype == "ccc":
                # D21: a source config holds the venue, never a guess. A CCC row
                # is filed at one of two venues and both addresses used to be
                # constants inside the fetcher, so a stale address could not be
                # seen or corrected without reading Python.
                if not cfg.get("pages") and not cfg.get("url"):
                    # This one silently produced 0 rows and a "returned 0 events"
                    # failure, which reads like a dead scraper rather than a typo.
                    errors.append(f"source {sid!r} (type ccc): needs 'pages' or "
                                  f"'url'")
                venues = cfg.get("venues") or {}
                for key in ("default", "hall"):
                    v = venues.get(key) or {}
                    if not v.get("name") or not v.get("address"):
                        errors.append(
                            f"source {sid!r} (type ccc): venue {key!r} needs a "
                            f"name AND an address, or its rows publish with no "
                            f"place to go")
            if ftype == "api":
                cals = cfg.get("calendars") or []
                unmapped = [
                    c for c in cals
                    if not cfg.get("calendar_venues", {}).get(c, {}).get("name")
                    or not cfg.get("calendar_venues", {}).get(c, {}).get("address")]
                if unmapped:
                    # A hard failure, not a guess. A wrong address publishes
                    # silently and nothing downstream can detect it; this one
                    # did exactly that for 300 rows from an unmapped calendar.
                    errors.append(f"source {sid!r}: {len(unmapped)} calendar "
                                  f"id(s) have no name AND address in "
                                  f"calendar_venues: {unmapped}")
            # The two keys that govern how much pressure a run may put on a
            # host, and how much of a listing it may read. Both are read with
            # int()/float() inside the fetchers, where a typo becomes a
            # ValueError from a frame that names neither the file nor the key --
            # and a `crawl_delay` that silently falls back to the default is
            # worse still, because the run then crawls at the wrong rate and
            # reports nothing.
            for key in ("max_pages", "detail_cap", "max_requests_per_run"):
                if cfg.get(key) is not None and not str(cfg[key]).strip(
                ).lstrip("-").isdigit():
                    errors.append(f"source {sid!r}: {key}={cfg[key]!r} is not a "
                                  f"whole number")
            if cfg.get("crawl_delay") is not None:
                try:
                    if float(cfg["crawl_delay"]) < 0:
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append(
                        f"source {sid!r}: crawl_delay={cfg['crawl_delay']!r} is "
                        f"not a non-negative number of seconds (0 is floored "
                        f"up to the shared minimum, not taken as 'no limit')")
        if cfg.get("group") != "snapshot":
            continue
        snap = cfg.get("snapshot")
        if not snap:
            errors.append(f"source {sid!r}: no 'snapshot' configured, so its "
                          f"rows would be fetched and then discarded")
        elif not str(snap).endswith(".json"):
            errors.append(f"source {sid!r}: snapshot {snap!r} should end in "
                          f"'.json'")
        else:
            # A snapshot name is the only thing tying a config entry to a file
            # on disk; a renamed one orphans the old file with nothing to say so.
            if snap in seen_snapshots:
                errors.append(f"source {sid!r}: snapshot {snap!r} is already "
                              f"claimed by {seen_snapshots[snap]!r}")
            seen_snapshots[snap] = sid
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
    believe: reconcile_store() drops the rows no source still justifies, and the
    source that stopped justifying them is this one.

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


def plain_sources_missing(plain_ids, plain_ok, only_source=None):
    """Plain sources that owe rows to data/raw_events.json but did not deliver.

    The file holds *all* of the plain sources or it is misleading, so it may
    only be written when every one of them fetched cleanly. The check used to
    be `if plain_rows:`, which is true as soon as one of them succeeds -- so a
    kingston_hubs failure wrote a raw_events.json holding chatty_cafe alone, and
    the next stage could not tell that from a complete fetch. It reads the file,
    finds a source that no longer states its rows, and `reconcile_store()`
    withdraws that source's 524 rows: the calendar quietly loses a third of
    itself over one failed request.

    A `--source` run is a deliberate single-source fetch and the run summary
    already says the file now holds that source alone, so only the requested
    one is required. Intersecting with `plain_ids` is what keeps a snapshot
    source run (`--source ccc`) from being read as a plain source that failed
    to arrive -- a check that would have turned every snapshot debug run into a
    non-zero exit.
    """
    required = (set(plain_ids) & {only_source}) if only_source else set(plain_ids)
    return sorted(required - set(plain_ok))


def normalize(rows):
    """The two derived fields every snapshot row carries.

    Both former fetchers did this, in two near-identical passes. It lives here
    now so a row cannot get a `price_sort` in one script and not the other.
    """
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
            # mis-parses to 23:00, and unrelated times ("bookings close 5pm")
            # would be picked up. Times come from labelled fields only.
            try:
                dt = parse_day_month_year(r["datetime_text"])
            except Exception:
                dt = None
        r["datetime_iso"] = dt.isoformat() if dt else None
        # webfetch_http owns the price rule; it is imported here rather than
        # wrapped, so there is one reader of it. (This module used to re-export
        # a `price_sort()` that only forwarded, which was a second name for a
        # rule nothing fetched through.)
        r["price_sort"] = _price_sort(r.get("price_text"))
        out.append(r)
    return out


_SESSIONS = {}


def load_config():
    """Every source entry, tagged with which config list it came from.

    Lives in config.py, which is the one reader of sources.yaml; this is the name
    the rest of the pipeline already imports, so it stays as a re-export.
    """
    return _config.load_config()


def session_for(cfg):
    """The session this source's host needs.

    Both kinds are built lazily and then reused, so a nine-source run opens one
    connection pool rather than nine. `impersonate: true` selects the Chrome-TLS
    session; everything else gets the plain urllib one, which is what the source
    was using before the two scripts were merged.
    """
    kind = "curl" if cfg.get("impersonate") else "plain"
    if kind not in _SESSIONS:
        if kind == "curl":
            from webfetch_http import make_session
            _SESSIONS[kind] = make_session()
        else:
            from webfetch_http import make_plain_session
            _SESSIONS[kind] = make_plain_session()
    return _SESSIONS[kind]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=None)
    ap.add_argument("--max-pages", type=int, default=None)
    ap.add_argument("--detail-cap", type=int, default=None)
    ap.add_argument("--check-config", action="store_true",
                    help="validate sources.yaml and exit without fetching")
    args = ap.parse_args()

    entries = load_config()

    # Every config fault at once, before any request. A typo should be one
    # message naming the entry, not a KeyError from inside a fetcher after
    # three sources have already been fetched.
    config_errors = validate_config(entries)
    if config_errors:
        print(f"FAIL: sources.yaml has {len(config_errors)} config fault(s):")
        for e in config_errors:
            print(f"  - {e}")
        if not args.check_config:
            print("\nnothing fetched; fix sources.yaml and re-run")
        sys.exit(1)
    if args.check_config:
        print(f"config ok: {len(entries)} source(s), types "
              f"{', '.join(sorted(FETCHERS))}")
        return

    if args.source:
        # A typo used to match nothing, skip the whole loop and exit 0 -- a
        # "successful" run that fetched no data. Validate against the real ids
        # so a miss is loud, and list them to make the correct id obvious.
        known = [c.get("id") for c in entries]
        if args.source not in known:
            print(f"No source with id {args.source!r}. "
                  f"Available: {', '.join(str(i) for i in known)}")
            sys.exit(2)

    failures = []
    plain_rows = []
    # Every plain source configured, so the shared file can be written only when
    # all of them actually contributed. `raw_events.json` holds *all* of them or
    # it is misleading: a file holding one source's rows is not "the plain
    # sources' output", and the next stage cannot tell the difference.
    plain_ids = {c.get("id") for c in entries if c.get("group") != "snapshot"}
    plain_ok = set()
    for cfg in entries:
        if args.source and cfg.get("id") != args.source:
            continue
        sid = cfg["id"]
        ftype = cfg["type"]
        session = session_for(cfg)
        set_reporting_source(sid)
        report(f"fetching {cfg['name']}...")
        run_cfg = dict(cfg)
        if args.max_pages is not None:
            run_cfg["max_pages"] = args.max_pages
        # detail_cap bounds per-detail-page fetches, so it only means anything
        # for a source that opens one page per event.
        fetcher, takes_cap = FETCHERS[ftype]
        cap = (args.detail_cap if args.detail_cap is not None
               else run_cfg.get("detail_cap"))
        try:
            rows = fetcher(run_cfg, session, cap) if takes_cap \
                else fetcher(run_cfg, session)
        except PartialFetch as e:
            # The ONE signal that means "do not publish". A fetch that stopped
            # early, was blocked, or was misconfigured yields a subset or
            # nothing, and writing it would replace a good snapshot with less
            # data than the last run published.
            path = _snapshot_path(cfg)
            report(f"PARTIAL FETCH ({e.reason}); keeping existing "
                   f"{path or 'raw_events.json'} "
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
            # case that genuinely means an empty source. An existing snapshot is
            # still kept: a source that goes quiet for a season must not erase
            # the season it already published.
            report("no rows; refusing to clobber an existing snapshot",
                   level="warn")
            failures.append((sid, "returned 0 rows"))
            continue
        if ftype not in SNAPSHOT_TYPES:
            plain_rows.extend(rows)
            plain_ok.add(sid)
            continue
        path = _snapshot_path(cfg)
        # Read the old size before the write; after it, the file is the new one.
        previous = _previous_count(path)
        write_json(path, rows)
        report(f"wrote {path} ({_count_change(previous, len(rows))})")

    set_reporting_source(None)

    # One file for the plain sources, and only when every one of them ran.
    # Written last, so a partial run cannot half-replace it.
    # plain_sources_missing() carries the rule, and the reason.
    raw_path = ROOT / "data" / "raw_events.json"
    missing = plain_sources_missing(plain_ids, plain_ok, args.source)
    if missing:
        report(f"not writing {raw_path.name}: no clean fetch for "
               f"{', '.join(sorted(missing))}, so it would hold those sources' "
               f"rows absent. Existing file left alone.", level="error")
        failures.append(("raw_events.json",
                         f"incomplete plain fetch, missing "
                         f"{', '.join(sorted(missing))}"))
    elif plain_rows:
        previous = _previous_count(raw_path)
        write_json(raw_path, plain_rows)
        report(f"wrote {raw_path} "
               f"({_count_change(previous, len(plain_rows))})")
    else:
        report(f"no rows for {raw_path.name}; existing file left alone",
               level="warn")

    if failures:
        print(f"\n{len(failures)} source(s) did not fetch cleanly:")
        for sid, reason in failures:
            print(f"  FAIL: {sid}: {reason}")
        sys.exit(1)
    # The number actually run, not the number configured: a `--source` run
    # fetched one, and reporting nine made a single-source debug run look like a
    # complete refresh when it was nothing of the kind.
    ran = 1 if args.source else len(entries)
    print(f"fetch ok: {ran} of {len(entries)} configured source(s)"
          + ("  (--source run: raw_events.json now holds this source only)"
             if args.source else ""))


def _snapshot_path(cfg):
    if cfg["type"] not in SNAPSHOT_TYPES:
        return None
    return str(SNAP_DIR / cfg["snapshot"])


if __name__ == "__main__":
    main()