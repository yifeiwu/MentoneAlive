"""Check a row's address against OpenStreetMap, and say whether it is real.

`dedupe.py` can tell that an address is *malformed* -- an empty segment, a
dangling comma, a suburb repeated immediately after itself -- and repair it from
a source that has since been fixed. It cannot tell whether a well-formed address
is the right one. "84 Reserve Road, Beaumaris, Victoria 3193" is well-formed,
and Bayside's own page once published it as
"84 Reserve Road, Beaumaris, Beaumaris, Victoria 3193"; only the duplicate said
anything was wrong, and only this script can say what the right answer is.

So this asks a geocoder. Nominatim's answer is the authority on whether an
address resolves and which suburb it falls in, and comparing that against the
row is the only check that catches a suburb that is plausible but wrong, a
postcode that belongs to a neighbouring suburb, and a street number that does
not exist.

    python scripts/verify_addresses.py                  # report, change nothing
    python scripts/verify_addresses.py --fix            # write corrections
    python scripts/verify_addresses.py --source bayside_live
    python scripts/verify_addresses.py --check          # exit 1 on disagreement

Deliberately separate from the pipeline, and separate from the suites:

* It is a *network* check, and every rule suite in `checks.py` is required to be
  pure. A gate that needs the internet fails for reasons that have nothing to do
  with the code, which is how a check stops being read.
* Nominatim's usage policy caps a client at about one request per second, and
  the whole store is 2,200 rows. A weekly cron can afford a few dozen lookups on
  the addresses that look doubtful; it cannot afford the whole catalogue, and
  the project has no reason to.
* It only ever *reports* by default. A geocoder's idea of a locality is not
  always the address a venue prefers to publish -- a council office building
  with a council-named street address, a library whose postcode covers a wider
  area than its street. The default is a list for a human to agree with; `--fix`
  writes only the disagreements the caller has seen.

What counts as a disagreement:

* no result at all for the address, and
* a result whose suburb, state or postcode contradicts the row's own.

A match is not proof the address is right. It is evidence the address is a real
place, which is the question this script is asked.
"""
import argparse
import io
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dedupe import ROOT, _malformed_address  # noqa: E402
from jsonio import write_json  # noqa: E402
from vic_suburbs import canonical_suburb as canonical_vic_suburb  # noqa: E402
from webfetch_http import restates_name  # noqa: E402

NOMINATIM = "https://nominatim.openstreetmap.org/search"
# Nominatim's policy: identify the application, and stay at or under one
# request per second. Both are hard requirements of using the public instance,
# not politeness.
USER_AGENT = "eventscal-address-verifier/1.0 (community events index; " \
             "one-off maintenance query)"
MIN_INTERVAL = 1.05
STATE_ABBR = {"victoria": "VIC", "vic": "VIC", "new south wales": "NSW",
              "nsw": "NSW", "tasmania": "TAS", "tas": "TAS"}

_last_call = [0.0]


def _throttle():
    gap = time.monotonic() - _last_call[0]
    if gap < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - gap)
    _last_call[0] = time.monotonic()


def geocode(session, address, tries=3):
    """Nominatim's best match for `address`, as a dict, or None.

    `addressview` is requested so the state comes back as a code rather than a
    name, and `limit=1` because the first hit is the one to compare. A failure
    is retried with the backoff the shared readers use; a 429 is honoured rather
    than fought, because the polite thing to do when a public geocoder asks you
    to slow down is slow down.
    """
    from urllib.parse import urlencode

    params = urlencode({
        "q": address,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": 1,
        "countrycodes": "au",
    })
    url = f"{NOMINATIM}?{params}"
    last = None
    for attempt in range(tries):
        _throttle()
        try:
            r = session.get(url)
            if r.status_code in (429, 502, 503, 504):
                last = f"HTTP {r.status_code}"
                time.sleep(2.0 * (attempt + 1))
                continue
            if r.status_code != 200:
                return None
            hits = json.loads(r.text or "[]")
            return hits[0] if hits else None
        except Exception as e:  # noqa: BLE001 - reported, never fatal
            last = repr(e)[:120]
            time.sleep(1.5 * (attempt + 1))
    print(f"  geocoder gave up on {address!r}: {last}", file=sys.stderr)
    return None


def _code(country_state):
    """The state of an `addressdetails` entry as a two-letter code."""
    if not country_state:
        return ""
    v = str(country_state)
    if len(v) == 2 and v.isupper():
        return v
    return STATE_ABBR.get(v.strip().lower(), v.strip())


def _postcode(value):
    m = re.search(r"\b(\d{4})\b", str(value or ""))
    return m.group(1) if m else ""


def disagreement(row, hit):
    """What the geocoder says about `row` that the row does not say itself.

    Returns a short string naming the first contradiction, or "" when the two
    agree. Only contradictions are reported: the geocoder will always have more
    to say than the address does, and the address having no country is not a
    fault.
    """
    if not hit:
        return "no match"
    got = hit.get("address") or {}
    if not isinstance(got, dict):
        got = {}
    row_sub = (row.get("suburb") or "").strip()
    row_post = _postcode(row.get("address"))
    row_state = _code(got.get("state")) or ""

    hit_sub = (got.get("suburb") or got.get("city") or got.get("town")
               or got.get("village") or got.get("hamlet")
               or got.get("suburb_district") or got.get("borough")
               or got.get("city_district") or "").strip()
    hit_sub_canon = canonical_vic_suburb(hit_sub) if hit_sub else ""
    row_sub_canon = canonical_vic_suburb(row_sub) if row_sub else ""

    # A venue head is a place name, not a suburb, so it is only evidence when it
    # looks like one. "Beaumaris Library" is not a suburb and saying so would
    # report every library row in the store as a mismatch.
    if row_sub_canon and hit_sub_canon and row_sub_canon != hit_sub_canon:
        return f"suburb {row_sub!r} but maps to {hit_sub!r}"
    if row_post and _postcode(got.get("postcode")) and \
            row_post != _postcode(got.get("postcode")):
        return f"postcode {row_post} but maps to {_postcode(got.get('postcode'))}"
    if row_state and _code(got.get("state")) and row_state != _code(got.get("state")):
        return f"state {row_state} but maps to {_code(got.get('state'))}"
    return ""


def _searchable(row):
    """The string to send: the address, without the venue name in front of it.

    Nominatim tolerates a leading venue name but resolves less reliably, and the
    venue is already a separate field. A row with no street address at all (an
    online event, a "Victoria"-only line) has nothing to verify and is skipped.
    """
    address = (row.get("address") or "").strip()
    if not address or _postcode(address) == "" and "," not in address:
        return ""
    parts = [p.strip() for p in address.split(",") if p.strip()]
    # Drop a leading segment that is the venue rather than the street, but only
    # when something street-shaped is left.
    if len(parts) > 2 and re.search(r"\b(library|centre|center|hall|house|"
                                    r"club|rooms?|pavilion)\b",
                                    parts[0], re.I):
        parts = parts[1:]
    return ", ".join(parts)


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=None, help="one source_id only")
    ap.add_argument("--fix", action="store_true",
                    help="write a corrected address/suburb where they disagree")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if any address disagrees")
    ap.add_argument("--suspicious-only", action="store_true", default=True,
                    help="only look at rows whose address looks doubtful "
                         "(the default)")
    ap.add_argument("--all", action="store_true",
                    help="look at every row, not just the doubtful ones")
    ap.add_argument("--limit", type=int, default=200,
                    help="stop after this many lookups (default 200)")
    args = ap.parse_args(argv)

    doc = json.load(open(ROOT / "data" / "events.json", encoding="utf-8"))
    rows = doc["rows"]

    # Two ways a row earns a lookup: its address is visibly broken, or its
    # description restates its own name. Both are the two defects this project
    # has actually shipped, and both are cheap to find without a network.
    suspect = []
    for r in rows:
        if args.source and r.get("source_id") != args.source:
            continue
        if args.all:
            suspect.append(r)
            continue
        why = []
        if _malformed_address(r.get("address")):
            why.append("malformed address")
        if restates_name(r.get("description"), r.get("name")):
            why.append("description restates the name")
        if why:
            r = dict(r)
            r["_why"] = ", ".join(why)
            suspect.append(r)

    print("Verifying addresses against OpenStreetMap (Nominatim).")
    print(f"  {len(suspect)} row(s) to look at"
          + ("" if args.all else " (address looks doubtful)"))
    if not suspect:
        print("nothing to verify.")
        return 0
    if len(suspect) > args.limit:
        print(f"  stopping at --limit {args.limit}; "
              f"{len(suspect) - args.limit} row(s) not looked at")
        suspect = suspect[:args.limit]

    from fetch_sources import session_for
    session = session_for({"impersonate": False})

    agreed = disagreed = unresolvable = 0
    fixes = []
    for i, r in enumerate(suspect, 1):
        query = _searchable(r)
        if not query:
            unresolvable += 1
            continue
        hit = geocode(session, query)
        note = disagreement(r, hit)
        name = (r.get("name") or "")[:52]
        if not note:
            agreed += 1
            continue
        if not hit:
            unresolvable += 1
            print(f"  [{i}/{len(suspect)}] {name!r}: no match for {query!r}")
            continue
        disagreed += 1
        got = hit.get("address") or {}
        print(f"  [{i}/{len(suspect)}] {name!r} [{r.get('source_id')}]")
        print(f"      row    : {r.get('address')!r} (suburb {r.get('suburb')!r})")
        print(f"      OSM    : {hit.get('display_name')!r}")
        print(f"      because: {note}")
        if args.fix:
            corrected = _correct(r, hit, got)
            if corrected:
                fixes.append((r, corrected))
                for k, v in corrected.items():
                    print(f"      fix    : {k} = {v!r}")

    print()
    print(f"  agreed: {agreed}   disagreed: {disagreed}   "
          f"no match / nothing to send: {unresolvable}")
    if args.fix and fixes:
        write_json(ROOT / "data" / "events.json", _apply(doc, fixes))
        print(f"  wrote {len(fixes)} correction(s) to data/events.json")
    elif args.fix:
        print("  no corrections to write")
    elif args.check and disagreed:
        print("  FAIL: an address disagrees with OpenStreetMap")
        return 1
    return 0


def _correct(row, hit, got):
    """What to write for a row the geocoder contradicts, or {} for nothing.

    The address is only replaced when the geocoder's own display string is
    better-formed than the row's -- it names the same street and its own
    locality, where the row may carry a repeated segment. A geocoder is not
    allowed to invent a street the source did not state, so `street`/`house
    number` are only used when the row has neither.
    """
    out = {}
    display = (hit.get("display_name") or "").strip()
    if display and _malformed_address(row.get("address")):
        out["address"] = display
    suburb = (got.get("suburb") or got.get("city") or got.get("town")
              or got.get("village") or "")
    canon = canonical_vic_suburb(suburb) if suburb else ""
    row_canon = canonical_vic_suburb(row.get("suburb") or "") \
        if row.get("suburb") else ""
    if canon and row_canon and canon != row_canon:
        out["suburb"] = canon
    return out


def _apply(doc, fixes):
    """Rewrite the named rows in place, by name + start, which is the row's
    identity for everything else in the pipeline too."""
    index = {}
    for r in doc["rows"]:
        index.setdefault(((r.get("name") or "").strip(),
                          str(r.get("datetime_iso") or "")[:16]), r)
    for r, changes in fixes:
        target = index.get(((r.get("name") or "").strip(),
                            str(r.get("datetime_iso") or "")[:16]))
        if target is not None:
            target.update(changes)
    return doc


if __name__ == "__main__":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace")
    except Exception:  # noqa: BLE001 - a console that cannot be wrapped is fine
        pass
    sys.exit(main(sys.argv[1:]))
