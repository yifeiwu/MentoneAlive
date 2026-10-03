"""Exercise validate_config() and the fetchers' failure signals.

Read-only: no network, no writes. Each case asserts that a config fault or a
broken fetch is reported as such, rather than collapsing into "returned 0
rows" and letting a good snapshot be overwritten.
"""
import copy
import sys
from datetime import date, timedelta

sys.path.insert(0, "scripts")

import jsonio  # noqa: E402
from checks import check as _check  # noqa: E402
from dedupe import (_normalize_raw, load_live_inputs,  # noqa: E402
                    reconcile_store, series_id_for)
import fetch_sources  # noqa: E402
from fetch_sources import (_count_change, _previous_count,  # noqa: E402
                           validate_config)
from jsonio import write_json  # noqa: E402
from webfetch_http import (PartialFetch, enrich_details, make_row,  # noqa: E402
                           set_reporting_source)

set_reporting_source("test")

# `load_config()` is the one reader of sources.yaml's two lists, and it is what
# tags each entry with the group that decides snapshot ownership. This used to
# rebuild that mapping here with `"webfetch" if c in CONFIG["webfetch"]`, which
# is a list membership test -- dict equality, not identity -- so it was quadratic
# and would have tagged two identical entries as belonging to the first list
# either appeared in. The two can no longer disagree about what is configured.
#
# It also used to load sources.yaml itself into a module-level CONFIG, only to
# `del CONFIG` a few lines later -- which read the file off the current working
# directory at import time, for a value nothing used.
GOOD = fetch_sources.load_config()

failures = []


def check(label, actual, expected):
    return _check(label, actual, expected, failures)


def cfg_with(sid, **changes):
    out = copy.deepcopy(GOOD)
    for entry in out:
        if entry.get("id") == sid:
            entry.update(changes)
            return entry
    raise KeyError(sid)


# --- config validation ----------------------------------------------------
CONFIG_CASES = [
    ("the real config is valid", GOOD, 0),
    ("an unknown type is named", [cfg_with("bayside_live", type="nope")], 1),
    ("a missing url is named", [cfg_with("bayside_live", url="")], 1),
    ("a missing snapshot is named", [cfg_with("bayside_live", snapshot="")], 1),
    ("a non-json snapshot is named",
     [cfg_with("bayside_live", snapshot="bayside_auto")], 1),
    ("the seniors year is required",
     [cfg_with("kingston_seniors", year=None)], 1),
    ("the seniors pdf_url is required",
     [cfg_with("kingston_seniors", pdf_url="")], 1),
    ("ccc needs pages or url", [cfg_with("ccc", pages=[], url="")], 1),
    # Duplicating a whole entry duplicates its id AND its snapshot, so both
    # are reported.
    ("a duplicate id is caught", GOOD + [cfg_with("bayside_live")], 2),
    ("two sources claiming one snapshot are caught",
     [cfg_with("kingston_arts", snapshot="bayside_auto.json"),
      cfg_with("bayside_live")], 1),
    ("a missing id is caught", [{"name": "x", "type": "bayside"}], 1),
    # One pass reports every type-independent fault, not just the first.
    ("type-independent faults are all reported",
     [cfg_with("bayside_live", url="", snapshot="", name="")], 3),
    # An unknown type skips only the keys that need a type to know -- and the
    # snapshot check, which is keyed on the config list rather than the type,
    # still runs.
    ("an unknown type still reports the rest",
     [cfg_with("bayside_live", type="nope", snapshot="")], 2),
    # A plain-source entry owns no snapshot, so none is demanded of it.
    ("a plain source needs no snapshot",
     [cfg_with("kingston_hubs", snapshot=None)], 0),
]

for label, entries, expected in CONFIG_CASES:
    errors = validate_config(entries)
    check(label, len(errors), expected)

# The same rule applies to the plain-source half, which shares this validator
# now that there is one dispatcher: a Kingston Hubs calendar with no venue and
# address is a hard failure, because the alternative is a wrong address
# published silently.
def api_errors(**changes):
    return [e for e in validate_config([cfg_with("kingston_hubs", **changes)])
            if "no name AND address" in e]


check("a calendar with no venue/address is named",
      len(api_errors(calendars=["x"], calendar_venues={})), 1)
check("a half-mapped calendar is named",
      len(api_errors(
          calendars=["a1bc2435-21cb-4d26-9b8a-80fc0a7a74df"],
          calendar_venues={"a1bc2435-21cb-4d26-9b8a-80fc0a7a74df":
                           {"name": "Chelsea Activity Hub"}})), 1)
check("a fully mapped calendar is accepted",
      len(api_errors()), 0)


# --- the one failure signal ----------------------------------------------
# A detail crawl that mostly fails is a broken crawl. Before this was a bare
# `continue`, a WAF block on the event pages overwrote a good snapshot with a
# listing-only file -- every venue blank -- and the run stayed green.
class Session:
    """Serves `ok` pages and blocks everything else, with no network."""

    def __init__(self, ok):
        self.ok = ok

    def get(self, url):
        if url in self.ok:
            return type("R", (), {"status_code": 200, "text": "x" * 2000})()
        return type("R", (), {"status_code": 403, "text": "blocked",
                              "content": b"blocked"})()


def rows_with_sources(n):
    return [make_row("test", f"E{i}", f"https://example.invalid/{i}")
            for i in range(n)]


ALL_OK = {f"https://example.invalid/{i}" for i in range(4)}
NONE_OK = set()

DETAIL_CASES = [
    ("all detail pages load", ALL_OK, 4, False),
    ("no detail page loads", NONE_OK, 4, True),
    ("one of four loads", {"https://example.invalid/0"}, 4, True),
    ("two of four load", {"https://example.invalid/0",
                          "https://example.invalid/1"}, 4, False),
    ("nothing to fetch is not a failure", NONE_OK, 0, False),
]

for label, ok_urls, n_rows, should_raise in DETAIL_CASES:
    rows = rows_with_sources(n_rows)
    raised = None
    try:
        enrich_details(Session(ok_urls), rows, None, lambda r, h: None,
                       sleep=0)
    except PartialFetch as e:
        raised = e
    check(label, raised is not None, should_raise)


# --- a truncated-but-successful fetch is reported ------------------------
# None of the above fires for a fetch that succeeds and returns a fraction of
# the real data. That is what a starved --detail-cap produced once: a good
# 221-row snapshot replaced by 65 undated rows, silently.
# _previous_count must read the file BEFORE the write, or "was N" always
# reports the count that was just written and the comparison is vacuous -- so
# this case writes a snapshot of its own into a temp dir and reads that, rather
# than asserting against the committed ccc.json. It used to hard-code 221,
# which meant a legitimate change in how many classes the CCC site lists failed
# the build with a message about a file this suite does not control.
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

with tempfile.TemporaryDirectory() as _tmp:
    snap = Path(_tmp) / "snap.json"

    write_json(snap, [{"name": f"row {i}"} for i in range(100)])
    before = _previous_count(snap)
    check("a snapshot's own row count is read back", before, 100)
    check("a 35% shrink is called out loudly",
          "35% smaller" in _count_change(before, 65), True)
    check("a real snapshot that grew is not called out",
          "smaller" in _count_change(before, 400), False)
    check("the same size is not called out",
          "smaller" in _count_change(before, 100), False)
    check("a missing previous snapshot is not a shrink",
          "smaller" in _count_change(None, 10), False)
    check("an unreadable previous snapshot does not raise",
          _count_change(None, 10), "10 rows (no previous snapshot)")


# --- a store row's justification must not depend on the run date ----------
# The failure this pins is not a wrong value, it is a correct one that goes
# wrong on its own. A weekly series publishes its next 12 occurrences *from the
# run date*, so the stored rows are a snapshot of a rolling window.
# reconcile_store() used to re-expand each listing's prose with the CURRENT run
# date and compare timestamps against the stored ones, which meant the window it
# compared against had slid forward since the rows were written: 97 rows were
# reported unjustified 8 days after a build, 130 after 15, 382 after two months,
# and health_check.py fails the build on any drop. So the build was a time bomb
# with a seven-day fuse, and the run date is not a thing a correct reconciliation
# may read.
#
# series_id made the test a set membership instead, and these cases assert the
# two properties that fix has to have: stable across every future run date, and
# still actually able to notice a withdrawal.
# The live set is built from the committed snapshots and archive only, with
# `use_raw=False` excluding data/raw_events.json. It used to include that file,
# which made this the one suite in the runner whose result depended on whatever
# the last fetch had left behind: `fetch_sources.py --source <id>` overwrites it
# with that one source, so the "live" set silently became one source wide and
# these cases started failing for reasons that had nothing to do with the code
# under test. The file is gitignored, so it is not part of what this suite is
# entitled to depend on.
_STORE = [_normalize_raw(dict(r))
          for r in jsonio.read_json("data/events.json")["rows"]]
_LIVE = [_normalize_raw(dict(r))
         for r in load_live_inputs(quiet=True, use_raw=False)]
_TODAY = date(2026, 10, 1)


def dropped_on(day):
    return len(reconcile_store(list(_STORE), list(_LIVE), today=day,
                               report=False)[1])


check("the committed store is reconciled clean today", dropped_on(_TODAY), 0)
# One week, one month, one quarter, one year out: the answer must not move.
check("reconciliation does not drift with the run date",
      [dropped_on(_TODAY + timedelta(days=d))
       for d in (7, 30, 91, 365)], [0, 0, 0, 0])

# ...and the check must not have been defanged to achieve that. Withdrawing a
# single series from the live set must take exactly that series' rows down. Both
# the series and the expected count are derived rather than written down: this
# used to name "Bayside Farmers" and assert `== 12`, so it would fail the moment
# that series gained a session -- or vanished, taking the `next()` with it.
# Anchor on the largest multi-row series this source has, so the case is about a
# series rather than a one-off (the first bayston row in the store is a single
# occurrence, and withdrawing it would assert almost nothing).
_by_sid = {}
for _r in _STORE:
    if _r.get("source_id") == "bayside_live":
        _by_sid.setdefault(_r.get("series_id") or series_id_for(_r),
                           []).append(_r)
_sid, _same = max(_by_sid.items(), key=lambda kv: len(kv[1]))
_without = [r for r in _LIVE
            if (r.get("series_id") or series_id_for(r)) != _sid]
_lost = reconcile_store(list(_STORE), _without, today=_TODAY, report=False)[1]
check("the series this case withdraws is a real multi-row series",
      len(_same) > 1, True)
check("a withdrawn series is noticed", len(_lost), len(_same))
check("and only that series goes", sorted({r["name"] for r in _lost}),
      sorted({r["name"] for r in _same}))

# A source that is absent entirely is out of season or broken, not withdrawn,
# and must not take its existing rows down with it.
_absent = [r for r in _LIVE if r.get("source_id") != "kingston_seniors"]
check("an absent source does not withdraw its rows",
      len(reconcile_store(list(_STORE), _absent, today=_TODAY,
                          report=False)[1]), 0)


if failures:
    print(f"\nfailure_signals: {len(failures)} case(s) FAILED")
    raise SystemExit(1)
