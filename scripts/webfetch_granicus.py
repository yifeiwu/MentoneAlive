"""Kingston Council + Kingston Arts (Granicus listings)."""
import re
from datetime import datetime

from bs4 import BeautifulSoup

from webfetch_http import (MIN_CRAWL_DELAY, Pacer, PartialFetch, combine,
                           enrich_details, make_row, paged_listing,
                           pager_control_names, parse_day_month_year, report)
from venues import STREET_SUFFIX_WORDS, needs_address

# ---------------------------------------------------------------------------
# Granicus (Kingston Council + Kingston Arts; the council listing is 31 pages)
# ---------------------------------------------------------------------------
#
# The pager, and why this used to read one page
# ------------------------------------------------
# The council listing was fetched with a single GET and the note "pager is JS
# postback -- page 1 only". The pager is an ASP.NET postback, yes, and it also
# has a non-JS route: a page-number `<select>` and a "Go" submit button, both
# carrying `btn_scPagingNonJS_enabled` classes. A POST carrying the form's
# hidden state and `ctl10$ctl00$ctl07=18` returns page 18. `paged_listing`
# drives it, and the viewstate is re-read from every response because each
# response issues a fresh one and the previous is spent.
#
# The cost of not walking it was not a missing page, it was a *silently*
# missing page. The listing is sorted by next occurrence, page 1 holds the next
# ten events, and events rotated off it as real dates filled those two days.
# Chinese Senior Citizens Club of Kingston sat on page 18 and Tea & Talk
# Chinese Conversation Table on page 27; both stayed published upstream the
# whole time, both returned HTTP 200, and both were absent from every snapshot
# from the moment their date passed the ten-event window. Nothing failed,
# because page 1 is a perfectly good fetch of a listing that is 31 pages long.
#
# A source with no `max_pages` still reads page 1 only. That is deliberate:
# `kingston_arts` is one page deep and should not pay for a walk it does not
# need.

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
# loose postcode pattern accepts a year, so "Refugia 2026" matched. The word
# list is shared with build_site.extract_suburb(), which refuses to read one
# of these as a suburb.
GRANICUS_STREET_RE = re.compile(
    r"^\s*\d+[A-Za-z]?(?:[-/][A-Za-z0-9]+)*\s|"
    r"\b(?:" + STREET_SUFFIX_WORDS + r")\b", re.I)
# A Victorian postcode. Enough on its own to call a string an address, which is
# what the listing cards mostly carry.
GRANICUS_POSTCODE_RE = re.compile(r"\b3\d{3}\b")


def _is_street_line(text):
    return bool(GRANICUS_STREET_RE.search(text or ""))


def _states_a_place(address):
    """True when this string is somewhere a reader could be sent.

    Not "non-empty". Thirty-one of the council's 301 listings state their
    address as the literal words "Multiple locations" -- a library storytime
    that runs at three branches, a road safety program that runs wherever it
    is booked. That string is non-empty, so it satisfied both the old
    `drop_venueless()` and the health check's "every non-online row has an
    address", and 31 events would have published with an address no reader can
    act on and a blank suburb.

    It has to be a place or it has to be nothing: a postcode, or a street line.
    Anything else is a label, and the detail pass gets the chance to replace it
    with a real one first.
    """
    text = (address or "").strip()
    return bool(GRANICUS_POSTCODE_RE.search(text) or _is_street_line(text))


def fetch_granicus(cfg, session=None, detail_cap=None):
    # One clock for the whole source. The listing walk and the detail crawl run
    # on the same pacer, so `crawl_delay` means one request per N seconds of
    # this host rather than per loop -- which is the only reading of it that
    # bounds the pressure put on the site. The floor is the shared one, so a
    # `crawl_delay: 0` cannot turn this into an unbounded crawler.
    pacer = Pacer(delay=cfg.get("crawl_delay"), floor=MIN_CRAWL_DELAY,
                  default=0.0)
    listing = paged_listing(session, cfg, pacer=pacer)

    rows = []
    for page_no, page in enumerate(listing.pages, 1):
        found = _rows_from_page(cfg, page)
        if page_no == 1 and not found:
            # A listing page that will not load is a broken fetch, not an empty
            # calendar. Returning [] made the orchestrator report "returned 0
            # rows", which is also what a quiet week looks like, and the
            # previous good snapshot was kept for the wrong reason.
            raise PartialFetch(
                f"listing page {cfg['url']} returned no cards -- the markup "
                f"changed, or this source is genuinely empty")
        rows.extend(found)
    report(f"listing: {len(rows)} events"
           + (f" over {len(listing.pages)} of {listing.claimed} pages"
              if listing.claimed and len(listing.pages) > 1 else ""))
    enrich_granicus_details(session, rows, detail_cap, pacer)
    return drop_venueless(rows)


def _rows_from_page(cfg, soup):
    """One listing page's worth of rows, in the order the site prints them."""
    rows = []
    for card in soup.select("div.list-item-container article"):
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
        # The card states day/month/year in three elements and often omits the
        # year, so it is appended rather than searched for. Decided once,
        # because the same test was being run twice to build one argument.
        has_year = bool(re.search(r"\d{4}", date_text))
        if date_text and not has_year:
            report(f"date {date_text!r} has no year, appending current year",
                   level="debug")
        day = parse_day_month_year(date_text if has_year or not date_text
                                   else f"{date_text} {datetime.now().year}")
        rows.append(make_row(
            cfg["id"], name, link,
            datetime_iso=day.isoformat() if day else "",
            datetime_text=date_text,
            location=venue,
            # Seeded from the listing's address block, which on 267 of the
            # council's 301 listings already holds a full street address. The
            # detail pass replaces it with a cleaner one and supplies the start
            # time, which no card states; drop_venueless() then discards
            # anything still holding only a label.
            address=venue,
            description=desc[:400],
        ))
    return rows


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

    The test is "states a place", not "is not blank". This ran *after* the
    detail pass, so a row still holding a venue name or the words "Multiple
    locations" has been given its chance and did not take it -- the docstring
    claimed that and the code only ever checked for emptiness, which is how 31
    unplaceable listings would have passed a check that asks for an address.
    """
    kept, dropped = [], []
    for r in rows:
        if not needs_address(r) or _states_a_place(r.get("address")):
            kept.append(r)
        else:
            dropped.append(r)
    if dropped:
        report(f"dropped {len(dropped)} listing(s) with no venue (a campaign "
               f"page, or a program that runs at several sites, rather than an "
               f"event at a place): "
               f"{[r.get('name') for r in dropped][:5]}", level="warn")
    return kept


def enrich_granicus_details(session, rows, cap, pacer=None):
    """Fill each row's real date, street address and (when the page gives no
    venue) a venue name, from the event's own page."""
    n = enrich_details(session, rows, cap, _apply_granicus_detail,
                       label="granicus", pacer=pacer)
    report(f"details enriched: {n}")
    if cap is not None and len(rows) > cap:
        # The listing card states a date and no time, so an unenriched row keeps
        # midnight and renders as *all day*. That is the same class of quietly
        # wrong row as the missing pages were -- a class that is invisible in the
        # output, because an all-day row looks like a well-formed row -- so the
        # count that did not get a page is named rather than left to be
        # inferred from a cap in the config.
        report(f"{len(rows) - cap} of {len(rows)} rows were not opened "
               f"(detail_cap {cap}), so they keep the card's date and no start "
               f"time and will publish as all day", level="warn")


def _apply_granicus_detail(r, html):
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("#main-content") or soup
    date_el = main.select_one("p.event-date")
    if date_el:
        txt = date_el.get_text(" ", strip=True)
        m = re.search(r"(\w+),\s*(\d{1,2})\s+(\w+)\s+(\d{4})", txt)
        if m:
            day = parse_day_month_year(
                f"{m.group(2)} {m.group(3)} {m.group(4)}")
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
    street = suburb = postcode = None
    # The newline form, tried first because it says unambiguously where each
    # segment ends. Every candidate is checked: the loose postcode pattern
    # also matches a title followed by a year ("Stitch with Sappho,
    # workshops during 2026") or a contact block followed by a phone prefix
    # ("Contact, Community Connections 1300"), and a title is not a place
    # to send anyone. Requiring a street in group 2 -- a number, or a
    # road-type word -- plus a Victorian postcode keeps those out, and
    # scanning every match (rather than the first) lets a false hit earlier
    # in the page fall through to the real address below it.
    for m in GRANICUS_ADDRESS_RE.finditer(text):
        cand_street, cand_suburb, cand_postcode = (
            m.group(2), m.group(3), m.group(4))
        if _is_street_line(cand_street) and cand_postcode.startswith("3"):
            street, suburb, postcode = cand_street, cand_suburb, cand_postcode
            break
    if street is None:
        inline_text = (block or main).get_text(" ", strip=True)
        for m in GRANICUS_ADDRESS_INLINE_RE.finditer(inline_text):
            cand_street = m.group(1)
            # An inline match can also be a title followed by a year, and
            # the postcode pattern is loose enough to accept one: an event
            # called "Refugia 2026" was published as the address
            # "Kerri Wilson McConchie, Refugia 2026". Requiring a street
            # in group 1 -- a number, or a road-type word -- keeps a
            # person's name out of the address field.
            if not _is_street_line(cand_street):
                continue
            if not m.group(3).startswith("3"):
                continue
            street, suburb, postcode = cand_street, m.group(2), m.group(3)
            break
    if street:
        r["address"] = f"{street.strip()}, {suburb.strip()} " \
                       f"{postcode.strip()}"
        if not r["location"]:
            # Only derive a venue name from the street when the page gave
            # none. Strip a leading house number: "979 Nepean Highway" is
            # an address, not a place name.
            name_only = re.sub(r"^\d+[A-Za-z]?\s*[-\s]\s*", "", street.strip())
            r["location"] = name_only if len(name_only) <= 60 \
                else name_only[:57] + "..."


if __name__ == "__main__":
    # The address block is the only place a Granicus row states where to go,
    # and a row whose "address" is the venue name sends a reader to a suburb
    # on the strength of a place they cannot look up. The pattern missed the
    # format the site actually publishes, so this never fired and 20 published
    # rows carried a venue name as their address.
    #
    # These cases run the production reader (_apply_granicus_detail) rather
    # than a reimplementation of it. The harness used to repeat the
    # newline-then-inline dispatch by hand, so it validated a copy that could
    # drift from the code that runs.
    def address_of(html_text):
        row = make_row("test", "Event", "https://example.invalid/x",
                       location="Kingston Arts Centre",
                       address="Kingston Arts Centre")
        _apply_granicus_detail(row, html_text)
        addr = row.get("address") or ""
        # The caller seeds address from the listing's venue name, so an address
        # the reader did not replace is reported as no address at all.
        return None if addr == "Kingston Arts Centre" else addr

    failures = []

    def check(label, actual, expected):
        if actual == expected:
            print(f"ok   {label}")
        else:
            print(f"FAIL {label}\n       actual:   {actual!r}"
                  f"\n       expected: {expected!r}")
            failures.append(label)

    # --- what the detail reader makes of a page's address block -------------
    check("tag-separated address",
          address_of("<div>Kingston Arts Centre</div><div>979 Nepean "
                     "Highway</div><div>Moorabbin 3189</div>"),
          "979 Nepean Highway, Moorabbin 3189")
    check("inline address",
          address_of("<p>Kingston Arts Centre, 979 Nepean Highway, "
                     "Moorabbin 3189</p>"),
          "Kingston Arts Centre, 979 Nepean Highway, Moorabbin 3189")
    check("inline address with a state",
          address_of("<p>1 Example St, Cheltenham, VIC 3192</p>"),
          "1 Example St, Cheltenham 3192")
    check("two-word suburb",
          address_of("<p>Frankston North Library, 21 Beach St, "
                     "Frankston North VIC 3199</p>"),
          "Frankston North Library, 21 Beach St, Frankston North 3199")
    check("a page with no address yields nothing",
          address_of("<div>Some event page with no location block</div>"),
          None)
    # An event title is not a street address, and the loose postcode
    # pattern accepts a year, so "Refugia 2026" matched and published
    # "Kerri Wilson McConchie, Refugia 2026" as the address.
    check("a title followed by a year is not an address",
          address_of("<p>Kerri Wilson McConchie, Refugia 2026</p>"), None)
    check("a title followed by words is not an address",
          address_of("<p>Susannah Langley, Testing Grounds Sounds</p>"), None)
    # A workshop blurb followed by a year is not an address either, and
    # the newline form had no street guard at all, so "Stitch with
    # Sappho, workshops during 2026" published and "workshops during"
    # appeared as a suburb.
    check("a workshop blurb followed by a year is not an address",
          address_of("<div>Header</div><div>Stitch with Sappho</div>"
                     "<div>workshops during 2026</div>"), None)
    # A contact block followed by a phone prefix is not an address, and
    # for the same missing guard "Contact, Community Connections 1300"
    # published with "Community Connections" as the suburb.
    check("a contact block followed by a phone prefix is not an address",
          address_of("<div>Header</div><div>Contact</div>"
                     "<div>Community Connections 1300</div>"), None)
    # A false hit earlier in the page must not hide the real address
    # below it: the first candidate is skipped and the scan continues.
    check("a false hit falls through to the real address",
          address_of("<div>Stitch with Sappho</div><div>workshops during "
                     "2026</div><div>Shirley Burke Theatre</div>"
                     "<div>64 Parkers Road</div><div>Parkdale 3195</div>"),
          "64 Parkers Road, Parkdale 3195")

    # --- a string in the address field is not the same as a place in it ---
    # What walking the whole listing brought with it. `drop_venueless` used to
    # ask only whether the field was blank, and 31 of the council's 301
    # listings say "Multiple locations" -- non-empty, so it satisfied that test
    # and the health check's "every non-online row has an address", and would
    # have published a row with no suburb and nowhere to send a reader.
    def kept(name, location, address):
        row = make_row("test", name, "https://example.invalid/x",
                       location=location, address=address)
        return bool(drop_venueless([row]))

    check("a full street address is kept",
          kept("Tai Chi", "Clarinda Community Centre",
               "58B Viney Street, Clarinda 3169"), True)
    # The listing card's own form, venue name prefixed, for a row the detail
    # pass has not cleaned yet. Still somewhere a reader can be sent.
    check("a card address with the venue prefixed is kept",
          kept("Storytime", "Cheltenham Library",
               "Cheltenham Library, 12 Stanley Avenue, Cheltenham 3192"), True)
    check("'Multiple locations' is not a place",
          kept("Storytime", "Cheltenham Library", "Multiple locations"), False)
    check("a bare venue name is not a place",
          kept("Tai Chi", "Clarinda Community Centre",
               "Clarinda Community Centre"), False)
    check("an empty address is not a place",
          kept("Biketober", "", ""), False)
    # An online event is exempt, and the exemption is exactly "there is nowhere
    # to be sent", so the address test does not apply to it.
    check("an online row needs no address",
          kept("Webinar", "Online via Zoom", ""), True)

    # --- the pager walk, against fake pages -------------------------------
    # The failure these pin is the one the council listing actually had: a POST
    # that is accepted, answered with page 1, and published as if it were the
    # whole listing. Chinese Senior Citizens Club sat on page 18 and Tea & Talk
    # on page 27 for months that way, both still live on the council's site.
    #
    # The fake pages declare the control names in their own markup and the fake
    # session reads them back off page 1 with the production
    # `pager_control_names`, so the walk has to *discover* them. A hard-coded
    # name would make these pass while the real thing silently stopped paging.

    def listing_page(n, claimed):
        opts = "".join(f'<option value="{i}">{i}</option>'
                       for i in range(1, claimed + 1))
        # `get()` refuses a body under 1000 bytes as a truncated response, so
        # the fakes are padded past it. Without that they fail as "listing page
        # failed to load" before the pager is ever reached, which would make
        # every case below pass for the wrong reason.
        pad = "<p>" + ("filler " * 120) + "</p>"
        return (
            '<form id="mainForm"><input type="hidden" name="v" value="s">'
            f'{pad}<div class="seamless-pagination-data">'
            f'<select name="ctl10$ctl00$ctl07">{opts}</select></div>'
            '<div class="seamless-pagination-controls">'
            '<input type="submit" name="ctl10$ctl00$ctl08" value="Go">'
            '</div>'
            f'<div class="seamless-pagination-info">Page {n} of {claimed}'
            f'</div>{pad}</form>')

    class FakeResponse:
        def __init__(self, text):
            self.status_code = 200
            self.text = text
            self.content = text.encode()

    def walk(pages, max_pages=None):
        """Drive paged_listing over fake pages; return (result, request log)."""
        log = []

        class FakeSession:
            def get(self, url, **kw):
                log.append(("GET", 1))
                return FakeResponse(pages[0])

            def post(self, url, data=None, **kw):
                select, go = pager_control_names(
                    BeautifulSoup(pages[0], "html.parser"))
                assert data.get(go) == "Go", "the Go button was not discovered"
                want = int(data[select])
                log.append(("POST", want))
                return FakeResponse(pages[want - 1])

        cfg = {"id": "t", "url": "https://x.invalid/e"}
        if max_pages is not None:
            cfg["max_pages"] = max_pages
        return paged_listing(FakeSession(), cfg), log

    three = [listing_page(n, 3) for n in (1, 2, 3)]

    got, log = walk(three)
    check("a source with no max_pages still reads one page",
          len(got.pages), 1)
    check("and issues exactly one request", len(log), 1)

    got, log = walk(three, max_pages=3)
    check("max_pages walks every page the site claims", len(got.pages), 3)
    check("the claimed depth is reported", got.claimed, 3)
    check("nothing was capped off", got.capped, False)
    check("one request per page: the first, then one POST each",
          len(log), 3)
    check("and the pages asked for are 2 then 3",
          [want for _, want in log[1:]], [2, 3])

    got, _ = walk(three, max_pages=2)
    check("a budget below the claim stops the walk", len(got.pages), 2)
    check("and reports that it was capped, not broken", got.capped, True)

    # Page 2 answering with page 1 again: a wrong control name, or a viewstate
    # the server will not accept.
    try:
        walk([three[0], three[0], three[2]], max_pages=3)
        check("a pager that will not advance is refused", "published", "refused")
    except PartialFetch as e:
        check("a pager that will not advance is refused",
              "did not advance" in e.reason, True)

    # A listing that stopped saying how deep it is, on a source configured to be
    # walked. Publishing page 1 here is the defect this change exists to remove,
    # so it must not be a quiet success.
    mute = ['<form id="mainForm">' + "<p>" + ("filler " * 200) + "</p>"
            '<div class="list-item-container"><article>'
            '<a href="https://x.invalid/a">A</a></article></div></form>']
    try:
        walk(mute, max_pages=10)
        check("a walk that cannot see a page count is refused",
              "published", "refused")
    except PartialFetch as e:
        check("a walk that cannot see a page count is refused",
              "no page count" in e.reason, True)

    # A page beyond the first failing to load is a walk cut short, not a budget
    # reached, and must not publish.
    class BrokenSession:
        def get(self, url, **kw):
            return FakeResponse(three[0])

        def post(self, url, data=None, **kw):
            return FakeResponse("blocked")

    try:
        paged_listing(BrokenSession(),
                      {"id": "t", "url": "https://x.invalid/e",
                       "max_pages": 3})
        check("a page that will not load is refused", "published", "refused")
    except PartialFetch as e:
        check("a page that will not load is refused", "did not load" in e.reason,
              True)

    # --- the rate limit ---------------------------------------------------
    # The council source sets the slowest delay in the config, and a
    # `crawl_delay: 0` must not be a way to switch the limit off.
    check("a crawl_delay of 0 still yields the floor",
          Pacer(delay=0, floor=MIN_CRAWL_DELAY).delay, MIN_CRAWL_DELAY)
    check("a crawl_delay below the floor is raised to it",
          Pacer(delay=0.01, floor=MIN_CRAWL_DELAY).delay, MIN_CRAWL_DELAY)
    check("the configured 5s is taken as given",
          Pacer(delay=5.0, floor=MIN_CRAWL_DELAY).delay, 5.0)
    # A value that is not a number falls back to the fetcher's own default
    # (none) and is then floored -- so a typo slows down to the shared minimum
    # rather than switching the limit off, and it does not silently become the
    # 5s the operator asked for either.
    check("a nonsense crawl_delay lands on the floor, not on no limit",
          Pacer(delay="soon", floor=MIN_CRAWL_DELAY, default=0.0).delay,
          MIN_CRAWL_DELAY)
    # One clock for the listing walk and the detail crawl, which is the only
    # reading of the number that bounds pressure on the host.
    waits = []
    pacer = Pacer(delay=5.0, floor=MIN_CRAWL_DELAY, pace=waits.append,
                  jitter=0)
    pacer.take()
    check("the first request is not delayed", len(waits), 0)
    pacer.take()
    check("and the second waits out the full interval",
          round(waits[0]), 5)
    check("the pacer counted both requests", pacer.spent, 2)

    if failures:
        print(f"\ngranicus: {len(failures)} case(s) FAILED")
        raise SystemExit(1)
    print("\nall granicus cases as expected")