"""Kingston Council + Kingston Arts (Granicus listings, page 1)."""
import re
import time
from datetime import datetime

from bs4 import BeautifulSoup

from webfetch_http import combine, get, parse_day_month_year
from venues import needs_address

# ---------------------------------------------------------------------------
# Granicus (Kingston Council + Kingston Arts, page 1; pager is JS postback)
# ---------------------------------------------------------------------------

# "Kingston Arts Centre, 979 Nepean Highway, Moorabbin 3189", optionally with
# a "VIC" the site does not actually write, and tolerating an address split
# across tags (the caller reads the page with get_text("\n")).
# The suburb is a single word in every Australian address these sites publish,
# so matching one word (rather than "up to 40 characters") is what stops the
# street being absorbed into it when the segments are space-separated rather
# than comma-separated. Where a suburb is two words ("Frankston North") the
# non-greedy street still gives the right split, because the postcode anchors
# the match.
GRANICUS_ADDRESS_RE = re.compile(
    r"([A-Za-z0-9'’.][A-Za-z0-9'’.,\- ]{2,90}?)\s*\n\s*"
    r"([A-Za-z0-9][A-Za-z0-9'’. \-]{2,60}?)\s*\n\s*"
    r"([A-Za-z][A-Za-z .'-]{1,30}?)\s*,?\s*"
    r"(?:VIC\.?\s*)?(\d{4})\b")
# The same address, but every segment on one line -- a venue that writes it
# inline. Commas are required so "979" cannot be read as the street.
GRANICUS_ADDRESS_INLINE_RE = re.compile(
    r"([A-Za-z0-9'’.][A-Za-z0-9'’.,\- ]{2,90}?),\s*"
    r"([A-Za-z][A-Za-z .'-]{1,40}?)\s*,?\s*"
    r"(?:VIC\.?\s*)?(\d{4})\b")
# A street line: a house number at the start, or a road-type word anywhere in
# it. Used to reject an inline "address" that is really an event title -- the
# loose postcode pattern accepts a year, so "Refugia 2026" matched.
GRANICUS_STREET_RE = re.compile(
    r"^\s*\d+[A-Za-z]?(?:[-/][A-Za-z0-9]+)*\s|"
    r"\b(?:Street|St|Road|Rd|Avenue|Ave|Highway|Hwy|Parade|Pde|Drive|Dr|Lane|"
    r"Ln|Place|Pl|Square|Sq|Terrace|Court|Ct|Boulevard|Blvd|Walk|Crescent|"
    r"Promenade|Prom|Esplanade|Crescent|Cct|Circuit)\b", re.I)


def _is_street_line(text):
    return bool(GRANICUS_STREET_RE.search(text or ""))


def fetch_granicus(session, cfg, detail_cap):
    html = get(session, cfg["url"], retries=3)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("div.list-item-container article")
    rows = []
    for card in cards:
        a = card.select_one("a[href]")
        title_el = card.select_one("h2.list-item-title, h3.list-item-title")
        if not a or not title_el:
            continue
        link = a["href"]
        # Only absolute http(s) links are safe to publish. Anything else --
        # notably javascript: -- would be inlined into index.html and reach an
        # href, where HTML-escaping does not neutralise the scheme.
        if link.startswith("/"):
            link = cfg.get("base", "https://www.kingston.vic.gov.au") + link
        if not link.startswith(("http://", "https://")):
            continue
        name = title_el.get_text(strip=True)
        d = card.select_one(".part-date")
        mo = card.select_one(".part-month")
        yr = card.select_one(".part-year")
        date_text = " ".join(x.get_text(strip=True) for x in (d, mo, yr) if x)
        desc_el = card.select_one(".list-item-block-desc")
        desc = desc_el.get_text(" ", strip=True) if desc_el else name
        addr_el = card.select_one(".list-item-address")
        venue = addr_el.get_text(" ", strip=True) if addr_el else ""
        if date_text and not re.search(r"\d{4}", date_text):
            print(f"  {cfg['id']}: date {date_text!r} has no year, appending current year")
        day = parse_day_month_year(date_text + f" {datetime.now().year}"
                                   if date_text and not re.search(r"\d{4}", date_text)
                                   else date_text)
        rows.append({
            "name": name,
            "datetime_text": date_text,
            "datetime_iso": day.isoformat() if day else "",
            "location": venue,
            "address": venue,
            "price_text": "",
            "description": desc[:400],
            "source": link,
            "source_id": cfg["id"],
        })
    print(f"  {cfg['id']} listing: {len(rows)} (page 1; JS pager needs manual snapshots for deeper pages)")
    enrich_granicus_details(session, rows, detail_cap)
    return drop_venueless(rows)


def drop_venueless(rows):
    """Drop listings the source never gives a venue for.

    Not every Granicus page is an event with a place. `biodiversity-month` is a
    month-long campaign page whose five constituent events are at five
    different reserves and clubs, and the page itself carries no Location block
    at all. Publishing it as one row produced a calendar entry with no address
    and a date range that no reader can act on -- the same reason
    recurrence.py removes undateable listings.

    It cannot be given a synthetic address either, which is the lesson from
    fetch_kingston_hubs(): one plausible-looking address is worse than none,
    because a wrong venue sends a reader to the wrong suburb.
    """
    kept, dropped = [], []
    for r in rows:
        if (r.get("address") or "").strip() or not needs_address(r):
            kept.append(r)
        else:
            dropped.append(r)
    if dropped:
        print(f"  Dropped {len(dropped)} listing(s) with no venue (a campaign "
              f"page, not an event at a place): "
              f"{[r.get('name') for r in dropped][:5]}")
    return kept


def enrich_granicus_details(session, rows, cap):
    n = 0
    for r in rows:
        if n >= cap:
            break
        html = get(session, r["source"])
        if not html:
            continue
        n += 1
        soup = BeautifulSoup(html, "html.parser")
        main = soup.select_one("#main-content") or soup
        date_el = main.select_one("p.event-date")
        if date_el:
            txt = date_el.get_text(" ", strip=True)
            m = re.search(r"(\w+),\s*(\d{1,2})\s+(\w+)\s+(\d{4})", txt)
            if m:
                day = parse_day_month_year(f"{m.group(2)} {m.group(3)} {m.group(4)}")
                if day:
                    r["datetime_iso"] = combine(day, txt).isoformat()
                    r["datetime_text"] = txt[:120]
        # The street address, which lives in a labelled block on the detail
        # page. Two things this pattern had to allow for, both of which made
        # it never fire, so every row kept the venue *name* as its address
        # (fetch_granicus() seeds "address" from the listing's address block,
        # which on these pages holds the venue name):
        #
        #   * Granicus writes "Moorabbin 3189", not "Moorabbin, VIC 3189",
        #     so requiring a VIC token missed the real format.
        #   * get_text("\n") puts a newline between the venue, the street and
        #     the suburb, and the character class excluded \n, so an address
        #     split across tags could not match either.
        #
        # Two forms, both anchored on the postcode: the newline form, tried
        # first because it says unambiguously where each segment ends, and the
        # inline form for a venue that writes the address on one line.
        #
        # Scoped to the page's own address block where one exists. Read from
        # the whole page, the loose postcode pattern also matches an event
        # title followed by a year, and a title is not a place to send
        # anyone. Not every page has the block, so the whole page is the
        # fallback rather than only option.
        block = main.select_one(".address-block, .event-address, "
                                "#event-address, .location-block")
        text = (block or main).get_text("\n", strip=True)
        m = GRANICUS_ADDRESS_RE.search(text)
        if m:
            # venue, street, suburb, postcode. The venue has its own line in
            # this form and is not part of the address, so the street is
            # group 2.
            street, suburb, postcode = m.group(2), m.group(3), m.group(4)
        else:
            m = GRANICUS_ADDRESS_INLINE_RE.search(
                (block or main).get_text(" ", strip=True))
            street = m.group(1) if m else None
            if street:
                # An inline match can also be a title followed by a year, and
                # the postcode pattern is loose enough to accept one: an event
                # called "Refugia 2026" was published as the address
                # "Kerri Wilson McConchie, Refugia 2026". Requiring a street
                # in group 1 -- a number, or a road-type word -- keeps a
                # person's name out of the address field.
                if not _is_street_line(street):
                    street = None
                else:
                    suburb, postcode = m.group(2), m.group(3)
        if street:
            r["address"] = f"{street.strip()}, {suburb.strip()} " \
                           f"{postcode.strip()}"
            if not r["location"] or r["location"] in ("Greater Dandenong",):
                # Only derive a venue name from the street when the page gave
                # none. Strip a leading house number: "979 Nepean Highway" is
                # an address, not a place name.
                name_only = re.sub(r"^\d+[A-Za-z]?\s*[-\s]\s*", "", street.strip())
                r["location"] = name_only if len(name_only) <= 60 \
                    else name_only[:57] + "..."
        time.sleep(0.2)
    print(f"  granicus details enriched: {n}")


if __name__ == "__main__":
    # The address block is the only place a Granicus row states where to go,
    # and a row whose "address" is the venue name sends a reader to a suburb
    # on the strength of a place they cannot look up. The pattern missed the
    # format the site actually publishes, so this never fired and 20 published
    # rows carried a venue name as their address.
    def address_of(html_text, mode="newline"):
        text = (BeautifulSoup(html_text, "html.parser")
                .get_text("\n" if mode == "newline" else " ", strip=True))
        m = GRANICUS_ADDRESS_RE.search(text)
        if m:
            return f"{m.group(2).strip()}, {m.group(3).strip()} {m.group(4)}"
        m = GRANICUS_ADDRESS_INLINE_RE.search(text)
        if not m or not _is_street_line(m.group(1)):
            return None
        return f"{m.group(1).strip()}, {m.group(2).strip()} {m.group(3)}"

    TESTS = [
        # The real shape: venue, street and suburb in separate elements, with
        # no "VIC" -- Granicus writes "Moorabbin 3189".
        ("tag-separated address",
         address_of("<div>Kingston Arts Centre</div><div>979 Nepean "
                    "Highway</div><div>Moorabbin 3189</div>"),
         "979 Nepean Highway, Moorabbin 3189"),
        ("inline address",
         address_of("<p>Kingston Arts Centre, 979 Nepean Highway, "
                    "Moorabbin 3189</p>", "space"),
         "Kingston Arts Centre, 979 Nepean Highway, Moorabbin 3189"),
        ("inline address with a state",
         address_of("<p>1 Example St, Cheltenham, VIC 3192</p>", "space"),
         "1 Example St, Cheltenham 3192"),
        ("two-word suburb",
         address_of("<p>Frankston North Library, 21 Beach St, "
                    "Frankston North VIC 3199</p>", "space"),
         "Frankston North Library, 21 Beach St, Frankston North 3199"),
        ("a page with no address yields nothing",
         address_of("<div>Some event page with no location block</div>"),
         None),
        # An event title is not a street address, and the loose postcode
        # pattern accepts a year, so "Refugia 2026" matched and published
        # "Kerri Wilson McConchie, Refugia 2026" as the address.
        ("a title followed by a year is not an address",
         address_of("<p>Kerri Wilson McConchie, Refugia 2026</p>", "space"),
         None),
        ("a title followed by words is not an address",
         address_of("<p>Susannah Langley, Testing Grounds Sounds</p>", "space"),
         None),
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
        print(f"\ngranicus: {len(failures)}/{len(TESTS)} cases FAILED")
        raise SystemExit(1)
    print(f"\nall {len(TESTS)} granicus address cases as expected")
