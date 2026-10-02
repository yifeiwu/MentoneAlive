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
import re
import sys
import time
from datetime import datetime

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


def make_row(source_id, name, source, datetime_iso="", datetime_text="",
             location="", address="", price_text="", description=""):
    """One snapshot row, with every documented key always present.

    `datetime_iso` stays "" for a dateless listing rather than being stamped
    with the fetch time: a made-up timestamp is discarded again downstream, and
    while it is in the file it looks like a real date to anything that reads
    it. dedupe.py/recurrence.py derive the date from the text.
    """
    return {
        "name": name or "",
        "datetime_text": datetime_text or "",
        "datetime_iso": datetime_iso or "",
        "location": location or "",
        "address": address or "",
        "price_text": price_text or "",
        "description": description or "",
        "source": source or "",
        "source_id": source_id,
    }


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


class _Response:
    """What the fetch layer reads off a response: status, text, bytes."""

    def __init__(self, status_code, body):
        self.status_code = status_code
        self.content = body
        self.text = body.decode("utf-8", "ignore")


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


def get(session, url, retries=2, min_len=1000):
    last = None
    for attempt in range(retries + 1):
        try:
            r = session.get(url)
            if r.status_code == 200 and len(r.text) >= min_len:
                return r.text
            last = f"HTTP {r.status_code} ({len(r.text)} bytes)"
        except Exception as e:
            last = repr(e)[:120]
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    report(f"GET failed {url}: {last}", level="warn")
    return None


def fetch_bytes(session, url, min_len, retries=2):
    """Binary body of `url`, or None. The PDF path; get() is the text one.

    Same retry policy as get(), and the same "small body means something went
    wrong" guard, so the seniors guide's download loop is not a second copy of
    it.
    """
    last = None
    for attempt in range(retries + 1):
        try:
            r = session.get(url)
            if r.status_code == 200 and len(r.content) >= min_len:
                return r.content
            last = f"HTTP {r.status_code} ({len(r.content)} bytes)"
        except Exception as e:
            last = repr(e)[:120]
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    report(f"GET failed {url}: {last}", level="warn")
    return None


# A detail crawl that opened *some* pages but almost none of them is a broken
# crawl, not a source whose event pages are gone. Below this fraction of
# attempted pages succeeding, the snapshot is not replaced.
DETAIL_MIN_SUCCESS_RATIO = 0.5


def enrich_details(session, rows, cap, apply_one, *, sleep=0.2, label=""):
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
    """
    attempted = enriched = 0
    for r in rows:
        if cap is not None and enriched >= cap:
            break
        url = r.get("source")
        if not url:
            continue
        attempted += 1
        html = get(session, url)
        if not html:
            continue
        apply_one(r, html)
        enriched += 1
        time.sleep(sleep)
    if attempted and enriched / attempted < DETAIL_MIN_SUCCESS_RATIO:
        raise PartialFetch(
            f"only {enriched}/{attempted} detail pages loaded"
            f"{f' for {label}' if label else ''} -- the listing is intact but "
            f"its event pages are not")
    return enriched


# --- written times -------------------------------------------------------
# One owner for "what time does this line state", replacing four hand-rolled
# copies of the same twelve-hour conversion.

def _hhmm(hour, minute, meridiem):
    """24-hour (hour, minute) from a loose hour / optional minutes / optional
    meridiem. Clamped, so a malformed value degrades rather than raising."""
    hour = int(hour)
    minute = int(minute or 0)
    ap = (meridiem or "").strip().lower()
    if ap == "pm" and hour != 12:
        hour += 12
    elif ap == "am" and hour == 12:
        hour = 0
    return max(0, min(23, hour)), max(0, min(59, minute))


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
    ]

    failures = []
    for label, actual, expected in TESTS:
        if actual == expected:
            print(f"ok   {label}")
        else:
            print(f"FAIL {label}\n       actual:   {actual!r}"
                  f"\n       expected: {expected!r}")
            failures.append(label)

    if failures:
        print(f"\nwebfetch_http: {len(failures)}/{len(TESTS)} cases FAILED")
        raise SystemExit(1)
    print(f"\nall {len(TESTS)} webfetch_http cases as expected")


