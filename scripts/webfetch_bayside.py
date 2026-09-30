"""Bayside Council events (Drupal, ?page=N pagination)."""
import re
import time
from datetime import datetime

from bs4 import BeautifulSoup

from webfetch_http import (PartialFetch, combine, enrich_details, get,
                           make_row, parse_day_month_year, report)

# ---------------------------------------------------------------------------
# Bayside (Drupal, ?page=N)
# ---------------------------------------------------------------------------

def fetch_bayside(cfg, session=None, detail_cap=None):
    rows, seen_links = [], set()
    max_pages = cfg.get("max_pages", 12)
    for page in range(max_pages):
        url = cfg["url"] if page == 0 else f"{cfg['url']}?page={page}"
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
                # The listing card states no venue; the detail pass fills both
                # location and address. Seeded blank rather than guessed, since
                # a wrong venue is worse than a missing one.
                description=name,
            ))
        report(f"page {page}: {fresh} new")
        if fresh == 0:
            break
        time.sleep(0.3)
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
        # Each part keeps its own trailing comma from the venue block, so
        # joining with ", " produced "14 Willis St,, Hampton" and
        # "Bayley Arts Gallery,, 1 Avoca Street". Two published rows.
        parts = [p.strip().strip(",").strip() for p in loc.split("|")
                 if p.strip() and p.strip().lower() != "australia"]
        if parts:
            r["location"] = parts[0]
            r["address"] = ", ".join(parts)
    if cost and not r["price_text"]:
        r["price_text"] = cost
