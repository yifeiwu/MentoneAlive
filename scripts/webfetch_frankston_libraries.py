"""Frankston City Libraries What's On (SirsiDynix / Encore-style CMS).

The listing page publishes ten cards and no pagination that works -- `?page=2`,
`?p=2`, `?pageNumber=2` all return page 1, and `/Page-2` is a 404 -- so the ten
cards are what the listing offers, and each card links to its own page.

Those own pages are where the series live. A recurring library programme lists
every one of its dates in the page body, which is why three series that were
being withheld from the page (Justice of the Peace Saturdays, Basic Tech Help,
Weekend Board Games) had no dates to publish: the fixture recorded them without
a schedule and there was no fetcher to supply one. This fetcher does.

Two traps, both found by reading the page rather than the docs:

* `Next date: Saturday, 03 October 2026 | 10:00 AM to 01:00 PM` is the *next*
  occurrence, not the only one. The whole run of dates is in the page body, so
  the fetcher collects every date string and emits one row per date. Taking
  `.event-date` alone publishes twelve January sessions as one, and drops the
  other eleven.
* The date is split across three sibling spans (`part-date`, `part-month`,
  `part-year`) inside a card but written as one string on a detail page, so the
  card reader and the detail reader are different functions on purpose.
"""
import re
from datetime import date

from bs4 import BeautifulSoup

from webfetch_http import (PartialFetch, combine, get, make_row,
                           parse_day_month_year, range_start_time, report)

# ---------------------------------------------------------------------------
# Frankston City Libraries
# ---------------------------------------------------------------------------

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}

# A full date as the detail pages write it, e.g.
# "Saturday, 03 October 2026". The comma is optional and sometimes absent.
DATE_RE = re.compile(
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s*"
    r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", re.I)

# "10:00 AM to 01:00 PM" / "10:00 AM - 01:00 PM". Only the start is wanted: a
# session's start time is its identity for dedupe, and a range that crosses
# noon or midnight is why range_start_time() exists rather than a bare
# \d{1,2}:\d{2}.
TIME_RE = re.compile(
    r"(\d{1,2}:\d{2}\s*(?:AM|PM)?)\s*(?:to|-|–|—)\s*"
    r"\d{1,2}:\d{2}\s*(?:AM|PM)?", re.I)

NEXT_DATE_RE = re.compile(r"Next date:\s*(.*)$", re.S)

# Ignored outright: these state a month with no day ("December 2026"), which is
# a schedule note and not an occurrence. Materialising the first of the month
# would publish a library session on New Year's Eve.
_NO_DAY_RE = re.compile(r"^\s*[A-Za-z]+\s+\d{4}\s*$")


def _detail_dates(html):
    """(day, start_time) for every date the detail page publishes.

    Every date in the body, not just `.event-date`: that element names the next
    occurrence only, and a term's remaining dates sit after it in the same run.
    """
    soup = BeautifulSoup(html, "html.parser")
    # The time is stated once, next to "Next date:", and applies to the run.
    stamp = ""
    node = soup.select_one(".event-date")
    if node:
        tm = TIME_RE.search(node.get_text(" ", strip=True))
        if tm:
            stamp = tm.group(1)

    seen, out = set(), []
    for m in DATE_RE.finditer(html):
        day_num, month_name, year = m.group(1), m.group(2), m.group(3)
        month = MONTHS.get(month_name.strip().lower()[:9])
        if not month:
            continue
        try:
            d = date(int(year), month, int(day_num))
        except ValueError:
            continue
        if d in seen:
            continue
        seen.add(d)
        out.append((d, stamp))
    return out


def _listing_cards(soup):
    """(name, link, date_text, description, address, price) per card."""
    for art in soup.select("article"):
        title = art.select_one("h2.list-item-title")
        link = art.select_one("a[href]")
        if not title or not link:
            continue
        date_el = art.select_one(".list-item-block-date")
        desc = art.select_one(".list-item-block-desc")
        addr = art.select_one("p.list-item-address")
        tags = art.select_one(".tagged-as-list .text")
        yield (
            title.get_text(" ", strip=True),
            link.get("href"),
            date_el.get_text(" ", strip=True) if date_el else "",
            (desc.get_text(" ", strip=True) if desc else ""),
            (addr.get_text(" ", strip=True) if addr else "").replace("\xa0", " "),
            (tags.get_text(" ", strip=True) if tags else "").lstrip(", "),
        )


def _detail_facts(html):
    """(description, address, price) from a detail page."""
    soup = BeautifulSoup(html, "html.parser")
    desc = ""
    for sel in (".event-description", ".item-detail", "main"):
        node = soup.select_one(sel)
        if node:
            desc = node.get_text(" ", strip=True)
            break
    addr = ""
    for sel in ("[class*=address]", ".event-location"):
        node = soup.select_one(sel)
        if node:
            addr = node.get_text(" ", strip=True).replace("\xa0", " ")
            break
    price = ""
    for node in soup.select("[class*=cost]"):
        t = node.get_text(" ", strip=True)
        if t and t.lower() != "cost":
            price = t
            break
    return desc, addr, price


def fetch_frankston_libraries(cfg, session=None, detail_cap=None):
    """The ten listed programmes, each expanded to every date it publishes."""
    listing = cfg["url"]
    html = get(session, listing)
    if not html:
        raise PartialFetch(f"{listing} failed to load")
    cards = list(_listing_cards(BeautifulSoup(html, "html.parser")))
    if not cards:
        raise PartialFetch(
            f"{listing} published no cards -- the CMS markup changed, and "
            f"returning nothing would replace a good snapshot with silence")

    cap = detail_cap if detail_cap is not None else len(cards)
    rows, opened = [], 0
    # The listing repeats a card when a series has both an early date and a run
    # of later ones: "Weekend Board Games Carrum Downs" appeared twice, once for
    # a single 2 Nov date and once for the twenty-two-date term. Fetched twice,
    # emitted twice, and every duplicate merge in dedupe.py then throws all but
    # one copy away -- so the waste is real and the dedupe handles it. Keyed on
    # the link so a genuine re-list of the same page is fetched once.
    # Later card wins, and that is deliberate rather than an accident of dict
    # insertion. The two cards for one series state different things: the first
    # is a single early date, the second the term's full run, and the first is
    # also the one whose description is a stub. Taking the first would publish
    # the series with one description and lose the other twenty-one dates -- the
    # run is only ever in the later card.
    by_link, order = {}, []
    for card in cards:
        link = card[1]
        if link not in by_link:
            order.append(link)
        by_link[link] = card
    cards = [by_link[l] for l in order][:cap]
    for name, link, date_text, desc, addr, tags in cards:
        full = link if link.startswith("http") else listing.rsplit("/", 1)[0] + link
        detail = get(session, full)
        if not detail:
            # One unreadable page is a reason to publish fewer rows, not to
            # fail the source: a 403 on one programme must not take the other
            # nine down, and the count below says how many were reached.
            report(f"detail unreadable: {full}", level="warn")
            continue
        opened += 1
        d_desc, d_addr, d_price = _detail_facts(detail)
        dates = _detail_dates(detail)
        if not dates:
            # No date on its own page means there is nothing to publish. The
            # card's own date is a fallback only for a one-off, which is the
            # common case here: a workshop's page states its single date.
            day = parse_day_month_year(date_text)
            if not day:
                report(f"no date on page, skipped: {name}", level="warn")
                continue
            dates = [(day, "")]
        for d, stamp in dates:
            when = d.isoformat()
            rows.append(make_row(
                cfg["id"], name, full,
                datetime_iso=combine(d, stamp).isoformat() if stamp else
                f"{when}T00:00:00",
                datetime_text=f"{d.strftime('%d %b %Y')}"
                              + (f" {stamp}" if stamp else ""),
                # The card's address is the venue with its street; the detail
                # page repeats it in prose, so the card is preferred where it
                # has one.
                address=addr or d_addr,
                location=(addr or d_addr).split(",")[0],
                price_text=d_price or ("Free" if "free" in (d_desc + desc).lower()
                                       else ""),
                description=desc or d_desc or name,
            ))
        report(f"{name}: {len(dates)} date(s)")

    if not rows:
        raise PartialFetch(
            f"ten library pages read and none produced a dated row -- the "
            f"detail markup has probably changed")
    report(f"{len(rows)} rows from {opened} of {len(cards)} listed programmes")
    return rows


if __name__ == "__main__":
    import sys

    from checks import check as _check

    failures = []

    def ck(label, actual, expected):
        return _check(label, actual, expected, failures)

    page = """<html><body><article>
      <a href="/Whats-On/Justice-of-the-Peace-Saturdays-Frankston-Library-Jul-2026">
      <h2 class="list-item-title">Justice of the Peace Saturdays</h2></a>
      <p class="clearfix"><span class="list-item-block-date">03 Oct 2026</span>
      <span class="list-item-block-desc">This service is free.</span></p>
      <p class="list-item-address">Frankston Library,&#160;60 Playne Street,&#160;Frankston&#160;3199</p>
      <p class="tagged-as-list"><span class="text">, Adult Event</span></p>
    </article></body></html>"""
    cards = list(_listing_cards(BeautifulSoup(page, "html.parser")))
    ck("a card yields one entry", len(cards), 1)
    ck("the card's title is the name", cards[0][0], "Justice of the Peace Saturdays")
    ck("the card's date is read", cards[0][2], "03 Oct 2026")
    ck("the nbsp in an address is a space",
       cards[0][4], "Frankston Library, 60 Playne Street, Frankston 3199")
    ck("the venue is the head of the address",
       cards[0][4].split(",")[0], "Frankston Library")

    detail = """<html><body>
      <div class="event-date">Next date: Saturday, 03 October 2026 |
        10:00 AM to 01:00 PM</div>
      <p>Saturday, 10 October 2026</p><p>Saturday, 17 October 2026</p>
      <p>Saturday, 24 October 2026</p><p>Saturday, 07 November 2026</p>
      <p>December 2026</p></body></html>"""
    dates = _detail_dates(detail)
    # Five, not four: the 07 November occurrence is in the body and belongs in
    # the run. The date earlier in this file was written before that line was
    # added and the count was never raised with it.
    ck("every published date is collected, not just the next one",
       len(dates), 5)
    ck("the run is in date order",
       [d.isoformat() for d, _ in dates],
       ["2026-10-03", "2026-10-10", "2026-10-17", "2026-10-24", "2026-11-07"])
    ck("the stated time is attached to each", {s for _, s in dates}, {"10:00 AM"})
    ck("a month with no day is not an occurrence",
       any(d.isoformat() == "2026-12-01" for d, _ in dates), False)
    ck("a single-date page still yields one row",
       len(_detail_dates("<p>Friday, 02 October 2026</p>")), 1)
    ck("a day with no stated time is still one row",
       [d.isoformat() for d, _ in _detail_dates("<p>Friday, 02 October 2026</p>")],
       ["2026-10-02"])

    two = """<html><body>
      <article><a href="/Whats-On/A"><h2 class="list-item-title">Board Games</h2></a>
      <span class="list-item-block-date">03 Oct 2026</span></article>
      <article><a href="/Whats-On/A"><h2 class="list-item-title">Board Games</h2></a>
      <span class="list-item-block-date">25 Oct 2026</span></article>
      <article><a href="/Whats-On/B"><h2 class="list-item-title">Other</h2></a>
      <span class="list-item-block-date">04 Oct 2026</span></article>
    </body></html>"""
    listed = list(_listing_cards(BeautifulSoup(two, "html.parser")))
    ck("a repeated card is not a second programme", len(listed), 3)
    seen, order = {}, []
    for c in listed:
        if c[1] not in seen:
            order.append(c[1])
        seen[c[1]] = c
    ck("de-duplicating by link keeps the first", order,
       ["/Whats-On/A", "/Whats-On/B"])
    ck("and the surviving card is the later, fuller one",
       seen["/Whats-On/A"][2], "25 Oct 2026")

    if failures:
        print(f"\nwebfetch_frankston_libraries: {len(failures)} case(s) FAILED")
        sys.exit(1)
    print("\nall Frankston-libraries reader cases as expected")