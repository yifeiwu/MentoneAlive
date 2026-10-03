"""The fetch call convention, asserted without touching the network.

`fetch_sources.py` calls every fetcher the same way -- `fetcher(cfg, session)`,
plus a `detail_cap` for the three snapshot types that crawl detail pages, and
it detects that third argument with `inspect.signature`. So a fetcher whose
signature drifts is not a clean failure: it compiles, `checks.py` passes,
`--check-config` passes, and the source only breaks on a live run, where the
error arrives as a `TypeError` after every earlier source has already been
fetched and written.

That is not hypothetical. Merging the two fetchers left four of the nine
`def`s at `(cfg)` while their bodies had already been rewritten to use
`session`, so `kingston_hubs`, `greater_dandenong`, `gd_libraries` and
`chatty_cafe` all failed at runtime on the first full run -- and
`greater_dandenong`/`gd_libraries` would additionally have raised `NameError`
on the unbound name if the call had got that far.

This asserts the convention structurally instead, which is the only place it
can be checked without a network round trip.
"""
import inspect
import sys

import checks
import fetch_sources


def _check_signatures(failures):
    """Every fetcher matches how fetch_sources calls it."""
    for cfg in fetch_sources.load_config():
        sid, ftype = cfg.get("id"), cfg.get("type")
        if ftype not in fetch_sources.FETCHERS:
            checks.check(f"{sid}: type {ftype!r} has a fetcher", False, True,
                         failures)
            continue
        fn = fetch_sources.FETCHERS[ftype][0]
        params = list(inspect.signature(fn).parameters)
        # The first two are positional and always present; the third is how a
        # fetcher opts into a detail cap, so its name has to stay stable too.
        prefix_ok = params[:2] == ["cfg", "session"]
        cap_ok = len(params) in (2, 3) and (
            len(params) == 2 or params[2] == "detail_cap")
        checks.check(f"{sid}: signature {params}", prefix_ok and cap_ok, True,
                     failures)


def _check_config_shape(failures):
    """A source is either snapshot-owning or raw-feeding, never undecided."""
    for cfg in fetch_sources.load_config():
        sid, group = cfg.get("id"), cfg.get("group")
        checks.check(f"{sid}: group is snapshot or shared",
                     group in ("snapshot", "shared"), True, failures)
        # `group` is derived from which list the entry came from, and it is what
        # decides snapshot ownership, so the two must agree: a snapshot type in
        # the shared list would write a file nobody commits, and a shared type
        # in the snapshot list would lose rows that only raw_events.json keeps.
        expected = ("snapshot"
                    if cfg.get("type") in fetch_sources.SNAPSHOT_TYPES
                    else "shared")
        checks.check(f"{sid}: group matches type {cfg.get('type')!r}",
                     group, expected, failures)


def _check_impersonation(failures):
    """Impersonation is declared where the transport needs it, and nowhere else.

    The Greater Dandenong fetchers reach a browser-impersonating session for
    their detail pages; the Granicus host needs one for the listing; so do the
    OpenCities directory and the libraries CMS, which both answer a plain
    request with 403. Everyone else is plain urllib. A flag moved to the wrong
    source is a silent TLS failure, not a crash, so it is worth pinning -- and
    a flag left off a host that needs one fails the fetch rather than degrading,
    which is the better of the two failure modes but still a failure.
    """
    curl = {c["id"] for c in fetch_sources.load_config()
            if c.get("impersonate")}
    expected = {"kingston_council", "kingston_arts", "kingston_groups",
                "greater_dandenong", "gd_libraries", "frankston_libraries"}
    checks.check("impersonate sources", curl, expected, failures)

    # And the two GD fetchers must accept a session, since that is how they get
    # the shared one rather than building their own per run.
    for ftype in ("greater_dandenong", "gd_libraries"):
        fn = fetch_sources.FETCHERS[ftype][0]
        checks.check(f"{ftype}: accepts a session",
                     "session" in inspect.signature(fn).parameters, True,
                     failures)


def _check_plain_completeness(failures):
    """data/raw_events.json is written only when every plain source delivered.

    It holds *all* of the plain sources or it is misleading, and the next stage
    cannot tell a partial file from a complete one: it reads the file, finds a
    source that no longer states its rows, and `reconcile_store()` withdraws
    them. The guard was `if plain_rows:`, which is true as soon as one plain
    source succeeds -- so a single kingston_hubs failure wrote a file holding
    chatty_cafe alone and the calendar lost 524 rows over one request, with
    every gate still green.
    """
    p = fetch_sources.plain_sources_missing
    plain = {"kingston_hubs", "greater_dandenong", "gd_libraries",
             "chatty_cafe"}
    checks.check("a complete plain fetch owes nothing",
                 p(plain, plain), [], failures)
    checks.check("one plain source missing is named",
                 p(plain, plain - {"kingston_hubs"}), ["kingston_hubs"],
                 failures)
    checks.check("every missing plain source is named",
                 p(plain, set()), sorted(plain), failures)
    # A --source run is a deliberate single-source fetch, and the run summary
    # already says the file now holds that source alone.
    checks.check("a --source run owes only that source",
                 p(plain, {"chatty_cafe"}, "chatty_cafe"), [], failures)
    checks.check("a --source run that failed still owes it",
                 p(plain, set(), "chatty_cafe"), ["chatty_cafe"], failures)
    # A snapshot source is not a plain source that failed to arrive: without
    # this, `--source ccc` would report an incomplete raw_events.json and exit
    # non-zero on every snapshot debug run.
    checks.check("a snapshot-only run owes nothing",
                 p(plain, set(), "ccc"), [], failures)


def main():
    failures = []
    for name, fn in (("signatures", _check_signatures),
                     ("config shape", _check_config_shape),
                     ("impersonation", _check_impersonation),
                     ("plain-source completeness", _check_plain_completeness)):
        print(f"  {name}")
        fn(failures)
    if failures:
        print(f"\nFAIL: {len(failures)} fetcher convention check(s) failed")
        return 1
    print("\nall fetcher convention checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
