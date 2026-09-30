# Community Events Index

A searchable, sortable offline index of community events across Kingston,
Bayside, Greater Dandenong (Springvale/Keysborough), Frankston and
neighbourhood-house venues near Chelsea/Cheltenham/Mentone/Mordialloc.

## Sources

| Source | Label | Method |
|--------|-------|--------|
| Kingston Hubs (OpenCities calendar API) | `kingston_hubs` | JSON API (`fetch_events.py`), venue per calendar id in `sources.yaml` |
| Kingston Council upcoming events | `kingston_council` | HTML, page 1 (Granicus needs browser TLS) |
| Kingston Arts | `kingston_arts` | HTML, page 1 (same platform) |
| Kingston Seniors Festival (annual PDF guide) | `kingston_seniors` | PDF parse + hand-checked overrides |
| Kingston Libraries | via Council/Hubs listings | — |
| Bayside Council events | `bayside_live` | HTML, full `?page=` pagination |
| Bayside Seniors Festival | `bayside_seniors` | HTML, festival page pagination |
| Greater Dandenong (Springvale/Keysborough filter) | `greater_dandenong` | HTML + per-event detail pages (`fetch_events.py`) |
| Greater Dandenong Libraries | `gd_libraries` | HTML (`fetch_events.py`) |
| Chatty Cafe venue directory | `chatty_cafe` | Venue pages (`fetch_events.py`) |
| Cheltenham Community Centre term classes | `ccc` | HTML + Humanitix term ranges |
| Frankston / Bayside archived | `frankston_archived`, `bayside_archived` | Static snapshots (live pages WAF-blocked) |

Plain `urllib`/`requests` gets HTTP 403 from the Granicus WAF, so the
`kingston_*`, Bayside and CCC sources are fetched with `curl-cffi` Chrome
TLS impersonation in `scripts/webfetch_*.py` (one module per source).
Deeper Granicus pages (JS-postback pager) are covered by manual
`*_manual.json` snapshots if needed.

Greater Dandenong is the odd one out: its *listing* page answers plain
`urllib`, but every event **detail** page returns 403 to it. The suburb the
catchment is built on is only on that detail page, so `fetch_greater_dandenong`
runs entirely on a `curl-cffi` session. Fetching it per event costs about 50
requests and ~40s; see below for why it has to.

## The Greater Dandenong catchment

`sources.yaml` configures `suburb_filter: [Springvale, Keysborough]`, and for
a long time that filter did nothing at all. The listing card carries only a
title, a date and a category — no venue, no address — so
`_passes_suburb_filter()` classified every row as "names no recognisable
suburb" and kept it. All 348 rows were admitted, 33 of them explicitly at
Dandenong or Noble Park, and all published at the placeholder location
`"Greater Dandenong"`.

The event detail page has what the card lacks, in named fields:

```html
<div class="field__label">Location</div>
<div class="field__item">
  <div class="field--name-field-title-address">Noble Park Community Centre</div>
  <div class="field--name-field-address-text">44 Memorial Drive, Noble Park</div>
</div>
```

So the fetcher now opens each event, reads those two fields, derives the
suburb from the address, and filters on it. In the run that prompted this
change, the council listed 51 events and 15 were in the catchment; the other 36
were Dandenong (17), Noble Park (16) and Heatherton (2), and were dropped. (A
point-in-time observation, not a standing total — read the live figure from
`counts` in `data/events.json`.) `suburb_filter` is the single lever
if you want them back — add `"Dandenong"` and `"Noble Park"` to it and the
count roughly triples. Online events (`location: "Online"`) have no suburb and
are always kept.

Two further things fell out of the same fetcher:

- **`?page=N` is an offset, not a page number.** The listing has a JS "Load
  More" control, so page 1 returns everything page 0 returned *plus* a few
  more. Walking it without a seen-set re-parsed every earlier card on every
  page: 348 card reads to find 45 distinct events, with the duplicates left
  for `dedupe.py` to unpick. It now keys on the event URL and stops when a
  page yields nothing new.
- **`max_pages` was being ignored.** `fetch_greater_dandenong` clamped it to
  `min(cfg["max_pages"], 8)` regardless of the configured value. The clamp
  is gone; the "no new events" exit is what bounds the crawl.

The events also now publish their **own stated date** rather than a projected
12-occurrence series. The card's date field ("Thursday 1 October, 10:00am")
parses cleanly, and previously that date was being thrown away: a fetch-time
stamp marked the row `has_real_date: false`, so it was discarded and re-derived
from the description instead. With the stamp gone the real date survives, which
matches the rule the rest of the pipeline already follows — a source-supplied
date always wins. The cost is that a weekly council class now shows its next
occurrence rather than twelve.

`gd_libraries` is the same CMS, so it gets the same treatment: 8 of its 20
events now carry a real venue. Its published count fell from 36 to 3, which is
correct — five of its events are the same events `greater_dandenong` lists,
and `dedupe_by_source_url()` now merges them cleanly instead of leaving two
projected series that only partly overlapped.

## Chatty Cafe: a live schedule only wins if it parses

`sources.yaml` carries a fallback schedule per venue, and the venue page is
fetched anyway so a changed schedule on the site is visible. The live value is
preferred — but only after `_chatty_schedule_is_usable()` asks
`recurrence.build_spec()` whether the extracted text actually produces dated
occurrences.

That check is not defensive padding. The extractor used to return a
reassembly of two groups, and the time group could not consume `.30am` or a
bare `am`, so it stopped at the hour digits: `Tuesday 10.00am - 11.30am` came
back as `Tuesday 10`. A bare weekday with no time is exactly what the date
parser rejects, and because the *live* value replaced the configured one, the
correct fallback could no longer be reached. Six of the twenty venues were
dropped from the calendar entirely — Chelsea Activity Hub, both Matt's Place
venues, Bentleigh Library, Brighton Library and St Aidan's Parkdale — with
`chatty_cafe` still well above its floor of 10 rows, so nothing turned red.

The extractor now returns the whole matched phrase, which also preserves a
cadence stated inside the gap (`Friday (fortnightly) 10.30am-11.30am`), and it
will not read a day-of-month as an hour: the gap between the weekday and the
time cannot cross a digit, so `Monday 20th April at 10.30am` yields nothing and
the configured schedule stands. `2nd Tuesday of the month at 11.00am` is
likewise excluded, because matching it as weekday+time would turn a monthly
session into every Tuesday.

Each venue's slug must be **its own** page. `eau-verte-cafe` was configured
with Eclair Boulangerie's name and Hampton address; that slug is a different
venue, in Lake Wendouree. Every published row therefore carried a Ballarat
link, and the live schedule was read from the wrong page.

## Cheltenham Community Centre terms publish every session

A Humanitix term link carries one JSON-LD block whose `startDate` is the first
session and whose `endDate` is the end of the last. Reading `startDate` alone
published an 11-week class as a single row, so the ten other sessions a member
could attend were simply absent — while the same class *without* a booking link
expanded to 12. `enrich_humanitix()` now walks the range weekly (same
weekday, capped at 12 like every other expansion) and returns the extra rows
for the caller to append.

`startDate` is sometimes given with no time component (`T00:00:00`), which
would publish a 9:30am class at midnight — and midnight is this pipeline's
marker for "date known, time not stated", so the page would render it as
*all day*. The time is taken from the listing's own stated schedule instead.

## Every event has an address, unless it is online

A published row has to say where to go. The address is what the map link, the
`.ics` `LOCATION` and the CSV export are built from, so a missing one ships an
event a reader cannot locate.

That check is worth spelling out because the failure it was written for was not
a missing address but a **wrong** one, which nothing downstream can detect.
`fetch_kingston_hubs()` used to fall back to a generic
`("Kingston Hubs", "Chelsea 3196")` for any `CalendarId` it had no mapping for.
`sources.yaml` listed two calendars but never got a `calendar_venues` block, so
every item from the *Patterson Lakes* calendar — **300 rows, 23% of the
calendar** — published as venue "Kingston Hubs" with a Chelsea address, and
`extract_suburb()` helpfully derived `suburb: "Chelsea"` from it. The one
warning that noticed this printed once per run and the build stayed green.

So the API supplies neither venue nor address, and `calendar_venues` in
`sources.yaml` is the only place they come from:

```yaml
calendar_venues:
  "a1bc2435-...": {name: "Chelsea Activity Hub",              address: "3-5 Showers Ave, Chelsea 3196"}
  "74480036-...": {name: "Patterson Lakes Community Centre",  address: "54-70 Thompson Rd, Patterson Lakes 3197"}
```

A calendar in `calendars` with no entry there — or an entry missing either
field — is now a **hard fetch failure**, and a misconfigured source exits before
`raw_events.json` is written, leaving the previous good file intact. The
alternative, publishing a row with an empty address, only moves the problem
downstream: the run is green and the bad row is on the page.

`reconcile_store()` then removes the phantom rows left behind by the old
fallback, because the venue it justifies them by is no longer the venue the
source names.

**The other exemption is only for online events.** `scripts/venues.py` holds
that one rule, and both the fetcher and the verifier read it from there, because
if they disagreed the fetcher would keep publishing rows the check then
rejects. Blank is deliberately *not* online: a missing venue is the defect, so
treating it as an exemption would hide exactly what the check is for.

Two listings needed that judgement rather than a lookup:

- **`biodiversity-month`** (Kingston Council) is a month-long campaign page whose
  five constituent events are at five different reserves and clubs, and the page
  carries no Location block at all. There is no address to publish, so
  `webfetch_granicus.py` drops venue-less listings rather than inventing one.
- Three `frankston_archived` programmes published `location: "Frankston,
  VIC"` / `"Langwarrin, VIC"` with an empty address — a suburb in the venue
  field. The venues came from the listing pages and their own sites: Saint
  Pauls Community Centre (confirmed on Frankston City Council's own event
  page), Frankston Brewhouse, and McClelland Sculpture Park and Gallery.

Filling a *blank* field is not the same as correcting a wrong one, and only the
former was implemented. `_merge_sources()` kept the stored row's venue because
it was non-empty, and `reconcile_store()` could not catch it either: it judges a
row against its source through `_venue_compatible()`, where
`venue_head("Frankston, VIC")` is a strict prefix of
`venue_head("Frankston Brewhouse")` and so counts as the same place. The store
published a suburb as a venue indefinitely. The merge now replaces a venue
whose head is a strict prefix of the incoming one — the narrower string is the
one that has to go — and `refresh_source` likewise lets a freshly fetched URL
become the row's primary `source`, so a re-slugged venue stops publishing a
dead link. Either way the superseded value is kept in `sources`, which is also
what `reconcile_store()` checks, so nothing loses its justification.

## Pipeline

```bash
pip install beautifulsoup4==4.15.0 pyyaml==6.0.2 rapidfuzz==3.14.6 curl-cffi==0.16.3 pypdf==6.19.0  # pinned versions
python scripts/fetch_events.py                # Python-safe sources → data/raw_events.json
python scripts/webfetch_sources.py            # browser-impersonating sources → scripts/webfetch_snapshots/*.json
python scripts/dedupe.py                      # merge + dedupe → data/events.json
python scripts/build_site.py                  # types/commercial/status → render index.html (repo root, GitHub Pages)
python scripts/health_check.py                # fail loudly on bad output
python scripts/activity_types.py              # assert the classifier rules
python scripts/status.py                      # assert the sold-out / service rules
python scripts/recurrence.py                  # assert the date-inference rules
python scripts/commercial.py                  # assert the commercial-detection rules
python scripts/webfetch_granicus.py           # assert the Granicus address parsing
python scripts/webfetch_http.py               # assert the shared time/month/row parsing
python scripts/failure_signals.py             # assert config validation + "do not publish"
python scripts/fetcher_equivalence.py         # assert the fetchers extract the same rows
python scripts/render_check.py                # prove index.html renders rows, and measure the grid
```

The order matters in one place: `build_site.py` must run **before**
`health_check.py`, because the health check verifies the status and
default-hidden flags that `build_site.py` writes. Running it the other way
round fails the build, which is the intended outcome.

The eight assertion scripts are pure: no network, no writes, and each exits
non-zero with the actual value and the expected one. The GHA workflow runs
all of them, so a rule change that alters a classification fails the build
before it can reach the published page. `render_check.py` is separate because
it is not pure: it renders `index.html` and measures the result.

`webfetch_http.py` is the single owner of what every source used to repeat:
the row shape (`make_row`), a month name to a number (`month_number`), a
written time to (hour, minute) (`parse_time`, `range_start_time`,
`line_range_starts`), the detail-page fetch loop (`enrich_details`) and progress
reporting (`report`). The street-word list is the exception: it lives in
`venues.py`, with `is_online`, because `build_site.py` needs it too and the
render layer should not have to import the fetch layer. For the same reason
`webfetch_http.py` imports `curl_cffi` inside `make_session()` rather than at
module level, so `fetch_events.py` -- which reaches these hosts with `urllib`
and does no browser impersonation -- can use `month_number()` without pulling in
a network library.

The time conversion had three near-identical copies, and one disagreed with
the other two about a range that states its meridiem once — the guide's own
house style, `10:30-11:30am`. Reading the start's *optional* meridiem group
without falling back to the end's raised `AttributeError`, which the
orchestrator's generic handler turned into a discarded snapshot for the entire
festival. `range_start_time()` is the one reader now, and
`webfetch_http.py`'s self-test pins the cases whose failure mode is a
plausible-looking wrong hour rather than a crash.

`scripts/` layout: `fetch_events.py` (API/Drupal/venue sources),
`webfetch_http.py` (the shared owners: row shape, time/month parsing, the
detail loop, reporting), `webfetch_{bayside,granicus,ccc,seniors}.py` (one
fetcher each), `webfetch_sources.py` (thin orchestrator + config validation),
`dedupe.py`, `recurrence.py`, `commercial.py`, `status.py`,
`activity_types.py`, `venues.py` (what counts as an event held online),
`jsonio.py` (atomic writes), `build_site.py`, `health_check.py`,
`render_check.py`.


### Failure behaviour

Both fetchers **exit non-zero** when a source does not fetch cleanly, so a WAF
block or a markup change turns the Actions run red instead of quietly
publishing a smaller calendar.

The contract is one signal, and it is worth stating exactly:

- **`raise PartialFetch` is the only "do not publish" signal.** It means the
  fetch was cut short, blocked, or misconfigured, and the existing snapshot
  survives. A fetcher that returns `[]` means the source genuinely has nothing
  — so `[]` now has one meaning, where it previously had four (WAF block, no
  events, no `year:` configured, PDF download failed), all reported to the
  operator as "returned 0 rows";
- a source that raises, or returns 0 rows, is a hard failure;
- a source that returns 0 rows never overwrites its snapshot — a festival out
  of season must not erase the season it already published;
- a source that *raises* on a config fault (a Kingston Hubs calendar with no
  venue/address) exits before writing anything, so the previous
  `raw_events.json` survives rather than being replaced by a partial run;
- a multi-page crawl that stops early raises `PartialFetch`, and the existing
  snapshot is kept — a partial crawl is indistinguishable from a source with
  genuinely no events, so it must not replace good data.

**A detail-page block is now protected too.** It used to be a bare
`continue` in all three sources, which meant a WAF block on the *event* pages
was indistinguishable from a source with no event pages: the snapshot was
overwritten with a listing-only file, every venue blank, and the run stayed
green. `enrich_details()` raises `PartialFetch` when fewer than half its
attempts load.

**Config faults are found before anything is fetched.** `validate_config()`
runs over every entry up front — required keys per `type:`, a snapshot name
that ends in `.json`, no two sources claiming one snapshot, no duplicate id —
so a typo is one message naming the entry, reported once, instead of a
`KeyError('url')` from three frames inside a fetcher after the earlier sources
have already been crawled. Run it on its own with
`python scripts/webfetch_sources.py --check-config`.

**A successful but truncated fetch is still visible.** None of the above
covers a fetch that *succeeds* and returns a fraction of the real data — a
starved `--detail-cap`, a markup change that drops one section, a site that
quietly stops listing half its events. `PartialFetch` does not fire, and
`reconcile_store()` will believe the new rows: it drops whatever no source
justifies, and the source that stopped justifying them is this one. So every
write prints the previous size beside the new one, and a shrink is called out
in as many words. It is a warning rather than a refusal, because a term
genuinely ending does shrink a source, and blocking that would be worse than
reporting it.


## Deduplication (`dedupe.py`)

1. **Exact hash** on (normalized name, *start time*, normalized location).
   The start time is part of the key so two sessions of one class at a venue
   in a day (`Cert II in EAL` 9am and 12:30pm) both survive.
2. **Same name + location** across sources merges source lists, keeping the
   dated variant — except when the two rows start at different times of day
   (separate occurrences are never collapsed).
3. **Fuzzy**: name similarity ≥ 0.75 AND same day within ±30 min AND strict
   location equality.
4. **Prune** events older than 90 days. Timestamps are converted to
   Australia/Melbourne rather than having their UTC offset stripped, so events
   near the 90-day boundary are not mis-pruned.
5. **Date inference** (`recurrence.py`), then a final **exact-only** collapse
   keyed on name + *start time* + location.
6. **Same session, two sources** (`dedupe_by_source_url`): two independent
   problems produced the same visible duplicate.

   *Same listing page, different venue string* — `kingston_arts` +
   `kingston_council` both hit kingstonarts.com.au, and `greater_dandenong` +
   `gd_libraries` both hit the GD libraries site. `location_matches()` is
   strict equality, so `Kingston Arts Centre` and
   `Kingston Arts Centre, 979 Nepean Hwy` never matched.

   *Same programme, different title* — sources style one event differently.
   The national Chatty Cafe directory writes
   `Chatty Cafe - Cheltenham Community Centre`, the venue's own site writes
   `Chatty Cafe`, and a seniors listing writes
   `Chatty Cafe - Connect over a Cuppa`. Whole-string similarity lands around
   0.5 for these, well under the 0.75 fuzzy threshold.

   The programme key holds a **list** of candidates, not one row. It used to be
   a single slot claimed by `setdefault` and never released, so the first row to
   arrive owned `(programme, time)` for the rest of the run — and since a merge
   also requires a compatible venue, a row that *couldn't* merge (a session at a
   different hall, or a stale store row naming a venue the source has since
   corrected) left the key pointing at itself, and every later row was compared
   against that one instead of against each other. Two genuine twins could both
   survive that way: `Tai Chi` at Patterson Lakes from `kingston_hubs` and from
   `kingston_council`, held apart by a stale `Kingston Hubs` row that
   `reconcile_store()` then dropped, leaving the duplicate in a green-looking
   store. Comparing against every compatible candidate fixes it without
   loosening the venue check.

   What all of these share is the **base name** — the title up to its first
   ` - `, `:` or `|`, accent-folded so `Café` matches `Cafe`. A merge requires
   that base name plus the same date, start time and a compatible venue. The
   venue check is what keeps genuinely distinct events apart: two
   STEADYstrength classes at two different halls, or `Chatty Cafe - Game On!`
   at 10:00 against the 11:00 series at the same venue.

7. **Untimed twins** (`drop_untimed_twins`): a listing that states only
   "Wednesday" yields an occurrence at 00:00. When a timed occurrence of the
   same programme at the same venue exists, the midnight row restates that
   session rather than adding an event — and it publishes a 12am start that
   does not exist. Matched across the whole run, not per date, because the
   timed and untimed listings often cover different spans.

Step 5's fuzzy pass is deliberately not re-run after inference: sibling
courses at one venue (`Cert I in EAL` vs `Cert III in EAL`, both Monday 9am)
score above 0.75 and would fuse into a single event.

Step 6 runs **after** inference, because the duplicated rows are the
`date_inferred` ones. Neither a shared URL nor a matching description is
required: the CCC page lists two different STEADYstrength classes, both
Tuesdays 10:00, at two different halls — same name, same time, same URL,
genuinely different events — so the *venue* is what separates them.
`health_check.py` re-checks for survivors of both step 6 and step 7.

A row carrying a date but flagged `has_real_date: false` is self-contradictory
— older runs stamped such rows with the fetch time, so the published date
silently became the day the pipeline ran. The stamp is discarded and the row
is re-derived from its text.

### The store is held against its sources (`reconcile_store`)

`data/events.json` is a cache, and a cache is only correct if every entry can
still be re-derived from the source it came from. The merge is append-only —
nothing deleted a row — so two things survived as phantoms that no
duplicate check can see, because the stale row differs from the row that
replaced it in *exactly* the field being compared:

- **A listing's time is corrected upstream.** The Granicus listing card gives
  a date with no time and the detail page gives the real one, so the same URL
  is fetched twice with different times. Both landed in the store; the earlier
  one sat beside the correction for the full 90-day prune window.
- **A listing is withdrawn, or the page is edited.** A store expansion of
  twelve inferred rows outlives the listing it came from. `STEADYstrength`
  was the live case: the Cheltenham Community Centre page listed it twice,
  and when the second copy (at a hall the page no longer mentions) went away,
  the store kept all twelve of its rows — including six on the wrong weekday,
  because that copy had lost the word "Thursdays" and the second session
  inherited the first one's weekday.

`reconcile_store()` runs after everything has been merged in, and drops any
row that **none of its own recorded sources** still justify. Two boundaries
keep it from deleting good data:

- a row is kept if *any* of its `sources` still vouches for it, so a
  cross-source merge is not undone by one of its parents moving on;
- a row whose `source_label` is absent from this run's inputs entirely is
  left alone. A seasonal festival out of season, or a fetcher that failed,
  must not take its existing rows down with it — the 90-day prune still
  bounds those.

`health_check.py` re-runs the same function over the published store and fails
if anything would be dropped, so a stale row cannot survive a green build.

#### A field the source has *corrected* is re-derived too

Deleting phantoms is only half of "a cache is re-derivable". `_merge_sources()`
deliberately only fills a **blank** field — whatever the store already has
wins — which is right when the store's value is merely plainer, and wrong when
the fetcher that produced it was buggy. Two such defects persisted through
repeated re-crawls of already-corrected sources:

- 86 rows carrying `"14 Willis St,, Hampton, Victoria 3188"` — a `", "` join
  applied to venue segments that already ended in commas;
- 96 rows carrying `"$12 per session FIND OUT MORE BUTTON Find Out More"` — a
  Weebly section span that ran into the next block's call to action.

So a row that its own sources still justify is checked for a *malformed*
stored field, and re-derived from the live row when there is one. The
replacement is deliberately narrow, because the alternative is trading one
wrong value for another:

- an address is replaced only when the stored one is visibly broken (an empty
  `,,` segment, a dangling leading or trailing comma) **and** the live one is
  not. A terse but well-formed address is left alone — the store's value may
  have come from a second source that knew better.
- a price is replaced only when the live one is **shorter**, which for a cost
  field means the page furniture has been cut. A long-but-clean price, like
  `"$120 for 10 weeks class pass | $15 casual"`, is never truncated.

A dateless listing expands into a dozen store rows, so the repair falls back
from an exact timestamp match to `(name, url)` for those, which is the same
key `_justification_keys()` uses.

### Inferred rows are refreshed, never frozen

Inferred dates are written back into the store, so a row dated by an older,
buggier build keeps its old time indefinitely — nothing re-derives it.
`refresh_inferred()` runs before `resolve_dateless()` and re-derives any
`date_inferred` row whose stored time disagrees with the time its own text
states **for that row's weekday**. It acts only when the weekday states
exactly one time: `Cert III in EAL` runs twice on Mondays (09:00 and 12:30),
so either stored value is legitimate and both are left alone.

The replacement is matched on the **date** as well as the time, and the row is
replaced in place rather than re-expanded. Two things follow from that, both
of which were defects:

- Re-expanding per row *multiplied* them. Three stored midnight copies of one
  weekly series became 36 rows, because each row independently re-expanded to
  the whole series and `dedupe_exact()` had to collapse the result afterwards.
  A refresh corrects a row; it does not add occurrences.
- A time-only check authorised a **date change**. The re-expansion dropped the
  original row and kept its replacement, so a stored occurrence could be moved
  to a day its own text never produced, and nothing noticed: a stored Friday
  for `"Fifth Friday of every month"` was deleted and seven other Fridays
  published. The stored date must appear in the re-expansion for the
  substitution to be accepted.

This is what clears two classes of stale row:

- **midnight copies** of time-stated series — every Chatty Cafe venue whose
  schedule says "10.30am" was also published at 00:00, because the weekday
  was recognised but a lone time was not paired with it;
- **mis-parsed times** — `Bingo Bonanza` at 12:00 because "an **afternoon** of
  fun" matched the new `noon` token before the word boundary was added.

`refresh_inferred()` only ever corrects a row's **time**. A row whose *date*
was derived wrongly is not touched by it, which is why `reconcile_store()`
exists as well: re-deriving from the source also re-derives the date.

### A second session on one weekday is real, not a parser bug

`weekday_slots()` pairs each weekday with the times that follow it, and a
bare second time inherits the weekday before it. That looks wrong until you
check the corpus, and 5 of the 6 sources that exercise it depend on it:

| Text | Slots | Correct? |
| --- | --- | --- |
| `Mondays. 9am - 12pm. 12:30pm - 3:30pm` | Mon 09:00, Mon 12:30 | yes, two Monday sessions |
| `Wednesdays. 9am - 12pm. 12:30pm - 3:30pm` | Wed 09:00, Wed 12:30 | yes |
| `Tuesdays, Beginner: 10:00am–11:00am Social: 11:00am–12:00pm` | Tue 10:00, Tue 11:00 | yes, two levels of Pickleball |
| `Mondays 10:30am - 11:30am \| Fridays 1pm - 2pm \| Fridays 2pm - 3pm` | Mon 10:30, Fri 13:00, Fri 14:00 | yes, two Friday sessions |

The sixth was the one that looked wrong — `STEADYstrength`, whose hall copy
had genuinely lost the word "Thursdays" — and it turned out to be a stale
snapshot rather than a parser fault. Narrowing the carry-forward would have
broken the four real cases, so it is left alone and the snapshot is fixed by
`reconcile_store()` instead.

### Time formats the parser accepts

`am/pm` (`9am`, `5.30pm`, `11:15am`), 24-hour (`14:00`, `18.30`), `noon` and
`midday` (`12noon`, `noon - 1pm`), and a bare hour as one end of a range
(`12 - 1.30pm`). A bare `12` is *not* a standalone token: schedule text is full
of bare numbers ("Monday 28 September") and matching digits alone turns
day-of-month into an hour. Every token is `\b`-anchored so "afternoon" is never
read as "noon".

### Reproducibility

Both `dedupe.py` and `recurrence.py` anchor every date calculation on "today".
Set `SOURCE_DATE_EPOCH` (a Unix timestamp) to pin it and make a run
reproducible:

```bash
SOURCE_DATE_EPOCH=1789948800 python scripts/dedupe.py
```

## Date inference (`recurrence.py`)

An event needs a real date to be placed on a calendar, so **every published
row carries one** — `health_check.py` fails the build otherwise. Some sources
publish recurring programs without per-occurrence dates and state the pattern
in prose instead ("Wednesdays. 2:00pm – 3:30pm", "on the fourth Saturday of
every month"). Those are parsed into a recurrence spec and expanded into
concrete dated rows.

A weekday and a time with **no date at all** is a pattern — that is how these
sources write a recurring program, and 15 published Chatty Cafe series say it
exactly that way ("Tuesday 11am – 1pm" for a session open every week of term).
A weekday **with** a date is that one session: `Friday 2 October, 11:00am` is
one event, and reading it as weekly would fabricate twelve. A date that has
just passed settles the listing rather than re-expanding it, so a finished
afternoon is not republished as twelve future ones.

The test is the presence of a date, not the plurality of the weekday:
`Tuesdays 9am` and `Tuesday 9am` both mean a weekly class, while `Tuesday
9am` next to a stated date means one session.

Recognised patterns, in priority order:

| Pattern | Example | Expansion |
| --- | --- | --- |
| Nth weekday of month | `1st Wednesday of every month` | 12 months |
| Fortnightly | `Every second Saturday`, `Friday (fortnightly)` | every 2nd week |
| Explicit range | `Term 4 (17th October - 5th December)` | that window only |
| Full date | `Thursday, May 25th, 2023` | single occurrence |
| Weekday(s) + times | `Mondays and Thursdays 9am - 12pm` | 12 occurrences |
| Weekday in the title | `Friday After School STEAM session` | 12 occurrences, all-day |

Details worth knowing:

- **12 occurrences max** per source event, earliest first, so a weekly class
  covers ~3 months and a monthly one ~1 year. A stated `10 weeks` overrides
  the cap, counted in **weeks**: it is multiplied by the sessions per week, so
  a twice-weekly "6 weeks" course is 12 sessions. Where an explicit date range
  is *also* given it wins and the week count is ignored, because it is the
  more specific statement of the same thing — that is what stops a
  "Weeks: 10" term running to 14 December losing its final Monday.
- **A month window belongs to one year.** "from February to November" states
  months, never a year, so on its own the window is satisfied again by the
  same months of the following year. It is bound to the year that contains
  the day the listing is read, which is what stops a Feb–Nov season
  reappearing twelve months later.
- **A fortnightly series has a phase, and it comes from the start date.** A
  fortnight divides the week in two, so anchoring the phase on "today" made
  the published dates shift by a week every time the pipeline ran. With a
  stated start the phase is that start; without one, today remains the anchor
  and the series is still weekly-shaped and reproducible.
- **Explicit ranges resolve to the current year.** A window that has already
  finished is treated as stale and dropped rather than rolled forward, so
  2023 workshop write-ups and last year's terms do not reappear. A finished
  range settles the listing even when it carries no year: "from 1 June to
  31 August" read in September is a closed reading period, not next June.
- **A year-less date does not roll forward on a technicality.** Greater
  Dandenong states dates with no year ("28 Sep Drop-In Casual Basketball
  Monday 28 September, 5:30pm"). A loose date already in the past used to
  roll into the following year, so a listing read the day after it happened
  was published twelve months out. Rolling now requires the current-year
  reading to be more than `YEAR_ROLL_GRACE_DAYS` (14) days past; within that
  window the listing is simply stale and is dropped.
- **A source-supplied date always wins.** A dateless stub whose name and
  location already have a real dated sibling is dropped in favour of it.
  Previously *inferred* siblings do not count, so re-runs stay stable.
- Times are inferred only when stated; everything else becomes an all-day
  entry. `2.30pm` and `2:30pm` both parse, and the `15pm` typo on the CCC
  netball page is read as 3pm.
- Undateable listings are **removed**, with the reason printed by
  `dedupe.py`. This drops 24/7 helplines, open-ended enrolments, one-off
  exhibitions with no dates, and sponsor acknowledgements — none of which can
  go on a calendar.

Note that the `greater_dandenong` source has no parseable date in its own
date field: the day appears only inside the description, so **all** of its
rows are `date_inferred`. A change to the date parser therefore moves that
source wholesale, which is worth knowing before blaming a single row.

Inferred rows are tagged `date_inferred: true` and carry a `recurrence`
label (e.g. `Every Wednesday`, `Fourth Saturday of every month`) so a
reader can tell a stated date from a derived one. The table renders that
label under the timestamp, and carries it into the `.ics` export as
`X-COMMENTS-DERIVED-DATE`, so a derived date does not silently become a
confirmed one once it leaves the page.

### Nothing validates an inferred *date*

A wrong date is the worst defect this pipeline can ship: the row renders, it
looks bookable, and it is simply the wrong day. So two checks cover it, and
the second exists because the first could not.

The obvious one compares each stored timestamp against the time its own text
states. That check compares the store against **the same parser that produced
it**, so any parser bug is self-consistent and passes — every date defect
below reached a green build through it. What is actually needed is a question
the parser was not asked: *is this row's date still one the text produces at
all?* `health_check.py` re-expands each inferred series and fails if a stored
date is absent from the result. That is independent of how the time was
parsed, so it catches a wrong day, a wrong phase and a series that no longer
exists.

The date defects it was written for, all of which had shipped:

| Defect | Published | Correct |
| --- | --- | --- |
| A month window with no year | 12 rows, 10 of them next season | 2 rows, this season |
| `Weeks: 10` counted as 10 sessions | 10 Mondays, last one 7 Dec | 11, ending 14 Dec as stated |
| Fortnightly phase anchored on "today" | different dates each run | same dates, last named session kept |
| A dated single session read as a pattern | 12 Tuesdays from one afternoon | 1 |
| A bare-hour range read end-first | `Tuesdays 6 – 8pm` at 20:00 | 18:00–20:00 |
| `First Tuesday each week` read as monthly | 12 dates 12 months apart | 12 consecutive Tuesdays |
| A booking deadline read as a session | a phantom 4pm class each day | 2 sessions |

`recurrence.py` asserts all of them on a fixed reference date, so the
expected dates are literal rather than relative to whenever the suite runs.

A midnight stamp is the pipeline's marker for "date known, time not stated",
not a 00:00 start, so the table shows those as `all day`. Every inferred row
that lands on `00:00` was checked: none of them state a time in their own
text. A source that states a time in the small hours is normalised to the
marker rather than published as one: the Kingston Hubs API returns
`12:30:00 AM` for *Social Jigsaw Group*, and the page renders an exact `00:00`
as *all day* but `00:30` as a real half-past-midnight start, so the fetcher
floors the hour.

## A cadence stated as a property, not a repetition

`Every second Saturday` is recognised as fortnightly, but a venue can also
state the cadence as a property of the weekday — `Friday (fortnightly)
10.30am-11.30am` (a Chatty Cafe listing) or `Monday 10.30am (every second
week)`. Neither matched, so both fell through to the weekly branch and
published a session on the weeks the venue does not open: 12 weekly rows where
the source says fortnightly. Both forms are recognised now, guarded on a
single named weekday, because the fortnight expansion walks one weekday per
period.

## Commercial events (`commercial.py`)

Pub/meal-deal promos (parma/steak/happy-hour/hotel jobs) and priced-or-pub
trivia are flagged `is_commercial` and hidden by default in the UI
(checkbox to show). Gold-coin community trivia stays visible.

## Sold out, cancelled, and services that are not events (`status.py`)

Two other things stop a listing being worth a reader's time, and neither is
answered by the listing's own fields.

**Can I still go?** Venues write the status into the listing rather than a
field of it. Granicus puts the whole status sentence where the date goes —
`"Sold out: Wednesday, 30 September 2026 | 11:00 AM to 12:00 PM"` — so the
time is parsed out of a sentence that also says the event is unavailable, and
the row is published looking perfectly bookable. Greater Dandenong Libraries
puts it in the title (`FULLY BOOKED - Card Making - Libraries After Dark`).
`event_status()` reads the leading status phrase, then the title, then the
blurb, and records `status` / `status_detail` / `status_label`. Sold out and
fully booked rows stay visible but badged, because a listing you can no longer
book is still worth knowing about; **cancelled** rows are hidden, since a
cancelled event is worse than an absent one.

**Is this an event at all?** A community centre's `Takeaway Meals` — "Take
home delicious, nutritious meals for one. Available Tuesday to Friday,
10am-2pm" — is a service with an opening window, not a session to attend, and
the calendar was giving it four slots a week. `is_ongoing_service()` flags it
as `is_service`. It is deliberately *not* `is_commercial`: the meals are
subsidised, and calling a council service a "commercial pub/meal deal" would
put a false fact in `events.json` and the CSV export.

`build_site.py` writes `hidden_by_default = is_commercial or is_service or
cancelled`, which is the one flag the UI filters on, so all three share the
single existing checkbox. `health_check.py` re-derives both classifications
from each row's own text and fails if the stored flags disagree, so a
`status.py` regression cannot leave a sold-out workshop looking bookable.

Run `python scripts/status.py` to check the 10 known cases. It **asserts**
rather than prints, and it earned its place immediately: the first version of
the service pattern included `\bcommunity\s*meals?\b`, which matched
*"Community Meals Cooking Class"* and would have hidden a real class.

## Activity types (`activity_types.py`)

Rule-based classifier over 17 types. Each event collects **every** matching
tag (multi-tag): children/family is orthogonal to market/musical, so a kids
market is both `Children & Families` and `Market & Exhibition`. The title
(plus any source-supplied category) and the free-text description are matched
as a union, and results are returned in `TYPES` order with `["Other"]` iff
nothing matches (never alongside real tags).

Because tags compose rather than collapsing to one winner, rule order is no
longer load-bearing for correctness. Two orderings that used to matter are
now just history: `Social & Community` used to sit before `Food & Drink` so
the Chatty Cafe program was not filed as dining (now every Chatty session is
both), and the nutrition pre-rule used to keep `Health & Wellbeing` ahead of
the meal words (now "Eat Well, Age Well" is both food and health).

Patterns are anchored with `\b` wherever the unanchored form also matched
inside an unrelated word — `r"organ\b"` matched "Janis **Morg**an" and filed an
art workshop as music, `r"eat "` matched "great", "meat" and "beat",
`r"tablet"` matched "table**top**" (now `\btablets?\b`), and `\barts?\b`
matched "martial **art**" (now `(?<!martial )\barts?\b`, so an aikido
demonstration is sport, not craft).

The UI filter is OR: an event stays visible while **any** of its tags is
ticked, and hides only once every tag it carries is unticked. Checkbox counts
are per tag, so they sum to more than the event total.

Run `python scripts/activity_types.py` to check the 56 known
name/description → tags cases. It **asserts** rather than prints, so a rule
change that alters a classification fails loudly.

## Seniors Festival overrides

The Kingston guide's multi-column spreads lose column association in
flat-text PDF extraction, so `scripts/seniors_festival_overrides.json`
pins hand-checked sessions for those events. Values go stale each October
with the new guide — refresh them then. The `year` is required in
`sources.yaml`; there is no fallback to the current year, which would
reinterpret the whole document.

### Refreshing the seniors festival each October

Two values must move together, and `health_check.py` fails the build if they
disagree:

| File | Key | Meaning |
| --- | --- | --- |
| `scripts/sources.yaml` | `kingston_seniors.year` | the guide being parsed |
| `scripts/seniors_festival_overrides.json` | `year` | the guide the sessions were transcribed from |

The override dates are **literal and never re-stamped**: the festival falls on
different days each year, so a 2026 file cannot be shifted to 2027
mechanically. If the two years drift apart, the next run emits last year's
dates, `prune_old()` deletes them as over 90 days old, and the whole festival
vanishes — which used to be a *warning*, because `kingston_seniors` is
warn-only so that out-of-season decay is tolerated.

`seniors_config_errors()` now separates those two cases. A stale `year` in
`sources.yaml` is a hard error only during the festival months (Sept–Nov);
outside that window a stale year is correct, since last year's festival really
has finished. A mismatch between the two files is always an error, because no
season makes that legitimate.


## GitHub Actions

`.github/workflows/update-events.yml` runs daily at 06:00 UTC with
per-step timeouts: fetch → webfetch → dedupe → build → **health check**
→ commit (`data/events.json`, `index.html`, snapshots) → Pages deploy.

The health check enforces a total floor, per-source floors for year-round
sources, zero exact duplicates, zero same-listing duplicates, zero
same-programme duplicates, no inferred row whose stored time contradicts its
own text, that every row has a real date, that **every row that is not held
online has an address**, that every source label has badge CSS and a friendly
name, and that both the template and the built `index.html` are a single
document with no unfilled placeholders and no calls to undefined functions.
Seasonal sources (seniors festivals) are warn-only since they legitimately decay
out of season.

Five further checks cover the defects described above, each of which was
verified to fail the build when reintroduced:

- **no row the sources do not back** - it re-runs `dedupe.reconcile_store()`
  over the published rows against this run's inputs, so a stale start time or
  a withdrawn listing cannot survive a green build;
- **`build_site.py` actually ran** - every row carries a `hidden_by_default`
  flag, without which the page would quietly show every sold-out workshop and
  drop-in service;
- **status/service flags match their own text** - re-derived, not trusted, so
  a `status.py` regression cannot leave a sold-out row looking bookable;
- **the Greater Dandenong detail fetch still works** - if most of its rows are
  back at the generic `Greater Dandenong` location, the venue is unknown again
  and the catchment is not filtering anything;
- **no Greater Dandenong venue outside the configured catchment** - read back
  from `sources.yaml`, so widening `suburb_filter` is the way to admit more,
  and events held online are exempt (they have no suburb).

And four covering the page rather than the data, each verified the same way —
see *Interaction defects found in review* below for what each was written for:

- **no duplicate `id`, and balanced container tags** - an edit once left a second
  copy of two buttons and a stray `</div>`, which `getElementById` resolved
  around silently while the page rendered both;
- **no `hidden` attribute overridden by a `display` value** - `.filterpanel` set
  `display:flex` at a higher specificity than the UA's `[hidden]`, so the panel
  rendered open while its toggle reported `aria-expanded="false"`;
- **the table's secondary text declares a rem size of at least 0.75rem** -
  `rem` rather than a minimum, because a rem length cannot compound with a
  parent's font-size the way an `em` one does, and a *missing* declaration is
  a failure because it inherits from that chain;
- **`.tcheck` has a `min-height` of at least 24px** - all 49 filters are
  checkboxes, and the `<label>` is what a click lands on.


The Greater Dandenong floor is deliberately low (10, see `MIN_SOURCE` in
`health_check.py`). That source is a narrow, genuinely filtered catchment
publishing one stated date each, so the check's job is to catch the scraper
dying at 0 rows, not a quiet season.

### The page is rendered, not just inspected (`render_check.py`)

Every other check reads Python or `events.json`. None of them execute
JavaScript, so a syntax error in `src/templates/index.html` builds cleanly,
passes the health check, and publishes a **blank calendar**. That is not
hypothetical: a missing closing paren in the source-status block did exactly
this, and a regex scan for undefined names could not see it, because a parse
error is not an undefined name.

`render_check.py` loads the real built page in a headless Chromium browser and
asserts the results table has rows and the count line is populated. On failure
it re-renders with an error handler attached and prints the JavaScript error
with its line number, so the output names the fix rather than just "no rows".

It takes about a second. If no Chromium-family browser is found it prints a
`SKIP` and exits 0, so it stays usable on a machine with no browser; set
`$BROWSER` to force a specific one. The GitHub runner has Chrome
preinstalled, so CI always gets the real check.

## Mobile accessibility is asserted, not hoped for

The page is one hand-written document, so nothing in the pipeline stops a CSS
edit from quietly breaking the phone layout. The mobile card layout below
`768px` was the worst case: it set `display:block` on `table`/`tbody`/`tr`/`td`,
which strips the implicit ARIA roles browsers derive from `display`, and it
labelled each stacked cell with `td::before{content:attr(data-label)}`.
Generated content is absent from the accessibility tree, so a card that *looks*
right read as an unlabelled wall of values. On top of that the field labels
chained `table .9em` → `td .82em` → `.desc .9em`, which put descriptions at
10.6px, addresses and card buttons at 10px, and badges and field labels at
8.9px. Both pages passed every other check.

So the invariants are asserted in two places.

**`health_check.py`** reads the stylesheet and the markup: explicit
`table`/`rowgroup`/`row`/`cell`/`columnheader` roles; a `.visually-hidden`
helper that actually clips; no `::before` content carrying a field name;
`<main>`, `<caption>`, `role="status"`, `aria-controls` and a mobile sort
control present; every `.badge-*` background at 4.5:1 against white (six were
2.85–4.17:1); interactive control borders at 3:1, using `--control-border`
rather than the decorative `--border`; `body` line-height at least 1.5; no
form control below 16px, which is where iOS Safari zooms the viewport on focus
and never zooms back; and a `:focus-visible` ring somewhere, since the only
one was on the sort headers and mobile hides those.

**`render_check.py`** measures the rendered DOM and the actual phone layout,
because neither the template text nor `--dump-dom` can see these: real
`.celllabel` text in every rendered data cell, a live region on the result
count, calendar buttons whose `aria-label` contains their own visible text
(2.5.3), a populated sort control, and then — with a probe injected and the
page re-rendered at phone width — that no table cell falls below 12px, that the
page does not scroll sideways, that the control bar is not `position:sticky`,
and that the sort headers have left the tab order while thead is hidden.

Two notes for anyone extending this. CSS comments are stripped before any
stylesheet assertion, because they are prose *about* the selector being
searched for and a comment mentioning `<select>` was enough to make the
font-size check report a control that does not exist. And headless Chrome on
Windows will not give a viewport narrower than ~477 CSS px, so the layout
assertion is written against whatever `clientWidth` it actually gets and the
probe prints it, rather than against 375.

### Mobile-only behaviours worth knowing

- **Type is expressed in `rem` under `768px`**, not in a chain of `em`, because
  that chain is what produced the sub-10px text. Each nested element has its
  own floor.
- **`.controls` is `position:static` on mobile.** Sticking a ~300px column to
  the top of a phone viewport occluded more than half the screen with the
  results scrolling underneath it.
- **The thead is clipped, never `display:none`, on mobile** — the latter also
  removes the column names from the accessibility tree. Since the sort headers
  stay `tabindex=0`, a `matchMedia` handler drops them to `-1` under the
  breakpoint and restores them above, so a keyboard user never tabs into an
  invisible control.
- **Column widths come from `<colgroup>`, and the table is
  `table-layout:fixed`.** Under auto layout the widest unbreakable value in a
  column decides that column's width, and no later rule can lower it. The
  price column was ~430px of a ~1240px table — a third of the viewport —
  because the cell was `nowrap` and one CCC row's `price_text` was
  `"Physiotherapy fees apply FIND OUT MORE BUTTON Find Out More"`, 59
  characters of page furniture that a fetcher bug had put in a cost field.
  Its own 90th percentile is 4 characters and 681 of 1522 rows have no price
  at all. Fixed layout takes the widths from the `<colgroup>` instead, so
  content wraps and the allocation is expressed in one place.

  The widths are ordered by how much text a column carries and how much a
  reader needs it, not by how it happened to come out:

  | Column | Share | Why |
  | --- | --- | --- |
  | Event | 24% | most important field, p90 50 chars |
  | Details | 20% | most verbose (p90 226, max 400) but least important to scan |
  | Location | 16% | where to go; carries the address too |
  | Date & time | 13% | the default sort key and what a reader scans for |
  | Type | 10% | a filter facet, median 18 chars |
  | Links | 10% | two fixed-width buttons |
  | Price | 7% | rarely long, rarely important |

- **`td.price` and `td.when` are `white-space:normal`, not `nowrap`.** Both were
  nowrap, which is what let one long value dictate a column, and the date
  cell's recurrence chip (`"Every Tuesday, Wednesday, Thursday and Friday"`,
  45 characters) would overflow a 13% column. If a `nowrap` is ever restored
  for a `td` selector, the mobile override has to repeat the element
  (`td.when`, not `.when`) or the base rule wins on specificity and the chip
  runs off the right edge of the screen.

- **The two most verbose cells are clamped, and width alone does not bound a
  row.** A 400-character description in a 20% column is still seven lines, and
  a row is as tall as its worst cell, so `.desc` is `-webkit-line-clamp:3` and
  the price 2, with the full value on `title` and still in the `.ics` and the
  CSV. Both clamps are released under 768px, where the card layout has the
  full width and the reader wants the whole text.

- **`render_check.py` measures the desktop grid, not just the mobile one.**
  The markup was correct in the broken case and only the layout was wrong, and
  `.tablewrap{overflow-x:auto}` turned a 1435px table in a 1241px viewport into
  a silent horizontal scroll rather than a visible break. The probe asserts
  `table-layout:fixed`, that every column received a width (which catches a
  `<colgroup>` whose order no longer matches the headers), that the grid fits,
  and that the clamps are actually bounding the tallest row.
- **Accessible names contain their visible text.** A voice-control user has to
  be able to say what they can see, so the calendar button is
  `aria-label="+ Calendar for <name>"`, not "Add to calendar". The outbound
  arrow is `aria-hidden` so it is not read as "north east arrow".
- **The inferred-date note is real text, not a `title`.** A title needs a
  hover, so the fact that 32% of published dates were derived rather than
  published was completely unreachable on a touch screen.

## Interaction defects found in review, and what fixed them

The pipeline's checks read data and CSS; almost none of them execute a user
gesture. A review of the built page in a real browser found four defects that
every existing check passed, and the same review found the page's typography
was *worse* on desktop than on the phone. Each of these has an assertion now, and
each assertion has been verified to fail the build when the defect is put back.

### Search matched the whole query as one literal substring

`hay.indexOf(q)` with no tokenisation, so any multi-word query returned
nothing at all. Measured against the shipped index:

| Query | Before | After |
| --- | --- | --- |
| `yoga cheltenham` | 0 | 24 |
| `chatty cafe cheltenham` | 0 | 24 |

The zero was the problem, not the miss: the empty state read "No events match
your filters", so a reader who had typed two words concluded there was nothing
on. The query is now split on whitespace and every term must appear. It stays a
substring test rather than word-boundary matching, so `chi` also matches
`Chisholm` — that over-matches, and the alternative under-matched.

### The sort could not be reversed on a phone

Below 768px the `thead` is clipped, so the `<select>` is the only sort control.
Its `change` handler reversed the direction only when `k === state.k`, and a
`<select>` fires no `change` event when the already-selected option is chosen
again. **The `state.dir*=-1` branch was unreachable**, so a phone reader could
pick a column and not one of them could reverse it. There is now a direction
button beside the select, and all three controls (two headers-worth of `th`
click, the select, the button) go through one `applySort()`, so they cannot
drift apart again.

### Sorting did not reset to page 1

Every filter set `state.page=1`; both sort handlers did not. Sorting 1522 rows
by name from page 20 landed the reader mid-alphabet with no indication that
pages 1–19 now existed. `applySort()` owns it for all three controls.

### The filter panel was never actually hidden

`.filterpanel{display:flex}` has specificity 0,1,0 and the UA rule for
`[hidden]` is `[hidden]{display:none}` at 0,0,1,0 — so the class rule won and
`<div id="filterpanel" hidden>` rendered **open on every load**, while the
toggle beside it reported `aria-expanded="false"`. It cost ~340px of viewport
above the results; on a phone it was 162px of the control bar. This is
invisible to a text check, because both halves of the markup are individually
correct, and it was missed by reading the template. `health_check.py` now
compares every element carrying a `hidden` attribute against the CSS rules for
its class, and requires a `.[cls][hidden]{display:none}` guard.

### The desktop table was the one with the 10px text

The `768px` block exists because an `em` chain compounded `table .9em → td →
.desc .88em` down to 10.6px on a phone. That chain was never fixed for the
desktop table, which is the wider and more common viewport. Measured there
before this change:

| Element | Desktop | Mobile |
| --- | --- | --- |
| description | 12.7px | 14px |
| address | 11.8px | 13px |
| recurrence chip | 11.8px | 13px |
| source / status badges | **10.8px** | 12px |
| Website / + Calendar | 12.2px | 14px |
| Reset filters | **11.8px** | 14px |

Every one is now `rem`, on both breakpoints, and `health_check.py` asserts each
selector declares a rem size of at least `0.75rem`. Requiring **rem** rather
than a minimum size is the part that matters: a rem length cannot compound, so
the check holds whatever a future edit sets the parent to. Deleting the
declaration is also a failure — an element with no `font-size` inherits from
the chain, which is how the badge reached 10.8px in the first place.

The same check covers target size (WCAG 2.2 SC 2.5.8). All 49 filters are
checkboxes, and `.tcheck` had no `min-height`, so the target was the native
13px box alone. The `<label>` is what a click actually lands on, and it is now
at least 24px on desktop and 44px on a phone.

### Smaller things, same cause

- **The date was printed twice on every row.** The default sort groups rows
  under day headers, and the header directly above already stated
  "Wednesday, 30 September 2026" — which each of the fifty rows beneath it then
  repeated as "Wed 30 Sep 2026". `fmtDate()` takes a `timeOnly` flag now and
  prints just the time under a grouping, with the full date kept in a
  `.visually-hidden` span so a screen reader reading cell by cell still hears
  it. This is also what the 13% Date & time column was sized for.
- **The count announced once per keystroke.** The visible count was itself the
  `role="status"` live region, and the search box re-renders 150ms after each
  keystroke, so a screen reader spoke the result count once per character. The
  visible count is written immediately and a visually-hidden twin is written on
  a 600ms trailing timer, only when the text has changed. `render_check.py`
  asserts both exist, both are populated, and the visible one is *not* a live
  region.
- **The "hide" checkbox and its count named the wrong things.** It covered
  commercial, service **and cancelled**, and both the label and the count line
  said "sold out" instead. Sold-out rows are deliberately still visible with a
  badge, so the label was wrong in both directions; the count is now split into
  the three reasons it actually has.
- **The empty state named the filters.** It listed what is actually excluding
  rows, and only offered the reset button when something is set.
- **The CSV dropped `date_inferred` and `recurrence`.** The `.ics` carries
  `X-COMMENTS-DERIVED-DATE` precisely so a derived date does not become a
  confirmed one off the page, and the CSV — 489 derived rows — undid it. It now
  carries `DateInferred`, `Recurrence`, `Suburb`, `StatusLabel` and
  `ServiceReason` alongside the 13 it had.
- **Two controls had no clear affordance.** There is a × inside the search
  field and Escape clears it; Enter flushes the pending redraw and dismisses the
  mobile keyboard, which is what `enterkeyhint="search"` had been promising.
- **Inline `onclick` on the calendar and reset buttons.** Both are read by one
  delegated listener on `#rows`, which also survives the tbody being rewritten
  by every render. Inline handlers would stop the page working under a CSP that
  forbids `unsafe-inline`.
- **A `<noscript>` fallback.** With JS off the page showed headers and an empty
  table. It now says so and links to `data/events.json`.

### Two invariants that are structural rather than visual

An edit to the control bar once left a **second copy of the CSV and Filters
buttons and a stray `</div>`** — `getElementById` addressed the first of each,
the page rendered both, and every other check passed. So:

- **ids must be unique.** There is no legitimate reason for two elements to
  share one, and the failure is silent rather than loud.
- **Container tags must balance** (`div`, `table`, `thead`, `tbody`, `main`,
  `nav`, `select`). An unbalanced `<div>` changes the shape of everything after
  it without failing anything.

Both run against the template *and* the built `index.html`, with `<style>`,
`<script>` and comments stripped first — the JSON payload is inlined into the
page and contains no tags, and a selector mentioned in a CSS comment is prose,
not a rule.

### Things this deliberately did not change

- **Facet counts are whole-index totals, not per-filter.** `Exercise &
  Fitness (441)` is how many events carry that tag across the index, not how
  many your other filters leave. Recomputing them per pass means a second
  filtered count for every facet on every keystroke. The panel hint now says
  which it is, and the suburb checkboxes carry counts too, so the two groups
  behave alike.
- **The `.ics` duration is still a flat 60 minutes.** No end time exists in the
  data, and inventing one per event type would be a guess.
- **The Type column still prints every tag.** 178 rows carry three or more and
  the longest joined string is 71 characters, so the column wraps to four or
  five lines. Truncating to a primary tag needs a real notion of primary, which
  `classify_types()` does not currently have.

## Dependency upgrades

Dependencies are pinned in `pyproject.toml`. To upgrade:
`pip install -U <pkg>`, run the full pipeline locally
(`fetch` can be skipped; use `--source <id> --max-pages 2 --detail-cap 2`
for a fast webfetch slice), confirm `health_check.py` passes, then update
the pin in `pyproject.toml`.

## Data files are written atomically

`jsonio.write_json` stages to a temp file and renames, so an exception
mid-serialisation can never truncate `data/events.json` or
`data/raw_events.json`. A truncated store would abort the next `dedupe.py`
run on `JSONDecodeError` and wedge the pipeline.

## Data schema (rows in `data/events.json`)

```json
{
  "name": "Event name",
  "datetime_iso": "2026-10-15T10:00:00",
  "datetime_display": "Wed 15 Oct 2026, 10:00 AM",
  "datetime_text": "Wednesday 15 October, 10:00am - 12:00pm",
  "has_real_date": true,
  "date_inferred": true,
  "recurrence": "Every Wednesday",
  "price_text": "$5",
  "price_sort": 5.0,
  "location": "Chelsea Activity Hub",
  "address": "3-5 Showers Ave, Chelsea 3196",
  "suburb": "Chelsea",
  "description": "Event description...",
  "types": ["Exercise & Fitness", "Social & Community"],
  "source": "https://...",
  "source_label": "kingston_hubs",
  "sources": ["https://..."],
  "is_commercial": false,
  "commercial_reason": "",
  "is_service": false,
  "service_reason": "",
  "status": "",
  "status_label": "",
  "status_detail": "",
  "hidden_by_default": false
}
```

`types`, `suburb`, `is_commercial`, `is_service`, `status` and
`hidden_by_default` are written by `build_site.py`, not by the fetchers —
`dedupe.py` owns everything to the left of them (fetchers may already supply
`suburb` from a detail page, which `build_site.py` preserves).
`hidden_by_default` is the single flag the UI filters on, and it is
`is_commercial or is_service or (status == "cancelled")`. `type_counts` at
the top level counts per tag, so its values sum to more than the row total.
