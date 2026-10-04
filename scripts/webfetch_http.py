"""Shared HTTP + date helpers for webfetch sources.

The single owner for the four things every source repeats: the row shape, a
month name to a number, a written time to (hour, minute), and the detail-page
fetch loop. Each had several near-identical copies, and one copy of the time
conversion disagreed with the others about a range that states its meridiem
once -- which raised an AttributeError and silently discarded a source's whole
snapshot.

Fetchers report through `report()` rather than print(), so a line can be
attributed to the source that produced it and the verbosity can be filtered in
one place.
"""
import os
import random
import re
import sys
import time
from collections import namedtuple
from datetime import datetime

from bs4 import BeautifulSoup

# curl_cffi is imported inside make_session(), not here. This module also owns
# the pure date/time/row helpers, and fetch_urllib_sources.py imports those
# while having no browser impersonation of its own, as does the self-test
# below. A module-level import would make both require a network library at
# import time just to parse a month name.

# --- reporting ------------------------------------------------------------
# Every fetcher used to print directly, with a hand-typed two- or four-space
# indent standing in for a log level. That is the only observable behaviour a
# fetcher has, which is why none of them could be tested. Messages now carry
# their source and their level, and the indent is derived, not remembered.

_LEVELS = {"debug": 0, "info": 1, "warn": 2, "error": 3}
_LEVEL_NAMES = {"info": "", "warn": "WARNING", "error": "ERROR"}

# Set EVENTS_FETCH_VERBOSE=debug to see the per-page chatter as well.
_verbose = os.environ.get("EVENTS_FETCH_VERBOSE", "info").lower()
# An unrecognised value falls back to info rather than raising: a typo in a log
# setting should not take down a fetch that has already started.
_min_level = _LEVELS.get(_verbose, _LEVELS["info"])

# The source currently being fetched, so a message does not have to repeat it.
_current = {"id": None}


def set_reporting_source(source_id):
    """Attribute subsequent messages to this source id."""
    _current["id"] = source_id


def report(message, level="info"):
    """Print one fetch-progress line, tagged with its source and level."""
    if _LEVELS.get(level, 1) < _min_level:
        return
    tag = _LEVEL_NAMES.get(level, "")
    who = _current["id"] or "fetch"
    stream = sys.stderr if level in ("warn", "error") else sys.stdout
    indent = "    " if level == "debug" else "  "
    prefix = f"{who}: " if tag == "" else f"{who}: {tag}: "
    print(f"{indent}{prefix}{message}", file=stream)


# Month names, in one place. "sept" is a real fourth character that neither a
# 3-letter prefix nor a 3-letter table covers, which is why the lookups below
# try four characters before three.
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11,
    "dec": 12,
}
FULL_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}


def month_number(name):
    """Month number for a full or abbreviated month name, else None.

    Accepts "September", "Sept", "Sep", "SEP", "sept." -- sources write all
    four, and the festivals in particular print the full name.
    """
    key = (name or "").strip().lower().rstrip(".")
    if not key:
        return None
    if key in FULL_MONTHS:
        return FULL_MONTHS[key]
    return MONTHS.get(key[:4]) or MONTHS.get(key[:3])


class PartialFetch(Exception):
    """Raised when a fetch is cut short (network/WAF/markup/config).

    Carries whatever was collected so far so the caller can report it, but
    the result must NOT be written over a previously-good snapshot: a partial
    crawl is indistinguishable from a source that legitimately has no events.

    This is the ONLY "do not publish" signal. A fetcher that returns [] means
    "this source genuinely has nothing"; anything broken -- a WAF block, a
    failed download, a missing required config key -- must raise this, or the
    orchestrator cannot tell a dead scraper from a quiet season and will
    overwrite a good snapshot with a fraction of the real data.
    """
    def __init__(self, reason, rows=None):
        super().__init__(reason)
        self.reason = reason
        self.rows = rows or []


# The row shape every source emits, and the one that lands in a snapshot.
# This is the single place that shape is written down; it used to be
# documented only as prose in webfetch_snapshots/README.md and hand-rolled
# four times, so a fetcher that forgot a key produced a row the rest of the
# pipeline had to discover by failing.
ROW_FIELDS = ("name", "datetime_text", "datetime_iso", "location", "address",
              "price_text", "description", "source", "source_id")


def restates_name(description, name):
    """True when a description is just the row's own title.

    Five fetchers reached for `description=<the page's prose> or name` so the
    column would never be empty, and for every listing that states no prose of
    its own that wrote the title into the description. 667 rows -- all 524 of
    kingston_hubs and all 143 of bayside_live, 30% of the store. The page
    printed each event's name twice, the search haystack counted it twice, and
    `classify_types` read the title as if it were the listing's description.

    Lived here, next to the row shape, so that `make_row` and the store's
    repair pass cannot disagree about it. They did: the repair pass normalised
    internal whitespace and casing, `make_row` only stripped the ends, so a
    description reading " Tai  Chi " against a title of "Tai Chi" was kept by
    one and recognised by the other.

    The comparison is whole-string equality, not containment: a restatement is a
    copy of the entire title, so there is no word in common between a real
    description and the name it would be wrongly condemned for sharing one. The
    length floor is only there to skip a degenerate two-letter title, and it is
    deliberately small -- a first version used 8, which silently spared every
    short class name ("Tai Chi", "Zumba", "PlaySpace") and left 123 rows of
    kingston_hubs still carrying their own title, which is most of that source.
    """
    d = " ".join((description or "").split()).casefold()
    n = " ".join((name or "").split()).casefold()
    return bool(n) and len(n) >= 3 and d == n


def make_row(source_id, name, source, datetime_iso="", datetime_text="",
             location="", address="", price_text="", description=""):
    """One snapshot row, with every documented key always present.

    `datetime_iso` stays "" for a dateless listing rather than being stamped
    with the fetch time: a made-up timestamp is discarded again downstream, and
    while it is in the file it looks like a real date to anything that reads
    it. dedupe.py/recurrence.py derive the date from the text.

    A description that restates the row's own name is dropped rather than
    stored. Five fetchers reach for `description=<whatever the page gave me>
    or name` so the column is never empty, and for every listing that states no
    prose of its own that writes the title into the description -- 667 rows,
    30% of the store. The page then prints the name twice, the search haystack
    counts it twice, and the classifier reads the title as if it were the
    listing's description. None of those is visible in a row that looks
    well-formed, and each fetcher's fallback is defensible on its own, so the
    rule belongs here: one owner of the row shape decides what a description is
    allowed to be. A row that genuinely has no description has none, which is
    what the page has always rendered for a blank one.
    """
    name = name or ""
    description = description or ""
    if name.strip() and restates_name(description, name):
        description = ""
    return {
        "name": name,
        "datetime_text": datetime_text or "",
        "datetime_iso": datetime_iso or "",
        "location": location or "",
        "address": address or "",
        "price_text": price_text or "",
        "description": description,
        "source": source or "",
        "source_id": source_id,
    }


def join_address(parts):
    """Join an address's pre-split segments into one clean string.

    The venue blocks these sites print are a list of fields, not one address,
    and two things go wrong when they are concatenated naively.

    A trailing "Australia" is a country, not part of a street address, so it
    is dropped -- alongside the blank separators the markup leaves between the
    fields, which is where the published "14 Willis St,, Hampton" came from.

    The suburb is sometimes listed twice, because that is what the source's own
    page says: Bayside renders Location as
    `['84 Reserve Road', 'Beaumaris', 'Beaumaris', 'Victoria 3193', 'Australia']`
    and ten of its rows were published with the suburb repeated. A repeated
    adjacent segment is never meaningful in a street address, so it collapses
    here rather than being detected downstream -- dedupe.py's `_malformed()`
    only recognised ",," and a dangling separator, and a well-formed-looking
    duplicate segment is exactly what it could not see.

    The repeat is not always between two of the caller's own parts: for some
    Bayside events the page renders the locality as a single run reading
    "Brighton, Brighton", so a part can carry the repeat internally. Every part
    is therefore split on commas before the collapse, which handles both levels
    with one rule, and order is otherwise preserved.
    """
    flat = []
    for raw in parts or []:
        for seg in (raw or "").split(","):
            seg = seg.strip().strip(".").strip()
            if not seg or seg.lower() == "australia":
                continue
            if flat and seg.casefold() == flat[-1].casefold():
                continue
            flat.append(seg)
    return ", ".join(flat)


def make_session():
    """A curl-cffi session presenting a Chrome TLS fingerprint."""
    from curl_cffi import requests as cr

    s = cr.Session(impersonate="chrome", timeout=15)
    s.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-AU,en;q=0.9",
    })
    return s


def price_sort(cost):
    """Sortable number for a `price_text`, or None when it states no amount.

    0.0 for anything reading as free, 1.0 for a gold coin donation, otherwise
    the first dollar amount found. Lives here rather than in fetch_sources.py
    because it is a rule about a *row's* fields, and dedupe.py needs it too:
    `_merge_sources()` only fills blanks, so a stored row that predates
    `price_sort` keeps None forever and the page's free filter misses it --
    56 rows carried a price_text with no price_sort, including 19 that said
    "Free". The derived value belongs next to the row shape it is derived from.
    """
    if not cost:
        return None
    if re.search(r"\bfree\b", cost, re.I):
        return 0.0
    if re.search(r"gold coin", cost, re.I):
        return 1.0
    m = re.search(r"\$\s*(\d+(?:\.\d+)?)", cost)
    return float(m.group(1)) if m else None


def decode_body(body):
    """Decode a response body the way a browser would.

    UTF-8 when the bytes are valid UTF-8, which is every source today. Failing
    that, cp1252 rather than dropping the bytes on the floor.

    The `"ignore"` this replaces was quiet about a real failure: it deletes any
    byte sequence that is not valid UTF-8, so a source that serves latin-1 loses
    the character outright -- "Cafe" with the accent dropped, not "Cafe" with
    the accent mangled -- and nothing in the snapshot records that anything was
    lost. The name then looks like a different event to dedupe and to anyone
    reading the page. cp1252 cannot fail, and its 0x80-0x9F range maps to real
    characters rather than to nothing.

    A `Content-Type` charset would be the strictly better signal, but the
    session layer hands the body over without headers, so there is nothing to
    read one from.
    """
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("cp1252", "replace")


class _Response:
    """What the fetch layer reads off a response: status, text, bytes."""

    def __init__(self, status_code, body):
        self.status_code = status_code
        self.content = body
        self.text = decode_body(body)


class PlainSession:
    """A urllib-backed session with the same shape as a curl_cffi one.

    Every source takes a session, because `impersonate: true` in sources.yaml
    decides *which kind* of session a host needs -- not whether a session
    exists. Four hosts answer plain urllib; two need Chrome TLS impersonation
    because a WAF blocks them. Handing a source None because it is not
    impersonated is a failure mode that only appears on the sources which
    happen to be fine.

    `post()` exists because a source needs to, not because anything else does:
    the Kingston directory paginates by ASP.NET postback, carrying a ~46 KB
    `__SEAMLESSVIEWSTATE` blob plus a pager control name that changes with the
    template. A source that needs a POST should not have to demand a
    browser-impersonating session for it, which is what omitting this would
    force -- `impersonate` is meant to declare a property of the host's TLS, not
    of the HTTP verbs its controls happen to use.
    """

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

    HEADERS = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "*/*;q=0.8",
        "Accept-Language": "en-AU,en;q=0.9",
    }

    def __init__(self, timeout=15):
        self.timeout = timeout

    def get(self, url, timeout=None, **kwargs):
        import urllib.request

        req = urllib.request.Request(url, headers=dict(self.HEADERS))
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return _Response(r.status, r.read())
        except urllib.error.HTTPError as e:
            return _Response(e.code, e.read())
        except Exception as e:
            # curl_cffi raises on a connection error, and `get()` above turns
            # that into "HTTP 0", so match it rather than letting a different
            # exception type escape from under the shared retry loop.
            return _Response(0, str(e).encode())

    def post(self, url, data=None, timeout=None, **kwargs):
        import urllib.parse
        import urllib.request

        body = urllib.parse.urlencode(data or {}).encode()
        headers = dict(self.HEADERS)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return _Response(r.status, r.read())
        except urllib.error.HTTPError as e:
            return _Response(e.code, e.read())
        except Exception as e:
            return _Response(0, str(e).encode())


def make_plain_session():
    return PlainSession()


def _retry(session, url, accept, retries=2):
    """One retry loop, shared by the text and the binary readers.

    `accept` is the predicate a body has to satisfy to count as a good
    response, and it is handed the response so a caller can judge length on
    whichever field it actually reads -- `get` on `.text`, `fetch_bytes` on
    `.content`. The policy is one place: the attempt count, the backoff, and
    the "a short body means something went wrong" guard.

    These were two hand-written loops until now, and `fetch_bytes`' docstring
    claimed it existed precisely so the seniors guide's download would not be
    "a second copy" of `get` -- which it was, retry for retry.
    """
    last = None
    for attempt in range(retries + 1):
        try:
            r = session.get(url)
            if r.status_code == 200:
                body = accept(r)
                if body:
                    return body
            last = f"HTTP {r.status_code} ({len(getattr(r, 'content', b''))} bytes)"
        except Exception as e:
            last = repr(e)[:120]
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    report(f"GET failed {url}: {last}", level="warn")
    return None


def get(session, url, retries=2, min_len=1000):
    """Response text, or None. The page reader."""
    return _retry(session, url, lambda r: r.text if len(r.text) >= min_len
                  else None, retries)


def fetch_bytes(session, url, min_len, retries=2):
    """Binary body of `url`, or None. The PDF path; get() is the text one."""
    return _retry(session, url, lambda r: r.content
                  if len(r.content) >= min_len else None, retries)


# --- pacing -----------------------------------------------------------------

# The shortest gap a crawl here will accept whatever the config says. A
# `crawl_delay: 0` line in sources.yaml reads like a fix and is the opposite of
# one, so the floor is deliberately not overridable downward. Each source passes
# the floor its own host warrants.
MIN_CRAWL_DELAY = 0.35


def _pace(seconds):
    """Wait between requests. A module-level seam so the tests can count it.

    Monkeypatching `time.sleep` instead would be global -- every fetcher here
    imports the same module object -- so a test counting deliberate delays would
    also be counting `_retry`'s backoff, and could not tell the two apart. That
    distinction is the whole question this throttle exists to answer.
    """
    time.sleep(seconds)


class Pacer:
    """One owner for how fast a crawl is allowed to ask.

    The delay this replaces was a `time.sleep` at the bottom of the request
    loop, which put the rate limit on the wrong branch: every `continue` between
    the request and the sleep -- a page that failed, one that parsed to nothing,
    one that fell outside a horizon -- was fetched with no pause at all. Those
    are not the rare paths. Measured on the Frankston sitemap, sixteen requests
    and zero delays, because the majority of its pages take an early `continue`.

    So pacing sits immediately before each request, and every path through the
    loop goes through it. Three properties a bare sleep cannot give:

    * **A floor.** `max(delay, floor)`, so no config value can turn a source
      into an unbounded crawler.
    * **A budget.** `budget` covers every request in the run, so a per-source
      page or detail cap cannot ask for more pressure than the host tolerates.
    * **Jitter.** A uniform interval is a machine signature. Up to a quarter of
      the delay is random.

    The gap is measured from the previous *request*, not from the previous
    wait, so a slow page -- a big detail page, a connection that took two
    seconds -- shortens the next gap instead of adding to it. Pausing a fixed
    interval *after* each request makes the real rate a function of page size,
    which is the opposite of a rate limit.
    """

    def __init__(self, delay=None, floor=0.0, default=0.0, budget=None,
                 pace=None, jitter=None):
        try:
            want = float(delay) if delay is not None else float(default)
        except (TypeError, ValueError):
            want = float(default)
        self.delay = max(want, float(floor))
        self.budget = int(budget) if budget else None
        self._pace = pace or _pace
        self._jitter = jitter if jitter is not None else self.delay * 0.25
        self.spent = 0
        self._last = None

    def take(self):
        """Wait out the interval, then spend one request. False when spent.

        The first request is not delayed: there is no previous one to be a
        distance from, and a leading sleep is dead time on every run.
        """
        if self.budget is not None and self.spent >= self.budget:
            return False
        if self._last is not None:
            gap = self.delay - (time.monotonic() - self._last)
            if gap > 0:
                self._pace(gap + (random.random() * self._jitter))
        self.spent += 1
        self._last = time.monotonic()
        return True


# --- ASP.NET postback pagination -------------------------------------------
#
# Granicus "Seamless CMS" listings ignore `?page=` and move only on a POST
# carrying the form's own hidden state, so two of them need this and a third
# caller would too. It lives here rather than in either fetcher because the
# mechanism is the platform's, not a source's, and a per-source copy is a copy
# that can drift from the one that is actually running.
#
# The control names (`ctl10$ctl00$ctl07`, `ctl10$ctl00$ctl08`) are generated
# from the control tree, so they are discovered from the markup rather than
# hard-coded. That matters more than it looks: a POST with a wrong field name is
# *accepted* and the server re-serves page 1, silently. `paged_listing` treats
# exactly that as the failure it is.

# "Page 1 of 31"
PAGE_INFO_RE = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)")


def page_hidden_fields(soup):
    """The form's hidden inputs, which carry the postback state.

    Reissued on every response, so it has to be re-read from each page rather
    than kept from the first: the previous blob is spent.
    """
    return {i.get("name"): i.get("value", "")
            for i in soup.select("form#mainForm input[type=hidden]")
            if i.get("name")}


def pager_control_names(soup):
    """The page-number select and its Go button, found rather than assumed.

    Both are identified by what they contain rather than by their name, so a
    template that renumbers its controls still works. A name is a property of
    this page's markup today and not a fact about the platform.
    """
    select_name = None
    for sel in soup.select(".seamless-pagination-data select"):
        options = [o.get("value", "") for o in sel.select("option")]
        if len(options) > 1 and all(v.strip().isdigit() for v in options):
            select_name = sel.get("name")
            break
    go_name = None
    for btn in soup.select(".seamless-pagination-controls input[type=submit]"):
        if (btn.get("value") or "").strip().lower() == "go":
            go_name = btn.get("name")
            break
    return select_name, go_name


# A page count the site states that this walk did not reach is a budget stop,
# which is a warning; a page that failed to load is a broken fetch, which is
# not. The two are kept apart because conflating them either refuses to publish
# a deliberately bounded crawl or publishes a truncated one as if it were whole.
Listing = namedtuple("Listing", "pages claimed capped")


def paged_listing(session, cfg, *, pacer=None):
    """Every listing page this source is allowed to read, and how many it claims.

    `max_pages` is the budget and it is opt-in: a source that does not set one
    reads page 1 only, which is what every Granicus source did until the council
    listing turned out to be 31 pages deep and the deeper half of the calendar
    was invisible for that reason.

    Raises `PartialFetch` for the failures that must not publish -- the first
    page will not load, the pager cannot be driven at all, the listing stopped
    stating how deep it is, and a page beyond the first that fails to load.
    That last one is a walk cut short rather than a budget reached, and the two
    are kept apart deliberately: refusing to publish a deliberately bounded
    crawl and publishing a truncated one as if it were whole are both wrong, in
    opposite directions.

    Returns `capped=True` when the budget, rather than the site, decided where
    to stop. That is a warning the caller is expected to print, naming how much
    of the listing it did not read.
    """
    url = cfg["url"]
    if pacer:
        pacer.take()
    first = get(session, url, retries=3)
    if not first:
        raise PartialFetch(f"listing page {url} failed to load")

    soup = BeautifulSoup(first, "html.parser")
    found = PAGE_INFO_RE.search(soup.get_text(" ", strip=True))
    claimed = int(found.group(2)) if found else None

    max_pages = cfg.get("max_pages")
    if not max_pages:
        return Listing([soup], claimed, False)
    if claimed is None:
        # A source that asked to be walked past page 1 and whose listing no
        # longer states how deep it is. Publishing page 1 here is exactly the
        # defect the walk exists to prevent -- a fetch that succeeds, looks
        # healthy, and is a fraction of the listing -- so it is refused rather
        # than reported.
        raise PartialFetch(
            f"{url} states no page count, and this source is configured with "
            f"max_pages: {max_pages} -- either the pager's 'Page 1 of N' text "
            f"moved or this listing no longer paginates. Refusing rather than "
            f"publishing page 1 as if it were the whole listing")
    if claimed <= 1:
        return Listing([soup], claimed, False)

    select_name, go_name = pager_control_names(soup)
    if not (select_name and go_name):
        raise PartialFetch(
            f"{url} has no usable pagination controls -- the pager markup "
            f"changed, and this fetcher cannot enumerate past the first page")

    pages, page = [soup], 1
    while page < min(int(max_pages), claimed):
        page += 1
        data = page_hidden_fields(soup)
        data[select_name] = str(page)
        data[go_name] = "Go"
        if pacer:
            pacer.take()
        response = session.post(url, data=data)
        html = getattr(response, "text", "") or ""
        if getattr(response, "status_code", 0) != 200 or len(html) < 1000:
            raise PartialFetch(
                f"page {page} of {url} did not load "
                f"(HTTP {getattr(response, 'status_code', '?')}) -- the walk "
                f"stopped short, so publishing would replace a good snapshot "
                f"with a fraction of the listing")
        soup = BeautifulSoup(html, "html.parser")
        got = PAGE_INFO_RE.search(soup.get_text(" ", strip=True))
        if got and got.group(1) == "1":
            # The POST was accepted and the server re-served page 1, which is
            # what a wrong control name looks like. Publishing now would mean
            # shipping the first page as if it were the whole listing -- which
            # is precisely the defect this walk was added to fix.
            raise PartialFetch(
                f"paging {url} did not advance: POST returned page 1 again, so "
                f"the pager control name or the viewstate is wrong -- refusing "
                f"to publish the first page as if it were the whole listing")
        pages.append(soup)
        if page % 5 == 0:
            report(f"  page {page} of {claimed}", level="debug")

    capped = len(pages) < claimed
    if capped:
        report(f"listing claims {claimed} pages; read {len(pages)} "
               f"(max_pages budget) -- the events past page {len(pages)} are "
               f"not in this snapshot", level="warn")
    return Listing(pages, claimed, capped)


# A detail crawl that opened *some* pages but almost none of them is a broken
# crawl, not a source whose event pages are gone. Below this fraction of
# attempted pages succeeding, the snapshot is not replaced.
DETAIL_MIN_SUCCESS_RATIO = 0.5


def enrich_details(session, rows, cap, apply_one, *, sleep=0.2, label="",
                   pacer=None):
    """Fetch each row's own page and let `apply_one` fill it in, in place.

    The three listing sources each need a detail pass -- the listing card
    carries a date and a title, and the venue, the real time and the cost are
    on the event's own page -- and each had its own copy of this loop with its
    own idea of what counts against the cap.

    Failure is signalled, not swallowed. A detail page that will not load was
    previously a bare `continue`, so a WAF block on the detail pages looked
    exactly like a source with no detail pages: the snapshot was overwritten
    with a listing-only file, every venue blank, and the run stayed green.
    Under DETAIL_MIN_SUCCESS_RATIO of attempts succeeding, this raises
    PartialFetch so the previous snapshot survives.

    `pacer` overrides `sleep`, and `sleep` is then ignored. It is passed rather
    than multiplied here so that a source which also paces its *listing* pages
    runs on one clock: the delay belongs to the source's relationship with its
    host, not to whichever loop happens to be making the request.
    """
    pace = pacer if pacer is not None else Pacer(delay=sleep)
    attempted = enriched = 0
    for r in rows:
        if cap is not None and enriched >= cap:
            break
        url = r.get("source")
        if not url:
            continue
        attempted += 1
        # Before the request, on every path. The delay used to sit after
        # `enrich_details`'s own success branch, so a page that failed to load
        # -- the one branch that most deserves a pause -- was retried straight
        # on with none.
        pace.take()
        html = get(session, url)
        if not html:
            continue
        apply_one(r, html)
        enriched += 1
    if attempted and enriched / attempted < DETAIL_MIN_SUCCESS_RATIO:
        raise PartialFetch(
            f"only {enriched}/{attempted} detail pages loaded"
            f"{f' for {label}' if label else ''} -- the listing is intact but "
            f"its event pages are not")
    return enriched


# --- written times -------------------------------------------------------
# One owner for "what time does this line state", replacing four hand-rolled
# copies of the same twelve-hour conversion.

def _hhmm(hour, minute, meridiem, clamp=True):
    """24-hour (hour, minute) from a loose hour / optional minutes / optional
    meridiem.

    The meridiem is accepted in every form the sources write -- "am", "a",
    "p.m.", "pm" -- because the two regexes that feed this disagree about what
    they capture: the shared TIME_RANGE_RE captures the whole word, the
    directory's HOURS_RANGE_RE a single letter. When each version accepted only
    its own spelling, every caller holding the other had to reshape it at the
    call site, and webfetch_ccc.py was appending "m" to a bare "p" to get a word
    its helper would recognise. Silently, too: the wrong spelling is not an
    error here, it is just a morning.

    clamp=True saturates a malformed value, which is what the generic listing
    readers want -- one page printing "25:00" should not stop a crawl.
    clamp=False returns (None, None) instead, which is what the opening-hours
    reader wants, because it decides "opening hours, not a meeting" by comparing
    times: a clamped 23:59 would quietly pass a test meant to refuse the row.
    """
    try:
        hour = int(hour)
        minute = int(minute or 0)
    except (TypeError, ValueError):
        # Each caller keeps the behaviour it already had: the clamped readers
        # were never handed a non-numeric hour, and the refusing one wants None.
        if clamp:
            raise
        return None, None
    ap = (meridiem or "").strip().lower().replace(".", "")
    if ap in ("p", "pm") and hour != 12:
        hour += 12
    elif ap in ("a", "am") and hour == 12:
        hour = 0
    if clamp:
        return max(0, min(23, hour)), max(0, min(59, minute))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None, None
    return hour, minute


def _hhmm_str(hour, minute, meridiem, clamp=True):
    """("HH:MM") for the same conversion, or None when _hhmm refuses it.

    Separate because the two consumers want different shapes and neither should
    have to build the other's: schedule text needs the string, and the
    opening-hours plausibility test needs minutes-past-midnight.
    """
    hour, minute = _hhmm(hour, minute, meridiem, clamp=clamp)
    return None if hour is None else f"{hour:02d}:{minute:02d}"


# A time range that states a meridiem. The pattern requires one on the *end*,
# which is what stops a bare number span from matching, so "1-31" (a date
# range) and "5-10" (a price) are not read as sessions. The start's meridiem
# is optional, because a printed range commonly states it once: "10:30-11:30am",
# "7.30 - 9.00pm".
TIME_RANGE_RE = re.compile(
    r"(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)?\s*(?:-|–|to)\s*"
    r"(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)", re.I)


def range_start_time(m):
    """(hour, minute) start of a TIME_RANGE_RE match, or None for no match.

    A range that states its meridiem once puts it on the end and the start
    inherits it, so "10:30-11:30am" is a 10:30 start rather than an ambiguous
    one. Reading the start's *optional* group without falling back to the end's
    is what used to raise AttributeError on the guide's own house style.
    """
    if m is None:
        return None
    return _hhmm(m.group(1), m.group(2), m.group(3) or m.group(6))


def line_range_starts(lines, lo=None, hi=None):
    """[(line_idx, (hour, minute))] start of every time range within a window.

    `lo`/`hi` bound the pairing context to one event's own block; omit them to
    scan every line. Time is only ever taken from labelled or adjacent lines,
    so a range belonging to a neighbouring card is not read as this one's.
    """
    out = []
    for li, ln in enumerate(lines):
        if lo is not None and not (lo <= li <= hi):
            continue
        for m in TIME_RANGE_RE.finditer(ln or ""):
            start = range_start_time(m)
            if start:
                out.append((li, start))
    return out


def parse_time(text):
    """Return (hour, minute) from strings like '07:30 PM', '12:00pm-01:30pm',
    '9.30am', '9 - 11am'.

    The hour/minute separator is `:` or `.`. The previous pattern only
    accepted `:`, and because the minute group was optional it backtracked
    onto the *minute* digits: '9.30am' matched as hour=30 and clamped to
    23:00, so a 9:30am class was published at 11pm. Anchoring on a single
    1-2 digit hour and requiring a 2-digit minute when a separator is
    present fixes that, and the hour is rejected when implausible (>23) so a
    bare number before am/pm is never read as an hour.

    A range that writes its meridiem once ('9 - 11am', '9:00 - 11:00am') is
    read from its *start*. The meridiem is required on every candidate, so the
    leading number could never satisfy it and `re.search` found the second
    one instead: '9 - 11am' returned 11:00, publishing a 9am class at 11am.

    A range with no meridiem at all ('9 - 8pm' is fine, '9 - 12' is not) is
    rejected when inheriting the end's meridiem would run the clock backwards,
    because a session that ends before it starts is not a session.
    """
    raw = (text or "").lower()
    # A range first. "10am - 11am" does not match this (its start carries its
    # own meridiem) and falls through to the single-time form, which reads
    # the start of either.
    m = re.search(r"\b(\d{1,2})\s*(?:[:.](\d{2}))?\s*"
                  r"(?:-|–|to|until|till)\s*"
                  r"(\d{1,2})\s*(?:[:.](\d{2}))?\s*(am|pm)\b", raw)
    _is_range = bool(m)
    if m:
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(5)
    else:
        m = re.search(r"\b(\d{1,2})\s*(?:[:.](\d{2}))?\s*(am|pm)\b", raw)
        if not m:
            return None
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if h > 23 or mi > 59:
        return None
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if _is_range and m:
        end_h, end_mi, end_ap = int(m.group(3)), int(m.group(4) or 0), m.group(5)
        if end_ap == "pm" and end_h != 12:
            end_h += 12
        if end_ap == "am" and end_h == 12:
            end_h = 0
        if (h, mi) > (end_h, end_mi):
            return None
    # The clamping and 12-hour rollover live in _hhmm so this and the written
    # -range readers above cannot drift apart.
    return _hhmm(h, mi, None)


def parse_day_month_year(text):
    """Parse '28 Sep 2026' / '02 October 2026' / '10 Jun 2026 to 06 Jan 2027' (start)."""
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text or "")
    if not m:
        return None
    mon = month_number(m.group(2))
    if not mon:
        return None
    try:
        return datetime(int(m.group(3)), mon, int(m.group(1)))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Self-tests. Run by `python scripts/webfetch_http.py` and by the GHA workflow.
#
# This module is the shared owner of every written-time conversion, so a
# regression here moves every source at once. The cases below are the ones
# whose failure mode was a plausible-looking wrong hour, not a crash.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# The self-test above exercises this, so it has to be defined before the
# `__main__` block runs. It used to be defined after it, which meant the three
# combine() cases raised NameError and the suite could not be run at all until
# they were added.
# ---------------------------------------------------------------------------

def combine(dt_day, time_text):
    """`dt_day` at the time `time_text` states, or unchanged if it states none.

    Takes a `date` as well as a `datetime`, because a card that states only a
    calendar day has one and the caller still wants an ISO string out. Before
    this, a fetcher holding a plain `date` reached `dt_day.replace(hour=...)`
    and got "TypeError: 'hour' is an invalid keyword argument for replace()".
    """
    tm = parse_time(time_text)
    if not tm:
        return dt_day
    if isinstance(dt_day, datetime):
        return dt_day.replace(hour=tm[0], minute=tm[1])
    return datetime(dt_day.year, dt_day.month, dt_day.day,
                    tm[0], tm[1])


if __name__ == "__main__":
    from datetime import date as _date
    # (label, actual, expected)
    TESTS = [
        # parse_time: the four regressions its docstring records.
        # A dotted minute is minutes, not the hour: the old optional-minute
        # group backtracked onto "30" and clamped to 23:00.
        ("dotted minutes stay minutes",
         parse_time("9.30am"), (9, 30)),
        # A range that states its meridiem once is read from its START. The
        # leading number could not satisfy the required meridiem, so the
        # search skipped it and published a 9am class at 11am.
        ("a range's meridiem does not move the start",
         parse_time("9 - 11am"), (9, 0)),
        # Backwards is not a session.
        ("a backwards range is refused",
         parse_time("9 - 8pm"), None),
        # 12-hour rollover. A 24-hour form is deliberately NOT read here --
        # this reader exists for the labelled time fields on the listing
        # pages, which write am/pm. recurrence._to_hhmm owns 24-hour.
        ("12pm is noon, not midnight", parse_time("12pm"), (12, 0)),
        ("12am is midnight", parse_time("12am"), (0, 0)),
        # A 4-digit year is never an hour.
        ("a year is not a time", parse_time("2026"), None),

        # range_start_time / line_range_starts: the meridiem-once form that
        # used to raise AttributeError on the seniors guide.
        ("meridiem once, on the end",
         range_start_time(TIME_RANGE_RE.search("10:30-11:30am")), (10, 30)),
        ("meridiem once, afternoon",
         range_start_time(TIME_RANGE_RE.search("7.30 - 9.00pm")), (19, 30)),
        ("meridiem on both ends",
         range_start_time(TIME_RANGE_RE.search("9:30am - 11:00am")), (9, 30)),
        ("bare start inherits the end's meridiem",
         range_start_time(TIME_RANGE_RE.search("9-11am")), (9, 0)),
        # A date span and a price must not read as a session. This is the
        # guard that makes the start's meridiem optional safely.
        ("a date span is not a session",
         range_start_time(TIME_RANGE_RE.search("1-31 October")), None),
        ("a bare price is not a session",
         range_start_time(TIME_RANGE_RE.search("Cost $5-10")), None),

        # line_range_starts: line numbers are what the seniors pairing uses,
        # and the window is what keeps one card's time off another.
        ("line numbers are preserved",
         line_range_starts(["nope", "10:30-11:30am", "also nope"]),
         [(1, (10, 30))]),
        ("the window excludes lines outside it",
         line_range_starts(["10:30-11:30am", "x", "1:00-2:00pm"], 2, 2),
         [(2, (13, 0))]),

        # month_number: the four forms the sources actually write.
        ("full month name", month_number("September"), 9),
        ("four-letter abbreviation", month_number("Sept"), 9),
        ("three-letter abbreviation", month_number("Sep"), 9),
        ("mixed case with a full stop", month_number("OCTOBER."), 10),
        ("an unknown word is not a month", month_number("Term"), None),
        ("empty is not a month", month_number(""), None),
        # combine() must take a plain date as well as a datetime: a fetcher
        # reading a card that states only a calendar day has a date, and
        # dt_day.replace(hour=...) then raised "TypeError: 'hour' is an invalid
        # keyword argument for replace()" from inside the fetcher, naming
        # neither the field nor the row.
        ("combine takes a date and a time",
         combine(_date(2026, 10, 2), "10:00 AM").isoformat(),
         "2026-10-02T10:00:00"),
        ("combine takes a datetime too",
         combine(datetime(2026, 10, 2, 9, 0), "10:00 AM").isoformat(),
         "2026-10-02T10:00:00"),
        ("combine leaves a day alone when no time is stated",
         combine(_date(2026, 10, 2), "").isoformat(), "2026-10-02"),

        # _hhmm / _hhmm_str: one conversion, two shapes, two clamping policies.
        # The meridiem spellings are the point -- the two regexes that feed this
        # capture different ones, and accepting only one shape is what made
        # webfetch_ccc.py rewrite "p" as "pm" at the call site.
        ("a bare meridiem letter means the afternoon",
         _hhmm(9, 0, "p"), (21, 0)),
        ("a full meridiem still means the afternoon",
         _hhmm(9, 0, "pm"), (21, 0)),
        ("a bare meridiem letter means the morning",
         _hhmm(9, 0, "a"), (9, 0)),
        ("a punctuated meridiem is understood",
         _hhmm(9, 0, "p.m."), (21, 0)),
        ("12a is midnight in either spelling",
         (_hhmm(12, 0, "a"), _hhmm(12, 0, "am")), ((0, 0), (0, 0))),
        ("12p is noon in either spelling",
         (_hhmm(12, 0, "p"), _hhmm(12, 0, "pm")), ((12, 0), (12, 0))),
        ("the string form agrees with the pair",
         _hhmm_str(9, 5, "p"), "21:05"),
        ("the string form pads",
         _hhmm_str(9, 5, None), "09:05"),
        # clamp: a malformed listing time must not stop a crawl, but a
        # malformed *opening hours* entry must be refused rather than clamped
        # into passing the plausibility test.
        ("clamped, a nonsense hour saturates",
         _hhmm(25, 0, None), (23, 0)),
        ("clamped, a nonsense minute saturates",
         _hhmm(9, 99, None), (9, 59)),
        ("refused, a nonsense hour is None",
         _hhmm(25, 0, None, clamp=False), (None, None)),
        ("refused, a nonsense minute is None",
         _hhmm(9, 99, None, clamp=False), (None, None)),
        ("refused, the string form is None too",
         _hhmm_str(25, 0, None, clamp=False), None),
        ("refused, a real hour still converts",
         _hhmm_str(9, 30, "a", clamp=False), "09:30"),
    ]

    failures = []

    from checks import check as _check

    for label, actual, expected in TESTS:
        _check(label, actual, expected, failures)

    # --- make_row: a description that restates the name is not a description -
    # Five fetchers reached for `description=<prose> or name`. That published
    # 667 rows -- every kingston_hubs and every bayside_live row, 30% of the
    # store -- with the event's own title in the description column.
    ROW_CASES = [
        ("a description that is the name is dropped",
         make_row("s", "PlaySpace", "u", description="PlaySpace")["description"],
         ""),
        ("casing does not hide a restatement",
         make_row("s", "Tai Chi", "u", description="TAI CHI")["description"],
         ""),
        ("whitespace does not hide a restatement",
         make_row("s", "Tai Chi", "u", description=" Tai  Chi ")["description"],
         ""),
        ("a short class name is still a restatement",
         make_row("s", "Zumba", "u", description="Zumba")["description"], ""),
        ("real prose is kept",
         make_row("s", "Tai Chi", "u",
                  description="Slow, gentle forms suited to older adults."
                  )["description"],
         "Slow, gentle forms suited to older adults."),
        ("prose that merely shares a word is kept",
         make_row("s", "Tai Chi", "u",
                  description="Tai chi for beginners, no experience needed."
                  )["description"],
         "Tai chi for beginners, no experience needed."),
        ("a nameless row keeps its description",
         make_row("s", "", "u", description="Tai Chi")["description"], "Tai Chi"),
        ("None is normalised to blank, not kept as None",
         make_row("s", "PlaySpace", "u", description=None)["description"], ""),
    ]

    # --- join_address: the shapes Bayside's own venue block renders ---------
    ADDRESS_CASES = [
        ("plain segments join in order",
         join_address(["Beaumaris Library", "96 Reserve Road", "Beaumaris",
                       "Victoria 3193"]),
         "Beaumaris Library, 96 Reserve Road, Beaumaris, Victoria 3193"),
        ("the country is dropped",
         join_address(["84 Reserve Road", "Beaumaris", "Victoria 3193",
                       "Australia"]),
         "84 Reserve Road, Beaumaris, Victoria 3193"),
        # The one that shipped: Bayside lists the suburb twice in its own
        # Location field, so ten rows published as
        # "84 Reserve Road, Beaumaris, Beaumaris, Victoria 3193".
        ("a repeated suburb segment collapses",
         join_address(["84 Reserve Road", "Beaumaris", "Beaumaris",
                       "Victoria 3193", "Australia"]),
         "84 Reserve Road, Beaumaris, Victoria 3193"),
        # ...and for some events the repeat is inside a single run, so a part
        # can carry it rather than arriving as two parts.
        ("a repeated suburb inside one part collapses",
         join_address(["Green Point", "Brighton, Brighton", "Victoria 3186"]),
         "Green Point, Brighton, Victoria 3186"),
        ("blank separators from the markup are dropped",
         join_address(["14 Willis St", "", "Hampton", "", "Victoria 3188"]),
         "14 Willis St, Hampton, Victoria 3188"),
        ("trailing commas on a part are stripped",
         join_address(["14 Willis St,", "Hampton,", "Victoria 3188"]),
         "14 Willis St, Hampton, Victoria 3188"),
        ("nothing in, nothing out",
         join_address([]), ""),
        ("a non-adjacent repeat is left alone",
         join_address(["Hall", "Beaumaris", "Street", "Beaumaris"]),
         "Hall, Beaumaris, Street, Beaumaris"),
    ]

    for group in (ROW_CASES, ADDRESS_CASES):
        for label, actual, expected in group:
            _check(label, actual, expected, failures)

    if failures:
        print(f"\nwebfetch_http: {len(failures)}/{len(TESTS)} cases FAILED")
        raise SystemExit(1)
    print(f"\nall {len(TESTS)} webfetch_http cases as expected")


