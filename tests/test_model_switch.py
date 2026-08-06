"""Switching model mid-session must reach the screen straight away.

The two sources know different things. The hook is the only one that can see
WAITING, so it decides the status. But it read the model from the transcript at
the moment it fired -- before the reply it belongs to existed -- so after a
switch it keeps naming the previous model. Hooks outrank the watcher for a
couple of minutes, and the screen showed the old name for that whole time.

The watcher re-reads the transcript every cycle, so for the model its answer is
never older and usually newer. Status from one, model from the other.

    python tests/test_model_switch.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from geekmagic.daemon import Controller, Session  # noqa: E402

failures: list[str] = []
SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label:<50} got={got!r}")
    if not ok:
        print(f"       {'':<50} want={want!r}")
        failures.append(label)


class FakeDevice:
    def __init__(self):
        self.theme = 3
        self.shown: list[str] = []

    def get_theme(self):
        return self.theme

    def set_theme(self, theme):
        self.theme = int(theme)

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
        self.shown.append(path)


def write_transcript(path: Path, model: str) -> None:
    """Append an assistant message, the way a reply from that model would."""
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"message": {"role": "assistant", "model": model}}) + "\n")


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    projects = tmp / "projects" / "proj"
    sessions = tmp / "sessions"
    projects.mkdir(parents=True)
    sessions.mkdir(parents=True)
    transcript = projects / f"{SESSION}.jsonl"
    write_transcript(transcript, "claude-opus-5")

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    (sessions / f"{proc.pid}.json").write_text(
        json.dumps({"pid": proc.pid, "sessionId": SESSION, "kind": "interactive"}),
        encoding="utf-8",
    )

    ctrl = Controller({
        "device_host": "127.0.0.1",
        "state_file": str(tmp / "frames.json"),
        "transcript_dir": str(tmp / "projects"),
        "sessions_dir": str(sessions),
        "log_file": "",
        "detect_apps": False,
        "read_account_usage": False,
        "watch_interval": 0.0,
        "min_push_interval": 0.0,
        "reconcile_interval": 0,
        "sync_on_start": False,
    })
    ctrl.dev = FakeDevice()

    try:
        # A hook fired while the previous model was still the last one written.
        ctrl.sessions[SESSION] = Session(
            provider="anthropic", model="claude-opus-5",
            status="waiting", source="hook",
        )
        ctrl._poll_watcher()
        winner = ctrl._winner()
        print("\nBefore the switch")
        check("the model is the one in the transcript",
              winner.model, "claude-opus-5")
        check("and the status is the hook's", winner.status, "waiting")

        print("\nThe user switches model and a reply arrives")
        write_transcript(transcript, "claude-fable-5")
        ctrl._poll_watcher()
        winner = ctrl._winner()
        check("the new model reaches the screen at once",
              winner.model, "claude-fable-5")
        check("while the status still comes from the hook",
              winner.status, "waiting")
        check("and the hook session was the one updated",
              ctrl.sessions[SESSION].model, "claude-fable-5")

        print("\nThe watcher loses sight of the session")
        ctrl.watched.clear()
        ctrl.sessions[SESSION].model = "claude-fable-5"
        ctrl._poll_watcher()
        # Nothing newer to offer, so the last known name is kept rather than
        # blanked: a screen naming no model at all would be worse.
        check("the last known model is kept",
              ctrl.sessions[SESSION].model, "claude-fable-5")
    finally:
        proc.terminate()
        proc.wait()

    print()
    if failures:
        print(f"  {len(failures)} check(s) failed\n")
        return 1
    print("  all checks passed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
