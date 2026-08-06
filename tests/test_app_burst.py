"""What an AI application on screen should and should not mean.

Two distinctions, both of which got this wrong at some point:

  open vs merely running -- these apps keep running after you close them to the
  notification area. A tray-only app must not hold the screen, or the weather
  station never comes back.

  working vs idle -- they touch their storage on their own for a sync or a
  notification. Treating one such write as work reported a busy assistant for
  minutes at a time. A real reply writes repeatedly over several seconds.

    python tests/test_app_burst.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import geekmagic.apps as apps_module  # noqa: E402
from geekmagic.apps import AppDetector, AppRule  # noqa: E402

failures: list[str] = []
PID = 4242


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label:<46} got={got!r}")
    if not ok:
        print(f"       {'':<46} want={want!r}")
        failures.append(label)


# Margin kept wide on purpose: a tighter one made this test fail under load,
# and an intermittently red test is worse than no test at all.
LINGER = 2.0
AFTER_LINGER = 5.0


def build(linger: float = LINGER):
    """A detector watching one file, with the platform lookups stubbed out."""
    tmp = Path(tempfile.mkdtemp())
    activity = tmp / "store.log"
    activity.write_text("x", encoding="utf-8")
    rule = AppRule(
        key="fake", provider="acme", model="Acme",
        executables=["fake.exe"], activity=[str(activity)],
        burst_window=25.0, min_changes=2,
    )
    return AppDetector(rules=[rule], linger=linger), activity


def set_platform(running: bool, has_window: bool) -> None:
    apps_module.running_processes = (
        (lambda wanted: {"fake.exe": [(PID, r"C:\apps\fake.exe")]})
        if running else (lambda wanted: {})
    )
    apps_module.visible_window_pids = lambda: ({PID} if has_window else set())


def status_of(detector) -> str | None:
    result = detector.poll()
    return result.get("app:fake", {}).get("status")


def touch(path: Path, when: float) -> None:
    os.utime(path, (when, when))


def main() -> int:
    print("\nAn open app with nothing happening")
    detector, activity = build()
    set_platform(running=True, has_window=True)
    check("shows as idle", status_of(detector), "idle")
    touch(activity, time.time())
    check("a single background write is not work", status_of(detector), "idle")
    check("and still is not on the next poll", status_of(detector), "idle")

    print("\nA reply streaming in")
    detector, activity = build()
    set_platform(running=True, has_window=True)
    base = time.time()
    detector.poll()                     # baseline reading
    for offset in (1, 2):               # two changes on top of it
        touch(activity, base + offset)
        status = status_of(detector)
    check("repeated writes mean working", status, "working")

    print("\nThe reply finishes")
    time.sleep(AFTER_LINGER)
    check("falls back to idle, app still open", status_of(detector), "idle")

    print("\nClosed to the notification area")
    set_platform(running=True, has_window=False)
    check("running with no window shows nothing", detector.poll(), {})

    print("\nReopened")
    set_platform(running=True, has_window=True)
    check("visible again, so idle once more", status_of(detector), "idle")

    print("\nQuit entirely")
    set_platform(running=False, has_window=False)
    check("no process, nothing to show", detector.poll(), {})

    print("\nA platform that cannot report windows")
    detector, _ = build()
    apps_module.running_processes = lambda wanted: {
        "fake.exe": [(PID, "/usr/bin/fake.exe")]
    }
    apps_module.visible_window_pids = lambda: None
    check("running is enough rather than hiding everything",
          status_of(detector), "idle")

    print()
    if failures:
        print(f"  {len(failures)} check(s) failed\n")
        return 1
    print("  all checks passed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
