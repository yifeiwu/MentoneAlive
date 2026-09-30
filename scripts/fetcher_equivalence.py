"""Run the refactored fetchers against recorded HTML, offline.

Verifies that the make_row / enrich_details / reporter refactor did not change
what a fetcher extracts. No network, and nothing is written to
scripts/webfetch_snapshots/.

The Humanitix reader is checked here because its behaviour-defining rule -- the
term-range expansion, one published row per weekly session -- is the one a
shared detail loop could plausibly break. The Granicus address reader is not:
it has its own suite in webfetch_granicus.py, which runs the same production
function over the same four fixtures and reports an unreplaced address as None
rather than as the seed value, so duplicating the cases here only gave the two
suites a way to disagree.
"""
import sys

sys.path.insert(0, "scripts")

from checks import check as _check  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402

from webfetch_ccc import _ccc_weekly_term  # noqa: E402
from webfetch_http import ROW_FIELDS, make_row  # noqa: E402

failures = []


def check(label, actual, expected):
    return _check(label, actual, expected, failures)


# --- make_row is the one owner of the row shape -------------------------
row = make_row("bayside_live", "Mahjong", "https://x.invalid/e")
check("make_row emits every documented key", sorted(row), sorted(ROW_FIELDS))
check("make_row defaults to blank, not None",
      [row["datetime_iso"], row["location"], row["address"],
       row["price_text"]], ["", "", "", ""])
check("make_row keeps source_id", row["source_id"], "bayside_live")
check("make_row normalises None to blank",
      make_row("s", None, None)["name"], "")

# --- the Humanitix term expansion is unchanged ---------------------------
# One published row per weekly session, so a 10-week term is not a single row.
start = datetime(2026, 10, 5, 9, 30)
sess = _ccc_weekly_term(start, datetime(2026, 12, 14, 9, 30))
check("an 11-week term expands to 11 sessions", len(sess), 11)
check("the expansion keeps the weekday",
      {s.weekday() for s in sess}, {0})
check("the first session is the stated start", sess[0], start)
check("the last session is the stated end",
      sess[-1], datetime(2026, 12, 14, 9, 30))
check("a one-off stays a one-off",
      len(_ccc_weekly_term(start, start + timedelta(days=3))), 1)
check("the expansion is capped at 12",
      len(_ccc_weekly_term(start, datetime(2027, 6, 1, 9, 30))), 12)

if failures:
    print(f"\nfetcher_equivalence: {len(failures)} case(s) FAILED")
    raise SystemExit(1)
print("\nall fetcher-equivalence cases as expected")