"""Switching model must show up before the assistant has replied.

The transcript only names a model once a reply written by it exists. Switch
model and ask something, and until the answer starts arriving the transcript
still names the previous one -- measured at forty seconds on a real session,
which is most of the time you spend waiting.

Claude Code writes the new choice to settings.json the moment it is made, so
that file is the only source that knows in time. It wins whenever it is newer
than the transcript, and loses again as soon as a reply lands.

    python tests/test_model_selected.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from geekmagic.watcher import TranscriptWatcher  # noqa: E402

failures: list[str] = []
SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label:<52} got={got!r}")
    if not ok:
        print(f"       {'':<52} want={want!r}")
        failures.append(label)


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    projects = tmp / "projects" / "proj"
    projects.mkdir(parents=True)
    (tmp / "sessions").mkdir()
    transcript = projects / f"{SESSION}.jsonl"
    settings = tmp / "settings.json"

    def reply(model: str, when: float) -> None:
        with open(transcript, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"message": {"role": "assistant",
                                             "model": model}}) + "\n")
        os.utime(transcript, (when, when))

    def choose(value: str, when: float) -> None:
        settings.write_text(json.dumps({"model": value}), encoding="utf-8")
        os.utime(settings, (when, when))

    def watcher() -> TranscriptWatcher:
        return TranscriptWatcher(
            root=tmp / "projects", sessions_dir=tmp / "sessions",
            settings_path=settings, working_window=1e9,
        )

    def reported(w: TranscriptWatcher) -> str | None:
        found = w.poll()
        return found.get(SESSION, {}).get("model")

    now = time.time()

    print("\nA session with replies from two models")
    reply("claude-sonnet-5", now - 300)
    reply("claude-opus-5", now - 100)
    choose("opus", now - 200)                 # chosen before the last reply
    check("the transcript is newer, so it decides",
          reported(watcher()), "claude-opus-5")

    print("\nThe user switches, and has not been answered yet")
    choose("sonnet", now - 10)                # chosen after the last reply
    check("the new choice is shown straight away",
          reported(watcher()), "claude-sonnet-5")

    print("\nThe reply finally arrives")
    reply("claude-sonnet-5", now)
    check("the transcript takes over again, same answer",
          reported(watcher()), "claude-sonnet-5")

    print("\nA short name never seen in this session")
    choose("haiku", time.time())
    check("shown as written, rather than not at all",
          reported(watcher()), "haiku")

    print("\nA full id with a context marker")
    choose("claude-fable-5[1m]", time.time())
    check("the marker is trimmed", reported(watcher()), "claude-fable-5")

    print("\nNo settings file at all")
    settings.unlink()
    check("falls back to the transcript",
          reported(watcher()), "claude-sonnet-5")

    print()
    if failures:
        print(f"  {len(failures)} check(s) failed\n")
        return 1
    print("  all checks passed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
