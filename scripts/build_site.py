"""Generate the offline index.html from events.json + template."""
import html
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from activity_types import TYPES, classify_types
from commercial import is_commercial
from jsonio import write_json
from status import STATUS_LABELS, event_status, is_ongoing_service
from venues import extract_suburb
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


# extract_suburb() moved to venues.py: the fetch layer needs it for the catchment
# filter and the render layer needs it to publish, and it was living here so
# that fetch_urllib_sources.py had to import a rendering module.

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


def on_page(row):
    """True when a store row belongs on the page.

    Archived rows are the delisted series kept in data/events.json as the record
    that they ever existed -- a weekly farmers' market is worth keeping -- but
    they are not events anybody can attend, so they stay off the list.

    `is not True`, not a truth test: the flag is written by dedupe.py as a
    boolean, and an unconfirmed or confirmed-live series is carried by `False`
    or by the flag's absence. A truth test would withhold a row whose `archived`
    happened to be a non-empty string or a non-zero number, and none of the
    three readers of this rule (this function, the count below, and the test)
    should be able to disagree about which rows that is.
    """
    return row.get("archived") is not True


def main():
    with open(ROOT / "data" / "events.json", encoding="utf-8") as f:
        data = json.load(f)

    # Delisted series stay in data/events.json -- they are the only record that
    # a weekly farmers' market or a library course ever existed -- but they are
    # not events anybody can attend, so they are kept off the list. The split
    # is made after every row is annotated, so the store is a uniform set and
    # an archived row is described in the same terms as a live one.
    every_row = data.get("rows", [])
    print(f"Building site with {len(every_row)} events...")

    for r in every_row:
        # `source_types` is what the source called the row itself: Kingston's
        # community-groups directory states a curated category on every entry.
        # That is the council classifying its own listing, which beats anything
        # inferred from the prose -- see SOURCE_TAXONOMY for the cases where
        # inference was actively worse than useless.
        r["types"] = classify_types(r.get("name", ""),
                                     r.get("description") or "",
                                     r.get("source_id", ""),
                                     r.get("source_types"))
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

    data["rows"] = every_row
    write_json(ROOT / "data" / "events.json", data)
    n_arch = sum(1 for r in every_row if r.get("archived"))
    print(f"Wrote data/events.json with types + commercial "
          f"({sum(1 for r in every_row if r.get('is_commercial'))} commercial, "
          f"{sum(1 for r in every_row if r.get('is_service'))} services, "
          f"{sum(1 for r in every_row if r.get('status'))} sold out / fully booked"
          + (f", {n_arch} archived" if n_arch else "") + ")")

    # Withheld before the page's own counts are taken, so the filter list and
    # the source list describe what a reader can actually act on. An archived
    # source contributing checkboxes would offer filters that match nothing.
    rows = [r for r in every_row if on_page(r)]
    print(f"  {len(rows)} rows on the page"
          + (f", {len(every_row) - len(rows)} archived rows withheld"
             if len(every_row) != len(rows) else ""))

    type_counts = Counter(t for r in rows for t in r.get("types", ["Other"]))
    # Keep checkbox order stable and in TYPES order (not alphabetical), so the
    # filter list reads as a curated taxonomy rather than reshuffling.
    _order = {t: i for i, t in enumerate(TYPES)}
    types = sorted(type_counts.keys(), key=lambda t: _order.get(t, 999))
    sources = sorted({r.get("source_id", "unknown") for r in rows})

    template_path = ROOT / "src" / "templates" / "index.html"
    with open(template_path, encoding="utf-8") as f:
        template = _strip_build_comments(f.read())

    # Only the fields the page reads, and only when they carry something.
    # `sources`, `datetime_text`, `series_id`, `source_types` and `date_text`
    # are pipeline-internal: a reader of data/events.json gets them from the
    # committed store, but inlining all of them for every row is ~100 KB of page
    # nobody can search. `source_id` IS inlined, under what used to be the
    # identically-valued `source_label`.
    PAGE_FIELDS = (
        "name", "datetime_iso", "location", "address", "suburb", "description",
        "price_text", "price_sort", "types", "source", "source_id",
        "date_inferred", "recurrence", "is_commercial", "commercial_reason",
        "is_service", "service_reason", "status", "status_label",
        "status_detail", "hidden_by_default",
    )
    # A false flag is worth omitting: only 13 of 1531 rows carry a true one, and
    # every read of all three is guarded, so absent already means false. A zero
    # is NOT: `price_sort == 0` is 514 rows and is how `isFree()` recognises a
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

    # A row's provenance must not reach the reader. `archived` is how this
    # project obtained a listing, and it was being printed as a badge suffix --
    # "Bayside (archived)", "Frankston (archived)" -- which offered a third
    # state that does not exist for anyone reading the page. An event shown here
    # is running; one that is not running is not shown (D44). So the badge names
    # the publisher and nothing more.
    #
    # Asserted on the built artefact rather than on the template source, because
    # that is the only string a reader can actually see: the source ids
    # `bayside_archived` and `frankston_archived` are still in the payload's
    # `source_id` and in the CSS class names, and a check on the source would
    # have to know the difference between those and prose. What must never
    # appear is the provenance rendered as text.
    leaked = [t for t in ("(archived)", "(delisted)", "(archive)")
              if t in html_out]
    if leaked:
        print("FAIL: internal provenance reached the page as text: "
              f"{', '.join(leaked)}. The badge names the publisher only.")
        sys.exit(1)

    print(f"Wrote index.html ({len(html_out)} bytes)")


if __name__ == "__main__":
    if "--test" in sys.argv:
        # Imported here, not at module scope: `dedupe` pulls in rapidfuzz and
        # recurrence, and a suburb-extraction case should not need them to
        # import. Only the store-flag case below does.
        from dedupe import _normalize_raw

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

        # Which rows reach the page. Calls the same `on_page()` the build does,
        # so the suite cannot assert about a predicate no code runs -- the two
        # were written differently here (`not r.get(...)`) and in main()
        # (`is not True`), which is a divergence a test exists to prevent.
        def _on_page(rows):
            return [r for r in rows if on_page(r)]

        _TESTS += [
            ("an archived row is withheld from the page",
             len(_on_page([
                 {"name": "Live", "source_id": "ccc"},
                 {"name": "Delisted", "source_id": "bayside_archived",
                  "archived": True}])), 1),
            ("a live row is kept", "Live" in [
                r["name"] for r in _on_page([{"name": "Live"}])], True),
            # archived: False is not the same as absent. A confirmed-live
            # archived series is listed, and the flag is what says so; a build
            # that treated any archived row as hidden would drop ten running
            # Frankston series the moment the first one was confirmed.
            ("a confirmed-live archived series is listed",
             len(_on_page([
                 {"name": "Market", "source_id": "bayside_archived",
                  "archived": False, "live_confirmed": True}])),
             1),
            # The predicate itself, which is what makes the three cases above
            # mean anything: only the literal True withholds a row.
            ("only archived: True is withheld",
             [len(_on_page([{"archived": v}]))
              for v in (True, False, None, 0, "")],
             [0, 1, 1, 1, 1]),
            # The check that matters: the flag is provenance, and the only place
            # that knows it is the file the row was read from. If it is lost,
            # delisted programmes are quietly published again. Asserted through
            # `_normalize_raw`, which is the function that has to carry it -- a
            # dict literal can only ever agree with itself, which is what the
            # previous version of this case did.
            ("an archived row keeps its flag through the store",
             _normalize_raw({"name": "Delisted", "archived": True},
                            quiet=True).get("archived"),
             True),
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
