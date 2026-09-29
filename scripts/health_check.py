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
from pathlib import Path

import yaml

from dedupe import (PRUNE_DAYS, load_live_inputs, name_head,
                    reconcile_store, reference_today, venue_head)
from recurrence import weekday_slots
from status import STATUS_LABELS, event_status, is_ongoing_service

ROOT = Path(__file__).resolve().parent.parent

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
        with open(ROOT / "scripts" / "sources.yaml", encoding="utf-8") as f:
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
        with open(ROOT / "scripts" / "seniors_festival_overrides.json",
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


# --- mobile accessibility invariants -------------------------------------
# The page is a single hand-written document, so nothing in the pipeline
# stops a CSS edit from quietly breaking the phone layout or dropping a
# label. These are the specific regressions this build has actually had,
# written down so they cannot come back unnoticed.


def _relative_luminance(hex_colour):
    """WCAG 2.x relative luminance for a #rgb / #rrggbb string."""
    h = (hex_colour or "").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6 or not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        raise ValueError(f"not a hex colour: {hex_colour!r}")
    out = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255
        out.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def contrast_ratio(fg, bg):
    """WCAG contrast ratio between two hex colours, 1.0-21.0."""
    a, b = _relative_luminance(fg), _relative_luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


# WCAG 2.2 1.4.3 Contrast (Minimum) for normal-size text, and 1.4.11 for the
# boundary of an interactive control.
CONTRAST_TEXT = 4.5
CONTRAST_UI = 3.0

# 1.4.12 Text Spacing requires 1.5x line height.
MIN_LINE_HEIGHT = 1.5

# iOS Safari zooms the viewport on focus of any form control computed below
# 16px and never zooms back out, stranding the reader in a magnified page
# with the controls scrolled off-screen. 1.4.4 wants text to scale, but
# 16px is the floor that keeps a phone from doing this.
MIN_FORM_FONT_PX = 16.0
DEFAULT_FONT_PX = 16.0


def a11y_errors(source, name):
    """Accessibility invariants for one copy of the page.

    `source` is the markup, `name` how to refer to it in a message. Runs
    against both the template and the built index.html: a fix that was
    never rebuilt leaves the published page broken, and health_check.py
    already checks the built artefact for unfilled placeholders for
    exactly that reason.
    """
    errors = []

    # CSS comments are stripped before any stylesheet assertion. They are prose
    # about the stylesheet, not stylesheet, and they mention the very selectors
    # being looked for -- a comment reading "the selector covers the sort
    # <select> and iOS Safari zooms..." was enough to make the font-size check
    # report a form control that does not exist.
    css = re.sub(r"/\*.*?\*/", "", source, flags=re.S)

    # The card layout under 768px does `table/tbody/tr/td{display:block}`.
    # That is deliberate -- a 7-column table at 375px is worse -- but
    # browsers derive those implicit ARIA roles from `display`, so every one
    # of them collapses to `generic` and each cell loses its column header.
    # Explicit roles are not derived from `display`, so they survive; these
    # assertions are on the markup, per element, rather than a blanket ban
    # on the layout.
    for markup, why in (
        (r'<table[^>]*role="table"', "the table needs an explicit role"),
        (r'<thead[^>]*role="rowgroup"', "thead needs an explicit rolegroup"),
        (r'<tbody[^>]*role="rowgroup"', "tbody needs an explicit rolegroup"),
        (r'<th[^>]*role="columnheader"', "sortable headers need columnheader"),
        (r'''role=["']row["']''',
         "rows need an explicit role to survive display:block"),
        (r'''role=["']cell["']''',
         "cells need an explicit role to survive display:block"),
    ):
        if not re.search(markup, source):
            errors.append(f"{name}: missing {markup} ({why})")

    # The same display rewrite takes the thead out of the accessibility tree
    # as well as off the screen, so it must be clipped, never display:none.
    if re.search(r"thead\s*(,[^{]*)?\{[^}]*display\s*:\s*none", css):
        errors.append(
            f"{name}: thead is display:none, which removes the column names "
            f"from the accessibility tree as well as the screen")

    # Field names were rendered with `td::before{content:attr(data-label)}`.
    # Generated content is not reliably present in the accessibility tree,
    # so VoiceOver and TalkBack announced a wall of unlabelled values.
    if re.search(r"td\s*(,[^{]*)?::(before|after)\s*\{[^}]*content\s*:\s*"
                 r"attr\(\s*data-label", css):
        errors.append(
            f"{name}: td field labels come from ::before content, which screen "
            f"readers do not announce -- emit a real element instead")

    # The same hidden-text helper has to exist, because the fixes that
    # replace the above all depend on it.
    if "visually-hidden" not in css:
        errors.append(f"{name}: no .visually-hidden helper for screen-reader-"
                      f"only text")
    elif not re.search(r"\.visually-hidden\s*\{[^}]*clip", css):
        errors.append(f"{name}: .visually-hidden does not clip its content, so "
                      f"it will be visible on screen")

    for required, why in (
        ("<main", "no main landmark or skip link on a 2000-row list page"),
        ("<caption", "the table has no caption, and thead is hidden on mobile"),
        ('role="status"', "filter results are re-rendered with no announcement"),
        ('aria-controls="filterpanel"',
         "the Filters toggle does not point at the panel it opens"),
        ('aria-label="Sort events by"',
         "sorting is unreachable on mobile, where thead is hidden"),
    ):
        if required not in source:
            errors.append(f"{name}: missing {required} ({why})")

    # 1.4.3: every badge paints white text on its own background. Six did
    # not clear 4.5:1, and at 9px on a phone the shortfall is much more
    # visible than the number suggests.
    for label, colour in re.findall(r"\.badge-([a-z_]+)\{background:(#[0-9a-fA-F]{3,6})",
                                    css):
        try:
            ratio = contrast_ratio(colour, "#ffffff")
        except ValueError as e:
            errors.append(f"{name}: badge {label}: {e}")
            continue
        if ratio < CONTRAST_TEXT:
            errors.append(
                f"badge {label}: white on {colour} is {ratio:.2f}:1, needs "
                f"{CONTRAST_TEXT}:1 (WCAG 1.4.3)")

    # 1.4.11: the quick-filter chips were bordered in a colour 1.44:1
    # against white, so the control boundary was effectively invisible.
    for colour in re.findall(r"--control-border\s*:\s*(#[0-9a-fA-F]{3,6})",
                             css):
        ratio = contrast_ratio(colour, "#ffffff")
        if ratio < CONTRAST_UI:
            errors.append(
                f"--control-border {colour} is {ratio:.2f}:1 against white, "
                f"needs {CONTRAST_UI}:1 for a control boundary (WCAG 1.4.11)")

    # ...and a control has to actually use it. Checking the token's value
    # alone let a rule fall back to --border, which is decorative table-grid
    # grey at 1.44:1, and the check stayed green.
    for selector in (r"\.qf-btn", r"\.src-link", r"\.ics-btn",
                     r"\.filtergroup select", r"\.tcheck input"):
        for body in re.findall(selector + r"[^{}]*\{([^}]*)\}", css):
            if "var(--border)" in body:
                errors.append(
                    f"{selector} draws its boundary with var(--border), which "
                    f"is 1.44:1 against white; interactive controls need "
                    f"var(--control-border) (WCAG 1.4.11)")

    # 1.4.12.
    body_lh = re.search(r"body\s*\{[^}]*line-height\s*:\s*([\d.]+)", css)
    if not body_lh:
        errors.append(f"{name}: body has no line-height to check (1.4.12)")
    elif float(body_lh.group(1)) < MIN_LINE_HEIGHT:
        errors.append(
            f"body line-height {body_lh.group(1)} is under "
            f"{MIN_LINE_HEIGHT} (WCAG 1.4.12 Text Spacing)")

    # iOS viewport zoom, for every selector that styles a form control.
    # `em` is resolved against a 16px base here, which is the real parent
    # chain for every rule that matches (all of them sit inside body-level
    # containers); a deeper `em` chain only shrinks it further, so this
    # cannot miss.
    for selector, size, unit in re.findall(
            r"((?:input|select|textarea)[^{}]*)\{[^}]*font-size\s*:\s*"
            r"([\d.]+)(rem|em)", css):
        px = float(size) * DEFAULT_FONT_PX
        if px < MIN_FORM_FONT_PX:
            errors.append(
                f"{selector.strip()}: font-size {size}{unit} is {px:.1f}px -- "
                f"iOS Safari zooms on focus below {MIN_FORM_FONT_PX:.0f}px and "
                f"never zooms back")

    # 2.4.7 Focus Visible. The one custom ring was on `th[data-k]:focus`,
    # and `th` is hidden on mobile, so keyboard and Switch Control users on
    # a phone had no indicator at all.
    if not re.search(r":focus-visible\s*\{", css):
        errors.append(f"{name}: no :focus-visible ring anywhere; the only one "
                      f"was on the sort headers, which mobile hides")

    return errors


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
                  in weekday_slots(text)
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
        venue = venue_head(r.get("location"))
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

    # The store must be exactly what the sources justify. Re-running the
    # pipeline's own reconciliation is the check: a row that would be dropped
    # now is a row the store is still carrying that no source backs, which is
    # how a corrected start time or a withdrawn listing survives as a phantom
    # the exact-duplicate checks cannot see.
    live = load_live_inputs(quiet=True)
    if live is None:
        errors.append("could not load source inputs to reconcile against - "
                      "did fetch_events.py / webfetch_sources.py run?")
    else:
        kept, dropped = reconcile_store(rows, live, reference_today())
        if dropped:
            sample = ", ".join(
                f"{r.get('name')!r} {str(r.get('datetime_iso'))[:16]}"
                for r in dropped[:5])
            errors.append(
                f"{len(dropped)} rows in events.json are not backed by any "
                f"source (corrected time, or listing withdrawn) - "
                f"dedupe.py must run after the fetches: {sample}")

    # A listing the venue has stopped selling, or a drop-in service rather
    # than a session, is hidden by default. If build_site.py did not run, the
    # keys are absent and the page would quietly show them all.
    unflagged = [r for r in rows if "hidden_by_default" not in r]
    if unflagged:
        errors.append(f"{len(unflagged)} rows have no hidden_by_default flag - "
                      f"did build_site.py run?")
    else:
        # Re-derive rather than trust: a status.py regression must not leave
        # a sold-out workshop sitting in the published data looking bookable.
        stale_status, stale_service = [], []
        for r in rows:
            status, _detail = event_status(r)
            if status != (r.get("status") or ""):
                stale_status.append(r.get("name"))
            service, _reason = is_ongoing_service(r)
            if service != bool(r.get("is_service")):
                stale_service.append(r.get("name"))
        if stale_status:
            errors.append(f"{len(stale_status)} rows whose sold-out/cancelled "
                          f"status disagrees with their own text: "
                          f"{', '.join(sorted(set(stale_status))[:5])}")
        if stale_service:
            errors.append(f"{len(stale_service)} rows whose drop-in-service "
                          f"flag disagrees with their own text: "
                          f"{', '.join(sorted(set(stale_service))[:5])}")
        unknown = {r.get("status") for r in rows
                   if r.get("status") and r["status"] not in STATUS_LABELS}
        if unknown:
            errors.append(f"rows carry an unknown status {sorted(unknown)} - "
                          f"add it to STATUS_LABELS so the UI can label it")

    # Every source label needs a badge: CSS class + friendly-name entries,
    # or its badge renders as invisible white-on-white text.
    try:
        with open(ROOT / "src" / "templates" / "index.html", encoding="utf-8") as f:
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
        errors.extend(a11y_errors(tpl, "template"))
    except FileNotFoundError as e:
        errors.append(f"template missing: {e}")

    # The built page is what users actually load; check the artefact too.
    try:
        with open(ROOT / "index.html", encoding="utf-8") as f:
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
        # The template being accessible proves nothing about the page users
        # actually load: an uncommitted rebuild ships the old markup. This
        # ran the same battery against index.html and stayed green for a
        # build that had never been made.
        errors.extend(a11y_errors(built, "index.html"))
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
