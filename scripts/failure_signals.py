"""Exercise validate_config() and the fetchers' failure signals.

Read-only: no network, no writes. Each case asserts that a config fault or a
broken fetch is reported as such, rather than collapsing into "returned 0
rows" and letting a good snapshot be overwritten.
"""
import copy
import sys

sys.path.insert(0, "scripts")

import yaml  # noqa: E402

from webfetch_http import (PartialFetch, enrich_details, make_row,  # noqa: E402
                           report, set_reporting_source)
from webfetch_sources import (_count_change, _previous_count,  # noqa: E402
                              validate_config)

set_reporting_source("test")

with open("scripts/sources.yaml", encoding="utf-8") as f:
    GOOD = yaml.safe_load(f)["webfetch"]

failures = []


def cfg_with(sid, **changes):
    out = copy.deepcopy(GOOD)
    for entry in out:
        if entry.get("id") == sid:
            entry.update(changes)
            return entry
    raise KeyError(sid)


def check(label, actual, expected):
    ok = actual == expected
    print(f"{'ok  ' if ok else 'FAIL'} {label}"
          + ("" if ok else f"\n       actual:   {actual!r}"
                          f"\n       expected: {expected!r}"))
    if not ok:
        failures.append(label)


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
    # An unknown type skips only the keys that need a type to know.
    ("an unknown type still reports the rest",
     [cfg_with("bayside_live", type="nope", snapshot="")], 2),
]

for label, entries, expected in CONFIG_CASES:
    errors = validate_config(entries)
    ok = len(errors) == expected
    print(f"{'ok  ' if ok else 'FAIL'} {label}"
          + ("" if ok else f"\n       expected {expected} error(s), got "
                          f"{len(errors)}: {errors}"))
    if not ok:
        failures.append(label)


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
    ok = (raised is not None) == should_raise
    print(f"{'ok  ' if ok else 'FAIL'} {label}"
          + ("" if ok else f"\n       expected "
                          f"{'a PartialFetch' if should_raise else 'success'}, "
                          f"got {raised!r}"))
    if not ok:
        failures.append(label)


# --- a truncated-but-successful fetch is reported ------------------------
# None of the above fires for a fetch that succeeds and returns a fraction of
# the real data. That is what a starved --detail-cap produced once: a good
# 221-row snapshot replaced by 65 undated rows, silently.
# _previous_count must read the file BEFORE the write, or "was N" always
# reports the count that was just written and the comparison is vacuous.
SNAP = "scripts/webfetch_snapshots/ccc.json"
before = _previous_count(SNAP)
check("the committed ccc snapshot reads 221 rows", before, 221)
check("a 70% shrink is called out loudly",
      "70% smaller" in _count_change(before, 65), True)
check("a real snapshot that grew is not called out",
      "smaller" in _count_change(before, 400), False)
check("the same size is not called out",
      "smaller" in _count_change(before, 221), False)
check("a missing previous snapshot is not a shrink",
      "smaller" in _count_change(None, 10), False)
check("an unreadable previous snapshot does not raise",
      _count_change(None, 10), "10 rows (no previous snapshot)")


if failures:
    print(f"\nfailure_signals: {len(failures)} case(s) FAILED")
    raise SystemExit(1)
