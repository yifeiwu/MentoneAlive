# Snapshots

Auto-refreshed by `python scripts/fetch_sources.py`, which runs in GHA before
`dedupe.py`. Do not edit these by hand.

- `kingston_council.json` — Granicus page 1 + details
- `kingston_arts.json` — Granicus page 1 + details
- `bayside_auto.json` — all `?page=N` + details
- `kingston_seniors.json` — Seniors Festival PDF guide + overrides
- `ccc.json` — Cheltenham Community Centre term classes + Humanitix dates
- `kingston_groups.json` — OpenCities directory, every entry's own page
- `frankston_auto.json` — What's On Frankston sitemap, one page per occurrence

`kingston_seniors.json` is produced from the annual Seniors Festival Event Guide
PDF (see `pdf_url` in `sources.yaml`) plus hand-checked session data in
`scripts/seniors_festival_overrides.json`, for the multi-column spreads whose
shared date rows lose column association in flat-text extraction. The overrides
go stale each October with the new guide — refresh the values then.

Manual overflow snapshots (Granicus deeper pages, libraries) go here as
`*_manual.json` and are merged automatically. `dedupe.py` merges every `*.json`
in this directory plus `scripts/archived_events.json`.

Fetcher code lives in `scripts/webfetch_{http,bayside,granicus,ccc,seniors}.py`,
orchestrated by `scripts/fetch_sources.py`.

Row schema matches `webfetch_http.make_row`: `name`, `datetime_text`,
`datetime_iso` (ISO or null), `location`, `address`, `price_text`,
`description`, `source`, `source_id`, `source_label`. Two sources add one
field each: `kingston_groups` adds `source_types` (the council's own category
terms, verbatim), and `frankston_live` stamps the site's own `eventIdentifier`
GUID into `series_id` rather than deriving one.
`price_sort`.
`kingston_groups.json` holds one row per surviving group, not one per meeting. A
group that states its schedule in its own prose stays dateless and is expanded
downstream by `recurrence.py`; one whose schedule exists only as a per-weekday
hours table is materialised here, because the table is the only statement of
the schedule there is. A group stating neither is dropped rather than published
without a place or a time.
