"""The assertion harness every rule suite shares, and the runner for all of them.

Each rule in this pipeline -- the date parser, the classifier, the status
detector, the failure signals -- ships as a module with an `__main__` block full
of cases. That is deliberate: the rules live next to the cases that pin them, so
a rule and its evidence cannot drift apart, and the module runs standalone.

What was not deliberate is that eight of those blocks also hand-rolled the same
seven-line `check()` helper and the same print-and-exit tail, in six slightly
different formats. So:

    python scripts/checks.py               # every suite, non-zero on any failure
    python scripts/checks.py recurrence    # one suite
    python scripts/checks.py --list

The suites stay where they are. This only owns the boilerplate.
"""
import runpy
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUITE_DIR = ROOT / "scripts"

# Each suite is a module in scripts/ whose __main__ block asserts its rules and
# exits non-zero on failure. Order is only for readable output.
SUITES = (
    ("activity_types", "56 name/description -> tags cases"),
    ("status", "10 sold-out / service cases"),
    ("recurrence", "the date-inference table, on a fixed reference date"),
    ("commercial", "commercial detection rules"),
    ("webfetch_granicus", "Granicus address parsing"),
    ("webfetch_http", "shared time/month/row parsing"),
    ("failure_signals", "config validation and the do-not-publish signal"),
    ("fetcher_equivalence", "the fetchers extract the same rows they used to"),
)


def check(label, actual, expected, failures):
    """Assert one equality, print one line, and record a failure.

    `failures` is passed in rather than kept as module state so a suite that
    imports this can hold its own list; there is no global to reset between
    suites, which was the other thing the copies got wrong.
    """
    ok = actual == expected
    print(f"  {'ok  ' if ok else 'FAIL'} {label}"
          + ("" if ok else f"\n         actual:   {actual!r}"
                          f"\n         expected: {expected!r}"))
    if not ok:
        failures.append(label)
    return ok


def run_suite(name, note):
    """Run one suite as a subprocess and report whether it passed.

    A subprocess rather than an import, because each suite runs its cases at
    module scope under `if __name__ == "__main__"`, and because a suite that
    imports cleanly and then raises should not take the runner down with it.
    """
    print(f"== {name}: {note}")
    proc = subprocess.run([sys.executable, f"scripts/{name}.py"],
                          cwd=ROOT, capture_output=True, text=True)
    out = (proc.stdout or "").rstrip()
    shown = 0
    for line in out.split("\n"):
        # Suites already print their own per-case lines; echo them so one runner
        # shows the same detail as running the suite directly. The prefix test
        # is on the stripped line, because suites that adopted checks.check()
        # indent their output while the older ones do not.
        st = line.strip()
        if st.startswith(("ok", "FAIL", "all ", "fetcher_equivalence:")):
            print("  " + st)
            shown += 1
    if proc.returncode != 0:
        print(f"  FAIL: {name} exited {proc.returncode}")
        for stream in (proc.stdout, proc.stderr):
            for line in (stream or "").strip().split("\n")[-30:]:
                if line and line.strip() not in ("",) and not line.strip().startswith(
                        ("ok", "all ")):
                    print("      " + line.strip())
        return False
    return True


def main(argv):
    if "--list" in argv:
        print("suites:")
        for name, note in SUITES:
            print(f"  {name:24s} {note}")
        return 0
    wanted = [a for a in argv if not a.startswith("-")]
    selected = [(n, d) for n, d in SUITES if not wanted or n in wanted]
    if wanted:
        unknown = set(wanted) - {n for n, _ in SUITES}
        if unknown:
            print(f"no such suite: {', '.join(sorted(unknown))}")
            print(f"known: {', '.join(n for n, _ in SUITES)}")
            return 2
    failed = [name for name, note in selected if not run_suite(name, note)]
    print()
    if failed:
        print(f"FAIL: {len(failed)} of {len(selected)} suite(s) failed: "
              f"{', '.join(failed)}")
        return 1
    print(f"checks ok: {len(selected)} suite(s) passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))