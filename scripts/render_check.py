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
TEMPLATE = os.path.join("src", "templates", "index.html")

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


def run_browser(browser, target, profile):
    """Dump the rendered DOM of `target`. Returns stdout, or "" on failure."""
    cmd = [
        browser, "--headless", "--disable-gpu", "--no-sandbox",
        "--no-first-run", "--no-default-browser-check",
        "--user-data-dir=" + profile,
        "--virtual-time-budget=" + VIRTUAL_TIME_MS,
        "--dump-dom", target,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout or ""


def tbody_of(dom):
    match = re.search(r'<tbody id="rows">(.*?)</tbody>', dom, re.S)
    return match.group(1) if match else ""


def describe_js_error(browser, profile):
    """Re-render with an error handler to name the failing line.

    Only called after a failure, so the passing path stays fast.
    """
    src = open(INDEX, encoding="utf-8").read()
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
        count = re.search(r'id="count">([^<]*)', dom)
        count_text = count.group(1).strip() if count else ""
        if not re.match(r"Showing \d+-\d+ of \d+ events", count_text):
            errors.append("result count line not populated: %r" % count_text[:60])

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
            total = len(json.load(open(EVENTS, encoding="utf-8"))["rows"])
            header = re.search(r"(\d+)\s*events", dom)
            if header and int(header.group(1)) != total:
                print("NOTE: page header claims %s events but data/events.json "
                      "has %d; rebuild before trusting this."
                      % (header.group(1), total))
        except (OSError, ValueError, KeyError):
            pass

        print("render ok: %s rendered %d rows (%s)"
              % (os.path.basename(browser), n_rows, count_text))
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
