"""A lone file write must not put an AI status on screen.

Desktop apps touch their storage occasionally on their own -- a sync, a
notification -- with nobody talking to them. Treating one write as activity put
an AI status on the device for minutes while the user was just browsing the web.

Streaming a reply is different: it writes repeatedly over several seconds. This
checks that the detector tells the two apart, and that a finished reply releases
the screen once the linger elapses.

    python tests/test_app_burst.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from geekmagic.apps import AppDetector, AppRule  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label:<44} got={got!r} want={want!r}")
    if not ok:
        failures.append(label)


def build(tmp: Path):
    """A detector watching one file, with every process check satisfied."""
    activity = tmp / "store.log"
    activity.write_text("x", encoding="utf-8")
    rule = AppRule(
        key="fake", provider="acme", model="Acme",
        executables=["fake.exe"], activity=[str(activity)],
        burst_window=25.0, min_changes=2,
    )
    detector = AppDetector(rules=[rule], linger=3.0)
    # The process lookup is not what is under test here.
    detector_running = {"fake.exe": [r"C:\apps\fake.exe"]}
    import geekmagic.apps as apps_module
    apps_module.running_processes = lambda wanted: detector_running
    return detector, activity


def touch(path: Path, when: float) -> None:
    import os
    os.utime(path, (when, when))


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    detector, activity = build(tmp)
    now = time.time()

    print("\nA single background write")
    touch(activity, now)
    check("first poll sees it, but one change is not a burst",
          detector.poll(), {})
    check("polling again with nothing new stays empty",
          detector.poll(), {})
    check("and again", detector.poll(), {})

    print("\nA reply streaming in")
    detector2, activity2 = build(Path(tempfile.mkdtemp()))
    base = time.time()
    touch(activity2, base)
    detector2.poll()
    touch(activity2, base + 1)          # second distinct change -> a burst
    result = detector2.poll()
    check("two changes inside the window count as working",
          list(result.keys()), ["app:fake"])
    check("and the status is working",
          result.get("app:fake", {}).get("status"), "working")

    print("\nThe reply finishes")
    time.sleep(3.2)                      # longer than linger
    check("the app drops out once the linger elapses",
          detector2.poll(), {})

    print("\nThe app is closed")
    import geekmagic.apps as apps_module
    apps_module.running_processes = lambda wanted: {}
    check("a process that is gone reports nothing",
          detector2.poll(), {})

    print()
    if failures:
        print(f"  {len(failures)} check(s) failed\n")
        return 1
    print("  all checks passed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
