# Snapshots

Auto-refreshed by `python scripts/fetch_sources.py`, which runs in GHA before
`dedupe.py`. Do not edit these by hand.

- `kingston_council.json` — Granicus page 1 + details
- `kingston_arts.json` — Granicus page 1 + details
- `bayside_auto.json` — all `?page=N` + details
- `kingston_seniors.json` — Seniors Festival PDF guide + overrides
- `ccc.json` — Cheltenham Community Centre term classes + Humanitix dates
- `kingston_groups.json` — OpenCities directory, every entry's own page

`frankston_auto.json` is listed nowhere above because it does not exist yet: the
`frankston_live` config entry is commented out until its first crawl completes.
The crawl is resumable (a slice per run, progress in `*.progress.json`) — see
the note on that config entry for how to run it to completion.

`kingston_seniors.json` is produced from the annual Seniors Festival Event Guide
PDF (see `pdf_url` in `sources.yaml`) plus hand-checked session data in
`scripts/seniors_festival_overrides.json`, for the multi-column spreads whose
shared date rows lose column association in flat-text extraction. The overrides
go stale each October with the new guide — refresh the values then.

Manual overflow snapshots (Granicus deeper pages, libraries) go here as
`*_manual.json` and are merged automatically. `dedupe.py` merges every `*.json`
in this directory plus `scripts/archived_events.json`.

- `frankston_libraries.json` — Frankston City Libraries, ten programmes
  expanded to every date each one's own page states

`*.progress.json` is crawl bookkeeping, not source data, and is gitignored. It
records which occurrence pages a resumable crawl has read and the rows they
yielded, so a crawl split across runs neither re-reads a page nor republishes
nothing once it is finished. `dedupe.py` skips these files, and skips a
snapshot whose source still has one — a partial crawl publishes nothing.

Fetcher code lives in
`scripts/webfetch_{http,bayside,granicus,ccc,seniors,directory,everi,frankston_libraries}.py`,
orchestrated by `scripts/fetch_sources.py`.

Row schema matches `webfetch_http.make_row`: `name`, `datetime_text`,
`datetime_iso` (ISO or null), `location`, `address`, `price_text`,
`price_sort`, `description`, `source`, `source_id`. Two sources add one field
each: `kingston_groups` adds `source_types` (the council's own category terms,
verbatim), and `frankston_live` would stamp the site's own `eventIdentifier`
GUID into `series_id` rather than deriving one.
`kingston_groups.json` holds one row per surviving group, not one per meeting. A
group that states its schedule in its own prose stays dateless and is expanded
downstream by `recurrence.py`; one whose schedule exists only as a per-weekday
hours table is materialised here, because the table is the only statement of
the schedule there is. A group stating neither is dropped rather than published
without a place or a time.
