"""Claude Code status line: prints the status bar and caches the session data.

This script is the only place where Claude Code exposes two things the hooks
never receive:

  * model.id / model.display_name    -> the model name to show
  * rate_limits.five_hour / seven_day -> how much of each usage window is spent

They are cached in .cache/session-<id>.json; gm_hook.py reads them back and
forwards them to the daemon. rate_limits only appears on Claude.ai subscriptions
(Pro/Max) and only after the first response in a session, so its absence is a
perfectly normal case.

Whatever is printed on stdout is what you see in the terminal.
"""

from __future__ import annotations

import json
import os
import sys
import time

CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache"
)


def write_cache(session: str, payload: dict) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        blob = json.dumps(payload)
        for name in (f"session-{session}.json", "session-last.json"):
            tmp = os.path.join(CACHE_DIR, name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(blob)
            os.replace(tmp, os.path.join(CACHE_DIR, name))
    except OSError:
        pass


def bar(pct: float, width: int = 10) -> str:
    filled = int(round(max(0.0, min(100.0, pct)) / 100 * width))
    return "#" * filled + "-" * (width - filled)


def main() -> int:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        data = {}

    model = data.get("model") or {}
    model_id = model.get("id") or ""
    display = model.get("display_name") or model_id or "claude"
    session = str(data.get("session_id") or "default")

    limits = data.get("rate_limits") or {}
    usage = {}
    for key in ("five_hour", "seven_day"):
        window = limits.get(key)
        if isinstance(window, dict) and window.get("used_percentage") is not None:
            usage[key] = {
                "used_percentage": window.get("used_percentage"),
                "resets_at": window.get("resets_at"),
            }

    write_cache(session, {
        "model": model_id or display,
        "display": display,
        "usage": usage,
        "ts": time.time(),
    })

    # --- the line shown in the terminal
    workspace = data.get("workspace") or {}
    folder = os.path.basename(workspace.get("current_dir") or data.get("cwd") or "")
    parts = [f"[{display}]"]
    if folder:
        parts.append(folder)
    ctx = (data.get("context_window") or {}).get("used_percentage")
    if ctx is not None:
        parts.append(f"ctx {ctx:.0f}%")
    five = usage.get("five_hour", {}).get("used_percentage")
    if five is not None:
        parts.append(f"5h [{bar(five)}] {five:.0f}%")
    print(" | ".join(parts))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("[claude]")
        sys.exit(0)
