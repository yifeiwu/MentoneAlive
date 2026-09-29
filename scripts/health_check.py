"""Pipeline health check — fail loudly instead of publishing bad data.

Run after build_site.py in GHA. Checks:
- data/events.json loads and has a sane total
- year-round sources above generous floors (catches dead scrapers)
- no exact (name, date, location) duplicates (catches dedupe regressions)
- every row carries a real date (nothing unplaceable on a calendar)
- every source label has badge CSS + friendly-name entries in the template
- the template is a single document with no unfilled placeholders
- the built index.html is a single document that actually parsed its data

Seasonal/static sources (seniors festivals, archived rows) are warn-only:
their counts legitimately decay to zero out of season.
"""
import json
import re
import sys
from collections import Counter
from datetime import date

import yaml

from dedupe import PRUNE_DAYS, _venue_head, name_head
from recurrence import _weekday_slots

PLACEHOLDERS = ("__EVENTS_DATA__", "__GENERATED_AT__", "__EVENT_COUNT__",
                "__SOURCE_COUNT__", "__TYPE_CHECKBOXES__")

# The seniors festivals run Sept-Nov (SENIORS_MONTHS in webfetch_seniors.py).
# Outside that window a stale configured year is harmless -- last year's
# festival really has finished and 0 rows is the honest answer.
SENIORS_FESTIVAL_MONTHS = frozenset({9, 10, 11})


def seniors_config_errors(today=None):
    """Config faults that would empty the seniors festival *silently*.

    The festival guide is annual, so a `year` that no longer matches makes
    _seniors_footer_re() stop matching, no day-list gets dated, and
    prune_old() then deletes every row. kingston_seniors is warn-only by
    design, which is exactly why that total wipeout currently only warns.

    Split out of main() and parameterised on `today` so it can be exercised
    against a past or future year without editing the config.
    """
    today = today or date.today()
    try:
        with open("scripts/sources.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return ["sources.yaml missing - cannot verify the seniors config"]
    except yaml.YAMLError as e:
        return [f"sources.yaml unparseable: {e}"]

    seniors = next((s for s in (cfg.get("webfetch") or [])
                    if s.get("type") == "kingston_seniors_pdf"), None)
    if seniors is None:
        return ["sources.yaml has no kingston_seniors_pdf source"]

    cfg_year = seniors.get("year")
    if cfg_year is None:
        # webfetch_seniors.py skips the source outright when 'year' is absent.
        return ["kingston_seniors: sources.yaml has no 'year', so the festival "
                "guide is skipped entirely"]

    errors = []
    try:
        with open("scripts/seniors_festival_overrides.json",
                  encoding="utf-8") as f:
            ov_year = (json.load(f) or {}).get("year")
    except (OSError, ValueError) as e:
        ov_year = None
        errors.append(f"seniors overrides unreadable: {e}")

    # Both values are hand-edited, so neither int() conversion is safe to leave
    # unguarded -- a typo would otherwise abort the health check with a
    # traceback instead of reporting the config fault it is.
    try:
        cfg_year_int = int(cfg_year)
    except (TypeError, ValueError):
        errors.append(f"kingston_seniors: sources.yaml year {cfg_year!r} is not "
                      f"an integer")
        return errors
    try:
        ov_year_int = None if ov_year is None else int(ov_year)
    except (TypeError, ValueError):
        ov_year_int = None
        errors.append(f"seniors overrides year {ov_year!r} is not an integer")

    if ov_year is None:
        errors.append("seniors overrides: no 'year' key, so a stale "
                      "transcription cannot be detected")
    elif ov_year_int is not None and ov_year_int != cfg_year_int:
        errors.append(
            f"seniors overrides transcribed for {ov_year} but sources.yaml "
            f"says {cfg_year}: those dates are all >{PRUNE_DAYS}d old and get "
            f"pruned, emptying the festival")

    if (cfg_year_int != today.year
            and today.month in SENIORS_FESTIVAL_MONTHS):
        errors.append(
            f"kingston_seniors: sources.yaml year {cfg_year} is not the "
            f"current year {today.year} and the festival is in season - bump "
            f"it when the new guide is published")
    return errors


def _weekday_of(iso):
    """Python weekday (Mon=0) for an ISO timestamp, or None."""
    try:
        return date.fromisoformat(iso[:10]).weekday()
    except ValueError:
        return None


MIN_TOTAL = 700
# Generous floors (~25-50% of normal) for always-on sources.
# gd_libraries is low because dedupe_by_source_url() collapses the listings
# that greater_dandenong also scrapes from the same page; the four that remain
# are the only events unique to that source.
MIN_SOURCE = {
    "kingston_hubs": 200,
    "bayside_live": 60,
    "greater_dandenong": 10,
    "ccc": 10,
    "chatty_cafe": 10,
    "kingston_council": 3,
    "kingston_arts": 3,
    "gd_libraries": 3,
}
WARN_ONLY = {"kingston_seniors", "bayside_seniors", "bayside_archived",
             "frankston_archived"}


def main():
    with open("data/events.json", encoding="utf-8") as f:
        data = json.load(f)
    rows = data.get("rows", [])
    errors, warnings = [], []

    if len(rows) < MIN_TOTAL:
        errors.append(f"total {len(rows)} < floor {MIN_TOTAL}")

    labels = Counter(r.get("source_label", "unknown") for r in rows)
    for src, floor in MIN_SOURCE.items():
        if labels.get(src, 0) < floor:
            errors.append(f"source {src}: {labels.get(src, 0)} < floor {floor}")
    for src in WARN_ONLY:
        if labels.get(src, 0) == 0:
            warnings.append(f"source {src}: 0 rows (seasonal/static, ok)")

    # Warn-only above covers "out of season". A stale *config* is a different
    # fault and has to fail the build, or the whole festival disappears without
    # anyone noticing until October.
    errors.extend(seniors_config_errors())

    # Start time is part of the key: a venue can legitimately run the same
    # class twice in one day ('Cert II in EAL' Wed 9am and 12:30pm), so
    # collapsing on the date alone would report real sessions as duplicates.
    seen, dups = set(), 0
    for r in rows:
        iso = str(r.get("datetime_iso") or "").replace("Z", "")
        stamp = iso[:16] if "T" in iso else ""
        key = (" ".join((r.get("name") or "").lower().split()),
               stamp,
               " ".join((r.get("location") or "").lower().split()))
        if key in seen:
            dups += 1
        seen.add(key)
    if dups:
        errors.append(f"{dups} exact (name, start, location) duplicates")

    # Same listing page reported by two scrapers. These differ only in how they
    # name the venue, so the exact check above cannot see them. The description
    # must agree, which keeps genuinely distinct events that share a page (two
    # STEADYstrength classes at two different halls on one CCC page).
    listing_seen, listing_dups = {}, 0
    for r in rows:
        url = (r.get("source") or "").rstrip("/")
        stamp = str(r.get("datetime_iso") or "")[:16]
        name = " ".join((r.get("name") or "").lower().split())
        if not url or "T" not in stamp or not name:
            continue
        desc = " ".join((r.get("description") or "").lower().split())
        key = (url, name, stamp)
        prev = listing_seen.get(key)
        if prev is not None and prev == desc:
            listing_dups += 1
        else:
            listing_seen.setdefault(key, desc)
    if listing_dups:
        errors.append(f"{listing_dups} same-listing duplicates (one event "
                      f"page, two scrapers) - dedupe_by_source_url() regressed")

    # A row dated by inference must not sit at midnight when its own text
    # states a start time for that weekday: recurrence.py used to leave those
    # at 00:00, and because inferred dates are stored they then persisted
    # forever. Only unambiguous cases are flagged -- where a weekday states
    # two times (a morning and an afternoon session) either is legitimate.
    stale = []
    for r in rows:
        if not r.get("date_inferred"):
            continue
        iso = str(r.get("datetime_iso") or "")
        if len(iso) < 16:
            continue
        text = r.get("description") or ""
        stated = {start for day, start, _end
                  in _weekday_slots(text)
                  if start and _weekday_of(iso) == day}
        if len(stated) == 1 and iso[11:16] not in stated:
            stale.append(f"{r.get('name')} ({iso[11:16]} vs {stated.pop()})")
    if stale:
        errors.append(
            f"{len(stale)} inferred rows whose stored time contradicts the "
            f"text: {', '.join(sorted(stale)[:5])}")

    # One session, two sources, two titles. Sources style the same programme
    # differently ("Chatty Cafe - Cheltenham Community Centre" vs "Chatty
    # Cafe" vs "Chatty Cafe - Connect over a Cuppa"), so a same-name check
    # cannot see it; what is shared is the base name, venue, date and time.
    prog_seen, prog_dups = {}, 0
    for r in rows:
        iso = str(r.get("datetime_iso") or "")
        head = name_head(r.get("name"))
        venue = _venue_head(r.get("location"))
        if not head or not venue or "T" not in iso:
            continue
        key = (head, venue, iso)
        if key in prog_seen:
            prog_dups += 1
        prog_seen.setdefault(key, r)
    if prog_dups:
        errors.append(
            f"{prog_dups} same-programme duplicates (one session, two "
            f"titles) - dedupe_by_source_url() regressed")

    dateless = [r for r in rows if not r.get("datetime_iso")]
    if dateless:
        sample = ", ".join(sorted({(r.get("name") or "?")[:40]
                                   for r in dateless})[:5])
        errors.append(f"{len(dateless)} dateless rows (need a date to be "
                      f"placed on a calendar): {sample}")

    # Every source label needs a badge: CSS class + friendly-name entries,
    # or its badge renders as invisible white-on-white text.
    try:
        with open("src/templates/index.html", encoding="utf-8") as f:
            tpl = f.read()
        css = set(re.findall(r"\.badge-([a-z_]+)\{", tpl))
        for label in labels:
            if label == "unknown":
                continue
            if label not in css:
                errors.append(f"source {label}: missing .badge-{label} CSS")
            if not re.search(r"[{,]" + re.escape(label) + r":", tpl):
                errors.append(f"source {label}: missing friendly name in maps")

        # A duplicated template silently inlines the whole event array twice
        # and ships a page whose JS never runs.
        doctypes = tpl.lower().count("<!doctype")
        if doctypes != 1:
            errors.append(f"template has {doctypes} <!DOCTYPE> (want exactly 1)")
        if tpl.lower().count("</html>") != 1:
            errors.append(f"template has {tpl.lower().count('</html>')} </html> "
                          f"(want exactly 1)")
        for ph in PLACEHOLDERS:
            if tpl.count(ph) != 1:
                errors.append(f"placeholder {ph} appears {tpl.count(ph)}x "
                              f"(want exactly 1)")
        # Top-level calls to functions that are never defined abort the whole
        # script block before render() runs.
        for fn in ("parseURLState", "updateURL", "updatePagination"):
            if not re.search(r"function\s+" + fn + r"\s*\(", tpl):
                errors.append(f"template calls {fn}() but never defines it")
    except FileNotFoundError as e:
        errors.append(f"template missing: {e}")

    # The built page is what users actually load; check the artefact too.
    try:
        with open("index.html", encoding="utf-8") as f:
            built = f.read()
        n_doctype = built.lower().count("<!doctype")
        if n_doctype != 1:
            errors.append(f"index.html has {n_doctype} <!DOCTYPE> "
                          f"(want exactly 1)")
        for ph in PLACEHOLDERS:
            if ph in built:
                errors.append(f"index.html still contains {ph}")
        if built.lower().count("</html>") != 1:
            errors.append("index.html has a duplicated/partial document")
    except FileNotFoundError:
        errors.append("index.html missing - did build_site.py run?")

    for w in warnings:
        print(f"WARN: {w}")
    if errors:
        for e in errors:
            print(f"FAIL: {e}")
        sys.exit(1)
    print(f"health ok: {len(rows)} events, {len(labels)} sources, 0 dupes")


if __name__ == "__main__":
    main()
