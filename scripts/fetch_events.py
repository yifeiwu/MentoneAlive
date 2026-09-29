"""Multi-source event fetcher (PYTHON sources only for GHA).

Reads sources.yaml, fetches from each PYTHON source, normalizes to a common
schema, and writes raw_events.json. Webfetch sources (Kingston Council,
Kingston Arts, Bayside live, Kingston/Frankston libraries) are snapshotted
manually via browser into scripts/webfetch_snapshots/*.json and merged in
dedupe.py — never fetched here (Python is WAF-blocked for those hosts).
"""
import json
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import yaml
from bs4 import BeautifulSoup

from jsonio import write_json

ROOT = Path(__file__).resolve().parent.parent

BASE = "https://www.kingston.vic.gov.au"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/json,*/*",
    "Referer": BASE,
}


def _get(url, headers=None, timeout=15, retries=3):
    if retries < 1:
        raise ValueError("retries must be >= 1")
    h = dict(HEADERS)
    if headers:
        h.update(headers)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:
            last = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise last


def _post(url, payload, headers=None, timeout=15, retries=3):
    if retries < 1:
        raise ValueError("retries must be >= 1")
    data = json.dumps(payload).encode()
    h = dict(HEADERS)
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


MONTH_ABBR = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
              "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11,
              "dec": 12}


def _parse_date(s, ref=None):
    """Parse a source date string into a datetime, or None if unrecognised.

    Month names are resolved through a fixed table rather than strptime's
    '%b', which follows the process LC_TIME locale and raises on a
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
        mon = MONTH_ABBR.get(mon_name[:4].lower()) or \
            MONTH_ABBR.get(mon_name[:3].lower())
        if mon:
            if ap == "pm" and h != 12:
                h += 12
            if ap == "am" and h == 12:
                h = 0
            # Anchor on the reference year, rolling forward when the month has
            # already passed, so a "28 December" listing in November is next
            # year rather than 11 months in the past.
            for year in (ref.year, ref.year + 1):
                try:
                    return datetime(year, mon, day, h % 24, mi)
                except ValueError:
                    continue
    # "28 Sep 2026" / "28 September 2026" bare dates (midnight).
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", s)
    if m:
        mon = MONTH_ABBR.get(m.group(2)[:4].lower()) or \
            MONTH_ABBR.get(m.group(2)[:3].lower())
        if mon:
            try:
                return datetime(int(m.group(3)), mon, int(m.group(1)))
            except ValueError:
                return None
    return None


def _price_sort(cost):
    if not cost:
        return None
    if re.search(r"\bfree\b", cost, re.I):
        return 0.0
    if re.search(r"gold coin", cost, re.I):
        return 1.0
    m = re.search(r"\$\s*(\d+(?:\.\d+)?)", cost)
    return float(m.group(1)) if m else None


def _fmt_dt(dt):
    return dt.strftime("%a %d %b %Y, %I:%M %p").replace(" 0", " ")


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

def fetch_kingston_hubs(cfg):
    cal_ids = cfg["calendars"]
    # Explicit id -> venue map. Treating "any id that is not cal_ids[0]" as
    # Patterson Lakes would silently attach the wrong venue *and* the wrong
    # street address to a newly added calendar.
    venues = cfg.get("calendar_venues") or {
        cal_ids[0]: ("Chelsea Activity Hub",
                     "3-5 Showers Ave, Chelsea 3196"),
    }
    rows = []
    unmapped_calendars = set()
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
            print(f"  Kingston Hubs month {start[:7]}: FAILED {e!r}")
            continue
        # Parse outside the request try, so one malformed card cannot discard
        # every row already collected for this month.
        try:
            data = json.loads(resp)
        except ValueError as e:
            print(f"  Kingston Hubs month {start[:7]}: bad JSON {e!r}")
            continue
        for day in data.get("data", []) or []:
            for it in day.get("Items", []) or []:
                name = it.get("Name")
                dt = _parse_date(it.get("DateTime"))
                if dt is None or not name:
                    continue  # skip undateable Hub items; no fake stamps
                venue, address = venues.get(
                    it.get("CalendarId"),
                    ("Kingston Hubs", "Chelsea 3196"))
                if it.get("CalendarId") not in venues:
                    # Once per calendar, not once per event: this fired inside
                    # the item loop and printed ~500 identical lines a run,
                    # burying the real per-source summaries underneath it.
                    if it.get("CalendarId") not in unmapped_calendars:
                        unmapped_calendars.add(it.get("CalendarId"))
                        print(f"  Kingston Hubs: unmapped CalendarId "
                              f"{it.get('CalendarId')!r}, using generic venue")
                rows.append({
                    "name": name,
                    "datetime_text": it.get("DateTime", ""),
                    "datetime_iso": dt.isoformat(),
                    "location": venue,
                    "address": address,
                    "price_text": "",
                    "description": name,
                    "source": BASE,
                    "source_id": cfg["id"],
                })
    return rows


# NOTE: Kingston Council / Bayside Council / Frankston live pages are
# WAF-blocked for Python urllib. They are snapshotted via browser into
# scripts/webfetch_snapshots/*.json instead — see dedupe.py. No fetch_*
# functions for them here by design.


# ---------------------------------------------------------------------------
# Source: Greater Dandenong (Drupal HTML, Python-safe)
# ---------------------------------------------------------------------------

# Suburbs the catchment covers. Used to decide whether a row is *out of area*
# (drop) or merely *unclassifiable* (keep).
GD_CATCHMENT = ("springvale", "keysborough", "dandenong", "doveton",
                "cleveland", "noble park", "rowville", "braeside",
                "dandenong south", "dandenong north", "notting hill",
                "bangholme", "heatherton", "mordialloc")

# The detail page states the venue under a labelled field, e.g.
#   Location | Noble Park Community Centre
#            | 44 Memorial Drive, Noble Park
# The listing card has no venue at all -- only title, date and category -- so
# this is the only place the suburb appears.
_GD_LOCATION_LABEL = re.compile(r"^\s*Location\s*$", re.I)
_GD_VENUE_HOSTS = ("greaterdandenong.vic.gov.au",)


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


def _suburb_from_address(address):
    """Suburb from the tail of an Australian address line.

    "44 Memorial Drive, Noble Park" -> "Noble Park"
    "1 Smith St, Dandenong VIC 3175" -> "Dandenong"
    """
    segments = [s.strip() for s in (address or "").split(",") if s.strip()]
    if not segments:
        return ""
    last = re.sub(r"\b(?:VIC|Victoria)\b\.?\s*\d{4}\s*$", "", segments[-1],
                  flags=re.I).strip(" ,.")
    return last


def _classifiable(row, known=GD_CATCHMENT):
    """True when the row names a suburb we recognise, in or out of area."""
    blob = " ".join([row.get("name", ""), row.get("location", ""),
                     row.get("address", ""), row.get("description", "")]).lower()
    return any(s in blob for s in known)


def _passes_suburb_filter(row, allowed):
    """Keep a row when it is in area, or when its suburb is unknown.

    With the detail page fetched, `suburb` is known for every event, so this
    is now a real filter rather than a pass-everything: the listing cards
    carried no venue, so before enrichment every row fell through to
    "unclassifiable, keep it" and the configured catchment did nothing.
    """
    if not allowed:
        return True
    allowed_lower = {a.lower() for a in allowed}
    suburb = (row.get("suburb") or "").lower()
    if suburb:
        return suburb in allowed_lower
    # A venue we could not place: fall back to the whole blob, then to keep.
    blob = " ".join([row.get("name", ""), row.get("location", ""),
                     row.get("address", ""), row.get("description", "")]).lower()
    if any(a in blob for a in allowed_lower):
        return True
    return not _classifiable(row)


def _gd_session():
    """A browser-impersonating session for Greater Dandenong.

    The listing page answers plain urllib, but every event *detail* page
    returns 403 to it, so the same fetcher that worked on the card is blocked
    on the one page the suburb is actually on. curl_cffi with Chrome TLS
    impersonation gets through both. webfetch_http is import-safe here: it
    does not import this module.
    """
    from webfetch_http import make_session
    return make_session()


def _gd_fetch(session, url, timeout=15):
    r = session.get(url, timeout=timeout)
    if r.status_code != 200 or not r.text:
        return None
    return r.text


def fetch_greater_dandenong(cfg):
    session = _gd_session()
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
    for page in range(max_pages):
        url = cfg["url"] if page == 0 else f"{cfg['url']}?page={page}"
        try:
            html = _gd_fetch(session, url, timeout=12)
        except Exception as e:
            print(f"  Greater Dandenong page {page}: FAILED {e!r}")
            continue
        if not html:
            print(f"  Greater Dandenong page {page}: no content")
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
            print(f"  Greater Dandenong: no new events past page {page - 1}")
            break
        time.sleep(0.2)
    print(f"  Greater Dandenong: {len(cards)} distinct events from the listing")

    # Enrich with the venue, which is only on the detail page, then filter.
    rows, placed, unplaced, dropped = [], 0, 0, 0
    for i, link in enumerate(cards):
        if i >= detail_cap:
            print(f"  Greater Dandenong: detail_cap {detail_cap} reached, "
                  f"{len(cards) - i} events left without a venue")
            break
        card = cards[link]
        venue = address = ""
        try:
            html = _gd_fetch(session, link, timeout=12)
        except Exception as e:
            print(f"    detail FAILED {card['name']!r}: {e!r}")
        if html:
            venue, address = _gd_detail_location(
                BeautifulSoup(html, "html.parser"))
        row = {
            "name": card["name"],
            "datetime_text": card["datetime_text"],
            # Deliberately empty: this source's date lives in the description,
            # and stamping the fetch time here produced a bogus timestamp that
            # dedupe.py then had to discard on every single run.
            "datetime_iso": "",
            "location": venue or card["location"] or "Greater Dandenong",
            "address": ", ".join(p for p in (venue, address) if p),
            "suburb": _suburb_from_address(address),
            "price_text": "",
            "description": card["description"],
            "source": link,
            "source_id": cfg["id"],
        }
        if row["suburb"]:
            placed += 1
        else:
            unplaced += 1
        if _passes_suburb_filter(row, allowed):
            rows.append(row)
        else:
            dropped += 1
        time.sleep(0.15)

    kept_subs = sorted({r["suburb"] for r in rows if r["suburb"]})
    print(f"  Greater Dandenong: venue found for {placed}, "
          f"suburb unknown for {unplaced}")
    print(f"  Greater Dandenong: catchment {list(allowed)} keeps {len(rows)}, "
          f"drops {dropped}")
    if kept_subs:
        print(f"    suburbs kept: {', '.join(kept_subs)}")
    return rows


# Frankston live (Everi) is WAF-blocked for Python; see webfetch snapshots.

def fetch_gd_libraries(cfg):
    # GD Libraries returns all events on one page; ?page=N is ignored, so
    # there is no pagination loop here.
    rows = []
    url = cfg["url"]
    session = _gd_session()
    try:
        html = _gd_fetch(session, url, timeout=12)
    except Exception as e:
        print(f"  GD Libraries: FAILED {e!r}")
        return rows
    if not html:
        print("  GD Libraries: no content")
        return rows
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
            rows.append({
                "name": name,
                "datetime_text": date_text,
                "datetime_iso": "",
                "location": "Greater Dandenong Libraries",
                "address": "",
                "price_text": "",
                "description": card.get_text(" ", strip=True)[:300],
                "source": link or cfg["url"],
                "source_id": cfg["id"],
            })
    except Exception as e:
        print(f"  GD Libraries: parse FAILED {e!r}")
        return rows

    # Same platform as the council listing, so the venue is on the detail page
    # under a labelled Location field. Without it every row read "Greater
    # Dandenong Libraries" and no suburb could be told -- the same gap that
    # made the council catchment filter a no-op.
    detail_cap = cfg.get("detail_cap", 60)
    placed = 0
    for row in rows[:detail_cap]:
        try:
            detail = _gd_fetch(session, row["source"], timeout=12)
        except Exception as e:
            print(f"    detail FAILED {row['name']!r}: {e!r}")
            continue
        if not detail:
            continue
        venue, address = _gd_detail_location(
            BeautifulSoup(detail, "html.parser"))
        if venue:
            row["location"] = venue
            row["address"] = ", ".join(p for p in (venue, address) if p)
            row["suburb"] = _suburb_from_address(address)
            if row["suburb"]:
                placed += 1
        time.sleep(0.15)
    print(f"  GD Libraries: {len(rows)} events, venue resolved for {placed}")
    return rows


CHATTY_DAYTIME_RE = re.compile(
    r"\b((?:every\s+|each\s+)?(?:mon|tues|wednes|thurs|fri|satur|sun)day"
    r"(?:\s*(?:and|to|,|/)\s*(?:mon|tues|wednes|thurs|fri|satur|sun)day)*)"
    r"\W{0,12}(\d{1,2}(?::\d{2})?\s*(?:am|pm)?(?:\s*(?:-|–|to)\s*"
    r"\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?)", re.I)


def _chatty_live_schedule(html):
    """First '<weekday(s)> <time>' phrase on a Chatty Cafe venue page."""
    if not html:
        return ""
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    m = CHATTY_DAYTIME_RE.search(text)
    if not m:
        return ""
    return re.sub(r"\s+", " ", f"{m.group(1)} {m.group(2)}").strip()[:80]


def fetch_chatty_cafe(cfg):
    """Fetch Chatty Cafe venues.

    sources.yaml holds the venue list and a fallback schedule. The live page is
    fetched anyway, so prefer a schedule it actually states and fall back to
    the configured one -- previously the page was downloaded and discarded, so
    a changed schedule on the site was invisible.
    """
    rows = []
    for venue in cfg.get("venues", []):
        url = f"https://chattycafeaustralia.org.au/venue/{venue['slug']}/"
        schedule = venue["schedule"]
        try:
            html = _get(url, timeout=10)
            live = _chatty_live_schedule(html)
            if live and live != schedule:
                print(f"  Chatty Cafe {venue['name']}: schedule updated "
                      f"from site")
                schedule = live
        except Exception as e:
            # Keep the configured schedule rather than dropping the venue.
            print(f"  Chatty Cafe {venue['name']}: fetch FAILED {e!r}, "
                  f"using configured schedule")
        rows.append({
            "name": f"Chatty Cafe - {venue['name']}",
            "datetime_text": schedule,
            "datetime_iso": "",
            "location": venue["name"],
            "address": venue["address"],
            "price_text": "Free",
            "description": f"Chatty Cafe at {venue['name']}. {schedule}. "
                           f"A welcoming space for conversation and connection.",
            "source": url,
            "source_id": cfg["id"],
        })
    return rows


def fetch_source(cfg):
    sid = cfg["id"]
    if sid == "kingston_hubs":
        return fetch_kingston_hubs(cfg)
    if sid == "greater_dandenong":
        return fetch_greater_dandenong(cfg)
    if sid == "gd_libraries":
        return fetch_gd_libraries(cfg)
    if sid == "chatty_cafe":
        return fetch_chatty_cafe(cfg)
    # Webfetch-only sources are never fetched in GHA.
    print(f"  skipped (webfetch-only): {sid}")
    return []


def main():
    with open(ROOT / "scripts" / "sources.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    all_rows = []
    failures = []
    for cfg in config["sources"]:
        print(f"Fetching {cfg['name']} ({cfg['id']})...")
        rows = fetch_source(cfg)
        print(f"  -> {len(rows)} events")
        if not rows:
            # A WAF block or a markup change looks exactly like a source that
            # genuinely has no events. Fail the run so CI goes red.
            failures.append((cfg["id"], "returned 0 events"))
        all_rows.extend(rows)

    for r in all_rows:
        # Use existing datetime_iso if valid; only parse from text if missing.
        # Dateless rows keep datetime_iso=None (no fake now() stamps).
        dt = None
        has_real_date = False
        if r.get("datetime_iso"):
            try:
                dt = datetime.fromisoformat(r["datetime_iso"])
                has_real_date = True
            except (ValueError, TypeError):
                dt = None
        if dt is None:
            dt = _parse_date(r.get("datetime_text"))
            if dt is not None:
                has_real_date = True
        r["datetime_iso"] = dt.isoformat() if dt else None
        r["datetime_display"] = (_fmt_dt(dt) if dt
                                  else (r.get("datetime_text") or "").strip())
        r["price_sort"] = _price_sort(r.get("price_text"))
        r["source_label"] = r.get("source_id", "unknown")
        r["has_real_date"] = has_real_date

    print(f"\nTotal raw events: {len(all_rows)}")
    write_json(ROOT / "data" / "raw_events.json", all_rows)
    print("Wrote data/raw_events.json")

    if failures:
        print(f"\n{len(failures)} source(s) returned nothing:")
        for sid, reason in failures:
            print(f"  FAIL: {sid}: {reason}")
        sys.exit(1)


if __name__ == "__main__":
    main()
