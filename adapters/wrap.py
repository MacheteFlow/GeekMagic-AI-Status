"""Universal adapter: put any command-line tool on the screen.

For assistants that have no event system of their own. It wraps the command:
while it runs you see WORKING, and when it exits the screen goes back to the
weather station.

    python adapters/wrap.py --provider openai --model gpt-5 -- codex

    python adapters/wrap.py --provider google --model gemini-2.5-pro -- gemini

What this cannot do: tell WAITING apart from WORKING. From outside a process
there is no way to see when the assistant is asking you a question. That needs
a hook into the tool's own events, the way the Claude Code hooks do it; from
any script it is just a POST to /state with status "waiting".
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

PORT = int(os.environ.get("GEEKMAGIC_PORT", "8787"))
BASE = f"http://127.0.0.1:{PORT}"


def post(path: str, payload: dict, timeout: float = 2.0) -> bool:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        BASE + path, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=timeout).read()
        return True
    except (urllib.error.URLError, OSError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--provider", default="", help="small line at the top")
    parser.add_argument("--model", required=True, help="large line in the middle")
    parser.add_argument("--status", default="working",
                        choices=["working", "waiting", "idle", "error"])
    parser.add_argument("--session", default=None,
                        help="session id; a new one per run by default")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="the command to run, after --")
    args = parser.parse_args()

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("no command to run (put it after --)")

    session = args.session or f"wrap-{uuid.uuid4().hex[:8]}"
    if not post("/state", {
        "session": session, "provider": args.provider,
        "model": args.model, "status": args.status,
    }):
        print("[geekmagic] daemon not listening: carrying on without the screen",
              file=sys.stderr)

    try:
        return subprocess.call(command)
    except FileNotFoundError:
        print(f"[geekmagic] command not found: {command[0]}", file=sys.stderr)
        return 127
    except KeyboardInterrupt:
        return 130
    finally:
        # Whatever happens, the screen must not stay stuck.
        post("/clear", {"session": session})


if __name__ == "__main__":
    raise SystemExit(main())
