"""Deduplicate events across sources.

Passes:
   1. Exact hash on (normalized_name, date_part, normalized_location).
   Date part (not full timestamp) collapses fetch-time microsecond stamps.
   Dateless rows (iso=None/'') hash with '' and CAN merge.
2. Same name+location across different sources (user rule): if normalized
   name and location are equal but dates differ/empty, merge sources and
   keep the dated version. Collapses cross-source duplicates.
3. Fuzzy: name similarity >= 0.75 AND same day within +/-30min AND
   location equality (strict, not substring).

Prunes rows older than 90 days. Merges raw + archived +
scripts/webfetch_snapshots/*.json. Rows still lacking a date afterwards are
resolved by scripts/recurrence.py, which infers them from the description and
drops whatever it cannot date — every published event carries a real date.
"""
import glob
import hashlib
import json
import os
import re
import sys
import unicodedata
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from rapidfuzz import fuzz

from jsonio import read_json, write_json
from recurrence import infer_event, refresh_inferred, resolve_dateless

PRUNE_DAYS = 90
ROOT = Path(__file__).resolve().parent.parent
# All published times are Melbourne local. Aware source stamps are converted
# to this zone rather than having their offset stripped.
LOCAL_TZ = ZoneInfo("Australia/Melbourne")


def reference_today():
    """Today, overridable via SOURCE_DATE_EPOCH for reproducible runs."""
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch:
        try:
            return date.fromtimestamp(int(epoch))
        except (ValueError, OverflowError, OSError):
            pass
    return date.today()


def normalize_name(name):
    return " ".join((name or "").lower().strip().split())


def fold_accents(text):
    """Strip diacritics: 'Café' and 'Cafe' name the same programme.

    One source copies 'Chatty Café' from the venue's own site while the
    national directory writes 'Chatty Cafe' unaccented.
    """
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c))


# A subtitle or qualifier after a separator: "Chatty Cafe - Connect over a
# Cuppa" and "Chatty Cafe" are the same program named by two sources.
_SUBTITLE_SPLIT_RE = re.compile(r"\s+[-|:–—]\s+|\s+[|:]\s+")

# Generic trailing tags that carry no programme identity.
_NAME_NOISE_RE = re.compile(
    r"\s*\((?:new|sessions?|series|class|term \d+)\)\s*$", re.I)


def name_head(name):
    """The programme-identifying part of a name, ignoring any subtitle.

    'Chatty Cafe - Connect over a Cuppa' -> 'chatty cafe'
    'Love to Live - Gentle Chair-Based Exercise' -> 'love to live'
    """
    s = _NAME_NOISE_RE.sub("", normalize_name(name))
    return fold_accents(_SUBTITLE_SPLIT_RE.split(s)[0].strip())


def normalize_location(loc):
    return " ".join((loc or "").lower().strip().split())


def date_part(iso):
    if not iso:
        return ""
    try:
        return _local(iso).date().isoformat()
    except (ValueError, TypeError):
        return str(iso)[:10]


def _same_time_of_day(a, b):
    """True when two rows start at the same time of day.

    Two sessions of one class at one venue on the same day ("Cert II in EAL"
    9am and 12:30pm) are distinct events and must not merge. Rows without a
    time are treated as matching, so all-day listings still collapse.
    """
    ta = str(a.get("datetime_iso") or "")[11:16]
    tb = str(b.get("datetime_iso") or "")[11:16]
    if not ta or not tb or ta == "00:00" or tb == "00:00":
        return True
    return ta == tb


def name_similarity(a, b):
    return fuzz.ratio(normalize_name(a), normalize_name(b)) / 100.0


def time_matches(dt1_str, dt2_str, tolerance_minutes=30):
    try:
        if not dt1_str or not dt2_str:
            return False
        t1 = _local(dt1_str)
        t2 = _local(dt2_str)
        if t1.date() != t2.date():
            return False
        return abs(t1 - t2) <= timedelta(minutes=tolerance_minutes)
    except (ValueError, TypeError):
        return False


def slot_hash(event):
    """Hash of (name, start time, location) -- the dedupe key.

    The start time is part of the key, where an earlier (name, date,
    location) hash was used: a venue can legitimately run the same class
    twice in one day ('Cert II in EAL' Wednesdays 9am-12pm and
    12:30pm-3:30pm), so collapsing on the date alone would erase the
    second session.
    """
    iso = str(event.get("datetime_iso") or "").replace("Z", "")
    stamp = iso[:16] if "T" in iso else ""
    key = (f"{normalize_name(event.get('name', ''))}|"
           f"{stamp}|"
           f"{normalize_location(event.get('location', ''))}")
    return hashlib.md5(key.encode()).hexdigest()


def location_matches(loc1, loc2):
    n1 = normalize_location(loc1)
    n2 = normalize_location(loc2)
    if not n1 or not n2:
        return False
    # Strict equality only — substring ("Bayside" in "Bayside Libraries",
    # generic "Greater Dandenong") caused false merges.
    return n1 == n2


def venue_head(loc):
    """First comma-separated segment of a location, normalised.

    'Kingston Arts Centre, 979 Nepean Hwy' and 'Kingston Arts Centre' are the
    same venue written two ways; the address tail is the difference.
    """
    return normalize_location((loc or "").split(",")[0])


def _venue_compatible(loc1, loc2):
    """True when two location strings plausibly name the same place."""
    a, b = venue_head(loc1), venue_head(loc2)
    if not a or not b:
        return False
    if a == b:
        return True
    # One source often gives the short name, the other the organisation:
    # 'Greater Dandenong' vs 'Greater Dandenong Libraries'.
    return a.startswith(b) or b.startswith(a)


def dedupe_by_source_url(rows):
    """Collapse two sources reporting the same event, however they name it.

    Two independent problems produced the same visible duplicate:

    1. Several source pairs read the *same listing page* and differ only in
       the venue string — `kingston_arts` + `kingston_council` both hit
       kingstonarts.com.au, `greater_dandenong` + `gd_libraries` both hit the
       GD libraries site. `location_matches()` is strict equality, so
       'Kingston Arts Centre' and 'Kingston Arts Centre, 979 Nepean Hwy'
       never matched.

    2. Sources style one programme differently. The national Chatty Cafe
       directory writes "Chatty Cafe - Cheltenham Community Centre", the
       venue site writes "Chatty Cafe", a seniors listing writes
       "Chatty Cafe - Connect over a Cuppa" — the same session, three
       titles. Whole-string similarity lands around 0.5 for these, well under
       the fuzzy threshold, so nothing merged them.

    A merge needs: same date and start time, the same programme (shared base
    name), and compatible venues. The venue check is what keeps genuinely
    distinct events apart — two STEADYstrength classes at two different
    halls, or "Chatty Cafe - Game On!" at 10:00 against the 11:00 series.
    """
    index = {}
    prog_index = {}
    out, merged = [], 0
    for r in rows:
        stamp = str(r.get("datetime_iso") or "")[:16]
        name = normalize_name(r.get("name", ""))
        if "T" not in stamp or not name:
            out.append(r)
            continue
        # Two keys: the exact (url, name) pair, and the looser programme key
        # that ignores subtitle and venue wording.
        url = (r.get("source") or "").rstrip("/")
        head = name_head(r.get("name"))
        by_url = index.get(("url", url, name, stamp))
        # Candidates, not a single slot. The programme key used to hold one row,
        # claimed by `setdefault` and never released, so the first row to arrive
        # owned (programme, time) for the rest of the run. A merge still needs a
        # compatible venue, so a row that cannot merge -- a session at a
        # different hall, or a stale store row naming a venue the source has
        # since corrected -- left the key pointing at itself and every *later*
        # row was compared against that one instead of against each other. Two
        # real twins could therefore both survive: `Tai Chi` at Patterson Lakes
        # from kingston_hubs and from kingston_council, held apart by a stale
        # `Kingston Hubs` row that reconcile_store() then dropped, leaving the
        # duplicate visible in a green-looking store.
        candidates = prog_index.setdefault((head, stamp), [])
        hit = by_url
        if hit is None or not _venue_compatible(hit.get("location"),
                                                r.get("location")):
            for cand in candidates:
                if _venue_compatible(cand.get("location"), r.get("location")):
                    hit = cand
                    break
        if hit is not None:
            # A shared base name plus the same date, start time and venue is
            # already decisive, so no description check is needed. Requiring
            # the prose to match as well would miss these: the venue site and
            # the national directory write entirely different blurbs for the
            # same weekly session.
            # Rows are walked in order -- the stored rows first, then this
            # run's -- so `r` is the later, fresher observation and its URL
            # becomes the primary link. A Chatty Cafe venue was re-slugged, and
            # without this the stored rows kept the dead page forever: this
            # merge is the only place the two could ever be reconciled, because
            # deduplicate() runs before inference and so never saw a dateless
            # twin of an already-expanded stored series.
            _merge_sources(hit, r, refresh_source=True)
            merged += 1
            continue
        index.setdefault(("url", url, name, stamp), r)
        candidates.append(r)
        out.append(r)
    if merged:
        print(f"  Collapsed {merged} same-event duplicates "
              f"(one session reported by two sources)")
    return out


def drop_untimed_twins(rows):
    """Drop midnight occurrences when a timed one exists for the same session.

    A listing that states only "Wednesday" yields an occurrence at 00:00. If
    another source states a real time for the same programme at the same
    venue, the midnight row restates that session rather than adding an event
    -- and publishing it shows a 12am start that does not exist.

    Matched on programme + venue across the whole run, not per date: the timed
    listing and the untimed one often cover different spans (one source
    listed the term, the other kept publishing the weekly session into
    December), yet they are the same Wednesday class.

    Only fires when a timed counterpart exists, so a genuinely untimed series
    with no timed twin is left alone.
    """
    timed = set()
    for r in rows:
        iso = str(r.get("datetime_iso") or "")
        if len(iso) >= 16 and iso[11:16] != "00:00":
            timed.add((name_head(r.get("name")),
                       venue_head(r.get("location"))))
    kept, dropped = [], 0
    for r in rows:
        iso = str(r.get("datetime_iso") or "")
        if len(iso) >= 16 and iso[11:16] == "00:00":
            if (name_head(r.get("name")),
                    venue_head(r.get("location"))) in timed:
                dropped += 1
                continue
        kept.append(r)
    if dropped:
        print(f"  Dropped {dropped} midnight rows that restate a timed session")
    return kept


def _merge_sources(existing, candidate, refresh_source=False):
    """Fold `candidate` into `existing`, keeping `existing` as the kept row.

    `refresh_source` is set when `candidate` is a row this run just fetched,
    which makes its `source` the URL the source publishes today. The kept row
    can carry a dead one: a Chatty Cafe venue was re-slugged, and every
    published row kept the old page as its primary link, because nothing here
    overwrote a non-blank field. `source` is what the page's "source" link and
    the CSV export point a reader at, so a stale one is as wrong as a stale
    address. The superseded URL is kept in `sources`, which is also what
    reconcile_store() checks, so nothing loses its justification.
    """
    if not isinstance(existing.get("sources"), list):
        existing["sources"] = [existing.get("source", "")]
    cand_src = candidate.get("source", "")
    if cand_src and cand_src not in existing["sources"]:
        existing["sources"].append(cand_src)
    if refresh_source and cand_src and cand_src != existing.get("source"):
        existing["source"] = cand_src
    # Prefer keeping a real date over a dateless duplicate.
    if not existing.get("datetime_iso") and candidate.get("datetime_iso"):
        for k in ("datetime_iso", "datetime_text", "has_real_date"):
            # `is not None` would skip a genuine empty string, leaving the
            # dateless row with a blank display after gaining a real date.
            if candidate.get(k):
                existing[k] = candidate[k]
        # The date is now source-supplied, not inferred. Leaving the flag set
        # would make resolve_dateless keep ignoring this row's real date, so a
        # dateless twin is never suppressed in favour of it.
        existing.pop("date_inferred", None)
        existing.pop("recurrence", None)
    # Fill in fields the kept row was missing rather than the better row.
    # `suburb` is in the list because build_site.py writes a derived value into
    # the store: when an address carries no "VIC ####" for extract_suburb() to
    # anchor on it stores "", and a source that *did* know the suburb (Greater
    # Dandenong reads it off the event detail page) would then never win.
    for k in ("location", "address", "suburb", "description", "price_text"):
        if not (existing.get(k) or "").strip() and (candidate.get(k) or "").strip():
            existing[k] = candidate[k]
    # A blank field is filled from the twin, but a *less specific* one is
    # replaced. `archived_events.json` was corrected to name venues it had
    # previously left as a bare suburb ("Frankston, VIC" -> "Frankston
    # Brewhouse"); the stored rows kept the old string forever, because the
    # only path that writes a location is the fill-blank one above and these
    # rows were not blank. reconcile_store() cannot catch it either: it judges
    # the row against the source through _venue_compatible(), where
    # venue_head("Frankston, VIC") is a prefix of venue_head("Frankston
    # Brewhouse") and so counts as the same place. The narrower string is the
    # one that has to go, so the upgrade is keyed on the venue head being a
    # strict prefix -- an equal head, or an unrelated one, is left alone.
    if (candidate.get("location") or "").strip():
        old_head, new_head = venue_head(existing.get("location")), \
            venue_head(candidate.get("location"))
        if new_head and old_head and new_head != old_head and new_head.startswith(old_head):
            existing["location"] = candidate["location"].strip()


def _justification_keys(row, today):
    """The (name, url, timestamp, venue) tuples `row` justifies in the store.

    A source-supplied dated row stands for exactly one slot. A dateless row
    stands for whatever recurrence.py expands it into, which is computed here
    with the same function the pipeline uses so the two cannot drift.
    """
    name = normalize_name(row.get("name"))
    url = (row.get("source") or "").rstrip("/")
    if not name:
        return set()
    iso = str(row.get("datetime_iso") or "")
    if iso and row.get("has_real_date", True):
        return {(name, url, iso[:16], venue_head(row.get("location")))}
    keys = set()
    try:
        made, _reason = infer_event(row, today)
    except Exception:
        return keys
    for made_row in made:
        keys.add((name, url, str(made_row.get("datetime_iso"))[:16],
                  venue_head(made_row.get("location"))))
    return keys


def reconcile_store(rows, live_rows, today=None, report=True):
    """Drop stored rows that this run's sources no longer justify.

    The store is a cache of what the sources have published, and a cache is
    only correct if every entry can still be re-derived from the source it
    came from. Two things broke that, because the merge is append-only and
    nothing ever deleted:

    * **A listing's time is corrected upstream.** The Granicus listing card
      yields a date with no time and the detail page supplies one, so the
      same URL is fetched twice with different times. Both land in the store
      and the earlier one is never removed.
    * **A listing is withdrawn, or the page is edited.** A store expansion
      of twelve inferred rows outlives the listing it came from, so the
      calendar keeps showing an event the venue has dropped.

    Both are invisible to the exact-duplicate checks, because the stale row
    has a *different* timestamp or venue from the row that replaced it --
    that difference is the whole point.

    A row is judged only against the sources it claims (`source` plus every
    URL merged into `sources`), so a row is kept as long as one source still
    vouches for it. And it is only dropped when its own source_label was
    crawled successfully this run: a source that is absent entirely (a
    seasonal festival out of season, a fetcher that failed) must not take its
    existing rows down with it, so those rows are left for the 90-day prune.
    """
    today = today or reference_today()
    live_labels = {r.get("source_label") for r in live_rows if r.get("source_label")}

    # Only expand the dateless listings the store actually references, so the
    # cost is bounded by the store rather than by the whole crawl.
    referenced = set()
    for r in rows:
        for url in [r.get("source")] + list(r.get("sources") or []):
            if url:
                referenced.add((normalize_name(r.get("name")),
                                url.rstrip("/")))
    justified = set()
    for src in live_rows:
        key = (normalize_name(src.get("name")),
               (src.get("source") or "").rstrip("/"))
        if key not in referenced:
            continue
        justified |= _justification_keys(src, today)

    # venue compatibility, so a venue string enriched from a sibling source
    # does not read as a contradiction.
    by_slot = {}
    for name, url, stamp, venue in justified:
        by_slot.setdefault((name, url, stamp), set()).add(venue)

    # The live row behind each slot, so a stored field the source has since
    # *corrected* can be re-derived. _merge_sources() only fills blanks, by
    # design -- a field the store already has wins -- which is right when the
    # store's value is merely plainer but wrong when the source has since
    # cleaned it up. 86 rows carried "14 Willis St,, Hampton, Victoria 3188"
    # and 96 carried "$12 per session FIND OUT MORE BUTTON Find Out More",
    # both from fetcher bugs fixed here, and no amount of re-crawling would
    # have cleared them: the store is append-only by design.
    #
    # Two rules keep this from clobbering a good value with a worse one. An
    # address is only replaced when the stored one is *malformed*; a price is
    # only replaced when the live one is *shorter*, which for a cost field
    # means the page furniture has been cut and a real amount removed with it.
    # A long-but-clean value is left exactly as the store has it.
    live_by_slot = {}
    for src in live_rows:
        iso = str(src.get("datetime_iso") or "")
        if not iso:
            continue
        key = (normalize_name(src.get("name")),
               (src.get("source") or "").rstrip("/"), iso[:16])
        live_by_slot.setdefault(key, []).append(src)
    # A dateless listing expands into many store rows -- a weekly course is
    # one snapshot row and a dozen published ones -- so a store row has no
    # single live row to match on its timestamp. Fall back to (name, url) for
    # those, which is the same key _justification_keys() uses.
    live_by_listing = {}
    for src in live_rows:
        key = (normalize_name(src.get("name")),
               (src.get("source") or "").rstrip("/"))
        live_by_listing.setdefault(key, []).append(src)

    kept, dropped = [], []
    repaired = 0
    for r in rows:
        iso = str(r.get("datetime_iso") or "")
        if not iso or r.get("source_label") not in live_labels:
            kept.append(r)
            continue
        name = normalize_name(r.get("name"))
        venue = venue_head(r.get("location"))
        urls = [(r.get("source") or "").rstrip("/")] + \
               [u.rstrip("/") for u in (r.get("sources") or []) if u]
        ok = False
        for url in urls:
            if not url:
                continue
            venues = by_slot.get((name, url, iso[:16]))
            if venues is None:
                continue
            if not venue or venue in venues:
                ok = True
                break
            if any(_venue_compatible(v, venue) for v in venues if v):
                ok = True
                break
        if ok:
            kept.append(r)
        else:
            dropped.append(r)
        # A row the source vouches for, but whose address or location the
        # source has since corrected into a well-formed value. Applied after
        # the keep/drop decision so it cannot affect which rows survive.
        for url in urls:
            if not url:
                continue
            candidates = live_by_slot.get((name, url, iso[:16]))
            if not candidates:
                candidates = live_by_listing.get((name, url), ())
            for live in candidates:
                for field in ("address", "location"):
                    stored = (r.get(field) or "").strip()
                    fresh = (live.get(field) or "").strip()
                    if stored and fresh and _malformed(stored) \
                            and not _malformed(fresh):
                        r[field] = fresh
                        repaired += 1
                # A price the source has since cleaned. The same
                # never-overwrite rule applies: a fetcher that used to publish
                # the page's call-to-action as a price ("\$12 per session FIND
                # OUT MORE BUTTON Find Out More", 96 rows) leaves the store
                # holding it, because _merge_sources() only fills blanks.
                # Tested on the *live* value, not a pattern, so a genuinely
                # long price is never truncated.
                stored_price = (r.get("price_text") or "").strip()
                fresh_price = (live.get("price_text") or "").strip()
                if stored_price and fresh_price and len(fresh_price) < len(
                        stored_price):
                    r["price_text"] = fresh_price
                    repaired += 1
                break

    if repaired and report:
        print(f"  Repaired {repaired} stored field(s) the source has since "
              f"corrected (malformed address, re-derived from the live row)")
    if dropped and report:
        print(f"  Dropped {len(dropped)} stored rows the sources no longer "
              f"publish (corrected time, or listing withdrawn)")
        for r in dropped[:10]:
            print(f"    {r.get('name')!r} {str(r.get('datetime_iso'))[:16]} "
                  f"[{r.get('source_label')}] {r.get('location')!r}")
        if len(dropped) > 10:
            print(f"    ... and {len(dropped) - 10} more")
    return kept, dropped


def _malformed(value):
    """True when an address string is visibly broken rather than merely terse.

    Two shapes, both produced by joining pre-punctuated parts: an empty
    segment ("14 Willis St,, Hampton") and a dangling separator at either end.
    Checked on the stored value so only a broken one is replaced -- a terse or
    unusual but well-formed address is left as the store has it, because the
    store's value may have come from a second source that knew better.
    """
    v = (value or "").strip()
    if not v:
        return False
    return ",," in v or v.startswith(",") or v.endswith(",")


def _local(value):
    """Naive Melbourne-local datetime from an ISO string.

    Aware stamps must be converted, not merely stripped: dropping the offset
    would compare UTC-naive against a local cutoff and mis-prune events within
    the UTC offset of the 90-day boundary.
    """
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(LOCAL_TZ).replace(tzinfo=None)
    return dt


def deduplicate(new_events, existing_events):
    merged = list(existing_events)
    index = {}
    name_loc_index = {}
    date_index = {}
    for e in merged:
        # slot_hash keeps the start time, so two sessions of one class at a
        # venue on the same day ("Cert II in EAL" 9am and 12:30pm) do not
        # collapse into a single event.
        index.setdefault(slot_hash(e), e)
        n = normalize_name(e.get("name", ""))
        l = normalize_location(e.get("location", ""))
        if n and l:
            name_loc_index.setdefault((n, l), []).append(e)
        dp = date_part(e.get("datetime_iso"))
        if dp:
            date_index.setdefault(dp, []).append(e)

    new_count = 0
    for candidate in new_events:
        cand_hash = slot_hash(candidate)

        # Pass 1: exact (name + date + location)
        hit = index.get(cand_hash)
        if hit is not None:
            _merge_sources(hit, candidate, refresh_source=True)
            continue

        found = False
        cand_name = normalize_name(candidate.get("name", ""))
        cand_loc = normalize_location(candidate.get("location", ""))
        # Computed once, up front, and unconditionally: both pass gates below
        # and the index update at the end of the loop read it, so a candidate
        # with no datetime_iso used to reach them unassigned and raise
        # UnboundLocalError, killing the whole run.
        cand_dp = date_part(candidate.get("datetime_iso"))

        # Pass 2: same name+location across sources. Only merge when BOTH
        # have real dates on the SAME day AND at the same time of day.
        # Dateless rows never trigger cross-source merges in Pass 2 (per user
        # rule: dateless is not an event).
        if cand_name and cand_loc:
            if cand_dp:
                for existing in name_loc_index.get((cand_name, cand_loc), []):
                    exist_dp = date_part(existing.get("datetime_iso"))
                    if not exist_dp:
                        continue
                    if cand_dp != exist_dp:
                        continue
                    if not _same_time_of_day(candidate, existing):
                        continue
                    _merge_sources(existing, candidate, refresh_source=True)
                    index.setdefault(cand_hash, existing)
                    found = True
                    break

        # Pass 3: fuzzy (same day, close time, strict location)
        if not found and cand_dp:
            for existing in date_index.get(cand_dp, []):
                if not existing.get("datetime_iso"):
                    continue
                if name_similarity(existing.get("name", ""),
                                   candidate.get("name", "")) < 0.75:
                    continue
                if not location_matches(existing.get("location", ""),
                                        candidate.get("location", "")):
                    continue
                if not time_matches(existing.get("datetime_iso", ""),
                                    candidate.get("datetime_iso", "")):
                    continue
                _merge_sources(existing, candidate, refresh_source=True)
                found = True
                break

        if not found:
            candidate["sources"] = [candidate.get("source", "")]
            merged.append(candidate)
            index.setdefault(cand_hash, candidate)
            if cand_name and cand_loc:
                name_loc_index.setdefault((cand_name, cand_loc), []).append(candidate)
            if cand_dp:
                date_index.setdefault(cand_dp, []).append(candidate)
            new_count += 1

    return merged, new_count


def _normalize_raw(row, quiet=False):
    """Backfill fields for archived/webfetch rows lacking fetch normalization."""
    row.setdefault("source_label", row.get("source_id", "unknown"))
    # Normalise first: " " and "None" are both truthy but carry no date, and
    # would otherwise build a bogus content hash off a whitespace day-part.
    iso = (row.get("datetime_iso") or "").strip() \
        if isinstance(row.get("datetime_iso"), str) else row.get("datetime_iso")
    row["datetime_iso"] = iso or None
    if "has_real_date" not in row:
        row["has_real_date"] = bool(iso)
    # A row that carries a date but is flagged as not having a real one is
    # self-contradictory: an old bug stamped such rows with the fetch time, so
    # the published date silently became the day the pipeline ran. Trust the
    # flag -- without a real date the row goes back to recurrence.py, which
    # re-derives it from the text or drops it.
    if row["datetime_iso"] and not row["has_real_date"]:
        if not quiet:
            print(f"  discarding unstamped date for {row.get('name')!r} "
                  f"({row['datetime_iso']}); will re-derive from text")
        row["datetime_iso"] = None
    if not isinstance(row.get("sources"), list):
        row["sources"] = [row.get("source", "")]
    # Two fields no consumer reads: `datetime_display` (a formatted copy of
    # datetime_iso, which the page recomputes) and `date_text` (never read by
    # anything, including back when the archive fixture carried it). No fetcher
    # writes them any more, but a snapshot committed before that still carries
    # them, and the store is supposed to be the documented schema rather than
    # whatever the last fetch happened to emit.
    row.pop("datetime_display", None)
    row.pop("date_text", None)
    # Dedupe sources list
    seen, uniq = set(), []
    for s in row["sources"]:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    row["sources"] = uniq
    return row


def dedupe_exact(rows):
    """Collapse exact (name, start time, location) duplicates only.

    Re-run after date inference, where a whole recurring series has just been
    materialised. Fuzzy Pass 3 is unsafe there: sibling courses at one venue
    ('Cert I in EAL' vs 'Cert III in EAL', same Monday 9am) score above 0.75
    and would silently fuse into one event. The start time is part of the key
    so two sessions of one class in the same day both survive.
    """
    index, out = {}, []
    for r in rows:
        key = slot_hash(r)
        hit = index.get(key)
        if hit is not None:
            # This is where an already-expanded stored series meets the fresh
            # expansion of the same dateless listing, so it is also where a
            # re-slugged source URL gets replaced. `_merge_sources` records
            # the superseded URL in `sources` either way.
            _merge_sources(hit, r, refresh_source=True)
            continue
        index[key] = r
        out.append(r)
    return out


def prune_old(rows, days=PRUNE_DAYS, today=None):
    # datetime.combine(day, dt_time.min) -- datetime.min is a *time* object
    # and raises AttributeError, and the stdlib `time` module is shadowed here
    # by the retry helper's import, so use an aliased datetime.time.
    cutoff = datetime.combine(today or reference_today(), dt_time.min) \
        - timedelta(days=days)
    kept = []
    pruned = 0
    for r in rows:
        iso = r.get("datetime_iso")
        if iso:
            try:
                if _local(iso) < cutoff:
                    pruned += 1
                    continue
            except (ValueError, TypeError):
                pass
        kept.append(r)
    return kept, pruned


def load_live_inputs(quiet=False):
    """Every row the sources published this run, normalised.

    raw_events.json plus the archived fixtures plus every webfetch snapshot.
    Shared with health_check.py, which re-runs reconcile_store() over this to
    assert the published store is still what the sources justify -- if the two
    ever load different inputs, that check would pass while the pipeline had
    already drifted.
    """
    raw = read_json(ROOT / "data" / "raw_events.json")
    if raw is None:
        if quiet:
            return None
        print("FAIL: data/raw_events.json missing - run fetch_sources.py first")
        sys.exit(1)
    if not isinstance(raw, list):
        if quiet:
            return None
        print("FAIL: data/raw_events.json is not a JSON array")
        sys.exit(1)

    archived = read_json(ROOT / "scripts" / "archived_events.json", default=[])
    if isinstance(archived, list):
        if not quiet:
            print(f"Archived events: {len(archived)}")
        raw.extend(archived)
    elif not quiet:
        print("No archived events file found")

    snap_count = 0
    snapshot_failures = []
    for path in sorted(glob.glob(str(ROOT / "scripts" / "webfetch_snapshots" / "*.json"))):
        try:
            snap = read_json(path)
            if isinstance(snap, dict):
                snap = snap.get("rows", [])
            if not isinstance(snap, list):
                # list.extend() on a string would append one garbage row per
                # character.
                if not quiet:
                    print(f"Snapshot {path}: unexpected shape "
                          f"{type(snap).__name__}, skipped")
                snapshot_failures.append(path)
                continue
            if not quiet:
                print(f"Snapshot {path}: {len(snap)}")
            raw.extend(snap)
            snap_count += len(snap)
        except (json.JSONDecodeError, OSError) as e:
            if not quiet:
                print(f"Snapshot {path}: FAILED {e}")
            snapshot_failures.append(path)
    if not quiet:
        print(f"Webfetch snapshots total: {snap_count}")
    if snapshot_failures:
        if quiet:
            return None
        print(f"FAIL: {len(snapshot_failures)} snapshot file(s) unreadable: "
              f"{snapshot_failures}")
        sys.exit(1)
    return [_normalize_raw(r, quiet=quiet) for r in raw if isinstance(r, dict)]


def main():
    # Already normalised by load_live_inputs. Snapshot what the sources
    # justify *before* the merge mutates them: _merge_sources() fills a kept
    # row's blank location from its twin, which would change the very keys
    # reconcile_store() compares against.
    live = load_live_inputs()
    raw = [dict(r) for r in live]

    try:
        existing = read_json(ROOT / "data" / "events.json", default={}).get("rows", [])
    except json.JSONDecodeError as e:
        # A truncated events.json wedges every later run; say so explicitly
        # rather than surfacing a stack trace deep in the merge.
        print(f"FAIL: data/events.json is corrupt ({e}). "
              f"Restore it from git and re-run.")
        sys.exit(1)

    # The canonical store also needs normalising: rows written by older runs
    # can carry an unstamped date, and merging would carry it straight through.
    existing = [_normalize_raw(r) for r in existing if isinstance(r, dict)]
    # Collapse pre-existing duplicates already in the canonical store
    # (legacy runs appended dateless rows without dedup).
    existing_clean, _ = deduplicate(existing, [])
    collapsed = len(existing) - len(existing_clean)
    if collapsed:
        print(f"Collapsed {collapsed} legacy duplicates in store")
    existing = existing_clean
    today = reference_today()
    # Dedupe within the incoming batch first (collapses fetch-time dups),
    # then merge against canonical store.
    batch_deduped, _ = deduplicate(raw, [])
    merged, new_count = deduplicate(batch_deduped, existing)
    merged = dedupe_exact(merged)
    # Inferred rows are stored, so a bad inference persists until refreshed.
    merged = refresh_inferred(merged, today)
    merged, inferred = resolve_dateless(merged, today)
    print(f"Dates inferred: {inferred['expanded']} dateless rows expanded, "
          f"{inferred['dropped']} undatable rows dropped")
    for name, reason in sorted(inferred["reasons"].items()):
        print(f"  dropped: {name} ({reason})")
    merged = dedupe_exact(merged)
    # After inference: the duplicated rows are date_inferred, so this pass only
    # has something to match once resolve_dateless has materialised them.
    merged = dedupe_by_source_url(merged)
    merged = dedupe_exact(merged)
    # A midnight row is a restatement of a timed session, not an extra event.
    merged = drop_untimed_twins(merged)
    # Everything the sources still publish has now been merged in, so the
    # store can be held against them: a row none of its own sources justify is
    # a leftover from a listing that was corrected or withdrawn.
    merged, stale_stored = reconcile_store(merged, live, today)
    merged, pruned = prune_old(merged, PRUNE_DAYS, today)
    # No local "nothing publishes without a date" filter here: resolve_dateless
    # keeps a row only when it already has a datetime_iso or infer_event dated
    # it, and everything between here and there only ever drops rows. So such a
    # filter could not fire. health_check.py checks the same invariant on the
    # published artefact instead, which is where a violation would actually
    # matter.
    print(f"After dedup: {len(merged)} ({new_count} new, {pruned} pruned "
          f">{PRUNE_DAYS}d, {inferred['dropped']} undatable, "
          f"{len(stale_stored)} unbacked)")

    write_json(ROOT / "data" / "events.json", {"rows": merged})
    print("Wrote data/events.json")


if __name__ == "__main__":
    main()
