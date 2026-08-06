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

Two details took measuring to get right.

"Running" is not the same as "open". These apps keep running after you close
their window to the notification area, so a process check alone would mean the
weather station never comes back. An app therefore counts as open only while it
owns a visible window; one minimised to the taskbar still does, one hidden in
the tray does not.

Telling working from idle is the other. These apps touch their store
occasionally on their own -- a sync, a notification -- and treating one write as
activity reported a busy assistant for minutes on end. Streaming a reply writes
repeatedly over several seconds, so work is only believed when the file changes
*more than once* inside a short window. Measured on a real installation: an idle
app produced zero writes in eighty seconds, while a reply produced a steady
burst.

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
    # How long a burst of writes is collected over, and how many changes must
    # land inside it before the app counts as working.
    #
    # A change means the file differing from the previous reading, so the first
    # reading is only a baseline and never counts by itself -- otherwise the
    # very first write after startup would look like activity. Two is the point:
    # it rules out the lone background write that fooled an earlier version,
    # while any real reply produces many.
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


def _running_windows(wanted: set[str]) -> dict[str, list[tuple[int, str]]]:
    """Basename -> [(pid, full path)], for the executables we care about.

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

    found: dict[str, list[tuple[int, str]]] = {}
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
                found.setdefault(name, []).append((entry.th32ProcessID, path))
            if not k32.Process32Next(snapshot, ctypes.byref(entry)):
                break
    finally:
        k32.CloseHandle(snapshot)
    return found


def _running_posix(wanted: set[str]) -> dict[str, list[tuple[int, str]]]:
    found: dict[str, list[tuple[int, str]]] = {}
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
                found.setdefault(name, []).append((int(entry.name), path))
        return found
    # macOS and the rest: ask ps once rather than poke at every process.
    import subprocess
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "pid=,comm="], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return found
    for line in out.splitlines():
        pid_text, _, path = line.strip().partition(" ")
        name = os.path.basename(path.strip()).lower()
        if name in wanted and pid_text.isdigit():
            found.setdefault(name, []).append((int(pid_text), path.strip()))
    return found


def running_processes(wanted: set[str]) -> dict[str, list[tuple[int, str]]]:
    if os.name == "nt":
        return _running_windows(wanted)
    return _running_posix(wanted)


def visible_window_pids() -> set[int] | None:
    """Processes that own at least one visible top-level window.

    This is what separates an app you have open from one you closed to the
    notification area: a tray-only app owns no visible window, while a window
    minimised to the taskbar still counts as visible to Windows.

    Returns None where the question cannot be answered, so callers can fall
    back to "running is enough" instead of concluding everything is hidden.
    """
    if os.name != "nt":
        return None

    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.windll.user32
    pids: set[int] = set()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        # Windows with no title are toolbars, message sinks and other
        # invisible-in-practice helpers that every Electron app creates.
        if user32.GetWindowTextLengthW(hwnd) == 0:
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            pids.add(pid.value)
        return True

    if not user32.EnumWindows(callback, 0):
        return None
    return pids


# ---------------------------------------------------------------- detector


class AppDetector:
    def __init__(self, rules: list[AppRule] | None = None,
                 linger: float = 25.0, require_window: bool = True):
        self.rules = rules if rules is not None else list(BUILTIN_APPS)
        # How long an app keeps the working status after its last write, so a
        # reply arriving in chunks does not flicker between working and idle.
        self.linger = linger
        # Whether an app must own a visible window to count as open at all.
        self.require_window = require_window
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

        # The oldest reading in the window is the baseline it is measured
        # against, so n readings describe n-1 changes.
        if len(history) - 1 < rule.min_changes:
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
        """Return the AI apps that are open, keyed by "app:<key>".

        An app counts as open while it owns a visible window. Closing it to the
        notification area leaves the process running, and that must not hold the
        screen -- half the point of the weather station is that it comes back.

        Status is working while the activity file is changing, idle otherwise.
        """
        wanted = {exe for rule in self.rules for exe in rule.executables}
        if not wanted:
            return {}
        processes = running_processes(wanted)
        visible = visible_window_pids() if self.require_window else None
        now = time.time()
        found: dict[str, dict] = {}

        for rule in self.rules:
            key = f"app:{rule.key}"
            entries = [
                entry for exe in rule.executables for entry in processes.get(exe, [])
            ]
            if rule.path_contains:
                entries = [
                    entry for entry in entries
                    if rule.path_contains.lower() in entry[1].lower()
                ]
            if not entries:
                # Gone: forget its history so a restart starts from scratch.
                self._seen.pop(key, None)
                continue

            # `visible` is None when the platform cannot answer, in which case
            # running has to be enough rather than hiding every app.
            if visible is not None and not any(pid in visible for pid, _ in entries):
                self._seen.pop(key, None)
                continue

            status = "idle"
            if rule.activity:
                newest = self.newest_activity(rule.activity)
                if newest is not None and self._working_since(
                    key, newest, rule, now
                ) is not None:
                    status = "working"

            found[key] = {
                "provider": rule.provider,
                "model": rule.model,
                "status": status,
                "age": 0.0,
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
        print("  no AI application open (tray-only ones do not count)")
    for key, info in sorted(results.items()):
        print(f"  {key:<22} {info['model']:<12} {info['status']}")
