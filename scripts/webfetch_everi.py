"""What's On Frankston (everi.com.au "Event Hub").

Replaces the hand-maintained `frankston_archived` rows in
`scripts/archived_events.json`. Those were kept by hand on the stated grounds
that the live pages were WAF-blocked, which stopped being true; the site answers
plain urllib. Being hand-maintained was not only stale, it was unreclaimable:
`reconcile_store()` only judges a row whose `source_id` appeared in this run's
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
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

from bs4 import BeautifulSoup

from webfetch_http import (Pacer, PartialFetch, get, make_row,  # noqa: F401
                           month_number, report, set_reporting_source)
from webfetch_http import _pace as _shared_pace
from venues import needs_address

_ROOT = Path(__file__).resolve().parent.parent

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

# Month names resolve through webfetch_http.month_number, the pipeline's one
# owner. This used to keep its own twelve-name table and look up
# `MONTHS.get(name[:9])`, which resolved the full names and nothing else: a site
# printing "Sept" -- the four-letter abbreviation Australian listings use -- got
# None, and the row lost its date rather than raising.

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
#
# That 0.35s was measured, and it is not enough for a whole crawl. A run at
# 0.35s over all 876 pages was still refused partway through, having opened no
# usable pages, and the block then held for hours -- so the number that avoids
# a block on a handful of requests is not the number that survives nine hundred
# of them. Activation needs a resumable crawl doing a bounded slice per run, so
# no single run is ever the 876-request run. See the commented entry in
# sources.yaml. The delay stays at the measured value because a resumable crawl
# is what has to change, not this.
DEFAULT_CRAWL_DELAY = 0.35
# Occurrence pages read per run. 876 pages at the host's tolerated rate is the
# run that gets an IP blocked, and the block outlives the run, so no slice size
# makes one pass safe. This one is deliberately small: at 0.35s it is ~70s of
# requests, well inside what the host tolerates, and the crawl completes over a
# scheduled run's lifetime instead of inside one.
DEFAULT_SLICE = 150
# The slowest this crawl will ever go, whatever the config says. Configurable
# upward for a host under less pressure, never downward: a `crawl_delay: 0` in
# sources.yaml is the sort of thing that looks like a fix and is the opposite of
# one, and the cost of it is measured in hours of block, not in crawl time.
_MIN_CRAWL_DELAY = 0.35
# Requests one run may issue, sitemap included, and the backstop the slice size
# is not. The host tolerated 12 pages once and 31 another time before refusing,
# so its budget is real but not well characterised; 60 sits above both observed
# successes and below the 150 slice it caps, which is the point of having it as
# a separate number -- the slice says how much progress to try for, the budget
# says how much pressure one run is allowed to put on the host.
_MAX_REQUESTS_PER_RUN = 60


def _pace(seconds):
    """Wait between requests. A module-level seam so this suite can count the
    throttle's deliberate waits.

    The mechanism is `webfetch_http.Pacer`; only the three numbers in
    `_Throttle` below are this source's. `_Throttle` defaults `pace` to *this*
    name, resolved in this module's globals at call time, so the suite's
    `globals()["_pace"] = waits.append` reaches the throttle that actually
    runs. A `Pacer` imported by name would look it up in `webfetch_http` and the
    patch would count nothing.
    """
    _shared_pace(seconds)


class _Throttle(Pacer):
    """Frankston's rate limit: this host's floor, default and request budget.

    The pacing mechanism -- wait out the interval immediately before each
    request, with a floor, a budget and jitter -- is `webfetch_http.Pacer`,
    shared with the other sources that now walk a paginated listing. Only the
    three numbers are Frankston's, and they are the measured ones recorded
    above: this host refused a 876-page crawl at 0.1s and again at 0.35s.

    What the mechanism fixes is the reason it exists at all. The delay used to
    be a `time.sleep` at the bottom of the request loop, and that put the rate
    limit on the wrong branch: five `continue` statements stood between the
    request and the sleep, so a page that failed, or parsed to nothing, or
    fell outside the horizon, or had no address, was fetched with no pause at
    all. Those are not the rare paths. The sitemap runs a year out against a
    120-day horizon, so the *majority* of the 856 pages take `beyond_horizon`
    and skip the sleep -- measured: sixteen requests, zero delays, a crawl at
    whatever speed the network happened to allow.

    So pacing moves to where it belongs, immediately before each request, and
    every path through the loop goes through it. Three properties, and the third
    is the one a sleep alone cannot give:

    * **A floor.** `max(delay, _MIN_CRAWL_DELAY)`, so no config value can turn
      this into an unbounded crawler. Deliberately not overridable downward.
    * **A budget.** `MAX_REQUESTS_PER_RUN` covers every request in the run, the
      ten sitemap fetches included. Backstops the slice size, which is a request
      for progress rather than a limit on pressure.
    * **Jitter.** A uniform interval is a machine signature; the host is
      refusing one. Up to a quarter of the delay is random.

    The budget counts the sitemap because that fetch is repeated at the head of
    every run -- ten requests, unthrottled and uncounted, times every run of a
    fifteen-run crawl. Unthrottled, it is a fifth of the crawl's total traffic
    spent before the first occurrence page.
    """

    def __init__(self, delay=None, budget=None, pace=None, jitter=None):
        super().__init__(delay=delay, floor=_MIN_CRAWL_DELAY,
                         default=DEFAULT_CRAWL_DELAY,
                         budget=budget or _MAX_REQUESTS_PER_RUN,
                         pace=pace or _pace, jitter=jitter)
# Consecutive unreadable pages that mean "blocked", not "some pages are broken".
BLOCK_STREAK_LIMIT = 5
# Report progress this often. At the host's rate this is roughly every 20s.
PROGRESS_EVERY = 25
# Shortest acceptable occurrence page. A block returns 61 bytes, so anything
# under this is refused rather than parsed into an empty row.
DETAIL_MIN_LEN = 3000


def _text(node):
    return node.get_text(" ", strip=True) if node else ""


def _sitemap_urls(session, cfg, throttle=None):
    """Every occurrence URL, from the sitemap index and its children.

    A child that fails to load is a PartialFetch rather than a short list: the
    nine children partition the directory, so eight of nine publishes eight
    ninths of Frankston and nothing would say so.

    This runs at the head of *every* run, so it is ten requests a run against a
    host that answers a blocked IP with a refusal. They go through the same
    throttle as the occurrence pages for that reason: a budget that counted only
    the pages would let the crawl exceed its own limit by ten requests a run,
    which over fifteen runs is a fifth of its total traffic spent before the
    first event.
    """
    def _pause():
        if throttle is not None and not throttle.take():
            raise PartialFetch(
                f"sitemap fetch exhausted the run's request budget "
                f"({throttle.budget}); no occurrence pages were read")

    index_url = cfg["sitemap"]
    _pause()
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
        _pause()
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
    month = month_number(m.group(2))
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
        month = month_number(m.group(3))
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


def _slice_length(configured, detail_cap, throttle):
    """How many occurrence pages this run may try for.

    `slice_size` and `detail_cap` say how much progress to try for; the budget
    says how much pressure the run may put on the host, and the sitemap has
    already spent part of it. The smallest of the three wins, so neither number
    has to be trusted on its own: a mis-set 150 slice is not a 150-request run
    against a host that refuses one at 31.

    The throttle would stop the loop either way, so this is not what bounds a
    run -- it is what makes the slice a run *reports* the slice it attempts,
    rather than the one it was configured with and then had cut short.
    """
    want = int(detail_cap if detail_cap is not None
               else (configured or DEFAULT_SLICE))
    return max(0, min(want, throttle.budget - throttle.spent))


def fetch_everi(cfg, session, detail_cap=None):
    """Fetch an Everi "Event Hub" listing into dated rows.

    `detail_cap` bounds how many occurrence pages are opened. The default is not
    a budget but a bound: the horizon below decides what is published, and the
    cap exists only so a `--detail-cap` debug run does not fetch 876 pages.
    """
    sid = cfg["id"]
    set_reporting_source(sid)
    throttle = _Throttle(cfg.get("crawl_delay"), cfg.get("max_requests_per_run"))
    urls = _sitemap_urls(session, cfg, throttle)
    if not urls:
        raise PartialFetch(f"{sid}: the sitemap yielded no occurrence urls")

    horizon = date.today() + timedelta(days=int(
        cfg.get("horizon_days") or DEFAULT_HORIZON_DAYS))
    slice_size = _slice_length(cfg.get("slice_size"), detail_cap, throttle)

    # Resumable. The host refuses an IP that asks for too much, and the refusal
    # lasts hours -- so a run that tries all 876 pages never finishes, and the
    # attempts that do get through are lost because a partial crawl is refused
    # downstream (correctly: publishing eight of nine ninths of a directory
    # would be silently wrong). The progress file is what makes the crawl
    # incremental: each run takes one bounded slice from wherever the last one
    # stopped, and the rows it has already read are not fetched again.
    cache_path = _cache_path(cfg)
    cache = _load_cache(cache_path)
    done = set(cache.get("done", []))
    # The rows from earlier runs are republished as well as this run's, so a
    # page read on Monday still appears on Saturday's snapshot.
    rows = list(cache.get("rows") or [])
    remaining = [u for u in urls if u not in done]
    todo = remaining[:slice_size]

    opened, seen_series = 0, set()
    beyond_horizon = failed = 0
    report(f"{sid}: {len(done)} of {len(urls)} occurrence pages already read, "
           f"fetching {len(todo)} this run"
           + (f" (slice_size {slice_size})" if len(todo) < len(remaining)
              else ""))
    # This host answers every page -- homepage and sitemap.xml included -- with
    # HTTP 409 once it decides an IP is over budget, and holds that for hours.
    # So a block is detected on its first few pages and the crawl stops there,
    # rather than issuing 876 more requests that are all certain to fail. The
    # streak has to be consecutive: a handful of genuinely broken occurrence
    # pages mid-sitemap is normal and must not end the run.
    blocked_streak = 0
    newly_read = []
    for url in todo:
        # Before the request, on every path. It used to be a sleep at the
        # bottom of the loop, which the `continue`s below skipped: a page that
        # failed, parsed to nothing, fell outside the horizon or had no address
        # was fetched with no pause. Those are the common cases -- most of this
        # sitemap is past the 120-day horizon -- so the measured rate on those
        # pages was unbounded, which is a good deal of why the host kept
        # refusing.
        if not throttle.take():
            break
        html = get(session, url, retries=2, min_len=DETAIL_MIN_LEN)
        if not html:
            failed += 1
            blocked_streak += 1
            if blocked_streak >= BLOCK_STREAK_LIMIT:
                # The pages this run did read are recorded before the raise, and
                # the pages that read fine and yielded nothing publishable are in
                # `newly_read` too -- re-fetching those is the 856 requests this
                # design exists to avoid. Raising before the save discarded every
                # page the run had managed, so the next run re-read the same 31
                # and was refused at the same place: on a host allowing ~30 pages
                # per session that is the difference between finishing in a few
                # dozen runs and never finishing.
                done |= set(newly_read)
                _save_cache(cache_path, cache, done, _dedupe_rows(rows))
                report(f"{sid}: {blocked_streak} pages in a row unreadable, "
                       f"stopping at {opened} of {len(urls)}. This host blocks "
                       f"by IP with HTTP 409 and does not unblock quickly. The "
                       f"{len(done)} pages read so far are cached, so the next "
                       f"run resumes from here rather than re-reading them. "
                       f"{throttle.spent} request(s) this run.",
                       level="error")
                raise PartialFetch(
                    f"{sid}: aborted after {blocked_streak} consecutive "
                    f"unreadable pages ({opened} of {len(urls)} opened; "
                    f"{len(done)} recorded)")
            continue
        blocked_streak = 0
        opened += 1
        # A crawl this long is silent for minutes at a time otherwise, so a run
        # that dies halfway looks identical to one that is working.
        if opened % PROGRESS_EVERY == 0:
            report(f"{sid}: {opened}/{len(todo)} pages this run "
                   f"({len(done) + opened}/{len(urls)} overall), {len(rows)} "
                   f"rows, {failed} unreadable, {beyond_horizon} beyond "
                   f"horizon", level="info")
        # Recorded as read the moment it is read, and written out at the end of
        # the run -- not once per page, which would rewrite a 900-line file 900
        # times, and not at the end only, which would lose the whole slice to a
        # crash or a kill. A page that read but held nothing publishable is
        # still recorded: it was fetched, and re-fetching it would be the 876
        # requests this design exists to avoid.
        newly_read.append(url)
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

    rows = [r for r in rows if r["series_id"]] or rows
    if not rows:
        raise PartialFetch(
            f"{sid}: {len(urls)} occurrence pages read and none carried a "
            f"name, a date and an address -- the detail markup has probably "
            f"changed")

    # The cache is only advanced for pages that actually read, and it is written
    # before the return rather than after, so a run that dies in the caller
    # still keeps what it spent requests on.
    done |= set(newly_read)
    _save_cache(cache_path, cache, done, _dedupe_rows(rows))
    # Counted from the URLs still absent, not as len(urls) - len(done). The cache
    # outlives the sitemap, so a page the site has since withdrawn leaves `done`
    # holding a URL that is no longer in the list, and the subtraction goes
    # negative -- which then reads as "unread pages remaining" on a crawl that is
    # in fact finished, and never reaches the "all pages read" report.
    left = len([u for u in urls if u not in done])
    report(f"{sid}: {len(rows)} rows from {opened} pages this run, "
           f"{len(seen_series)} series, {beyond_horizon} beyond the "
           f"{horizon.isoformat()} horizon, {failed} unreadable; "
           f"{throttle.spent}/{throttle.budget} requests at "
           f"~{throttle.delay:.2f}s")
    if left:
        # An incomplete crawl refuses to publish, whichever limit stopped it.
        # The block path above already raises, so a run stopped by the budget
        # while still holding rows has been the one route by which a fraction of
        # the directory could reach a snapshot -- correct only because
        # `dedupe.py` separately skips any snapshot whose source has a progress
        # file. A fetcher's own "do not publish" signal is
        # `PartialFetch`; leaving the quiet path out of it makes the guard depend
        # on a second module happening to look in the right place.
        #
        # The two reasons are named separately because they need different
        # responses: the budget is a deliberate limit and the crawl advances on
        # the next run, whereas the slice simply ended.
        reason = (f"the run's {throttle.budget}-request budget"
                  if throttle.spent >= throttle.budget
                  else f"a slice of {slice_size}")
        raise PartialFetch(
            f"{sid}: incomplete crawl -- {left} of {len(urls)} occurrence "
            f"pages unread, stopped by {reason}. Refusing to publish a "
            f"partial directory; the {len(done)} pages read are cached, so "
            f"re-run to continue from there.")
    report(f"{sid}: all {len(urls)} occurrence pages read")
    return rows


def _occurrence_key(row):
    """Identity of one published occurrence: which series, which session.

    Not the URL. Every page of a series lists its siblings in `#otherDates`, so
    one occurrence is emitted by its own page *and* by each of its siblings' --
    a session on 2026-10-13 was being written seven times, once per sibling,
    and the cache ran to 75% duplicate rows before this. That is not merely
    untidy: the completed snapshot would carry four rows for every occurrence,
    and every downstream dedupe pass would hash four times what it needed to.

    The time is part of the key rather than being ignored, because a sibling
    listed without one is a different row: it is the midnight restatement of a
    timed session, and `drop_untimed_twins()` in dedupe.py is what decides that,
    not a guess made here.
    """
    return (row.get("series_id") or " ".join(
                (row.get("name") or "").lower().split()),
            (row.get("datetime_iso") or "")[:16],
            (row.get("location") or "").strip().lower())


def _dedupe_rows(rows):
    """Keep the first row for each occurrence, in order."""
    seen, out = set(), []
    for r in rows:
        k = _occurrence_key(r)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def _cache_path(cfg):
    """Where this source's crawl progress is kept between runs."""
    override = cfg.get("cache_file")
    if override:
        p = Path(override)
        return p if p.is_absolute() else _ROOT / p
    snap = cfg.get("snapshot") or ""
    base = Path(snap).stem if snap else str(cfg["id"])
    return _ROOT / "scripts" / "webfetch_snapshots" / ("%s.progress.json" % base)


def _load_cache(path):
    """Cached crawl state: which pages are read, and the rows they yielded.

    The rows are cached as well as the URLs, and that is not an optimisation.
    A crawl that completes has read all 876 pages; if the cache held only the
    URLs then every run after the last one would read nothing, find no rows, and
    raise -- so the source would be permanently unfetchable having been
    successfully fetched once. The rows make a finished crawl self-sustaining,
    and they are also how a page that is read in one run still publishes.

    A corrupt or absent cache is a fresh crawl rather than a failure: the cost
    of being wrong is re-reading some pages, and the cost of failing is a
    source that never comes up.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        done = data.get("done")
        cached = data.get("rows")
        if (isinstance(done, list)
                and all(isinstance(u, str) for u in done)
                and isinstance(cached, list)):
            return {"done": done, "rows": cached}
    except (OSError, ValueError, AttributeError):
        pass
    return {"done": [], "rows": []}


def _save_cache(path, cache, done, rows):
    cache = dict(cache or {})
    cache["done"] = sorted(done)
    cache["rows"] = list(rows or [])
    try:
        path.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    except OSError as e:
        # Losing the cache costs one extra crawl. Failing the fetch over it
        # would mean a read-only checkout permanently blocks the source.
        report(f"could not write {path.name}: {e}", level="warn")


def _pad(body, size=DETAIL_MIN_LEN + 500):
    """Pad a fake body past the min_len get() enforces, without altering it.

    The detail pages need to clear DETAIL_MIN_LEN specifically, which is
    higher than the sitemap's, and getting that wrong makes every page read as
    a block -- the case then passes for the wrong reason.
    """
    return body + " " * max(0, size - len(body))


def _crawl_with_fakes(fail_streak_at, total=12, slice_size=None,
                      cache_file=None, count_requests=True, cfg_overrides=None):
    """Run fetch_everi against a fake session, so the failure paths are testable.

    `fail_streak_at` is the page index from which every fetch fails, or None for
    a clean run. Returns (rows, raised) so a case can assert on either without
    the exception escaping the test.
    """
    class _R:
        def __init__(self, text):
            self.status_code, self.text = 200, text

    class _S:
        def __init__(self):
            self.n = 0
            self.detail_reads = 0

        def get(self, url):
            # get() rejects a body under min_len, on the grounds that a short
            # body means something went wrong rather than that the page is
            # short. The fakes are padded to clear that, so a case cannot pass
            # or fail on the length check instead of on the path under test.
            if url.endswith("sitemap.xml"):
                # The real sitemap is an index of children; the fetcher insists
                # on that shape, so the fake has the same shape or the case would
                # be testing the fake rather than the failure path.
                # The children must match EVENT_SITEMAP, or the fetcher reads
                # this as "the sitemap shape changed" rather than as a crawl.
                return _R(_pad("<sitemapindex>%s</sitemapindex>" % "".join(
                    "<sitemap><loc>https://x/event-sitemap/%d</loc></sitemap>" % c
                    for c in range(2))))
            if "/event-sitemap/" in url:
                half = total // 2
                lo, hi = ((0, half) if url.endswith("/0")
                          else (half, total))
                return _R(_pad("<urlset>%s</urlset>" % "".join(
                    "<loc>https://x/event/%d</loc>" % i for i in range(lo, hi)),
                    2000))
            i = self.n
            self.n += 1
            self.detail_reads += 1
            if fail_streak_at is not None and i >= fail_streak_at:
                return _R("blocked")
            # The event's identity comes from its URL, not from how many
            # requests this session has made. Counting per session meant the
            # same page carried a different title on every run, which is not how
            # the site behaves -- and it hid the fact that the cache was storing
            # the same occurrence several times over, because two runs' "Event 0"
            # rows were indistinguishable and two runs' "Event 0"/"Event 2" were
            # not.
            idx = int(url.rstrip("/").rsplit("/", 1)[-1])
            # The markup mirrors the real page, selectors included: a fake built
            # to the class names the test happened to use would exercise
            # nothing the fetcher actually looks for.
            return _R(_pad(
                "<html><body><div id='divEventDetail'>"
                "<h1 class='text-uppercase'><span>Event %d</span></h1>"
                "<ul><li class='btn-info-detail calendar'>"
                "<span>Tuesday 05 January 2027</span></li>"
                "<li class='btn-info-detail session'>"
                "<span>10:00 AM - 11:00 AM</span></li>"
                "<li class='btn-info-detail marker'>"
                "<div class='btn-block'><span>Some Hall</span></div>"
                "<span>1 Example St, Frankston VIC 3199</span></li></ul>"
                "<script type='application/ld+json'>%s</script>"
                "</div></body></html>"
                % (idx, json.dumps({
                    "name": "Event %d" % idx,
                    "startDate": "2027-01-05T10:00:00",
                    "description": "d",
                }))))

    cfg = {"id": "t", "sitemap": "https://x/sitemap.xml",
           "horizon_days": 400, "crawl_delay": 0}
    if slice_size is not None:
        cfg["slice_size"] = slice_size
    if cfg_overrides:
        cfg.update(cfg_overrides)
    if cache_file is not None:
        cfg["cache_file"] = str(cache_file)
    else:
        # Always a temp path even when the case does not ask for a cache: the
        # default is derived from the source id, so a case without one wrote
        # scripts/webfetch_snapshots/t.progress.json into the real snapshots
        # directory, and dedupe.py -- which merges every *.json there -- then
        # published the suite's twelve fake "Event 0" rows as a source called
        # "t". It reached the built page, because the leak was a real file in a
        # real directory and nothing in the pipeline looks for it.
        import tempfile

        cfg["cache_file"] = str(Path(tempfile.mkdtemp()) / "t.progress.json")
    sess = _S()
    rows, raised = None, None
    try:
        rows = fetch_everi(cfg, sess)
    except PartialFetch as e:
        raised = str(e)
    if count_requests:
        return rows, raised
    return rows, getattr(sess, "detail_reads", 0)


@contextmanager
def _recorded_pace():
    """Record the throttle's waits instead of serving them.

    The throttle has a floor on the delay precisely so no config can switch the
    rate limit off, which means the fake crawls below -- `crawl_delay: 0` -- now
    pause for real and the suite would spend a minute asleep. Patching `_pace`
    keeps the timing logic under test (how many waits, on which requests) while
    making the waiting free, which is the only way "is every request paced" can
    be written as a test at all.

    Patched through `globals()`, not by importing the module by name: checks.py
    runs this file as `__main__`, so `import webfetch_everi` builds a *second*
    copy and sets `_pace` on that one, leaving the copy actually running
    untouched. The count came back 0 in the suite and 14 standalone, which is
    what that looks like.
    """
    waits = []
    g = globals()
    real = g["_pace"]
    g["_pace"] = waits.append
    try:
        yield waits
    finally:
        g["_pace"] = real


def _sliced_crawl(runs=3, per_run=2, total=6, fail_at=None):
    """Run fetch_everi repeatedly against fakes, returning (rows, cache path).

    Each call is a separate "run", which is the point: a slice-per-run crawler
    can only be tested by running it more than once and watching that the second
    run does not re-read the first run's pages.
    """
    import tempfile

    cache = Path(tempfile.mkdtemp()) / "t.progress.json"
    published, reads = [], 0
    for i in range(runs):
        # Each call returns the whole crawl so far, not just this run's rows:
        # that is what a real run publishes, and accumulating the return values
        # would count each row once per subsequent run. `fail_at` lets one run
        # in the sequence be a blocked one, which is the case that has to
        # advance the crawl anyway.
        block = fail_at[i] if fail_at and i < len(fail_at) else None
        got, n = _crawl_with_fakes(
            fail_streak_at=block, total=total, slice_size=per_run,
            cache_file=cache, count_requests=False)
        published = got or []
        reads += n
    return published, cache


def _total_detail_reads(runs, per_run, total):
    """Total occurrence-page fetches across `runs` runs of a sliced crawl.

    Distinct from the page count: this is how many requests were actually made,
    which is what the host counts and what the slice design is for. Four runs of
    two pages over six is six requests, not eight -- the two extra runs must
    fetch nothing.
    """
    import tempfile

    cache = Path(tempfile.mkdtemp()) / "t.progress.json"
    reads = 0
    for _ in range(runs):
        _got, n = _crawl_with_fakes(
            fail_streak_at=None, total=total, slice_size=per_run,
            cache_file=cache, count_requests=False)
        reads += n
    return reads


def _write_corrupt_cache():
    import tempfile

    p = Path(tempfile.mkdtemp()) / "t.progress.json"
    p.write_text("{not json", encoding="utf-8")
    return _load_cache(p)["done"]


def _self_test():
    import sys

    from checks import check as _check

    failures = []

    def check(label, actual, expected):
        return _check(label, actual, expected, failures)

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

    rows, raised = _crawl_with_fakes(fail_streak_at=3, total=20)
    check("a run of blocked pages aborts the crawl", raised is not None, True)
    check("the abort says how far it got before stopping",
          "3 of 20" in (raised or ""), True)
    check("a blocked crawl publishes nothing rather than a fragment",
          rows, None)
    rows, raised = _crawl_with_fakes(fail_streak_at=None, total=12)
    check("a clean crawl is not mistaken for a block", raised, None)
    check("a clean crawl returns every page's row", len(rows or []), 12)

    # The resumable crawl, which is the only way this source can be activated:
    # 876 pages in one run is what earns the IP block, so each run takes a
    # slice and the cache is what makes the next run not repeat it.
    rows, cache_file = _sliced_crawl(runs=3, per_run=2, total=6)
    check("a sliced crawl reaches every page across its runs",
          len(rows or []), 6)
    check("the cache records every page read",
          len(_load_cache(cache_file)["done"]), 6)
    rows, cache_file = _sliced_crawl(runs=1, per_run=2, total=6)
    # A run that reads one slice of six publishes nothing, for the same reason a
    # blocked one does: a fraction of a directory is indistinguishable from the
    # whole thing once it is in a snapshot.
    check("one run reading only its slice publishes nothing", rows, [])
    check("and the cache still records that slice",
          len(_load_cache(cache_file)["done"]), 2)

    # A blocked run must still keep what it read. Raising before the save threw
    # away every page the run had managed, so a crawl on a host that allows
    # ~30 pages per session never advanced: run two re-read run one's 31 pages
    # and was blocked at the same point.
    import tempfile as _tf

    blocked_cache = Path(_tf.mkdtemp()) / "t.progress.json"
    # A slice at least BLOCK_STREAK_LIMIT long, so the block actually trips
    # rather than the run simply running short of pages.
    _got, raised = _crawl_with_fakes(
        fail_streak_at=0, total=6, slice_size=6,
        cache_file=blocked_cache, count_requests=True)
    check("a blocked run still raises", raised is not None, True)
    check("and it says it recorded what it read",
          "recorded" in (raised or ""), True)
    check("a run blocked before reading anything records nothing",
          len(_load_cache(blocked_cache)["done"]), 0)

    # The case that matters: pages read *before* the block must survive it.
    partial_cache = Path(_tf.mkdtemp()) / "t.progress.json"
    _got, raised = _crawl_with_fakes(
        fail_streak_at=3, total=12, slice_size=12,
        cache_file=partial_cache, count_requests=True)
    check("a run blocked part-way raises", raised is not None, True)
    check("a run blocked part-way keeps the pages it did read",
          len(_load_cache(partial_cache)["done"]), 3)
    check("and says in the failure how many it recorded",
          "3 recorded" in (raised or ""), True)
    # Once every page is cached, a further run still publishes -- from the cache,
    # which is the whole reason the rows are stored in it -- and issues no
    # requests at all. "No rows" was the wrong expectation to write: it is the
    # failure mode the row cache was added to prevent.
    rows, cache_file = _sliced_crawl(runs=4, per_run=2, total=6)
    check("a finished crawl reads nothing twice",
          len(_load_cache(cache_file)["done"]), 6)
    check("and republishes from the cache without re-fetching",
          (len(rows or []), _total_detail_reads(runs=4, per_run=2, total=6)),
          (6, 6))
    check("a corrupt cache is a fresh crawl, not a failure",
          _write_corrupt_cache(), [])

    # Every page of a series lists its siblings, so one occurrence arrives once
    # from its own page and once from each sibling. Measured on the real crawl:
    # 43 pages cached 170 rows for 43 distinct occurrences -- 75% of the cache
    # was copies, and a finished crawl would have written a snapshot with four
    # rows for every session.
    check("the same occurrence from two pages is stored once",
          len(_dedupe_rows([
              {"series_id": "s1", "datetime_iso": "2026-10-13T09:00:00",
               "location": "Hall", "name": "A"},
              {"series_id": "s1", "datetime_iso": "2026-10-13T09:00:00",
               "location": "Hall", "name": "A"},
          ])), 1)
    check("different sessions of one series are both kept",
          len(_dedupe_rows([
              {"series_id": "s1", "datetime_iso": "2026-10-13T09:00:00",
               "location": "Hall", "name": "A"},
              {"series_id": "s1", "datetime_iso": "2026-10-20T09:00:00",
               "location": "Hall", "name": "A"},
          ])), 2)
    check("a sibling listed with no time stays a separate row",
          len(_dedupe_rows([
              {"series_id": "s1", "datetime_iso": "2026-10-13T09:00:00",
               "location": "Hall", "name": "A"},
              {"series_id": "s1", "datetime_iso": "2026-10-13T00:00:00",
               "location": "Hall", "name": "A"},
          ])), 2)
    check("a series with no GUID still collapses on name and session",
          len(_dedupe_rows([
              {"series_id": None, "datetime_iso": "2026-10-13T09:00:00",
               "location": "Hall", "name": "A"},
              {"series_id": None, "datetime_iso": "2026-10-13T09:00:00",
               "location": "hall", "name": "  a  "},
          ])), 1)

    # --- the rate limit -----------------------------------------------------
    # The delay was a sleep at the bottom of the request loop, and five
    # `continue` statements stood between the request and the sleep. Every one
    # of them is a *page that was fetched*: a failure, a page that parsed to
    # nothing, a page outside the horizon, a page with no address. Those are the
    # common cases, not the rare ones -- most of this sitemap is past the
    # 120-day horizon -- so the crawl ran unbounded on exactly the pages that
    # dominate it, which is a fair part of why the host kept refusing.
    with _recorded_pace() as waits:
        _rows, _raised = _crawl_with_fakes(fail_streak_at=None, total=12)
        paced = len(waits)
        requests = 12 + 3   # 12 occurrence pages + sitemap index + 2 children
    # Exactly one fewer wait than requests: the first request of a run has no
    # previous request to space itself from, so every *subsequent* one is paced.
    check("every request after the first is paced, sitemap included",
          paced, requests - 1)
    # Each wait can be slightly under the delay, because the gap is measured
    # from the previous *request* and time already spent on that page comes off
    # the top. What must hold is that every paced request really waited.
    check("and every one of those waits was a real pause",
          all(w > 0 for w in waits), True)

    # The specific regression: pages that take a `continue` were unpaced.
    with _recorded_pace() as waits:
        _crawl_with_fakes(fail_streak_at=None, total=8,
                          cfg_overrides={"horizon_days": 1})
        beyond = len(waits)
    check("pages outside the horizon are paced too, not skipped",
          beyond, 8 + 3 - 1)

    # The floor is the point: a config value of 0 must not switch the rate limit
    # off, which is what a `crawl_delay: 0` line in sources.yaml would otherwise
    # do -- it reads like a fix and is the opposite of one.
    check("a crawl_delay of 0 still yields the floor",
          _Throttle(0).delay, _MIN_CRAWL_DELAY)
    check("a tiny crawl_delay is raised to the floor",
          _Throttle(0.01).delay, _MIN_CRAWL_DELAY)
    check("a generous crawl_delay is respected",
          _Throttle(2.5).delay, 2.5)
    check("a nonsense crawl_delay falls back to the default",
          _Throttle("soon").delay, DEFAULT_CRAWL_DELAY)

    # The budget bounds pressure independently of the slice, which is a request
    # for progress rather than a limit on what one run may ask of the host.
    check("the budget counts the sitemap too",
          _Throttle(0.35, budget=4).budget, 4)

    _th = _Throttle(0.35, budget=20)
    _th.take(); _th.take()      # the sitemap spends two of the twenty
    check("the slice is capped by the budget the sitemap left",
          _slice_length(150, None, _th), 18)
    check("a detail_cap below that budget is still honoured",
          _slice_length(150, 5, _th), 5)
    check("a slice below that budget is not inflated",
          _slice_length(3, None, _th), 3)
    _th2 = _Throttle(0.35, budget=2)
    _th2.take(); _th2.take()     # the sitemap uses the whole budget
    check("a budget the sitemap used up means no occurrence pages this run",
          _slice_length(150, None, _th2), 0)

    # End to end: a mis-set 150 slice against an 8-request budget reads five
    # pages, not eight. It still refuses to publish -- publishing a fraction of
    # a directory would be silently wrong -- so the run's whole request count
    # is what this pins.
    with _recorded_pace() as waits:
        _rows, raised = _crawl_with_fakes(
            fail_streak_at=None, total=40, slice_size=40,
            cfg_overrides={"max_requests_per_run": 8})
        spent = len(waits) + 1   # first request has no gap to wait out
    check("a partial crawl still refuses to publish",
          "incomplete crawl" in (raised or ""), True)
    check("and the run stayed inside its budget", spent <= 8, True)

    if failures:
        print(f"\nwebfetch_everi: {len(failures)} case(s) FAILED")
        sys.exit(1)
    print("\nall Everi-reader cases as expected")


def _main(argv):
    """`python scripts/webfetch_everi.py --slice N` -- one bounded crawl run.

    Exists because activation needs repeated runs to complete the crawl, and the
    alternative -- hand-editing the commented config entry and calling
    fetch_sources.py -- is how a slice size or a cache path ends up wrong in the
    committed config.
    """
    n = argv[argv.index("--slice") + 1] if "--slice" in argv else None
    cfg = json.loads(
        (Path(__file__).resolve().parent / "frankston_live.slice.json")
        .read_text(encoding="utf-8"))
    if n:
        cfg["slice_size"] = int(n)
    # An impersonating session, since the host answers plain urllib with 409
    # before it ever looks at the path.
    from webfetch_http import make_session

    # An incomplete crawl raises PartialFetch, which is the fetcher's "do not
    # publish" signal. For the operator driving this loop by hand that is not an
    # error -- it means "the crawl advanced, run it again" -- so it is reported
    # and exits 0. Letting it reach the traceback would print a stack for the
    # expected outcome of every run but the last.
    try:
        rows = fetch_everi(cfg, make_session())
    except PartialFetch as e:
        print(f"\nnot finished: {e}")
        return
    print(f"\n{len(rows)} rows -- crawl complete, this can now be "
          f"enabled in sources.yaml")


if __name__ == "__main__":
    import sys as _sys

    if len(_sys.argv) > 1 and _sys.argv[1] != "--test":
        _main(_sys.argv)
    else:
        _self_test()