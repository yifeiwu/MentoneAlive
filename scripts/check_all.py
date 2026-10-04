"""Run the whole pipeline in the order CI runs it, and stop at the first failure.

    python scripts/check_all.py                # everything, in CI order
    python scripts/check_all.py --list
    python scripts/check_all.py dedupe build_site    # just these, still in order

The order is not alphabetical and not the order that reads best; it is the order
in which each stage's output is the next stage's input. `checks.py` runs first
because the rules are cheap and a broken rule should be reported before the
network is touched at all. `fetch_sources.py` then rebuilds every snapshot, and
everything after it reads what that produced.

Why this exists at all, given CI already runs the same six commands: the six
commands are the pipeline, and nothing in the repo said so. The order lived in
`.github/workflows/update-events.yml`, which is the wrong place to look when
you want to reproduce a green run locally, and which a contributor reading it
has no reason to believe is the canonical order rather than one schedule's
arrangement. This is that list, in Python, next to the stages.

It writes: `fetch_sources.py` rewrites the snapshots and `data/raw_events.json`,
`dedupe.py` rewrites `data/events.json`, `build_site.py` rewrites `index.html`.
That is the point -- the same commands CI runs, in the same order, so a local
green means what a CI green means. Use `checks.py` alone for the rules with no
network and no writes.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (stage, what it does, writes something). The middle column is what `--list`
# prints; it is the sentence you want when a stage fails and you are deciding
# whether the next one is worth running.
STAGES = [
    ("checks", "the rule suites, no network and no writes", False),
    ("fetch_sources", "re-crawl every source into its snapshot", True),
    ("dedupe", "merge the snapshots into data/events.json", True),
    ("build_site", "classify rows and rebuild index.html", True),
    ("health_check", "fail loudly on bad output", False),
    ("render_check", "render the built page and measure it", False),
]


def stage_note(name):
    for stage, note, _ in STAGES:
        if stage == name:
            return note
    return ""


def run_stage(name):
    """Run one stage, streaming its output. Returns (ok, seconds)."""
    started = time.monotonic()
    print(f"\n=== {name}: {stage_note(name)}", flush=True)
    proc = subprocess.run([sys.executable, f"scripts/{name}.py"], cwd=ROOT)
    elapsed = time.monotonic() - started
    return proc.returncode == 0, elapsed


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the pipeline in CI order, stopping at the first failure.")
    parser.add_argument("stages", nargs="*",
                        help="stage names to run, in the order given here")
    parser.add_argument("--list", action="store_true",
                        help="list the stages and exit")
    args = parser.parse_args(argv)

    if args.list:
        print("pipeline:")
        for name, note, writes in STAGES:
            print(f"  {name:16s} {note}{'  [writes]' if writes else ''}")
        return 0

    selected = [s for s in args.stages if not s.startswith("-")]
    unknown = set(selected) - {name for name, _, _ in STAGES}
    if unknown:
        print(f"no such stage: {', '.join(sorted(unknown))}", file=sys.stderr)
        print(f"known: {', '.join(name for name, _, _ in STAGES)}",
              file=sys.stderr)
        return 2

    # Named stages keep the canonical order rather than the order they were typed,
    # because a caller who lists `build_site dedupe` has made a mistake, not a
    # request to build from last run's store.
    todo = [s for s in STAGES if not selected or s[0] in selected]

    done = []
    for name, _, _ in todo:
        ok, elapsed = run_stage(name)
        if not ok:
            print(f"\nFAIL: {name} exited non-zero after {elapsed:.0f}s. "
                  f"Stopped; {len(todo) - len(done) - 1} later stage(s) not run.")
            print(f"Earlier this run: {', '.join(done) if done else '(none)'}")
            return 1
        done.append(name)
        print(f"--- {name} ok ({elapsed:.0f}s)", flush=True)

    print(f"\ncheck_all ok: {len(done)} stage(s) -- {', '.join(done)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())