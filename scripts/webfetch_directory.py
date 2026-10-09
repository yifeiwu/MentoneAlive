"""OpenCities local directories (Kingston's community groups).

One fetcher for the platform rather than for the council, the same way
`webfetch_granicus.py` serves both kingston_council and kingston_arts: adding a
second council's directory is a `sources.yaml` entry, not a second scraper. The
platform is OpenCities / Granicus "Seamless CMS" -- an ASP.NET WebForms page
whose listing is server-rendered inside a Telerik search control.

What this source is for
-----------------------

A community group is not an event. It has no date; it has a *schedule* --
"every third Monday at 10am" -- and the calendar needs it as twelve dated rows.
So the listing, which states no time at all, is only half the crawl: every
entry's own page has to be opened, because that is the only place the schedule
is written down. See `_apply_directory_detail`.

Two ways a group states when it meets, and they are not equally trustworthy:

* The **description prose**, which the group itself writes and which often says
  exactly the right thing ("monthly meetings ... on the third Monday of each
  month starting at 10.00am"). Preferred, because it is the group's own claim
  about its own schedule.
* A per-weekday **Hours** table, present on roughly half the directory. It is
  the venue's opening hours more often than it is a meeting, and the two are
  indistinguishable by shape -- the golf club lists 07:00-18:00 seven days a
  week. `_schedule_from_hours` is where that is filtered.

A group with a place but no time, or a time but no place, is dropped. Neither
half is actionable on a calendar, and the alternative is a row a reader cannot
act on -- the same reasoning as `webfetch_granicus.drop_venueless()` and D20.

The pagination trap
-------------------

This listing is 12 pages deep and paginates by **ASP.NET postback only**.
`?page=2` and `?oc_page=2` are both accepted and both ignored: the server
returns page 1. Only a POST carrying `__SEAMLESSVIEWSTATE` (a ~46 KB gzipped
blob, reissued on every response) plus the pager's own control names moves the
page. The control names are `ctl10$ctl00$ctl07` and so on, which are generated
by the template and change when a control is added above the pager -- so they
are discovered from the markup rather than hard-coded.

The failure this has to catch: a POST with a wrong field name is *accepted* and
returns page 1 again, silently. So the collected URLs are counted against the
"N Result(s) Found" total the listing prints, and a shortfall is a PartialFetch
rather than a snapshot of the first ten groups.
"""
import re
from datetime import date

from bs4 import BeautifulSoup

from recurrence import materialise
from webfetch_http import (MIN_CRAWL_DELAY, Pacer, PartialFetch, _hhmm,
                           _hhmm_str, get, make_row, paged_listing, report,
                           set_reporting_source)
from venues import needs_address

# The interval this source was actually running at, so `crawl_delay` can
# override it and nothing else can change it by accident.
DEFAULT_CRAWL_DELAY = 0.15

# --- listing markup --------------------------------------------------------
CARD = "div.list-item-container article"
CARD_NAME = "h2.list-item-title"
CARD_ADDRESS = "p.list-item-address"
CARD_TAGS = "div.tagged-as-list div.text li span"

# The card's summary is a bare `<p>` with no class attribute at all -- not
# `class=""`, the attribute is absent, and the template even emits it with a
# stray space (`<p >`). So it cannot be selected by name and is found by
# elimination: the only direct-child <p> of the card's link with no class.

# "117 Result(s) Found"
RESULTS_RE = re.compile(r"(\d[\d,]*)\s*Result")

# --- detail markup ---------------------------------------------------------
DETAIL_COLUMN = "div.grid.obj-directory > div.col-m-8"
LOCATION_HEADING = "h2.sub-title"
LOCATION_CLASS = "sub-title"
HOURS_BLOCK = ".side-box-section.hours-details"
HOURS_DAY = "span.hours-day"
HOURS_DAY_CLASS = "hours-day"
HOURS_TIMES_CLASS = "hours-time-list"
# "07:00 AM-10:00 AM" -- the dash is U+2013 on the page and can arrive as a
# replacement character through some encodings, so both are accepted.
HOURS_RANGE_RE = re.compile(
    r"(\d{1,2}):(\d{2})\s*([AaPp])\.?[Mm]\.?"
    r"\s*[\u2013\u2014\ufffd-]\s*"
    r"(\d{1,2}):(\d{2})\s*([AaPp])\.?[Mm]\.?")

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
        "sunday")
_DAY_INDEX = {d: i for i, d in enumerate(DAYS)}

# How long a stated meeting may run before it reads as the venue's opening
# hours rather than a session: the church hall's 07:00-18:00 is not a meeting
# anyone attends for eleven hours.
MAX_MEETING_HOURS = 6
# ...and a "meeting" on this many days is a venue, not a group.
MAX_MEETING_DAYS = 4


def _fetch_listing_pages(session, cfg):
    """Every listing card on every page, plus the total the site claims.

    The walk itself -- the postback, the re-read viewstate, the check that the
    pager actually moved -- is `webfetch_http.paged_listing`, shared with the
    council events listing, which paginates the same way for the same reason.
    What is left here is this source's own reading of a card.
    """
    url = cfg["url"]
    listing = paged_listing(session, cfg)

    cards, seen = [], set()

    def take(page):
        new = 0
        for card in page.select(CARD):
            link = card.select_one("a[href]")
            title = card.select_one(CARD_NAME)
            if not link or not title:
                continue
            href = link["href"]
            if href in seen:
                continue
            seen.add(href)
            cards.append((href, card))
            new += 1
        return new

    for page in listing.pages:
        if not take(page):
            # A page with no cards at all after page 1 means the walk is being
            # served something other than the listing.
            break
    total = RESULTS_RE.search(listing.pages[-1].get_text(" ", strip=True))
    claimed = int(total.group(1).replace(",", "")) if total else None
    if claimed is not None and len(cards) < claimed:
        raise PartialFetch(
            f"collected {len(cards)} of the {claimed} entries {url} says it "
            f"lists -- the walk stopped early, so publishing would replace a "
            f"good snapshot with a fraction of the directory")
    report(f"listing: {len(cards)} entries"
           + (f" of {claimed} claimed" if claimed else "")
           + (f" over {len(listing.pages)} of {listing.claimed} pages"
              if listing.claimed else ""))
    return cards


def _card_summary(card):
    """The card's free-text summary.

    The only classless <p> among the card's own paragraphs, and the only way to
    tell it from the address line: selecting `p` and taking the first would
    return the street address, which then reads as the description and reaches
    the classifier as prose about a road.
    """
    for p in card.select("p"):
        if not (p.get("class") or []):
            text = p.get_text(" ", strip=True)
            if text:
                return text
    return ""


def _card_address(card):
    el = card.select_one(CARD_ADDRESS)
    return el.get_text(" ", strip=True) if el else ""


def _split_address(text):
    """(venue, street, suburb, postcode) from the detail page's Location block.

    Read line by line rather than with one pattern over the block, because the
    page does not write the address in a single fixed shape and a pattern that
    fits one loses the rest. What is actually constant is the *order*: a venue
    name, then the street, then a line ending in the suburb and postcode -- and
    the last line is the only reliable anchor on all three, because the street
    is sometimes two lines joined by a comma ("Unit 8, 19-23 Kylie Place") and
    sometimes has no house number at all ("Fraser Ave").

    Anything in brackets is cut from the street: it is a landmark description,
    not part of the address, and publishing it would send a reader looking for a
    road that does not exist.

    Returns None when there is no street, or no suburb to go with a postcode. A
    location stated as a postcode alone ("6-7/556 North Road, 3204") is not a
    place to send anyone, which is D20 -- and dropping it here is preferable to
    publishing an address the suburb filter cannot place.
    """
    lines = [" ".join(ln.split()) for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln and ln.lower() != "view map"]
    if not lines:
        return None

    tail = None
    for i in range(len(lines) - 1, -1, -1):
        m = re.search(r"(?:VIC\.?\s*)?(3\d{3})\s*$", lines[i])
        if m:
            # The suburb is only the last comma-segment before the postcode.
            # On the detail page that is the whole line; on a listing card the
            # same line also carries the venue and street, and reading the lot
            # as a suburb would fail on the comma alone.
            prefix = lines[i][:m.start()].strip(" ,")
            tail = (i, m.start(), prefix, prefix.rsplit(",", 1)[-1].strip(),
                    m.group(1))
            break
    if tail is None:
        return None
    idx, at, prefix, suburb, postcode = tail
    if not re.fullmatch(r"[A-Za-z][A-Za-z .'-]*", suburb or ""):
        return None

    if idx == 0:
        # Everything on one line: "Venue, 12 Katoomba Street, Hampton East,
        # 3188". The suburb has already been taken off the end, so what remains
        # is a venue and a street separated by a comma -- or a bare street, when
        # the listing gave no venue name.
        segments = [s.strip() for s in prefix.split(",")]
        rest = ", ".join(segments[:-1]).strip() if len(segments) > 1 else prefix
        if "," in rest:
            venue, street = rest.rsplit(",", 1)
            venue, street = venue.strip(), street.strip()
        else:
            venue, street = "", rest
    else:
        head = lines[:idx]
        venue = head[0].rstrip(" ,") if head else ""
        street = ", ".join(ln.rstrip(" ,") for ln in head[1:])
        if not street:
            # No venue line: the first line is the street itself.
            street, venue = venue, ""
    street = re.split(r"\s*\(", street)[0].strip(" ,")
    if not street:
        return None
    return venue, street, suburb, postcode


def _schedule_from_hours(hours_html):
    """A schedule string from the detail page's per-weekday hours table.

    What the table actually holds is not one weekly window per day but a list of
    individual sessions -- Chelsea Sports Club lists seven separate Sunday
    entries, 4-9pm and 4-8pm repeated, plus one 12pm-10pm -- with no date on
    any of them. So the table states the window a group is active on a weekday,
    not what a single session is.

    That makes aggregation the only honest reading, and it is also what
    distinguishes a meeting from opening hours: take the earliest start and the
    latest end across a day's sessions, and reject the whole day when that
    window is longer than MAX_MEETING_HOURS. Radio Carrum's Sunday is 09:00,
    12:00-21:00 five times over and 19:00-21:00, which aggregates to twelve
    hours and is the station's opening hours, not a meeting. Chelsea Sports
    Club's aggregates to five (16:00-21:00), which is a match.

    Returning None when nothing survives is the case this exists for: the
    Australasian Golf Club's 07:00-18:00 daily table would otherwise publish as
    a daily eleven-hour "group meeting", which is worse than publishing nothing.

    Individual sessions already over MAX_MEETING_HOURS are dropped before the
    aggregate is taken, so one long outlier (a festival session, a late hire)
    does not make an otherwise ordinary day fail.

    The walk is by document position, not by list item, and that is the whole
    fix. The page never closes its `<li>` elements, so BeautifulSoup nests every
    weekday inside the first one and `.hours-list > li` matches exactly one item
    -- the first weekday, which is usually Sunday and usually `Closed`. The
    reader therefore saw either nothing or a single wrong day, and two of the
    rules above could never fire: `MAX_MEETING_HOURS` had nothing to aggregate
    and `MAX_MEETING_DAYS` could not be exceeded by a one-element list. They were
    written for the case they are now in, and this restores it.

    Assigning each time list to the most recent `.hours-day` before it is the
    only reading here that is per-day. Scoping a day item's own subtree does not
    work, because the next weekday is a *descendant* of the previous one: Monday
    would collect Tuesday's sessions.
    """
    soup = BeautifulSoup(hours_html, "html.parser")
    by_day = {}
    day, closed = None, False
    for node in soup.descendants:
        classes = getattr(node, "get", lambda *_a, **_k: None)(
            "class") or []
        if HOURS_DAY_CLASS in classes:
            day = _DAY_INDEX.get(node.get_text(" ", strip=True).lower()[:9])
            closed = False
            continue
        if "hours-status" in classes and "closed" in classes:
            closed = True
            continue
        if HOURS_TIMES_CLASS not in classes or day is None or closed:
            continue
        for entry in node.select("li"):
            m = HOURS_RANGE_RE.search(entry.get_text(" ", strip=True))
            if not m:
                continue
            start_hm = _hhmm(m.group(1), m.group(2), m.group(3), clamp=False)
            end_hm = _hhmm(m.group(4), m.group(5), m.group(6), clamp=False)
            if start_hm is None or end_hm is None:
                continue
            start_m = start_hm[0] * 60 + start_hm[1]
            end_m = end_hm[0] * 60 + end_hm[1]
            if (end_m - start_m) % (24 * 60) > MAX_MEETING_HOURS * 60:
                continue
            by_day.setdefault(day, []).append(
                (start_m, _hhmm_str(m.group(1), m.group(2), m.group(3),
                                    clamp=False),
                 end_m, _hhmm_str(m.group(4), m.group(5), m.group(6),
                                  clamp=False)))

    days = []
    for day in sorted(by_day):
        sessions = by_day[day]
        low = min(s[0] for s in sessions)
        high = max(s[2] for s in sessions)
        if high - low > MAX_MEETING_HOURS * 60:
            report(f"  {DAYS[day]}: {(high - low) // 3600}h window -- "
                   f"opening hours, not a meeting", level="debug")
            continue
        start = next(s[1] for s in sessions if s[0] == low)
        end = next(s[3] for s in sessions if s[2] == high)
        days.append(f"every {DAYS[day].capitalize()} {start} - {end}"
                    if start != end else
                    f"every {DAYS[day].capitalize()} {start}")
    if not days:
        return None
    if len(days) > MAX_MEETING_DAYS:
        report(f"  hours table spans {len(days)} days -- a venue's opening "
               f"hours, not a group's meeting", level="debug")
        return None
    return ", ".join(days)


def _apply_directory_detail(row, html):
    """Fill a group row from its own page. The schedule lives here, nowhere else."""
    soup = BeautifulSoup(html, "html.parser")
    column = soup.select_one(DETAIL_COLUMN) or soup

    # The description is the paragraphs *before* the Location heading. Taking
    # every <p> in the column instead would add the venue's street address and
    # the words "View Map" to the description, which is both shown to a reader
    # and fed to the classifier.
    prose = []
    for el in column.find_all(["p", "h2"]):
        # Compare against the class, not the selector: `el.get("class")` is
        # ["sub-title"], and testing the "h2.sub-title" selector string against
        # that list is always False, so the loop never stopped and every
        # description ended with the venue's street address and the words
        # "View Map" -- shown to a reader and fed to the classifier.
        if el.name == "h2" and LOCATION_CLASS in (el.get("class") or []):
            break
        if el.name == "p":
            text = el.get_text(" ", strip=True)
            if text:
                prose.append(text)
    if prose:
        row["description"] = " ".join(prose)

    heading = column.select_one(LOCATION_HEADING)
    if heading:
        block = heading.find_next("p")
        parts = _split_address(block.get_text("\n", strip=True)
                               if block else "")
        if parts:
            venue, street, suburb, postcode = parts
            row["location"] = venue or street
            row["address"] = f"{street}, {suburb} {postcode}"

    hours = soup.select_one(HOURS_BLOCK)
    if hours:
        schedule = _schedule_from_hours(str(hours))
        if schedule:
            row["_hours_schedule"] = schedule


def _usable_schedule(row):
    """(schedule_text, from_hours) when this group states a time, else None.

    Prose first, then the hours table. The test is the real parser rather than a
    look for a digit: a schedule that parses is one that will publish, and one
    that does not is one that silently deletes a group from the calendar.

    Two rejections beyond the parser's own:

    * **No start time.** `materialise` accepts a days-only schedule and would
      return midnight rows -- "every Sunday morning" is not an event a reader can
      turn up to.
    * **Nothing in the future.** A church whose description mentions "Sunday 22
      December" gets that occurrence dated 2024-12-22, two years in the past,
      because the year is inferred rather than stated. Publishing that puts a
      2024 row in a 2026 calendar; `prune_old` would remove it later and the
      health check would read it first. The run date is not a thing this source
      is entitled to compare against -- but a *past* date is never a meeting
      anyone can attend.
    """
    today = date.today()
    probe = dict(row)
    probe["datetime_text"] = ""

    def publishable(made):
        return bool(made) and any(
            r["datetime_iso"][11:16] != "00:00"
            and date.fromisoformat(r["datetime_iso"][:10]) >= today
            for r in made)

    made, _spec, _reason = materialise(probe, row.get("description", ""))
    if publishable(made):
        return row.get("description", ""), False
    hours = row.get("_hours_schedule")
    if hours:
        made, _spec, _reason = materialise(probe, hours)
        if publishable(made):
            return hours, True
    return None


def fetch_directory(cfg, session, detail_cap=None):
    """Fetch an OpenCities local directory as recurring meetings.

    `detail_cap` bounds how many of the entries' own pages are opened, because
    that is one request per group and the directory's size is not ours to
    choose. Reaching the cap is reported, since a directory that grew past it
    would otherwise publish a fraction of itself without saying so.
    """
    sid = cfg["id"]
    set_reporting_source(sid)
    cards = _fetch_listing_pages(session, cfg)

    # One pacer for the whole source, so the budget covers the detail pages and
    # the listing walk together. `crawl_delay` was validated for this source and
    # read by nobody: the only delay here was a `time.sleep(0.15)` at the bottom
    # of the detail loop, which the five `continue`s above it skipped.
    pacer = Pacer(cfg.get("crawl_delay"), floor=MIN_CRAWL_DELAY,
                  default=DEFAULT_CRAWL_DELAY)

    rows, dropped, attempted, opened = [], {"no place": 0, "no time": 0,
                                            "fetch failed": 0}, 0, 0
    for href, card in cards:
        if detail_cap is not None and opened >= detail_cap:
            report(f"detail cap {detail_cap} reached; "
                   f"{len(cards) - len(rows) - sum(dropped.values())} "
                   f"entries left unopened", level="warn")
            break
        name = card.select_one(CARD_NAME).get_text(" ", strip=True)
        tags = [t.get_text(" ", strip=True) for t in card.select(CARD_TAGS)]
        row = make_row(sid, name, href,
                       location=_card_address(card),
                       address=_card_address(card),
                       description=_card_summary(card))
        row["source_types"] = [t for t in tags if t]

        attempted += 1
        # The pause belongs immediately before the request, not at the bottom of
        # the loop: five `continue`s stand between the two, and the first of them
        # is a detail page that did not load -- so a failed page cost no pause and
        # the next request went straight out behind it. That is the branch this
        # host's 404s actually take.
        if not pacer.take():
            report(f"stopped at the run's {pacer.budget}-request budget; "
                   f"{len(cards) - len(rows) - sum(dropped.values())} entries "
                   f"left unopened", level="warn")
            break
        html = get(session, href, retries=2, min_len=2000)
        if not html:
            dropped["fetch failed"] += 1
            report(f"{name!r}: detail page did not load", level="warn")
            continue
        opened += 1
        _apply_directory_detail(row, html)

        # Eleven of the 117 detail pages carry no Location block at all, so the
        # listing card's own address is the fallback. The card states a street
        # rather than a venue name, which is the opposite trade from the Granicus
        # source -- there the card holds only a venue and the detail page holds
        # the street -- so neither source is usable without the other.
        if not _split_address(row.get("address") or ""):
            card_parts = _split_address(_card_address(card))
            if card_parts:
                venue, street, suburb, postcode = card_parts
                row["location"] = row["location"] or venue or street
                row["address"] = f"{street}, {suburb} {postcode}"

        if not (row.get("address") or "").strip() and needs_address(row):
            dropped["no place"] += 1
            continue
        found = _usable_schedule(row)
        if not found:
            dropped["no time"] += 1
            report(f"dropped {name!r}: states a place but no meeting time",
                   level="debug")
            continue
        schedule, from_hours = found
        if from_hours:
            made, _spec, reason = materialise(row, schedule)
            if not made:
                dropped["no time"] += 1
                report(f"dropped {name!r}: {reason}", level="warn")
                continue
            rows.extend(made)
            report(f"  {name}: dated from the hours table "
                   f"({len(made)} occurrences)", level="debug")
        else:
            # Dateless, with the schedule left in `description` where
            # recurrence.py looks for it first. Deliberately NOT copied into
            # `datetime_text`: fetch_sources.normalize() runs
            # parse_day_month_year() over that field, and a church whose
            # description mentions "Sunday 22 December" had that read as a
            # date in the year the text implied -- 2024-12-22, two years stale
            # in a 2026 calendar. The description already carries the schedule,
            # so the copy bought nothing.
            rows.append(row)

    # `_hours_schedule` is scratch space for _usable_schedule, carried on the row
    # because that is the only thing both the prose test and the hours test read.
    # It is not part of the row schema, and left on the row it reaches the
    # committed store and then the page, which is where an internal key belongs
    # least.
    for r in rows:
        r.pop("_hours_schedule", None)

    if not rows:
        raise PartialFetch(
            f"{sid}: no group survived the place-and-time filter "
            f"({attempted} entries opened, {dropped}) -- the markup or the "
            f"schedule wording has probably changed")
    report(f"{sid}: {len(rows)} rows from {attempted} entries "
           f"({opened} pages opened)")
    if dropped:
        report(f"  dropped {dropped}")
    return rows


def _self_test():
    """The three decisions that are this module's, run over the real markup."""
    import sys

    from checks import check as _check

    failures = []

    def check(label, actual, expected):
        return _check(label, actual, expected, failures)

    # A bare <p> with no class attribute, alongside a classed address line.
    card = BeautifulSoup(
        '<div class="list-item-container"><article><a href="/x">'
        '<h2 class="list-item-title">G</h2>'
        '<p class="oc-thumbnail-image"><img></p>'
        '<p class="list-item-address">1 St Rd, Cheltenham 3192</p>'
        '<p >We meet on Fridays.</p>'
        '<div class="tagged-as-list"><div class="text"><ul><li>'
        '<span>Probus</span></li></ul></div></div></a></article></div>',
        "html.parser")
    check("the summary is the classless paragraph",
          _card_summary(card.select_one(CARD)), "We meet on Fridays.")
    check("the card address is not read as the summary",
          _card_address(card.select_one(CARD)), "1 St Rd, Cheltenham 3192")
    check("the card's taxonomy terms are collected",
          [t.get_text(strip=True)
           for t in card.select(CARD_TAGS)], ["Probus"])

    for label, text, expected in [
        ("venue / street / suburb / postcode",
         "National Water Sports Centre\n5 Riverend Road\nBangholme 3175",
         ("National Water Sports Centre", "5 Riverend Road",
          "Bangholme", "3175")),
        # The street is two comma-joined lines here, and the first is the
        # group's own registered name, which is also the venue.
        ("a two-line street with the group name as venue",
         "Austin 7 Club Inc\nUnit 8, 19-23 Kylie Place\nCheltenham 3192",
         ("Austin 7 Club Inc", "Unit 8, 19-23 Kylie Place", "Cheltenham",
          "3192")),
        # No house number at all: a golf course is addressed by its name.
        ("a street with no house number",
         "Edithvale Public Golf Course\nFraser Ave\nEdithvale 3196",
         ("Edithvale Public Golf Course", "Fraser Ave", "Edithvale", "3196")),
        ("an address with a comma-separated aside",
         "Cheltenham Hall\n1218 Nepean Hwy Service Rd, (South corner of "
         "Nepean Highway service Road and Charman Road)\nCheltenham 3192",
         ("Cheltenham Hall", "1218 Nepean Hwy Service Rd", "Cheltenham",
          "3192")),
        # A postcode with no suburb names no place a reader can find.
        ("a postcode with no suburb is not a place",
         "Ormond Arcade - A Path To Follow\n6-7/556 North Road\n3204", None),
        ("a missing suburb mid-block is not a place",
         "6-7/556 North Road,\n 3204", None),
        # The listing card's own form, used as the fallback for the eleven
        # detail pages with no Location block.
        ("a one-line card address with a venue",
         "BayCISS - Bayside Community Information & Support Service, "
         "12 Katoomba Street, Hampton East, 3188",
         ("BayCISS - Bayside Community Information & Support Service",
          "12 Katoomba Street", "Hampton East", "3188")),
        ("a one-line card address that is just a street",
         "5 Riverend Road, Bangholme 3175",
         ("", "5 Riverend Road", "Bangholme", "3175")),
        ("no location block at all", "", None),
    ]:
        check(f"address: {label}", _split_address(text), expected)

    hours = """<div class="hours-list">
      <li><span class='hours-day'> Sunday </span>
        <ul class="hours-time-list"><li>07:00 AM&#8211;10:00 AM</li></ul></li>
      <li><span class='hours-day'> Monday </span>
        <span class="hours-status closed">Closed</span></li>
      <li><span class='hours-day'> Thursday </span>
        <ul class="hours-time-list"><li>04:00 PM&#8211;06:00 PM</li></ul></li>
    </div>"""
    check("a plausible hours table becomes a schedule",
          _schedule_from_hours(hours),
          # Monday-indexed, so Thursday sorts before Sunday.
          "every Thursday 16:00 - 18:00, every Sunday 07:00 - 10:00")

    # One weekday, many sessions: the table lists dated occurrences, not one
    # window, so the seven Chelsea Sports Club entries have to become one clause
    # rather than seven "every Sunday" ones.
    check("repeated sessions on one day aggregate to one clause",
          _schedule_from_hours(
              "<div class='hours-list'><li><span class='hours-day'>Sunday"
              "</span><ul class='hours-time-list'>"
              "<li>04:00 PM&#8211;09:00 PM</li><li>04:00 PM&#8211;08:00 PM</li>"
              "<li>12:00 PM&#8211;10:00 PM</li></ul></li></div>"),
          "every Sunday 16:00 - 21:00")
    # ...and the day is refused when the aggregate is longer than a meeting.
    check("a day whose sessions span the day is refused",
          _schedule_from_hours(
              "<div class='hours-list'><li><span class='hours-day'>Sunday"
              "</span><ul class='hours-time-list'>"
              "<li>07:00 AM&#8211;12:00 PM</li><li>05:00 PM&#8211;09:00 PM</li>"
              "</ul></li></div>"), None)
    check("an all-day, seven-day table is refused as opening hours",
          _schedule_from_hours(
              "<div class='hours-list'>" + "".join(
                  f"<li><span class='hours-day'>{d}</span>"
                  f"<ul class='hours-time-list'><li>07:00 AM&#8211;06:00 PM"
                  f"</li></ul></li>"
                  for d in ("Sunday", "Monday", "Tuesday", "Wednesday",
                            "Thursday", "Friday", "Saturday"))
              + "</div>"), None)
    check("a day marked closed is not read as a session",
          _schedule_from_hours(
              "<div class='hours-list'><li><span class='hours-day'>Sunday"
              "</span><span class='hours-status closed'>Closed</span>"
              "<ul class='hours-time-list'><li>10:00 AM&#8211;11:30 AM</li>"
              "</ul></li></div>"), None)

    # The page does not close its <li> elements, and every fixture above does,
    # which is how a reader that walked `.hours-list > li` could pass its whole
    # suite and still see one day in production: BeautifulSoup nests each
    # weekday inside the first, so a direct-child selector matches exactly one
    # item. These three use the real shape.
    unclosed = ("<div class='hours-list'>"
                "<li class='current-day-item-no'>"
                "<span class='hours-day'> Sunday </span>"
                "<span class='hours-status closed'>Closed</span>"
                "<li class='current-day-item-no'>"
                "<span class='hours-day'> Tuesday </span>"
                "<ul class='hours-time-list'><li>09:00 AM&#8211;12:00 PM</li>"
                "</ul>"
                "<li class='current-day-item-no'>"
                "<span class='hours-day'> Wednesday </span>"
                "<ul class='hours-time-list'><li>09:00 AM&#8211;12:00 PM</li>"
                "</ul></div>")
    check("unclosed list items still yield every open day",
          _schedule_from_hours(unclosed),
          "every Tuesday 09:00 - 12:00, every Wednesday 09:00 - 12:00")
    # The first row is closed, which is the case that used to return nothing.
    check("a closed first day does not hide the days after it",
          _schedule_from_hours(unclosed) is None, False)
    # MAX_MEETING_DAYS was dead for the same reason and is what keeps a venue's
    # opening hours out: Moorabbin Air Museum states Mon-Fri 10:00-16:00.
    check("a five-day window is refused as opening hours",
          _schedule_from_hours(
              "<div class='hours-list'>" + "".join(
                  f"<li><span class='hours-day'>{d}</span>"
                  f"<ul class='hours-time-list'><li>10:00 AM&#8211;04:00 PM"
                  f"</li></ul>"
                  for d in ("Monday", "Tuesday", "Wednesday", "Thursday",
                            "Friday"))
              + "</div>"), None)
    # A genuinely three-day group at the same time each day is kept: the day
    # count, not the uniformity, is what distinguishes it from a venue.
    check("a three-day group with one window a day is kept",
          _schedule_from_hours(unclosed),
          "every Tuesday 09:00 - 12:00, every Wednesday 09:00 - 12:00")
    check("an all-closed table yields no schedule",
          _schedule_from_hours(
              "<div class='hours-list'><li><span class='hours-day'>Sunday"
              "</span><span class='hours-status closed'>Closed</span></li>"
              "</div>"), None)

    row = make_row("t", "G", "https://example.invalid/g")
    made, _s, _r = materialise(row, "every Sunday 07:00 - 10:00")
    check("a schedule materialises into a weekly series",
          len(made), 12)
    check("every occurrence is at the stated time",
          sorted({r["datetime_iso"][11:16] for r in made}), ["07:00"])
    check("and every occurrence is a Sunday",
          sorted({date.fromisoformat(r["datetime_iso"][:10]).weekday()
                  for r in made}), [6])
    check("and the rows carry a series id", bool(made[0].get("series_id")), True)
    check("and all twelve share it",
          len({r["series_id"] for r in made}), 1)
    made, _s, reason = materialise(row, "every Sunday morning")
    check("a days-only schedule states no time", (bool(made), reason is not None),
          (False, True))
    # normalize() parses a date out of datetime_text, so a prose description
    # left in that field became a date in the year the text implied. This is the
    # shape that produced a 2024-12-22 row in a 2026 calendar.
    check("a prose description left in datetime_text is the fetcher's "
          "decision, not make_row's",
          make_row("t", "G", "https://example.invalid/g")["datetime_text"], "")

    if failures:
        print(f"\nwebfetch_directory: {len(failures)} case(s) FAILED")
        sys.exit(1)
    print("\nall directory-reader cases as expected")


if __name__ == "__main__":
    _self_test()