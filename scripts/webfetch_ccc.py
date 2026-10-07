"""Cheltenham Community Centre (Weebly term classes + Humanitix)."""
import re
from datetime import datetime, timedelta

from bs4 import BeautifulSoup, NavigableString

from webfetch_http import (DETAIL_MIN_SUCCESS_RATIO, MIN_CRAWL_DELAY, Pacer,
                           PartialFetch, _hhmm, get, make_row, report)

# ---------------------------------------------------------------------------
# Cheltenham Community Centre (Weebly term classes + Humanitix bookings)
# ---------------------------------------------------------------------------

# The interval this source was actually running at, kept so `crawl_delay` can
# override it and nothing else can change it by accident.
DEFAULT_CRAWL_DELAY = 0.2

CCC_LABELS = ("Term:", "When:", "Time:", "Where:", "Cost:",
              "Instructor:", "Facilitator:")

# The hall is only chosen on a word-bounded mention: a plain `"hall" in text`
# test also matches "challenging" and "shall", which silently mis-filed
# Community Centre classes at the Hall. The venue is the singular "Cheltenham
# Hall", so the token is matched in the singular too -- a plural "halls" is
# ambiguous ("Halls Cafe") and is not on its own evidence for the venue.
CCC_HALL_RE = re.compile(r"\bhall\b", re.I)


def _ccc_clean_cost(c):
    """A cost field, with the page's own furniture removed.

    Weebly section spans run to the next heading, so a cost that is the last
    labelled field in its section picks up the trailing call to action:
    "Physiotherapy fees apply FIND OUT MORE BUTTON Find Out More". The button
    label is page furniture, not a price, and it was being published as one --
    which also made the price column the widest thing on the page, since the
    cell was nowrap and that string is 59 characters.
    """
    c = re.sub(r"\bBook here\b.*$", "", c or "")
    # The button, its label repeated by Weebly for screen readers, and the
    # "View & Book" style links that follow the same pattern.
    c = re.sub(r"\bFIND OUT MORE BUTTON\b.*$", "", c, flags=re.I)
    c = re.sub(r"\b(?:View\s*&\s*Book|Read more|More info|Enrol now|"
               r"Book\s*now)\b.*$", "", c, flags=re.I)
    c = re.sub(r"=+", "", c)
    c = re.sub(r"\s+", " ", c).strip(" -|;,.")
    if len(c) > 60:
        c = c[:60].rsplit(" ", 1)[0]
    return c


def _ccc_field(text, label, stop=CCC_LABELS):
    others = "|".join(re.escape(s) for s in stop if s != label)
    m = re.search(label.rstrip(":") + r":\s*(.+?)(?:" + others + r"|$)",
                  text, re.S | re.I)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1)).strip(" -|;")[:200]


CCC_JUNK_TITLES = ("how do i enrol", "refund policy", "venuesactivities",
                   "course enquiry", "you can enrol", "to enrol in any",
                   "if withdrawal", "what our participants", "term:",
                   "quick facts", "loved by", "thank you", "sponsor",
                   "hear from", "testimonial", "what our")


def _ccc_clean_title(t):
    t = re.sub(r"[\u200b\xa0]+", " ", t or "")
    t = t.replace("ZumbaAr Gold", "Zumba Gold")
    t = re.sub(r"\ufffd+", "", t)
    return re.sub(r"\s+", " ", t).strip()[:80]


def _ccc_section_starts(main):
    """Ordered (element, kind) section starts: h2s + labeled paragraphs."""
    starts = []
    for h in main.select("h1, h2, h3"):
        t = _ccc_clean_title(h.get_text(" ", strip=True))
        if t and len(t) > 2:
            starts.append((h, "h", t))
    for block in main.select("div.paragraph"):
        text = block.get_text(" ", strip=True)
        if len(text) < 40:
            continue
        # Matched case-insensitively, and against the module's own label set
        # (CCC_LABELS) rather than a hand-copied subset. Both mattered: a
        # paragraph labelled "time:" in lower case was skipped, and one whose
        # only labels were "Time:"/"Instructor:" was skipped too. Either way
        # the block was not a section start, so its content was absorbed into
        # the *previous* class -- the next class's time published as this
        # one's, and its title as this one's Cost.
        if not re.search("|".join(rf"{lbl.strip(':')}\s*:" for lbl in CCC_LABELS),
                         text, re.I):
            continue
        title = ""
        strong = block.select_one("strong, b, font")
        if strong:
            cand = strong.get_text(strip=True)
            if cand and len(cand) < 60 and text.startswith(cand[:12]):
                title = cand
        if not title:
            title = text.split(".")[0]
        title = _ccc_clean_title(title)
        if len(title) < 3:
            continue
        starts.append((block, "labels", title))
    # BeautifulSoup has no `sourceline` (lxml only), so sort by document
    # order via a descendant index instead of a key that is always 0.
    order = {id(el): i for i, el in enumerate(main.descendants)}
    starts.sort(key=lambda s: order.get(id(s[0]), 0))
    return starts


def _ccc_section_span(start_el, next_el, max_elements=4000):
    """Text + links between start_el (inclusive) and next_el (exclusive).

    The walk is capped: for the final section on a page there is no next_el,
    so next_elements would otherwise run on through <script> and the footer,
    pulling unrelated text into the description.
    """
    parts, links, seen_links = [], [], set()
    for count, el in enumerate(start_el.next_elements):
        if count >= max_elements:
            break
        if el is next_el:
            break
        if isinstance(el, NavigableString):
            if getattr(el.parent, "name", "") not in ("script", "style"):
                t = str(el).strip()
                if t:
                    parts.append(t)
        elif getattr(el, "name", None) == "a" and el.get("href"):
            href = el["href"]
            if href not in seen_links:
                seen_links.add(href)
                links.append(href)
    return " ".join(parts), links


def _ccc_trunc_start(text, limit=120):
    """Trim a long address from the *end*, keeping the beginning.

    Slicing from the start ([:120]) keeps the venue name and street, which is
    the part that identifies the venue, and drops the tail. The old
    `clean[-limit:]` kept the *end* -- postcode and suburb -- and discarded the
    street name, which is the exact failure this function exists to prevent.
    """
    clean = re.sub(r"\s+", " ", text or "").strip()
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip(" ,") + "..."


WEEKDAY_RE = (r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)s?"
              r"(?:\s*(?:to|-|–)\s*(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)s?)?"
              r"|Every second Saturday|First Tuesday of every month|daily|weekly")
TIME_RE = (r"\d{1,2}(?::\d{2})?\s*(?:am|pm)\s*(?:[-–]|to)\s*"
           r"\d{1,2}(?::\d{2})?\s*(?:am|pm)")


_CCC_TIME_RE = re.compile(
    r"\b(\d{1,2})[:.](\d{2})\s*([ap])\.?\s*m\.?"
    r"|\b(\d{1,2})\s*([ap])\.?\s*m\.?"
    r"|\b(\d{1,2})\s*(?:noon|midday)\b", re.I)


def _ccc_first_time(text):
    """'HH:MM' for the first time stated in `text`, or '' if it states none.

    The regex here reads a form parse_time() does not -- '12noon' and
    '11 midday' are both in use in this source's schedules -- so this stays a
    separate reader. Only the 12-hour conversion is shared, through _hhmm, which
    also clamps: this regex will happily capture '99pm', and an unclamped
    '111:00' reaches datetime.replace(hour=111) and takes the whole source down.
    """
    m = _CCC_TIME_RE.search(text or "")
    if not m:
        return ""
    if m.group(1):
        hour, minute, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    elif m.group(4):
        hour, minute, ap = int(m.group(4)), 0, m.group(5).lower()
    else:
        return "12:00"
    # The regex captures a single meridiem letter ("9p"), which _hhmm now takes
    # as it stands -- it used to want the whole word, so this appended an "m".
    hour, minute = _hhmm(hour, minute, ap)
    return f"{hour:02d}:{minute:02d}"


def _ccc_free_signals(text):
    bits = []
    m = re.search(WEEKDAY_RE, text, re.I)
    if m:
        bits.append(m.group(0).strip())
    for m in re.finditer(TIME_RE, text, re.I):
        bits.append(m.group(0).strip())
        if len(bits) > 3:
            break
    m = re.search(r"Term\s*\d[^.)]{0,60}", text, re.I)
    if m:
        bits.append(re.sub(r"\s+", " ", m.group(0)).strip())
    m = re.search(r"(Free|\$[\d,]+(?:\.\d+)?[^.\n]{0,50})", text, re.I)
    cost = re.sub(r"\s+", " ", m.group(0)).strip() if m else ""
    return bits, cost


def fetch_ccc(cfg, session=None, detail_cap=None):
    rows, seen = [], set()
    # Paced before each request rather than after each page, and shared with the
    # booking-page pass so one pacer covers every request this source makes. The
    # `time.sleep(0.2)` this replaces sat at the bottom of both loops, which put
    # the rate limit on the wrong branch: `enrich_humanitix` has six `continue`s
    # between its request and its sleep, and the first of them is "the page did
    # not load" -- so a booking page that failed cost no pause at all, and the
    # next request went out immediately behind it. `crawl_delay` was validated
    # for this source and read by nobody.
    pacer = Pacer(cfg.get("crawl_delay"), floor=MIN_CRAWL_DELAY,
                  default=DEFAULT_CRAWL_DELAY)
    for page_url in cfg.get("pages", [cfg.get("url", "")]):
        if not page_url:
            continue
        if not pacer.take():
            raise PartialFetch(
                f"stopped at the run's {pacer.budget}-request budget before "
                f"{page_url}", rows)
        html = get(session, page_url)
        if not html:
            # One unreachable program page means a partial term listing, which
            # would silently replace a good snapshot with less data.
            raise PartialFetch(f"page {page_url} failed to load", rows)
        soup = BeautifulSoup(html, "html.parser")
        main = (soup.select_one("#wsite-content")
                or soup.select_one("#main-wrap") or soup)
        starts = _ccc_section_starts(main)
        n = 0
        # Labeled blocks first (richer fields); h2 sections fill gaps.
        for pass_kind in ("labels", "h"):
            for i, (el, kind, title) in enumerate(starts):
                if kind != pass_kind:
                    continue
                # Labeled blocks without their own bold lead inherit the
                # nearest preceding h2 title (Weebly puts h2 outside the div).
                if kind == "labels":
                    block_text = el.get_text(" ", strip=True)
                    strong = el.select_one("strong, b, font")
                    lead = (strong.get_text(strip=True) if strong else "")
                    # A bare label ("Term:", "T") is not a title.
                    has_lead = bool(lead and len(lead) < 60 and len(lead) > 3
                                    and not lead.endswith(":")
                                    and block_text.startswith(lead[:12]))
                    if not has_lead:
                        for _el2, _k2, _t2 in reversed(starts[:i]):
                            if _k2 == "h" and not _t2.lower().startswith(
                                    CCC_JUNK_TITLES):
                                title = _t2
                                break
                if title.lower().startswith(CCC_JUNK_TITLES):
                    continue
                title = _ccc_clean_title(title)
                if len(title) < 3 or re.match(r"^\w+ \d{1,2} \w+ \d{4}$",
                                              title):
                    continue  # pure-date heading, not an activity
                next_el = starts[i + 1][0] if i + 1 < len(starts) else None
                span_text, span_links = _ccc_section_span(el, next_el)
                span_text = re.sub(r"[\u200b\xa0]+", " ", span_text)
                span_text = re.sub(r"\s+", " ", span_text)
                hum = ""
                for href in span_links:
                    if "humanitix.com" in href:
                        hum = href.split("?")[0]
                        break
                if kind == "labels":
                    term = _ccc_field(span_text, "Term:")
                    when = _ccc_field(span_text, "When:")
                    tme = _ccc_field(span_text, "Time:")
                    where = _ccc_field(span_text, "Where:")
                    cost = _ccc_field(span_text, "Cost:")
                    cost = _ccc_clean_cost(cost)
                    instr = (_ccc_field(span_text, "Instructor:")
                             or _ccc_field(span_text, "Facilitator:"))
                    schedule = ". ".join(x for x in (term, when, tme) if x)
                    m_lab = re.search(
                        r"(Term|When|Time|Where|Cost|Instructor|Facilitator)\s*:",
                        span_text)
                    blurb = (span_text[:m_lab.start()] if m_lab else span_text)
                    blurb = re.sub(r"\s+", " ",
                                   blurb.replace(title, "", 1)).strip()[:300]
                    desc = ". ".join(x for x in
                                     (blurb, schedule,
                                      f"Instructor: {instr}" if instr else "")
                                     if x)
                else:
                    bits, cost = _ccc_free_signals(span_text)
                    cost = _ccc_clean_cost(cost)
                    generic = bool(re.search(
                        r"activit|class|course|program|guide|polic|terms|about|contact|galler|enrol|refund|volunteer|venue",
                        title, re.I))
                    if len(bits) < 1 and "free" not in span_text.lower() \
                            and not (len(span_text) > 150 and not generic):
                        continue
                    where, instr, term = "", "", ""
                    schedule = ". ".join(bits[:4])
                    blurb = span_text.replace(title, "", 1).strip()[:300]
                    desc = ". ".join(x for x in (blurb, schedule) if x)
                if not schedule and not cost:
                    continue
                # Word-bounded: a bare substring test matched "challenging",
                # "shall" and "shallot", which then filed a Cheltenham
                # Community Centre class at the Hall. Only a real mention of
                # the hall selects the hall venue.
                # Both venues come from `venues:` in sources.yaml rather than
                # from constants here, which is what D21 says: a source config
                # holds the venue, never a guess. They were the clearest breach
                # of that in the tree -- four addresses in a fetcher module, with
                # no way to see or change them without reading Python.
                venues = cfg.get("venues") or {}
                default = venues.get("default") or {}
                venue = default.get("name") or ""
                addr = default.get("address") or ""
                if CCC_HALL_RE.search(span_text):
                    hall = venues.get("hall") or {}
                    venue = hall.get("name") or venue
                    addr = hall.get("address") or addr
                if not venue or not addr:
                    raise PartialFetch(
                        f"no venues configured for {cfg['id']}: every CCC row "
                        f"is filed at a venue, and guessing one publishes a "
                        f"wrong address silently")
                m_addr = re.search(r"(.+?VIC\s*\d{4})", where if kind == "labels"
                                  else span_text)
                if m_addr:
                    addr = _ccc_trunc_start(m_addr.group(1))
                dedup_key = (title.lower(), venue.lower())
                if dedup_key in seen:
                    # Prefer the Humanitix-linked variant for enrichment.
                    for r in rows:
                        if (r["name"].lower(), r["location"].lower()) == dedup_key \
                                and hum and "humanitix.com" not in r["source"]:
                            r["source"] = hum
                    continue
                seen.add(dedup_key)
                rows.append(make_row(
                    cfg["id"], title, hum or page_url,
                    # A Weebly class states its pattern in prose ("Wednesdays.
                    # 9am - 12pm"), never a per-occurrence date, so this stays
                    # blank and recurrence.py derives the dates.
                    datetime_text=schedule,
                    location=venue,
                    address=addr,
                    price_text=cost,
                    # No `or title`: a listing that states no prose gets no
                    # description. Falling back to the heading wrote the name
                    # into the description column, which the page then printed
                    # twice. `make_row` drops that case too, so this is belt
                    # and braces -- but the fetcher should not be asking for it.
                    description=desc[:400],
                ))
                n += 1
        report(f"{page_url.split('/')[-1]}: {n} activities")
    rows.extend(enrich_humanitix(session, rows, detail_cap, pacer=pacer))
    return rows


# One published row per weekly session, so a 10-week term does not become a
# single event. Matches MAX_OCCURRENCES in recurrence.py, which caps the
# inferred expansions this replaces.
CCC_TERM_MAX_ROWS = 12


def _ccc_weekly_term(start, end, cap=CCC_TERM_MAX_ROWS):
    """Weekly start datetimes from `start` to `end` inclusive, same weekday.

    A Humanitix term event carries one JSON-LD block whose startDate is the
    first session and whose endDate is the end of the last. Publishing only the
    startDate turned an 11-week class into a single row, so the other ten
    sessions a member could attend were simply absent from the calendar.

    Returns a single-element list when the range is under two weeks, so a
    genuine one-off is untouched.
    """
    if end - start < timedelta(days=7):
        return [start]
    out, cur = [], start
    while cur <= end and len(out) < cap:
        out.append(cur)
        cur += timedelta(days=7)
    return out


def enrich_humanitix(session, rows, cap, pacer=None):
    """Attach Humanitix dates, and expand a term range into weekly rows.

    Returns the extra rows a multi-week term expands into; the caller appends
    them. `rows` is mutated in place as before.

    The fetch loop is not the shared enrich_details() one, because it is not a
    detail pass over every row: it skips every row that is not a Humanitix
    link, and it can return *new* rows rather than only filling existing ones.
    It still shares the failure contract -- a cap's worth of attempts that
    mostly fail to load is a broken crawl, not a source with no booking pages,
    and must not replace a good snapshot.
    """
    import json as _json
    n = 0
    extra = []
    attempted = 0
    for r in rows:
        if "humanitix.com" not in (r.get("source") or ""):
            continue
        if cap is not None and n >= cap:
            continue
        attempted += 1
        # The pause belongs here rather than at the bottom of the loop: six
        # `continue`s stand between this request and where the sleep used to be,
        # and the first of them is a page that failed to load.
        if pacer is not None and not pacer.take():
            break
        html = get(session, r["source"])
        if not html:
            continue
        m = re.search(r'<script type="application/ld\+json">(.*?)</script>',
                      html, re.S)
        if not m:
            continue
        try:
            data = _json.loads(m.group(1))
        except Exception:
            continue
        if isinstance(data, list):
            data = next((d for d in data
                         if isinstance(d, dict) and d.get("@type") == "Event"),
                        {})
        if not isinstance(data, dict):
            continue
        # Count only rows we actually parsed, so malformed JSON-LD cannot
        # silently consume the detail_cap budget.
        n += 1
        try:
            start = datetime.fromisoformat(
                str(data.get("startDate", "")).replace("Z", "+00:00"))
            start = start.replace(tzinfo=None)
        except (ValueError, TypeError):
            n -= 1
            continue
        end = start
        try:
            end = datetime.fromisoformat(
                str(data.get("endDate", "")).replace("Z", "+00:00"))
            end = end.replace(tzinfo=None)
        except (ValueError, TypeError):
            pass
        if end < start:
            end = start
        # JSON-LD allows location to be an object, an array of objects, or a
        # string. Normalise to a dict before subscripting.
        loc = data.get("location")
        if isinstance(loc, list):
            loc = next((x for x in loc if isinstance(x, dict)), {})
        if not isinstance(loc, dict):
            loc = {}
        if loc.get("name"):
            r["location"] = str(loc["name"]).strip()
        addr = loc.get("address")
        if isinstance(addr, list):
            addr = next((x for x in addr if isinstance(x, dict)), {})
        if isinstance(addr, dict) and addr.get("streetAddress"):
            r["address"] = str(addr["streetAddress"]).strip()

        # Re-clean the cost here as well. A row that matched a heading rather
        # than a labelled block gets its fields from the heading's own span, so
        # the same button label reaches price_text by a second route, and
        # re-running the section-start fix alone left it in place.
        if r.get("price_text"):
            r["price_text"] = _ccc_clean_cost(r["price_text"])

        sessions = _ccc_weekly_term(start, end)
        # Some listings give a startDate with no time component ("T00:00:00"),
        # which would publish a 9:30am class as midnight -- and midnight is
        # this pipeline's marker for "date known, time not stated", so the page
        # would render it as all day. Take the time from the listing's own
        # stated schedule instead of inventing or dropping one.
        stated = _ccc_first_time(f"{r.get('datetime_text', '')} "
                                 f"{r.get('description', '')}")
        for i, when in enumerate(sessions):
            row = r if i == 0 else dict(r)
            if stated and (when.hour, when.minute) == (0, 0):
                hh, mm = (int(x) for x in stated.split(":"))
                when = when.replace(hour=hh, minute=mm)
            row["datetime_iso"] = when.isoformat()
            if i:
                extra.append(row)
    report(f"humanitix enriched: {n}"
           + (f" (+{len(extra)} term rows)" if extra else ""))
    if attempted and n / attempted < DETAIL_MIN_SUCCESS_RATIO:
        raise PartialFetch(
            f"only {n}/{attempted} humanitix booking pages parsed for ccc -- "
            f"the listing is intact but its booking pages are not", extra)
    return extra


if __name__ == "__main__":
    import sys

    from checks import check as _check

    failures = []

    def ck(label, actual, expected):
        return _check(label, actual, expected, failures)

    # The cost reader is what stopped "Physiotherapy fees apply FIND OUT MORE
    # BUTTON Find Out More" being published as a price, so each kind of trailing
    # furniture the Weebly markup leaves behind gets its own case.
    ck("a button label is not a price",
       _ccc_clean_cost("Physiotherapy fees apply FIND OUT MORE BUTTON "
                       "Find Out More"),
       "Physiotherapy fees apply")
    ck("a book-now link is not a price",
       _ccc_clean_cost("$15 Book here"), "$15")
    ck("a view-and-book link is not a price",
       _ccc_clean_cost("Gold coin donation Read more"), "Gold coin donation")
    ck("an enrol link is not a price",
       _ccc_clean_cost("$5 per session Enrol now"), "$5 per session")
    ck("a plain cost is left alone", _ccc_clean_cost("$5 per session"),
       "$5 per session")
    ck("an empty cost is still empty", _ccc_clean_cost(""), "")
    ck("a cost of nothing but furniture reads empty",
       _ccc_clean_cost("FIND OUT MORE BUTTON Find Out More"), "")
    ck("a long cost is trimmed on a word boundary",
       ("and then " * 20).startswith(_ccc_clean_cost("and then " * 20) + " ")
       and len(_ccc_clean_cost("and then " * 20)) <= 60, True)

    # _ccc_field must stop at the NEXT label rather than swallowing the rest of
    # the paragraph, which is how one class's time used to publish as another's.
    text = ("Time: Thursdays 9:30am - 10:30am\n"
            "Instructor: Larisa\n"
            "Cost: FREE")
    ck("a labelled field stops at the next label", _ccc_field(text, "Time"),
       "Thursdays 9:30am - 10:30am")
    ck("the last label runs to the end", _ccc_field(text, "Cost"), "FREE")
    ck("a missing field reads empty", _ccc_field(text, "Venue"), "")
    ck("a field is matched case-insensitively",
       _ccc_field("time: 9:30am", "Time"), "9:30am")

    # The time regex reads forms the shared parse_time() does not: '12noon' and
    # '11 midday'. It also has to hand a bare meridiem letter to _hhmm, which is
    # what the old 'ap' + 'm' workaround used to build.
    for text_in, want in [
            ("Thursdays 9.30a.m.", "09:30"),
            ("Starts at 12noon", "12:00"),
            ("7pm", "19:00"),
            ("9 a.m. to 10:30am", "09:00"),
            ("Saturdays, 1.00 pm", "13:00"),
            ("no time stated", ""),
    ]:
        ck("first time of %r is %s" % (text_in, want or "(none)"),
           _ccc_first_time(text_in), want)

    # "11 midday" and "11 noon" both publish 12:00, and the hour the regex
    # captures for them (group 6) is discarded rather than read. That is the
    # defensible answer -- midday is noon is 12:00 whatever number precedes it
    # -- but it is a trap for the next reader, so it is pinned here. See D50.
    ck("a stated hour before midday does not become the start time",
       _ccc_first_time("11 midday"), "12:00")
    ck("12noon reads as midday", _ccc_first_time("12 noon"), "12:00")

    # _hhmm clamps, and this is the case that reason exists: an unclamped
    # 111:00 reaches datetime.replace(hour=111) and takes the source down.
    ck("an impossible hour is clamped into range, not published",
       _ccc_first_time("99pm"), "23:00")
    ck("a clamped time is a real datetime hour",
       0 <= datetime.strptime(_ccc_first_time("99pm"), "%H:%M").hour <= 23,
       True)
    ck("a title drops zero-width and replacement characters",
       _ccc_clean_title("Chair\u200b Yoga\ufffd"), "Chair Yoga")
    ck("the ZumbaAr artefact is repaired",
       _ccc_clean_title("ZumbaAr Gold"), "Zumba Gold")

    # A long address keeps its beginning: the venue name and street identify it,
    # and slicing from the end kept the postcode while discarding the street.
    long_addr = ("Cheltenham Community Centre, 8 Chesterville Road, "
                 + "somewhere " * 12 + "Cheltenham VIC 3192")
    ck("a truncated address keeps the venue and street",
       _ccc_trunc_start(long_addr).startswith(
           "Cheltenham Community Centre, 8 Chesterville Road"), True)
    ck("a truncated address is marked as truncated",
       _ccc_trunc_start(long_addr).endswith("..."), True)
    ck("a short address is untouched",
       _ccc_trunc_start("8 Chesterville Road, Cheltenham VIC 3192"),
       "8 Chesterville Road, Cheltenham VIC 3192")

    # Section starts: a labelled paragraph is a start only if it carries one of
    # the module's own labels. Missing this made a class's content get absorbed
    # into the PREVIOUS class, so the next class's time published as this one's.
    labelled = BeautifulSoup(
        '<div id="wsite-content">'
        '<h2 class="heading">Chair Yoga</h2>'
        '<div class="paragraph"><p><strong>Zumba Gold</strong> '
        'Time: Tuesdays 10am. Instructor: Sam. Cost: FREE</p></div>'
        '</div>', "html.parser").select_one("#wsite-content")
    starts = _ccc_section_starts(labelled)
    ck("a heading and a labelled paragraph are both starts", len(starts), 2)
    ck("the starts are in document order",
       [k for _, k, _t in starts], ["h", "labels"])
    ck("a labelled paragraph takes its bolded name as the title",
       starts[1][2], "Zumba Gold")

    unlabelled = BeautifulSoup(
        '<div id="wsite-content"><h2>Chair Yoga</h2>'
        '<div class="paragraph"><p>Some prose about the class that runs on '
        'for long enough to pass the length floor here.</p></div>'
        '</div>', "html.parser").select_one("#wsite-content")
    ck("an unlabelled paragraph is not a section start",
       len(_ccc_section_starts(unlabelled)), 1)

    lower = BeautifulSoup(
        '<div id="wsite-content">'
        '<div class="paragraph"><p><strong>Morning Flow</strong> '
        'time: Mondays 7am. instructor: Sam. cost: FREE</p></div>'
        '</div>', "html.parser").select_one("#wsite-content")
    ck("a lower-case label still starts a section",
       len(_ccc_section_starts(lower)), 1)

    # _ccc_section_span must not run past the next section start: for the LAST
    # section there is no next_el, and an uncapped walk ran on through <script>
    # and the footer, pulling unrelated text into the description.
    body = BeautifulSoup(
        '<div id="wsite-content"><h2>Chair Yoga</h2><p>alpha text</p>'
        '<h2>Zumba Gold</h2><p>beta text</p></div>', "html.parser")
    main = body.select_one("#wsite-content")
    secs = _ccc_section_starts(main)
    text_a, _links = _ccc_section_span(secs[0][0], secs[1][0])
    ck("a section stops at the next heading", "beta" in text_a, False)
    ck("a section keeps its own text", "alpha" in text_a, True)
    _last, links = _ccc_section_span(secs[1][0], None, max_elements=2)
    ck("the walk is capped when there is no next section",
       len(links), 0)

    # The term expansion: publishing only the startDate turned an 11-week class
    # into a single row, so the other ten sessions were simply absent.
    start = datetime(2026, 7, 16, 9, 30)
    end = datetime(2026, 9, 17, 10, 30)
    term = _ccc_weekly_term(start, end)
    ck("a term is expanded to one row per week", len(term), 10)
    ck("the term starts on the stated date", term[0], start)
    ck("the term ends on the stated date", term[-1], datetime(2026, 9, 17, 9, 30))
    ck("every term row is the same weekday",
       {t.weekday() for t in term}, {start.weekday()})
    ck("a one-off is not expanded",
       _ccc_weekly_term(start, start + timedelta(days=2)), [start])
    ck("the cap bounds the expansion",
       len(_ccc_weekly_term(start, start + timedelta(days=400), cap=5)), 5)

    # The free-signals reader feeds the cost/schedule columns on the listing.
    bits, cost = _ccc_free_signals(
        "Thursdays 9:30am - 10:30am. Term 4 runs for 10 weeks. Cost: FREE")
    ck("the weekday is captured as a schedule signal",
       any("Thursday" in b for b in bits), True)
    ck("the term is captured", any("Term 4" in b for b in bits), True)
    ck("a free cost is captured", cost.upper().startswith("FREE"), True)
    _b2, cost2 = _ccc_free_signals("Cost: $15 per session")
    ck("a priced cost is captured with its amount", "$15" in cost2, True)
    _b3, cost3 = _ccc_free_signals("Come along any time")
    ck("no stated cost reads empty", cost3, "")

    if failures:
        print("\n%d failure(s)" % len(failures))
        sys.exit(1)
    print("\nwebfetch_ccc: all checks passed")
