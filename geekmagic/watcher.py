"""Detects live Claude Code sessions by watching their transcripts.

Hooks are loaded when a session starts, so a session that was already open when
you installed this will never fire them. The transcript files, on the other
hand, are written continuously by every running session, and their filename is
the session id. Watching them means sessions are picked up straight away, with
no restart and no cooperation from the editor at all.

Two different questions are being answered here, and they need two sources:

  * "is this session still open?"  - from ~/.claude/sessions/*.json, which
                                     records the process id of every running
                                     session. An open session keeps the screen
                                     lit even while you sit and read, which
                                     transcript activity alone cannot tell.
  * "is it busy right now?"        - from how recently the transcript grew.

WAITING is not visible from here at all: a session asking you a question looks
exactly like an idle one from the outside. That state only comes from the
Notification hook. So this is the fallback that always works, and hooks refine
it when available.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

DEFAULT_ROOT = Path.home() / ".claude" / "projects"
DEFAULT_SESSIONS = Path.home() / ".claude" / "sessions"


def pid_alive(pid: int) -> bool:
    """Is that process still running?

    On Windows os.kill() is not a liveness probe -- it terminates the target --
    so the process has to be opened through the API instead.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class TranscriptWatcher:
    def __init__(
        self,
        root: Path | str | None = None,
        working_window: float = 12.0,
        active_window: float = 900.0,
        provider: str = "anthropic",
        sessions_dir: Path | str | None = None,
    ):
        self.root = Path(root) if root else DEFAULT_ROOT
        self.sessions_dir = Path(sessions_dir) if sessions_dir else DEFAULT_SESSIONS
        # A transcript touched more recently than this means "still busy".
        self.working_window = working_window
        # Older than this, a session with no running process is forgotten.
        self.active_window = active_window
        self.provider = provider
        # path -> (mtime seen, model found) so the tail is re-read only on change
        self._model_cache: dict[str, tuple[float, str]] = {}

    # ------------------------------------------------------------ internals

    def open_session_ids(self) -> set[str]:
        """Session ids whose process is still running.

        Claude Code writes one small file per session here, named after the pid.
        Stale entries can survive a crash, so the pid is verified rather than
        trusted.
        """
        if not self.sessions_dir.is_dir():
            return set()
        alive: set[str] = set()
        for path in self.sessions_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            session_id = data.get("sessionId")
            pid = data.get("pid")
            if session_id and isinstance(pid, int) and pid_alive(pid):
                alive.add(str(session_id))
        return alive

    @staticmethod
    def _model_from_tail(path: Path) -> str:
        """Read the model from the last assistant message, tail only."""
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 200_000))
                tail = fh.read().decode("utf-8", "replace")
        except OSError:
            return ""
        for line in reversed(tail.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            model = (entry.get("message") or {}).get("model")
            # Entries Claude Code generates itself are tagged with placeholders
            # like "<synthetic>": that is not a model anyone wants on screen.
            if model and not str(model).startswith("<"):
                return str(model)
        return ""

    def _model_for(self, path: Path, mtime: float) -> str:
        key = str(path)
        cached = self._model_cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
        model = self._model_from_tail(path)
        self._model_cache[key] = (mtime, model)
        return model

    # ------------------------------------------------------------ public API

    def poll(self) -> dict[str, dict]:
        """Return the live sessions, keyed by session id.

        Each value is {"provider", "model", "status", "age", "open"} and is
        meant to be fed straight into the daemon. `open` marks a session whose
        process is still running: those must not be timed out just because you
        spent a while reading the last answer.
        """
        if not self.root.is_dir():
            return {}
        now = time.time()
        open_ids = self.open_session_ids()
        found: dict[str, dict] = {}

        for path in self.root.glob("*/*.jsonl"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            age = now - mtime
            session_id = path.stem
            is_open = session_id in open_ids
            if not is_open and age > self.active_window:
                continue

            model = self._model_for(path, mtime)
            if not model:
                continue  # no assistant reply yet: nothing worth showing

            status = "working" if age <= self.working_window else "idle"
            # Several transcripts can move at once (subagents have their own).
            # Keep the freshest entry for a given id.
            previous = found.get(session_id)
            if previous is None or age < previous["age"]:
                found[session_id] = {
                    "provider": self.provider,
                    "model": model,
                    "status": status,
                    "age": age,
                    "open": is_open,
                }
        return found


if __name__ == "__main__":
    watcher = TranscriptWatcher()
    print(f"Watching {watcher.root}")
    print(f"Open sessions: {sorted(s[:8] for s in watcher.open_session_ids())}")
    for sid, info in sorted(watcher.poll().items(), key=lambda kv: kv[1]["age"]):
        flag = "open" if info["open"] else "closed"
        print(f"  {sid[:8]}  {info['model']:<28} {info['status']:<8} "
              f"{flag:<7} {info['age']:.0f}s ago")
