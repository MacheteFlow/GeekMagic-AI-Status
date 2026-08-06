"""A pause inside a task is not the end of the task.

With one threshold deciding both directions, the ordinary gaps within a single
piece of work -- the seconds between tool calls, while an answer is composed --
read as the work finishing. Measured on a real session: seven changes of state
in one minute, throughout one continuous task. The screen flickered between
orange and green, and said "done" when nothing was done.

Two thresholds fix it. Work starts the moment the transcript moves; it ends only
after real silence. In between, whatever was true stays true.

    python tests/test_hysteresis.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from geekmagic.watcher import TranscriptWatcher  # noqa: E402

failures: list[str] = []
SESSION = "s1"


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label:<52} got={got!r}")
    if not ok:
        print(f"       {'':<52} want={want!r}")
        failures.append(label)


def main() -> int:
    w = TranscriptWatcher(working_window=12.0, idle_after=50.0)

    print("\nA task getting under way")
    check("the transcript just moved", w._status_for(SESSION, 1), "working")
    check("still inside the working window", w._status_for(SESSION, 11), "working")

    print("\nA pause in the middle of it")
    check("past the working window, but not silence yet",
          w._status_for(SESSION, 20), "working")
    check("a longer pause still counts as working",
          w._status_for(SESSION, 49), "working")

    print("\nThe task actually ends")
    check("silence long enough means idle", w._status_for(SESSION, 51), "idle")
    check("and it stays idle", w._status_for(SESSION, 200), "idle")

    print("\nWork resumes")
    check("a fresh write is working again", w._status_for(SESSION, 2), "working")

    print("\nComing back to an idle session in the middle band")
    fresh = TranscriptWatcher(working_window=12.0, idle_after=50.0)
    check("with nothing remembered, assume idle rather than busy",
          fresh._status_for("unknown", 30), "idle")

    print("\nSessions do not borrow each other's state")
    w._status_for("a", 1)                     # a is working
    check("b is judged on its own silence", w._status_for("b", 30), "idle")
    check("and a is unaffected", w._status_for("a", 30), "working")

    print("\nThresholds that cross over")
    odd = TranscriptWatcher(working_window=30.0, idle_after=10.0)
    check("idle_after is never below working_window", odd.idle_after, 30.0)

    print()
    if failures:
        print(f"  {len(failures)} check(s) failed\n")
        return 1
    print("  all checks passed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
