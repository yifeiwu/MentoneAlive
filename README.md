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
| Kingston Hubs (OpenCities calendar API) | `kingston_hubs` | 523 | JSON API, `urlopen`. Venue per calendar id in `sources.yaml` |
| Cheltenham Community Centre | `ccc` | 390 | HTML + Humanitix term ranges |
| Chatty Cafe venue directory | `chatty_cafe` | 234 | Venue pages, fallback schedule in config |
| Kingston Seniors Festival | `kingston_seniors` | 159 | Annual PDF guide + hand-checked overrides |
| Bayside Council events | `bayside_live` | 142 | HTML, full `?page=` pagination |
| Frankston archived programmes | `frankston_archived` | 26 | Static snapshot (live pages WAF-blocked) |
| Greater Dandenong | `greater_dandenong` | 18 | HTML + per-event detail pages, `suburb_filter` applied |
| Bayside archived programmes | `bayside_archived` | 12 | Static snapshot |
| Kingston Arts | `kingston_arts` | 10 | HTML, same platform as `kingston_council` |
| Kingston Council upcoming events | `kingston_council` | 4 | HTML, page 1 only (Granicus pager is a JS postback) |
| Greater Dandenong Libraries | `gd_libraries` | 4 | HTML, same CMS and detail treatment |

Two hosts 403 a plain `urllib` request outright and so need browser TLS
impersonation: `kingston_council` and `kingston_arts`. The two Greater Dandenong
sources need it for their event **detail** pages only — the listing answers
plain HTTP, and the suburb that the catchment filter depends on is on the detail
page. Everything else is plain HTTP. That is four `impersonate: true` entries in
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
```

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
lazily and reused, so a nine-source run opens one pool of each rather than nine.

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

Two orderings that are load-bearing:

- Step 5's fuzzy pass is **not** re-run after inference. Sibling courses at one
  venue (`Cert I in EAL` vs `Cert III in EAL`, both Monday 9am) score above 0.75
  and would fuse into one event.
- Step 6 runs **after** inference, because the duplicated rows it exists to
  merge are the `date_inferred` ones.

`reconcile_store()` then drops any row that **none of its own recorded sources**
still justify — see [D6](docs/decisions.md) for the two boundaries that keep it
from deleting good data. `refresh_inferred()` re-derives a stored `date_inferred`
row whose time disagrees with its own text.

## Date inference (`recurrence.py`)

An event needs a real date to be placed on a calendar, so **every published row
carries one** — `health_check.py` fails the build otherwise. Some sources state
a recurring pattern in prose; those are parsed into a spec and expanded.

| Pattern | Example | Expansion |
| --- | --- | --- |
| Nth weekday of month | `1st Wednesday of every month` | 12 months |
| Fortnightly | `Every second Saturday`, `Friday (fortnightly)` | every 2nd week |
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

`activity_types.py` classifies over 17 types, multi-tag, from the title (plus any
source-supplied category) and the description as a union. Results come back in
`TYPES` order with `["Other"]` only when nothing matched. Because tags compose,
rule order is not load-bearing for correctness. The UI filter is OR: an event
stays visible while any of its tags is ticked. See
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
| `activity_types` | 56 name/description → tags cases |
| `status` | 10 sold-out / service cases |
| `recurrence` | the date-inference table, on a fixed reference date |
| `commercial` | commercial detection rules |
| `webfetch_granicus` | Granicus address parsing |
| `webfetch_http` | shared time/month/row parsing |
| `failure_signals` | config validation and the "do not publish" signal |
| `fetcher_equivalence` | the fetchers extract the same rows they used to |

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