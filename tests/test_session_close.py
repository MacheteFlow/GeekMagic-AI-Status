"""Closing the editor must bring the weather station back.

This covers the awkward case: a session that is open but quiet looks, from the
transcript alone, exactly like a session whose editor has been closed. Only the
process check tells them apart, and getting it wrong in either direction is
visible on the desk -- either the screen drops back to the weather while you are
still reading an answer, or it stays lit forever after you quit.

Runs against fake transcript and session directories plus a real throwaway
process, so the same pid check as production is exercised. The device is a stub
that records theme changes; no hardware is touched.

    python tests/test_session_close.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from geekmagic.daemon import Controller  # noqa: E402

SESSION_ID = "11111111-2222-3333-4444-555555555555"


class FakeDevice:
    """Records theme changes instead of talking to the hardware."""

    def __init__(self):
        self.theme = 1
        self.log: list[str] = []

    def get_theme(self):
        return self.theme

    def set_theme(self, theme):
        self.theme = int(theme)
        self.log.append(f"theme={theme}")

    def get_album(self):
        return {"autoplay": 1, "i_i": 10}

    def set_autoplay(self, enabled, interval_s=30):
        pass

    def list_files(self, directory="/image"):
        return []

    def upload(self, directory, filename, data):
        pass

    def delete(self, path):
        pass

    def show_image(self, path):
        self.log.append(f"img={path}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    projects = tmp / "projects" / "proj"
    sessions = tmp / "sessions"
    projects.mkdir(parents=True)
    sessions.mkdir(parents=True)

    # A plausible transcript: all that matters is a model on the last line.
    (projects / f"{SESSION_ID}.jsonl").write_text(
        json.dumps({"message": {"model": "claude-opus-5", "role": "assistant"}}) + "\n",
        encoding="utf-8",
    )

    # A real process, so pid_alive() is genuinely exercised before we kill it.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    registry = sessions / f"{proc.pid}.json"
    registry.write_text(
        json.dumps({"pid": proc.pid, "sessionId": SESSION_ID, "kind": "interactive"}),
        encoding="utf-8",
    )

    ctrl = Controller({
        "device_host": "127.0.0.1",
        "state_file": str(tmp / "frames.json"),
        "transcript_dir": str(tmp / "projects"),
        "sessions_dir": str(sessions),
        "read_account_usage": False,
        "watch_interval": 0.0,
        "min_push_interval": 0.0,
        "closed_grace_seconds": 2,
        # Deliberately huge: closing the editor must win over the idle timeout,
        # not merely happen to coincide with it.
        "idle_grace_seconds": 999,
        "watch_working_seconds": 5,
        "sync_on_start": False,
    })
    ctrl.dev = FakeDevice()

    def step(label: str) -> None:
        ctrl._poll_watcher()
        ctrl._tick()
        winner = ctrl._winner()
        state = "no session"
        if winner:
            state = (f"status={winner.status} open={winner.open} "
                     f"ended={winner.ended}")
        print(f"  {label:<26} theme={ctrl.dev.theme}  {state}")

    print("\nClosing the editor with a session open\n")
    step("1. session open")
    if ctrl.dev.theme != 3:
        print("  FAILED: should have switched to Photo Album")
        proc.terminate()
        return 1

    proc.terminate()
    proc.wait()
    registry.unlink()  # Claude Code cleans its own registry up
    print("\n  -- editor closed (process gone) --\n")

    step("2. immediately after")
    time.sleep(2.5)
    step("3. after closed_grace")

    ok = ctrl.dev.theme == 1
    print(f"\n  result: {'OK - weather restored' if ok else 'FAILED'}")
    print(f"  device history: {ctrl.dev.log}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
