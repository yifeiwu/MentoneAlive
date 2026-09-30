"""Generate the offline index.html from events.json + template."""
import html
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from activity_types import TYPES, classify_types
from commercial import is_commercial
from jsonio import write_json
from status import STATUS_LABELS, event_status, is_ongoing_service
from venues import STREET_SUFFIX_WORDS
from vic_suburbs import canonical_suburb

ROOT = Path(__file__).resolve().parent.parent

# Only these may be published as a link target. A javascript:/data: URL in
# row["source"] would otherwise be inlined verbatim and reach an href, where
# HTML-escaping does not neutralise the scheme.
SAFE_SCHEMES = ("http://", "https://", "mailto:")
# Same-root relative links and in-page anchors. Note "/" cannot simply join
# SAFE_SCHEMES: "//evil.example/x" is protocol-relative and would resolve to
# another origin, so a leading "/" is only allowed when not doubled.
SAFE_PREFIXES = ("/", "#")

# The state token that follows the suburb in an Australian address. The old
# postcode fallback matched the last word before the postcode, which for
# "..., Beaumaris, Victoria 3193" is the *state* -- so 131 rows got a suburb
# of "Victoria". Suburbs that are also state names are not a thing in VIC, so
# treating these as non-suburbs is safe.
_STATE_TOKENS = {"victoria", "vic", "vics", "australia", "nsw", "new south wales",
                 "queensland", "qld", "sa", "south australia", "tas", "tasmania",
                 "nt", "wa", "western australia", "act"}


def _safe_url(value):
    """Return the URL if it uses an acceptable scheme, else an empty string."""
    url = (value or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        # Protocol-relative: inherits the page scheme but points at another
        # origin, which is exactly the exfiltration the scheme check exists to
        # prevent. Reject before the relative-prefix test below.
        return ""
    low = url.lower()
    if url.startswith(SAFE_PREFIXES) or low.startswith(SAFE_SCHEMES):
        return url
    return ""


def extract_suburb(address):
    """Extract Australian suburb from an address string.

    Anchored on the postcode, which is the reliable token, and reads the
    token(s) immediately before it. The previous version matched the last
    word before the postcode, so a well-formed address such as
    "Beaumaris Library, 96 Reserve Road, Beaumaris, Victoria 3193" yielded
    "Victoria" -- the state -- as the suburb. Reading the whole segment
    before the state (and refusing state names outright) fixes that.

    The bare-postcode fallback is anchored to the end of the string and only
    accepts a Victorian postcode (3xxx). Unanchored, any four digits --
    a street number ("1218 Nepean Highway"), a year ("workshops during
    2026") or a phone fragment ("Community Connections 1300") -- read as
    a postcode and the comma-segment before it published as a suburb.

    Every candidate is validated against vic_suburbs.KNOWN_SUBURBS (gazetted
    Victorian localities for the catchment). An unknown candidate returns ""
    rather than publishing a venue name or parsing artefact as a suburb; the
    health check then names the newcomer so a real locality can be added.
    """
    a = (address or "").strip()
    if not a:
        return ""

    # "... VIC 3192" / "... Victoria 3193" -> capture everything before the
    # state token. Non-greedy avoids a trailing comma in the capture, and
    # since the match must end at the state+postcode there is only one place
    # it can anchor, so first vs last is not the issue.
    m = re.search(r"^(.+?)[,\s]+(?:VIC|Victoria)\.?\s+(3\d{3})\b", a, re.I)
    if m:
        head = m.group(1)
        # Take the final comma-delimited segment: that is the suburb, while
        # the earlier ones are the venue and street.
        seg = re.split(r",", head)[-1].strip()
        if (seg and seg.lower() not in _STATE_TOKENS
                and not _is_street(seg) and not re.search(r"\d", seg)):
            return canonical_suburb(seg)
        return ""

    # "..., Frankston, VIC" -- state present, no postcode. Same rule.
    m = re.search(r"^(.+?)[,\s]+(?:VIC|Victoria)\s*$", a, re.I)
    if m:
        seg = re.split(r",", m.group(1))[-1].strip()
        if (seg and seg.lower() not in _STATE_TOKENS
                and not _is_street(seg) and not re.search(r"\d", seg)):
            return canonical_suburb(seg)
        return ""

    # No state token at all. Fall back to a bare postcode, but only when it
    # ends the address and looks like a Victorian postcode -- "Patterson Lakes
    # Community Centre, 2-30 Thompson Rd, Patterson Lakes 3198" ends in the
    # postcode and its last token is a street, so guard against that. Without
    # the end anchor and the 3xxx requirement, a house number ("Cheltenham
    # Hall, 1218 Nepean Highway, Cheltenham"), a year ("..., workshops
    # during 2026") or a phone fragment ("..., Community Connections 1300")
    # all read as postcodes.
    m = re.search(r"^(.+?)[,\s]+(3\d{3})\s*[.,]?\s*(?:,\s*Australia\s*)?$", a, re.I)
    if m:
        seg = re.split(r",", m.group(1))[-1].strip()
        if (seg and seg.lower() not in _STATE_TOKENS
                and not _is_street(seg) and not re.search(r"\d", seg)):
            return canonical_suburb(seg)
        return ""

    # No postcode or state at all, but a street plus a trailing suburb:
    # "Cheltenham Hall, 1218 Nepean Highway, Cheltenham" states the suburb
    # plainly. Accept the last comma segment when an earlier segment looks
    # like a street and the last looks like a suburb (no digits, not a
    # state, not a street). Without the street requirement, a bare venue
    # name ("Kingston Arts Centre") would publish as its own suburb; without
    # the no-digits requirement, a title plus a year ("..., workshops
    # during 2026") would do the same.
    segs = [s.strip().strip(".") for s in re.split(r",", a) if s.strip()]
    if len(segs) >= 2:
        last = segs[-1]
        if (2 <= len(last) <= 40 and last.lower() not in _STATE_TOKENS
                and not _is_street(last) and not re.search(r"\d", last)
                and re.fullmatch(r"[A-Za-z][A-Za-z .'\-]*", last)
                and any(re.match(r"^\s*\d", s) or _is_street(s)
                        for s in segs[:-1])):
            return canonical_suburb(last)
    return ""


_STREET_TAIL = re.compile(
    r"\b(" + STREET_SUFFIX_WORDS + r")\b\.?$", re.I)


def _is_street(seg):
    """True when a comma-segment is a street/road name, not a suburb."""
    return bool(_STREET_TAIL.search(seg.strip()))


def _strip_build_comments(source):
    """Drop the code's own prose from a built page, keeping the page working.

    The template carries ~25 KB of rationale for the CSS and JS, and that same
    rationale also lives in docs/decisions.md and in the check docstrings. It is
    worth keeping in the repo and not worth shipping to every reader of the
    page.

    The rules are deliberately conservative, because the inlined JSON payload
    arrives by `replace()` and a naive strip would corrupt it:

    - CSS block comments, whole-block. Safe here because no CSS string literal
      contains `/*` -- the only `content:` values are `center`.
    - JS line comments, but only whole lines whose first non-space character is
      `//`. Trailing comments are left alone, because the ICS PRODID is
      `"PRODID:-//Events Index//EN"` and a rule that could match mid-line would
      be one regex away from rewriting it.
    - HTML comments.

    Call this on the template, before any payload is substituted.
    """
    def _style(m):
        return m.group(1) + re.sub(r"/\*.*?\*/", "", m.group(2), flags=re.S) \
            + m.group(3)

    def _script(m):
        kept = [l for l in m.group(2).split("\n") if not l.lstrip().startswith("//")]
        return m.group(1) + "\n".join(kept) + m.group(3)

    source = re.sub(r"(<style\b[^>]*>)(.*?)(</style>)", _style, source,
                    flags=re.S | re.I)
    source = re.sub(r"(<script\b[^>]*>)(.*?)(</script>)", _script, source,
                    flags=re.S | re.I)
    return re.sub(r"<!--.*?-->", "", source, flags=re.S)


def main():
    with open(ROOT / "data" / "events.json", encoding="utf-8") as f:
        data = json.load(f)

    rows = data.get("rows", [])
    print(f"Building site with {len(rows)} events...")

    for r in rows:
        r["types"] = classify_types(r.get("name", ""), r.get("description") or "", r.get("source_label", ""))
        r.pop("type", None)
        # A fetcher that opened the event's own page already knows where it
        # is: Greater Dandenong states the suburb on the detail page, and its
        # addresses carry no "VIC ####" for extract_suburb to anchor on, so
        # deriving it here would throw that away and leave the row suburb-less.
        # Either way the suburb is validated against the gazetted list, so a
        # fetcher typo or a parsing artefact publishes as "" rather than as a
        # filter checkbox nobody can act on.
        r["suburb"] = canonical_suburb(r.get("suburb") or "") or extract_suburb(
            r.get("address") or r.get("location") or "")
        flag, reason = is_commercial(r)
        r["is_commercial"] = flag
        r["commercial_reason"] = reason
        # Two more reasons a reader should not have to discover on arrival:
        # the venue has stopped bookings, or the listing is a drop-in service
        # with an opening window rather than a session to attend. They are
        # kept as separate fields because "this is a pub promotion" and "this
        # is the centre's meal service" are different facts, but they share
        # one default-hidden control in the UI.
        status, detail = event_status(r)
        r["status"] = status
        r["status_detail"] = detail
        r["status_label"] = STATUS_LABELS.get(status, "")
        service, service_reason = is_ongoing_service(r)
        r["is_service"] = service
        r["service_reason"] = service_reason
        r["hidden_by_default"] = bool(flag or service or status == "cancelled")
        r["source"] = _safe_url(r.get("source"))
        r["sources"] = [_safe_url(u) for u in (r.get("sources") or [])]
        r["sources"] = [u for u in r["sources"] if u]

    data["rows"] = rows
    write_json(ROOT / "data" / "events.json", data)
    print(f"Wrote data/events.json with types + commercial "
          f"({sum(1 for r in rows if r.get('is_commercial'))} commercial, "
          f"{sum(1 for r in rows if r.get('is_service'))} services, "
          f"{sum(1 for r in rows if r.get('status'))} sold out / fully booked)")

    type_counts = Counter(t for r in rows for t in r.get("types", ["Other"]))
    # Keep checkbox order stable and in TYPES order (not alphabetical), so the
    # filter list reads as a curated taxonomy rather than reshuffling.
    _order = {t: i for i, t in enumerate(TYPES)}
    types = sorted(type_counts.keys(), key=lambda t: _order.get(t, 999))
    sources = sorted({r.get("source_label", "unknown") for r in rows})

    template_path = ROOT / "src" / "templates" / "index.html"
    with open(template_path, encoding="utf-8") as f:
        template = _strip_build_comments(f.read())

    # Only the fields the page reads, and only when they carry something.
    # `sources`, `datetime_display`, `datetime_text`, `has_real_date`,
    # `source_id` and `date_text` are pipeline-internal: a reader of
    # data/events.json gets them from the committed store, but inlining all of
    # them for every row is ~100 KB of page nobody can search.
    PAGE_FIELDS = (
        "name", "datetime_iso", "location", "address", "suburb", "description",
        "price_text", "price_sort", "types", "source", "source_label",
        "date_inferred", "recurrence", "is_commercial", "commercial_reason",
        "is_service", "service_reason", "status", "status_label",
        "status_detail", "hidden_by_default",
    )
    # A false flag is worth omitting (1513 of 1522 rows carry one, and every
    # read of all three is guarded, so absent already means false). A zero is
    # NOT: `price_sort == 0` is 514 rows and is how `isFree()` recognises a
    # free event, so it is kept. Note that `value in ("", [], None, False)`
    # cannot express that -- `0.0 == False` in Python, so it would drop every
    # free row's price.
    FLAGS = ("is_commercial", "is_service", "hidden_by_default")

    def _keep(field, value):
        if value is None or value == "" or value == []:
            return False
        if field in FLAGS and value is False:
            return False
        return True

    payload = [{k: r[k] for k in PAGE_FIELDS if k in r and _keep(k, r[k])}
               for r in rows]

    # Inlined into a <script> block: "</" prevents a </script> breakout. The
    # U+2028/U+2029 escapes are not needed -- both became legal in JavaScript
    # string literals in ES2019, and every browser since accepts them raw.
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    html_out = template.replace("__EVENTS_DATA__", data_json)
    html_out = html_out.replace("__GENERATED_AT__", datetime.now().strftime("%Y-%m-%d %H:%M"))
    html_out = html_out.replace("__EVENT_COUNT__", str(len(rows)))
    html_out = html_out.replace("__SOURCE_COUNT__", str(len(sources)))

    type_html = ""
    for t in types:
        t_esc = html.escape(t, quote=True)
        type_html += f'<label class="tcheck"><input type="checkbox" data-type="{t_esc}" checked> {html.escape(t)} ({type_counts[t]})</label>\n'
    html_out = html_out.replace("__TYPE_CHECKBOXES__", type_html)

    with open(ROOT / "index.html", "w", encoding="utf-8") as f:
        f.write(html_out)

    print(f"Wrote index.html ({len(html_out)} bytes)")


if __name__ == "__main__":
    import sys as _sys
    if "--test" in _sys.argv:
        # Suburb extraction cases. Run by `python scripts/build_site.py
        # --test` and by checks.py, so a change to the address patterns
        # fails the build before a venue name or a year can publish as a
        # suburb again.
        _TESTS = [
            # Well-formed addresses keep working.
            ("VIC with postcode",
             extract_suburb("Beaumaris Library, 96 Reserve Road, Beaumaris, "
                            "Victoria 3193"),
             "Beaumaris"),
            ("bare postcode at the end",
             extract_suburb("64 Parkers Road, Parkdale 3195"),
             "Parkdale"),
            ("venue plus street plus suburb, no postcode",
             extract_suburb("Cheltenham Hall, 1218 Nepean Highway, Cheltenham"),
             "Cheltenham"),
            # A street number is not a postcode: the old unanchored fallback
            # read "1218" and published the venue as the suburb.
            ("a street number is not a postcode",
             extract_suburb("Cheltenham Hall, 1218 Nepean Highway, "
                            "Cheltenham VIC 3192"),
             "Cheltenham"),
            # A year is not a postcode, so a title plus a year publishes no
            # suburb rather than "workshops during".
            ("a year is not a suburb",
             extract_suburb("Stitch with Sappho, workshops during 2026"),
             ""),
            # A phone prefix is not a postcode, so a contact block publishes
            # no suburb rather than "Community Connections".
            ("a phone prefix is not a suburb",
             extract_suburb("Contact, Community Connections 1300"),
             ""),
            # A bare venue name is not a suburb.
            ("a venue name alone is not a suburb",
             extract_suburb("Kingston Arts Centre"),
             ""),
            # A state name is never a suburb.
            ("a state is not a suburb",
             extract_suburb("Gardiners Creek and Anniversary Trail Loop Walk, "
                            "Victoria"),
             ""),
            # A well-formed address in an unknown locality publishes no
            # suburb rather than a new filter checkbox: the newcomer is for
            # the curator to add to vic_suburbs.py, not for the parser to
            # invent. (Sydney is not in the catchment set.)
            ("an unknown locality is not a suburb",
             extract_suburb("1 Example St, Sydney NSW 2000"),
             ""),
            ("a misspelt catchment suburb is not silently kept",
             extract_suburb("8 Chesterville Rd, Cheltenahm VIC 3192"),
             ""),
            # Lookup is case-insensitive but publishes the gazetted spelling,
            # so the suburb column stays stable however a source capitalised it.
            ("casing is canonicalised",
             extract_suburb("8 Chesterville Rd, CHELTENHAM VIC 3192"),
             "Cheltenham"),
        ]
        _failures = []
        for _label, _actual, _expected in _TESTS:
            if _actual == _expected:
                print(f"ok   {_label}")
            else:
                print(f"FAIL {_label}\n       actual:   {_actual!r}"
                      f"\n       expected: {_expected!r}")
                _failures.append(_label)
        if _failures:
            print(f"\nbuild_site: {len(_failures)}/{len(_TESTS)} cases FAILED")
            raise SystemExit(1)
        print(f"\nall {len(_TESTS)} build_site suburb cases as expected")
    else:
        main()
