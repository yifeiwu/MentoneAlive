# Community Events Index

A searchable, sortable offline index of community events across Kingston,
Bayside, Greater Dandenong (Springvale/Keysborough), Frankston and
neighbourhood-house venues near Chelsea/Cheltenham/Mentone/Mordialloc.

> **What this file is:** how to run it, what it reads, what it writes.
> **Why it works the way it does:** [`docs/decisions.md`](docs/decisions.md).
>
> Every rule in the pipeline is there because something plausible-looking got
> through a green build. Those stories belong in one place, and that place
> should be able to tell a load-bearing invariant from scar tissue.

## Sources

Rows are counted from the last `data/events.json`; the figure is a point-in-time
observation, not a standing total. Read the live number from the store.

| Source | Label | Rows | Method |
|--------|-------|------|--------|
| Kingston Hubs (OpenCities calendar API) | `kingston_hubs` | 518 | JSON API, `urlopen`. Venue per calendar id in `sources.yaml` |
| Cheltenham Community Centre | `ccc` | 397 | HTML + Humanitix term ranges |
| Kingston Community Groups | `kingston_groups` | 343 | OpenCities local directory, ASP.NET postback paging + one detail page per group |
| Chatty Cafe venue directory | `chatty_cafe` | 237 | Venue pages, fallback schedule in config |
| Kingston Council events | `kingston_council` | 231 | HTML, all 31 listing pages walked by postback + one detail page per event |
| Kingston Seniors Festival | `kingston_seniors` | 158 | Annual PDF guide + hand-checked overrides |
| Frankston City Libraries | `frankston_libraries` | 145 | Ten listed programmes, each expanded to every date its own page states |
| Bayside Council events | `bayside_live` | 143 | HTML, full `?page=` pagination |
| Greater Dandenong | `greater_dandenong` | 132 | HTML + per-event detail pages, `suburb_filter` applied |
| Greater Dandenong Libraries | `gd_libraries` | 60 | HTML, same CMS and detail treatment |
| Kingston Arts | `kingston_arts` | 10 | HTML, one page deep |
| Frankston archived programmes | `frankston_archived` | 28 | `archived_events.json`; withheld from the page, and the live site's config entry is commented out (see below) |
| Bayside archived programmes | `bayside_archived` | 12 | `archived_events.json`, series the live feed no longer carries; withheld from the page |

### Paginating a listing that is deeper than one page

A listing page is not a listing. `kingston_council` reads page 1 of a **31-page,
301-event** Granicus listing, sorted by next occurrence, and page 1 is the next
ten of them — so events *rotated* off the front as real dates filled it. Two sat
on pages 18 and 27 (`Chinese Senior Citizens Club of Kingston`, `Tea & Talk
Chinese Conversation Table`), stayed live on the council's own site the whole
time, and were absent from every snapshot. Nothing failed: `MIN_SOURCE` for that
source was 3, against a ten-row page.

The pager is an ASP.NET postback with a **non-JS** route — a page-number
`<select>` and a `Go` submit — and `webfetch_http.paged_listing` drives it,
shared with the community-groups directory. `max_pages` is the budget (opt-in:
a source without one still reads page 1), `crawl_delay` is the rate, and both
are read from `sources.yaml`.

| | |
| --- | --- |
| Budget reached | a **warning** naming the pages not read — an operator's choice |
| A page fails to load | `PartialFetch` — the previous snapshot survives |
| The pager will not advance | `PartialFetch` — a wrong control name is *accepted* and answers with page 1 |
| The listing stops stating its depth | `PartialFetch` — publishing page 1 there is the defect itself |

The rate is 5s between **every** request this source makes, listing pages and
detail pages on one clock, which is ~28 min of deliberate waiting and the reason
the fetch step's timeout is 60. It is applied before each request rather than
after, so a run whose pages are failing cannot hammer hardest; see
[D47](docs/decisions.md).

Frankston has a written and tested fetcher (`scripts/webfetch_everi.py`) and is
not yet active: the host blocks an IP that asks for too much, and 876 pages in
one run is too much. The crawl is now resumable — `slice_size` pages per run,
with the pages read *and the rows they yielded* cached in a gitignored
`*.progress.json` — so it completes across several scheduled runs instead of
inside one. Until it does, `frankston_live` stays commented out and
`frankston_archived` carries the 28 rows. A partial crawl publishes nothing:
the fetcher refuses it, `dedupe.py` skips the progress file, and it skips a
snapshot whose source still has one.

Rows in `scripts/archived_events.json` are what is left of sources this project
could not crawl, and each carries a `status` recording whether its series is
still running:

| `status` | Meaning | On the page? |
|----------|---------|--------------|
| `live` | confirmed still running; the organiser's own site is in `live_url` | yes |
| `unverified` | not checked, or the owner could not be reached | no |
| `finished` | checked and gone | no |

Withheld rows stay in `data/events.json` as a record — a weekly market is worth
keeping — but off the list, since an unconfirmed series is the one whose dates
are most likely to have moved, and a stale date is worse than no listing. The
status is recomputed every run, so relisting a programme brings it back with no
migration. `python scripts/archived_coverage.py` reports which archived series
a live feed still publishes; `scripts/apply_archive_fixes.py` is the one-off
that recorded the hand-checked statuses.

This matters more than it sounds: writing `frankston_archived` off as
unreachable also wrote off ten series that were still running on their
organisers' own sites, and one of them published a market on 26 December that
the organiser states does not happen.

Four hosts 403 a plain `urllib` request outright and so need browser TLS
impersonation: `kingston_council` and `kingston_arts`, the OpenCities directory
behind `kingston_groups`, and the libraries CMS behind
`frankston_libraries`. The two Greater Dandenong
sources need it for their event **detail** pages only — the listing answers
plain HTTP, and the suburb that the catchment filter depends on is on the detail
page. Everything else is plain HTTP. That is six `impersonate: true` entries in
`sources.yaml` rather than a second script; see [D2a](docs/decisions.md).

## Pipeline

```bash
pip install .             # pins live in pyproject.toml, read from there

python scripts/fetch_sources.py           # all sources -> snapshots
python scripts/dedupe.py                  # merge + dedupe -> data/events.json
python scripts/build_site.py              # types/commercial/status -> render index.html
python scripts/health_check.py            # fail loudly on bad output
python scripts/checks.py                  # all rule assertions (exit non-zero on failure)
python scripts/render_check.py            # render index.html in headless Chrome, measure it
python scripts/quality_audit.py           # report duplication and data quality (no gate)
```

`quality_audit.py` is a report rather than a gate, on purpose: the other two
assert invariants, and a number that moves as the sources move is information
rather than a failure. It exists for the class of defect no gate covers — a
description that is the page's address block, an internal scratch field that
reached the store, two sources styling one class differently and producing two
rows for it. Run it after `build_site.py`, which writes the artefact it reads.

One it currently reports, and which no gate will: a description that leaks the
page's address block, or a source styling one class two ways. These move with
the sources, which is why it is a report and not a gate.

Useful flags:

```bash
python scripts/fetch_sources.py --check-config                  # validate sources.yaml only
python scripts/fetch_sources.py --source kingston_hubs           # one source
python scripts/fetch_sources.py --source bayside_live --max-pages 2 --detail-cap 5
SOURCE_DATE_EPOCH=1789948800 python scripts/dedupe.py            # pin "today" for a reproducible run
```

**Ordering matters in one place:** `build_site.py` must run **before**
`health_check.py`, because the health check verifies the status and
default-hidden flags that `build_site.py` writes. Running it the other way round
fails the build, which is the intended outcome.

### Fetching

One fetcher, `scripts/fetch_sources.py`, reads `scripts/sources.yaml` and
writes its output in two places:

- `scripts/webfetch_snapshots/*.json`, one per source, for the entries in the
  config's `webfetch:` list — these are committed, so each source's rows stay
  separately visible and one overwritten file cannot lose that view;
- `data/raw_events.json`, a bare list, for the `sources:` list. Gitignored: it is
  scratch for `dedupe.py`, regenerated on every run.

Every source is reached through a session, and `impersonate: true` selects which
kind: a `curl_cffi` Chrome-TLS session, or the plain `urllib` one. Both are built
lazily and reused, so an eleven-source run opens one pool of each rather
than eleven.

`scripts/webfetch_http.py` owns the shared pieces — the session constructors, row
shape (`make_row`), month names, written-time conversion, the detail-page crawl
(`enrich_details`) and progress reporting. One module per platform lives
alongside it: `webfetch_{bayside,granicus,ccc,seniors}.py`. The sources that
reach their host with plain `urllib` live in `scripts/fetch_urllib_sources.py`.

Manual overflow snapshots named `<id>_manual.json` are merged automatically, for
the Granicus sources whose pager is a JS postback that cannot be walked.

### Failure behaviour

Full rules in [D3](docs/decisions.md). In short:

- **`raise PartialFetch` is the only "do not publish" signal.** It means the
  fetch was cut short, blocked, or misconfigured, and the existing snapshot
  survives.
- A fetcher that returns `[]` means the source genuinely has nothing. Both cases
  exit non-zero.
- A source that returns 0 rows never overwrites its snapshot — a festival out of
  season must not erase the season it already published.
- Config faults are found before anything is fetched, all at once.
- A *successful but truncated* fetch prints the previous size beside the new
  one and calls out a shrink in as many words. It is a warning, not a refusal,
  because a term genuinely ending does shrink a source.

## Deduplication (`dedupe.py`)

1. **Exact hash** on (normalized name, *start time*, normalized location). The
   start time is part of the key so two sessions of one class at a venue in a
   day both survive.
2. **Same name + location** across sources merges source lists, keeping the
   dated variant — except when the two rows start at different times of day.
3. **Fuzzy**: name similarity ≥ 0.75 AND same day within ±30 min AND strict
   location equality.
4. **Prune** events older than 90 days, in Australia/Melbourne (not by stripping
   a UTC offset, which mis-prunes events near the boundary).
5. **Date inference** (`recurrence.py`), then a final **exact-only** collapse
   keyed on name + *start time* + location.
6. **Same session, two sources** (`dedupe_by_source_url`), keyed on the event's
   **base name** — the title up to its first ` - `, `:` or `|`, accent-folded —
   plus the same date, start time and a compatible venue. The programme key holds
   a *list* of candidates, so two genuine twins can both merge.
7. **Untimed twins** (`drop_untimed_twins`): a listing stating only "Wednesday"
   yields an occurrence at 00:00. When a timed occurrence of the same programme
   exists, the midnight row restates that session rather than adding an event.
8. **A listing re-dated by a later fetch** (`drop_superseded_listing_rows`): a
   listing that runs for weeks states its *next* occurrence, which advances
   while the run does — so every fetch returns it at a new start time and the
   store keeps a copy per run. Rows of one listing that name a next occurrence,
   and that the sources no longer state as a slot, collapse onto the current one.
   A listing is only treated this way when it says so: a multi-day listing
   materialised one row per day (`Fairies at Rippon Lea`) is indistinguishable
   by shape, and is left alone. See [D46](docs/decisions.md).

Two orderings that are load-bearing:

- Step 5's fuzzy pass is **not** re-run after inference. Sibling courses at one
  venue (`Cert I in EAL` vs `Cert III in EAL`, both Monday 9am) score above 0.75
  and would fuse into one event.
- Step 6 runs **after** inference, because the duplicated rows it exists to
  merge are the `date_inferred` ones.
- Step 8 runs **before** `reconcile_store()`, which cannot make this call:
  its series test keeps any row whose `(source, name, venue)` is still
  published, and these rows are that one listing at eight different times.

`reconcile_store()` then drops any row that **none of its own recorded sources**
still justify - see [D6](docs/decisions.md) for the two boundaries that keep it
from deleting good data. `refresh_inferred()` re-derives a stored `date_inferred`
row whose time disagrees with its own text, and **withdraws** one whose own text
no longer produces its date at all - an inferred date is what the text yields,
not an independent fact, so a date the text cannot produce is a session that
cannot exist. Only dates from today onwards are tested, since an expansion runs
forward from today. See [D48](docs/decisions.md).

## Date inference (`recurrence.py`)

An event needs a real date to be placed on a calendar, so **every published row
carries one** — `health_check.py` fails the build otherwise. Some sources state
a recurring pattern in prose; those are parsed into a spec and expanded.

| Pattern | Example | Expansion |
| --- | --- | --- |
| Nth weekday of month | `1st Wednesday of every month` | 12 months |
| Fortnightly | `Every second Saturday`, `Friday (fortnightly)` | every 2nd week |

A fortnight's phase — which of the two is "first" — cannot come from the run
date, because that makes it depend on the weekday the pipeline happens to run
on: the same `Friday (fortnightly)` text inferred on a Friday starts that
Friday, and on the Saturday a week later. A stated start date is the venue's own
phase and wins; without one the phase is ISO week parity against a fixed epoch,
which is arbitrary but does not move. See [D48](docs/decisions.md).
| Explicit range | `Term 4 (17th October - 5th December)` | that window only |
| Full date | `Thursday, May 25th, 2023` | single occurrence |
| Weekday(s) + times | `Mondays and Thursdays 9am - 12pm` | 12 occurrences |
| Weekday in the title | `Friday After School STEAM session` | 12 occurrences, all-day |

The test is the presence of a date, not the plurality of the weekday: `Tuesdays
9am` and `Tuesday 9am` both mean a weekly class, while `Friday 2 October,
11:00am` is one session. A date that has just passed settles the listing rather
than re-expanding it.

Undateable listings are **removed**, with the reason printed. Inferred rows are
tagged `date_inferred: true` and carry a `recurrence` label, which the table
renders and the `.ics` and CSV exports carry as `X-COMMENTS-DERIVED-DATE`,
`DateInferred` and `Recurrence`.

> **A note specific to Greater Dandenong:** it has no parseable date in its own
> date field — the day appears only inside the description — so **all** of its
> rows are `date_inferred`. A change to the date parser moves that source
> wholesale. Read a single odd row there before blaming the fetcher.

The rules and the defects behind each one are in
[D10](docs/decisions.md)–[D19](docs/decisions.md).

### Time formats the parser accepts

`am/pm` (`9am`, `5.30pm`, `11:15am`), 24-hour (`14:00`, `18.30`), `noon` and
`midday` (`12noon`, `noon - 1pm`), and a bare hour as one end of a range
(`12 - 1.30pm`).

A bare `12` is *not* a standalone token — schedule text is full of bare numbers
("Monday 28 September") and matching digits alone turns day-of-month into an
hour. Every token is `\b`-anchored so "afternoon" is never read as "noon".
Midnight (`00:00`) is the pipeline's marker for "date known, time not stated",
which is why those rows render as *all day*.

A bare second time inherits the weekday before it — `Mondays 9am-12pm,
12:30pm-3:30pm` really is two Monday sessions. This is deliberate; see
[D19](docs/decisions.md).

## Classification

`activity_types.py` classifies over 18 real types (plus `Other`), multi-tag, from
the title (plus any source-supplied category) and the description as a union.
Results come back in `TYPES` order with `["Other"]` only when nothing matched.
Because tags compose, rule order is not load-bearing for correctness. The UI
filter is OR: an event stays visible while any of its tags is ticked. Where a
source publishes its own taxonomy of what a listing is (`SOURCE_TAXONOMY` —
Kingston's community-groups directory states a curated category per entry), that
is used in preference to matching the prose, and unioned with it. See
[D23](docs/decisions.md).

`commercial.py` flags pub/meal-deal promos and priced-or-pub trivia as
`is_commercial`; gold-coin community trivia stays visible.

`status.py` flags three separate things, none of which is answered by the
listing's own fields:

- **status** — sold out / fully booked stay *visible but badged*; **cancelled**
  rows are hidden.
- **services** — a community centre's takeaway meals is an opening window, not
  a session. Deliberately not "commercial", even when subsidised.

`build_site.py` writes `hidden_by_default = is_commercial or is_service or
cancelled`, which is the one flag the UI filters on. See
[D24](docs/decisions.md).

## Verification

`scripts/checks.py` runs every rule assertion:

```bash
python scripts/checks.py              # all suites
python scripts/checks.py recurrence   # one suite
```

Each suite is pure — no network, no writes — and exits non-zero with the actual
value and the expected one, so a rule change that alters a classification fails
the build before it can reach the published page. The suites are:

| Suite | Pins |
| --- | --- |
| `activity_types` | 64 name/description → tags cases |
| `status` | 10 sold-out / service cases |
| `recurrence` | the date-inference table, on a fixed reference date |
| `dedupe` | name, venue and series-key normalisation; the two field-level defects the store repair pass recognises (a description that restates the title, a repeated address segment); the archive file's shape |
| `commercial` | commercial detection rules |
| `webfetch_granicus` | Granicus address parsing, the postback pager walk, and the rate limit |
| `webfetch_directory` | directory cards, addresses and hours tables |
| `webfetch_everi` | Everi detail pages: dates, venues, series GUID |
| `webfetch_frankston_libraries` | library listing cards, the dates their pages state, and a resumable crawl |
| `webfetch_http` | shared time/month/row parsing, `join_address`, and the rule that a description may not restate its own name |
| `build_site` | suburb extraction, and which rows reach the page |
| `fetchers` | the fetch call convention, without a network, and the rule that `raw_events.json` is only written when every plain source delivered |
| `failure_signals` | config validation, the do-not-publish signal, and store stability across run dates |
| `fetcher_equivalence` | the fetchers extract the same rows they used to |

`build_site.py` and `dedupe.py` are both pipeline stages and suites, so
`checks.py` passes `--test` to them: running either from the runner does not
rebuild the site or re-run the merge.

`health_check.py` then verifies the *published output*: a total floor, per-source
floors, zero duplicates on all three keys, no inferred row whose stored date or
time contradicts its own text, every row dated, every non-online row addressed,
no row the sources do not back, the Greater Dandenong catchment still filtering,
and that the accessibility and layout invariants hold in the stylesheet and the
markup.

`render_check.py` renders the built page in headless Chromium and measures the
result. It is separate because it is not pure: it is the only check that
executes JavaScript, and a syntax error in the template would otherwise build
cleanly and publish a **blank calendar**. If no Chromium-family browser is found
it prints `SKIP` and exits 0; set `$BROWSER` to force a specific one. The GitHub
runner has Chrome preinstalled, so CI always gets the real check.

### Addresses: `scripts/verify_addresses.py`

`dedupe.py` can tell that an address is *malformed* and repair it from a source
that has since been fixed. It cannot tell whether a well-formed address is the
right one. That question needs a geocoder:

```bash
python scripts/verify_addresses.py                    # report, change nothing
python scripts/verify_addresses.py --source bayside_live --check
python scripts/verify_addresses.py --fix              # write corrections
```

It looks up OpenStreetMap through Nominatim and reports any row whose suburb,
state or postcode the map contradicts, defaulting to the rows whose address
already looks doubtful. A geocoder is a check, not a gate: it is kept out of the
pipeline and out of `checks.py` because every suite there is required to be
pure, and because Nominatim's usage policy caps a client at about one request
per second — fine for the few dozen doubtful rows, not for 2,200. It reports by
default and only writes under `--fix`, because a council's preferred way to
write its own address is not always the one the map would pick.

It earns its keep already: Bayside publishes the Sandringham Library at postcode
**3193**, and the library is at **3191**. Nothing in the pipeline can see that —
a postcode is four digits and looks like a postcode.

## Seasonal maintenance

### Refreshing the seniors festival each October

Two values must move together, and `health_check.py` fails the build if they
disagree:

| File | Key | Meaning |
| --- | --- | --- |
| `scripts/sources.yaml` | `kingston_seniors.year` | the guide being parsed |
| `scripts/seniors_festival_overrides.json` | `year` | the guide the sessions were transcribed from |

The override dates are **literal and never re-stamped** — the festival falls on
different days each year, so a 2026 file cannot be shifted mechanically. If the
two years drift, the next run emits last year's dates, `prune_old()` deletes
them as over 90 days old, and the festival vanishes.

A stale `year` in `sources.yaml` is a hard error only during the festival months
(Sept–Nov); outside that window a stale year is correct, since last year's
festival really has finished. A mismatch between the two files is *always* an
error.

The Kingston guide's multi-column spreads lose column association in flat-text
PDF extraction, which is why `seniors_festival_overrides.json` exists at all.

## GitHub Actions

`.github/workflows/update-events.yml` runs weekly on Monday at 06:00 UTC with
per-step timeouts: fetch → dedupe → build → health check → rule assertions →
render check → commit (`data/events.json`, `index.html`, snapshots) → Pages
deploy. `workflow_dispatch` triggers an off-cycle refresh.

A failed fetch leaves the last good `events.json` and `index.html` in place
rather than publishing a smaller calendar. That is intentional — do not add
`continue-on-error`.

The commit step must push, not just commit: `data/events.json` is the canonical
store the *next* `dedupe.py` run reads as its pre-merge baseline, so committing
without pushing leaves that store frozen and the repo silently drifts away from
the deployed site.

## Data files

Both stores are written atomically — `jsonio.write_json` stages to a temp file
and renames, so an exception mid-serialisation can never truncate
`data/events.json` or `data/raw_events.json`. A truncated store would abort the
next `dedupe.py` run on `JSONDecodeError` and wedge the pipeline.

| File | Written by | Committed |
| --- | --- | --- |
| `scripts/webfetch_snapshots/*.json` | `fetch_sources.py` | yes |
| `data/raw_events.json` | `fetch_sources.py` | no (gitignored) |
| `data/events.json` | `dedupe.py`, then `build_site.py` | yes |
| `index.html` | `build_site.py` | yes |

## Data schema (rows in `data/events.json`)

```json
{
  "name": "Event name",
  "datetime_iso": "2026-10-15T10:00:00",
  "datetime_text": "Wednesday 15 October, 10:00am - 12:00pm",
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
  "source_id": "kingston_hubs",
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
`dedupe.py` owns everything to the left of them (fetchers may already supply a
`suburb` from a detail page, which `build_site.py` preserves). `status` and
`is_commercial` are empty/`false` on most rows and are omitted from the built
page's inlined copy; `events.json` always carries them.

## Dependency upgrades

Dependencies are pinned in `pyproject.toml`. To upgrade: `pip install -U <pkg>`,
run the pipeline locally (`fetch` can be skipped — use
`--source <id> --max-pages 2 --detail-cap 2` for a fast slice), confirm
`health_check.py` passes, then update the pin.