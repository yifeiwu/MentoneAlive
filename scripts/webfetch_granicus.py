"""Kingston Council + Kingston Arts (Granicus listings, page 1)."""
import re
import time
from datetime import datetime

from bs4 import BeautifulSoup

from webfetch_http import combine, get, parse_day_month_year

# ---------------------------------------------------------------------------
# Granicus (Kingston Council + Kingston Arts, page 1; pager is JS postback)
# ---------------------------------------------------------------------------

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
    return rows


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
        text = main.get_text("\n", strip=True)
        m = re.search(r"([A-Za-z0-9'\-. ]+?),\s*([A-Za-z ]+?),\s*VIC\s*(\d{4})", text)
        if m:
            r["address"] = f"{m.group(1).strip()}, {m.group(2).strip()}, VIC {m.group(3)}"
            if not r["location"] or r["location"] in ("Greater Dandenong",):
                street = m.group(1).strip()
                r["location"] = street if len(street) <= 60 \
                    else street[:57] + "..."
        time.sleep(0.2)
    print(f"  granicus details enriched: {n}")
