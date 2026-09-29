"""Shared filesystem helpers for the pipeline.

All pipeline writers go through write_json() so an exception mid-serialisation
can never truncate a committed data file. A truncated data/events.json makes
the *next* dedupe.py run abort on JSONDecodeError, which wedges the pipeline
until someone deletes the file by hand.
"""
import json
import os
import tempfile
from pathlib import Path


def write_json(path, obj, indent=1):
    """Atomically write obj as JSON to path (write to temp, then rename)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent),
                               prefix=target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=indent)
        os.replace(tmp, target)
    except BaseException:
        # Never leave a stray temp file behind on failure.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path, default=None):
    """Read JSON, returning `default` when the file is absent.

    Raises json.JSONDecodeError on a corrupt file: that is a real fault the
    operator must see, not something to silently paper over.
    """
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
