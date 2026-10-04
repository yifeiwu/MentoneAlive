"""The one reader of scripts/sources.yaml.

Three modules needed this file and each grew its own `yaml.safe_load`: the
fetcher dispatch, the snapshot verifier in `dedupe.py`, and the archived-coverage
report. They did not agree on what happens when it is missing -- the fetcher let
the error propagate, `dedupe.py` warned and carried on with a toothless check,
the report let it propagate -- so "which sources are configured" had three
answers depending on which module you asked, and only one of them was reachable
from a test.

Deliberately dependent on nothing but `yaml`. This sits *below* the fetchers
rather than importing them: `fetch_sources.py` pulls in the HTTP layer, the pacer
and every source module, and `dedupe.py` must not pay for that to learn which
ids exist. Adding an import from here into a fetcher would put the cycle back.

    from config import load_config, source_ids
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "scripts" / "sources.yaml"


def read_config():
    """The file as parsed. Returns {} for an empty document; raises otherwise.

    Raises OSError if the file is missing and yaml.YAMLError if it is malformed,
    which is what the fetcher and the coverage report already relied on.
    """
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config():
    """Every source entry, tagged with which config list it came from.

    `group` decides whether an entry owns a committed snapshot file, and it is
    tagged here rather than in sources.yaml so the file stays a description of
    the sources rather than of this script's internals.
    """
    config = read_config()
    return ([dict(c, group="snapshot") for c in config.get("webfetch") or []]
            + [dict(c, group="shared") for c in config.get("sources") or []])


def source_ids(quiet=False):
    """Every `id:` the config declares, or None if the file could not be read.

    None rather than an empty set on failure, deliberately: the caller here uses
    this to decide whether a snapshot file belongs to a source that still exists,
    and an empty set would read as "every snapshot is stale" and quietly delete
    all of them. dedupe.py skips the snapshot walk instead, and says so.

    `quiet` suppresses the warning for callers that already report their own.
    """
    try:
        entries = load_config()
    except (OSError, ValueError, yaml.YAMLError, AttributeError):
        if not quiet:
            print("WARNING: sources.yaml unreadable; configured source ids "
                  "cannot be verified")
        return None
    return {e["id"] for e in entries if e.get("id")}