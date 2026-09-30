"""Smoke test: the built index.html must render a page containing events.

The build is Python, so nothing stops a JavaScript syntax error from reaching
the published page. That is not hypothetical: a missing closing paren in the
source-status block shipped a completely blank calendar while every other
check still passed -- `compileall` only reads Python, `health_check.py` only
reads events.json, and a regex scan for undefined names cannot see a parse
error. The only thing that catches it is executing the page.

This loads the real file in a headless browser and asserts the table has rows.
When it passes it costs about a second. When it fails it re-runs with an error
handler attached so the output names the actual JavaScript error and its line,
instead of just "no rows".

It also asserts the mobile accessibility invariants that health_check.py cannot
see, because they are a property of the DOM render() builds rather than of the
template text: real per-cell labels, a live region for the result count, and
calendar buttons whose accessible names name their event. The card layout used
to render a correct-looking table that VoiceOver and TalkBack read as an
unlabelled wall of text, and every static check passed while it did so.

Exits 0 on pass, 1 on failure, and 0 with a loud SKIP when no browser is
available -- an absent browser must not break a machine that has no GUI, but
the skip says so rather than passing quietly.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse

INDEX = "index.html"
EVENTS = os.path.join("data", "events.json")

# Enough for the page to load 1.6MB of inline JSON and run one render.
VIRTUAL_TIME_MS = "4000"

_BROWSER_ENV = "BROWSER"
_LINUX_NAMES = ("google-chrome-stable", "google-chrome", "chromium",
                "chromium-browser")
_WIN_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)
_MAC_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


def find_browser():
    """Locate a Chromium-family browser, or return None."""
    explicit = os.environ.get(_BROWSER_ENV)
    if explicit:
        return explicit if (os.path.isfile(explicit) or shutil.which(explicit)) else None
    for path in _WIN_PATHS + _MAC_PATHS:
        if os.path.isfile(path):
            return path
    for name in _LINUX_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def page_url(path):
    """file:// URL for a path, quoted so spaces do not truncate the argument."""
    return "file:///" + urllib.parse.quote(os.path.abspath(path).replace("\\", "/"))


def run_browser(browser, target, profile, size=None):
    """Dump the rendered DOM of `target`. Returns stdout, or "" on failure."""
    cmd = [
        browser, "--headless", "--disable-gpu", "--no-sandbox",
        "--no-first-run", "--no-default-browser-check",
        "--user-data-dir=" + profile,
        "--virtual-time-budget=" + VIRTUAL_TIME_MS,
    ]
    if size:
        cmd.append("--window-size=" + size)
    cmd += ["--dump-dom", target]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout or ""


def tbody_of(dom):
    # Attributes between the id and the closing bracket are expected: the
    # tbody carries role="rowgroup" so the mobile card layout does not strip
    # it, and a regex that pinned id="rows"> reported 0 rows for a page that
    # was rendering perfectly.
    match = re.search(r'<tbody id="rows"[^>]*>(.*?)</tbody>', dom, re.S)
    return match.group(1) if match else ""


# Markup the page must have in the *rendered* DOM for a phone screen reader
# to be usable. These are the assertions health_check.py cannot make: it reads
# the template's text, and the failures that matter most here happen in the
# string render() builds at runtime.
def dom_a11y_errors(dom, rows):
    errors = []

    # With thead visually hidden on mobile, the caption is the only thing
    # naming what the list is.
    if "<caption" not in dom:
        errors.append("rendered table has no <caption>")

    for node, why in (('id="count"', "result count"),
                      ("<main", "main landmark")):
        if node not in dom:
            errors.append(f"rendered page has no {why}")

    # role="status" is what makes a filter change announce itself; without it
    # a screen reader user is not told the list changed at all, and the
    # wholesale innerHTML replacement drops the virtual cursor regardless.
    count = re.search(r'<div[^>]*id="count"[^>]*>', dom)
    if count and 'role="status"' not in count.group(0):
        errors.append("result count is not a live region (role=\"status\"): "
                      "a filter change is never announced")
    if count and "aria-live" not in count.group(0):
        errors.append("result count has no aria-live")

    # Each data cell must carry its own label element. This is the assertion
    # that covers the original bug: the labels used to be ::before content,
    # which rendered correctly and was invisible to VoiceOver and TalkBack.
    # The day divider and the empty state span the full width and are not
    # data cells, so they are exempt.
    cells = re.findall(r"<td[^>]*>", rows)
    if cells:
        checked = unlabelled = 0
        for cell in cells:
            if "daydivider" in cell or "emptyrow" in cell:
                continue
            checked += 1
            end = rows.find("</td>", rows.find(cell) + len(cell))
            if 'class="celllabel"' not in rows[rows.find(cell):end]:
                unlabelled += 1
        if unlabelled:
            errors.append(
                f"{unlabelled} of {checked} rendered data cells have no "
                f".celllabel element")

    # One "+ Calendar" button per row is 50 identical labels unless each
    # names its event, and the name must contain the visible text or it
    # fails 2.5.3 Label in Name.
    btns = re.findall(r'<button class="ics-btn"[^>]*>([^<]*)</button>', rows)
    if not btns:
        errors.append("no .ics-btn calendar buttons rendered")
    else:
        ics = re.findall(r'<button class="ics-btn"([^>]*)>([^<]*)</button>', rows)
        unnamed = [v for attr, v in ics if "aria-label" not in attr]
        if unnamed:
            errors.append(
                f"{len(unnamed)} of {len(ics)} calendar buttons have no "
                f"aria-label naming their event")
        bad = []
        for attr, visible in ics:
            m = re.search(r'aria-label="([^"]*)"', attr)
            if m and visible.strip() and visible.strip() not in m.group(1):
                bad.append(visible.strip())
        if bad:
            errors.append(
                f"{len(bad)} calendar buttons have an aria-label that does not "
                f"contain their visible text (WCAG 2.5.3): {bad[0]!r}")

    # Sorting is the only thing thead did on mobile, and it is hidden there.
    m = re.search(r'<select[^>]*aria-label="Sort events by"[^>]*>(.*?)</select>',
                  dom, re.S)
    if not m:
        errors.append("no mobile sort control with aria-label=\"Sort events by\"")
    elif not re.findall(r"<option", m.group(1)):
        errors.append("mobile sort control has no options")

    return errors


# Smallest phone still in use. Everything in mobile_a11y_errors is measured
# here, because --dump-dom reports the DOM and not the layout: the unreadable
# 8.9px text, the horizontal overflow and the 300px sticky bar were all
# invisible to every other check in the pipeline.
#
# Windows will not give a headless window a narrower viewport than ~477 CSS px
# (the frame is subtracted from --window-size and then floored), so the real
# measurement is made at whatever clientWidth results and the assertion is
# written against that rather than against 375. It is still well inside the
# 768px breakpoint the card layout switches at, which is what these checks
# are about. The probe prints the width it actually got.
MOBILE_SIZE = "375,667"
# 1.4.4 Resize Text. Nothing legible in a list of 2000 events is under this.
MIN_LEGIBLE_PX = 12.0

_MOBILE_PROBE = """
<div id="mobileprobe">PENDING</div>
<script>
setTimeout(function(){
  var L = [];
  function cs(sel, prop) {
    var el = document.querySelector(sel);
    return el ? getComputedStyle(el)[prop] : null;
  }
  // A data cell, not the day divider that comes first in the tbody.
  var cell = document.querySelector('#rows .celllabel');
  cell = cell ? cell.parentElement : null;
  L.push('cell=' + (cell ? getComputedStyle(cell).fontSize : 'none'));
  L.push('label=' + cs('#rows .celllabel', 'fontSize'));
  L.push('desc=' + cs('#rows .desc', 'fontSize'));
  L.push('addr=' + cs('#rows .addr', 'fontSize'));
  L.push('badge=' + cs('#rows .badge', 'fontSize'));
  L.push('btn=' + cs('#rows .ics-btn', 'fontSize'));
  L.push('cellLabelDisplay=' + cs('#rows .celllabel', 'display'));
  var c = document.querySelector('.controls');
  L.push('controlsPos=' + getComputedStyle(c).position);
  L.push('controlsH=' + Math.round(c.getBoundingClientRect().height));
  L.push('docScrollW=' + document.documentElement.scrollWidth);
  // clientWidth, not innerWidth: innerWidth includes the vertical scrollbar
  // (~15px), so comparing against it would let a real 15px overflow pass.
  L.push('clientW=' + document.documentElement.clientWidth);
  L.push('innerW=' + window.innerWidth);
  L.push('sortVisible=' + (function(){
      var s = document.getElementById('sortpick');
      return s ? getComputedStyle(s.closest('.sortpick')).display : 'no select';
    })());
  L.push('thTabindex=' + (document.querySelector('th[data-k]') || {})
      .getAttribute('tabindex'));
  L.push('sortpickFont=' + (document.getElementById('sortpick')
      ? getComputedStyle(document.getElementById('sortpick')).fontSize : 'none'));
  document.getElementById('mobileprobe').textContent = L.join('|');
}, 2000);
</script>
"""


def measure_mobile(browser, profile):
    """Run the page at phone width with a layout probe attached.

    Returns {key: value} of the probe's output, or {} if it did not run.
    """
    with open(INDEX, encoding="utf-8") as f:
        src = f.read()
    src = src.replace("</body>", _MOBILE_PROBE + "</body>", 1)
    probe = os.path.join(profile, "mobile.html")
    with open(probe, "w", encoding="utf-8") as f:
        f.write(src)
    dom = run_browser(browser, page_url(probe), os.path.join(profile, "pm"),
                      size=MOBILE_SIZE)
    match = re.search(r'<div id="mobileprobe">(.*?)</div>', dom, re.S)
    if not match:
        return {}
    out = {}
    for pair in match.group(1).split("|"):
        if "=" in pair:
            key, _, value = pair.partition("=")
            out[key.strip()] = value.strip()
    return out


def mobile_a11y_errors(m):
    """Turn the phone-width measurements into failures.

    Every one of these was true of the page before this check existed, and
    all of them were invisible to a static read of the template.
    """
    if not m:
        return ["mobile layout probe did not run, so the phone layout is "
                "unverified"]

    errors = []

    # 1.4.4. The mobile card layout chained table .9em -> td .82em -> .desc
    # .9em, which put the description at 10.6px, the address and the buttons
    # in each card at 10px, and the badges and field labels at 8.9px.
    for key, what in (("cell", "table cell"), ("label", "cell field label"),
                      ("desc", "event description"), ("addr", "address"),
                      ("badge", "source badge"), ("btn", "card button")):
        value = m.get(key)
        if value and value.endswith("px"):
            px = float(value[:-2])
            if px < MIN_LEGIBLE_PX:
                errors.append(
                    f"on a phone the {what} renders at {px:.1f}px, under the "
                    f"{MIN_LEGIBLE_PX:.0f}px floor (WCAG 1.4.4)")

    # 1.4.10 Reflow: the page must not scroll sideways on a phone. The
    # recurrence chip is an inline-block inside a date cell that was
    # nowrap for the desktop grid, and kept on one line it ran 72px off the
    # right edge. Compared against clientWidth, not innerWidth, so the
    # scrollbar does not hide a small overflow.
    scroll_w, client_w = m.get("docScrollW"), m.get("clientW")
    if scroll_w and client_w and scroll_w.isdigit() and client_w.isdigit():
        if int(scroll_w) > int(client_w) + 1:
            errors.append(
                f"page scrolls sideways on a phone: {scroll_w}px of content in "
                f"a {client_w}px viewport (WCAG 1.4.10)")

    # The card layout's field labels are the whole point of the mobile
    # rewrite; if they stop rendering, a screen reader still has the
    # columnheader, but the sighted reader has nothing.
    if m.get("cellLabelDisplay") == "none":
        errors.append("cell field labels are hidden on mobile, so a card "
                      "shows unlabelled values")

    # A sticky control bar that is a third of a phone screen buries the list
    # it is meant to filter. This one measured ~298px of ~553px.
    if m.get("controlsPos") == "sticky":
        errors.append("the control bar is still position:sticky on mobile, "
                      "where it covered over half the viewport")

    if m.get("sortVisible") == "none":
        errors.append("sorting is unreachable on mobile: the sort control is "
                      "not displayed and thead is hidden")

    # The sort headers are tab stops that the mobile layout hides; leaving
    # them focusable means tabbing into an invisible control.
    if m.get("thTabindex") == "0":
        errors.append("sort headers are still in the tab order on mobile, "
                      "where thead is visually hidden")

    # iOS zooms on focus below 16px and never zooms back out.
    for key, what in (("sortpickFont", "sort control"),
                      ("cell", "table cell")):
        value = m.get(key)
        if value and value.endswith("px") and float(value[:-2]) < 16.0:
            errors.append(
                f"the {what} is {value} on mobile: iOS Safari zooms on focus "
                f"below 16px and never zooms back")

    return errors



def describe_js_error(browser, profile):
    """Re-render with an error handler to name the failing line.

    Only called after a failure, so the passing path stays fast.
    """
    with open(INDEX, encoding="utf-8") as f:
        src = f.read()
    handler = ("<script>window.__errs=[];"
               "window.onerror=function(m,s,l,c){window.__errs.push(m+' (line '+l+')');"
               "return false;};</script>")
    src = src.replace("<style>", handler + "<style>", 1)
    probe = ("<div id='probe'>PENDING</div><script>setTimeout(function(){"
             "document.getElementById('probe').textContent="
             "window.__errs.join(' | ')||'no error reported';},1500);</script>")
    src = src.replace("</body>", probe + "</body>", 1)
    tmp = os.path.join(profile, "probe.html")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(src)
    dom = run_browser(browser, page_url(tmp), os.path.join(profile, "p2"))
    match = re.search(r'<div id="probe">(.*?)</div>', dom, re.S)
    return match.group(1).strip() if match else "(no diagnostic returned)"


def main():
    if not os.path.isfile(INDEX):
        print(f"FAIL: {INDEX} not found - run scripts/build_site.py first")
        return 1
    html = open(INDEX, encoding="utf-8").read()
    if "__EVENTS_DATA__" in html or "__TYPE_CHECKBOXES__" in html:
        print(f"FAIL: {INDEX} still has unsubstituted template placeholders")
        return 1

    browser = find_browser()
    if not browser:
        print("SKIP: no Chromium-family browser found, cannot render the page.")
        print("      Install Chrome/Chromium/Edge or set $BROWSER to run this test.")
        print("      (On GitHub ubuntu-latest runners Chrome is preinstalled.)")
        return 0

    errors = []
    profile = tempfile.mkdtemp(prefix="rendercheck-")
    try:
        dom = run_browser(browser, page_url(INDEX), profile)
        if not dom:
            print(f"FAIL: browser produced no DOM ({os.path.basename(browser)})")
            return 1

        rows = tbody_of(dom)
        n_rows = len(re.findall(r"<tr", rows))
        if n_rows == 0:
            errors.append("table body rendered 0 rows")
        if "No events match" in rows:
            errors.append("page rendered the 'no events match' empty state")
        count = re.search(r'id="count"[^>]*>([^<]*)', dom)
        count_text = count.group(1).strip() if count else ""
        if not re.match(r"Showing \d+-\d+ of \d+ events", count_text):
            errors.append("result count line not populated: %r" % count_text[:60])

        # A page can render a perfect-looking list that no screen reader can
        # navigate, so these run on every pass rather than on failure.
        errors.extend(dom_a11y_errors(dom, rows))

        # Measure the phone layout too. None of the failures this catches are
        # visible in the DOM: 8.9px text, a 173px sideways overflow and a
        # control bar covering half the screen all rendered a page that every
        # other check in the pipeline called correct.
        mobile = measure_mobile(browser, profile)
        errors.extend(mobile_a11y_errors(mobile))

        if errors:
            print("FAIL: the built page did not render results")
            for e in errors:
                print("  - %s" % e)
            print("  browser said: %s" % describe_js_error(browser, profile))
            return 1

        # A stale page is a passing test of the wrong artefact, so say so.
        # Compare against the header's generated total, not the visible row
        # count: the default filters legitimately hide past and commercial
        # events, so the table is always smaller than the dataset.
        try:
            import json
            with open(EVENTS, encoding="utf-8") as jf:
                total = len(json.load(jf)["rows"])
            header = re.search(r"(\d+)\s*events", dom)
            if header and int(header.group(1)) != total:
                print("NOTE: page header claims %s events but data/events.json "
                      "has %d; rebuild before trusting this."
                      % (header.group(1), total))
        except (OSError, ValueError, KeyError):
            pass

        print("render ok: %s rendered %d rows (%s)"
              % (os.path.basename(browser), n_rows, count_text))
        if mobile:
            print("  mobile %s: cell %s, label %s, desc %s, controls %s (%s), "
                  "sort %s, width %s/%s"
                  % (MOBILE_SIZE, mobile.get("cell"), mobile.get("label"),
                     mobile.get("desc"), mobile.get("controlsPos"),
                     mobile.get("controlsH"), mobile.get("sortVisible"),
                     mobile.get("docScrollW"), mobile.get("clientW")))
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
