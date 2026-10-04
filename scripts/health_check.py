"""Pipeline health check — fail loudly instead of publishing bad data.

Run after build_site.py in GHA. Checks:
- data/events.json loads and has a sane total
- year-round sources above generous floors (catches dead scrapers)
- no exact (name, date, location) duplicates (catches dedupe regressions)
- every row carries a real date (nothing unplaceable on a calendar)
- every source label has badge CSS + friendly-name entries in the template
- the template is a single document with no unfilled placeholders
- the built index.html is a single document that actually parsed its data
- the page's markup: no duplicate id, balanced container tags, and no
  `hidden` attribute overridden by a `display` value in the CSS
- the table's secondary text declares a rem size (so it cannot compound),
  and the filter checkboxes declare a target of at least 24x24

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

from config import load_config as _config_entries  # noqa: E402
from config import read_config as _read_config  # noqa: E402
from config import source_ids as _config_source_ids  # noqa: E402

from activity_types import TYPES
from dedupe import (ARCHIVED_SOURCE_IDS, PRUNE_DAYS, load_live_inputs,
                    name_head, reconcile_store, reference_today, slot_hash,
                    venue_head)
from recurrence import infer_event, weekday_slots
from status import STATUS_LABELS, event_status, is_ongoing_service
from venues import is_online, needs_address
from webfetch_seniors import SENIORS_FESTIVAL_MONTHS

ROOT = Path(__file__).resolve().parent.parent

PLACEHOLDERS = ("__EVENTS_DATA__", "__GENERATED_AT__", "__EVENT_COUNT__",
                "__SOURCE_COUNT__", "__TYPE_CHECKBOXES__")

# Substituted more than once, deliberately: __EVENT_COUNT__ is also in the
# <noscript> fallback, where it tells a reader with JS off how much of the
# index they are not seeing. __EVENTS_DATA__ is the one that must appear
# exactly once -- a second copy inlines the whole event array twice and ships
# a page whose script never runs (see the count assertion below).
SINGLE_USE_PLACEHOLDERS = ("__EVENTS_DATA__",)

# The seniors festivals run Sept-Nov. Outside that window a stale configured year
# is harmless -- last year's festival really has finished and 0 rows is the honest
# answer. The window itself is owned by webfetch_seniors, which is where the guide's
# day-list pattern is built from the same three names; this used to carry its own
# copy while citing a constant here that did not exist.


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
        cfg = _read_config()
    except FileNotFoundError:
        return ["sources.yaml missing - cannot verify the seniors config"]
    except yaml.YAMLError as e:
        return [f"sources.yaml unparseable: {e}"]

    seniors = next((s for s in ((cfg.get("webfetch") or []) + (cfg.get("sources") or []))
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


def _suburb_in(row, allowed):
    """True when the row is inside the configured catchment.

    Deliberately the fetcher's own function, not a second reading of the rule.
    health_check used to carry a private copy, and the two disagreed about the
    case that mattered: a row whose suburb cannot be extracted was *kept* by
    the fetcher ("cannot place it, so do not discard it") and *failed* by the
    checker ("cannot place it, so it is out of area"). So the fetcher published
    'Mount Cannibal Hike and Barbeque' -- a reserve forty kilometres from
    Springvale -- and the check then failed the build on it. Calling the
    fetcher's function is what makes "inside the catchment" mean one thing.
    """
    from fetch_urllib_sources import _passes_suburb_filter
    return _passes_suburb_filter(row, {a.lower() for a in allowed})


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

# WCAG 2.2 SC 2.5.8 Target Size (Minimum). Every filter is a checkbox, and
# there are dozens of suburbs and as many activity types, so this is the check
# that matters
# for this page.
MIN_TARGET_PX = 24.0

# The secondary layer of the table -- description, address, recurrence chip,
# badges, the two row buttons -- was sized in em inside a table already at
# .9em, so it compounded down: 12.7px, 11.8px, 11.8px, 10.8px and 12.2px on
# desktop. The mobile breakpoint had already been rebuilt in rem to fix exactly
# this, which left the wider and more common viewport carrying the defect the
# phone case was written for. Every selector below now has to declare a rem
# size, because a rem size is what stops a parent's font-size compounding into
# it -- and at least MIN_LEGIBLE_REM, or the chain returns one notch down.
MIN_LEGIBLE_REM = 0.75
REM_SIZED_SELECTORS = (
    ".desc", ".addr", ".recur", ".badge", ".ics-btn", ".src-link",
    ".qf-btn", ".reset-btn", "footer", ".count", ".pagination button",
    ".pagination .pg-info", ".typefilter", ".typefilter .thead", "table",
)

# Container elements whose nesting must balance. <span> and friends are inline
# and frequently unclosed, so only the containers that hold the layout are
# counted; an unbalanced <div> silently changes the shape of everything after
# it without failing any other check.
BALANCED_TAGS = ("div", "table", "thead", "tbody", "main", "nav", "select")


def _strip_code(source):
    """Markup with <style>, <script> and comments removed.

    A duplicated id or an unbalanced tag inside a string in the script block is
    not a duplicated element, and the JSON payload inlined into the page
    contains no tags at all -- so all three are removed before counting.
    """
    source = re.sub(r"<!--.*?-->", "", source, flags=re.S)
    source = re.sub(r"<style\b[^>]*>.*?</style>", "", source, flags=re.S | re.I)
    source = re.sub(r"<script\b[^>]*>.*?</script>", "", source, flags=re.S | re.I)
    return source


_MEDIA_QUERY_RE = re.compile(r"@media\b")


def _strip_media(css):
    """CSS with every @media block removed, leaving the unconditional rules.

    The mobile block legitimately overrides .tcheck to a 44px target, so a check
    that reads the whole stylesheet sees only the more permissive of the two and
    passes even when the desktop rule has been deleted or reduced to the native
    13px box. The desktop rules are the ones that were wrong.
    """
    out = []
    i = 0
    while True:
        m = _MEDIA_QUERY_RE.search(css, i)
        if not m:
            out.append(css[i:])
            break
        out.append(css[i:m.start()])
        depth, j = 0, css.index("{", m.end())
        k = j
        while k < len(css):
            if css[k] == "{":
                depth += 1
            elif css[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        i = k + 1
    return "".join(out)


def _rules_for(css, selector):
    """Bodies of every rule whose selector list contains `selector` as a whole.

    The lookahead is what makes this correct, twice over. `.badge` must not
    match the `.badge-*` colour rules, which carry no font-size of their
    own, and `.addr` must not match a `.addrlocation` -- renaming a selector to
    dodge the check would otherwise pass it silently. And `.tcheck` must not
    match `.tcheck input`, which is a descendant selector about the box rather
    than the label: only `,` or `{` may follow, never a further name.
    """
    pattern = re.escape(selector) + r"(?=\s*[,{])"
    return re.findall(pattern + r"[^{}]*\{([^}]*)\}", css)


def _duplicate_ids(source):
    """Yield (id, count) for every id appearing on more than one element."""
    source = _strip_code(source)
    counts = Counter(re.findall(r'\sid="([^"]+)"', source))
    return [(i, n) for i, n in sorted(counts.items()) if n > 1]


def _unbalanced_tags(source):
    """Describe the first unbalanced container tag, or return "".

    Self-closing and void elements are excluded. <col>/<colgroup> are balanced
    by CSS (display:none) rather than by markup, so <col ...> is not counted.
    """
    source = _strip_code(source)
    counts = {}
    for tag in BALANCED_TAGS:
        opens = len(re.findall(r"<%s\b[^>]*>" % tag, source, re.I))
        closes = len(re.findall(r"</%s>" % tag, source, re.I))
        if opens != closes:
            counts[tag] = (opens, closes)
    if not counts:
        return ""
    return ", ".join(f"<{t}> {o} open vs {c} close"
                     for t, (o, c) in sorted(counts.items()))


def _hidden_visibility_errors(css, source, name):
    """An element with `hidden` must not also be given a `display` value.

    The UA stylesheet's `[hidden]{display:none}` has specificity 0,0,1,0, so a
    single class rule is enough to beat it. That is not hypothetical: the filter
    panel was `.filterpanel{display:flex}` and `<div id="filterpanel" hidden>`,
    so it rendered open on every load while the toggle beside it reported
    aria-expanded="false" -- and pushed roughly 340px of viewport above the
    results. Nothing in a text check sees it, because both halves of the markup
    are individually correct.
    """
    errors = []
    markup = _strip_code(source)
    # (selector, class) for every element carrying a hidden attribute.
    hidden_classes = set()
    for tag in re.findall(r"<[a-z][a-z0-9]*\b[^>]*\shidden\b[^>]*>", markup, re.I):
        hidden_classes.update(re.findall(r'class="([^"]+)"', tag))
    for cls in sorted(hidden_classes):
        bodies = _rules_for(css, "." + cls)
        if not bodies:
            continue
        # _rules_for excludes [hidden] selectors, so anything here sets a
        # display value on the element in its visible state.
        declares_display = any(re.search(r"\bdisplay\s*:\s*(?!none)\b", b)
                                for b in bodies)
        guarded = any(re.search(r"\bdisplay\s*:\s*none\b", b)
                      for b in _rules_for(css, "." + cls + "[hidden]"))
        if declares_display and not guarded:
            errors.append(
                f"{name}: .{cls} carries the hidden attribute in the markup but "
                f"sets display in CSS, so [hidden] is overridden and the element "
                f"is always visible. Add .{cls}[hidden]{{display:none}}")
    return errors


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
                     r"\.tcheck input", r"\.sortpick select"):
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
        errors.append(f"{name}: body line-height {body_lh.group(1)} is under "
                      f"{MIN_LINE_HEIGHT} (WCAG 1.4.12 Text Spacing)")

    errors.extend(_rem_floor_errors(css, name))
    errors.extend(_target_size_errors(css, name))
    errors.extend(_hidden_visibility_errors(css, source, name))


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
                      "was on the sort headers, which mobile hides")

    return errors


def _rem_floor_errors(css, name):
    """The table's secondary text must not be sized in a compounding chain.

    An em length inside a parent that is itself sized in em multiplies: the
    table was at .9em, so .88em for the description rendered at 12.7px and
    .75em for a badge at 10.8px, and the mobile breakpoint had to be rewritten
    to fix exactly that while desktop kept it. Requiring a rem length is the
    check that catches it, because a rem length cannot compound -- whatever the
    parent computes to, the child is that many pixels.
    """
    errors = []
    for selector in REM_SIZED_SELECTORS:
        # The desktop rules only: inside @media the mobile block is free to
        # re-set these, and it is the unconditional chain that compounded.
        bodies = _rules_for(_strip_media(css), selector)
        if not bodies:
            errors.append(f"{name}: no rule for {selector}, so the table's "
                          "secondary text has no size floor at all")
            continue
        # A selector may be declared across several rules -- .desc carries its
        # clamp in one and its size in another -- so this asks whether the
        # selector ends up with a compliant size, not whether every rule that
        # mentions it does.
        declared = []
        for body in bodies:
            m = re.search(r"font-size\s*:\s*([\d.]+)\s*(rem|em|px|%)?", body)
            if m:
                declared.append((m.group(1), m.group(2) or "px"))
        if not declared:
            # Not a pass. Dropping the declaration is how an explicit floor gets
            # lost in the first place: the element silently inherits whatever the
            # chain happens to compute to, which is how a badge reached 10.8px.
            errors.append(f"{name}: {selector} declares no font-size, so it "
                          "inherits from the em chain above it. Give it an "
                          f"explicit rem size of at least {MIN_LEGIBLE_REM}rem")
            continue
        for size, unit in declared:
            value = float(size)
            if unit == "em":
                errors.append(f"{name}: {selector} is sized in {size}em, "
                              "which multiplies with its parent's font-size. "
                              "Use rem so the size cannot compound")
            elif unit != "rem":
                errors.append(f"{name}: {selector} is sized in {unit}, not rem, "
                              "so it cannot be checked against the floor")
            elif value < MIN_LEGIBLE_REM:
                errors.append(f"{name}: {selector} is {size}rem "
                              f"({value * DEFAULT_FONT_PX:.1f}px), under the "
                              f"{MIN_LEGIBLE_REM}rem floor")
    return errors


def _target_size_errors(css, name):
    """Filter checkboxes must have a target of at least 24x24 (WCAG 2.5.8).

    Every filter on this page is a checkbox -- one per suburb and one per
    activity type -- so the native 13px box was the whole hit target. The label
    wraps each one, and the label is what a click lands on, so the rule that
    matters is the label's min-height; the box size is checked too so that a
    label which later loses its wrapper is caught.
    """
    errors = []
    labels = _rules_for(_strip_media(css), ".tcheck")
    if not labels:
        # Not a count: it was 49 (31 suburbs + 18 types) when written, which is
        # wrong the moment a suburb or a type is added, and a number in an
        # error message is a number that lies.
        errors.append(f"{name}: no .tcheck rule, so none of the "
                      "suburb and activity-type filter checkboxes have a "
                      "declared target size")
        return errors
    for body in labels:
        m = re.search(r"min-height\s*:\s*([\d.]+)px", body)
        if not m:
            errors.append(f"{name}: .tcheck has no min-height, so a filter "
                          f"checkbox's target is the {MIN_TARGET_PX:.0f}px "
                          "box alone (WCAG 2.5.8)")
        elif float(m.group(1)) < MIN_TARGET_PX:
            errors.append(f"{name}: .tcheck min-height is {m.group(1)}px, "
                          f"under the {MIN_TARGET_PX:.0f}px target "
                          "(WCAG 2.5.8)")
    return errors


MIN_TOTAL = 700
# Generous floors (~25-50% of normal) for always-on sources.
# gd_libraries is low because dedupe_by_source_url() collapses the listings
# that greater_dandenong also scrapes from the same page, leaving only the
# events unique to that source.
# greater_dandenong itself was 10 against 16 published, which fails in November
# for working correctly: its listing rotates its events in and out, and the
# source is not broken when it has four of a season's sixteen still to run. The
# floor is here to catch a dead scraper, not a quiet week, so it is set well
# under one listing's worth of rows.
# ccc and chatty_cafe were both 10, which was below the noise: chatty_cafe lost
# six of its twenty venues to a broken schedule extractor and still cleared 10
# by a factor of 16, and ccc lost every session of a term but the first. Both
# are ~12 rows per listing, so a floor under ~150 cannot see a handful of
# listings disappear.
MIN_SOURCE = {
    "kingston_hubs": 200,
    "kingston_groups": 40,
    "bayside_live": 60,
    "greater_dandenong": 6,
    "ccc": 150,
    "chatty_cafe": 150,
    # 267 published rows today, from a listing of 301 over 31 pages that this
    # source now walks (see sources.yaml). The floor used to be 3, which could
    # not see anything: the source was reading page 1 only and publishing ten
    # rows, and a floor of 3 called that healthy for months while two live
    # events sat on pages 18 and 27. It is now set where a walk that stops
    # early fails -- page 1 alone is 10, and half the listing is ~130, so 150
    # catches a truncated pager while leaving room for a quiet month.
    "kingston_council": 150,
    # Still one page deep and still read one page at a time, so its floor is a
    # count of that page. It is small because the page is small.
    "kingston_arts": 3,
    "gd_libraries": 3,
    # Ten listed programmes, of which four are recurring runs of a dozen or more
    # dates and the rest are one-offs -- 89 rows today. The listing page is ten
    # cards deep and does not paginate, so the floor is a count of what is
    # there, not a fraction of a longer list: a drop to 40 means half the term's
    # dates stopped being found, which is the failure this catches.
    "frankston_libraries": 40,
}
# Sources whose row count is reported but not failed on: seasonal and archived
# ones legitimately reach zero. Only sources that are actually configured are
# checked -- a name here for a source that is not in sources.yaml would warn
# about zero rows forever, which is the noise a warn list should not produce.
# (frankston_live was here while its config entry was commented out, so every
# build printed "source frankston_live: 0 rows" for a source that did not exist.)
WARN_ONLY = {"kingston_seniors"}

# Sources that only exist in scripts/archived_events.json. They are not in
# sources.yaml, so _undeclared_source_floors() never sees them, and their rows
# are withheld from the page by build_site.py -- so a count check against the
# store would be checking rows nobody can reach, and a floor would fail the
# build the day the last of them is retired, which is the goal rather than a
# regression. Reported, never enforced.
# Same names, from dedupe, which owns the list: it is what treats an archived
# row's source as exempt when judging whether the source is still published.
# Two literals spelled the same way were one rename away from disagreeing, and
# the disagreement would have been silent -- one check counting rows the other
# did not exempt.
ARCHIVED_SOURCES = ARCHIVED_SOURCE_IDS


def _undeclared_source_floors():
    """Configured sources that no floor set accounts for.

    A source id absent from both MIN_SOURCE and WARN_ONLY gets no check at all,
    so it can quietly reach zero rows and the build stays green -- which is
    exactly what would have happened to kingston_groups had it not been added to
    one of them. The floor number itself is a judgement call; whether a source
    has one is not, so that part is asserted rather than remembered.
    """
    configured = _config_source_ids()
    return sorted(configured - set(MIN_SOURCE) - set(WARN_ONLY))


class Report(list):
    """The findings, with warnings kept apart from errors.

    Separate because only errors fail the build: a seasonal source legitimately
    reaching zero rows is information, and mixing the two lists would mean
    deciding per message whether it matters.
    """

    def __init__(self):
        super().__init__()
        self.warnings = []

    def error(self, message):
        self.append(message)

    def warn(self, message):
        self.warnings.append(message)


def check_total(rows, rep):
    if len(rows) < MIN_TOTAL:
        rep.error(f"total {len(rows)} < floor {MIN_TOTAL}")


def check_source_floors(rows, rep):
    labels = Counter(r.get("source_id", "unknown") for r in rows)
    undeclared = _undeclared_source_floors()
    if undeclared:
        rep.error(
            f"{len(undeclared)} configured source(s) have no row floor: "
            f"{', '.join(undeclared)} -- add each to MIN_SOURCE (year-round) "
            f"or WARN_ONLY (seasonal/static), or it can reach zero rows "
            f"without failing the build")
    for src, floor in MIN_SOURCE.items():
        if labels.get(src, 0) < floor:
            rep.error(f"source {src}: {labels.get(src, 0)} < floor {floor}")
    for src in WARN_ONLY:
        if labels.get(src, 0) == 0:
            rep.warn(f"source {src}: 0 rows (seasonal/static, ok)")
    for src in sorted(ARCHIVED_SOURCES):
        mine = [r for r in rows if r.get("source_id") == src]
        live_n = sum(1 for r in mine if r.get("archived") is False)
        print(f"archived source {src}: {len(mine)} row(s) in the store, "
              f"{live_n} listed (confirmed live), {len(mine) - live_n} withheld")
    # Warn-only above covers "out of season". A stale *config* is a different
    # fault and has to fail the build, or the whole festival disappears without
    # anyone noticing until October.
    rep.extend(seniors_config_errors())


def check_exact_duplicates(rows, rep):
    # Start time is part of the key: a venue can legitimately run the same
    # class twice in one day ('Cert II in EAL' Wed 9am and 12:30pm), so
    # collapsing on the date alone would report real sessions as duplicates.
    # This is dedupe.slot_hash() rather than a third transcription of it: the
    # check and the merge that enforces it must agree on what "the same slot"
    # means, or the check reports duplicates the merge considers distinct (or
    # the reverse) and neither names the disagreement.
    seen, dups = set(), 0
    for r in rows:
        key = slot_hash(r)
        if key in seen:
            dups += 1
        seen.add(key)
    if dups:
        rep.error(f"{dups} exact (name, start, location) duplicates")


def check_archive_flags(rows, rep):
    # An archived row must be flagged and must be in the store; a live row must
    # not be flagged. Asserted rather than assumed because the flag is the only
    # thing keeping delisted programmes off the page: set wrongly it hides a
    # bookable event, and missing it publishes an event no council admits to.
    # A row from an archived source is listed only when the fixture says the
    # series was confirmed still running, and must agree on both: `archived`
    # False without `live_confirmed` would mean a relisted live source, and
    # `live_confirmed` without `archived` False would mean the flag was computed
    # and then ignored by the build.
    listed = [r for r in rows
              if r.get("source_id") in ARCHIVED_SOURCES
              and r.get("archived") is False]
    for r in listed:
        if not r.get("live_confirmed"):
            rep.error(
                f"row from {r.get('source_id')} is on the page without "
                f"live_confirmed ({r.get('name')!r}) - an archived series is "
                f"listed only when the fixture marks it status: live")
    confirmed = [r for r in rows if r.get("live_confirmed")]
    stale = sorted({r.get("source_id") for r in confirmed
                    if r.get("archived") is not False})
    if stale:
        rep.error(
            f"rows from {', '.join(stale)} are marked live_confirmed but "
            f"withheld from the page")
    if not any(r.get("live_confirmed") for r in rows) and listed == []:
        rep.warn("no archived series is confirmed live - if the "
                 "liveness check has not been run, "
                 "scripts/apply_archive_fixes.py is the place to say so")
    stale_flag = [r for r in rows
                  if r.get("archived") is not False and "T" not in str(
                      r.get("datetime_iso") or "")]
    if stale_flag:
        rep.error(
            f"{len(stale_flag)} withheld row(s) with no date - every row on "
            f"the page is an event at a time, and an undated one is both "
            f"unlistable and the sign of a broken archive")


def check_same_listing_duplicates(rows, rep):
    # Same listing page reported by two scrapers. These differ only in how they
    # name the venue, so the exact check above cannot see them. The description
    # must agree, which keeps genuinely distinct events that share a page (two
    # STEADYstrength classes at two different halls on one CCC page).
    # The key is dedupe's own (name, url, stamp) -- the one
    # dedupe_by_source_url() merges on -- so this reports what that pass would
    # actually collapse rather than a near neighbour of it.
    listing_seen, listing_dups = {}, 0
    for r in rows:
        url = (r.get("source") or "").rstrip("/")
        stamp = str(r.get("datetime_iso") or "")[:16]
        name = name_head(r.get("name"))
        if not url or "T" not in stamp or not name:
            continue
        desc = " ".join((r.get("description") or "").lower().split())
        key = (name, url, stamp)
        prev = listing_seen.get(key)
        if prev is not None and prev == desc:
            listing_dups += 1
        else:
            listing_seen.setdefault(key, desc)
    if listing_dups:
        rep.error(f"{listing_dups} same-listing duplicates (one event "
                  f"page, two scrapers) - dedupe_by_source_url() regressed")


def check_inferred_time_matches_text(rows, rep):
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
        rep.error(
            f"{len(stale)} inferred rows whose stored time contradicts the "
            f"text: {', '.join(sorted(stale)[:5])}")


def check_inferred_dates_reproduce(rows, rep):
    # The rule in the check above compares the stored time against
    # `weekday_slots()`, the same parser that produced the stored time, so a
    # parser bug is self-validating: every one of these shipped a
    # correct-looking wrong date through a green build.
    #
    # This one re-expands the *whole* series and asks a different question:
    # is the row's own date still one the text produces at all? That is
    # independent of how the time was parsed, so a wrong day, a wrong phase
    # or a series that no longer exists all fail here. It is the only check
    # in the pipeline that can see a date that is simply the wrong day.
    today = reference_today()
    orphan, by_series = [], {}
    for r in rows:
        if not r.get("date_inferred"):
            continue
        # A row that has already happened is not a wrong date, it is a date
        # that has gone by, and the 90-day prune is deliberately far too slow to
        # notice. Comparing yesterday's session against a fresh expansion of
        # "every Tuesday, Wednesday and Thursday" always mismatches, because
        # yesterday is not in next week's run -- so this reported 18 rows that
        # were correct when they were written and are merely finished. The
        # check is about a row claiming a day its text cannot produce, which a
        # past day does not claim.
        stored = str(r.get("datetime_iso") or "")[:10]
        if stored and stored < today.isoformat():
            continue
        by_series.setdefault((r.get("name"), r.get("source")), []).append(r)
    for (name, source), group in by_series.items():
        # The series is identified by the text it was inferred from; any one
        # of its rows carries it. Re-infer from the row itself so the check
        # runs the same code the pipeline ran.
        made, _reason = infer_event(group[0], today)
        if not made:
            orphan.append(f"{name!r} no longer expands from its own text")
            continue
        allowed = {str(m.get("datetime_iso") or "")[:10] for m in made}
        for r in group:
            day = str(r.get("datetime_iso") or "")[:10]
            if day and day not in allowed:
                orphan.append(f"{name!r} stored {day}, text yields "
                              f"{sorted(allowed)[0]}..{sorted(allowed)[-1]}")
    if orphan:
        rep.error(
            f"{len(orphan)} inferred rows sit on a date their own text does "
            f"not produce: {', '.join(sorted(set(orphan))[:5])}. "
            f"re-run scripts/dedupe.py to re-infer.")


def check_same_programme_duplicates(rows, rep):
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
        rep.error(
            f"{prog_dups} same-programme duplicates (one session, two "
            f"titles) - dedupe_by_source_url() regressed")


def check_dateless(rows, rep):
    dateless = [r for r in rows if not r.get("datetime_iso")]
    if dateless:
        sample = ", ".join(sorted({(r.get("name") or "?")[:40]
                                   for r in dateless})[:5])
        rep.error(f"{len(dateless)} dateless rows (need a date to be "
                  f"placed on a calendar): {sample}")


def check_addresses(rows, rep):
    # A published row has to say where to go. This is not a cosmetic check: the
    # address is what the map link, the .ics LOCATION and the CSV export are
    # built from, so a missing one silently ships an event a reader cannot
    # locate. The failure this was written for was a *wrong* address rather
    # than a missing one -- fetch_kingston_hubs() fell back to a generic
    # ("Kingston Hubs", "Chelsea 3196") for any CalendarId it had no mapping
    # for, and published 300 rows of the Patterson Lakes calendar under a
    # Chelsea address and a Chelsea suburb. An empty address is at least
    # visible, so the invariant is that every non-online row has one.
    no_address = [r for r in rows
                  if not (r.get("address") or "").strip()
                  and needs_address(r)]
    if no_address:
        by_source = Counter(r.get("source_id", "unknown")
                            for r in no_address)
        sample = ", ".join(
            f"{r.get('name')!r} @ {(r.get('location') or '').strip()!r}"
            for r in no_address[:5])
        rep.error(
            f"{len(no_address)} rows have no address (only an event held "
            f"online may omit one): {dict(by_source)} e.g. {sample}")


def check_suburbs(rows, rep):
    # Every published suburb must be a gazetted Victorian locality in the
    # catchment set (scripts/vic_suburbs.py). extract_suburb() already refuses
    # to invent one, so a failure here means either a fetcher carried a
    # suburb through verbatim that was never validated, or the catchment has
    # genuinely grown and the list needs the newcomer. Either way the fix is
    # explicit: add the real locality, or fix the extractor -- never silently
    # publish a new filter checkbox.
    from vic_suburbs import is_known_suburb
    unknown_suburbs = sorted(
        {s for s in ((r.get("suburb") or "").strip() for r in rows)
         if s and not is_known_suburb(s)})
    if unknown_suburbs:
        rep.error(
            f"{len(unknown_suburbs)} suburbs are not gazetted localities in "
            f"vic_suburbs.py: {', '.join(unknown_suburbs[:8])} - add the real "
            f"locality or fix the extractor")


def check_gd_catchment(rows, rep):
    # The Greater Dandenong catchment is only enforceable if the fetcher got a
    # real venue: the listing cards carry none, so a regression that drops the
    # detail fetch leaves every row at the generic "Greater Dandenong" location
    # and the configured suburb_filter silently admits the whole city again.
    gd = [r for r in rows if r.get("source_id") == "greater_dandenong"]
    if not gd:
        return
    generic = sum(1 for r in gd
                  if (r.get("location") or "").strip().lower()
                  in ("greater dandenong", "greater dandenong libraries", ""))
    if generic > len(gd) // 2:
        rep.error(
            f"{generic}/{len(gd)} greater_dandenong rows have no real "
            f"venue - the detail-page fetch has regressed and the "
            f"suburb_filter is not filtering")
    try:
        _all = _config_entries()
        gd_cfg = next((s for s in _all if s.get("id")
                       == "greater_dandenong"), None) or {}
    except (OSError, yaml.YAMLError) as e:
        gd_cfg = {}
        rep.error(f"sources.yaml unreadable while checking the GD "
                  f"catchment: {e}")
    allowed = {a.lower() for a in (gd_cfg.get("suburb_filter") or [])}
    if allowed:
        # An online event has no suburb to be out of, so it is not a
        # catchment violation.
        physical = [r for r in gd if is_online(r.get("location")) is False]
        outside = sorted({(r.get("location") or "").strip()
                          for r in physical
                          if not _suburb_in(r, allowed)})
        if outside:
            rep.error(
                f"greater_dandenong has {len(outside)} venues outside the "
                f"configured catchment {sorted(allowed)}: "
                f"{', '.join(outside[:4])}")


def check_types_schema(rows, rep):
    # Multi-tag schema: every row carries a non-empty `types` array of known
    # tags, never the legacy single `type` string. Without this a build_site
    # regression would silently ship unfilterable rows (the UI's OR filter
    # reads `types` via getTypes()).
    _valid = set(TYPES)
    bad_shape = [r for r in rows
                 if not isinstance(r.get("types"), list) or not r.get("types")]
    if bad_shape:
        sample = ", ".join(sorted({(r.get("name") or "?")[:40]
                                   for r in bad_shape})[:5])
        rep.error(f"{len(bad_shape)} rows with missing/empty types array: "
                  f"{sample}")
        return
    unknown_tags = sorted({t for r in rows for t in r.get("types", [])
                           if t not in _valid})
    if unknown_tags:
        rep.error(f"rows carry unknown activity tags {unknown_tags} - "
                  f"add them to TYPES so the UI can filter them")
    # Two checks that used to live here were removed rather than kept:
    # "any row still carrying a legacy single `type`" is redundant, because
    # a store where build_site.py did not run already fails the empty-types
    # check above and the missing-hidden_by_default check in
    # check_published_flags(); and "'Other' mixed with real tags" is
    # structurally impossible, since classify_types returns ["Other"] iff
    # nothing matched. Neither could fail in any reachable state.


def check_store_is_justified(rows, rep):
    # The store must be exactly what the sources justify. Re-running the
    # pipeline's own reconciliation is the check: a row that would be dropped
    # now is a row the store is still carrying that no source backs, which is
    # how a corrected start time or a withdrawn listing survives as a phantom
    # the exact-duplicate checks cannot see.
    live = load_live_inputs(quiet=True)
    if live is None:
        rep.error("could not load source inputs to reconcile against - "
                  "did fetch_sources.py run?")
        return
    kept, dropped = reconcile_store(rows, live, reference_today(), report=False)
    if dropped:
        sample = ", ".join(
            f"{r.get('name')!r} {str(r.get('datetime_iso'))[:16]}"
            for r in dropped[:5])
        rep.error(
            f"{len(dropped)} rows in events.json are not backed by any "
            f"source (corrected time, or listing withdrawn) - "
            f"dedupe.py must run after the fetches: {sample}")


def check_published_flags(rows, rep):
    # A listing the venue has stopped selling, or a drop-in service rather
    # than a session, is hidden by default. If build_site.py did not run, the
    # keys are absent and the page would quietly show them all.
    unflagged = [r for r in rows if "hidden_by_default" not in r]
    if unflagged:
        rep.error(f"{len(unflagged)} rows have no hidden_by_default flag - "
                  f"did build_site.py run?")
        return
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
        rep.error(f"{len(stale_status)} rows whose sold-out/cancelled "
                  f"status disagrees with their own text: "
                  f"{', '.join(sorted(set(stale_status))[:5])}")
    if stale_service:
        rep.error(f"{len(stale_service)} rows whose drop-in-service "
                  f"flag disagrees with their own text: "
                  f"{', '.join(sorted(set(stale_service))[:5])}")
    unknown = {r.get("status") for r in rows
               if r.get("status") and r["status"] not in STATUS_LABELS}
    if unknown:
        rep.error(f"rows carry an unknown status {sorted(unknown)} - "
                  f"add it to STATUS_LABELS so the UI can label it")


def check_template(rows, rep):
    # Every source label needs a badge: CSS class + friendly-name entries,
    # or its badge renders as invisible white-on-white text.
    labels = Counter(r.get("source_id", "unknown") for r in rows)
    try:
        with open(ROOT / "src" / "templates" / "index.html", encoding="utf-8") as f:
            tpl = f.read()
    except FileNotFoundError as e:
        rep.error(f"template missing: {e}")
        return
    css = set(re.findall(r"\.badge-([a-z_]+)\{", tpl))
    for label in labels:
        if label == "unknown":
            continue
        if label not in css:
            rep.error(f"source {label}: missing .badge-{label} CSS")
        if not re.search(r"[{,]" + re.escape(label) + r":", tpl):
            rep.error(f"source {label}: missing friendly name in maps")

    # A duplicated template silently inlines the whole event array twice
    # and ships a page whose JS never runs.
    doctypes = tpl.lower().count("<!doctype")
    if doctypes != 1:
        rep.error(f"template has {doctypes} <!DOCTYPE> (want exactly 1)")
    if tpl.lower().count("</html>") != 1:
        rep.error(f"template has {tpl.lower().count('</html>')} </html> "
                  "(want exactly 1)")

    # A repeated id or an unbalanced container is invisible to every other
    # check here and to a regex scan of the script block, and it is not
    # hypothetical: an edit to the control bar once left a second copy of
    # the CSV and Filters buttons and a stray </div>, so getElementById
    # silently addressed the first of each and the page rendered both. Every
    # id in the page is unique by definition -- there is no legitimate
    # reason for two elements to share one.
    for element, dupes in _duplicate_ids(tpl):
        rep.error(f"template has {dupes} elements with id={element!r}; "
                  "ids must be unique (getElementById addresses only "
                  "the first)")
    unbalanced = _unbalanced_tags(tpl)
    if unbalanced:
        rep.error("template markup is unbalanced: " + unbalanced)

    for ph in PLACEHOLDERS:
        if ph not in tpl:
            rep.error(f"placeholder {ph} is missing from the template")
        elif ph in SINGLE_USE_PLACEHOLDERS and tpl.count(ph) != 1:
            rep.error(f"placeholder {ph} appears {tpl.count(ph)}x "
                      "(want exactly 1)")
    # A top-level call to an undefined function aborts the whole script
    # block before render() runs. This does NOT try to catch that by
    # scanning for names -- a regex cannot see a parse error, which is the
    # more likely fault. render_check.py executes the built page and
    # asserts it produced rows, which covers both.
    rep.extend(a11y_errors(tpl, "template"))


def check_built_page(rows, rep):
    # The built page is what users actually load; check the artefact too.
    try:
        with open(ROOT / "index.html", encoding="utf-8") as f:
            built = f.read()
    except FileNotFoundError:
        rep.error("index.html missing - did build_site.py run?")
        return
    n_doctype = built.lower().count("<!doctype")
    if n_doctype != 1:
        rep.error(f"index.html has {n_doctype} <!DOCTYPE> (want exactly 1)")
    for ph in PLACEHOLDERS:
        if ph in built:
            rep.error(f"index.html still contains {ph}")
    if built.lower().count("</html>") != 1:
        rep.error("index.html has a duplicated/partial document")
    # The template being accessible proves nothing about the page users
    # actually load: an uncommitted rebuild ships the old markup. This
    # ran the same battery against index.html and stayed green for a
    # build that had never been made.
    rep.extend(a11y_errors(built, "index.html"))


# Every check, in the order they run. The order is the order they used to be
# written in and it is load-bearing only for the report: the first failing
# check should be the one whose message tells you what to go and look at, and
# that is not always the first thing wrong with a store.
CHECKS = [
    check_total,
    check_source_floors,
    check_exact_duplicates,
    check_archive_flags,
    check_same_listing_duplicates,
    check_inferred_time_matches_text,
    check_inferred_dates_reproduce,
    check_same_programme_duplicates,
    check_dateless,
    check_addresses,
    check_suburbs,
    check_gd_catchment,
    check_types_schema,
    check_store_is_justified,
    check_published_flags,
    check_template,
    check_built_page,
]


def main():
    with open("data/events.json", encoding="utf-8") as f:
        data = json.load(f)
    rows = data.get("rows", [])

    rep = Report()
    for check in CHECKS:
        check(rows, rep)

    labels = Counter(r.get("source_id", "unknown") for r in rows)
    for w in rep.warnings:
        print(f"WARN: {w}")
    if rep:
        for e in rep:
            print(f"FAIL: {e}")
        sys.exit(1)
    print(f"health ok: {len(rows)} events, {len(labels)} sources, 0 dupes")


if __name__ == "__main__":
    main()
