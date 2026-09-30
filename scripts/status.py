"""Listing status: sold out, fully booked, cancelled, or a drop-in service.

Two questions a reader needs answered before they plan a trip, neither of
which the raw listing answers on its face:

* **Can I still go?** Venues prefix the status onto the listing rather than
  putting it in a field. Granicus writes the whole status line into the date
  field -- "Sold out: Wednesday, 30 September 2026 | 11:00 AM to 12:00 PM" --
  so the time is parsed out of a sentence that also says the event is
  unavailable. Greater Dandenong Libraries puts it in the title
  ("FULLY BOOKED - Card Making"). A cancelled event is worse than absent: it
  looks bookable until you arrive.

* **Is this an event at all?** A community centre's "Takeaway Meals" runs
  Tuesday to Friday, 10am-2pm. It is a service with an opening window, not a
  session to attend, and the calendar gave it four slots a week. These are not
  commercial -- the meals are subsidised -- so they get their own flag rather
  than being mislabelled as a pub promotion.

Both stay visible but badged in the UI, except cancelled which is hidden
by default behind the same checkbox, and both are
recorded in events.json and the CSV export so the distinction survives.
"""
import re

# Status is written into the title by some venues, so the name counts too --
# but only as a whole word: "booked" alone appears in ordinary prose.
SOLD_OUT_RE = re.compile(r"\bsold\s*out\b|\bselling\s*(?:fast|out)\b", re.I)
FULLY_BOOKED_RE = re.compile(r"\bfull(?:y)?\s*booked\b|\bno\s*(?:places|spots)\b",
                             re.I)
CANCELLED_RE = re.compile(r"\bcancell?ed\b|\bcancellation\b|\bcancel\b|\bpostponed\b", re.I)

# "Sold out: Wednesday, 30 September..." -- the status leads the listing's
# own date field, so anchor on the start of the string.
_LEADING_STATUS_RE = re.compile(
    r"^\s*(sold\s*out|fully\s*booked|booked\s*out|booked\s*full|cancell?ed|"
    r"cancellation|cancel|postponed|selling\s*(?:fast|out)|no\s*(?:places|spots))\b[:\-\u2013]?", re.I)

# A drop-in service rather than a bookable session. Anchored on the phrase a
# venue actually uses to describe one, not on a bare noun, so "meals" in a
# cooking workshop's blurb is not caught. "Community meals" was tried and
# removed: it matches "Community Meals Cooking Class", which is a class.
SERVICE_RE = re.compile(
    r"\btake\s*away\s*meals?\b|\btakeaway\s*meals?\b|\bmeals?\s*(?:provided|"
    r"available|supplied)\b|\bmeals?\s*on\s*wheels\b", re.I)

STATUS_LABELS = {
    "sold_out": "SOLD OUT",
    "fully_booked": "FULLY BOOKED",
    "cancelled": "CANCELLED",
}


def event_status(row):
    """Return (status, detail). status is '' when the listing is open.

    detail is the raw status phrase, kept so the UI can say *why* a listing
    is hidden rather than only that something is.
    """
    name = row.get("name") or ""
    text = row.get("datetime_text") or ""
    desc = row.get("description") or ""

    # The date field leads with the status, which is the authoritative signal:
    # it is the venue's own wording for the listing, not prose scraped from
    # somewhere else on the page.
    m = _LEADING_STATUS_RE.match(text)
    if m:
        phrase = m.group(1)
        low = phrase.lower()
        if low.startswith("cancel") or low.startswith("postponed"):
            return "cancelled", phrase
        if "booked" in low:
            return "fully_booked", phrase
        return "sold_out", phrase

    for rx, status in ((CANCELLED_RE, "cancelled"),
                       (SOLD_OUT_RE, "sold_out"),
                       (FULLY_BOOKED_RE, "fully_booked")):
        # Names are matched whole-phrase; a blurb is matched too, but a
        # cancelled word in one event's description does not cancel another.
        #
        # The detail is the phrase that actually matched. `rx.search(name or
        # desc)` was re-run to get it, which reads the name when the match was
        # in the description -- and raises AttributeError when `name` is empty
        # and the match was only in the description, because `or` then picks
        # the empty name. A real listing hit this: a fetched row with a blank
        # name and "cancelled" in its description took down build_site.py.
        hit = rx.search(name) or rx.search(desc)
        if hit:
            return status, hit.group(0)
    return "", ""


def is_ongoing_service(row):
    """Return (flag, reason) when the listing is a service, not an event."""
    name = row.get("name") or ""
    desc = row.get("description") or ""
    m = SERVICE_RE.search(name)
    if not m:
        m = SERVICE_RE.search(desc)
    if not m:
        return False, ""
    return True, f"service:{re.sub(r'\\s+', ' ', m.group(0)).strip().lower()}"


if __name__ == "__main__":
    # Asserted, not printed: these classifications decide what a reader does
    # not see, and a false positive hides a real event.
    TESTS = [
        # (row, expected status, expected is_service)
        # The venue's own status line leads the listing's date field.
        ({"name": "Clowning Workshop with Trash Test Dummies",
          "datetime_text": "Sold out: Wednesday, 30 September 2026 | 11:00 AM to 12:00 PM"},
         "sold_out", False),
        ({"name": "Mosaics for Beginners",
          "datetime_text": "Fully booked: Monday, 28 September 2026 | 06:30 PM to 08:00 PM"},
         "fully_booked", False),
        # Greater Dandenong Libraries puts it in the title.
        ({"name": "FULLY BOOKED - Card Making - Libraries After Dark",
          "datetime_text": "2026-10-01 (to 01 Oct 2026)"},
         "fully_booked", False),
        ({"name": "Cancelled Craft Session",
          "datetime_text": "Cancelled: Friday, 9 October 2026 | 10:00 AM"},
         "cancelled", False),
        # A service with an opening window, not a session to attend.
        ({"name": "Takeaway Meals",
          "description": "Take home delicious, nutritious meals for one. "
                         "Available Tuesday to Friday, 10am-2pm."},
         "", True),
        # --- must NOT fire -----------------------------------------------
        ({"name": "Community Meals Cooking Class",
          "description": "Learn to cook hearty family meals on a budget."},
         "", False),
        ({"name": "Book Club", "description": "Booking opens next week."},
         "", False),
        ({"name": "Yoga", "description": "Mats provided. 6:30pm."},
         "", False),
        # "booked" in ordinary prose is not a booking status.
        ({"name": "Storytime", "description": "A pre-booked favourite."},
         "", False),
        ({"name": "Zumba Gold", "datetime_text": "Thursday 08 October, 11:30 AM"},
         "", False),
        # A fetched row with no title and the word in its body. The detail
        # was re-read from `name or desc`, which with an empty name is the
        # empty name, and took build_site.py down with an AttributeError.
        ({"name": "", "description": "This session has been cancelled."},
         "cancelled", False),
        ({"name": "", "description": "Now fully booked, sorry."},
         "fully_booked", False),
    ]
    failures = []
    for row, expected_status, expected_service in TESTS:
        status, _detail = event_status(row)
        service, _reason = is_ongoing_service(row)
        ok = status == expected_status and service == expected_service
        print("%s %-55s status=%-13s service=%s"
              % ("ok " if ok else "BAD", (row.get("name") or "")[:55],
                 status or "-", service))
        if not ok:
            failures.append((row.get("name"), expected_status, expected_service,
                             status, service))
    if failures:
        print("\n%d status/service failure(s):" % len(failures))
        for name, es, ev, gs, gv in failures:
            print(f"  {name!r}: expected ({es!r}, service={ev}), "
                  f"got ({gs!r}, service={gv})")
        raise SystemExit(1)
    print(f"\nall {len(TESTS)} status/service classifications as expected")
