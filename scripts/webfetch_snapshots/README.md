# Snapshots

Auto-refreshed by `python scripts/fetch_sources.py`, which runs in GHA before
`dedupe.py`. Do not edit these by hand.

- `kingston_council.json` — Granicus page 1 + details
- `kingston_arts.json` — Granicus page 1 + details
- `bayside_auto.json` — all `?page=N` + details
- `kingston_seniors.json` — Seniors Festival PDF guide + overrides
- `ccc.json` — Cheltenham Community Centre term classes + Humanitix dates

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
`description`, `source`, `source_id`, `source_label`, `has_real_date`,
`price_sort`.