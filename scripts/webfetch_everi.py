"""What's On Frankston (everi.com.au "Event Hub").

Replaces the hand-maintained `frankston_archived` rows in
`scripts/archived_events.json`. Those were kept by hand on the stated grounds
that the live pages were WAF-blocked, which stopped being true; the site answers
plain urllib. Being hand-maintained was not only stale, it was unreclaimable:
`reconcile_store()` only judges a row whose `source_label` appeared in this run's
live rows, and no fetcher ever produced one, so a listing withdrawn by its
organiser sat on the calendar forever. `Trivia on Tap` was published as twelve
rows reaching 2027-09-03 from a transcription nobody could cancel, and five of
the twenty archived listings had quietly left the site.

Enumeration: the sitemap, and only the sitemap
------------------------------------------------

The site's own two ways of paging are both broken, in the way that is worst for
a crawler -- they succeed and return the wrong thing:

* `?page=N` is accepted and ignored. Pages 1, 2, 3 and 20 return the same twenty
  events, differing only in the order of same-date ties. Page 21 soft-404s.
* `POST /event/load-more-events` returns the same twenty and then reports
  `HasNextPage: false`, while 876 events exist. That endpoint needs the page's
  embedded `query` object replayed with a growing array of full 36-character
  GUIDs, and it declines to continue without them.

So: `sitemap.xml` is a sitemap index of nine `event-sitemap/N` children, 876
occurrence URLs, no overlap, and `/event-sitemap/10` soft-404s, so the range is
exactly 1..9. That is the whole directory and it is read from there.

A series is many pages
----------------------

The site expands one series into one page per occurrence -- `Friday Night Reset`
is eleven pages, `34282077-a` through `34282087-a` -- and every occurrence
shares an `eventIdentifier` GUID, published in a hidden input:

    <input type="hidden" id="relateEventUrl"
           value="/event/related-events?...&eventId=34282077-a
                  &eventIdentifier=1ff2f53c-...&latitude=-38.1487006
                  &longitude=145.1212243" />

That GUID is the site's own series identity, so it is stamped into `series_id`
rather than the derived one `recurrence.series_id_for()` computes. Withdrawing
the series then removes all eleven rows at once, which is the thing a derived id
cannot do on its own and the reason D38 left room for a source-supplied value.

What not to trust
-----------------

The JSON-LD is excellent for `name`, `startDate`, `endDate`, `description` and a
PostalAddress, and wrong or missing in five places that all had to be worked
around rather than used:

* `offers.priceCurrency` is hardcoded `"USD"` and `offers.availability` is
  hardcoded `"InStock"`. There is **no** `offers.price`. Price comes from
  `li.btn-info-detail.pricing` or not at all.
* `location.address.streetAddress` is frequently degraded -- `" High St"` with a
  leading space and no house number -- so the venue name is taken from
  `location.name`'s first comma segment instead, which also carries a `\\ufffd`
  wherever a line break was.
* `performer.name` is `"UnKnow"` when unset, which is not an organiser.
* `<link rel="canonical">` points at the **last** occurrence of the series, so
  canonicalising on it would collapse eleven rows into one.
* The listing's `p.date` is server-rendered relative text ("Today", "2 - 4 Oct",
  "+8 dates"), never a date.
"""
import json
import re
import time
from datetime import date, timedelta

from bs4 import BeautifulSoup

from webfetch_http import (PartialFetch, get, make_row, report,
                           set_reporting_source)
from venues import needs_address

# --- sitemap ---------------------------------------------------------------
SITEMAP_INDEX = re.compile(r"<loc>\s*([^<\s]+?)\s*</loc>")
EVENT_SITEMAP = re.compile(r"/event-sitemap/(\d+)\s*$")

# --- detail page -----------------------------------------------------------
DETAIL = "div#divEventDetail"
JSON_LD = "script[type='application/ld+json']"
RELATED = "input#relateEventUrl"
DETAIL_TITLE = "h1.text-uppercase span"
FIELD_DATE = "li.btn-info-detail.calendar span:first-of-type"
FIELD_SESSION = "li.btn-info-detail.session span"
FIELD_PRICE = "li.btn-info-detail.pricing"
FIELD_MARKER = "li.btn-info-detail.marker"
FIELD_CATEGORY = "p.category a"
OTHER_DATES = "#otherDates ul li span.other-event-date a"

# "Friday 02 October 2026" and "Thursday 29 October 2026, 7:00 PM - 9:00 PM"
DETAIL_DATE_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})")
DETAIL_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2})\s*([AaPp])\.?\s*[Mm]\.?")
OTHER_DATE_RE = re.compile(
    r"([A-Za-z]+day)\s+(\d{1,2})\s+([A-Za-z]+)\s+(\d{4}),?\s*"
    r"(?:(\d{1,2}):(\d{2})\s*([AaPp])\.?[Mm]\.?)?", re.I)

MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
          "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
          "november": 11, "december": 12}

# How far ahead a listing is published. The sitemap carries every occurrence the
# site knows about, which reaches a year out -- a series running to 2027-09 was
# the single worst row in the store before this source existed. The calendar is
# a rolling window (dedupe.PRUNE_DAYS is 90), so a source declaring its own is
# the honest way to bound it: the fetcher says which rows it chose not to
# publish, rather than a detail_cap silently truncating the crawl.
DEFAULT_HORIZON_DAYS = 120

# The pause between occurrence pages, and the reason it is not 0. This host
# rate-limits by IP: crawling all 876 sitemap pages at 0.1s apart returned
# "Your IP has been temporarily blocked, please try again later" (HTTP 409) on
# every page including the homepage, and the block outlived the run. 876 pages
# at 0.35s is about five minutes of deliberate waiting, which is cheaper than
# the crawl being refused. Configurable because a host under less pressure
# should not pay for another host's patience.
DEFAULT_CRAWL_DELAY = 0.35


def _text(node):
    return node.get_text(" ", strip=True) if node else ""


def _sitemap_urls(session, cfg):
    """Every occurrence URL, from the sitemap index and its children.

    A child that fails to load is a PartialFetch rather than a short list: the
    nine children partition the directory, so eight of nine publishes eight
    ninths of Frankston and nothing would say so.
    """
    index_url = cfg["sitemap"]
    html = get(session, index_url, retries=3)
    if not html:
        raise PartialFetch(f"sitemap index {index_url} failed to load")
    children = [u for u in SITEMAP_INDEX.findall(html)
                if EVENT_SITEMAP.search(u)]
    if not children:
        raise PartialFetch(
            f"{index_url} lists no event sitemaps -- the sitemap shape "
            f"changed, and publishing would drop the whole directory")
    urls = []
    for child in children:
        body = get(session, child, retries=3, min_len=500)
        if not body:
            raise PartialFetch(
                f"event sitemap {child} failed to load; the index lists "
                f"{len(children)} children and they partition the directory")
        found = [u for u in SITEMAP_INDEX.findall(body) if "/event/" in u]
        urls.extend(found)
        report(f"  {child.rsplit('/', 1)[-1]}: {len(found)} urls",
               level="debug")
    unique = sorted(set(urls))
    report(f"sitemap: {len(unique)} occurrence urls from "
           f"{len(children)} sitemaps")
    return unique


def _parse_when(text):
    """(date, "HH:MM") from a page's date+time wording, or (None, "").

    The detail page writes absolute dates ("Friday 02 October 2026") and
    12-hour times with a meridiem ("6:30 PM - 8:30 PM"). Both are read rather
    than the JSON-LD `startDate`, because this is where a series' *other*
    occurrences are listed and only prose carries them.
    """
    m = DETAIL_DATE_RE.search(text or "")
    if not m:
        return None, ""
    month = MONTHS.get(m.group(2).strip().lower()[:9])
    if not month:
        return None, ""
    try:
        day = date(int(m.group(3)), month, int(m.group(1)))
    except ValueError:
        return None, ""
    t = DETAIL_TIME_RE.search(text or "")
    if not t:
        return day, ""
    hour = int(t.group(1))
    if t.group(3).lower() == "p" and hour < 12:
        hour += 12
    elif t.group(3).lower() == "a" and hour == 12:
        hour = 0
    return day, f"{hour:02d}:{int(t.group(2)):02d}"


def _series_id_of(soup):
    """The site's own `eventIdentifier` GUID, or None.

    A source-supplied identity is authoritative where the derived one is
    inferred, so it goes in `series_id` and reconcile_store() prefers a row's
    own stamped value. Falls back to letting the caller derive one.
    """
    node = soup.select_one(RELATED)
    if not node:
        return None
    m = re.search(r"eventIdentifier=([0-9a-fA-F-]{8,})",
                  node.get("value", ""))
    return m.group(1) if m else None


def _venue_and_address(soup, ld):
    """(venue, address) preferring the visible block, falling back to JSON-LD.

    The visible `li.marker` is the only place the street number survives: the
    JSON-LD `streetAddress` for Friday Night Reset is `" High St"`, no house
    number, while the map block on the same page has "St Paul's Church hall,
    Cnr Bay & High streets". A reader needs the first.
    """
    marker = soup.select_one(FIELD_MARKER)
    venue = ""
    address = ""
    if marker:
        block = marker.select_one("div.btn-block span")
        venue = _text(block)
        address = _text(marker.find("span", recursive=False))
        if not address:
            address = _text(marker)
        if venue and venue in address:
            address = address.split(venue, 1)[1].strip(" ,")
    if not address:
        loc = ld.get("location") or {}
        name = (loc.get("name") or "").split(",")[0].strip()
        venue = venue or name
        parts = (loc.get("address") or {})
        street = (parts.get("streetAddress") or "").strip(" ,")
        if street:
            address = f"{street}, {parts.get('addressLocality') or ''}".strip()
            address = re.sub(r"\s+", " ", address).strip(" ,")
            if parts.get("postalCode"):
                address += f" {parts['postalCode']}"
    return venue, re.sub(r"\s+", " ", address).replace("\ufffd", " ").strip()


def _occurrence_dates(soup, first_iso):
    """Every date this page speaks for: its own, plus `#otherDates`.

    The page lists a series' sibling occurrences in prose ("Friday 09 October
    2026, 6:30 PM - 8:30 PM"), which is the only way to get more than one
    occurrence out of one request. It collapses after ten on a long series, so
    it is a supplement to the sitemap rather than a replacement for it.
    """
    found = {}
    own = _parse_when(_text(soup.select_one(FIELD_DATE))
                      + " " + _text(soup.select_one(FIELD_SESSION)))
    if own[0] is not None and first_iso:
        found[first_iso[:10]] = own[1]
    for a in soup.select(OTHER_DATES):
        text = a.get("title") or _text(a)
        m = OTHER_DATE_RE.search(text)
        if not m:
            continue
        month = MONTHS.get(m.group(3).strip().lower()[:9])
        if not month:
            continue
        try:
            day = date(int(m.group(4)), month, int(m.group(2)))
        except ValueError:
            continue
        stamp = ""
        if m.group(5):
            hour = int(m.group(5))
            if m.group(7).lower() == "p" and hour < 12:
                hour += 12
            elif m.group(7).lower() == "a" and hour == 12:
                hour = 0
            stamp = f"{hour:02d}:{int(m.group(6)):02d}"
        found.setdefault(day.isoformat(), stamp)
    return found


def fetch_everi(cfg, session, detail_cap=None):
    """Fetch an Everi "Event Hub" listing into dated rows.

    `detail_cap` bounds how many occurrence pages are opened. The default is not
    a budget but a bound: the horizon below decides what is published, and the
    cap exists only so a `--detail-cap` debug run does not fetch 876 pages.
    """
    sid = cfg["id"]
    set_reporting_source(sid)
    urls = _sitemap_urls(session, cfg)
    if not urls:
        raise PartialFetch(f"{sid}: the sitemap yielded no occurrence urls")

    horizon = date.today() + timedelta(days=int(
        cfg.get("horizon_days") or DEFAULT_HORIZON_DAYS))
    cap = detail_cap if detail_cap is not None else len(urls)
    delay = cfg.get("crawl_delay")
    delay = float(delay) if delay is not None else DEFAULT_CRAWL_DELAY

    rows, opened, seen_series = [], 0, set()
    beyond_horizon = failed = 0
    for url in urls:
        if opened >= cap:
            report(f"detail cap {cap} reached with {len(urls) - opened} "
                   f"pages unopened", level="warn")
            break
        html = get(session, url, retries=2, min_len=3000)
        if not html:
            failed += 1
            continue
        opened += 1
        soup = BeautifulSoup(html, "html.parser")
        if not soup.select_one(DETAIL):
            continue
        ld_node = soup.select_one(JSON_LD)
        try:
            ld = json.loads(ld_node.string) if ld_node else {}
        except (TypeError, ValueError):
            ld = {}
        start_iso = ld.get("startDate") or ""
        if start_iso[:10] and start_iso[:10] > horizon.isoformat():
            beyond_horizon += 1
            continue
        title = _text(soup.select_one(DETAIL_TITLE)) or ld.get("name") or ""
        if not title:
            continue
        venue, address = _venue_and_address(soup, ld)
        if not address and needs_address({"location": venue}):
            # No street anywhere on the page. Publishing the venue name as the
            # address would send a reader to a suburb on the strength of a place
            # they cannot look up -- see webfetch_granicus.drop_venueless().
            continue
        price = _text(soup.select_one(FIELD_PRICE))
        description = (ld.get("description") or "").strip() or title
        category = _text(soup.select_one(FIELD_CATEGORY))
        stamped = _series_id_of(soup)
        for when, stamp in _occurrence_dates(soup, start_iso).items():
            if when > horizon.isoformat():
                beyond_horizon += 1
                continue
            row = make_row(sid, title, url,
                           datetime_iso=f"{when}T{stamp or '00:00'}:00",
                           datetime_text=when,
                           location=venue,
                           address=address,
                           price_text=price,
                           description=description)
            if category:
                row["source_types"] = [category]
            row["series_id"] = stamped or None
            rows.append(row)
        if stamped:
            seen_series.add(stamped)
        time.sleep(delay)

    rows = [r for r in rows if r["series_id"]] or rows
    if not rows:
        raise PartialFetch(
            f"{sid}: {len(urls)} occurrence pages read and none carried a "
            f"name, a date and an address -- the detail markup has probably "
            f"changed")
    report(f"{sid}: {len(rows)} rows from {opened} pages, "
           f"{len(seen_series)} series, {beyond_horizon} beyond the "
           f"{horizon.isoformat()} horizon, {failed} unreadable")
    return rows


def _self_test():
    import sys

    failures = []

    def check(label, actual, expected):
        ok = actual == expected
        print("  %s %s%s" % ("ok  " if ok else "FAIL", label,
                             "" if ok else
                             "\n         actual:   %r\n         expected: %r"
                             % (actual, expected)))
        if not ok:
            failures.append(label)

    check("a detail date and session parse",
          _parse_when("Friday 02 October 2026 6:30 PM - 8:30 PM"),
          (date(2026, 10, 2), "18:30"))
    check("a midnight session does not become noon",
          _parse_when("Friday 02 October 2026 12:30 AM"), (date(2026, 10, 2),
                                                          "00:30"))
    check("noon is noon",
          _parse_when("Friday 02 October 2026 12:00 PM"),
          (date(2026, 10, 2), "12:00"))
    check("a date with no month name is not guessed",
          _parse_when("sometime soon"), (None, ""))
    check("an impossible date is refused",
          _parse_when("Friday 31 February 2026"), (None, ""))

    page = """<div id="divEventDetail">
      <div class="col-sm-6 right">
        <h1 class="text-uppercase"><span>Friday Night Reset</span></h1>
        <ul><li class="btn-info-detail calendar">
              <span>Friday 02 October 2026</span></li>
            <li class="btn-info-detail session"><span>6:30 PM - 8:30 PM</span></li>
            <li class="btn-info-detail pricing">$25 pay what you can</li>
            <li class="btn-info-detail marker">
              <div class="btn-block"><span>Saint Pauls Hall</span></div>
              <span>Cnr Bay &amp; High St, Frankston VIC 3199</span></li></ul>
        <div id="otherDates"><ul>
          <li><span class="other-event-date"><a title="Friday 09 October 2026, 6:30 PM - 8:30 PM">x</a></span></li>
          <li><span class="other-event-date"><a title="Friday 16 October 2026, 6:30 PM - 8:30 PM">x</a></span></li>
        </ul></div>
        <p class="category"><a href="/lifestyle-and-community">Lifestyle and Community</a></p>
      </div></div>
      <input type="hidden" id="relateEventUrl"
        value="/event/related-events?eventId=34282077-a&amp;eventIdentifier=1ff2f53c-b302-4f84-b8ef-821261f4bf89&amp;suburb=frankston">"""
    soup = BeautifulSoup(page, "html.parser")
    check("the venue comes from the named block",
          _text(soup.select_one("li.btn-info-detail.marker div.btn-block span")),
          "Saint Pauls Hall")
    check("the street keeps its house number, unlike the JSON-LD's",
          _venue_and_address(soup, {"location": {"address": {
              "streetAddress": " High St",
              "addressLocality": "Frankston", "postalCode": "3199"}}})[1],
          "Cnr Bay & High St, Frankston VIC 3199")
    check("the site's own series GUID is read",
          _series_id_of(soup), "1ff2f53c-b302-4f84-b8ef-821261f4bf89")
    dates = _occurrence_dates(soup, "2026-10-02T18:30:00")
    check("the page's own date and its siblings are all collected",
          sorted(dates), ["2026-10-02", "2026-10-09", "2026-10-16"])
    check("each date carries the stated start time",
          sorted({v for v in dates.values()}), ["18:30"])
    check("the price is read from the page, never the JSON-LD offers",
          _text(soup.select_one(FIELD_PRICE)), "$25 pay what you can")
    check("the category is read",
          _text(soup.select_one(FIELD_CATEGORY)), "Lifestyle and Community")

    if failures:
        print(f"\nwebfetch_everi: {len(failures)} case(s) FAILED")
        sys.exit(1)
    print("\nall Everi-reader cases as expected")


if __name__ == "__main__":
    _self_test()