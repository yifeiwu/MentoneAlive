# Community Events Index

A searchable, sortable offline index of community events across Kingston,
Bayside, Greater Dandenong (Springvale/Keysborough), Frankston and
neighbourhood-house venues near Chelsea/Cheltenham/Mentone/Mordialloc.

## Sources

| Source | Label | Method |
|--------|-------|--------|
| Kingston Hubs (OpenCities calendar API) | `kingston_hubs` | JSON API (`fetch_events.py`) |
| Kingston Council upcoming events | `kingston_council` | HTML, page 1 (Granicus needs browser TLS) |
| Kingston Arts | `kingston_arts` | HTML, page 1 (same platform) |
| Kingston Seniors Festival (annual PDF guide) | `kingston_seniors` | PDF parse + hand-checked overrides |
| Kingston Libraries | via Council/Hubs listings | — |
| Bayside Council events | `bayside_live` | HTML, full `?page=` pagination |
| Bayside Seniors Festival | `bayside_seniors` | HTML, festival page pagination |
| Greater Dandenong (Springvale/Keysborough filter) | `greater_dandenong` | HTML + per-event detail pages (`fetch_events.py`) |
| Greater Dandenong Libraries | `gd_libraries` | HTML (`fetch_events.py`) |
| Chatty Cafe venue directory | `chatty_cafe` | Venue pages (`fetch_events.py`) |
| Cheltenham Community Centre term classes | `ccc` | HTML + Humanitix dates |
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
suburb from the address, and filters on it. Of the 51 events the council
lists, 15 are in the catchment; the other 36 are Dandenong (17), Noble Park
(16) and Heatherton (2), and are dropped. `suburb_filter` is the single lever
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

## Pipeline

```bash
pip install -r requirements.txt               # pinned versions
python scripts/fetch_events.py                # Python-safe sources → data/raw_events.json
python scripts/webfetch_sources.py            # browser-impersonating sources → scripts/webfetch_snapshots/*.json
python scripts/dedupe.py                      # merge + dedupe → data/events.json
python scripts/build_site.py                  # types/commercial/status → render index.html (repo root, GitHub Pages)
python scripts/health_check.py                # fail loudly on bad output
python scripts/activity_types.py              # assert the classifier rules
python scripts/status.py                      # assert the sold-out / service rules
python scripts/render_check.py                # prove index.html renders rows, not a blank page
```

The order matters in one place: `build_site.py` must run **before**
`health_check.py`, because the health check verifies the status and
default-hidden flags that `build_site.py` writes. Running it the other way
round fails the build, which is the intended outcome.

`scripts/` layout: `fetch_events.py` (API/Drupal/venue sources),
`webfetch_http.py` (shared session/date helpers, `PartialFetch`),
`webfetch_{bayside,granicus,ccc,seniors}.py` (one fetcher each),
`webfetch_sources.py` (thin orchestrator), `dedupe.py`, `recurrence.py`,
`commercial.py`, `status.py`, `activity_types.py`, `jsonio.py` (atomic writes),
`build_site.py`, `health_check.py`, `render_check.py`.

### Failure behaviour

Both fetchers **exit non-zero** when a source does not fetch cleanly, so a WAF
block or a markup change turns the Actions run red instead of quietly
publishing a smaller calendar:

- a source that raises, or returns 0 rows, is a hard failure;
- a source that returns 0 rows never overwrites its snapshot;
- a multi-page crawl that stops early raises `PartialFetch`, and the existing
  snapshot is kept — a partial crawl is indistinguishable from a source with
  genuinely no events, so it must not replace good data.

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

### Inferred rows are refreshed, never frozen

Inferred dates are written back into the store, so a row dated by an older,
buggier build keeps its old time indefinitely — nothing re-derives it.
`refresh_inferred()` runs before `resolve_dateless()` and re-derives any
`date_inferred` row whose stored time disagrees with the time its own text
states **for that row's weekday**. It acts only when the weekday states
exactly one time: `Cert III in EAL` runs twice on Mondays (09:00 and 12:30),
so either stored value is legitimate and both are left alone.

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

A bare weekday is **not** a pattern. `Friday 2 October, 11:00am` is one dated
event; treating it as weekly would fabricate twelve. A weekday only becomes a
recurring series when something marks it as ongoing — a time range
(`Wednesdays 2:00pm - 3:30pm`) or recurring language (`every week`, `term`,
`ongoing`).

Recognised patterns, in priority order:

| Pattern | Example | Expansion |
| --- | --- | --- |
| Nth weekday of month | `1st Wednesday of every month` | 12 months |
| Fortnightly | `Every second Saturday` | every 2nd week |
| Explicit range | `Term 4 (17th October - 5th December)` | that window only |
| Full date | `Thursday, May 25th, 2023` | single occurrence |
| Weekday(s) + times | `Mondays and Thursdays 9am - 12pm` | 12 occurrences |
| Weekday in the title | `Friday After School STEAM session` | 12 occurrences, all-day |

Details worth knowing:

- **12 occurrences max** per source event, earliest first, so a weekly class
  covers ~3 months and a monthly one ~1 year. `Term 4 (10 weeks)` overrides
  the cap with the stated session count.
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

A midnight stamp is the pipeline's marker for "date known, time not stated",
not a 00:00 start, so the table shows those as `all day`. Every inferred row
that lands on `00:00` was checked: none of them state a time in their own
text.

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
own text, that every row has a real date, that every source label has badge
CSS and a friendly name, and that both the template and the built
`index.html` are a single document with no unfilled placeholders and no calls
to undefined functions. Seasonal sources (seniors festivals) are warn-only
since they legitimately decay out of season.

Five further checks cover the defects described above, each of which was
verified to fail the build when reintroduced:

- **no row the sources do not back** — it re-runs `dedupe.reconcile_store()`
  over the published rows against this run's inputs, so a stale start time or
  a withdrawn listing cannot survive a green build;
- **`build_site.py` actually ran** — every row carries a `hidden_by_default`
  flag, without which the page would quietly show every sold-out workshop and
  drop-in service;
- **status/service flags match their own text** — re-derived, not trusted, so
  a `status.py` regression cannot leave a sold-out row looking bookable;
- **the Greater Dandenong detail fetch still works** — if most of its rows are
  back at the generic `Greater Dandenong` location, the venue is unknown again
  and the catchment is not filtering anything;
- **no Greater Dandenong venue outside the configured catchment** — read back
  from `sources.yaml`, so widening `suburb_filter` is the way to admit more,
  and events held online are exempt (they have no suburb).

The Greater Dandenong floor is deliberately low (8). That source is a narrow,
genuinely filtered catchment of 15 events publishing one stated date each, so
the check's job is to catch the scraper dying at 0 rows, not a quiet season.

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
- **`td.when` must repeat the element in the mobile override** (`td.when`, not
  `.when`). The base `td.price,td.when{white-space:nowrap}` wins on
  specificity, and the recurrence chip inside it then runs off the right edge
  of the screen.
- **Accessible names contain their visible text.** A voice-control user has to
  be able to say what they can see, so the calendar button is
  `aria-label="+ Calendar for <name>"`, not "Add to calendar". The outbound
  arrow is `aria-hidden` so it is not read as "north east arrow".
- **The inferred-date note is real text, not a `title`.** A title needs a
  hover, so the fact that 58% of published dates were derived rather than
  published was completely unreachable on a touch screen.

## Dependency upgrades

`requirements.txt` is pinned to known-good versions. To upgrade:
`pip install -U <pkg>`, run the full pipeline locally
(`fetch` can be skipped; use `--source <id> --max-pages 2 --detail-cap 2`
for a fast webfetch slice), confirm `health_check.py` passes, then update
the pin.

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
