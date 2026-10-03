"""Source fetchers that reach their host with plain urllib.

Every fetcher here takes `(cfg, session)` and returns snapshot rows. `session`
is the run's shared session from `fetch_sources.session_for()` (plain or
browser-impersonating per `impersonate:`); Kingston Hubs and Chatty Cafe ignore
it for their listing requests but Greater Dandenong reuses it for detail pages
via `_gd_session()`. See `docs/decisions.md` D2a for why impersonation is a
per-source flag and not a script boundary.

`fetch_sources.py` owns the entry point, config validation, snapshot writing and
the failure rules. Nothing here writes a file.
"""
import json
import re
import time
import urllib.request
from datetime import datetime

from bs4 import BeautifulSoup

from venues import extract_suburb, is_online
from webfetch_http import (MIN_CRAWL_DELAY, Pacer, PartialFetch, enrich_details,
                           make_row, month_number, parse_day_month_year,
                           report)

# The interval the Greater Dandenong listing walk was actually running at, so
# `crawl_delay` can override it and nothing else can change it by accident.
GD_CRAWL_DELAY = 0.2

BASE = "https://www.kingston.vic.gov.au"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/json,*/*",
    "Referer": BASE,
}


def _request(url, data=None, headers=None, timeout=15, retries=3):
    """GET (data=None) or POST (data=bytes) with urllib, retrying and raising.

    One implementation rather than one per verb: the only difference between the
    two was the request body and three headers. Raises the last exception on
    failure, which is what lets a caller turn a page-0 miss into a PartialFetch.
    """
    if retries < 1:
        raise ValueError("retries must be >= 1")
    h = dict(HEADERS)
    if data is not None:
        h["Content-Type"] = "application/json; charset=utf-8"
        h["X-Requested-With"] = "XMLHttpRequest"
        h["Origin"] = BASE
    if headers:
        h.update(headers)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:
            last = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise last


def _get(url, headers=None, timeout=15, retries=3):
    return _request(url, None, headers, timeout, retries)


def _post(url, payload, headers=None, timeout=15, retries=3):
    return _request(url, json.dumps(payload).encode(), headers, timeout, retries)


def _parse_date(s, ref=None):
    """Parse a source date string into a datetime, or None if unrecognised.

    Month names are resolved through webfetch_http.month_number() rather than
    strptime's '%b', which follows the process LC_TIME locale and raises on a
    non-English Windows/GHA configuration.
    """
    s = (s or "").strip()
    if not s:
        return None
    ref = ref or datetime.now()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%A %d %B %Y",
                "%d %B %Y", "%d %b %Y", "%d/%m/%Y %I:%M:%S %p",
                "%d/%m/%Y %H:%M:%S", "%d %B %Y %I:%M %p"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    # "Tuesday 29 September, 8:00am" (no year).
    m = re.search(
        r"(\w+)\s+(\d{1,2})\s+([A-Za-z]+),?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)",
        s, re.I)
    if m:
        day, mon_name, h, mi, ap = int(m.group(2)), m.group(3), int(m.group(4)), \
            int(m.group(5) or 0), m.group(6).lower()
        mon = month_number(mon_name)
        if mon:
            if ap == "pm" and h != 12:
                h += 12
            if ap == "am" and h == 12:
                h = 0
            # Anchor on the reference year, rolling forward when the month has
            # already passed, so a "28 December" listing in November is next
            # year rather than 11 months in the past.
            candidates = []
            for year in (ref.year, ref.year + 1):
                try:
                    candidates.append(datetime(year, mon, day, h % 24, mi))
                except ValueError:
                    continue
            for cand in candidates:
                if cand >= ref:
                    return cand
            if candidates:
                return candidates[-1]
    # "28 Sep 2026" / "28 September 2026" bare dates (midnight). The shared
    # reader owns this shape: it is the same regex and the same month lookup,
    # so a bare date cannot parse one way here and another way there.
    return parse_day_month_year(s)


# price_sort lives in fetch_sources.py (the single canonical implementation);
# rows here carry price_text and are normalised downstream.
def _month_range(year, month, n_months):
    out = []
    y, m = year, month
    for _ in range(n_months):
        out.append(f"{y}-{m:02d}-01T00:00:00")
        m += 1
        if m == 13:
            m, y = 1, y + 1
    return out


def _add_one_month(iso):
    dt = datetime.fromisoformat(iso)
    if dt.month == 12:
        return f"{dt.year + 1}-01-01T00:00:00"
    return f"{dt.year}-{dt.month + 1:02d}-01T00:00:00"


# ---------------------------------------------------------------------------
# Source: Kingston Hubs (OpenCities calendar API)
# ---------------------------------------------------------------------------

def fetch_kingston_hubs(cfg, session=None):
    cal_ids = cfg["calendars"]
    # Explicit id -> venue map, and it must be complete. The API returns a
    # CalendarId and nothing else useful, so this map is the only source of a
    # venue name and a street address. An unmapped id used to fall back to a
    # generic ("Kingston Hubs", "Chelsea 3196"), which published 300 rows of
    # the *Patterson Lakes* calendar under a Chelsea address and a Chelsea
    # suburb -- a wrong address, which nothing downstream can detect, unlike a
    # missing one. So an unmapped id now fails the source.
    venues = {}
    for cal_id in cal_ids:
        entry = (cfg.get("calendar_venues") or {}).get(cal_id) or {}
        name = (entry.get("name") or "").strip()
        address = (entry.get("address") or "").strip()
        if not name or not address:
            raise ValueError(
                f"kingston_hubs: calendar {cal_id!r} has no name/address in "
                f"calendar_venues (got name={name!r} address={address!r}). "
                f"Every calendar in `calendars` needs both.")
        venues[cal_id] = (name, address)
    unmapped = set((cfg.get("calendar_venues") or {})) - set(cal_ids)
    if unmapped:
        # Dead entries rot into the same trap: a calendar is renamed upstream,
        # its id changes, the new id is added to `calendars`, and the stale
        # entry quietly stops applying.
        report(f"calendar_venues has entries for calendars not in `calendars`: "
               f"{sorted(unmapped)}", level="warn")
    rows = []
    now = datetime.now()
    for start in _month_range(now.year, now.month, 12):
        end = _add_one_month(start)
        payload = {
            "LanguageCode": "en-AU",
            "Ids": cal_ids,
            "StartDate": start,
            "EndDate": end,
        }
        try:
            resp = _post(f"{BASE}/ocapi/calendars/getcalendaritems", payload, timeout=15)
        except Exception as e:
            report(f"month {start[:7]}: FAILED {e!r}", level="warn")
            continue
        # Parse outside the request try, so one malformed card cannot discard
        # every row already collected for this month.
        try:
            data = json.loads(resp)
        except ValueError as e:
            report(f"month {start[:7]}: bad JSON {e!r}", level="warn")
            continue
        for day in data.get("data", []) or []:
            for it in day.get("Items", []) or []:
                name = it.get("Name")
                dt = _parse_date(it.get("DateTime"))
                if dt is None or not name:
                    continue  # skip undateable Hub items; no fake stamps
                # The API has been seen to return an item under a CalendarId
                # that is not in the request. The map is complete for the ids we
                # asked for, so anything else is unknown, and an unknown venue
                # must not inherit another calendar's address.
                cal_id = it.get("CalendarId")
                if cal_id not in venues:
                    raise ValueError(
                        f"kingston_hubs: API returned an item under "
                        f"CalendarId {cal_id!r}, which is not in "
                        f"calendar_venues ({sorted(venues)})")
                venue, address = venues[cal_id]
                # The API states a time in the small hours for a couple of
                # listings -- "Social Jigsaw Group" comes back as 12:30:00 AM.
                # That is a placeholder, not a 12:30am session, and publishing
                # it as one is worse than publishing no time at all: the page
                # renders an exact 00:00 as "all day" (the pipeline's marker
                # for "date known, time not stated") but 00:30 as a real
                # half-past-midnight start. Normalise the hour to the marker so
                # both read the same way.
                if dt.hour == 0:
                    dt = dt.replace(minute=0)
                rows.append(make_row(
                    cfg["id"], name, BASE,
                    datetime_iso=dt.isoformat(),
                    datetime_text=it.get("DateTime", ""),
                    location=venue,
                    address=address,
                    # The calendar API returns no prose at all -- an item is
                    # CalendarId, Id, MainContentId, Name and DateTime, and
                    # nothing else. This used to pass `description=name`, so
                    # all 524 rows of this source published with their own title
                    # in the description column: the page printed the name
                    # twice, the search haystack counted it twice, and the
                    # classifier read the title as the listing's description.
                    # A source that states no description gets none.
                    description="",
                ))
    return rows


# NOTE: Kingston Council / Bayside Council / Frankston live pages are
# WAF-blocked for Python urllib. They are snapshotted via browser into
# scripts/webfetch_snapshots/*.json instead — see dedupe.py. No fetch_*
# functions for them here by design.


# ---------------------------------------------------------------------------
# Source: Greater Dandenong (Drupal HTML, Python-safe)
# ---------------------------------------------------------------------------

# The detail page states the venue under a labelled field, e.g.
#   Location | Noble Park Community Centre
#            | 44 Memorial Drive, Noble Park
# The listing card has no venue at all -- only title, date and category -- so
# this is the only place the suburb appears.
_GD_LOCATION_LABEL = re.compile(r"^\s*Location\s*$", re.I)


def _gd_detail_location(soup):
    """(venue, address) from a GD detail page's labelled Location field.

    Read from the two named sub-fields rather than by splitting the field's
    text: `field-title-address` is the venue and `field-address-text` the
    street address, and they are separate elements however the paragraph
    happens to be wrapped.
    """
    side = soup.select_one(".event-columns__side") or soup
    for field in side.select(".field"):
        label = field.select_one(".field__label")
        if not label or not _GD_LOCATION_LABEL.match(
                label.get_text(" ", strip=True)):
            continue
        item = field.select_one(".field__item")
        if not item:
            return "", ""
        venue_el = item.select_one(".field--name-field-title-address")
        addr_el = item.select_one(".field--name-field-address-text")
        venue = venue_el.get_text(" ", strip=True) if venue_el else ""
        address = addr_el.get_text(" ", strip=True) if addr_el else ""
        if venue or address:
            return venue, address
        # Fallback for a layout that drops the field names: the first two
        # lines of the field are the venue and the address.
        lines = [ln.strip() for ln in item.get_text("\n").splitlines()
                 if ln.strip()]
        if not lines:
            return "", ""
        venue = lines[0]
        address = lines[1] if len(lines) > 1 else ""
        if address.lower() == venue.lower():
            address = ""
        return venue, address
    return "", ""


def _passes_suburb_filter(row, allowed):
    """Keep a row only when its own suburb is inside the configured catchment.

    This is also the rule health_check.py asserts with, and it is the strict
    reading on purpose. The two were written independently and disagreed about
    exactly the case that mattered -- a row whose suburb cannot be extracted.
    The fetcher kept it ("cannot place it, so do not throw it away") and the
    checker failed the build on it ("cannot place it, so it is outside the
    catchment"), which meant "Mount Cannibal Hike and Barbeque" -- a reserve
    some forty kilometres from Springvale, named after the place it is in --
    was published by one module and rejected by the other, and the build went
    red.

    One rule, and it is the strict one. An event the pipeline cannot place in
    the catchment is not published, because the alternative is that the filter
    publishes the whole city whenever the detail-page suburb extraction misses.
    A row that names an in-area suburb anywhere in its own text is kept, since
    that is a positive signal and costs nothing. Online events are exempt: there
    is no suburb for them to be outside of.
    """
    if not allowed:
        return True
    allowed_lower = {a.lower() for a in allowed}
    if is_online(row.get("location")):
        return True
    for candidate in (row.get("suburb"),
                      extract_suburb(row.get("address")
                                     or row.get("location") or "")):
        named = (candidate or "").strip().lower()
        if named:
            if named in allowed_lower:
                return True
            report(f"dropping {row.get('name')!r}: suburb {named!r} is "
                   f"outside {sorted(allowed)}", level="debug")
            return False
    blob = " ".join([row.get("name", ""), row.get("location", ""),
                     row.get("address", ""), row.get("description", "")]).lower()
    if any(a in blob for a in allowed_lower):
        return True
    report(f"dropping {row.get('name')!r}: no suburb could be read and "
           f"nothing names {sorted(allowed)}", level="debug")
    return False


def _gd_session(shared=None):
    """A browser-impersonating session for Greater Dandenong's detail pages.

    The listing page answers plain urllib, but the event *detail* pages -- the
    only place the suburb is stated -- do not reliably, so the fetcher that
    worked on the card is blocked on the one page the catchment filter depends
    on. Reuses the run's shared session when sources.yaml marked this source
    `impersonate: true`, and builds its own otherwise, so `--source
    greater_dandenong` still works if the flag is ever dropped.
    """
    if shared is not None:
        return shared
    from webfetch_http import make_session
    return make_session()


def _gd_fetch(session, url, timeout=15):
    r = session.get(url, timeout=timeout)
    if r.status_code != 200 or not r.text:
        return None
    return r.text


def fetch_greater_dandenong(cfg, session=None):
    session = _gd_session(session)
    allowed = cfg.get("suburb_filter", [])
    max_pages = cfg.get("max_pages", 8)
    detail_cap = cfg.get("detail_cap", 150)
    base = "https://www.greaterdandenong.vic.gov.au"

    # Crawl the listing first, keyed on the event URL. `?page=N` here is an
    # *offset* for a JS "Load More" control, not a page number: page 1 returns
    # everything page 0 returned plus a few more. Walking it without a seen-set
    # re-parsed every earlier card on every page -- 348 card reads to find 45
    # distinct events -- and left the duplicates for dedupe.py to unpick.
    cards = {}
    # Paced before each request rather than after each page. The `time.sleep(0.2)`
    # this replaces sat at the bottom of the loop, which the failure branch above
    # skipped with a `continue` -- so a page that could not be fetched cost no
    # pause and the retry went straight out behind it. `crawl_delay` was
    # validated for this source and read by nobody. Shared with the detail pass
    # so one budget covers every request.
    pacer = Pacer(cfg.get("crawl_delay"), floor=MIN_CRAWL_DELAY,
                  default=GD_CRAWL_DELAY)
    for page in range(max_pages):
        url = cfg["url"] if page == 0 else f"{cfg['url']}?page={page}"
        if not pacer.take():
            report(f"stopped at the run's {pacer.budget}-request budget "
                   f"before page {page}", level="warn")
            break
        try:
            html = _gd_fetch(session, url, timeout=12)
        except Exception as e:
            if page == 0:
                raise PartialFetch(f"Greater Dandenong page {page} FAILED {e!r}")
            report(f"page {page}: FAILED {e!r}", level="warn")
            continue
        if not html:
            if page == 0:
                raise PartialFetch(f"Greater Dandenong page {page}: no content")
            report(f"page {page}: no content", level="warn")
            break
        soup = BeautifulSoup(html, "html.parser")
        views = soup.select(".views-col")
        if not views:
            break
        fresh = 0
        for card in views:
            title_el = card.select_one(".title a") or card.select_one("h2 a, h3 a")
            if not title_el:
                continue
            link = title_el.get("href", "")
            if not link:
                continue
            if not link.startswith("http"):
                link = base + link
            if link in cards:
                continue
            date_el = card.select_one(".date")
            loc_el = card.select_one(
                ".location, .event-location, .views-field-field-location")
            cards[link] = {
                "name": title_el.get_text(strip=True),
                "datetime_text": date_el.get_text(strip=True) if date_el else "",
                "location": loc_el.get_text(strip=True) if loc_el else "",
                "description": card.get_text(" ", strip=True)[:300],
                "source": link,
            }
            fresh += 1
        if page and not fresh:
            # The listing has been exhausted; asking for more only re-sends
            # what we already have.
            report(f"no new events past page {page - 1}")
            break
    report(f"{len(cards)} distinct events from the listing")

    # Build every row from the card first, then open the detail pages in one
    # shared loop. This used to fetch details inline with its own `continue` on
    # failure, which bypassed enrich_details' PartialFetch guard entirely: a WAF
    # block on the detail pages published a listing-only snapshot with every
    # venue blank and the run stayed green.
    rows = [make_row(
        cfg["id"], card["name"], link,
        datetime_text=card["datetime_text"],
        location=card["location"] or "Greater Dandenong",
        description=card["description"],
    ) for link, card in list(cards.items())[:detail_cap]]
    if len(cards) > len(rows):
        report(f"detail_cap {detail_cap} reached, "
               f"{len(cards) - len(rows)} events left without a venue")

    def _apply_detail(row, html):
        venue, address = _gd_detail_location(BeautifulSoup(html, "html.parser"))
        if venue:
            row["location"] = venue
            row["address"] = ", ".join(p for p in (venue, address) if p)
            row["suburb"] = extract_suburb(address)

    enrich_details(session, rows, None, _apply_detail, sleep=0.15,
                   label="greater_dandenong", pacer=pacer)

    kept, placed, unplaced, dropped = [], 0, 0, 0
    for row in rows:
        if row.get("suburb"):
            placed += 1
        else:
            unplaced += 1
        if _passes_suburb_filter(row, allowed):
            kept.append(row)
        else:
            dropped += 1

    report(f"venue found for {placed}, suburb unknown for {unplaced}")
    report(f"catchment {list(allowed)} keeps {len(kept)}, drops {dropped}")
    kept_subs = sorted({r["suburb"] for r in kept if r["suburb"]})
    if kept_subs:
        report(f"suburbs kept: {', '.join(kept_subs)}", level="debug")
    return kept


# Frankston live (Everi) is WAF-blocked for Python; see webfetch snapshots.

def fetch_gd_libraries(cfg, session=None):
    # GD Libraries returns all events on one page; ?page=N is ignored, so
    # there is no pagination loop here.
    rows = []
    url = cfg["url"]
    allowed = cfg.get("suburb_filter", [])
    session = _gd_session(session)
    try:
        html = _gd_fetch(session, url, timeout=12)
    except Exception as e:
        raise PartialFetch(f"GD Libraries: FAILED {e!r}")
    if not html:
        raise PartialFetch("GD Libraries: no content")
    try:
        soup = BeautifulSoup(html, "html.parser")
        for card in soup.select(".views-col"):
            title_el = card.select_one(".title a")
            if not title_el:
                continue
            name = title_el.get_text(strip=True)
            if not name or len(name) < 3:
                continue
            link = title_el.get("href", "")
            if link and not link.startswith("http"):
                link = "https://libraries.greaterdandenong.vic.gov.au" + link
            date_el = card.select_one(".date")
            date_text = date_el.get_text(strip=True) if date_el else ""
            rows.append(make_row(
                cfg["id"], name, link or cfg["url"],
                datetime_text=date_text,
                location="Greater Dandenong Libraries",
                description=card.get_text(" ", strip=True)[:300],
            ))
    except Exception as e:
        report(f"parse FAILED {e!r}", level="warn")
        return rows

    # Same platform as the council listing, so the venue is on the detail page
    # under a labelled Location field. Without it every row read "Greater
    # Dandenong Libraries" and no suburb could be told -- the same gap that
    # made the council catchment filter a no-op. This uses the shared detail
    # loop so a block on the detail pages raises rather than publishing a
    # listing-only snapshot with every venue blank.
    def _apply_detail(row, html):
        venue, address = _gd_detail_location(BeautifulSoup(html, "html.parser"))
        if venue:
            row["location"] = venue
            row["address"] = ", ".join(p for p in (venue, address) if p)
            row["suburb"] = extract_suburb(address)

    enrich_details(session, rows, cfg.get("detail_cap", 60), _apply_detail,
                   sleep=0.15, label="gd_libraries")
    placed = sum(1 for r in rows if r.get("suburb"))

    if allowed:
        # Same catchment as greater_dandenong, so the same rule. Without it
        # this source published rows in Dandenong while its sibling dropped
        # them, and the two published different answers to one question.
        before = len(rows)
        rows = [r for r in rows if _passes_suburb_filter(r, allowed)]
        report(f"{before - len(rows)} row(s) outside suburb_filter {allowed}")
    report(f"{len(rows)} events, venue resolved for {placed}")
    return rows


_CHATTY_DAY = r"(?:mon|tues|wednes|thurs|fri|satur|sun)day"
# "Wednesdays & Thursdays", "Mondays, Wednesdays", "Every Thursday".
_CHATTY_DAYLIST = (rf"(?:every\s+|each\s+)?{_CHATTY_DAY}s?"
                   rf"(?:\s*(?:&|and|to|,|/)\s*{_CHATTY_DAY}s?)*")
# A time, in every form these pages use: "10.30am", "10:30am", "12noon",
# "11.30", and a range joined by -/–/to/until/till. Anchored so "20th" and
# "17 December" in the surrounding prose cannot be read as an hour.
_CHATTY_TIME_TOKEN = (r"(?:"
                      r"\d{1,2}[:.]\d{2}\s*(?:[ap]\.?\s*m\.?)?"
                      r"|\b\d{1,2}\s*(?:noon|midday)\b"
                      r"|\b\d{1,2}\s*[ap]\.?\s*m\.?"
                      r")")
_CHATTY_TIME = (rf"{_CHATTY_TIME_TOKEN}"
                rf"(?:\s*(?:-|–|—|to|until|till)\s*{_CHATTY_TIME_TOKEN})?")
# Filler between the weekday and the time: "from", "at", "on", a colon, a
# cadence note like "(fortnightly)". Deliberately cannot cross a digit, which
# is what stops "Monday 20th April at 10.30am" from reading "20" as a time.
_CHATTY_GAP = r"[^0-9\n]{0,24}?"
# "2nd Tuesday of the month at 11.00am" is a monthly pattern, not a weekly one.
# Matching it as weekday+time would turn it into every Tuesday, so the day list
# must not be followed by a month qualifier.
_CHATTY_NOT_MONTHLY = r"(?!\s+(?:of|each|in|on)\s+(?:the\s+|every\s+)?month)"
CHATTY_DAYTIME_RE = re.compile(
    rf"\b({_CHATTY_DAYLIST}){_CHATTY_NOT_MONTHLY}{_CHATTY_GAP}({_CHATTY_TIME})",
    re.I)

def _chatty_live_schedule(html):
    """The '<weekday(s)> <time>' a Chatty Cafe venue page states for its table.

    The whole matched phrase is returned, not a reassembly of the two groups,
    so a cadence the source states inside the gap -- "Friday (fortnightly)
    10.30am-11.30am" -- survives into the schedule text the date parser reads.

    A trailing parenthetical cadence is appended too: the page states
    "Tuesday 10.00am - 11.30am (1st & 3rd Tuesdays of the Month)" and the
    daytime match ends after the time, dropping the qualifier that says it
    is monthly. Without it the venue reads weekly. Only a short parenthetical
    naming a cadence (1st/2nd/month/fortnight/second) is kept, so a following
    sentence is never swallowed.
    """
    if not html:
        return ""
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    m = CHATTY_DAYTIME_RE.search(text)
    if not m:
        return ""
    out = re.sub(r"\s+", " ", m.group(0)).strip()
    tail = text[m.end():m.end() + 48]
    pm = re.match(r"\s*\(([^)]{0,40})\)", tail)
    if pm and _MONTHLY_MARKER_RE.search(pm.group(1)):
        out = (out + " (" + pm.group(1).strip() + ")")[:80]
    return out.strip()[:80]


def _chatty_schedule_is_usable(schedule):
    """True when recurrence.py can turn `schedule` into dated occurrences.

    The test is deliberately the real parser rather than a look of the string:
    a schedule that parses is one that will publish, and a schedule that does
    not is one that silently deletes a venue from the calendar. recurrence is
    imported lazily so that this module has no dependency on the date parser,
    which keeps the import graph one-directional.
    """
    try:
        from recurrence import build_spec
    except ImportError:
        return True  # cannot verify; trust the site as before
    row = {"name": "Chatty Cafe", "datetime_text": schedule,
           "description": schedule}
    spec, _reason = build_spec(row)
    return spec is not None and bool(spec.slots)


# A configured schedule stating a monthly/fortnightly cadence that the live
# page drops ("Tuesday 10.00am - 11.30am (1st & 3rd Tuesdays)" -> "Tuesday
# 10.00am - 11.30am"). The live value parses -- as weekly -- so the usability
# check above cannot see the loss, and the venue doubles from ~2 to ~4
# sessions a month. A monthly marker present in config but absent live means
# the site text is the lossy one, so config wins.
_MONTHLY_MARKER_RE = re.compile(
    r"\b(1st|2nd|3rd|4th|5th|first|second|third|fourth|fifth|fortnight"
    r"|every second|month)\b", re.I)


def _cadence_markers(text):
    """The cadence words in `text`, as a set, for comparing two schedules."""
    return {m.group(0).lower() for m in _MONTHLY_MARKER_RE.finditer(text or "")}


def _chatty_live_keeps_cadence(configured, live):
    """True when preferring `live` does not lose or contradict a cadence.

    Was "does the configured text mention a cadence and the live one not",
    which is a one-way test and so passed whenever *neither* mentioned one. The
    configured value for Chelsea Activity Hub is a bare "Every Tuesday" -- no
    marker at all -- while the live page states "(1st & 3rd Tuesdays of the
    Month)". The old test saw no marker in either and returned True, so the
    live value won, the venue went from one session a week to two a month, and
    the two coexist in the store: fourteen rows for one venue, six weekly and
    eight monthly, half of them on days the venue does not actually run.

    So the comparison is between the two texts rather than against one of
    them: any marker in the configured text that the live text drops loses the
    live text, and a live text that states *different* weekdays from the
    configured one also loses, since a venue's cadence is not something a
    reworded page is allowed to quietly double.
    """
    if not configured or not live:
        return True
    if _cadence_markers(configured) - _cadence_markers(live):
        return False
    if _cadence_markers(live) - _cadence_markers(configured):
        return False
    return True


def fetch_chatty_cafe(cfg, session=None):
    """Fetch Chatty Cafe venues.

    sources.yaml holds the venue list and a fallback schedule. The live page is
    fetched anyway, so prefer a schedule it actually states -- previously the
    page was downloaded and discarded, so a changed schedule on the site was
    invisible.

    The live value only wins when it is actually usable, which is checked by
    asking whether the date parser can build a schedule from it. A previous
    extractor truncated every time to its hour digits, so "Tuesday 10.00am -
    11.30am" became "Tuesday 10": a weekday with no time, which the parser
    cannot expand. Six of the twenty venues were then dropped entirely, because
    the live value replaced a correct configured schedule with an unusable one
    and the configured fallback could no longer be reached. Verifying the live
    value before preferring it is what makes "prefer the site" safe.
    """
    rows = []
    for venue in cfg.get("venues", []) or []:
        if not isinstance(venue, dict) or not venue.get("slug") or not venue.get("schedule"):
            report(f"skipping malformed venue entry {venue!r}", level="warn")
            continue
        vname = venue.get("name") or venue["slug"]
        url = f"https://chattycafeaustralia.org.au/venue/{venue['slug']}/"
        schedule = venue["schedule"]
        try:
            html = _get(url, timeout=10)
            live = _chatty_live_schedule(html)
            if live and live != schedule:
                if not _chatty_live_keeps_cadence(schedule, live):
                    report(f"{vname}: site text {live!r} drops the monthly "
                           f"cadence in configured {schedule!r}, keeping "
                           f"configured", level="warn")
                elif _chatty_schedule_is_usable(live):
                    report(f"{vname}: schedule updated from site ({live!r})")
                    schedule = live
                else:
                    report(f"{vname}: site text {live!r} is not a usable "
                           f"schedule, keeping configured {schedule!r}",
                           level="warn")
        except Exception as e:
            # Keep the configured schedule rather than dropping the venue.
            report(f"{vname}: fetch FAILED {e!r}, using configured schedule",
                   level="warn")
        rows.append(make_row(
            cfg["id"], f"Chatty Cafe - {vname}", url,
            datetime_text=schedule,
            location=vname,
            address=venue.get("address") or "",
            price_text="Free",
            description=f"Chatty Cafe at {vname}. {schedule}. "
                        f"A welcoming space for conversation and connection.",
        ))
    return rows
