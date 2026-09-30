"""Kingston Seniors Festival (annual PDF event guide)."""
import io
import re
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from webfetch_http import (PartialFetch, fetch_bytes, line_range_starts,
                           make_row, month_number, report)

# ---------------------------------------------------------------------------
# Kingston Seniors Festival (annual PDF event guide)
# ---------------------------------------------------------------------------

# The guide prints September, October and November only (SENIORS_DAYLIST_RE).
# Month *names* resolve through webfetch_http.month_number, the pipeline's one
# owner, so a guide that adds a month does not need a second table here.
SENIORS_CAT_RE = re.compile(
    r"^(Arts, Culture & Creativity|Body & Soul|Gather & Socialise|"
    r"Learning & Technology)(\s+(Arts, Culture & Creativity|Body & Soul|"
    r"Gather & Socialise|Learning & Technology))?$")
SENIORS_CONTACT_RE = re.compile(
    r"@|www\.|https?://|^\d[\d\s()+.-]{6,}$|^(Contact|Bookings are essential|"
    r"For event inquiries)$")
# Both ends accept an optional minutes field, and the meridiem is optional on
# the end so "10:30-11:30am" parses. Requiring an explicit one on both ends
# missed the shared form a printed guide actually uses, and the unparsed time
# then fell through to whichever time the nearest line mentioned -- including
# a neighbouring card's -- or to midnight. The pattern and the 12-hour
# conversion now live in webfetch_http (TIME_RANGE_RE, range_start_time), so
# this guide and the listing sources cannot disagree about what a time means.
# How far from a date line a time may be and still belong to the same event;
# line_range_starts() takes this as its window.
SENIORS_TIME_WINDOW_LINES = 6
SENIORS_DAYLIST_RE = re.compile(
    r"(\d{1,2}(?:\s*,\s*\d{1,2})*)\s+(September|October|November)\b", re.I)
SENIORS_STOP_RE = ("Bookings are essential", "For event inquiries", "Contact")
SENIORS_VENUE_END_RE = re.compile(
    r"(Centre|Center|Club|House|Hall|Hub|Librar\w+|Park|Gardens|Reserve|"
    r"Theatre|Hotel|Hospital|Church|School|Courts|Office)\W*$", re.I)
# Suburb names that end a title line during the bottom-up split.
SENIORS_PLACE_STOP = {"cheltenham", "kingston", "mentone"}

def _seniors_footer_re(year):
    return re.compile(r"^\d+\s*\|\s*" + str(year) + r" Seniors Festival|" + str(year) + r" Seniors Festival.*\|\s*\d+$")

SENIORS_VENUE_WORDS = {"centre", "center", "community", "club", "house",
                       "hall", "hub", "service", "gardens", "library",
                       "libraries", "neighbourhood", "neighborhood"}

# A line that begins a street address rather than continuing a venue name.
# Used to tell "Aspendale Gardens" / "Community Service" (one name split over
# two lines) from "Chelsea Library" / "12 Stanley Avenue" (name then street).
_SENIORS_STREET_RE = re.compile(
    r"^\s*(?:unit\s+\w+[\s,]*|corner\b|cnr\b|opposite\b|\d+\s*[A-Z-]|"
    r"\d+\s*$|[A-Z]{1,2}\s*$)", re.I)
SENIORS_ORG_END_RE = re.compile(
    r"(Centre|Center|Community|Club|Inc\.?|Group|Association|Choir|Council|"
    r"Australia|Ears|AccessCare|Hub|House|Hall|Service|Librar\w+|Arts|Theatre|"
    r"Region|Orchestra|Salon|Network|Project|Connections|Fellas)\W*$", re.I)
# Deliberately a strict SUBSET of SENIORS_ORG_END_RE, not a duplicate of it,
# and the two are not interchangeable. _seniors_title() uses this one only
# behind `acc_len < 12`, where it means "this line may still be part of a
# wrapped title, keep going"; SENIORS_ORG_END_RE alone at that point means
# "this is an organisation, the title ends here". Folding the narrow set into
# the wide one -- or deleting it as redundant -- makes a short partial title
# followed by "Community Library" or "Fine Arts" break instead of extending.
SENIORS_GENERIC_END_RE = re.compile(
    r"(Centre|Community|Club|Group|House|Hall|Hub|Service)\W*$", re.I)
SENIORS_SPLIT_FIX_RE = re.compile(r"\b([A-Z]) ([a-z]{2,})")
SENIORS_MOJIBAKE = (("CafAc", "Caf\u00e9"), ("SeA\ufffdor", "Se\u00f1or"),
                    ("SeAor", "Se\u00f1or"), ("you\ufffd?Tre", "you're"),
                    ("\ufffd?", "-"), ("\ufffd", ""))


def _seniors_fix_splits(text):
    text = re.sub(r"[\u2013\u2014]", "-", text or "")
    text = SENIORS_SPLIT_FIX_RE.sub(r"\1\2", text or "")
    for bad, good in SENIORS_MOJIBAKE:
        text = text.replace(bad, good)
    return text


def _seniors_title(head):
    """Bottom-up title/host split. Returns (title, host)."""
    acc = []
    i = len(head) - 1
    while i >= 0 and len(acc) < 6:
        ln = head[i]
        if not acc:
            acc.insert(0, ln)
            i -= 1
            continue
        acc_len = len(" ".join(acc))
        if acc[0].startswith("-") or re.search(r"[-–&/]$", ln):
            acc.insert(0, ln)
        elif acc_len < 12 and SENIORS_GENERIC_END_RE.search(ln):
            acc.insert(0, ln)
        elif SENIORS_ORG_END_RE.search(ln):
            break
        elif ln.strip().lower() in SENIORS_PLACE_STOP:
            break
        else:
            acc.insert(0, ln)
        i -= 1
    title = _seniors_fix_splits(" ".join(acc)).strip()
    host = _seniors_fix_splits(" ".join(head[:i + 1])).strip()
    return title, host


def _seniors_clean_desc(lines, footer_re):
    out = []
    for ln in lines:
        if footer_re.match(ln) or SENIORS_CAT_RE.match(ln):
            continue
        if re.match(r"^(BIKETOBER|Ride,? Rate|Why choose|Premium|To find out more"
                    r"|Scan the QR|Find out more|100\+ prizes|1-31$|Oct$)", ln):
            continue
        out.append(ln)
    while out and (SENIORS_CONTACT_RE.search(out[0]) or len(out[0]) < 4):
        out.pop(0)
    text = re.sub(r"\s+", " ", " ".join(out[-8:])).strip()
    return _seniors_fix_splits(text)[:500]


def _seniors_cost_value(lines, ci):
    """Cost value after a 'Cost' marker line: first line + $/keyword continuations."""
    vals = []
    for v in lines[ci + 1:ci + 4]:
        if v in SENIORS_STOP_RE or v == "Location":
            break
        if vals and not ("$" in v or re.match(
                r"(?i)^(full|conc|groups|gold|free|.*donation|.*coin)", v)):
            break
        vals.append(v)
        if len(vals) >= 2:
            break
    if not vals and ci + 1 < len(lines):
        vals = [lines[ci + 1]]
    text = _seniors_fix_splits(re.sub(r"\s+", " ", " ".join(vals)).strip())
    if len(text) <= 48:
        return text
    # A column-layout merge can glue prose onto the price line; keep only
    # leading price tokens ("Free Whether..." -> "Free").
    toks, keep = text.split(), []
    pricey = {"|", "+", "-", "per", "term", "session", "concession",
              "conc.", "groups", "full", "gold", "coin", "donation", "free",
              "special", "fest", "seniors", "month", "october", "of"}
    for t in toks:
        if not keep or "$" in t or t.strip(".,|").lower() in pricey \
                or re.match(r"^(Full|Conc\.?|Groups|[\d$])", t):
            keep.append(t)
        else:
            break
    text = " ".join(keep)[:60] or text[:60]
    return text


def _seniors_day_lists(lines):
    """Yield (line_idx, match) for day-list matches with line context."""
    for li, ln in enumerate(lines):
        for m in SENIORS_DAYLIST_RE.finditer(ln):
            yield li, m


def _seniors_expand(m, year):
    mon = month_number(m.group(2))
    out = []
    for d in re.findall(r"\d{1,2}", m.group(1)):
        try:
            out.append(datetime(year, mon, int(d)))
        except (ValueError, TypeError):
            pass
    return out


def _seniors_times(lines):
    """All (line_idx, (h, mi)) time-range starts in lines.

    A thin wrapper over webfetch_http.line_range_starts(), which owns the
    pattern and the 12-hour conversion. Skipping a range is worse than it
    sounds: the caller then picks the *nearest* time in the whole text, which
    on a two-card page is the other event's, and publishes one event's start as
    another's.
    """
    return line_range_starts(lines)


def _seniors_assemble(venue):
    """(location, address) from the venue lines a PDF card yields.

    pypdf gives one line per visual line, each keeping its own trailing
    punctuation, and a narrow column wraps a venue name across two of them
    ("Aspendale Gardens" / "Community Service"). Two faults came from that:

    * joining the segments with ", " produced "..., ,", because they already
      ended in commas. 86 published rows.
    * a name ending in a venue-tail word ("Gardens", "Library") was taken as
      complete, so the rest of the name was demoted into the address and the
      row published location="Aspendale Gardens" with "Community Service"
      sitting in the address.

    The prefixing of the venue into `address` is deliberate and matches the
    other sources ("Chelsea Activity Hub, 3-5 Showers Ave, Chelsea 3196").
    """
    venue = [v.strip().strip(",").strip() for v in venue]
    location = venue[0] if venue else ""
    addr_from = 1
    if len(venue) > 1 and venue[0].endswith("-"):
        location = venue[0][:-1] + " " + venue[1]
        addr_from = 2
    elif len(venue) > 1 and len(venue[0]) < 40 and not \
            SENIORS_VENUE_END_RE.search(venue[0]):
        location = (venue[0] + " " + venue[1]).strip()
        addr_from = 2
    # A tail word ends a name only when the next line is not more of it. A
    # street starts with a house number or a unit/cnr, and so does a suburb
    # line the PDF puts on its own ("12 Stanley Avenue" / "Cheltenham").
    if len(venue) > 1 and addr_from == 1:
        nxt = venue[1]
        if (SENIORS_VENUE_END_RE.search(location)
                and not _SENIORS_STREET_RE.match(nxt)
                and not re.match(r"^\d", nxt)
                and len(nxt) < 40 and not nxt.endswith(".")):
            location = (location + " " + nxt).strip()
            addr_from = 2
    address = re.sub(r"\s+", " ", ", ".join(
        [location] + venue[addr_from:])).strip(" ,")[:160]
    return location, address


def fetch_kingston_seniors(cfg, session=None):
    """Fetch the Kingston seniors festival guide.

    The PDF arrives whole, so there is no detail page to bound.
    """
    from pypdf import PdfReader
    pdf_url = cfg["pdf_url"]
    info_url = cfg.get("info_url", pdf_url)
    # No implicit datetime.now().year fallback: the guide is annual, and
    # defaulting to the run year silently reinterprets the whole document.
    # Its absence is a config fault, not a fetch outcome, so it is raised
    # rather than returned as an empty source.
    if "year" not in cfg:
        raise PartialFetch(
            f"kingston_seniors: sources.yaml has no 'year', so the festival "
            f"guide is skipped entirely. health_check.seniors_config_errors() "
            f"reports the same fault.")
    year = int(cfg["year"])
    footer_re = _seniors_footer_re(year)
    report("downloading seniors PDF...")
    pdf_bytes = fetch_bytes(session, pdf_url, min_len=100000, retries=2)
    if not pdf_bytes:
        # A download that did not happen is a broken fetch, not an empty
        # guide. Returning [] here used to be reported as "returned 0 rows",
        # indistinguishable from an out-of-season festival.
        raise PartialFetch(f"seniors PDF download failed ({pdf_url})")
    reader = PdfReader(io.BytesIO(pdf_bytes))
    full = "\n".join((p.extract_text() or "") for p in reader.pages)
    report(f"PDF: {len(reader.pages)} pages, {len(full)} chars")
    page_bounds = [0] + [m.end() for m in re.finditer(
        r"(?m)^\d+\s*\|.*\|\s*\d+\s*$", full)] + [len(full)]

    def page_of(pos):
        return max(0, bisect_right(page_bounds, pos) - 1)

    hb = [m for m in re.finditer(r"Hosted by", full)]
    # Chunks: (page_idx, body_text) -- one per "Hosted by" marker, spanning
    # from this marker to the next. The description is built from the chunk's
    # own lines; reaching back to the previous marker would give every event
    # the text of the one before it.
    chunks = []
    for ci, m in enumerate(hb):
        start = m.end()
        end = hb[ci + 1].start() if ci + 1 < len(hb) else len(full)
        chunks.append((page_of(m.start()), full[start:end]))
    rows, skipped = [], 0
    emitted_keys = []
    dateless = []  # (page_idx, order, base_row_dict)
    pool = []  # (page_idx, frozenset((mon,day)), match_line_text)
    for page_idx, body in chunks:
        lines = [ln.strip() for ln in body.split("\n") if ln.strip()]
        try:
            loc_i = next(i for i, ln in enumerate(lines) if ln == "Location")
        except StopIteration:
            skipped += 1
            continue
        head = lines[:loc_i]
        title, host = _seniors_title(head)
        if len(title) < 3:
            skipped += 1
            continue
        try:
            acc_i = next(i for i, ln in enumerate(lines)
                         if ln == "Venue accessibility")
            venue = lines[loc_i + 1:acc_i]
            tail = lines[acc_i + 1:]
        except StopIteration:
            venue = lines[loc_i + 1:loc_i + 4]
            tail = lines[loc_i + 4:]
        # Assemble venue and address: join a wrapped name, strip the per-line
        # trailing commas pypdf keeps. See _seniors_assemble.
        location, address = _seniors_assemble(
            [_seniors_fix_splits(v) for v in venue])
        # Strip venue text accidentally captured in the title (pypdf merges
        # host/title lines in narrow columns): full-venue prefix, infix
        # cut ("Kingston Active (Waves ...) Tai Chi"), then leading
        # venue-word fragments ("Centre Bus Trip").
        v0 = re.sub(r"\s+", " ", location).strip()
        t_norm = re.sub(r"\s+", " ", title).strip()
        if v0 and len(v0) > 8 and t_norm.startswith(v0):
            title = t_norm[len(v0):].strip(" -–():")
        else:
            title = t_norm
            if v0 and len(v0) > 8 and v0 in title:
                cut = title.split(v0)[-1].strip(" -–():")
                if len(cut) >= 3 and len(title.split(v0)[0]) > 0:
                    title = cut
        # Drop leading venue-word fragments ("Centre Bus Trip").
        parts = title.split()
        while len(parts) > 1 and parts[0].lower().rstrip(",") in \
                SENIORS_VENUE_WORDS:
            parts.pop(0)
        title = " ".join(parts)
        if len(title) < 3:
            skipped += 1
            continue
        cost = ""
        for ci, ln in enumerate(tail):
            if ln == "Cost":
                cost = _seniors_cost_value(tail, ci)
                break
        raw = " ".join(lines)
        source = info_url
        sm = re.search(r"(?:https?://|www\.)\S+", raw)
        if sm:
            url = re.sub(r"\s+", "", sm.group(0)).rstrip(".,)")
            if not url.startswith("http"):
                url = "https://" + url
            if len(url) > 12:
                source = url
        desc = _seniors_clean_desc(lines, footer_re)
        if host:
            desc = (desc + f" Hosted by {host}.").strip()[:500]
        # The card's own fields. Its dates come from the date rows below, so
        # the two datetime keys are empty here and filled in per session.
        base = make_row(cfg["id"], title, source,
                        location=location, address=address,
                        price_text=cost, description=desc or title)
        # Date matches with line numbers (line numbers relative to chunk).
        matches = [(li, m) for li, m in _seniors_day_lists(lines)]
        direct, pooled = [], []
        for mi, (li, m) in enumerate(matches):
            near_multis = sum(
                1 for mj, (lj, _) in enumerate(matches)
                if mj != mi and "," in matches[mj][1].group(1)
                and abs(lj - li) <= 3)
            if "," in m.group(1):
                # Multi-day list: pooled only when clustered with another
                # multi list (shared bottom date-row); isolated ones are the
                # event's own dates.
                if near_multis >= 1:
                    pooled.append((li, m))
                else:
                    direct.append((li, m))
            else:
                direct.append((li, m))
        times = _seniors_times(lines)

        def nearest_time(li):
            if not times:
                return None
            hit = min(times, key=lambda t: (abs(t[0]-li),
                                            0 if t[0] <= li else 1))
            # Bounded, because `times` is every time in the *chunk*, and a
            # chunk on a two-card page holds the neighbouring event's times
            # too. Unbounded, a card whose own time failed to parse took the
            # other card's: verified, a 10.30am class published at 19:00 from
            # the card below it. Six lines is close enough to be this event's
            # own time and far enough to be a different event's.
            if abs(hit[0] - li) > SENIORS_TIME_WINDOW_LINES:
                return None
            return hit[1]

        for li, m in direct:
            tm = nearest_time(li)
            for day in _seniors_expand(m, year):
                dt = day.replace(hour=tm[0], minute=tm[1]) if tm \
                    else day.replace(hour=0, minute=0)
                rows.append(dict(base,
                                 datetime_text=dt.strftime("%A %d %B, %I:%M %p"),
                                 datetime_iso=dt.isoformat()))
                emitted_keys.append(
                    (frozenset([(day.month, day.day)]), tm))
        if pooled:
            pool.append((page_idx, base, pooled, lines))
        if not direct:
            # No direct date here; may still gain dates from the page pool
            # below. `pooled` is the same case -- a chunk whose only dates are
            # multi-day lists is still dateless until the pool is reconciled.
            dateless.append((page_idx, base))
    # Map pool groups to dateless events, per page.
    pool_by_page, dateless_by_page = defaultdict(list), defaultdict(list)
    for entry in pool:
        pool_by_page[entry[0]].append(entry)
    for entry in dateless:
        dateless_by_page[entry[0]].append(entry)
    for page_idx in sorted(set(list(pool_by_page) + list(dateless_by_page))):
        # Re-derive groups with page-local time/cost pairing.
        groups = _seniors_pool_groups(pool_by_page[page_idx])
        # Skip groups already emitted directly. emitted_keys holds per-day
        # single frozensets while pooled groups span several days, so the
        # comparison is on the days actually covered.
        direct_days = {key[0] for key in emitted_keys}
        unclaimed = [g for g in groups
                     if not (g[0] & direct_days and len(g[0]) == 1)]
        targets = dateless_by_page[page_idx]
        if not targets or not unclaimed:
            continue
        if len(targets) == 1:
            assign = [(targets[0], g) for g in unclaimed]
        else:
            if len(unclaimed) != len(targets):
                # Association is positional, so a count mismatch means dates
                # and events do not line up. Say so rather than silently
                # truncating one side -- this is the column-association
                # failure the override file exists to correct.
                report(f"page {page_idx + 1}: {len(unclaimed)} date "
                       f"groups vs {len(targets)} dateless events "
                       f"(associating by position)", level="warn")
            assign = [(targets[i], g) for i, g in enumerate(unclaimed)
                      if i < len(targets)]
        for (_tpage, tbase), (days, tm, cost) in assign:
            for (mon, day) in sorted(days):
                try:
                    d = datetime(year, mon, day)
                except ValueError:
                    continue
                dt = d.replace(hour=tm[0], minute=tm[1]) if tm \
                    else d.replace(hour=0, minute=0)
                rows.append(dict(tbase,
                                 price_text=cost or tbase.get("price_text", ""),
                                 datetime_text=dt.strftime("%A %d %B, %I:%M %p"),
                                 datetime_iso=dt.isoformat()))
    # Dedupe identical (name, date) rows (the print PDF repeats some text
    # regions, which would otherwise double-emit sessions). Include the start
    # time: a venue can legitimately run the same activity twice in one day.
    seen, uniq = set(), []
    for r in rows:
        key = (r["name"].lower(), (r.get("datetime_iso") or "")[:16])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    rows = uniq
    rows = _seniors_apply_overrides(rows, cfg)
    # Overrides are applied by (name, venue) and can reintroduce a session the
    # pass above just removed, so re-check for exact duplicates.
    seen, uniq = set(), []
    for r in rows:
        key = (r["name"].lower(), (r.get("datetime_iso") or "")[:16],
               (r.get("location") or "").lower())
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    rows = uniq
    report(f"festival: {len(rows)} rows ({skipped} chunks skipped)")
    return rows


def _seniors_apply_overrides(rows, cfg):
    """Replace parsed rows with hand-checked sessions for the ambiguous
    multi-column spreads (shared bottom date-rows lose column association
    in flat-text extraction). Overrides live in
    scripts/seniors_festival_overrides.json; values transcribed from the
    rendered Event Guide for the configured year."""
    import json as _json
    overrides_path = Path(__file__).resolve().parent / "seniors_festival_overrides.json"
    try:
        with open(overrides_path, encoding="utf-8") as f:
            doc = _json.load(f)
        overrides = doc.get("overrides", [])
    except (FileNotFoundError, ValueError) as e:
        report(f"overrides: none ({e})", level="warn")
        return rows

    # The overrides are transcribed by hand from one year's rendered guide, and
    # every date in them is literal -- the festival falls on different days each
    # year, so nothing can re-stamp them. `year` records which guide they came
    # from. A mismatch with the configured year means the next run would emit
    # last year's dates: they are >90 days old, so prune_old() deletes them and
    # the festival silently disappears. Report it here, before the health check
    # turns it red.
    overrides_year = doc.get("year")
    if overrides_year is None:
        report("overrides: no 'year' key - cannot detect a stale transcription",
               level="warn")
    else:
        # A hand-edited file can hold anything; a bad value must not abort the
        # fetch, it just means staleness cannot be judged here.
        try:
            mismatched = int(overrides_year) != int(cfg["year"])
        except (TypeError, ValueError):
            mismatched = True
            overrides_year = repr(overrides_year)
        if mismatched:
            report(f"overrides STALE - transcribed for {overrides_year} but "
                   f"sources.yaml says {cfg['year']}. Those dates will be "
                   f"pruned as >90d old. Re-transcribe or drop the 'year' key.",
                   level="error")

    def norm(s):
        return " ".join((s or "").lower().split())

    def matches(r, ov):
        # Name match, then venue. A stored `hit` flag was set in both branches
        # and never read; the `else: return False` carried the control flow.
        if norm(r["name"]) == norm(ov["name"]):
            pass
        elif len(norm(ov["name"])) > 8 and norm(r["name"]).endswith(
                norm(ov["name"])):
            pass
        else:
            return False
        return ov["venue"] in norm(
            (r.get("location") or "") + " " + (r.get("address") or ""))

    mine = [r for r in rows if r.get("source_id") == cfg["id"]]
    rest = [r for r in rows if r.get("source_id") != cfg["id"]]
    kept, applied = [], 0
    for r in mine:
        hit = False
        for ov in overrides:
            if matches(r, ov):
                hit = True
                break
        if not hit:
            kept.append(r)
    for ov in overrides:
        base = None
        for r in mine:
            if matches(r, ov):
                base = r
                break
        for sess in ov.get("sessions", []):
            tm = sess.get("time") or ""
            # Validate rather than slicing blindly: a 4-char "9:00" would
            # silently become midnight, and "9:00a" would raise uncaught.
            hm = re.match(r"^(\d{1,2}):(\d{2})$", tm)
            if hm:
                hh, mm = int(hm.group(1)), int(hm.group(2))
                # A well-formed but out-of-range time ("24:00", "10:75")
                # passes the regex and then raises inside datetime.replace().
                # The except below catches that and `continue`s, which
                # silently dropped every session of the event with no warning
                # at all -- the run still succeeded, because other events
                # produced rows. Validate here so the fault is reported.
                if not (0 <= hh <= 23 and 0 <= mm <= 59):
                    report(f"override {ov['name']!r}: time {tm!r} is out of "
                           f"range, using midnight", level="warn")
                    hh, mm = 0, 0
            else:
                if tm:
                    report(f"override {ov['name']!r}: unparseable time {tm!r}, "
                           f"using midnight", level="warn")
                hh, mm = 0, 0
            for d in sess.get("dates", []):
                try:
                    dt = datetime.strptime(d, "%Y-%m-%d").replace(
                        hour=hh, minute=mm)
                except ValueError as e:
                    report(f"override {ov['name']!r}: date {d!r} unusable "
                           f"({e}), session skipped", level="warn")
                    continue
                # An override is a full row in its own right: it may state a
                # venue and address the PDF never yielded, so every key is
                # written rather than inherited.
                row = make_row(
                    cfg["id"], ov["name"],
                    (base or {}).get("source") or cfg.get("info_url", ""),
                    datetime_iso=dt.isoformat(),
                    datetime_text=dt.strftime("%A %d %B, %I:%M %p"),
                    location=ov.get("location") or (base or {}).get(
                        "location", ""),
                    address=ov.get("address") or (base or {}).get(
                        "address", ""),
                    price_text=sess.get("cost", ov.get("cost", "")),
                    description=(base or {}).get("description")
                    or ov.get("description", ov["name"]),
                )
                kept.append(row)
                applied += 1
    report(f"overrides: {len(overrides)} events, {applied} sessions")
    return rest + kept


def _seniors_pool_groups(entries):
    """Pair pooled day-lists with time + Cost value.

    entries: list of (page_idx, base, pooled[(li, match)], lines).
    Within one chunk tail the row is column-ordered, so when the counts
    of dates/times/costs agree they are zipped positionally; otherwise
    each group falls back to nearest-neighbour pairing.
    Returns [(frozenset((mon,day)), (h,mi)|None, cost_str)].
    """
    groups = []
    for _, base, pooled, lines in entries:
        texts = list(lines)
        # Restrict pairing context to the pooled span: the event's own
        # block times/costs must not leak into row pairing.
        lis = [li for li, _ in pooled]
        lo = max(0, min(lis) - SENIORS_TIME_WINDOW_LINES)
        hi = max(lis) + SENIORS_TIME_WINDOW_LINES + 2
        costs = []
        for li, ln in enumerate(texts):
            if ln == "Cost" and lo <= li <= hi:
                costs.append((li, _seniors_cost_value(texts, li)))
        times = line_range_starts(texts, lo, hi)
        dates = []
        for li, m in pooled:
            mon = month_number(m.group(2))
            days = frozenset((mon, int(d)) for d in
                             re.findall(r"\d{1,2}", m.group(1)) if mon)
            if days:
                dates.append((li, days))
        if dates and len(dates) == len(times) == len(costs) and len(dates) > 1:
            for (_, days), (_, tm), (_, cost) in zip(dates, times, costs):
                groups.append((days, tm, cost))
            continue
        for li, m in pooled:
            mon = month_number(m.group(2))
            if not mon:
                continue
            days = frozenset((mon, int(d)) for d in
                             re.findall(r"\d{1,2}", m.group(1)))
            if not days:
                continue
            tm = min(times, key=lambda t: (abs(t[0] - li),
                                           0 if t[0] <= li else 1))[1] \
                if times else None
            cost = ""
            near_before = [cl for cl, _ in costs if 0 <= li - cl <= 30]
            if near_before:
                cost = dict(costs)[max(near_before)]
            else:
                after = sorted(cl for cl, _ in costs if 0 < cl - li <= 12)
                if after:
                    cost = dict(costs)[after[0]]
            groups.append((days, tm, cost))
    return groups
