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
from recurrence import (infer_event, refresh_inferred, resolve_dateless,  # noqa: F401
                        series_id_for)
from webfetch_http import month_number, price_sort

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
    raw = event.get("datetime_iso") or ""
    try:
        stamp = _local(raw).strftime("%Y-%m-%dT%H:%M") if "T" in str(raw) else ""
    except (ValueError, TypeError):
        iso = str(raw).replace("Z", "")
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
        raw_stamp = r.get("datetime_iso") or ""
        try:
            stamp = _local(raw_stamp).strftime("%Y-%m-%dT%H:%M") if "T" in str(raw_stamp) else ""
        except (ValueError, TypeError):
            stamp = str(raw_stamp).replace("Z", "")[:16]
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
        iso = str(src.get("datetime_iso") or "")
        if not iso:
            continue
        key = (normalize_name(src.get("name")),
               (src.get("source") or "").rstrip("/"), iso[:16])
        index.setdefault(key, set()).add(venue_head(src.get("location")))
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
    iso = str(row.get("datetime_iso") or "")[:16]
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


# A listing that runs for a while states its span, and the span is what
# identifies it: "Next date: Friday, 02 October 2026 | 07:00 PM  to Saturday,
# 31 October 2026 | 11:59 PM". The opening date is a *cursor* -- the next
# occurrence -- and it advances as the run does, while the closing date does
# not. Split on the connector so only the far end is read.
_RANGE_SPLIT_RE = re.compile(r"\s+(?:to|-|–|—)\s+")
# A date as these sites write it out: "31 October 2026". Requires a year, so a
# connector followed by a day and a month ("Term 4 (17th October - 5th
# December)") is not read as the end of a run.
_WRITTEN_DATE_RE = re.compile(r"\b(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\b")


def range_end(text):
    """The closing date of a span a listing states, as an ISO day.

    None whenever the text states no span, and whenever what follows the
    connector is not a date: "11:00 AM to 04:00 PM" is one session's duration
    rather than a run of days, and a list of the dates still to come is not a
    span either. Both are load-bearing -- the sources publish each of those
    shapes, and a row whose text merely mentions two dates is not a listing
    seen on two different days.
    """
    parts = _RANGE_SPLIT_RE.split(text or "")
    if len(parts) < 2:
        return None
    m = _WRITTEN_DATE_RE.search(parts[-1])
    if not m:
        return None
    month = month_number(m.group(2))
    if not month:
        return None
    try:
        return date(int(m.group(3)), month, int(m.group(1))).isoformat()
    except ValueError:
        return None


def drop_superseded_range_rows(rows, live_rows, crawling=None):
    """Drop the copies of one listing that later runs replaced.

    A listing that runs for weeks is re-read every day, and its date text leads
    with the *next* occurrence rather than the start of the run. So each fetch
    yields the same listing at a different start time, every dedupe key in this
    file carries the start time, and the store accumulated a copy per run:
    `'Kingston Sounds' by Susannah Langley` reached eight rows, one per run,
    each restating one exhibition. `reconcile_store()` could not see it, because
    its series test keeps any row whose (source, name, venue) is still
    published -- which is the right rule for a materialised series and the wrong
    one here, where the rows carry real source-stated times that disagree.

    What separates the two is the closing date. A listing re-read on another day
    still states the same run, so its rows share one closing date; the rows of a
    materialised series each state *their own* date and no span at all
    (`'4/09/2026 9:30:00 AM'`), which is why this leaves the 58 stored Mahjong
    occurrences and the 35 it publishes alone.

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

    groups = {}
    ends = []
    for r in rows:
        end = range_end(r.get("datetime_text"))
        ends.append(end)
        if end:
            groups.setdefault((normalize_name(r.get("name")),
                               (r.get("source") or "").rstrip("/"), end),
                              []).append(r)
    # One row per key is the ordinary case and cannot be superseded.
    crowded = {key for key, grp in groups.items() if len(grp) > 1}

    kept, dropped = [], []
    for i, r in enumerate(rows):
        if ends[i]:
            key = (normalize_name(r.get("name")),
                   (r.get("source") or "").rstrip("/"), ends[i])
            judged = (key in crowded and r.get("source_id") not in crawling
                      and r.get("source_id") in live_labels)
            if judged and not _slot_is_stated(r, by_slot):
                dropped.append(r)
                continue
        kept.append(r)
    if dropped:
        print(f"  Dropped {len(dropped)} row(s) restating one listing a later "
              f"fetch re-dated (same run, a moved next-occurrence date)")
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
        iso = str(src.get("datetime_iso") or "")
        if not iso:
            continue
        key = (normalize_name(src.get("name")),
               (src.get("source") or "").rstrip("/"), iso[:16])
        live_by_slot.setdefault(key, []).append(src)
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
        iso = str(r.get("datetime_iso") or "")
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
                if stored_desc and fresh_desc and len(fresh_desc) < len(stored_desc) \
                        and _page_furniture(stored_desc) and not _page_furniture(fresh_desc):
                    r["description"] = fresh_desc
                    repaired += 1
                break

    if repaired and report:
        print(f"  Repaired {repaired} stored field(s) the source has since "
              f"corrected (malformed address, re-derived from the live row)")
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


_ARCHIVED_REQUIRED = ("name", "source", "source_id")

# Source ids that exist only in scripts/archived_events.json, which is not in
# sources.yaml and so is not in the loaded config. Named here because a
# snapshot's rows are checked against the set of ids any input may claim, and
# the archive's own ids would otherwise read as unconfigured.
ARCHIVED_SOURCE_IDS = {"bayside_archived", "frankston_archived", "ccc_archived"}


def _malformed_archived(archived):
    """Problems with `archived_events.json`, as readable lines.

    This file bypasses every other source mechanism: it is not in
    sources.yaml, so `validate_config()` never sees it, it cannot be run with
    `--source`, and no floor or snapshot rule covers it. Nothing enforced its
    shape either -- all 24 rows happen to carry the same eight keys, and a row
    missing `address` or with a mistyped `source_id` would fail much later and
    name a file nobody suspects, in a message about a missing address or a
    missing badge rather than about a typo in the archive.
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
        if row.get("source_id") != row.get("source_label"):
            problems.append(
                f"row {i} ({row.get('name')!r}) source_label "
                f"{row.get('source_label')!r} != source_id "
                f"{row.get('source_id')!r}")
    return problems


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
    try:
        import yaml as _yaml
        _cfg = _yaml.safe_load(
            (ROOT / "scripts" / "sources.yaml").read_text(encoding="utf-8"))
        for _e in (_cfg.get("webfetch") or []) + (_cfg.get("sources") or []):
            if _e.get("id"):
                known_source_ids.add(_e["id"])
    except (OSError, ValueError, AttributeError):
        # Without the config the check is toothless rather than wrong: every
        # snapshot is skipped and the run reports nothing, which fails loudly
        # in health_check rather than publishing a fraction.
        if not quiet:
            print("WARNING: sources.yaml unreadable; snapshot source ids "
                  "cannot be verified")
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
    return [_normalize_raw(r, quiet=quiet) for r in raw if isinstance(r, dict)]


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
    ]:
        check(label, actual, expected)

    # What identifies a listing that runs for weeks is its *closing* date: the
    # opening one is a cursor that advances on every fetch, so the store ends up
    # holding the same run once per run. These are the two halves of that
    # claim -- reading the closing date, and refusing to read one where there
    # is no span -- because a reader that over-matches would collapse the 58
    # stored Mahjong occurrences into one.
    granicus_range = ("Next date:\xa0Friday, 02 October 2026 | 07:00 PM "
                      "\r\n\tto Saturday, 31 October 2026 | 11:59 PM")
    for label, actual, expected in [
        ("a run's closing date is read past the connector",
         range_end(granicus_range), "2026-10-31"),
        ("a dash separates a run as well as the word to",
         range_end("Next date:\xa0Wednesday, 30 September 2026 | 09:00 PM "
                   "\r\n\t - Friday, 2 October 2026"), "2026-10-02"),
        # A session's own duration is not a run of days.
        ("an end time is not a closing date",
         range_end("Next date:\xa0Saturday, 03 October 2026 | 11:00 AM "
                   "\r\n\tto 04:00 PM"), None),
        ("a month with no day is not a closing date",
         range_end("Term 4 (17th October - 5th December)"), None),
        # A materialised series states its own date and no span, which is
        # exactly why 58 stored Mahjong rows are not duplicates of each other.
        ("a single stated date is not a run",
         range_end("4/09/2026 9:30:00 AM"), None),
        ("a list of the dates still to come is not a run",
         range_end("30 September 2026 1 October 2026 2 October 2026 "
                   "10:00am-11:15am"), None),
    ]:
        check(label, actual, expected)

    # The pass itself, on the shape that actually reached eight rows.
    arts_url = "https://www.kingstonarts.com.au/whats-on/kingston-sounds"

    def arts_row(stamp, text=granicus_range, source_id="kingston_arts",
                  url=arts_url, name="'Kingston Sounds' by Susannah Langley"):
        return {"name": name, "datetime_iso": stamp, "datetime_text": text,
                "location": "Kingston Arts Centre", "source": url,
                "source_id": source_id, "sources": [url]}

    live_arts = [arts_row("2026-10-02T19:00:00")]
    check("a run re-dated by every fetch leaves one row",
          len(drop_superseded_range_rows(
              [arts_row("2026-10-02T19:00:00"), arts_row("2026-10-01T11:00:00"),
               arts_row("2026-10-02T08:00:00")], live_arts)), 1)
    # Two sessions one page really does publish, both stated by the source, are
    # two events -- the pass may only drop what the source has stopped saying.
    check("two sessions the source still states both survive",
          len(drop_superseded_range_rows(
              [arts_row("2026-10-02T19:00:00"), arts_row("2026-10-03T11:00:00")],
              [arts_row("2026-10-02T19:00:00"),
               arts_row("2026-10-03T11:00:00")])), 2)
    # A source that did not report this run cannot say which copy is current,
    # so it does not get to take any of them down. Nor does one whose crawl is
    # still in progress: it has reported rows, but has not read the whole
    # listing, so its silence is not a withdrawal.
    check("a source absent from this run keeps its copies",
          len(drop_superseded_range_rows(
              [arts_row("2026-10-01T11:00:00"), arts_row("2026-10-02T08:00:00")],
              [])), 2)
    check("a mid-crawl source keeps its copies",
          len(drop_superseded_range_rows(
              [arts_row("2026-10-01T11:00:00"), arts_row("2026-10-02T08:00:00")],
              [{"source_id": "kingston_arts"}],
              crawling={"kingston_arts"})), 2)
    # And the materialised series this pass must not touch, at the size that
    # would show it had regressed into them. Each row states its own date and
    # no span, which is the whole reason the closing date identifies a run.
    mahjong = [{"name": "Mahjong", "location": "Chelsea Activity Hub",
                "datetime_text": text,
                "datetime_iso": stamp,
                "source": "https://www.kingston.vic.gov.au",
                "source_id": "kingston_hubs"}
               for stamp, text in (
                   ("2026-09-04T09:30:00", "4/09/2026 9:30:00 AM"),
                   ("2026-09-07T10:00:00", "7/09/2026 10:00:00 AM"),
                   ("2026-09-11T09:30:00", "11/09/2026 9:30:00 AM"),
                   ("2026-09-14T10:00:00", "14/09/2026 10:00:00 AM"))]
    check("a materialised series is left alone",
          len(drop_superseded_range_rows(mahjong, [])), 4)

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
    # After inference: the duplicated rows are date_inferred, so this pass only
    # has something to match once resolve_dateless has materialised them.
    merged = dedupe_by_source_url(merged)
    merged = dedupe_exact(merged)
    # A midnight row is a restatement of a timed session, not an extra event.
    merged = drop_untimed_twins(merged)
    # A listing that runs for weeks is re-dated by every fetch of it, and the
    # store keeps a copy per run. Held against the sources' own slots, which is
    # what reconcile_store() cannot do for them: its series test keeps a row
    # whose (source, name, venue) is still published, and these rows disagree on
    # the one field that key ignores.
    merged = drop_superseded_range_rows(merged, live)
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
