"""Claude Code hook: reports the session state to the daemon.

It runs on every event, so it has to be fast and must never fail loudly: it
only talks to localhost, never to the device, uses the standard library alone,
and exits 0 whatever happens.

Usage:
    python gm_hook.py <status>

where <status> is one of: working, waiting, idle, error, clear.
The event JSON arrives on stdin.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

TIMEOUT = 1.5
PROVIDER = os.environ.get("GEEKMAGIC_PROVIDER", "anthropic")
PORT = int(os.environ.get("GEEKMAGIC_PORT", "8787"))
BASE = f"http://127.0.0.1:{PORT}"

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache")


def read_event() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def model_from_transcript(path: str) -> str:
    """Pick the model out of the last assistant message in the JSONL transcript.

    The transcript can be large, so only its tail is read.
    """
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
            return model
    return ""


def cached_session(session: str) -> dict:
    """Read what the status line cached: model and rate-limit usage.

    Try the current session's file first, then the last one written by any
    session, which as a fallback beats nothing.
    """
    for name in (f"session-{session}.json", "session-last.json"):
        try:
            with open(os.path.join(CACHE_DIR, name), encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and data:
                return data
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def journal(event: str, status: str, model: str) -> None:
    """Append one line per hook invocation, for finding out what actually fires.

    WAITING is the most useful of the three states and the hardest to be sure
    about: it depends on Claude Code emitting Notification, and how often that
    happens depends on how permissions are set. Rather than assume, this records
    every event so a day of ordinary work answers the question.

    Kept small and truncated in one go: a diagnostic that fills the disk would
    be worse than the uncertainty it resolves.
    """
    path = os.path.join(CACHE_DIR, "hook-events.log")
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > 256_000:
            os.replace(path, path + ".1")
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp}\t{event}\t{status}\t{model}\n")
    except OSError:
        pass


def post(path: str, payload: dict) -> None:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        BASE + path, data=body, method="POST",
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
    )
    try:
        urllib.request.urlopen(req, timeout=TIMEOUT).read()
    except (urllib.error.URLError, OSError):
        # Daemon not running: a normal situation, not something to report.
        pass


def main() -> int:
    status = (sys.argv[1] if len(sys.argv) > 1 else "working").lower()
    event = read_event()
    session = str(event.get("session_id") or "default")
    name = str(event.get("hook_event_name") or "?")

    if status == "clear":
        journal(name, status, "")
        post("/clear", {"session": session})
        return 0

    cached = cached_session(session)

    model = ""
    transcript = event.get("transcript_path")
    if transcript:
        model = model_from_transcript(transcript)
    if not model:
        model = cached.get("model") or ""
    if not model:
        model = "claude"

    journal(name, status, model)
    post("/state", {
        "session": session,
        "provider": PROVIDER,
        "model": model,
        "status": status,
        "usage": cached.get("usage") or {},
    })
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # A hook must never get in the way of Claude Code.
        sys.exit(0)
