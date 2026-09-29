"""Shared HTTP + date helpers for webfetch sources."""
import re
import time
from datetime import datetime

from curl_cffi import requests as cr

MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"])}


class PartialFetch(Exception):
    """Raised when a multi-page crawl is cut short (network/WAF/markup).

    Carries whatever was collected so far so the caller can report it, but
    the result must NOT be written over a previously-good snapshot: a partial
    crawl is indistinguishable from a source that legitimately has no events.
    """
    def __init__(self, reason, rows=None):
        super().__init__(reason)
        self.reason = reason
        self.rows = rows or []


def make_session():
    s = cr.Session(impersonate="chrome", timeout=15)
    s.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-AU,en;q=0.9",
    })
    return s


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
    print(f"    GET failed {url}: {last}")
    return None


def parse_time(text):
    """Return (hour, minute) from strings like '07:30 PM', '12:00pm-01:30pm',
    '9.30am'.

    The hour/minute separator is `:` or `.`. The previous pattern only
    accepted `:`, and because the minute group was optional it backtracked
    onto the *minute* digits: '9.30am' matched as hour=30 and clamped to
    23:00, so a 9:30am class was published at 11pm. Anchoring on a single
    1-2 digit hour and requiring a 2-digit minute when a separator is
    present fixes that, and the hour is rejected when implausible (>23) so a
    bare number before am/pm is never read as an hour.
    """
    m = re.search(r"\b(\d{1,2})\s*(?:[:.](\d{2}))?\s*(am|pm)\b", (text or "").lower())
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if h > 23 or mi > 59:
        return None
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return max(0, min(23, h)), max(0, min(59, mi))


def parse_day_month_year(text):
    """Parse '28 Sep 2026' / '02 October 2026' / '10 Jun 2026 to 06 Jan 2027' (start)."""
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text or "")
    if not m:
        return None
    mon = MONTHS.get(m.group(2)[:3].lower())
    if not mon:
        return None
    try:
        return datetime(int(m.group(3)), mon, int(m.group(1)))
    except ValueError:
        return None


def combine(dt_day, time_text):
    tm = parse_time(time_text)
    if tm:
        return dt_day.replace(hour=tm[0], minute=tm[1])
    return dt_day
