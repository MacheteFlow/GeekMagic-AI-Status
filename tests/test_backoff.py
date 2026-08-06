"""An unreachable device must not become a retry storm.

Unplug the cube and every attempt costs the full timeout across all its retries.
Retrying 0.6 seconds later means hammering a network that has nothing to answer
and writing the same warning to the log forever, for as long as the device is
away -- which might be a weekend.

The wait doubles after each failure up to a ceiling, resets the moment the
device answers, and says so once rather than on every attempt.

    python tests/test_backoff.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from geekmagic.daemon import Controller, Session  # noqa: E402
from geekmagic.device import DeviceError  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label:<50} got={got!r}")
    if not ok:
        print(f"       {'':<50} want={want!r}")
        failures.append(label)


class DeadDevice:
    """Answers nothing, the way an unplugged device does."""

    def __init__(self):
        self.calls = 0
        self.alive = False
        # Remembers what it was told, so reconciliation has nothing to correct
        # once it is set. A stub that always claimed the wrong theme would keep
        # triggering a re-assert and hide what this test is measuring.
        self.theme = 1

    def _maybe(self):
        self.calls += 1
        if not self.alive:
            raise DeviceError("no route to host")

    def get_theme(self):
        self._maybe()
        return self.theme

    def get_album(self):
        self._maybe()
        return {"autoplay": 1, "i_i": 10}

    def set_autoplay(self, enabled, interval_s=30):
        self._maybe()

    def set_theme(self, theme):
        self._maybe()
        self.theme = int(theme)

    def list_files(self, directory="/image"):
        self._maybe()
        return []

    def upload(self, directory, filename, data):
        self._maybe()

    def delete(self, path):
        self._maybe()

    def show_image(self, path):
        self._maybe()


def build():
    tmp = Path(tempfile.mkdtemp())
    ctrl = Controller({
        "device_host": "127.0.0.1",
        "state_file": str(tmp / "frames.json"),
        "log_file": "",
        "watch_transcripts": False,
        "detect_apps": False,
        "read_account_usage": False,
        "min_push_interval": 0.0,
        "sync_on_start": False,
        "retry_backoff_base": 1.0,
        "retry_backoff_max": 8.0,
    })
    ctrl.dev = DeadDevice()
    ctrl.sessions["s1"] = Session(
        provider="anthropic", model="claude-opus-5", status="working", open=True,
    )
    return ctrl


def main() -> int:
    print("\nThe device stops answering")
    ctrl = build()
    now = time.time()

    ctrl._tick()
    check("the first failure is recorded", ctrl.failures, 1)
    check("and the next attempt is a second away",
          round(ctrl.retry_at - now), 1)
    calls_after_first = ctrl.dev.calls

    print("\nTicks while the wait is still running")
    ctrl._tick()
    ctrl._tick()
    check("the device is not touched again", ctrl.dev.calls, calls_after_first)
    check("and nothing is counted as a new failure", ctrl.failures, 1)

    print("\nThe wait doubles with each failure")
    delays = []
    for _ in range(5):
        ctrl.retry_at = 0.0                  # pretend the wait elapsed
        before = time.time()
        ctrl._tick()
        delays.append(round(ctrl.retry_at - before))
    check("1, 2, 4, 8 then held at the ceiling", delays, [2, 4, 8, 8, 8])
    check("failures keep counting", ctrl.failures, 6)

    print("\nThe device comes back")
    ctrl.dev.alive = True
    ctrl.retry_at = 0.0
    ctrl._tick()
    check("the counter resets", ctrl.failures, 0)
    check("and so does the wait", ctrl.retry_at, 0.0)

    print("\nA quiet tick with nothing to send")
    calls = ctrl.dev.calls
    ctrl._tick()
    # One call, and only one: the reconciliation probe asking which theme is up.
    # Nothing is uploaded and nothing is shown, because nothing changed.
    check("costs a single request, not a re-push", ctrl.dev.calls - calls, 1)

    print("\nAnd fails again later")
    ctrl.dev.alive = False
    # Something has to actually need sending, or there is nothing to fail.
    ctrl.sessions["s1"].status = "waiting"
    ctrl._tick()
    check("the wait starts over from the bottom, not the ceiling",
          ctrl.failures, 1)

    print()
    if failures:
        print(f"  {len(failures)} check(s) failed\n")
        return 1
    print("  all checks passed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
