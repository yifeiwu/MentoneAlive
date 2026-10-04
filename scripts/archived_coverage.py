"""Is every archived row still covered by the live source that replaced it?

    python scripts/archived_coverage.py

Prints, for each archived source, which of its rows a live feed still publishes.

Retiring an archived label is a one-line deletion with no downstream undo. The
calendar is a rolling window (dedupe.PRUNE_DAYS), so a row no live source
carries stops being *listable* either way; the question is whether it stops
deliberately or by accident. Everything printed as MISSING is the second kind.

Note what this is not. Since archived rows are withheld from the page
(build_site.py), "series no longer in the live feed" is already the correct
state for a delisted programme -- it is a record, not a listing. This reports
what the *sources* do, not what the store holds; quality_audit.py reports the
store. A series can be delisted and still be in data/events.json, and should
be.

Matching is on (normalised series name, calendar date), because that is what
"the same session" means. The description is not used: it is rewritten between
seasons. A row with no date is counted separately as UNDATED, since it stands
for a whole recurring term and cannot be matched date-for-date -- it is either
covered by series name alone or it is not covered at all, and this reports
which.

Deliberately not a check, for the same reason quality_audit.py is not: the
answer changes as the sources change, and the interesting case is the one where
a series has been discontinued council-wide. Failing a build on that would mean
arguing with the number instead of with the decision.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import read_config as _read_config  # noqa: E402
from dedupe import name_head  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOTS = ROOT / "scripts" / "webfetch_snapshots"
ARCHIVED = ROOT / "scripts" / "archived_events.json"

# Which live source is meant to replace which archived one. An archived source
# with no entry here is one nothing is expected to replace, and its rows are
# the only record that event existed.
REPLACED_BY = {
    "frankston_archived": "frankston_live",
    "bayside_archived": "bayside_live",
    "ccc_archived": "ccc",
}


def _dates(row):
    """Every calendar date this row stands for."""
    out = set()
    raw = row.get("datetime_iso") or ""
    if "T" in raw:
        out.add(raw[:10])
    for m in re.finditer(r"\d{4}-\d{2}-\d{2}", row.get("datetime_text") or ""):
        out.add(m.group(0))
    return out


def _series(row):
    return name_head(row.get("name") or "")


def _load(name):
    p = SNAPSHOTS / name
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data.get("rows", [])


def _snapshot_of(live_id, cfg):
    """The live source's snapshot filename, read from the config.

    Not "<id>.json": the live sources do not all follow that convention
    (bayside_live writes bayside_auto.json), and assuming they did made this
    script report "no coverage" for a source that does have a snapshot.
    """
    for entry in (cfg.get("webfetch") or []):
        if entry.get("id") == live_id and entry.get("snapshot"):
            return entry["snapshot"]
    return None


def coverage():
    cfg = _read_config()
    archived = json.loads(ARCHIVED.read_text(encoding="utf-8"))
    if isinstance(archived, dict):
        archived = archived.get("rows", [])

    by_source = {}
    for row in archived:
        by_source.setdefault(row.get("source_id") or "?", []).append(row)

    report = {}
    for src, rows in sorted(by_source.items()):
        live_id = REPLACED_BY.get(src)
        snap = _snapshot_of(live_id, cfg) if live_id else None
        live = _load(snap) if snap else None
        entry = {"rows": len(rows), "replaced_by": live_id,
                 "snapshot": snap, "covered": [], "missing": [],
                 "undated": [], "undated_covered": []}
        if live is None:
            entry["note"] = (
                "no live snapshot: %s" % (
                    "%s has no snapshot entry" % live_id if live_id
                    else "no replacement declared"))
            report[src] = entry
            continue

        live_keys, live_series = set(), set()
        for r in live:
            live_series.add(_series(r))
            for d in _dates(r):
                live_keys.add((_series(r), d))

        for r in rows:
            series, dates = _series(r), _dates(r)
            if not dates:
                # No date: covered if the live calendar still runs the series.
                (entry["undated_covered"] if series in live_series
                 else entry["undated"]).append(r)
            elif any((series, d) in live_keys for d in dates):
                entry["covered"].append(r)
            else:
                entry["missing"].append(r)
        report[src] = entry
    return report


def main():
    print("archived coverage\n")
    rep = coverage()
    for src, e in sorted(rep.items()):
        print("%-22s %3d rows  ->  %s" % (src, e["rows"],
                                          e["replaced_by"] or "(no replacement)"))
        if e.get("note"):
            print("    %s" % e["note"])
            print("    rows that would stop being published: %d" % e["rows"])
            print()
            continue
        print("    covered: %d    undated+covered: %d    undated+gone: %d    "
              "MISSING: %d"
              % (len(e["covered"]), len(e["undated_covered"]),
                 len(e["undated"]), len(e["missing"])))
        for r in e["missing"][:12]:
            print("      MISSING  %-50s %s" % ((r.get("name") or "")[:50],
                                                r.get("datetime_text") or ""))
        for r in e["undated"][:12]:
            print("      UNDATED  %-50s series no longer in the live feed"
                  % ((r.get("name") or "")[:50]))
        if len(e["missing"]) + len(e["undated"]) > 12:
            print("      ... and %d more"
                  % (len(e["missing"]) + len(e["undated"]) - 12))
        print()


if __name__ == "__main__":
    main()
