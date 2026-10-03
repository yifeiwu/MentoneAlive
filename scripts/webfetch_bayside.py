"""Bayside Council events (Drupal, ?page=N pagination)."""
import re
from datetime import datetime

from bs4 import BeautifulSoup

from webfetch_http import (MIN_CRAWL_DELAY, Pacer, PartialFetch, combine,
                           enrich_details, get, join_address, make_row,
                           parse_day_month_year, report)

# Bayside was paced by a `time.sleep(0.3)` at the bottom of the page loop, which
# the loop reaches only when a page yielded something new: the two `break`s above
# it -- page 0 legitimately empty, and a later page running dry -- skipped the
# pause entirely, so the request that discovered the listing was over cost
# nothing. `crawl_delay` was also validated for this source and then read by
# nobody. Pacing moves to immediately before the request, where every path
# through the loop passes it.
DEFAULT_CRAWL_DELAY = 0.3

# ---------------------------------------------------------------------------
# Bayside (Drupal, ?page=N)
# ---------------------------------------------------------------------------

def fetch_bayside(cfg, session=None, detail_cap=None):
    rows, seen_links = [], set()
    max_pages = cfg.get("max_pages", 12)
    pacer = Pacer(cfg.get("crawl_delay"), floor=MIN_CRAWL_DELAY,
                  default=DEFAULT_CRAWL_DELAY)
    for page in range(max_pages):
        url = cfg["url"] if page == 0 else f"{cfg['url']}?page={page}"
        if not pacer.take():
            raise PartialFetch(
                f"stopped at the run's {pacer.budget}-request budget after "
                f"page {page}", rows)
        html = get(session, url)
        if not html:
            # A page we could not load is a broken crawl, not an exhausted
            # listing. Returning a short list would silently replace a good
            # snapshot with a fraction of the real data.
            raise PartialFetch(f"page {page} failed to load", rows)
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select("li.listing-item")
        if not cards:
            # Only page 0 may legitimately be empty (site genuinely has no
            # events). A later page running dry means the markup changed.
            if page == 0:
                break
            raise PartialFetch(f"page {page} returned no listing items", rows)
        fresh = 0
        for card in cards:
            a = card.select_one('a[href^="/explore-bayside/events/"]')
            title_el = card.select_one("h3")
            if not a or not title_el:
                continue
            base = cfg.get("base", "https://www.bayside.vic.gov.au")
            link = base + a["href"]
            if link in seen_links:
                continue
            seen_links.add(link)
            fresh += 1
            name = title_el.get_text(strip=True)
            date_el = card.select_one(".listing-content-date")
            date_text = date_el.get_text(" ", strip=True) if date_el else ""
            price_el = card.select_one(".listing-content-price")
            price_text = price_el.get_text(" ", strip=True) if price_el else ""
            day = parse_day_month_year(date_text)
            rows.append(make_row(
                cfg["id"], name, link,
                datetime_iso=day.isoformat() if day else "",
                datetime_text=date_text,
                price_text=price_text,
                # `location` and `address` are seeded blank rather than
                # guessed, since a wrong venue is worse than a missing one; the
                # detail pass fills both.
                #
                # `description` is blank for the same reason, and not `name`.
                # The listing card states no prose, and every one of this
                # source's 143 rows was published with its own title in the
                # description column -- the page printed the name twice, the
                # search haystack counted it twice, and the classifier read the
                # title as the listing's description. `make_row` now drops a
                # description that restates the name, so the fetcher states the
                # fact and the row-shape owner enforces it.
                description="",
            ))
        report(f"page {page}: {fresh} new")
        if fresh == 0:
            break
    enrich_bayside_details(session, rows, detail_cap)
    return rows


def _label_content(soup, label):
    for item in soup.select(".event-venue-item"):
        lab = item.select_one(".event-venue-item-label")
        if lab and lab.get_text(strip=True).rstrip(":").lower() == label:
            content = item.select_one(".event-venue-item-content")
            if content:
                return content.get_text(" | ", strip=True)
    return ""


# Blocks that are not prose. The venue block sits between the title and the
# description on these pages, so "the first paragraph after the h1" has to be
# taken from outside them.
_NOT_PROSE = ("event-venue-item", "breadcrumb", "event-share", "script",
              "style", "nav", "header", "footer")


def _page_description(soup):
    """The listing's own prose: the first real paragraph after the title.

    Bayside's event pages carry no description class at all -- the text is an
    unlabelled `<p>` between the `<h1>` and the venue block, which is why this
    source published with `description=name` for all 143 of its rows and the
    page printed every event's name twice.

    The length floor is what keeps a stray one-line fragment out: a paragraph
    shorter than this is a button label or a byline, not the listing's
    description, and storing it would be worse than storing none.
    """
    h1 = soup.select_one("h1")
    if not h1:
        return ""
    for el in h1.find_all_next(["p", "div"]):
        if el.name == "div" and not el.select_one("p"):
            continue
        if el.name == "p":
            # Skip anything that is really a venue/label field wearing a <p>.
            if _inside_not_prose(el):
                continue
            text = el.get_text(" ", strip=True)
            if len(text) >= 40:
                return text[:400]
    return ""


def _inside_not_prose(el):
    """True when `el` sits inside one of the layout blocks, not in the prose.

    Written as a walk up the ancestors rather than a `class_=` callable: bs4
    passes a multi-valued class attribute to that callable as a list, so the
    shape of the argument depends on the element and the test has to cope with
    both. Reading the joined string here is the same rule with no such
    ambiguity.
    """
    for parent in el.parents:
        classes = " ".join(parent.get("class") or [])
        if any(name in classes for name in _NOT_PROSE):
            return True
        if parent.name in ("main", "body", "[document]"):
            return False
    return False


def enrich_bayside_details(session, rows, cap):
    """Fill each row's real date, venue, address and cost from its own page."""
    n = enrich_details(session, rows, cap, _apply_bayside_detail,
                       label="bayside")
    report(f"details enriched: {n}")


def _apply_bayside_detail(r, html):
    soup = BeautifulSoup(html, "html.parser")
    when = _label_content(soup, "when")
    tm = _label_content(soup, "time")
    loc = _label_content(soup, "location")
    cost = _label_content(soup, "cost") or _label_content(soup, "price")
    desc = _page_description(soup)
    day = None
    t = soup.select_one(".event-date-item time[datetime]")
    if t and t.get("datetime"):
        try:
            parsed = datetime.fromisoformat(
                t["datetime"].replace("Z", "+00:00"))
            day = datetime(parsed.year, parsed.month, parsed.day)
        except ValueError:
            day = None
    if day is None:
        day = parse_day_month_year(when or r["datetime_text"])
    if day:
        r["datetime_iso"] = combine(day, tm).isoformat()
        clean_when = re.sub(r"\s*\|\s*", " ", when).strip() if when else ""
        r["datetime_text"] = (clean_when + (f" {tm}" if tm else "")).strip()
    if loc:
        # _label_content joins each .event-venue-item's children with " | ",
        # so `loc` is already one string of pipe-separated parts. Split it
        # once here; re-splitting a pre-joined field picks up substrings.
        # `join_address` then drops the country, the blank separators the
        # markup leaves between fields (which is where the published
        # "14 Willis St,, Hampton" came from), and the repeated suburb Bayside
        # prints in its own Location block -- ten rows were published as
        # "84 Reserve Road, Beaumaris, Beaumaris, Victoria 3193".
        address = join_address(loc.split("|"))
        if address:
            r["address"] = address
            # The first surviving segment is the venue or the street, which is
            # what venue_head() and the location column both want.
            r["location"] = address.split(",")[0]
    if cost and not r["price_text"]:
        r["price_text"] = cost
    # Only overwrites a blank: a listing that states no prose keeps none, which
    # is what `make_row` now emits, and the row keeps an empty description
    # rather than regaining its own title.
    if desc and not r.get("description"):
        r["description"] = desc
