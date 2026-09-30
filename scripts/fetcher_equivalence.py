"""Run the refactored fetchers against recorded HTML, offline.

Verifies that the make_row / enrich_details / reporter refactor did not change
what a fetcher extracts. No network, and nothing is written to
scripts/webfetch_snapshots/.

The Granicus and Humanitix readers are checked here because their two
behaviour-defining rules -- the address form, and the term-range expansion --
are the ones a shared detail loop could plausibly break.
"""
import sys

sys.path.insert(0, "scripts")

from webfetch_ccc import _ccc_weekly_term  # noqa: E402
from webfetch_granicus import _apply_granicus_detail  # noqa: E402
from webfetch_http import ROW_FIELDS, make_row  # noqa: E402
from datetime import datetime  # noqa: E402

failures = []


def check(label, actual, expected):
    ok = actual == expected
    print(f"{'ok  ' if ok else 'FAIL'} {label}"
          + ("" if ok else f"\n       actual:   {actual!r}"
                          f"\n       expected: {expected!r}"))
    if not ok:
        failures.append(label)


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
      len(_ccc_weekly_term(start, start + __import__("datetime").timedelta(
          days=3))), 1)
check("the expansion is capped at 12",
      len(_ccc_weekly_term(start,
                           datetime(2027, 6, 1, 9, 30))), 12)

# --- the Granicus address reader still replaces a venue-name address ----
def address_after(html):
    r = make_row("kingston_arts", "Event", "https://x.invalid/e",
                 location="Kingston Arts Centre",
                 address="Kingston Arts Centre")
    _apply_granicus_detail(r, html)
    return r["address"]


check("a tag-separated address is read",
      address_after("<div>Kingston Arts Centre</div><div>979 Nepean "
                    "Highway</div><div>Moorabbin 3189</div>"),
      "979 Nepean Highway, Moorabbin 3189")
check("an inline address is read",
      address_after("<p>1 Example St, Cheltenham, VIC 3192</p>"),
      "1 Example St, Cheltenham 3192")
check("a title plus a year is not an address",
      address_after("<p>Kerri Wilson McConchie, Refugia 2026</p>"),
      "Kingston Arts Centre")
check("a page with no address leaves the seed in place",
      address_after("<div>nothing here</div>"),
      "Kingston Arts Centre")

if failures:
    print(f"\nfetcher_equivalence: {len(failures)} case(s) FAILED")
    raise SystemExit(1)
print("\nall fetcher-equivalence cases as expected")
