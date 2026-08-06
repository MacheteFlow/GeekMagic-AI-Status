"""Reads the account usage windows that Claude Code keeps in ~/.claude.json.

Claude Code refreshes `cachedUsageUtilization` there as it works, which makes it
the one source of usage figures that needs no cooperation at all: it does not go
through hooks, it does not need the status line, and it survives a session that
was already running when this tool was installed.

The status line remains a fallback for setups where that file is absent, but it
only starts producing data after Claude Code has been restarted, so this is
preferred whenever it is available.

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

    def read(self) -> dict:
        """Return {"five_hour": {"used_percentage": ..}, ...}, or {} if unavailable."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return {}
        if mtime == self._mtime:
            return self._cached

        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError, ValueError):
            return self._cached  # keep the last good read rather than blanking

        block = ((data.get("cachedUsageUtilization") or {}).get("utilization") or {})
        result: dict = {}
        for name in WINDOWS:
            window = block.get(name)
            if not isinstance(window, dict):
                continue
            value = window.get("utilization")
            if isinstance(value, (int, float)):
                result[name] = {"used_percentage": float(value)}

        self._mtime = mtime
        self._cached = result
        return result

    def fetched_at(self) -> float | None:
        """When Claude Code last refreshed the figures, as a unix timestamp."""
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        ms = (data.get("cachedUsageUtilization") or {}).get("fetchedAtMs")
        return ms / 1000 if isinstance(ms, (int, float)) else None


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
