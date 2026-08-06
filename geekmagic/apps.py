"""Detects AI applications that expose no events at all.

Claude Code can be followed precisely because it writes transcripts and fires
hooks. Most other assistants -- desktop apps, local model runners -- publish
nothing of the sort: the conversation lives in the cloud or inside a database
file. For those, two things can still be observed from outside:

    is the application running?   -> its process
    is it doing something now?    -> the mtime of the files it writes while
                                     streaming a reply

That is enough for WORKING. It is not enough for WAITING, and it never will be:
nothing outside the application knows that it asked you a question.

Two things must not hold the screen, and both took measuring to get right.

An application that is merely running is one: a chat client left open in the
background would otherwise mean the weather station never comes back.

A single file write is the other, and it is the subtler one. These apps touch
their store occasionally on their own -- a sync, a notification -- and treating
one write as activity showed an AI status for minutes while the user was just
browsing. Streaming a reply writes repeatedly over several seconds, so activity
is only believed when the file changes *more than once* inside a short window.
Measured on a real installation: an idle app produced zero writes in eighty
seconds, while a reply produced a steady burst.

The registry below is a starting point, not a closed list. Add your own through
`extra_apps` in config.json, using the same fields.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from glob import glob
from pathlib import Path

LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
APPDATA = os.environ.get("APPDATA", "")
HOME = str(Path.home())


@dataclass
class AppRule:
    """How to recognise one application, and how to tell when it is busy."""

    key: str
    provider: str
    model: str
    # Executable basenames, lowercase, without directory.
    executables: list[str]
    # Required substring of the full executable path. Needed when several
    # unrelated programs share a basename -- "claude.exe" is both the desktop
    # app and the editor extension's helper.
    path_contains: str = ""
    # Files the app touches while it works. Globs; the newest match wins.
    #
    # Choose these carefully. Many Electron apps rewrite "Local Storage" on a
    # timer whether or not anything is happening, which would report the app as
    # busy forever; the conversation data in IndexedDB only moves when there is
    # a real exchange. Verify a candidate path by watching its mtime while the
    # app sits idle: it should age steadily and never jump back.
    activity: list[str] = field(default_factory=list)
    # How long a burst of writes is collected over, and how many distinct
    # changes must land inside it before the app counts as working. Two is the
    # point: it rules out the lone background write that fooled the earlier
    # version, while any real reply produces many.
    burst_window: float = 25.0
    min_changes: int = 2


BUILTIN_APPS: list[AppRule] = [
    AppRule(
        key="claude-desktop",
        provider="anthropic",
        model="Claude",
        executables=["claude.exe", "claude"],
        path_contains="claude_",  # the MSIX package folder, not the extension
        # Only the leveldb store, never the sibling ".blob" tree: directory
        # timestamps there move for reasons unrelated to any conversation.
        activity=[
            rf"{LOCALAPPDATA}\Packages\Claude_*\LocalCache\Roaming\Claude\IndexedDB\*.leveldb\*",
            rf"{APPDATA}\Claude\IndexedDB\*.leveldb\*",
            f"{HOME}/Library/Application Support/Claude/IndexedDB/*.leveldb/*",
        ],
    ),
    AppRule(
        key="chatgpt-desktop",
        provider="openai",
        model="ChatGPT",
        executables=["chatgpt.exe", "chatgpt"],
        activity=[
            rf"{APPDATA}\ChatGPT\IndexedDB\*.leveldb\*",
            rf"{LOCALAPPDATA}\Packages\OpenAI*\LocalCache\Roaming\ChatGPT\IndexedDB\*.leveldb\*",
            f"{HOME}/Library/Application Support/ChatGPT/IndexedDB/*.leveldb/*",
        ],
    ),
    AppRule(
        key="ollama",
        provider="ollama",
        model="Ollama",
        executables=["ollama.exe", "ollama app.exe", "ollama"],
        activity=[
            rf"{LOCALAPPDATA}\Ollama\*.log",
            f"{HOME}/.ollama/logs/*.log",
        ],
    ),
    AppRule(
        key="lm-studio",
        provider="lm studio",
        model="LM Studio",
        executables=["lm studio.exe", "lm studio"],
        activity=[
            rf"{HOME}\.lmstudio\server-logs\*",
            f"{HOME}/.lmstudio/server-logs/*",
        ],
    ),
]


# ---------------------------------------------------------------- processes


def _running_windows(wanted: set[str]) -> dict[str, list[str]]:
    """Basename -> full paths, for the executables we care about.

    Enumerated with Toolhelp32, which hands back names cheaply; the full path is
    resolved only for the few processes whose name already matched, since that
    part needs a handle per process.
    """
    import ctypes
    import ctypes.wintypes as wt

    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    MAX_PATH = 260

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
            ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_char * MAX_PATH),
        ]

    k32 = ctypes.windll.kernel32
    snapshot = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == -1:
        return {}

    found: dict[str, list[str]] = {}
    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
    try:
        if not k32.Process32First(snapshot, ctypes.byref(entry)):
            return {}
        while True:
            name = entry.szExeFile.decode("utf-8", "replace").lower()
            if name in wanted:
                path = ""
                handle = k32.OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION, False, entry.th32ProcessID
                )
                if handle:
                    try:
                        buf = ctypes.create_unicode_buffer(32768)
                        size = wt.DWORD(32768)
                        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                            path = buf.value
                    finally:
                        k32.CloseHandle(handle)
                found.setdefault(name, []).append(path)
            if not k32.Process32Next(snapshot, ctypes.byref(entry)):
                break
    finally:
        k32.CloseHandle(snapshot)
    return found


def _running_posix(wanted: set[str]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    proc = Path("/proc")
    if proc.is_dir():  # Linux
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                path = os.readlink(entry / "exe")
            except OSError:
                continue
            name = os.path.basename(path).lower()
            if name in wanted:
                found.setdefault(name, []).append(path)
        return found
    # macOS and the rest: ask ps once rather than poke at every process.
    import subprocess
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "comm="], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return found
    for line in out.splitlines():
        path = line.strip()
        name = os.path.basename(path).lower()
        if name in wanted:
            found.setdefault(name, []).append(path)
    return found


def running_processes(wanted: set[str]) -> dict[str, list[str]]:
    if os.name == "nt":
        return _running_windows(wanted)
    return _running_posix(wanted)


# ---------------------------------------------------------------- detector


class AppDetector:
    def __init__(self, rules: list[AppRule] | None = None,
                 linger: float = 25.0):
        self.rules = rules if rules is not None else list(BUILTIN_APPS)
        # How long an app stays on screen after its last write, so the display
        # does not flicker between the chunks of a single reply. Once this
        # elapses the app drops out and the screen is released.
        self.linger = linger
        # key -> mtimes seen recently, used to tell a burst from a lone write.
        self._seen: dict[str, list[tuple[float, float]]] = {}

    def _working_since(self, key: str, mtime: float, rule: AppRule,
                       now: float) -> float | None:
        """When the app was last seen writing, if it counts as working.

        Two conditions, and both are needed. Enough changes inside the burst
        window, which rules out the lone background write. And a last change
        that is itself recent, without which a finished reply would keep
        asserting itself for the whole width of the window.
        """
        history = self._seen.setdefault(key, [])
        if not history or history[-1][1] != mtime:
            history.append((now, mtime))
        cutoff = now - rule.burst_window
        history[:] = [entry for entry in history if entry[0] >= cutoff]

        if len(history) < rule.min_changes:
            return None
        last_change = history[-1][0]
        if now - last_change > self.linger:
            return None
        return last_change

    @staticmethod
    def newest_activity(patterns: list[str]) -> float | None:
        newest: float | None = None
        for pattern in patterns:
            for path in glob(pattern):
                try:
                    mtime = os.stat(path).st_mtime
                except OSError:
                    continue
                if newest is None or mtime > newest:
                    newest = mtime
        return newest

    def poll(self) -> dict[str, dict]:
        """Return the AI apps that are actually working, keyed by "app:<key>".

        Only working apps are returned. There is deliberately no idle state
        here: an app we cannot prove is busy has no business holding the screen.
        """
        wanted = {exe for rule in self.rules for exe in rule.executables}
        if not wanted:
            return {}
        processes = running_processes(wanted)
        now = time.time()
        found: dict[str, dict] = {}

        for rule in self.rules:
            key = f"app:{rule.key}"
            paths = [p for exe in rule.executables for p in processes.get(exe, [])]
            running = bool(paths) and (
                not rule.path_contains
                or any(rule.path_contains.lower() in p.lower() for p in paths)
            )
            if not running:
                # Gone: forget its history so a restart starts from scratch.
                self._seen.pop(key, None)
                continue

            # With no activity files we can only say the app exists, which is
            # not a reason to hold the screen, so such rules are skipped here.
            if not rule.activity:
                continue
            newest = self.newest_activity(rule.activity)
            if newest is None:
                continue
            since = self._working_since(key, newest, rule, now)
            if since is None:
                continue

            found[key] = {
                "provider": rule.provider,
                "model": rule.model,
                "status": "working",
                "age": now - since,
            }
        return found


def rules_from_config(entries) -> list[AppRule]:
    """Build extra rules from config.json, ignoring anything malformed."""
    rules = []
    for entry in entries or []:
        if not isinstance(entry, dict) or not entry.get("executables"):
            continue
        try:
            rules.append(AppRule(
                key=str(entry.get("key") or entry.get("model") or "custom"),
                provider=str(entry.get("provider", "")),
                model=str(entry.get("model", "AI")),
                executables=[str(e).lower() for e in entry["executables"]],
                path_contains=str(entry.get("path_contains", "")),
                activity=[str(a) for a in entry.get("activity", [])],
                working_window=float(entry.get("working_window", 10.0)),
            ))
        except (TypeError, ValueError):
            continue
    return rules


if __name__ == "__main__":
    import sys

    detector = AppDetector()
    print("Known applications:", ", ".join(r.key for r in detector.rules))
    # One poll can never see a burst, since a burst is by definition more than
    # one change over time. Sample for a while so the output means something.
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    print(f"Sampling for {rounds * 3}s...")
    results: dict[str, dict] = {}
    for _ in range(rounds):
        results = detector.poll()
        time.sleep(3)
    if not results:
        print("  nothing working right now")
    for key, info in sorted(results.items(), key=lambda kv: kv[1]["age"]):
        print(f"  {key:<22} {info['model']:<12} {info['status']:<8} "
              f"last write {info['age']:.0f}s ago")
