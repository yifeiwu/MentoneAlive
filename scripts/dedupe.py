"""Deduplicate events across sources.

Passes:
   1. Exact hash on (normalized_name, start time, normalized_location).
   The start time, not the date: a venue can legitimately run the same class
   twice in one day, and a date-only key erased the second session. Seconds are
   dropped, so a fetch-time difference between two reads is not a difference.
2. Same name+location across different sources (user rule): if normalized
   name and location are equal but dates differ/empty, merge sources and
   keep the dated version. Collapses cross-source duplicates.
3. Fuzzy: name similarity >= 0.75 AND same day within +/-30min AND
   location equality (strict, not substring).

Prunes rows older than 90 days. Merges raw + archived +
scripts/webfetch_snapshots/*.json. Rows still lacking a date afterwards are
resolved by scripts/recurrence.py, which infers them from the description and
drops whatever it cannot date — every published event carries a real date.

reconcile_store() then drops any stored row that none of its own recorded
sources still justify, and repairs the fields a source has since corrected.
The store is append-only by design, so that repair pass is the only thing that
can clear a value a fixed fetcher used to write: it replaces a field only when
the stored one is known to be wrong (a malformed address, a description that
restates the row's own name), never merely because the store's value is
plainer than the source's.
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

import config as _config
from jsonio import read_json, write_json
from recurrence import (infer_event, refresh_inferred, resolve_dateless,  # noqa: F401
                        series_id_for)
from webfetch_http import price_sort, restates_name

PRUNE_DAYS = 90
ROOT = Path(__file__).resolve().parent.parent
# All published times are Melbourne local. Aware source stamps are converted
# to this zone rather than having their offset stripped.
LOCAL_TZ = ZoneInfo("Australia/Melbourne")


def _slot_stamp(value):
    """A stamp to the minute, Melbourne local, or "" when it states none.

    The one reader of "what slot is this row in". `slot_hash()` and
    `dedupe_by_source_url()` each had their own copy and they disagreed: the
    fallback for a stamp too malformed to convert tested for a "T" in one copy
    and not the other, so a timestamp that was unreadable *and* date-shaped
    produced "" from one and a garbage 16-character key from the other -- and
    the caller's own `if "T" not in stamp` guard, which is there to skip
    undated rows, then waved that garbage through as a real slot.

    An aware stamp is converted rather than stripped, and microseconds are
    dropped, so a fetch-time difference between two reads of one listing is not
    a difference of slot.
    """
    raw = str(value or "")
    if "T" not in raw:
        return ""
    try:
        return _local(raw).strftime("%Y-%m-%dT%H:%M")
    except (ValueError, TypeError):
        return raw.replace("Z", "")[:16]


def _slot_key(row):
    """The (name, url, start) tuple every live-slot index is keyed on.

    Used by both the index that says whether a slot is stated and the one that
    supplies a live row to repair against, so the two cannot index the same
    listing two different ways.
    """
    return (normalize_name(row.get("name")),
            (row.get("source") or "").rstrip("/"),
            _slot_stamp(row.get("datetime_iso")))


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
    """The normalised programme name every identity comparison runs on.

    Accents folded, trademark marks dropped, and a trailing qualifier removed --
    so "Zumba® Gold", "Zumba Gold" and "Zumba Gold (Mondays)" are one string.
    The qualifier is stripped *here* rather than only in `name_head()` because
    the fuzzy pass compares `name_similarity()`, which uses this function: left
    on, the qualified name scored 0.84 against the bare one and the class
    published twice at the same hall, hour and date.
    """
    return _NAME_NOISE_RE.sub("", fold_accents(_subtitle_split(name)))


def _subtitle_split(name):
    s = " ".join((name or "").lower().strip().split())
    return _SUBTITLE_SPLIT_RE.split(s)[0].strip()


def fold_accents(text):
    """Strip diacritics: 'Café' and 'Cafe' name the same programme.

    One source copies 'Chatty Café' from the venue's own site while the
    national directory writes 'Chatty Cafe' unaccented.

    Also drops the trademark marks, which are noise in a programme name: the CCC
    site titles a class 'Zumba® Gold' where the Kingston Seniors guide writes
    'Zumba Gold (Mondays)', and 0.65 similarity kept the two apart even though
    they are the same class at the same hall on the same day at the same hour.
    A symbol that carries no identifying information is not part of the identity.
    """
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(c for c in decomposed
                       if not unicodedata.combining(c)
                       and c not in "®™©")
    return unicodedata.normalize("NFKC", stripped)


# A subtitle or qualifier after a separator: "Chatty Cafe - Connect over a
# Cuppa" and "Chatty Cafe" are the same program named by two sources.
_SUBTITLE_SPLIT_RE = re.compile(r"\s+[-|:–—]\s+")

# Generic trailing tags that carry no programme identity.
#
# The weekday qualifier is here because sources add one to the same class on
# different days: the Seniors guide writes "Zumba Gold (Mondays)" and "Zumba Gold
# (Fridays)" where the CCC site writes plain "Zumba® Gold". Those are one
# programme, and with the suffix left on, the qualified name scored 0.65 against
# the bare one -- below the fuzzy threshold -- so Monday's class published
# twice at the same hall, hour and date. They still do not merge with each
# other: two sessions of one class are on different days, and the merge also
# requires the same day.
_NAME_NOISE_RE = re.compile(
    r"\s*\((?:new|sessions?|series|class|term \d+"
    r"|mon|tues|wednes|thurs|fri|satur|sun)days?"
    r"(?:\s*(?:&|and|,|/)\s*(?:mon|tues|wednes|thurs|fri|satur|sun)days?)*"
    r"\)\s*$", re.I)


def name_head(name):
    """The programme-identifying part of a name, ignoring any subtitle.

    'Chatty Cafe - Connect over a Cuppa' -> 'chatty cafe'
    'Love to Live - Gentle Chair-Based Exercise' -> 'love to live'
    """
    return normalize_name(name)


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
    time only match each other, so a timed session never collapses into an
    all-day (midnight) listing for the same programme.
    """
    def _tod(row):
        try:
            return _local(row.get("datetime_iso") or "").strftime("%H:%M")
        except (ValueError, TypeError):
            return ""
    ta, tb = _tod(a), _tod(b)
    untimed = {"", "00:00"}
    if ta in untimed or tb in untimed:
        return ta in untimed and tb in untimed
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
    key = (f"{normalize_name(event.get('name', ''))}|"
           f"{_slot_stamp(event.get('datetime_iso'))}|"
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
    if a.startswith(b) or b.startswith(a):
        # Prefix alone over-matches ("Park" vs "Parkview Tavern"), so require
        # the match to end on a word boundary.
        longer, shorter = (a, b) if len(a) >= len(b) else (b, a)
        rest = longer[len(shorter):]
        if not rest or rest[0] in " ,-/(":
            return True
    return False


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
        stamp = _slot_stamp(r.get("datetime_iso"))
        name = normalize_name(r.get("name", ""))
        if not stamp or not name:
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


def _slot_urls(row):
    """Every URL this row claims, primary first. A row is judged only against
    its own sources, so a row merged from two listings keeps both claims here
    and loses neither when one of them stops publishing it."""
    return [(row.get("source") or "").rstrip("/")] + \
           [u.rstrip("/") for u in (row.get("sources") or []) if u]


def _live_slot_index(live_rows):
    """(name, url, start) -> {venue head} for every dated row the sources state.

    Built here rather than inside reconcile_store() because two passes now read
    it, and the codebase's standing rule is that a check and the merge enforcing
    it must compute the same thing: if they disagree, one of them reports a
    duplicate the other considers distinct and neither names the disagreement.
    """
    index = {}
    for src in live_rows:
        if not src.get("datetime_iso"):
            continue
        index.setdefault(_slot_key(src), set()).add(
            venue_head(src.get("location")))
    return index


def _slot_is_stated(row, by_slot):
    """True when a live row states this exact slot for one of the row's URLs.

    The name, the start to the minute and a compatible venue all have to line
    up. The venue check is what keeps genuinely distinct events apart -- two
    STEADYstrength classes at two different halls, or "Chatty Cafe - Game On!"
    at 10:00 against the 11:00 series.
    """
    name = normalize_name(row.get("name"))
    venue = venue_head(row.get("location"))
    iso = _slot_stamp(row.get("datetime_iso"))
    for url in _slot_urls(row):
        if not url:
            continue
        venues = by_slot.get((name, url, iso))
        if venues is None:
            continue
        if not venue or venue in venues:
            return True
        if any(_venue_compatible(v, venue) for v in venues if v):
            return True
    return False


# The CMS's own label for the fact that a listing names its *next* occurrence
# rather than this event: "Next date: Saturday, 03 October 2026 | 11:00 AM  to
# 04:00 PM". Matched anywhere in the field, not only at the front, because the
# same field leads with the venue's status when there is one ("Sold out: Next
# date: ..." -- status.py reads that prefix, it does not write it).
_NEXT_DATE_RE = re.compile(r"\bNext date:", re.I)


def names_next_occurrence(text):
    """True when a listing's date field names its next occurrence, not its date.

    This is the whole discriminator between one listing restated by later
    fetches and a listing that genuinely runs many times. It has to be read off
    the source's own words because the two are otherwise identical: a
    multi-day listing materialised one row per day looks exactly like a listing
    re-dated per run -- same name, same URL, consecutive days, same time of
    day. `Fairies at Rippon Lea` is one row per day of a run that really does
    happen on each of them, `Holiday Activities` says `daily. 8:45am-4:15pm`,
    and both list their own dates rather than pointing at a cursor. The
    difference is that neither says "Next date".
    """
    return bool(_NEXT_DATE_RE.search(text or ""))


def drop_superseded_listing_rows(rows, live_rows, crawling=None):
    """Drop the copies of one listing that later fetches replaced.

    A listing that runs for weeks is re-read every day, and its date text leads
    with the *next* occurrence rather than the start of the run. So each fetch
    yields the same listing at a different start time, every dedupe key in this
    file carries the start time, and the store accumulated a copy per run:
    `'Kingston Sounds' by Susannah Langley` reached eight rows, one per run,
    each restating one exhibition. `reconcile_store()` could not see it, because
    its series test keeps any row whose (source, name, venue) is still
    published -- which is the right rule for a materialised series and the wrong
    one here, where the rows carry real source-stated times that disagree.

    What separates the two is `names_next_occurrence()`: a listing that names
    its next occurrence is naming a cursor, so several rows of it at different
    times are several readings of one event. The rows of a materialised series
    state *their own* date and no cursor (`'4/09/2026 9:30:00 AM'`), which is why
    this leaves the 58 stored Mahjong occurrences and the 35 it publishes
    alone, and why the rolling window behind them never slides.

    Even inside such a group a row is only dropped when the source no longer
    states its slot, so a page that really does publish two sessions at two
    different times keeps both. The guards on the source are reconcile_store's:
    a source absent from this run (out of season, or a fetch that failed) and a
    source mid-crawl both cannot say which copy is current, and neither takes its
    rows down. `crawling` is a parameter so that guard can be exercised without
    a progress file having to exist on disk.
    """
    by_slot = _live_slot_index(live_rows)
    live_labels = {r.get("source_id") for r in live_rows if r.get("source_id")}
    if crawling is None:
        crawling = _unfinished_crawl_sources()

    # Only the rows that name a cursor are judged, and only where a listing has
    # more than one of them: one reading of a cursor is not a superseded copy,
    # it is just the listing. Keyed on name and URL alone, so a re-slugged page
    # and a second listing at the same venue stay apart.
    groups = {}
    for r in rows:
        if not names_next_occurrence(r.get("datetime_text")):
            continue
        groups.setdefault((normalize_name(r.get("name")),
                           (r.get("source") or "").rstrip("/")), []).append(r)
    crowded = {key for key, grp in groups.items() if len(grp) > 1}

    stale = set()
    for key, grp in groups.items():
        if key not in crowded:
            continue
        for r in grp:
            # Same guards as reconcile_store: a source that did not report, or
            # whose crawl is unfinished, cannot say which reading is current.
            if (r.get("source_id") in crawling
                    or r.get("source_id") not in live_labels
                    or _slot_is_stated(r, by_slot)):
                continue
            stale.add(id(r))

    kept, dropped = [], []
    for r in rows:
        if id(r) in stale:
            dropped.append(r)
            continue
        kept.append(r)
    if dropped:
        print(f"  Dropped {len(dropped)} row(s) restating one listing a later "
              f"fetch re-dated (the listing names its next occurrence)")
        for r in dropped[:10]:
            print(f"    {r.get('name')!r} {str(r.get('datetime_iso'))[:16]} "
                  f"[{r.get('source_id')}]")
        if len(dropped) > 10:
            print(f"    ... and {len(dropped) - 10} more")
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
    # A delisted series stops being archived the moment a live source publishes
    # it again. Without this, re-listing a programme leaves it hidden forever:
    # the archive is the older row, so it is the one kept, and the flag set at
    # load time survives every run.
    if refresh_source:
        existing.pop("archived", None)
    cand_src = candidate.get("source", "")
    if cand_src and cand_src not in existing["sources"]:
        existing["sources"].append(cand_src)
    if refresh_source and cand_src and cand_src != existing.get("source"):
        existing["source"] = cand_src
    # Prefer keeping a real date over a dateless duplicate.
    if not existing.get("datetime_iso") and candidate.get("datetime_iso"):
        for k in ("datetime_iso", "datetime_text"):
            # `is not None` would skip a genuine empty string, leaving the
            # dateless row with a blank display after gaining a real date.
            if candidate.get(k):
                existing[k] = candidate[k]
        # The date is now source-supplied, not inferred. Leaving the flag set
        # would make resolve_dateless keep ignoring this row's real date, so a
        # dateless twin is never suppressed in favour of it.
        existing.pop("date_inferred", None)
        existing.pop("recurrence", None)
        existing.pop("series_id", None)
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


def _cadence_label(label):
    """A schedule label with the run-date-dependent parenthetical removed.

    `infer_event` labels a run as "Every Monday (to 14 Dec 2026)" or "Every
    Tuesday (6 weeks)", both of which read differently as the run date moves:
    the end date is fixed but "(N weeks)" shrinks every day. Comparing those
    labels whole therefore reintroduced exactly the sliding window this
    function's series test exists to avoid -- 48 rows dropped seven days after a
    build. What identifies the schedule is the cadence itself, so the
    parenthetical is dropped and "Every Monday" is compared with "Every Monday"
    whatever the term looks like today.
    """
    return re.sub(r"\s*\([^)]*\)\s*$", "", label or "").strip().lower()


def _unfinished_crawl_sources():
    """Sources with a crawl in progress, from the progress files on disk.

    Read from the filesystem rather than passed in, because the two are the
    same fact and this is where the store decides what a source justifies. The
    rows are already in the store when this runs -- a progress file holding real
    rows was merged once, and 16 of them survived every later run because a
    configured source that produced nothing this run is indistinguishable from
    a source that is merely quiet, which is the rule that keeps a failed fetch
    from withdrawing good rows. A progress file is the one thing that says
    "this source has not finished being crawled", so it has to be consulted
    there or the distinction is lost.
    """
    out = set()
    for p in glob.glob(str(ROOT / "scripts" / "webfetch_snapshots"
                           / "*.progress.json")):
        sid = Path(p).name[:-len(".progress.json")]
        out.add(sid)
    return out


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

    Two ways to be judged, and a row needs only one of them:

    * A **series** row is justified while its series is still published. This is
      a set membership test, which is why it is stable: the older test
      re-expanded the listing's prose with the *current* run date and compared
      timestamps, so the window slid forward every run and dropped correct rows
      in bulk (97 eight days after a build, 382 two months after).
    * A **one-off** has no series and corresponds to exactly one slot, so it is
      matched on (name, url, timestamp, venue).

    The series test is computed rather than read, so it also covers the rows
    that have no `series_id` written on them -- notably a one-off that was a
    dated listing when the store was built and whose source has since switched
    to publishing a dateless recurring schedule. Those rows were justified by
    the old code through `infer_event`, and would otherwise be dropped for
    having no live row to match a timestamp against.

    A row is judged only against the sources it claims (`source` plus every
    URL merged into `sources`), and it is only dropped when its own source_id
    was crawled successfully this run: a source that is absent entirely (a
    seasonal festival out of season, a fetcher that failed) must not take its
    existing rows down with it, so those rows are left for the 90-day prune.
    """
    today = today or reference_today()
    live_labels = {r.get("source_id") for r in live_rows if r.get("source_id")}
    # A source mid-crawl is judged against nothing: its rows are not yet
    # justified by a complete read, and equally not refuted by an incomplete
    # one. Kept as a separate set rather than removed from live_labels so the
    # distinction survives into the row loop, where it decides what happens.
    crawling = _unfinished_crawl_sources()

    # One call per live row rather than one per stored row it might justify,
    # and no expansion: the id is computed from fields the listing already
    # carries, so a dateless listing and its twelve materialised occurrences
    # agree without anything being re-parsed. A row's own stamped id wins,
    # because a source that publishes one is authoritative where ours is
    # inferred (webfetch_everi.py stamps the site's eventIdentifier GUID).
    live_series = set()
    for r in live_rows:
        if r.get("source_id"):
            live_series.add(r.get("series_id") or series_id_for(r))

    # Series membership alone is not enough to justify an inferred row, because
    # the membership test ignores *what* the series says now. A venue that
    # changes its schedule keeps its series id -- that is the point of the id,
    # and it is why the series test replaced the windowed one -- so a store
    # holding "every Tuesday" occurrences is still justified by a series whose
    # listing now says "1st & 3rd Tuesdays of the Month", and the six weekly
    # rows live alongside the nine monthly ones for one venue.
    #
    # So for a dateless listing the dates it currently produces are computed and
    # an inferred row has to be one of them. Only listings with stored inferred
    # rows are expanded: doing it for every live row would re-parse the whole
    # catalogue on every run to check rows that were never inferred.
    stored_series = {r.get("series_id") or series_id_for(r) for r in rows
                     if r.get("date_inferred") and r.get("source_id")}
    # The label the listing's current text expands to, NOT its dates. Comparing
    # dates is what this function used to do and it is the exact failure
    # series_id was introduced to fix: a weekly series publishes its next twelve
    # occurrences from the run date, so a stored row is a snapshot of a window
    # that has moved on, and comparing dates slid the window with it -- 97 rows
    # dropped eight days after a build, 382 after two months. The label is
    # date-independent, so it answers the question actually being asked ("is this
    # row an occurrence of the schedule the source states now?") without reading
    # the run date at all.
    label_of = {}
    for r in live_rows:
        sid = r.get("series_id") or series_id_for(r)
        if sid not in stored_series or sid in label_of:
            continue
        if str(r.get("datetime_iso") or "") and not r.get("date_inferred"):
            # A dated listing publishes its own slots; by_slot judges it.
            label_of[sid] = None
            continue
        # A listing whose only occurrence has passed yields a "label" that is
        # actually a refusal string -- infer_event returns the reason as the
        # second element when it expanded nothing. That is not a cadence, and
        # comparing a stored row against it would drop every row of a series the
        # moment its last date went by, which is the 48-at-seven-days result:
        # 36 gd_libraries rows and 12 greater_dandenong ones, all correct when
        # written. So a non-cadence label means "no opinion" and the series test
        # alone decides, exactly as it did before.
        made, label = infer_event(r, today)
        label_of[sid] = _cadence_label(label) if made and label else None

    # venue compatibility, so a venue string enriched from a sibling source
    # does not read as a contradiction.
    by_slot = _live_slot_index(live_rows)

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
        if not src.get("datetime_iso"):
            continue
        live_by_slot.setdefault(_slot_key(src), []).append(src)
    # A series is one snapshot row and a dozen published ones, so a store row
    # has no single live row to match on its timestamp. Fall back to
    # (name, url), which is the coarse key, and only for reading -- deciding
    # survival is the series_id test above.
    live_by_listing = {}
    for src in live_rows:
        key = (normalize_name(src.get("name")),
               (src.get("source") or "").rstrip("/"))
        live_by_listing.setdefault(key, []).append(src)

    kept, dropped = [], []
    series_kept = slot_kept = 0
    repaired = 0
    for r in rows:
        iso = _slot_stamp(r.get("datetime_iso"))
        if r.get("source_id") in crawling:
            # Mid-crawl. Kept, and not judged: this run read only part of the
            # source, so it cannot say whether a stored row is still published.
            # Dropping here would withdraw a source's rows every scheduled run
            # until its crawl completed, which is the reverse error and just as
            # wrong.
            kept.append(r)
            continue
        if not iso or r.get("source_id") not in live_labels:
            kept.append(r)
            continue
        sid = r.get("series_id") or series_id_for(r)
        if sid in live_series:
            # A live listing publishes this programme at this venue. The row's
            # timestamps are a materialisation of that listing's schedule, not
            # a claim about what the source publishes today, so they are not
            # compared: doing so only measured how far the run date had moved
            # since they were written, which slid the window forward every run
            # and dropped correct rows in bulk.
            #
            # The one comparison that does hold is against the *label* the
            # series' current text expands to, and only for a row the pipeline
            # inferred. That is what stops a changed schedule from accumulating:
            # a venue whose listing moves from "every Tuesday" to "1st & 3rd
            # Tuesdays of the Month" keeps its series id, so membership alone
            # cannot see that six weekly rows were written from a schedule the
            # source no longer states. The label carries no date, so this does
            # not reintroduce the sliding window; and a row the pipeline did not
            # infer is left to the slot test, which is about real timestamps.
            expected_label = label_of.get(sid)
            if (expected_label is not None and r.get("date_inferred")
                    and _cadence_label(r.get("recurrence")) != expected_label):
                # Not an occurrence of the schedule the source states now.
                # Dropped rather than re-dated, because resolve_dateless() has
                # already expanded the current schedule into fresh rows this
                # run; re-dating would invent a second set.
                dropped.append(r)
                continue
            # Deliberately not `continue`:
            # the repair pass below runs for every kept row, and skipping it
            # here is what left 344 directory rows holding the venue's street
            # address and the words "View Map" at the end of their description,
            # forever, because a live row had corrected them and nothing
            # downstream ever overwrites a field the store already has.
            kept.append(r)
            series_kept += 1
        else:
            # Otherwise the row stands for exactly one slot, so it needs a live
            # row stating that slot. This is what covers a one-off whose source
            # has since switched to publishing a dateless recurring schedule.
            if not _slot_is_stated(r, by_slot):
                dropped.append(r)
                continue
            slot_kept += 1
            kept.append(r)
        # A row the source vouches for, but whose address or location the
        # source has since corrected into a well-formed value. Applied after
        # the keep/drop decision so it cannot affect which rows survive.
        name = normalize_name(r.get("name"))
        for url in _slot_urls(r):
            if not url:
                continue
            candidates = live_by_slot.get((name, url, iso))
            if not candidates:
                candidates = live_by_listing.get((name, url), ())
            for live in candidates:
                for field in ("address", "location"):
                    stored = (r.get(field) or "").strip()
                    fresh = (live.get(field) or "").strip()
                    if stored and fresh and _malformed_address(stored) \
                            and not _malformed_address(fresh):
                        r[field] = fresh
                        repaired += 1
                # A price the source has since cleaned. The same
                # never-overwrite rule applies: a fetcher that used to publish
                # the page's call-to-action as a price ("$12 per session FIND
                # OUT MORE BUTTON Find Out More", 96 rows) leaves the store
                # holding it, because _merge_sources() only fills blanks.
                # Tested on the *live* value, not a pattern, so a genuinely
                # long price is never truncated.
                stored_price = (r.get("price_text") or "").strip()
                fresh_price = (live.get("price_text") or "").strip()
                if stored_price and fresh_price and len(fresh_price) < len(
                        stored_price) and (_malformed(stored_price) or len(stored_price) > 60
                        or "BUTTON" in stored_price or "FIND OUT" in stored_price.upper()):
                    r["price_text"] = fresh_price
                    repaired += 1
                # A description carrying page furniture rather than prose. The
                # same never-overwrite rule: only the stored value is judged, so
                # a terse or unusual but well-formed description is left alone.
                # 344 rows held the group's own street address and the words
                # "View Map" at the end of the description, which a directory
                # fetcher produced by reading every paragraph in a column
                # instead of stopping at the Location heading -- shown to a
                # reader, and fed to the classifier as text about a road.
                stored_desc = (r.get("description") or "").strip()
                fresh_desc = (live.get("description") or "").strip()
                if stored_desc and _restates_name(stored_desc,
                                                  r.get("name")):
                    # The stored description is the row's own title, not prose.
                    # Take the source's text where it has one, and clear the
                    # field where it does not -- which is the correct end state
                    # for a listing that states no description, and what
                    # `make_row` now emits for it. Every other rule in this pass
                    # is a never-overwrite rule; this is the one case where the
                    # stored value is known to be wrong rather than merely
                    # plainer, so a blank from the source is the improvement.
                    if fresh_desc and not _restates_name(fresh_desc,
                                                         r.get("name")):
                        r["description"] = fresh_desc
                    else:
                        r["description"] = ""
                    repaired += 1
                elif stored_desc and fresh_desc and len(fresh_desc) < len(stored_desc) \
                        and _page_furniture(stored_desc) and not _page_furniture(fresh_desc):
                    r["description"] = fresh_desc
                    repaired += 1
                # First match wins, and that is a real constraint rather than an
                # incidental one: `candidates` is a list, so a store row whose
                # own first live match is clean never sees a second one that
                # would have repaired it. Deliberate in the sense that a later
                # repair pass could always widen it; undocumented until now,
                # which is the part that mattered -- every other rule in this
                # pass is a never-overwrite rule, so a reader had no reason to
                # expect the search to stop at one candidate.
                break

    if repaired and report:
        print(f"  Repaired {repaired} stored field(s) the source has since "
              f"corrected (a malformed address, or a description that only "
              f"restated the row's own name)")
    if dropped and report:
        print(f"  Dropped {len(dropped)} stored rows the sources no longer "
              f"publish; kept {series_kept} on their series' identity and "
              f"{slot_kept} on a source-stated slot")
        for r in dropped[:10]:
            print(f"    {r.get('name')!r} {str(r.get('datetime_iso'))[:16]} "
                  f"[{r.get('source_id')}] {r.get('location')!r}")
        if len(dropped) > 10:
            print(f"    ... and {len(dropped) - 10} more")
    return kept, dropped


def _page_furniture(value):
    """True when a description ends in layout the page printed, not prose.

    A map link, or an address tail with the line breaks still in it. Both are
    what a fetcher that read the whole content column instead of the
    description produced, and both survive in the store because
    `_merge_sources()` never overwrites a field the store already has.
    """
    v = (value or "").strip()
    if not v:
        return False
    if re.search(r"\bview map\b", v, re.I):
        return True
    return bool(re.search(r"\n\s*\S+,\s*\n", v))


def _restates_name(description, name):
    """True when a description is just the row's own title. See the owner.

    `webfetch_http.restates_name` holds the rule, next to the row shape, because
    `make_row` has to apply the same test to a row as it is written and this
    function applies it to a row already in the store. They were separate
    implementations that disagreed -- this one normalised internal whitespace,
    `make_row` only stripped the ends -- so a description reading " Tai  Chi "
    was kept by the fetcher and recognised as a restatement here.

    Aliased rather than reimplemented so a caller reading this module does not
    have to know where the rule lives.
    """
    return restates_name(description, name)


def _malformed_address(value):
    """True when an address string is visibly broken rather than merely terse.

    Three shapes. Two come from joining pre-punctuated parts: an empty segment
    ("14 Willis St,, Hampton") and a dangling separator at either end. The third
    is a segment repeated immediately after itself, which is what Bayside's own
    venue block renders -- Location is
    `['84 Reserve Road', 'Beaumaris', 'Beaumaris', 'Victoria 3193', 'Australia']`
    -- and ten of its rows were published as
    "84 Reserve Road, Beaumaris, Beaumaris, Victoria 3193".

    The repeat is the one that mattered most and was hardest to see: it is
    well-formed punctuation and a real suburb, so it passed every eyeball and
    every earlier check, and it survived a live re-crawl because nothing
    recognised it as wrong. Checked on the stored value so only a broken one is
    replaced -- a terse or unusual but well-formed address is left as the store
    has it, because the store's value may have come from a second source that
    knew better.

    Deliberately separate from `_malformed`, which is also applied to
    `price_text`: a cost is never a comma-separated address, so the two shapes
    must not share one rule.
    """
    v = (value or "").strip()
    if not v:
        return False
    if ",," in v or v.startswith(",") or v.endswith(","):
        return True
    parts = [p.strip().casefold() for p in v.split(",")]
    return any(p and p == parts[i - 1] for i, p in enumerate(parts) if i)


def _malformed(value):
    """True when a value is visibly broken rather than merely terse.

    The address shapes, applied where a price is the field under test: an empty
    segment or a dangling separator. A price like "$12 per session FIND OUT
    MORE BUTTON" is caught by the longer rules at the call site, not here.
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


def _normalize_raw(row):
    """Backfill fields for archived/webfetch rows lacking fetch normalization.

    It took a `quiet` flag for a while and never read it, so three callers were
    passing `quiet=True` believing it suppressed output. It suppresses nothing.
    """
    row["source_id"] = row.get("source_id") or "unknown"
    # Two fields that were pure duplicates of others, retired rather than kept
    # in step. `source_label` was `source_id` in all 1531 rows under a second
    # name, and `has_real_date` read True for all 1531 because recurrence.py set
    # it on every row it *inferred* -- so a field documented as "the source
    # supplied this date" discriminated nothing, and the self-contradiction
    # check that used to read it could never fire. Popping them here rather than
    # in each reader is what retires them from the store: dedupe.py normalises
    # the canonical rows on every run, so they disappear without a migration.
    row.pop("source_label", None)
    row.pop("has_real_date", None)
    # Normalise first: " " and "None" are both truthy but carry no date, and
    # would otherwise build a bogus content hash off a whitespace day-part.
    iso = (row.get("datetime_iso") or "").strip() \
        if isinstance(row.get("datetime_iso"), str) else row.get("datetime_iso")
    row["datetime_iso"] = iso or None
    # Same repair for `price_sort`. `_merge_sources()` fills blanks only, by
    # design -- a field the store already has wins -- so a row stored before the
    # field existed keeps None indefinitely and the page's "free only" filter
    # misses it. 56 rows carried a price_text with no price_sort, 19 of them
    # reading "Free", so the filter disagreed with the price beside it.
    if row.get("price_sort") is None:
        row["price_sort"] = price_sort(row.get("price_text"))
    # A materialised series row needs its identity, or reconcile_store() falls
    # back to matching its timestamp -- which slides with the run date and
    # drops correct rows in bulk. Rows written before series_id existed have no
    # id, and a series is exactly one (source, name, venue), so deriving it
    # here is a lossless migration rather than a guess: the same listing's
    # twelve occurrences all derive the same value, and so does the live
    # dateless row they came from.
    if row.get("date_inferred") and not row.get("series_id"):
        row["series_id"] = series_id_for(row)
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
    # `_hours_schedule` is scratch space inside webfetch_directory.py: the
    # fetcher carries the page's hours table on the row because that is the only
    # thing both its prose test and its hours test read. It is not part of the
    # row schema, and `_merge_sources()` will not clear a field the store
    # already has, so rows already in the store kept it. Stripped here, with the
    # other pipeline-internal keys, rather than in the fetcher alone.
    row.pop("_hours_schedule", None)
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
    # and raises AttributeError, so the time class is aliased at the import.
    # (The alias used to be justified by this module shadowing the stdlib
    # `time` module, which it never did: the retry helper that shadowed it
    # lives in webfetch_http.py.)
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


_ARCHIVED_REQUIRED = ("name", "source", "source_id")
# The three values `_apply_archived_flags()` knows how to act on. It reads
# `status` directly, so a fourth spelling is not a warning about the file -- it
# is a series that silently stops publishing, because anything that is not
# literally "live" is withheld.
_ARCHIVED_STATUSES = ("live", "unverified", "finished")

# Source ids that exist only in scripts/archived_events.json, which is not in
# sources.yaml and so is not in the loaded config. Named here because a
# snapshot's rows are checked against the set of ids any input may claim, and
# the archive's own ids would otherwise read as unconfigured.
ARCHIVED_SOURCE_IDS = {"bayside_archived", "frankston_archived"}


def _malformed_archived(archived):
    """Problems with `archived_events.json`, as readable lines.

    This file bypasses every other source mechanism: it is not in
    sources.yaml, so `validate_config()` never sees it, it cannot be run with
    `--source`, and no floor or snapshot rule covers it. Nothing enforced its
    shape either -- a row missing `address` or with a mistyped `source_id`
    would fail much later and name a file nobody suspects, in a message about a
    missing address or a missing badge rather than about a typo in the archive.

    `status` is checked because it is the one field here that decides whether a
    series reaches the page: only the literal "live" is listed, and anything
    else -- including a typo, or a value nobody has written yet -- is withheld.
    So a misspelling is not a cosmetic fault in this file, it is a listing that
    quietly disappeared.

    It used to check that `source_label` equalled `source_id` instead, which no
    row in the file could satisfy: the field was retired (`_normalize_raw` pops
    it, D39) and 24 problems were printed on every single run, describing a file
    that was in fact fine.
    """
    problems = []
    if not isinstance(archived, list):
        return problems
    for i, row in enumerate(archived):
        if not isinstance(row, dict):
            problems.append(f"row {i} is a {type(row).__name__}, not an object")
            continue
        for key in _ARCHIVED_REQUIRED:
            if not (row.get(key) or "").strip():
                problems.append(f"row {i} ({row.get('name')!r}) has no {key!r}")
        if row.get("source_id") not in ARCHIVED_SOURCE_IDS:
            problems.append(
                f"row {i} ({row.get('name')!r}) source_id "
                f"{row.get('source_id')!r} is not an archived source "
                f"({', '.join(sorted(ARCHIVED_SOURCE_IDS))}), so its rows are "
                f"treated as unconfigured and skipped")
        status = row.get("status")
        if status not in _ARCHIVED_STATUSES:
            problems.append(
                f"row {i} ({row.get('name')!r}) status {status!r} is not one of "
                f"{', '.join(_ARCHIVED_STATUSES)}; the series is withheld from "
                f"the page unless it reads 'live'")
    return problems


def load_live_inputs(quiet=False, use_raw=True):
    """Every row the sources published this run, normalised.

    raw_events.json plus the archived fixtures plus every webfetch snapshot.
    Shared with health_check.py, which re-runs reconcile_store() over this to
    assert the published store is still what the sources justify -- if the two
    ever load different inputs, that check would pass while the pipeline had
    already drifted.

    `use_raw=False` drops raw_events.json, which is a gitignored scratch file
    that a `fetch_sources.py --source <id>` run overwrites with that one source.
    It exists for a caller that needs a live set determined by *committed* files
    alone -- failure_signals.py, whose whole claim is that the committed store
    reconciles clean, and which cannot assert that against an input that changes
    depending on what was last fetched on this machine.
    """
    raw = read_json(ROOT / "data" / "raw_events.json") if use_raw else []
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
        # Tagged `archived` to say where the row came from, and `status` to say
        # whether its series is still running -- two different facts, and only
        # this file knows either. `archived` alone was the mistake: it made
        # every row in a source delisted, so writing off a source as
        # unreachable also wrote off ten series that were still running.
        for r in archived:
            if isinstance(r, dict):
                r["archived"] = True
        raw.extend(archived)
    elif archived is None:
        if not quiet:
            print("No archived events file found")
    else:
        # A dict-shaped file was previously reported as "no archived events file
        # found", which is false and silent under `quiet` -- so a config mistake
        # read as an absence, and every archived row quietly stopped publishing.
        print(f"FAIL: scripts/archived_events.json is a "
              f"{type(archived).__name__}, not a list of rows")
        if not quiet:
            sys.exit(1)
    if not quiet:
        bad = _malformed_archived(archived)
        for line in bad:
            print(f"  archived_events.json: {line}")

    snap_count = 0
    snapshot_failures = []
    # Every source_id any input may legitimately carry: the raw fetch's rows,
    # the archive's own ids, and every configured source. Snapshot-only sources
    # matter -- a --source run leaves raw_events.json holding one source, so the
    # other snapshots' ids are absent from `raw` and would read as unconfigured.
    # Reading the set from the config is also what stops it drifting.
    known_source_ids = {r.get("source_id") for r in raw if isinstance(r, dict)}
    known_source_ids |= set(ARCHIVED_SOURCE_IDS)
    # config.source_ids() returns None rather than an empty set when sources.yaml
    # is unreadable, and leaving `known_source_ids` as raw+archived is what makes
    # the snapshot walk below skip every snapshot as unconfigured -- toothless
    # rather than wrong, and loud in health_check rather than publishing a
    # fraction. It must not become an empty set: that reads as "every snapshot is
    # stale" and would drop all of them.
    declared = _config.source_ids(quiet=quiet)
    if declared:
        known_source_ids |= declared
    for path in sorted(glob.glob(str(ROOT / "scripts" / "webfetch_snapshots" / "*.json"))):
        try:
            snap = read_json(path)
            if isinstance(snap, dict):
                snap = snap.get("rows", [])
            if isinstance(snap, list):
                # A *.progress.json is crawl bookkeeping, not source rows, and
                # merging one publishes whatever it happens to hold as a source
                # named after the fetcher's own test fixture. It reached the
                # built page once ("Event 0", source_id "t") because the file
                # was in a real directory and nothing checked what was in it.
                # The shape check below cannot catch it -- it is a valid list --
                # so it is excluded by name, and a source_id that no configured
                # or archived source claims is refused as well.
                if path.endswith(".progress.json"):
                    if not quiet:
                        print(f"Skipping crawl progress file "
                              f"{Path(path).name}")
                    continue
                # A partially-crawled source is not a source. The Frankston
                # progress file holds 36 real rows from the 12 pages read so
                # far, and the fetcher correctly refuses to publish them --
                # except this loop reads the file itself, so the guard upstream
                # never applied. Merging them puts a sixth of a directory on
                # the page with nothing recording that it is a sixth.
                claimed = {r.get("source_id") for r in snap if isinstance(r, dict)}
                unknown = claimed - known_source_ids
                if unknown:
                    if not quiet:
                        print(f"Skipping {Path(path).name}: rows from "
                              f"unconfigured source(s) {', '.join(sorted(unknown))}")
                    continue
                # Known source, but its crawl may be unfinished. A progress
                # file is skipped by name above; a *snapshot* is not, and the
                # first 12 pages of the Frankston crawl were written to one by an
                # earlier run, leaving 16 rows of a 856-page directory in the
                # store with nothing recording the crawl is 1% done. So a
                # snapshot whose source has a progress file is skipped too --
                # the fetcher refuses to publish a partial crawl, and this is
                # the same refusal, applied where the file landed.
                partial = {s for s in claimed
                           if (ROOT / "scripts" / "webfetch_snapshots" /
                               ("%s.progress.json" % s)).exists()}
                if partial:
                    if not quiet:
                        print(f"Skipping {Path(path).name}: crawl unfinished "
                              f"for {', '.join(sorted(partial))}")
                    continue
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
    return [_normalize_raw(r) for r in raw if isinstance(r, dict)]


def _self_test():
    """The name and venue normalisation the merge decides on.

    These had no suite of their own, which is how `dedupe.py` came to hold two
    separate readers of the same idea and a threshold that quietly excluded a
    class it was meant to catch. Run by `checks.py`.
    """
    import sys

    from checks import check as _check

    failures = []

    def check(label, actual, expected):
        return _check(label, actual, expected, failures)

    for label, actual, expected in [
        # Two sources styling the same programme differently.
        ("accented and unaccented are one programme",
         name_similarity("Chatty Café", "Chatty Cafe"), 1.0),
        ("a trademark mark is not part of the identity",
         name_similarity("Zumba® Gold", "Zumba Gold (Mondays)"), 1.0),
        # Two sessions of one class on different days stay distinct, because the
        # merge also requires the same day -- the name is allowed to collapse.
        ("a weekday qualifier does not merge two different days",
         name_head("Zumba Gold (Mondays)") == name_head("Zumba Gold (Fridays)"),
         True),
        ("a subtitle is not part of the identity",
         name_head("Chatty Cafe - Connect over a Cuppa"), "chatty cafe"),
        ("a weekday qualifier is not part of the identity",
         name_head("Zumba Gold (Mondays)"), "zumba gold"),
        ("a two-day qualifier is stripped whole",
         name_head("Line Dancing (Tuesdays & Thursdays)"), "line dancing"),
        # ...but a bracketed word that IS the programme must survive, and so must
        # a bracketed qualifier that is not at the end.
        ("a bracketed name that is the programme survives",
         name_head("Yoga (Gentle)"), "yoga (gentle)"),
        ("a bracketed qualifier mid-name is not stripped",
         name_head("Reading Group (Mornings) at Noon"),
         "reading group (mornings) at noon"),
        # The venue head is what decides whether two sources mean the same place.
        ("a venue and its full address are one venue",
         venue_head("Cheltenham Hall, 1218 Nepean Highway"),
         venue_head("Cheltenham Hall")),
        ("two different venues are not one venue",
         venue_head("Cheltenham Hall") == venue_head("Cheltenham Community Centre"),
         False),
        # slot_hash must keep the start time, or two sessions of one class in
        # one day collapse into one event.
        ("the dedupe key keeps the start time",
         slot_hash({"name": "EAL", "datetime_iso": "2026-10-07T09:00:00",
                    "location": "Hall"})
         == slot_hash({"name": "EAL", "datetime_iso": "2026-10-07T12:30:00",
                       "location": "Hall"}), False),
        ("the dedupe key ignores fetch-time microseconds",
         slot_hash({"name": "EAL", "datetime_iso": "2026-10-07T09:00:00",
                    "location": "Hall"})
         == slot_hash({"name": "EAL", "datetime_iso": "2026-10-07T09:00:00.123",
                       "location": "Hall"}), True),
        # A series identity must survive a source re-slugging a venue page, which
        # is why it is keyed on the venue and not on the URL.
        ("the series id ignores the source URL",
         series_id_for({"source_id": "s", "name": "N", "location": "V",
                        "source": "https://x.invalid/old-slug"})
         == series_id_for({"source_id": "s", "name": "N", "location": "V",
                           "source": "https://x.invalid/new-slug"}), True),
        ("the series id separates different sources",
         series_id_for({"source_id": "a", "name": "N", "location": "V"})
         != series_id_for({"source_id": "b", "name": "N", "location": "V"}),
         True),
        ("the series id separates different venues",
         series_id_for({"source_id": "s", "name": "N", "location": "V1"})
         != series_id_for({"source_id": "s", "name": "N", "location": "V2"}),
         True),

        # --- the two field-level defects the store repair pass recognises ----
        # 667 rows (all of kingston_hubs, all of bayside_live) carried the
        # event's own title in the description, and 10 carried an address with
        # the suburb repeated. Both are the shape a fetcher bug leaves behind
        # and no gate could see, because both are well-formed strings.
        ("a description that is the title is a restatement",
         _restates_name("PlaySpace", "PlaySpace"), True),
        ("a short class name still counts as a restatement",
         _restates_name("Tai Chi", "Tai Chi"), True),
        ("casing and spacing do not hide a restatement",
         _restates_name(" TAI  CHI ", "Tai Chi"), True),
        ("prose that merely shares a word is not a restatement",
         _restates_name("Tai chi for beginners", "Tai Chi"), False),
        ("real prose is not a restatement",
         _restates_name("Slow, gentle forms for older adults.", "Tai Chi"),
         False),
        # Bayside's own Location field: ten rows published as
        # "84 Reserve Road, Beaumaris, Beaumaris, Victoria 3193".
        ("a repeated suburb segment is a malformed address",
         _malformed_address("84 Reserve Road, Beaumaris, Beaumaris, "
                            "Victoria 3193"), True),
        ("an empty segment is still malformed",
         _malformed_address("14 Willis St,, Hampton, Victoria 3188"), True),
        ("a dangling separator is still malformed",
         _malformed_address(", Hampton, Victoria 3188"), True),
        ("a well-formed address is not malformed",
         _malformed_address("84 Reserve Road, Beaumaris, Victoria 3193"),
         False),
        ("a non-adjacent repeat is not malformed",
         _malformed_address("Hall, Beaumaris, Street, Beaumaris"), False),
        # The price rules must not inherit the address shapes: a cost is never
        # a comma-separated address, which is why the two predicates are
        # separate functions rather than one.
        ("a price is judged by its own rule",
         _malformed("$5, $5"), False),

        # --- one slot stamp, one reader ------------------------------------
        # `slot_hash` and `dedupe_by_source_url` each had their own reader and
        # they disagreed on the malformed fallback: one tested for a "T" in the
        # salvaged string and the other did not, so a stamp that was both
        # unreadable and date-shaped became a garbage key that the caller's own
        # `"T" not in stamp` guard then let through as a real slot.
        ("a dateless row has no slot",
         _slot_stamp(None) + _slot_stamp("") + _slot_stamp("2026-10-02"), ""),
        ("a dateless row does not hash to a dated one",
         slot_hash({"name": "N", "location": "V", "datetime_iso": ""})
         != slot_hash({"name": "N", "location": "V",
                       "datetime_iso": "2026-10-02T09:00:00"}), True),
        ("an unreadable date-shaped stamp still names a slot",
         "T" in _slot_stamp("2026-13-45T99:99:00"), True),
        ("a malformed dateless stamp is not a slot",
         _slot_stamp("not a date at all"), ""),
        ("an aware stamp is converted, not stripped",
         _slot_stamp("2026-10-02T09:00:00Z"), _slot_stamp(
             datetime(2026, 10, 2, 19, 0).isoformat())),

        # --- the archive file's shape, which nothing enforced ---------------
        # This used to assert that `source_label` equalled `source_id`, which
        # no row in the file could satisfy -- the field was retired -- so all 24
        # rows were reported on every run, describing a file that was fine.
        ("a well-formed archive row raises nothing",
         _malformed_archived([{"name": "N", "source": "https://x.invalid/",
                               "source_id": "bayside_archived",
                               "status": "live"}]), []),
        ("an archived source that is not a known one is named",
         len(_malformed_archived([{"name": "N", "source": "https://x.invalid/",
                                    "source_id": "typo_archived",
                                    "status": "live"}])), 1),
        # `status` is the one field that decides whether a series is listed, so
        # a misspelling is a listing that quietly disappeared.
        ("an unknown archived status is named",
         len(_malformed_archived([{"name": "N", "source": "https://x.invalid/",
                                    "source_id": "bayside_archived",
                                    "status": "Live"}])), 1),
        ("a missing archived status is named",
         len(_malformed_archived([{"name": "N", "source": "https://x.invalid/",
                                    "source_id": "bayside_archived"}])), 1),
        ("a row missing required keys names all of them at once",
         len(_malformed_archived([{"source_id": "bayside_archived",
                                    "status": "live"}])), 2),
    ]:
        check(label, actual, expected)

    # What tells one listing restated by later fetches apart from a listing that
    # genuinely runs many times is the source's own label for a cursor. These
    # are both halves of that claim -- reading the label, and not reading a shape
    # that merely looks like one -- because the second group is indistinguishable
    # from the first by name, URL, date and time alone.
    granicus_next = "Next date:\xa0Saturday, 03 October 2026 | 11:00 AM \r\n\tto 04:00 PM"
    for label, actual, expected in [
        ("a cursor is read from the source's own label",
         names_next_occurrence(granicus_next), True),
        # The status leads the same field, and status.py reads that prefix rather
        # than writing it, so the label is not always the first word.
        ("a status in front of the label does not hide it",
         names_next_occurrence("Sold out: " + granicus_next), True),
        # A materialised series states its own date, which is exactly why the 58
        # stored Mahjong rows are not copies of each other.
        ("a stated date is not a cursor",
         names_next_occurrence("4/09/2026 9:30:00 AM"), False),
        ("a cadence is not a cursor",
         names_next_occurrence("Every Wednesday (10 weeks)"), False),
        # The counterexample that keeps this rule from being written as "two
        # dates in the text": a page listing two real date ranges, one row each.
        ("two real date ranges on one page are not a cursor",
         names_next_occurrence("28 September 2026 to 2 October 2026 "
                               "29 September 2026 to 3 October 2026 "
                               "10:00am-11:15am"), False),
        ("a daily programme is not a cursor",
         names_next_occurrence("daily. 8:45am-4:15pm"), False),
    ]:
        check(label, actual, expected)

    # The pass itself, on the shape that actually reached eight rows.
    arts_url = "https://www.kingstonarts.com.au/whats-on/kingston-sounds"

    def arts_row(stamp, text=granicus_next, source_id="kingston_arts",
                 url=arts_url, name="'Kingston Sounds' by Susannah Langley",
                 venue="Kingston Arts Centre"):
        return {"name": name, "datetime_iso": stamp, "datetime_text": text,
                "location": venue, "source": url,
                "source_id": source_id, "sources": [url]}

    live_arts = [arts_row("2026-10-02T19:00:00")]
    check("a listing re-dated by every fetch leaves one row",
          len(drop_superseded_listing_rows(
              [arts_row("2026-10-02T19:00:00"), arts_row("2026-10-01T11:00:00"),
               arts_row("2026-10-02T08:00:00")], live_arts)), 1)
    # Two sessions one page really does publish, both stated by the source, are
    # two events -- the pass may only drop what the source has stopped saying.
    check("two sessions the source still states both survive",
          len(drop_superseded_listing_rows(
              [arts_row("2026-10-02T19:00:00"), arts_row("2026-10-03T11:00:00")],
              [arts_row("2026-10-02T19:00:00"),
               arts_row("2026-10-03T11:00:00")])), 2)
    # The same-day shape, which is the one a closing-date reader cannot see at
    # all: "Next date: Saturday, 03 October 2026 | 11:00 AM to 04:00 PM" states
    # a session's duration, not a run of days. Three exhibitions reached this.
    same_day = "Next date:\xa0%s | 11:00 AM \r\n\tto 04:00 PM"
    duong = "https://www.kingstonarts.com.au/whats-on/hand-me-down-by-andrew-duong"
    check("a same-day listing re-dated by every fetch leaves one row",
          len(drop_superseded_listing_rows(
              [arts_row("2026-10-03T11:00:00", same_day % "Saturday, 03 October 2026", url=duong),
               arts_row("2026-10-02T11:00:00", same_day % "Friday, 02 October 2026", url=duong),
               arts_row("2026-10-01T11:00:00", same_day % "Thursday, 01 October 2026", url=duong)],
              [arts_row("2026-10-03T11:00:00", same_day % "Saturday, 03 October 2026", url=duong)])), 1)
    # A source that did not report this run cannot say which copy is current,
    # so it does not get to take any of them down. Nor does one whose crawl is
    # still in progress: it has reported rows, but has not read the whole
    # listing, so its silence is not a withdrawal.
    check("a source absent from this run keeps its copies",
          len(drop_superseded_listing_rows(
              [arts_row("2026-10-01T11:00:00"), arts_row("2026-10-02T08:00:00")],
              [])), 2)
    check("a mid-crawl source keeps its copies",
          len(drop_superseded_listing_rows(
              [arts_row("2026-10-01T11:00:00"), arts_row("2026-10-02T08:00:00")],
              [{"source_id": "kingston_arts"}],
              crawling={"kingston_arts"})), 2)
    # And the two shapes this pass must not touch, each of which looks exactly
    # like a listing re-dated per run: a materialised series, and a multi-day
    # listing materialised one row per day of dates it lists itself.
    mahjong = [{"name": "Mahjong", "location": "Chelsea Activity Hub",
                "datetime_text": text, "datetime_iso": stamp,
                "source": "https://www.kingston.vic.gov.au",
                "source_id": "kingston_hubs"}
               for stamp, text in (
                   ("2026-09-04T09:30:00", "4/09/2026 9:30:00 AM"),
                   ("2026-09-07T10:00:00", "7/09/2026 10:00:00 AM"),
                   ("2026-09-11T09:30:00", "11/09/2026 9:30:00 AM"),
                   ("2026-09-14T10:00:00", "14/09/2026 10:00:00 AM"))]
    check("a materialised series is left alone",
          len(drop_superseded_listing_rows(mahjong, [])), 4)

    fairies = "https://www.bayside.vic.gov.au/explore-bayside/events/fairies-rippon-lea"
    multi_day = [{"name": "Fairies at Rippon Lea", "location": "192 Hotham St",
                  "datetime_iso": stamp, "source": fairies,
                  "source_id": "bayside_live", "sources": [fairies],
                  "datetime_text": text}
                 for stamp, text in (
                     ("2026-09-30T10:00:00", "30 September 2026 1 October 2026 "
                      "2 October 2026 3 October 2026 4 October 2026 10:00am-11:15am"),
                     ("2026-10-01T10:00:00", "1 October 2026 2 October 2026 "
                      "3 October 2026 4 October 2026 10:00am-11:15am"),
                     ("2026-10-02T10:00:00", "2 October 2026 3 October 2026 "
                      "4 October 2026 10:00am-11:15am"))]
    check("a multi-day listing keeps a row per day",
          len(drop_superseded_listing_rows(multi_day, [])), 3)

    if failures:
        print(f"\ndedupe: {len(failures)} case(s) FAILED")
        sys.exit(1)
    print("\nall dedupe normalisation cases as expected")


def _apply_archived_flags(rows, live_rows):
    """Set or clear `archived` on every row, from what the sources published.

    Recomputed rather than stamped once, because a stamp cannot survive the
    store: the canonical row is the *older* of a stored expansion and the
    dateless fixture it came from, so it is the stored row that gets kept, it
    was written before the flag existed, and the flag set on the fixture this
    run is discarded with it. Deriving it each run is also the only version
    that heals in both directions -- a delisted series is hidden, and a series
    a council starts publishing again reappears -- without a migration.
    """
    # Only rows that are still in the *archive file* carry a status. A stored
    # occurrence has `archived` set but no `status`, and a row that has been
    # materialised is no longer the dateless listing the id was derived from --
    # so the status is looked up by series, not read off the row.
    archived_sources = {r.get("source_id") for r in live_rows
                        if r.get("archived") and r.get("source_id")}
    # A source id in the archive that something else is *also* publishing means
    # the programme is listed again; the live row must win, so the archive is
    # not treated as authoritative for it.
    live_sources = {r.get("source_id") for r in live_rows
                    if r.get("source_id") and not r.get("archived")}
    hidden = archived_sources - live_sources
    # Per-series liveness, from the fixture's own `status`. This is what a
    # per-source flag could not express: a source is not live or delisted, its
    # series are individually one or the other. Ten of Frankston's were still
    # running when the whole source was written off as unreachable, and
    # hiding the source hid them too.
    #
    # Only "live" keeps a row on the page. "unverified" is withheld as well,
    # because an unconfirmed series is exactly the one whose dates may have
    # moved -- and publishing a stale date for a farmers' market is worse than
    # publishing nothing. Withheld is reversible: adding a `status` is a
    # one-word edit, and the row keeps its justification meanwhile.
    #
    # Computed from the dateless fixture, because series_id_for() reads the
    # source_url fields a *fetched* row carries and the fixture rows do not
    # have. The store's occurrences are stamped from the row that reached
    # materialise(), so matching the same way is what makes the two agree --
    # a lookup keyed any other way silently misses every occurrence and hides
    # the whole series.
    status_of = {}
    for r in live_rows:
        if r.get("archived") and r.get("status"):
            status_of[series_id_for(r)] = r["status"]
    n_flagged = 0
    for r in rows:
        # Looked up under both the row's own stamped id and one derived from
        # the row as it stands. A stored occurrence is stamped at materialise()
        # time from the dateless listing, while the fixture row is still that
        # listing -- so for every row with a real date the two ids differ, and
        # matching on the stamped id alone finds nothing and silently withholds
        # every series in the file. Both keys, so the lookup works whichever
        # side of materialise() the row is on.
        keys = {series_id_for(r)}
        if r.get("series_id"):
            keys.add(r["series_id"])
        status = next((status_of[k] for k in keys if k in status_of), None)
        if r.get("source_id") in hidden and status is not None:
            wanted = status == "live"
        else:
            # No status on file for this series. Withheld, because an
            # unconfirmed series is the one whose dates may have moved -- and a
            # stale farmers' market is worse than no market. This is the
            # reversible direction: adding `status` to the fixture row is a
            # one-word edit and the series comes back.
            wanted = False if r.get("source_id") in hidden else None
        if wanted is True:
            if not r.get("archived"):
                n_flagged += 1
            r["archived"] = False
            r["live_confirmed"] = True
        elif wanted is False:
            if r.get("archived") is not True:
                n_flagged += 1
            r["archived"] = True
        else:
            if r.get("archived"):
                n_flagged += 1
            r.pop("archived", None)
            r.pop("live_confirmed", None)
    return n_flagged


def main():
    # Already normalised by load_live_inputs. Snapshot what the sources
    # justify *before* the merge mutates them: _merge_sources() fills a kept
    # row's blank location from its twin, which would change the very keys
    # reconcile_store() compares against.
    live = load_live_inputs()
    raw = [dict(r) for r in live]

    try:
        stored_doc = read_json(ROOT / "data" / "events.json", default={})
        existing = stored_doc.get("rows", []) if isinstance(stored_doc, dict) else []
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
    # And again now that inference has run: the duplicated rows are the
    # date_inferred ones, so this pass only has something to match once
    # resolve_dateless has materialised them above.
    merged = dedupe_by_source_url(merged)
    merged = dedupe_exact(merged)
    # A midnight row is a restatement of a timed session, not an extra event.
    merged = drop_untimed_twins(merged)
    # A listing that runs for weeks is re-dated by every fetch of it, and the
    # store keeps a copy per run. Held against the sources' own slots, which is
    # what reconcile_store() cannot do for them: its series test keeps a row
    # whose (source, name, venue) is still published, and these rows disagree on
    # the one field that key ignores.
    merged = drop_superseded_listing_rows(merged, live)
    # Everything the sources still publish has now been merged in, so the
    # store can be held against them: a row none of its own sources justify is
    # a leftover from a listing that was corrected or withdrawn.
    merged, stale_stored = reconcile_store(merged, live, today)
    merged, pruned = prune_old(merged, PRUNE_DAYS, today)
    # Last, so it sees the rows that survived: a series withdrawn and expanded
    # over several runs leaves stored occurrences that no fixture names
    # directly, and they are the ones that would otherwise stay on the page.
    flagged = _apply_archived_flags(merged, live)
    if flagged:
        print(f"Archived (delisted, withheld from the page): {flagged} row(s)")
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
    # `python scripts/dedupe.py` runs the merge; `--test` runs the cases instead,
    # which is what checks.py needs so that checking dedupe does not rewrite
    # data/events.json as a side effect. Same convention as build_site.py.
    if "--test" in sys.argv:
        _self_test()
    else:
        main()
