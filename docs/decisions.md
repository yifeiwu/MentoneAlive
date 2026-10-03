# Architectural decisions

The *what* of this pipeline lives in `README.md`: which sources exist, how to
run it, what a row looks like. This file is the *why* — the rules it runs on,
what each one is defending against, and whether it still holds.

Every decision here was originally written as the story of a defect that had
already shipped. That is where they came from and it is worth keeping: each
rule exists because something plausible-looking got through a green build. But
a post-mortem is a poor specification, because it cannot tell a load-bearing
invariant from scar tissue. So each entry carries a verdict.

## Verdict scale

| Verdict | Meaning |
| --- | --- |
| **Hold** | The rule earns its cost. Say nothing more about it. |
| **Hold, but** | The rule is right; the stated rationale is now wrong, or narrower than it should be. |
| **Hold with caveat** | Correct, but it overloads a value or carries a known cost. Documented so the next person does not re-derive it. |
| **Superseded** | The rule was sound when written; its evidence no longer exists. Replaced here. |
| **Questionable** | The goal is right, the mechanism is not. Flagged, not yet changed. |

---

## 1. Pipeline shape

### D1. Snapshot JSON is the boundary between fetching and merging — **Hold**

Every fetcher writes `scripts/webfetch_snapshots/*.json` (or
`data/raw_events.json`); `dedupe.py` reads those files and nothing else. No
fetcher and `dedupe.py` never share a process.

This is the decision that makes the rest possible. It is why a fetch is
re-runnable in isolation, why a run is reproducible under `SOURCE_DATE_EPOCH`,
and why `reconcile_store` (D8) can ask "does any source still justify this
row?" — the answer is a set membership test against recorded `sources`.

*Owner:* `dedupe.load_live_inputs`, `fetch_sources.main`.

### D2. Fetchers split by TLS capability — **Superseded**

The original split was `fetch_events.py` (plain `urllib`) and
`webfetch_sources.py` (`curl-cffi` Chrome impersonation), on the stated grounds
that "plain `urllib`/`requests` gets HTTP 403 from the Granicus WAF".

Probing every configured host shows the claim is two hosts wide, not eight:

| Host | Plain urllib |
| --- | --- |
| `kingston.vic.gov.au` (council events) | **403** |
| `kingstonarts.com.au` | **403** |
| `bayside.vic.gov.au` (listing + detail) | 200 |
| `chelt.com.au` (CCC) | 200 |
| Kingston seniors PDF | 200 |
| `chattycafeaustralia.org.au` | 200 |
| `greaterdandenong.vic.gov.au` | listing 200; 3 of 4 detail pages 200, 1 blocked |
| `libraries.greaterdandenong.vic.gov.au` | 1 of 2 blocked — *under curl-cffi too* |

So two hosts need impersonation outright, and the two Greater Dandenong sources
need it **partially**: the listing answers plain HTTP, but the event detail pages
are where the suburb is stated, and the catchment filter has nothing to work
with without them. They are marked `impersonate: true` for that reason, not
because the listing needs it.

The last row is worth keeping in mind: one Greater Dandenong library URL returns
403 under `curl-cffi` as well, so impersonation is not a general answer to this
class of block — which is why `enrich_details` treats a mostly-failed detail
crawl as `PartialFetch` rather than trusting the session.

The stated reason the split had to persist was layering — `fetch_events.py` uses
`webfetch_http.month_number()` and must not pull in a network library — but
that stopped being true as soon as the Greater Dandenong fetcher reached for
`webfetch_http.make_session()` from inside the "urllib" fetcher.

**Replaced by:** one fetcher, one config list, `impersonate: true` set per
source. See D2a.

### D2a. One fetcher, impersonation as a per-source flag — **Hold**

Impersonation is a property of a *host*, not of a *script*. Expressing it as a
module boundary meant a whole second entry point, a second config list, a
second normalisation pass, and an inverted dependency where the
`curl-cffi` orchestrator imported two helpers from the `urllib` fetcher. It also
made it invisible which sources actually needed it.

One consequence is worth stating, because getting it wrong is silent: **every
source needs a session, and the flag selects which kind.** `webfetch_http`
therefore owns two constructors — `make_session()` (curl-cffi, Chrome TLS) and
`make_plain_session()` (a `urllib` shim with the same `get()` →
`.status_code`/`.text`/`.content` shape). Passing `None` to a source that is not
flagged looks right and fails only on the sources that happen to be working,
which is the worst possible time for it to surface.

*Owner:* `scripts/fetch_sources.py`, `webfetch_http.make_plain_session`,
`impersonate:` in `sources.yaml`.

### D3. `raise PartialFetch` is the only "do not publish" signal — **Hold**

The contract, stated exactly:

- a fetcher that **raises `PartialFetch`** means the fetch was cut short,
  blocked, or misconfigured — the existing snapshot survives;
- a fetcher that **returns `[]`** means the source genuinely has nothing.

Before this, `[]` carried four indistinguishable meanings (WAF block, no
events, no `year:` configured, PDF download failed), and all four were reported
to the operator as "returned 0 rows".

The related rules that follow from it:

- **A source that raises, or returns 0 rows, is a hard failure.** The fetcher
  exits non-zero.
- **0 rows never overwrites a snapshot.** A festival out of season must not
  erase the season it already published.
- **A crawl that stops early raises `PartialFetch`.** A partial crawl is
  indistinguishable from an empty source, so it must not replace good data.
- **A detail-page block raises too** (`enrich_details`, below 50% success).
  This used to be a bare `continue` in three sources, so a WAF block on the
  *event* pages was indistinguishable from a source with no event pages: the
  snapshot was overwritten listing-only, every venue blank, run still green.

*Owner:* `webfetch_http.PartialFetch`, `enrich_details`, `fetch_sources.main`.

### D4. Config is validated before any request — **Hold**

`validate_config()` runs over every entry up front — required keys per type, a
snapshot name ending in `.json`, no two sources claiming one snapshot, no
duplicate id — so a typo is one message naming the entry, reported once,
instead of a `KeyError('url')` from three frames inside a fetcher after the
earlier sources have already been crawled.

*Owner:* `validate_config`, `fetch_sources.main`.

### D5. A successful but truncated fetch is visible, not refused — **Hold**

None of D3 covers a fetch that *succeeds* and returns a fraction of the real
data: a starved `--detail-cap`, a markup change that drops one section, a site
that quietly stops listing half its events. `PartialFetch` does not fire, and
`reconcile_store` will believe the new rows — it drops whatever no source
justifies, and the source that stopped justifying them is this one.

So every write prints the previous size beside the new one, and a shrink is
called out in as many words.

It is deliberately a **warning rather than a refusal**, and that is the load-
bearing part of the decision: a term genuinely ending does shrink a source, and
blocking that would be worse than reporting it.

*Owner:* `_previous_count`, `_count_change`.

---

## 2. The store

### D6. `data/events.json` is a cache, and every row must be re-derivable — **Hold**

A cache is only correct if every entry can still be re-derived from the source
it came from. `reconcile_store()` runs after everything is merged and drops any
row that **none of its own recorded sources** still justify. `health_check.py`
re-runs it over the published store and fails if anything would be dropped.

Two boundaries keep it from deleting good data:

- a row is kept if *any* of its `sources` still vouches for it, so a cross-source
  merge is not undone by one of its parents moving on;
- a row whose `source_id` is absent from this run's inputs entirely is left alone
  alone — a seasonal festival out of season, or a fetcher that failed, must not
  take its existing rows down with it. The 90-day prune still bounds those.

This is what catches the two phantoms no duplicate check can see, because the
stale row differs from the row that replaced it in *exactly* the field being
compared: an upstream time correction (same URL, fetched twice, two times) and
a withdrawn listing (a twelve-row inferred expansion outliving its page).

*Owner:* `dedupe.reconcile_store`, `_justification_keys`.

### D7. Merging fills blanks only — **Hold, but**

`_merge_sources()` deliberately only fills a **blank** field; whatever the store
already has wins.

This is right when the store's value is merely plainer, and wrong when the
fetcher that produced it was buggy — in which case a wrong value is permanent,
because nothing re-derives it. Two such defects persisted through repeated
re-crawls of already-corrected sources: 86 rows carrying
`"14 Willis St,, Hampton, Victoria 3188"`, and 96 carrying
`"$12 per session FIND OUT MORE BUTTON Find Out More"`.

The two narrow exceptions that do replace a stored value are correct and should
stay:

- a **venue** whose head is a strict prefix of the incoming one — the narrower
  string is the one that has to go;
- an **address** only when the stored one is visibly broken (an empty `,,`
  segment, a dangling comma) *and* the live one is not;
- a **price** only when the live one is shorter, since for a cost field that
  means the page furniture has been cut.

**The caveat:** a blank-only merge is what forced D8 and D9 into existence. See
"the append-only merge" below.

### D8. A field the source has *corrected* is re-derived — **Hold**

A dateless listing expands into a dozen store rows, so the repair falls back
from an exact timestamp match to `(name, url)` — the same key
`_justification_keys()` uses.

*Owner:* `dedupe._merge_sources` malformed-field pass.

### D9. Inferred rows are refreshed, never frozen — **Hold**

`refresh_inferred()` runs before `resolve_dateless()` and re-derives any
`date_inferred` row whose stored time disagrees with the time its own text
states *for that row's weekday*. It acts only when the weekday states exactly
one time — `Cert III in EAL` runs twice on Mondays, so either stored value is
legitimate.

The replacement is matched on the **date** as well as the time, and the row is
replaced in place rather than re-expanded. Both halves of that are load-
bearing and both were defects:

- re-expanding per row *multiplied* them — three stored midnight copies of one
  weekly series became 36 rows;
- a time-only check authorised a **date change**, so a stored Friday for
  `"Fifth Friday of every month"` was deleted and seven other Fridays published.

It only ever corrects a row's **time**. A row whose *date* was derived wrongly
is not touched — which is why D6 exists as well: re-deriving from the source
re-derives the date too.

*Owner:* `recurrence.refresh_inferred`.

These four decisions — D6, D7, D8, D9 — are all consequences of one structural
choice, and the choice is not obviously right. See
[section 8](#8-the-largest-simplification-still-available).

---

## 3. Dates

### D10. Every published row carries a real date — **Hold**

Some sources publish recurring programmes with no per-occurrence date, stating
the pattern in prose instead ("Wednesdays. 2:00pm – 3:30pm"). Those are parsed
into a recurrence spec and expanded. `health_check.py` fails the build on a row
without a date.

Undateable listings are **removed**, with the reason printed — this drops 24/7
helplines, open-ended enrolments, one-off exhibitions with no dates, and sponsor
acknowledgements. None of them can go on a calendar.

*Owner:* `recurrence.build_spec`, `resolve_dateless`.

### D11. A source-supplied date always wins — **Hold**

A dateless stub whose name and location already have a real dated sibling is
dropped in favour of it. *Previously inferred* siblings do not count, so re-runs
stay stable.

The corollary: a fetch-time stamp is never a date. A row carrying a date but
flagged `has_real_date: false` is self-contradictory — older runs stamped such
rows with the fetch time, so the published date silently became the day the
pipeline ran.

### D12. A date without a weekday is a pattern; a weekday with a date is one session — **Hold**

The test is the presence of a date, not the plurality of the weekday.
`Tuesdays 9am` and `Tuesday 9am` both mean a weekly class; `Friday 2 October,
11:00am` is one event, and reading it as weekly would fabricate twelve.

A date that has just passed settles the listing rather than re-expanding it, so
a finished afternoon is not republished as twelve future ones.

### D13. At most 12 occurrences per source event — **Hold**

Earliest first, so a weekly class covers ~3 months and a monthly one ~1 year.
The cap is what stops one mis-parsed pattern from taking over the calendar — the
single most important safety property of the whole inference layer.

A stated `10 weeks` overrides the cap, counted in **weeks** and multiplied by
sessions per week, so a twice-weekly "6 weeks" course is 12 sessions. Where an
explicit date range is *also* given it wins and the week count is ignored,
because it is the more specific statement of the same thing — that is what
stops a "Weeks: 10" term running to 5 December losing its final Monday.

*Owner:* `recurrence.MAX_OCCURRENCES`, `expand`.

### D14. A month window belongs to one year — **Hold**

"from February to November" states months, never a year, so on its own the
window is satisfied again by the same months the following year. It is bound to
the year containing the day the listing is read.

Similarly, explicit ranges resolve to the current year and a window that has
already finished is **dropped rather than rolled forward** — 2023 workshop
write-ups and last year's terms must not reappear. A finished range settles the
listing even with no year: "from 1 June to 31 August" read in September is a
closed reading period, not next June.

A year-less date does not roll forward on a technicality either: rolling now
requires the current-year reading to be more than `YEAR_ROLL_GRACE_DAYS` (14)
days past. Within that window the listing is stale and is dropped.

### D15. A fortnightly series takes its phase from the start date — **Hold**

A fortnight divides the week in two, so anchoring the phase on "today" made the
published dates shift by a week every time the pipeline ran — a source that
changed its output on every build, for no reason. With a stated start the phase
is that start; without one, today remains the anchor and the series is still
weekly-shaped and reproducible.

A cadence may be stated as a property of the weekday rather than as a
repetition — `Friday (fortnightly) 10.30am-11.30am`,
`Monday 10.30am (every second week)`. Both are recognised, guarded on a single
named weekday, because the fortnight expansion walks one weekday per period.

### D16. Midnight means "time not stated", not 00:00 — **Hold, with caveat**

It is the pipeline's marker for a known date with no stated time, so the table
renders those as *all day*. A source that states a time in the small hours is
normalised to the marker rather than published as one.

**Caveat:** this overloads a real value. `00:00` is simultaneously "midnight",
"no time stated", and "date unknown to the fetcher". It is the reason
`refresh_inferred` had to be careful about *midnight copies* of time-stated
series, and the reason D18's second check exists. A sentinel that cannot be
mistaken for a time would remove a whole class of bug.

### D17. Nothing validates an inferred *date* except re-expansion — **Hold**

A wrong date is the worst defect this pipeline can ship: the row renders, it
looks bookable, and it is simply the wrong day.

The obvious check — compare each stored timestamp against the time its own text
states — is **self-consistent**, because it uses the same parser that produced
the value. Every date defect below reached a green build through it. What is
actually needed is a question the parser was not asked: *is this row's date
still one the text produces at all?* `health_check.py` re-expands each inferred
series and fails if a stored date is absent from the result. That is
independent of how the time was parsed, so it catches a wrong day, a wrong
phase, and a series that no longer exists.

| Defect | Published | Correct |
| --- | --- | --- |
| A month window with no year | 12 rows, 10 of them next season | 2 rows, this season |
| `Weeks: 10` counted as 10 sessions | 10 Mondays, last one 7 Dec | 11, ending 14 Dec as stated |
| Fortnightly phase anchored on "today" | different dates each run | same dates |
| A dated single session read as a pattern | 12 Tuesdays from one afternoon | 1 |
| A bare-hour range read end-first | `Tuesdays 6 – 8pm` at 20:00 | 18:00–20:00 |
| `First Tuesday each week` read as monthly | 12 dates 12 months apart | 12 consecutive Tuesdays |
| A booking deadline read as a session | a phantom 4pm class each day | 2 sessions |

`recurrence.py` asserts all of them on a fixed reference date, so expected
dates are literal rather than relative to whenever the suite runs.

### D18. A derived date stays labelled derived — **Hold**

Inferred rows are tagged `date_inferred: true` and carry a `recurrence` label
("Every Wednesday") so a reader can tell a stated date from a derived one. The
table renders that label under the timestamp, and it is carried into the `.ics`
export as `X-COMMENTS-DERIVED-DATE` and into the CSV as `DateInferred` and
`Recurrence` columns — so a derived date does not silently become a confirmed
one once it leaves the page.

The note is real text, not a `title` attribute: a title needs a hover, and the
fact that 32% of published dates were derived rather than published was
completely unreachable on a touch screen.

### D19. A bare second time inherits the weekday before it — **Hold**

`weekday_slots()` pairs each weekday with the times that follow it. This looks
like a parser bug and is not — 5 of the 6 sources that exercise it depend on it:

| Text | Slots | Correct? |
| --- | --- | --- |
| `Mondays. 9am - 12pm. 12:30pm - 3:30pm` | Mon 09:00, Mon 12:30 | two Monday sessions |
| `Tuesdays, Beginner: 10:00am–11:00am Social: 11:00am–12:00pm` | Tue 10:00, Tue 11:00 | two levels |
| `Mondays 10:30am - 11:30am \| Fridays 1pm - 2pm \| Fridays 2pm - 3pm` | Mon 10:30, Fri 13:00, Fri 14:00 | two Friday sessions |

The sixth case looked wrong — `STEADYstrength`, whose hall copy had genuinely
lost the word "Thursdays" — and turned out to be a stale snapshot rather than a
parser fault. Narrowing the carry-forward would have broken the four real
cases, so the snapshot is fixed by D6 instead.

**A bare `12` is not a standalone time token.** Schedule text is full of bare
numbers ("Monday 28 September") and matching digits alone turns day-of-month
into an hour. Every token is `\b`-anchored, so "afternoon" is never read as
"noon".

---

## 4. Places

### D20. Every event has an address, unless it is online — **Hold**

A published row has to say where to go. The address is what the map link, the
`.ics` `LOCATION` and the CSV export are built from, so a missing one ships an
event a reader cannot locate.

**The check exists for a *wrong* address, not a missing one** — nothing
downstream can detect a wrong address. `fetch_kingston_hubs()` used to fall back
to a generic `("Kingston Hubs", "Chelsea 3196")` for any `CalendarId` it had no
mapping for, and 300 rows from the *Patterson Lakes* calendar — 23% of the
calendar — published with a Chelsea address, with `extract_suburb()`
helpfully deriving `suburb: "Chelsea"` from it. The one warning that noticed
printed once per run and the build stayed green.

So the API supplies neither venue nor address, `calendar_venues` in
`sources.yaml` is the only place they come from, and a calendar with no entry
there is a **hard fetch failure**. A misconfigured source exits before
`raw_events.json` is written, leaving the previous good file intact. The
alternative — publishing a row with an empty address — only moves the problem
downstream: the run is green and the bad row is on the page.

**Blank is deliberately *not* online.** A missing venue is the defect, so
treating it as an exemption would hide exactly what the check is for. Nor is a
placeholder ("TBC", "To be confirmed") an exemption.

`venues.py` holds that one rule and both the fetcher and the verifier read it
from there, because if they disagreed the fetcher would keep publishing rows
the check then rejects.

*Owner:* `venues.is_online`, `venues.needs_address`, `health_check` address
check.

### D21. A source config holds the venue, never a guess — **Hold**

Every venue that is not stated by the source in a named field comes from
`sources.yaml`: `calendar_venues` for the Kingston Hubs API, `venues[].address`
for Chatty Cafe. Two listings needed judgement rather than a lookup, and both
choices are recorded in the config with their evidence:

- a month-long campaign page spanning five reserves carries no Location block
  at all, so the Granicus fetcher **drops** venue-less listings rather than
  inventing one;
- three archived programmes published a *suburb* in the venue field
  (`"Frankston, VIC"`), so the venues were taken from the venues' own sites and
  recorded explicitly.

### D22. `suburb_filter` is the single lever on catchment — **Hold**

`sources.yaml` configures `suburb_filter: [Springvale, Keysborough]` and the
suburb is only available from the **event detail page** — the listing card
carries a title, a date and a category, and no venue or address. That is why
the Greater Dandenong fetcher opens every event: the catchment is built on a
field the listing does not have.

Online events (`location: "Online"`) have no suburb and are always kept.

`health_check.py` reads the filter back from `sources.yaml`, so widening
`suburb_filter` is the only way to admit more, and the check catches a fetch
that has silently stopped resolving suburbs at all (rows falling back to the
generic `Greater Dandenong` location).

*Owner:* `fetch_greater_dandenong`, `_passes_suburb_filter`,
`build_site.extract_suburb`.

---

## 5. Classification and presentation

### D23. An event collects every type it matches — **Hold**

Rule-based classifier over 18 types, multi-tag: children/family is orthogonal
to market/musical, so a kids' market is both `Children & Families` and
`Market & Exhibition`. Title (plus any source-supplied category) and free-text
description are matched as a union, results are returned in `TYPES` order, and
`["Other"]` appears **iff** nothing matched — never alongside real tags.

Because tags compose rather than collapsing to one winner, **rule order is no
longer load-bearing for correctness**. Two orderings that used to matter are now
just history.

Patterns are anchored with `\b` wherever the unanchored form also matched
inside an unrelated word: `r"organ\b"` matched "Janis **Morg**an" and filed an
art workshop as music; `\barts?\b` matched "martial **art**" and filed an
aikido demonstration as sport.

*Owner:* `activity_types.classify_types`.

### D24. Three separate reasons a listing is not worth a reader's time — **Hold**

Two different questions, neither answered by the listing's own fields:

- **Can I still go?** Venues write the status *into* the listing rather than a
  field of it — Granicus puts the whole status sentence where the date goes
  (`"Sold out: Wednesday, 30 September 2026 | 11:00 AM to 12:00 PM"`), so the
  time is parsed out of a sentence that also says the event is unavailable.
  `event_status()` reads the leading status phrase, then the title, then the
  blurb. Sold out and fully booked rows stay **visible but badged**, because a
  listing you can no longer book is still worth knowing about. **Cancelled**
  rows are **hidden** — a cancelled event is worse than an absent one.
- **Is this an event at all?** A community centre's `Takeaway Meals` — "Take
  home delicious, nutritious meals for one. Available Tuesday to Friday,
  10am-2pm" — is a service with an opening window, not a session to attend, and
  the calendar was giving it four slots a week. `is_ongoing_service()` flags it.
  It is deliberately **not** `is_commercial`: the meals are subsidised, and
  calling a council service a "commercial pub/meal deal" would put a false fact
  in `events.json` and the CSV export.

`build_site.py` writes `hidden_by_default = is_commercial or is_service or
cancelled`, which is the **one flag the UI filters on**, so all three share the
single existing checkbox.

*Owner:* `status.event_status`, `is_ongoing_service`, `commercial.is_commercial`,
`build_site.hidden_by_default`.

### D25. One hand-written document, no framework, no build step — **Hold**

The page is a single HTML file with an inlined JSON payload, and it has to work
from `file://` with no server. That constraint is load-bearing, not incidental:
a `file://` origin is opaque, so `fetch('data/events.json')` is CORS-blocked in
every browser and the payload *must* be inlined.

It also rules out a framework. The most a framework would replace is ~22 KB of
vanilla JS, and it would cost a bundler, a lockfile, and a hydration model for
a table `render()` already rebuilds wholesale. The only things worth reaching
for are **platform** APIs — `Intl.DateTimeFormat`, and a real `<button>` inside
each `<th>` — not libraries.

*Owner:* `src/templates/index.html`, `build_site.py`.

### D26. Layout and accessibility are asserted, not hoped for — **Hold**

The page is hand-written, so nothing in the pipeline stops a CSS edit from
quietly breaking the phone layout. Invariants are asserted in two places:
`health_check.py` reads the stylesheet and markup (ARIA roles, a
`.visually-hidden` helper that actually clips, no `::before` content carrying a
field name, badge contrast, `rem` floors, 16px form controls, `:focus-visible`);
`render_check.py` measures the rendered DOM and the actual phone layout, because
neither the template text nor `--dump-dom` can see those.

Two notes for anyone extending it: CSS comments are stripped before any
stylesheet assertion (a comment mentioning `<select>` was enough to make the
font-size check report a control that does not exist), and assertions about
`font-size` require a **rem** length rather than a minimum, because a rem cannot
compound with a parent's font-size the way an `em` does — and a *missing*
declaration is a failure, since it inherits from that chain.

**The page must be rendered, not just inspected.** Every other check reads
Python or `events.json`; none of them execute JavaScript, so a syntax error in
the template builds cleanly, passes the health check, and publishes a **blank
calendar**. That is not hypothetical — a missing closing paren did exactly this,
and a regex scan for undefined names could not see it, because a parse error is
not an undefined name.

*Owner:* `health_check.a11y_errors`, `render_check`.

---

## 6. Page layout

These are all consequences of D25 and D26. Grouped because they share one
defect: every one of them was invisible to a text check and visible the moment
someone opened the built page in a browser.

### D27. Column widths come from `<colgroup>` with `table-layout:fixed` — **Hold**

Under auto layout the widest unbreakable value in a column decides that
column's width and no later rule can lower it. The price column was ~430px of a
~1240px table — a third of the viewport — because the cell was `nowrap` and one
row's `price_text` was `"Physiotherapy fees apply FIND OUT MORE BUTTON Find Out
More"`, 59 characters of page furniture a fetcher bug had put in a cost field.
Its own 90th percentile is 4 characters and 690 of 1,531 rows have no price at
all.

Fixed layout takes the widths from the `<colgroup>`, so content wraps and the
allocation is expressed in one place. The widths are ordered by how much text a
column carries and how much a reader needs it, not by how it happened to come
out:

| Column | Share | Why |
| --- | --- | --- |
| Event | 24% | most important field, p90 50 chars |
| Details | 20% | most verbose (p90 226, max 400) but least important to scan |
| Location | 16% | where to go; carries the address too |
| Date & time | 13% | the default sort key and what a reader scans for |
| Type | 10% | a filter facet, median 18 chars |
| Links | 10% | two fixed-width buttons |
| Price | 7% | rarely long, rarely important |

### D28. `.desc` and `.price` are clamped; width alone does not bound a row — **Hold**

A 400-character description in a 20% column is still seven lines, and a row is
as tall as its worst cell. Both clamps are released under 768px, where the card
layout has the full width and the reader wants the whole text. The full value
stays on `title` and in the `.ics` and the CSV.

### D29. Type is expressed in `rem` on both breakpoints, with a floor per element — **Hold**

Under 768px an `em` chain compounded `table .9em → td .82em → .desc .9em` down
to 10.6px on a phone. That chain was never fixed for the desktop table, which is
the wider and more common viewport, and the measured result was worse than the
phone:

| Element | Desktop (before) | Mobile |
| --- | --- | --- |
| description | 12.7px | 14px |
| address | 11.8px | 13px |
| recurrence chip | 11.8px | 13px |
| source / status badges | **10.8px** | 12px |
| Website / + Calendar | 12.2px | 14px |
| Reset filters | **11.8px** | 14px |

Every one is now `rem`, and `health_check.py` asserts each selector declares a
rem size of at least `0.75rem`. Requiring **rem** rather than a minimum is the
part that matters: a rem length cannot compound, so the check holds whatever a
future edit sets the parent to.

### D30. `.controls` is `position:static` on mobile — **Hold**

A sticky ~300px control column on a phone viewport occluded more than half the
screen, with the results scrolling underneath it.

### D31. The mobile thead is clipped, never `display:none` — **Hold**

`display:none` also removes the column names from the accessibility tree. The
sort headers stay `tabindex=0`, so a `matchMedia` handler drops them to `-1`
under the breakpoint and restores them above — a keyboard user never tabs into
an invisible control.

### D32. The mobile card layout sets explicit ARIA roles — **Hold**

Below 768px the card layout sets `display:block` on `table`/`tbody`/`tr`/`td`,
which strips the implicit ARIA roles browsers derive from `display`, and it used
to label each stacked cell with `td::before{content:attr(data-label)}`. Generated
content is absent from the accessibility tree, so a card that *looked* right
read as an unlabelled wall of values.

This is the most expensive decision in the file — ~80 lines of template plus
seven assertions exist because of this one line of CSS. It is still right: a
seven-column scrolling table at 375px is a worse reader experience.

### D33. Accessible names contain their visible text — **Hold**

A voice-control user has to be able to say what they can see, so the calendar
button is `aria-label="+ Calendar for <name>"`, not "Add to calendar". The
outbound arrow is `aria-hidden` so it is not read as "north east arrow".

### D34. The count line announces on a trailing timer, not per keystroke — **Hold**

The visible count was itself the `role="status"` live region, and the search box
re-renders 150ms after each keystroke, so a screen reader spoke the result count
once per character. The visible count is written immediately and a
visually-hidden twin is written on a 600ms trailing timer, only when the text
has changed.

### D35. Facet counts are whole-index totals, not per-filter — **Hold, with caveat**

`Exercise & Fitness (441)` is how many events carry that tag across the index,
not how many your other filters leave. Recomputing per pass means a second
filtered count for every facet on every keystroke. The panel hint says which it
is, and the suburb checkboxes carry counts too so the two groups behave alike.

**Caveat:** checkbox counts are per tag, so they sum to more than the event
total. That is correct given multi-tag classification (D23) but reads as a bug
to anyone who has not internalised it.

### D36. The `.ics` duration is a flat 60 minutes — **Hold, with caveat**

No end time exists in the data, and inventing one per event type would be a
guess. A guess published into someone's calendar is worse than an obviously
uniform default.

### D37. The Type column prints every tag — **Hold, with caveat**

178 rows carry three or more and the longest joined string is 71 characters, so
the column wraps to four or five lines. Truncating to a primary tag needs a real
notion of primary, which `classify_types()` does not have — and D23 removed the
thing that would have supplied one, by making rule order not load-bearing.

---

## 6a. Series identity

### D38. A recurring series carries a stable identity - **Hold**

`series_id_for()` is `sha1(source_id | normalised name | venue head)`, stamped on
every row a dateless listing is materialised into, and it is what `reconcile_store()`
judges a series row on: a set membership, not a timestamp comparison.

The alternative was a window. A weekly series publishes its next 12 occurrences
**from the run date**, so its stored rows are a snapshot of a rolling window.
The old test re-expanded each listing's prose with the *current* run date and
compared timestamps against the stored ones, which meant it was comparing rows
written on one date against an expansion computed on another. The window slid
forward every run, so reconciliation drifted with the calendar rather than with
any source: 97 rows reported unjustified eight days after a build, 130 after
fifteen, 382 after two months, and `health_check.py` fails the build on any
drop. The build was correct on the day it was committed and wrong the following
week, from no change in any input.

Keyed on the venue rather than the URL, because Chatty Cafe re-slugged a venue
page once and a URL key would have re-identified every series that venue owns on
the next run. Not folded for accents, because the id is per-source by
construction: it exists to recognise one listing's own occurrences, never to
merge two sources' listings, which is `dedupe.py`'s name/location match.

A source that publishes its own identity should use that instead: `webfetch_everi.py`
stamps the site's `eventIdentifier` GUID, which is authoritative where ours is
inferred. A row's own stamped value always wins over the computed one.

`series_id` does not replace D9. It answers *"is this listing still published?"*;
`refresh_inferred()` answers *"is this row's stored time still what the listing
says?"*. A Chatty Cafe venue that moves from Wednesday to Thursday keeps its
identity and still needs refreshing, so both remain.

### D39. `has_real_date` is removed, not repaired - **Hold**

The field was documented as "true only when the source supplied a date", and
`recurrence._row_with_date()` set it `True` on every row it *inferred*. It
therefore read `True` for all 1531 rows and discriminated nothing, while
`dedupe.py:688`'s self-contradiction check — the safety net for a real
fetch-time-stamp bug — could never fire on any row the pipeline produced.

`date_inferred` already separates the two cases and is what `resolve_dateless()`
and `refresh_inferred()` read, so the field was pure redundancy carrying a
misleading name. Removing it rather than fixing its meaning was the smaller
change: fixing it would have meant a data migration to restate 501 rows, to
protect a check that `date_inferred` performs anyway.

### D40. A group publishes only if it states both a place and a time - **Hold**

Kingston's community-groups directory lists 117 groups. A group is not an event:
it has a schedule, not a date. The listing states no time at all, so the crawl
opens every entry's own page, and that page is the only place a schedule is
written down. A group stating a place but no meeting time is dropped, and so is
one stating a time but no place.

Neither half is actionable on its own, and the alternative is a calendar row a
reader cannot act on -- the same reasoning as D20 and as
`webfetch_granicus.drop_venueless()`. Eleven of the 117 state no address
anywhere on the site, listing card or detail page, and are therefore not
published at all.

Where the schedule comes from, in order:

1. **The group's own prose.** Preferred, because it is the group describing its
   own meetings. "monthly meetings ... on the third Monday of each month
   starting at 10.00am" resolves to a monthly series on the third Monday at
   10:00.
2. **A per-weekday hours table**, present on 14 of the 117. This is *not* a
   weekly window but a list of individual sessions with no dates, so it is
   aggregated per weekday into the window the group is active in, and a day
   whose aggregate runs longer than six hours is refused as the venue's opening
   hours rather than a meeting. That test is what keeps the Australasian Golf
   Club's 07:00-18:00 daily table out of the calendar, and it is a judgement
   about what a group "having a schedule" means, which is why it is stated here
   rather than buried in the fetcher.

A schedule that names days but no start time is refused, and so is one whose
every occurrence is in the past -- the last of those caught a church whose
description mentions "Sunday 22 December", which the date parser resolved to
2024-12-22 and published into a 2026 calendar.

### D41. A group's tags come from the taxonomy it publishes - **Hold**

Kingston states a curated category on every directory entry -- `Probus`,
`Seniors`, `Sports and recreation`, `Men's sheds` -- and `SOURCE_TAXONOMY` maps
those fourteen terms onto `TYPES`.

This is preferred to matching the description, because the description is a
field the classification did badly. `r"probis"` matches only the plural, so
"Aspendale Probus Club" -- one of the most common group types in the directory,
because Kingston has three Probus clubs -- fell through to `["Other"]`. A
vintage car club came out as `Children & Families` + `Food & Drink` + `Info
Session` and a church as `Children & Families` + `Food & Drink`, from a mean of
2.9 tags per group. The prose regexes still run and still union on top, because
a group's subject and what its description mentions are different facts.

`Community Group` is keyed on the source id, as `Seniors Festival` is on
`kingston_seniors`, so a future directory of joinable groups inherits the tag
rather than needing its own wording.

### D42. "Inside the catchment" is one function, and it is the strict one - **Hold**

`suburb_filter` was implemented twice, and the two disagreed about exactly the
case that mattered. `fetch_urllib_sources._passes_suburb_filter()` ended
`return not _classifiable(row)`: a row whose suburb could not be extracted was
*kept*, on the reasoning that an extraction gap should not throw a real event
away. `health_check._suburb_in()` ended `return any(a in blob for a in
allowed)`: the same row was *rejected*, on the reasoning that an event which
cannot be placed is not in the catchment.

So the fetcher published "Mount Cannibal Hike and Barbeque" -- a reserve some
forty kilometres from Springvale, named after the place it is in -- and the
check then failed the build on it. Two modules, one invariant, opposite
answers, and a build that went red on a row a module had just created.

There is now one function, `fetch_urllib_sources._passes_suburb_filter()`, and
`health_check` calls it rather than carrying a copy. The strict reading wins:
an event the pipeline cannot place in the catchment is not published, because
the alternative is that the filter admits the whole city whenever the
detail-page suburb extraction misses. A row naming an in-area suburb anywhere in
its own text is still kept, since that is a positive signal and costs nothing,
and online events are exempt because they have no suburb to be outside of.

### D43. A weekday qualifier is not part of a programme's identity - **Hold**

Sources style one class differently: the Seniors guide writes "Zumba Gold
(Mondays)", the CCC site writes "Zumba® Gold". Those were 0.84 similar, below the
fuzzy threshold, so Monday's class published twice at the same hall, hour and
date.

`_NAME_NOISE_RE` already stripped a trailing "(new)", "(series)", "(term 4)".
The qualifier is now stripped in `normalize_name()` rather than only in
`name_head()`, because the fuzzy pass compares `name_similarity()`, which uses
`normalize_name()` -- stripping it in `name_head()` alone left the similarity
untouched and the duplicate standing.

Trademark marks are folded out for the same reason: `®`, `™` and `©` carry no
identifying information and one source's use of one should not create a second
row for a class.

Stripping the qualifier does merge "Zumba Gold (Mondays)" and "Zumba Gold
(Fridays)" as far as the *name* is concerned, which is safe because a merge also
requires the same day and the same venue: they are one programme on two days,
and the sessions stay distinct rows.

`dedupe.py` had no suite of its own, which is how it came to hold two readers of
one idea and a threshold that quietly excluded a class it was meant to catch.
It has one now, and `checks.py` passes `--test` to it — as it already did to
`build_site.py` — so running the suite does not re-run the merge.

---
### D44. "Archived" is a per-series fact, not a per-source one - **Hold**

`archived_events.json` is what is left of sources this project could not crawl,
and every row in it used to be withheld from the page. That treated a *source*
as live or dead when the truth is per-series: a source is a whole council's
listing page, and some of its programmes keep running on an organiser's own site
after the council stops listing them.

Checking them found ten that were still going -- the Bayside farmers' market
among them, on `baysidefarmersmarket.com.au`, 4th Saturday monthly -- and one
that was publishing a date that will not happen. The fixture described it as
"the fourth Saturday of every month", so `recurrence.py` materialised a 26
December session; the organiser's own site lists its 2026 dates and states
there is no December market. Checking the archive against the web is what
caught that, and it would have caught it for as long as the archive existed had
anyone looked.

So each fixture row now carries a `status` (`live` / `unverified` /
`finished`) and the owning site's `live_url`, and only `live` keeps a row on
the page. `unverified` is withheld too, which is the decision inside the
decision: an unconfirmed series is the one whose dates are most likely to have
moved, and a stale farmers' market is worse than no farmers' market. Withheld
is the reversible direction -- adding `status` is a one-word edit.

`archived` itself stays, meaning "this row came from the archive file", and it
is no longer conflated with "hide this row". Conflating them is what hid the
ten.

The lookup that decides is a series-id match, and it needed care: a stored
occurrence is stamped with its `series_id` at `materialise()` time, so its id
differs from the dateless fixture row it came from, and matching on the stamped
id alone finds nothing and silently withholds the entire file. Both keys are
tried.

None of that reaches the reader. The badge said "Bayside (archived)" and
"Frankston (archived)", which tells someone about how this project obtained a
listing rather than about the event, and offers a third state that does not
exist for them: an event on this page is running, and one that is not running
is not on this page. So the two Frankston and Bayside sources that arrive by
different internal routes read as the one publisher they are -- "Bayside",
"Frankston" -- and `archived` stays a fact about a row's origin, settled before
publication.

That is now asserted on the built artefact rather than left to review: a build
that renders the word "(archived)", "(delisted)" or "(archive)" as text fails.
The check is on `index.html` and not on the source because the source ids
`bayside_archived` and `frankston_archived` legitimately remain in the payload's
`source_id` and in the CSS class names -- only prose is the leak.

### D45. A crawl that cannot finish in one run must resume - **Hold**

`whatsonfrankston.com` refuses an IP that asks for too much, answering every
page -- homepage and sitemap included -- with HTTP 409, and holds it for hours.
The sitemap lists 856 occurrence pages. At the delay the host tolerates in
testing, that is the run that gets blocked; the delay was measured on a handful
of requests, not on nine hundred.

So `webfetch_everi.py` takes a bounded slice per run and caches both the pages
read and the rows they yielded, in a gitignored `*.progress.json`. No run is
ever the 856-request run.

Two things this had to get right, and both were wrong first:

* **The cached rows, not just the cached URLs.** With URLs alone, a finished
  crawl reads nothing on the next run, finds no rows, and raises -- so the
  source becomes permanently unfetchable having been successfully fetched once.
* **A blocked run keeps what it read.** Raising before the save discarded every
  page the run had managed, so the next run re-read the same thirty-one and was
  blocked at the same place. On a host allowing ~30 pages per session that is
  the difference between finishing in a few dozen runs and never finishing.

A partial crawl publishes nothing, and that is asserted in three places
because it had already leaked twice: the fetcher refuses it, `dedupe.py` skips
the progress file, and `dedupe.py` skips a snapshot whose source still has one.
The second guard exists because `dedupe.py` merges every `*.json` in the
snapshots directory, so it read the progress file directly and put sixteen rows
of a 1%-complete crawl on the page -- the fetcher's own guard never applied,
because the guard was in a different module.

`frankston_live` stays commented out until the crawl completes. Enabling it
before then turns every scheduled run red by design (D3), which is not a
signal anyone reads.

And the pause was on the wrong side of the request.

The delay lived at the bottom of the loop, as the last statement of a page that
parsed. Five `continue`s stood between the request and the sleep, and every one
of them is a page that was *fetched*: a failure, a page that parsed to nothing,
a page outside the horizon, a page with no address. Those are not the rare
paths -- the sitemap runs a year out against a 120-day horizon, so the majority
of its 856 pages take the horizon `continue` -- and measured on a run of pages
all outside the horizon, sixteen requests produced **zero** pauses. The crawl
was rate-limited precisely nowhere it spent its requests, which is a fair part
of why the host kept refusing it.

So pacing moved to `_Throttle`, immediately before each request, on every path,
and gained three properties a bare `sleep` cannot express:

* **A floor.** `max(configured, 0.35s)`, so no `crawl_delay` in a config can
  turn the rate limit off. Configurable upward, never down: a `crawl_delay: 0`
  line reads like a fix and is the opposite of one.
* **A budget** of 60 requests per run, sitemap fetches included. Those ten
  repeat at the head of every run, so uncounted they are a fifth of the crawl's
  total traffic spent before the first event.
* **A gap measured from the previous request**, not a fixed wait after each
  one. Sleeping a constant interval *after* a page makes the real rate a
  function of page size, which is the opposite of a rate limit.

The slice is now a request for progress and the budget is the limit on
pressure, and the fetcher caps one with the other -- so a mis-set `slice_size`
is no longer a 150-request run against a host that refuses one at 31.

Making the budget stop *publish* also closed a hole the block path had already
closed: a run that ran out of budget while holding rows used to return them,
correct only because `dedupe.py` separately skips any snapshot whose source has
a progress file. Both incomplete-crawl routes now raise `PartialFetch`, and the
hand-driven `--slice` loop reports that as progress rather than a traceback,
since it is the expected outcome of every run but the last.



### D46. A listing that names its *next* occurrence is one event — **Hold**

`'Kingston Sounds' by Susannah Langley` was on the page eight times. One
exhibition, one URL, one description — and eight different start times:
21:00 on 30 September, 11:00 and 12:00 on 1 October, then 08:00, 09:00, 14:00,
18:00 and 19:00 on 2 October. One row per pipeline run, and the store grew by
one every day. Six more listings were doing the same thing at the same rate:
`'Refugia' by Kerri Wilson McConchie`, `School holidays at Waves` and
`Mental Health Month` reached seven or eight rows each, and `'Hand me down' by
Andrew Duong`, `'Rendered Other and Cast Out' by Elizabeth Cole` and `Stitch
with Sappho` reached three — 33 rows in all.

The cause is one word on the page. Granicus writes the *next* occurrence, not
the start of the run:

```
Next date:  Friday, 02 October 2026 | 07:00 PM
       to Saturday, 31 October 2026 | 11:59 PM
```

That opening date is a cursor. It advances as the exhibition does, and the
closing date does not, so every fetch of a listing that runs for weeks returns
the same listing at a different start time.

Every check in this pipeline missed it, and each miss is load-bearing rather
than an oversight:

* `slot_hash` puts the start time in the key, so the eight rows were eight
  slots. That is correct — a venue can run the same class twice in a day, and
  collapsing on the date alone erases the second session.
* `dedupe_by_source_url` keys on the same `(name, url, stamp)`, and says so in
  its own docstring: it exists to merge *two sources* reporting one session.
* `health_check.py` asserts both of those keys, so the store was green.
* `reconcile_store()` D38's series test keeps any row whose
  `(source_id, name, venue)` is still published, and *deliberately* does not
  compare timestamps. All eight rows share one `series_id` — that is what it is
  — so the live row justified all eight.

The series test is right and stayed. What it cannot see is that these rows
carry real source-stated times which *disagree*, and a materialised series'
times disagree too — which is why the test ignores them.

So the first fix tried was the obvious one: a row the pipeline did not infer
stands for exactly one slot, so judge it against the sources' own slots. It
works, and it drops **175** rows, of which 129 are correct: `kingston_hubs`
materialises a directory entry into a dozen dated occurrences *without* tagging
them `date_inferred`, so its 58 stored `Mahjong` rows all look like eight
`Kingston Sounds` rows. It also reintroduces the exact failure D38 exists to
prevent — a listing publishes a rolling window, so an occurrence that has aged
out of the window fails the slot test and vanishes. D38 measured that at 97 rows
eight days after a build and 382 after two months.

What separates the two is that the listing *says* it is naming a cursor. This
cannot be read off the data — a listing re-dated per run and a listing that
genuinely runs every day are identical in name, URL, consecutive dates and time
of day. `Fairies at Rippon Lea` is a real run on 30 September to 4 October and
is stored as one row per day; `Holiday Activities` says `daily. 8:45am-4:15pm`
and is stored as three rows. Neither would survive a rule that read "several
rows of one listing, so they are duplicates".

The discriminator has to be the source's own label, and it is unambiguous: a
listing that writes `Next date:` is naming a cursor. So
`drop_superseded_listing_rows()` groups stored rows by `(name, url)`, keeps
only the rows carrying that label, and drops those the sources no longer state
as a slot.

The pass is narrow on purpose, and each narrowing is a case in `dedupe --test`:

* The label is read with a word boundary rather than anchored to the front,
  because the same field leads with the venue's status when there is one
  (`Sold out: Next date: ...`) — `status.py` reads that prefix rather than
  writing it, so a row can arrive with both.
* Inside such a group a row is only dropped when no live row states its slot,
  so a page that really does publish two sessions at two times keeps both.
* An absent source and a source mid-crawl are both spared, on D3's reasoning
  that silence is not a withdrawal. `crawling` is a parameter so that guard can
  be exercised without a progress file having to exist on disk.

The first version keyed the group on the *closing date* instead, on the theory
that a run's end is what identifies it. It was withdrawn: it is a strictly
narrower reading of the same thing (it cannot see `'Hand me down'`, whose text
ends in an end *time* rather than an end date), and standing alone it is
**wrong** — five `bayside_live` rows state two real date ranges on one page
(`28 September 2026 to 2 October 2026  29 September 2026 to 3 October 2026`)
and are genuine sessions, not readings of a cursor. The label subsumes it and
has no such counterexample, so the closing-date reader and its two regexes went
with it.

33 rows went, one per stale copy, and nothing was added or altered. 2184 rows,
zero duplicates. `_live_slot_index()` and `_slot_is_stated()` are shared with
`reconcile_store()` rather than written a second time, for the reason
`health_check.py` uses `dedupe.slot_hash()`: the pass that drops and the pass
that judges have to compute the same thing, or one of them reports a duplicate
the other considers distinct and neither names the disagreement.

*Owner:* `dedupe.drop_superseded_listing_rows`, `dedupe.names_next_occurrence`.

### D47. A paginated listing is walked, at a rate the host sets — **Hold**

`kingston_council` read exactly ten events for as long as it existed, and every
check was green. The Granicus listing is **301 events over 31 pages**, sorted by
next occurrence, and page 1 is the next ten of them. Nothing failed, because
reading page 1 of a 31-page listing is a perfectly good fetch — it is just 3% of
the calendar, and no invariant in this pipeline has an opinion about the other 97%.

The events did not vanish. They rotated off the front. `Chinese Senior Citizens
Club of Kingston` sat on **page 18** and `Tea & Talk Chinese Conversation Table`
on **page 27**; both stayed published on the council's own site the entire time
and both still answer `HTTP 200` with their full description and their next
occurrence. They were absent from every snapshot from the moment their date
passed the ten-event window. `Chinese Senior Citizens Club` was captured in
`59e4bb9` and lost in `70b2678` — a listing rotating, correctly, under a fetch
that could only ever see the front of it.

The note in `sources.yaml` said the pager needs JavaScript. It does not:

```html
<select name="ctl10$ctl00$ctl07"><option value="2">2</option>…</select>
<input type="submit" name="ctl10$ctl00$ctl08" value="Go" class="btn_scPagingNonJS_enabled">
```

Those are the non-JS controls, named so by the platform. A POST carrying the
form's hidden state and `ctl10$ctl00$ctl07=18` returns page 18. The
`webfetch_directory` listing has been walked this way for the whole life of the
project; the council listing never was. So the walk is `webfetch_http.paged_listing`,
shared by both, and the diagnosis in the config was wrong rather than
unfortunate.

**The rate is the host's decision, not the fetcher's.** `crawl_delay: 5.0` — one
request per five seconds, across the 31 listing pages *and* all 301 detail pages,
because that is the only reading of the number which bounds pressure on the site.
It is applied immediately before every request rather than after, on the strength
of D45: a delay on the wrong branch is a delay on the branch that most needs it,
and a Granicus fetch whose detail pages fail is exactly the run that would
otherwise hammer hardest. The floor is the shared `MIN_CRAWL_DELAY`, so a
`crawl_delay: 0` in the config cannot switch the limit off.

That is ~28 minutes of deliberate waiting, which is why `max_pages` is a *budget*
and not a target: `40` against a claimed 31, so a month that grows the listing is
followed rather than truncated at a fixed number. Reaching the budget is a
**warning naming the pages not read**, because it is an operator's choice. A page
that fails to load, a pager that will not advance, and a listing that stopped
stating how deep it is are all `PartialFetch` — those are a broken fetch, and
publishing page 1 in their place is the defect this whole entry is about.

The walk is opt-in per source. `kingston_arts` has no `max_pages`, so it still
reads one page and paces nothing: the listing is one page deep and should not pay
for a walk it does not need.

Two things the walk brought with it, both of which were invisible while it was
ten rows:

* **31 listings state their address as the literal words "Multiple locations"**
  — a library storytime at three branches, a road-safety program wherever it is
  booked. `drop_venueless()` asked only whether the field was blank, and a
  non-empty string satisfied both it and the health check's "every non-online row
  has an address". The test is now "states a place" — a postcode or a street line
  — and it runs after the detail pass, which is what its own docstring already
  claimed and the code never did. They are dropped, as the campaign pages were.
* **`detail_cap` must cover what the walk finds.** A listing card states a date
  and no time, so an unenriched row keeps midnight and renders as *all day* — a
  10am–3pm club publishing as all-day is the same class of quietly wrong row as
  the missing pages. `detail_cap: 320` over 301 rows, and a starved cap now warns
  with the count it did not open rather than leaving it to be inferred.

*Owner:* `webfetch_http.paged_listing`, `webfetch_http.Pacer`,
`webfetch_granicus.drop_venueless`, `sources.yaml` (`kingston_council`).

### D48. A fortnight's phase comes from the text, never from the run date - **Hold**

Found while verifying D47, and not caused by it: the store committed on Friday
2 October failed `health_check.py` on Saturday the 3rd. Eleven rows read
*"an inferred row sits on a date its own text does not produce"*.

`recurrence.py` anchored a fortnightly series on `today`, with a comment
conceding that this was *"the best available anchor"*. It is not. A fortnight
divides the week in two, so **which** fortnight comes first depends on the
weekday of the run. A Chatty Cafe venue states only `Friday (fortnightly)
10.30am-11.30am`, so:

| run date | weekday | first occurrence inferred |
| --- | --- | --- |
| Fri 2 Oct | Friday | 2, 16, 30 October |
| Sat 3 Oct | Saturday | 9, 23 October |

The same text, a whole period apart. The existing fix for this class of bug —
anchoring on the series' stated `start_date` — covered only the case where a
start date exists, and there is a pinned test for exactly that case. The
dateless case was left, and it is the common one.

The accumulated damage is worse than a phase flip. The store is append-only, so
both readings landed and merged: the 2nd, 9th, 16th, 23rd, 30th of October, every
Friday thereafter. **A class that meets every second Friday was publishing as a
weekly one — 23 sessions where there are 12.** Every one of them had a
`date_inferred` flag, a plausible time, and a passing duplicate check.

Two changes, and the second only matters because of the first:

* **The phase is ISO week parity against a fixed epoch** (`_PHASE_EPOCH`, a
  Monday in 2024) when the text states no start date. Arbitrary but stable,
  and arbitrary is the best available: the source does not say which fortnight
  it means, so no choice is more *correct* — it only has to be the same choice
  every run. A stated `start_date` still wins, because that is the venue's own
  phase and it is authoritative.
* **An inferred row is withdrawn when its own text no longer produces its
  date.** `refresh_inferred()` previously re-derived only a row whose *time*
  disagreed, and then required the replacement to contain the row's own stored
  date — a deliberate guard against silently moving a row to another day, and
  correct for the case it was written for. But an inferred date is not an
  independent fact about the world, it is what the text yields. A date the text
  cannot produce is not a session that moved; it is a session that cannot exist,
  and keeping it publishes a date the source never stated. Only dates from
  `today` onwards are tested, because an expansion runs forward from `today`
  and past rows are `prune_old`'s business at 90 days.

Eleven rows withdrawn, the class back to 12 fortnightly sessions, and a second
`dedupe.py` run withdraws 0.

*Owner:* `recurrence._PHASE_EPOCH`, the fortnightly branch of
`recurrence.expand`, `recurrence.refresh_inferred`.

## 7. Things that are not decisions, but look like they were

Noted here because each has cost real effort and will cost more if it is
mistaken for load-bearing.

| Thing | What it actually is |
| --- | --- |
| `bayside_seniors` | 74 fetched rows, **0 published**. Every event URL is byte-identical to one in `bayside_auto.json`. It cost a source entry, 7 listing pages and up to 90 detail fetches per run, and was invisible to the health check because seasonal sources are warn-only. **Removed.** |
| `MIN_TOTAL = 700` | The real index is 1,531 rows, so this can only fire after a 54% collapse — and `MIN_SOURCE` localises better. The per-source floors of 3 do the real work — except `kingston_council`'s, which was 3 against a ten-row page and so could not see the pager regress at all (D47). |
| `chatty_cafe` venue schedules | `sources.yaml` states a bare cadence (`Friday (fortnightly) 10.30am-11.30am`) for 21 venues, so the schedule is config rather than a reading of the venue's page. That is the right side of D21 to err on, but it means these rows depend on the recurrence expansion being stable across run dates — which it was not, for two reasons at once (D48). The config's `schedules` are the least-checked strings in the pipeline. |
| Horizon constants in `recurrence.py` | `WEEKLY/FORTNIGHTLY/MONTHLY_HORIZON_*` are all slack above the 12-occurrence cap, which always truncates first. They never decide an outcome; they only stop a runaway loop on a malformed spec. Kept, with a comment saying so — removing them would put a loop bound in charge of the last occurrence. |
| Archived sources | `frankston_archived`/`bayside_archived` carry no date of their own, so `prune_old` never touches them — they re-derive *forward* indefinitely and will keep publishing. A deliberate trade, not an oversight: the live pages are WAF-blocked. |
| The seniors festival | 10% of the calendar for one month of the year, and two files that must be re-synced by hand every October. Kept: it is a real event that is genuinely on, and the alternatives are a thinner calendar or a hand-written scraper for the PDF. |
| A 0-row seasonal source | Warn-only by design, but it also means a genuinely dead source in that set never fails. The floor set and the warn set need to be read together, not separately. |
| `datetime_display`, `date_text`, `source_label` | Three fields that were written on every row and read by nothing. `datetime_display` was a formatted copy of `datetime_iso`, which the page recomputes; `source_label` was `source_id` under a second name. **Removed**; `_normalize_raw` strips them at load so a snapshot committed before the change does not keep carrying them. (`source_id` is *not* in this list — it is the one field every reader keys on.) |
| `type:` on the plain sources | Was never read — `fetch_events.py` dispatched on `id` — while the *same key* was load-bearing on the webfetch half, meaning different things in one file. Now the single dispatch key for all nine sources. |
| `pip install .` | Did not work. `pyproject.toml` declared `build-backend = "setuptools.backends._legacy:_Backend"`, which is not a real backend, so the pinned dependencies had never been installable from the file that declared them and CI carried a second copy of the list. Fixed to `setuptools.build_meta` with `py-modules = []`, and CI now reads the pins. |

## 8. The largest simplification still available

Flagged rather than taken, because the blast radius is bigger than the rest of
this document put together. Verdict: **Questionable**.

**The append-only merge.** D6, D7, D8 and D9 are four mechanisms holding one
invariant: *the store is re-derivable from its sources*. The root cause is that
merging is append-only, so every correction arrives as a *new* row and every
removal has to be undone afterwards by a later pass. Each mechanism is
individually well-reasoned and each is also partly a workaround for the one
before it.

A replace-per-source-key merge — for each source, diff its rows against what the
store records for that source and replace the difference — would make D8 and D9
unnecessary and would make D6 a consequence rather than a pass. It also shrinks
the schema, because a row that exists because a source justified it needs no
bookkeeping to prove it.
