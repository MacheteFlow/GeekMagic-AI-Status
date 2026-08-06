"""Detects live Claude Code sessions by watching their transcripts.

Hooks are loaded when a session starts, so a session that was already open when
you installed this will never fire them. The transcript files, on the other
hand, are written continuously by every running session, and their filename is
the session id. Watching them means sessions are picked up straight away, with
no restart and no cooperation from the editor at all.

What this can tell, and what it cannot:

  * WORKING / IDLE  - inferred reliably from how recently the transcript grew
  * WAITING         - not visible here. A question waiting for you looks exactly
                      like an idle session from the outside. That state only
                      comes from the Notification hook.

So this is the fallback that always works, and hooks refine it when available.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

DEFAULT_ROOT = Path.home() / ".claude" / "projects"


class TranscriptWatcher:
    def __init__(
        self,
        root: Path | str | None = None,
        working_window: float = 12.0,
        active_window: float = 900.0,
        provider: str = "anthropic",
    ):
        self.root = Path(root) if root else DEFAULT_ROOT
        # A transcript touched more recently than this means "still busy".
        self.working_window = working_window
        # Older than this and the session is not considered live any more.
        self.active_window = active_window
        self.provider = provider
        # path -> (mtime seen, model found) so the tail is re-read only on change
        self._model_cache: dict[str, tuple[float, str]] = {}

    # ------------------------------------------------------------ internals

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
            if model:
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

        Each value is {"provider", "model", "status", "age"} and is meant to be
        fed straight into the daemon.
        """
        if not self.root.is_dir():
            return {}
        now = time.time()
        found: dict[str, dict] = {}

        for path in self.root.glob("*/*.jsonl"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            age = now - mtime
            if age > self.active_window:
                continue

            model = self._model_for(path, mtime)
            if not model:
                continue  # no assistant reply yet: nothing worth showing

            session_id = path.stem
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
                }
        return found


if __name__ == "__main__":
    watcher = TranscriptWatcher()
    print(f"Watching {watcher.root}")
    for sid, info in sorted(watcher.poll().items(), key=lambda kv: kv[1]["age"]):
        print(f"  {sid[:8]}  {info['model']:<28} {info['status']:<8} "
              f"{info['age']:.0f}s ago")
