"""Reads the account usage windows that Claude Code keeps in ~/.claude.json.

This source needs no cooperation at all: no hooks, no status line, and it works
for a session that was already running when this tool was installed.

What it is not is continuously fresh. Measured over two minutes of active work,
`fetchedAtMs` never moved: Claude Code rewrites the block on its own schedule,
and opening the Account & Usage panel is one of the things that prompts it. So
the figure here can be minutes old, and callers are given the timestamp
alongside the numbers so they can prefer a fresher source when one exists.

Only the usage block is read. That file also holds account identifiers, which
are of no interest here and are never copied anywhere.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_PATH = Path.home() / ".claude.json"

# Windows we know how to draw, mapped to the names used everywhere else.
WINDOWS = ("five_hour", "seven_day")


class AccountUsage:
    """Reads the usage block, re-parsing only when the file actually changes."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else DEFAULT_PATH
        self._mtime: float | None = None
        self._cached: dict = {}
        self._fetched_at: float | None = None

    def read_with_time(self) -> tuple[dict, float | None]:
        """The usage windows and when Claude Code last refreshed them.

        The file is large and re-parsing it every couple of seconds would be
        wasteful, so the result is kept until its mtime changes. Both values come
        from the same parse: asking for the timestamp separately would mean
        reading the whole file twice.
        """
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return {}, None
        if mtime == self._mtime:
            return self._cached, self._fetched_at

        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError, ValueError):
            # Claude Code may be rewriting it: keep the last good read.
            return self._cached, self._fetched_at

        cached = data.get("cachedUsageUtilization") or {}
        block = cached.get("utilization") or {}
        result: dict = {}
        for name in WINDOWS:
            window = block.get(name)
            if not isinstance(window, dict):
                continue
            value = window.get("utilization")
            if isinstance(value, (int, float)):
                result[name] = {"used_percentage": float(value)}

        ms = cached.get("fetchedAtMs")
        self._mtime = mtime
        self._cached = result
        self._fetched_at = ms / 1000 if isinstance(ms, (int, float)) else None
        return self._cached, self._fetched_at

    def read(self) -> dict:
        """Return {"five_hour": {"used_percentage": ..}, ...}, or {} if unavailable."""
        return self.read_with_time()[0]

    def fetched_at(self) -> float | None:
        """When Claude Code last refreshed the figures, as a unix timestamp."""
        return self.read_with_time()[1]


if __name__ == "__main__":
    import time

    usage = AccountUsage()
    print(f"Reading {usage.path}")
    data = usage.read()
    if not data:
        print("  no usage data available")
    for name, window in data.items():
        print(f"  {name:<12} {window['used_percentage']:.0f}%")
    fetched = usage.fetched_at()
    if fetched:
        print(f"  refreshed {time.time() - fetched:.0f}s ago "
              f"({time.strftime('%H:%M:%S', time.localtime(fetched))})")
