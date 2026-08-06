"""The freshest usage figure wins, whichever source it came from.

Two places report the same numbers and neither is reliably ahead. The block in
~/.claude.json needs nothing configured, but Claude Code rewrites it on its own
schedule -- measured sitting unchanged through two minutes of active work. The
status line is handed live figures on every render, but only exists once one has
been set up and a session started since.

Ranking them by preference showed a stale number whenever the preferred one
happened to be the older. Comparing timestamps is the only thing that holds.

    python tests/test_usage_source.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from geekmagic.daemon import Controller  # noqa: E402

failures: list[str] = []
SESSION = "s1"


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label:<46} got={got!r}")
    if not ok:
        print(f"       {'':<46} want={want!r}")
        failures.append(label)


def build(tmp: Path):
    cache = tmp / "cache"
    cache.mkdir(exist_ok=True)
    ctrl = Controller({
        "device_host": "127.0.0.1",
        "state_file": str(cache / "frames.json"),
        "usage_file": str(tmp / "claude.json"),
        "watch_transcripts": False,
        "detect_apps": False,
        "sync_on_start": False,
    })
    return ctrl, cache


def write_account(tmp: Path, percent: int, fetched_at: float) -> None:
    (tmp / "claude.json").write_text(json.dumps({
        "cachedUsageUtilization": {
            "fetchedAtMs": fetched_at * 1000,
            "utilization": {"five_hour": {"utilization": percent}},
        }
    }), encoding="utf-8")


def write_statusline(cache: Path, percent: int, ts: float) -> None:
    (cache / f"session-{SESSION}.json").write_text(json.dumps({
        "model": "claude-opus-5",
        "usage": {"five_hour": {"used_percentage": percent}},
        "ts": ts,
    }), encoding="utf-8")


def five_hour(ctrl) -> int | None:
    usage = ctrl._usage_for(SESSION)
    window = usage.get("five_hour")
    return None if window is None else int(window["used_percentage"])


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    now = time.time()

    print("\nOne source only")
    ctrl, cache = build(tmp)
    check("nothing anywhere means no bars", five_hour(ctrl), None)
    write_account(tmp, 40, now - 600)
    check("the account file alone is used", five_hour(ctrl), 40)

    print("\nBoth present")
    write_statusline(cache, 55, now)
    check("the fresher status line wins", five_hour(ctrl), 55)

    ctrl, cache = build(tmp)            # new controller: no cached parse
    write_account(tmp, 61, now)
    write_statusline(cache, 55, now - 600)
    check("a fresher account file wins instead", five_hour(ctrl), 61)

    print("\nA source with no timestamp")
    ctrl, cache = build(tmp)
    (cache / f"session-{SESSION}.json").write_text(json.dumps({
        "usage": {"five_hour": {"used_percentage": 99}},
    }), encoding="utf-8")
    write_account(tmp, 61, now)
    check("undated loses to dated rather than winning by luck",
          five_hour(ctrl), 61)

    print("\nAnother provider's model")
    check("Anthropic percentages are not shown against it",
          ctrl._usage_for(SESSION, "openai"), {})

    print()
    if failures:
        print(f"  {len(failures)} check(s) failed\n")
        return 1
    print("  all checks passed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
