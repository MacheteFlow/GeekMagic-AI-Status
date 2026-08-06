"""Runs every test in tests/ and reports one verdict.

The tests are plain scripts rather than a framework: each one exits non-zero if
something is wrong. This gathers them behind a single command, which is what a
person needs before opening a pull request and what continuous integration needs
to decide whether a change is broken.

    python run_tests.py
    python run_tests.py -v        # show each test's own output
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TESTS = ROOT / "tests"


def main() -> int:
    verbose = "-v" in sys.argv or "--verbose" in sys.argv
    scripts = sorted(TESTS.glob("test_*.py"))
    if not scripts:
        print("no tests found in tests/")
        return 1

    print(f"running {len(scripts)} test files with {sys.executable}\n")
    failed: list[str] = []
    for script in scripts:
        started = time.time()
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=not verbose, text=True, cwd=str(ROOT),
        )
        elapsed = time.time() - started
        ok = result.returncode == 0
        print(f"  {'PASS' if ok else 'FAIL'}  {script.name:<28} {elapsed:5.1f}s")
        if not ok:
            failed.append(script.name)
            if not verbose:
                # Only the failing output is worth reading; a passing test that
                # printed pages of detail would bury it.
                for line in (result.stdout or "").splitlines():
                    print(f"        {line}")
                for line in (result.stderr or "").splitlines():
                    print(f"        {line}")

    print()
    if failed:
        print(f"{len(failed)} of {len(scripts)} failed: {', '.join(failed)}")
        return 1
    print(f"all {len(scripts)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
