"""Deduplication and data-quality audit of the published store.

    python scripts/quality_audit.py

Read-only, and deliberately not a check: it reports, it does not assert. Every
gate in this project is `checks.py` or `health_check.py`, and both fail the
build -- which is right for an invariant ("every row has a date") and wrong for
a measurement ("how many rows are dated 200 days out", "which names are over 70
characters"). A number that changes as the sources change is information, and
failing a build on it would mean tuning the number rather than the thing.

What it is for is the class of defect no gate covers. `health_check.py` asserts
that every published row carries a date, a suburb and a known type; nothing
asserted that a description was a description rather than the page's address
block, that an internal scratch field had not reached the store, or that two
sources styling one class differently had produced two rows for it. Each of
those was found here rather than by a failing check.

Run it after `build_site.py`, which is what writes the artefact it reads.

Findings it reported on the day it was written, all since fixed, kept as the
regression list:
  * 344 directory descriptions ending in the venue's street address and the
    words "View Map" -- the fetcher read every paragraph in a content column
    instead of stopping at the Location heading.
  * 72 rows carrying `_hours_schedule`, a scratch key that had reached the
    committed store and then the page.
  * 56 rows with a `price_text` and no `price_sort`, 19 of them reading "Free",
    because `_merge_sources()` fills blanks and the derived value predated the
    field.
  * One class published twice at one hall, hour and date: 'Zumba® Gold' against
    'Zumba Gold (Mondays)', 0.84 similar, below the merge threshold.
  * `greater_dandenong` publishing a reserve forty kilometres from Springvale
    while health_check failed the build on the same rows, because the fetcher
    and the checker held opposite rules about an unplaceable suburb.
"""
import collections
import json
import re
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, "scripts")
from rapidfuzz import fuzz

from dedupe import name_head, normalize_name, slot_hash, venue_head
from venues import extract_suburb, is_online

TODAY = date.today()
rows = json.load(open("data/events.json", encoding="utf-8"))["rows"]
print("store: %d rows, %d sources\n" % (len(rows), len({r["source_id"] for r in rows})))

def section(name):
    print("=" * 72)
    print(name)
    print("=" * 72)

# ---------------------------------------------------------------- schema
section("SCHEMA")
keys = collections.Counter()
for r in rows:
    keys.update(r.keys())
for k, n in sorted(keys.items(), key=lambda kv: -kv[1]):
    flag = "" if n == len(rows) else "  <-- PARTIAL (%d)" % (len(rows) - n)
    print("   %-18s %5d%s" % (k, n, flag))
dup_of = [r for r in rows if r.get("source_label")]
print("   legacy source_label still present: %d" % len(dup_of))
print("   has_real_date still present:         %d"
      % sum(1 for r in rows if "has_real_date" in r))

# ------------------------------------------------------------ duplicates
section("DUPLICATES")
exact = collections.Counter(slot_hash(r) for r in rows)
print("exact (name, start, location) collisions: %d rows"
      % sum(v - 1 for v in exact.values() if v > 1))

prog = collections.Counter()
for r in rows:
    iso = str(r.get("datetime_iso") or "")
    if "T" not in iso:
        continue
    prog[(name_head(r.get("name")), venue_head(r.get("location")), iso[:16])] += 1
prog_dups = {k: v for k, v in prog.items() if v > 1}
print("same programme+venue+start groups:       %d" % len(prog_dups))

# near-duplicates: different source, high name similarity, same day+time
buckets = collections.defaultdict(list)
for r in rows:
    iso = str(r.get("datetime_iso") or "")
    if "T" in iso:
        # Bucketed on the venue too, because that is what the fuzzy merge
        # requires: same day, within 30 minutes, same venue, similar name.
        # Without the venue this reports every hall that runs the same class,
        # which is a list of correct rows rather than a list of duplicates.
        buckets[(iso[:10], iso[11:16],
                 venue_head(r.get("location")))].append(r)
near = []
for (day, t, _venue), group in buckets.items():
    if len(group) < 2:
        continue
    for i in range(len(group)):
        for j in range(i + 1, len(group)):
            a, b = group[i], group[j]
            if a["source_id"] == b["source_id"]:
                continue
            s = fuzz.ratio(normalize_name(a["name"]), normalize_name(b["name"])) / 100
            if s >= 0.80:
                near.append((round(s, 2), a, b))
print("cross-source near-duplicates (>=0.80):   %d pairs" % len(near))
for s, a, b in sorted(near, key=lambda x: -x[0])[:8]:
    print("      %.2f %-32s [%-14s]  vs  %-32s [%-14s] %s"
          % (s, a["name"][:32], a["source_id"], b["name"][:32], b["source_id"],
             (a.get("location") or "")[:28]))

# identical source URL reused across many rows
url_rows = collections.defaultdict(set)
for r in rows:
    url_rows[r.get("source", "")].add(r.get("name", ""))
multi = {u: n for u, n in url_rows.items() if len(n) > 1}
print("source URLs carrying >1 distinct name:   %d" % len(multi))
for u, n in sorted(multi.items(), key=lambda kv: -len(kv[1]))[:5]:
    print("      %-58s %d names" % (u[-58:], len(n)))

# ------------------------------------------------------------- dates
section("DATES")
past = [r for r in rows if str(r.get("datetime_iso") or "")[:10] < TODAY.isoformat()]
future_far = [r for r in rows
              if str(r.get("datetime_iso") or "")[:10]
              > (TODAY + timedelta(days=200)).isoformat()]
midnight = [r for r in rows
            if str(r.get("datetime_iso") or "")[11:16] == "00:00"]
nodate = [r for r in rows if not str(r.get("datetime_iso") or "")]
print("rows dated before today:   %d" % len(past))
print("rows dated >200 days out:  %d" % len(future_far))
print("rows at 00:00 (no time):  %d" % len(midnight))
print("rows with no date:         %d" % len(nodate))
for r in future_far[:5]:
    print("      %-40s %s [%s]" % (r["name"][:40], r["datetime_iso"][:10],
                                   r["source_id"]))
print()
dated = sorted(str(r["datetime_iso"])[:10] for r in rows if r.get("datetime_iso"))
print("date span: %s .. %s" % (dated[0], dated[-1]))

# ------------------------------------------------------------ series
section("SERIES / RECURRENCE")
inf = [r for r in rows if r.get("date_inferred")]
sids = collections.Counter(r.get("series_id") for r in inf if r.get("series_id"))
print("date_inferred rows:            %d" % len(inf))
print("  with a series_id:            %d" % sum(sids.values()))
print("  without one:                 %d"
      % sum(1 for r in inf if not r.get("series_id")))
print("distinct series:               %d" % len(sids))
spread = collections.Counter(sids.values())
print("occurrences per series:        %s"
      % ", ".join("%d x%d" % (n, c) for n, c in sorted(spread.items())))
orphan = [r for r in rows if r.get("series_id") and not r.get("date_inferred")]
print("series_id on a non-inferred row: %d" % len(orphan))
sized = [r for r in rows if r.get("date_inferred")
         and r.get("recurrence") and sids.get(r.get("series_id"), 0) != 1]
print("rows sharing a series but disagreeing on the label: %d" % len(sized))
for r in sized[:4]:
    same = [x for x in inf if x.get("series_id") == r["series_id"]]
    print("      %-34s %-24r vs %r"
          % (r["name"][:34], r.get("recurrence"),
             sorted({x.get("recurrence") for x in same})))

# ------------------------------------------------------------- places
section("PLACES / SUBURBS")
nosub = [r for r in rows if not (r.get("suburb") or "").strip()]
noaddr = [r for r in rows if not (r.get("address") or "").strip()
          and not is_online(r.get("location"))]
print("rows with no suburb:            %d" % len(nosub))
for r in nosub[:6]:
    print("      %-38s %-44s [%s]" % (r["name"][:38], (r.get("address") or "")[:44],
                                    r["source_id"]))
print("non-online rows with no address: %d" % len(noaddr))
mismatch = [r for r in rows if r.get("suburb")
            and extract_suburb(r.get("address") or "")
            and extract_suburb(r["address"]).lower() != r["suburb"].lower()]
print("stored suburb != address suburb: %d" % len(mismatch))
bad = [r for r in rows if (r.get("address") or "").strip()
       and (",," in r["address"] or r["address"].strip().endswith(",")
            or r["address"].strip().startswith(","))]
print("malformed addresses (,,-style):  %d" % len(bad))
for r in bad[:5]:
    print("      %r" % r["address"][:70])
print("suburb distribution:")
for s, c in collections.Counter(r.get("suburb") or "(none)" for r in rows).most_common(8):
    print("      %-22s %d" % (s, c))

# ------------------------------------------------------------- text
section("TEXT QUALITY")
MOJI = re.compile(r"[�]|\bCaf\b|Ã")
long_names = [r for r in rows if len(r.get("name") or "") > 70]
print("names over 70 chars:      %d" % len(long_names))
for r in long_names[:5]:
    print("      %r" % r["name"][:96])
moji = [r for r in rows if MOJI.search((r.get("name") or "") + (r.get("description") or ""))]
print("names/descriptions with mojibake: %d" % len(moji))
for r in moji[:6]:
    print("      [%s] %s" % (r["source_id"], r["name"][:74]))
leak = [r for r in rows if "View Map" in (r.get("description") or "")
        or re.search(r"\d{4}\s*$", (r.get("description") or "").strip())]
print("descriptions ending in a postcode / holding 'View Map': %d" % len(leak))
for r in leak[:4]:
    print("      [%s] %s ... %r" % (r["source_id"], r["name"][:34],
                                   (r.get("description") or "")[-40:]))
trunc = [r for r in rows if re.search(r"\.\.\.$", (r.get("name") or ""))]
print("names that look truncated (trailing ...): %d" % len(trunc))
for r in trunc[:6]:
    print("      [%s] %r" % (r["source_id"], r["name"][:80]))

# ------------------------------------------------------------- money
section("PRICE")
ps = [r for r in rows if r.get("price_text") and r.get("price_sort") is None]
print("rows with price_text but no price_sort: %d" % len(ps))
for r in ps[:6]:
    print("      [%s] %-34s %r" % (r["source_id"], r["name"][:34],
                                   r["price_text"][:40]))
free = [r for r in rows if r.get("price_sort") == 0]
print("rows priced free (price_sort 0):       %d" % len(free))
freet = [r for r in rows if re.search(r"\bfree\b", r.get("price_text") or "", re.I)]
print("  ...of which price_text says 'free':   %d" % len(freet))
money = [r for r in rows if re.search(r"\$", r.get("price_text") or "")
         and r.get("price_sort") is None]
print("rows showing a $ amount but unsorted:   %d" % len(money))
for r in money[:6]:
    print("      %r" % r["price_text"][:60])

# ------------------------------------------------------------- types
section("CLASSIFICATION")
# `types` is derived by build_site.py, not by a fetcher, so it exists on every
# row only once a build has run. A row added by a dedupe after the last build
# has none, and this section indexed r["types"] directly -- so the audit died
# with a KeyError on exactly the rows it existed to draw attention to, taking
# every section after it with it. A report-only tool that crashes is worse than
# no tool: the run looks like it found nothing.
classified = [r for r in rows if r.get("types")]
unclassified = [r for r in rows if not r.get("types")]
tc = collections.Counter(t for r in classified for t in r["types"])
base = len(classified) or 1
for t, c in tc.most_common():
    print("   %-24s %5d  (%.0f%% of classified rows)"
          % (t, c, 100 * c / base))
per = collections.Counter(len(r["types"]) for r in classified)
print("tags per row: %s" % ", ".join("%d x%d" % (k, v) for k, v in sorted(per.items())))
if unclassified:
    print()
    print("rows not yet classified (%d) -- added since the last build, which is"
          % len(unclassified))
    print("when build_site.py writes this field. Re-run build_site.py, or run")
    print("the pipeline in order; these are not lost, only unlabelled:")
    for sid, n in collections.Counter(
            r.get("source_id") for r in unclassified).most_common():
        print("      %-22s %5d" % (sid, n))
print()
print("groups published with no recognised subject tag:")
for r in rows:
    if r.get("source_id") == "kingston_groups" and r.get("types") == ["Community Group"]:
        print("      %r" % r["name"][:70])

# ------------------------------------------------------------- flags
section("FLAGS")
for flag in ("hidden_by_default", "is_commercial", "is_service", "date_inferred"):
    n = sum(1 for r in rows if r.get(flag))
    print("   %-20s %5d  (%.1f%%)" % (flag, n, 100 * n / len(rows)))
print("status values: %s" % collections.Counter(r.get("status") for r in rows if r.get("status")))

# ------------------------------------------------------------- sources
section("SOURCES")
for sid, n in collections.Counter(r["source_id"] for r in rows).most_common():
    past_n = sum(1 for r in rows if r["source_id"] == sid
                 and str(r.get("datetime_iso") or "")[:10] < TODAY.isoformat())
    print("   %-20s %5d rows%s" % (sid, n,
                                   "   (%d already past)" % past_n if past_n else ""))