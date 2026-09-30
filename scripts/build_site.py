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
    if url.startswith(SAFE_PREFIXES) or url.startswith(SAFE_SCHEMES):
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
    """
    a = (address or "").strip()
    if not a:
        return ""

    # "... VIC 3192" / "... Victoria 3193" -> capture everything before the
    # state token. Non-greedy so the *last* state+postcode is used, not the
    # first, since an address can name two suburbs ("Beaumaris, Beaumaris").
    m = re.search(r"^(.+?)[,\s]+(?:VIC|Victoria)\.?\s+\d{4}\b", a, re.I)
    if m:
        head = m.group(1)
        # Take the final comma-delimited segment: that is the suburb, while
        # the earlier ones are the venue and street.
        seg = re.split(r",", head)[-1].strip()
        if seg and seg.lower() not in _STATE_TOKENS and not _is_street(seg):
            return seg
        return ""

    # "..., Frankston, VIC" -- state present, no postcode. Same rule.
    m = re.search(r"^(.+?)[,\s]+(?:VIC|Victoria)\s*$", a, re.I)
    if m:
        seg = re.split(r",", m.group(1))[-1].strip()
        if seg and seg.lower() not in _STATE_TOKENS and not _is_street(seg):
            return seg
        return ""

    # No state token at all. Fall back to a bare postcode when present, but
    # only accept a short non-street segment -- "Patterson Lakes Community
    # Centre, 2-30 Thompson Rd, Patterson Lakes 3198" ends in the postcode and
    # its last token is a street, so guard against that.
    m = re.search(r"^(.+?)[,\s]+(\d{4})\b", a)
    if m:
        seg = re.split(r",", m.group(1))[-1].strip()
        if seg and seg.lower() not in _STATE_TOKENS and not _is_street(seg):
            return seg
    return ""


_STREET_TAIL = re.compile(
    r"\b(Road|Rd|Street|St|Avenue|Ave|Highway|Pde|Parade|Drive|Dr|Lane|Ln|Place|"
    r"Pl|Square|Sq|Terrace|Court|Ct|Boulevard|Blvd|Walk|Crescent|Cres|Close|"
    r"Way|Trail|Parkway|Circuit|Cct|Promenade|Prom|Esplanade)\b\.?$", re.I)


def _is_street(seg):
    """True when a comma-segment is a street/road name, not a suburb."""
    return bool(_STREET_TAIL.search(seg.strip()))


def main():
    with open(ROOT / "data" / "events.json", encoding="utf-8") as f:
        data = json.load(f)

    rows = data.get("rows", [])
    print(f"Building site with {len(rows)} events...")

    for r in rows:
        r["types"] = classify_types(r.get("name", ""), r.get("description") or "")
        r.pop("type", None)
        # A fetcher that opened the event's own page already knows where it
        # is: Greater Dandenong states the suburb on the detail page, and its
        # addresses carry no "VIC ####" for extract_suburb to anchor on, so
        # deriving it here would throw that away and leave the row suburb-less.
        r["suburb"] = (r.get("suburb") or "").strip() or extract_suburb(
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
        r["sources"] = [u for u in (r.get("sources") or []) if _safe_url(u)]

    data["rows"] = rows
    data["type_counts"] = dict(Counter(
        t for r in rows for t in (r.get("types") or ["Other"])))
    data["commercial_count"] = sum(1 for r in rows if r.get("is_commercial"))
    data["service_count"] = sum(1 for r in rows if r.get("is_service"))
    data["unavailable_count"] = sum(1 for r in rows if r.get("status"))
    write_json(ROOT / "data" / "events.json", data)
    print(f"Wrote data/events.json with types + commercial "
          f"({data['commercial_count']} commercial, {data['service_count']} "
          f"services, {data['unavailable_count']} sold out / fully booked)")

    type_counts = Counter(t for r in rows for t in r.get("types", ["Other"]))
    # Keep checkbox order stable and in TYPES order (not alphabetical), so the
    # filter list reads as a curated taxonomy rather than reshuffling.
    _order = {t: i for i, t in enumerate(TYPES)}
    types = sorted(type_counts.keys(), key=lambda t: _order.get(t, 999))
    sources = sorted({r.get("source_label", "unknown") for r in rows})

    template_path = ROOT / "src" / "templates" / "index.html"
    with open(template_path, encoding="utf-8") as f:
        template = f.read()

    data_json = json.dumps(rows, ensure_ascii=False)
    # Inlined into a <script> block: "</" prevents a </script> breakout, and
    # U+2028/U+2029 are JavaScript line terminators in string literals before
    # ES2019, which would break the parse for the whole page.
    data_json = (data_json.replace("</", "<\\/")
                 .replace("\u2028", "\\u2028")
                 .replace("\u2029", "\\u2029"))

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
    main()
