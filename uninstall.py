"""Removes everything the installer added.

Puts things back the way they were: hooks and status line removed from Claude
Code, autostart entry deleted, generated frames cleared from the device and the
screen returned to the weather station. Your own images and settings are left
untouched.

    python uninstall.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

CLAUDE_DIR = Path.home() / ".claude"
SETTINGS = CLAUDE_DIR / "settings.json"


def ok(text: str) -> None:
    print(f"  [OK]  {text}")


def warn(text: str) -> None:
    print(f"  [!]   {text}")


def ask(question: str, default: str = "y") -> bool:
    hint = "Y/n" if default == "y" else "y/N"
    try:
        answer = input(f"  {question} [{hint}] ").strip().lower()
    except EOFError:
        return default == "y"
    if not answer:
        return default == "y"
    return answer in ("y", "yes")


def clean_claude() -> None:
    print("\n-- Claude Code --")
    if not SETTINGS.is_file():
        print("  Nothing to clean up.")
        return
    try:
        settings = json.loads(SETTINGS.read_text("utf-8"))
    except json.JSONDecodeError:
        warn(f"{SETTINGS} is not valid JSON. Leaving it alone.")
        return

    backup = SETTINGS.with_name(f"settings.json.backup-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(SETTINGS, backup)
    ok(f"Backed up to {backup.name}")

    removed = 0
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        for entry in list(entries):
            inner = entry.get("hooks", []) if isinstance(entry, dict) else []
            if any("gm_hook.py" in str(h.get("command", "")) for h in inner):
                entries.remove(entry)
                removed += 1
        if not entries:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    ok(f"Hooks removed: {removed}")

    line = settings.get("statusLine")
    if line and "statusline.py" in json.dumps(line):
        settings.pop("statusLine")
        ok("Status line removed")

    SETTINGS.write_text(json.dumps(settings, indent=2) + "\n", "utf-8")
    ok(f"Updated {SETTINGS}")


def clean_autostart() -> None:
    print("\n-- Start on boot --")
    if os.name != "nt":
        print("  Nothing to do on this system.")
        return
    done = False

    # The scheduled task is what recent installs create; the Startup shortcut
    # is the older fallback. Both are removed, since a machine may carry either.
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "Unregister-ScheduledTask -TaskName 'GeekMagic AI Status' "
         "-Confirm:$false -ErrorAction Stop"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        ok("Scheduled task removed")
        done = True

    vbs = Path(os.environ["APPDATA"]) / \
        "Microsoft/Windows/Start Menu/Programs/Startup/GeekMagic AI Status.vbs"
    if vbs.is_file():
        vbs.unlink()
        ok(f"Removed {vbs.name}")
        done = True

    if not done:
        print("  It was not enabled.")


def clean_device() -> None:
    print("\n-- Device --")
    from geekmagic import config
    from geekmagic.device import DeviceError, SmallTVUltra

    cfg = config.load()
    host = cfg["device_host"]
    dev = SmallTVUltra(host, timeout=15.0, retries=4)
    if not dev.ping():
        warn(f"{host} is unreachable: skipping device cleanup.")
        return

    removed = 0
    try:
        for entry in dev.list_files("/image"):
            if entry["name"].startswith("ai_"):
                dev.delete(entry["path"])
                removed += 1
        ok(f"Frames removed from the device: {removed} (your images are kept)")
    except DeviceError as exc:
        warn(f"Cleanup incomplete: {exc}")

    theme = cfg.get("stock_theme") or 1
    state_file = Path(cfg["state_file"])
    try:  # if the daemon died while active, the right theme is recorded here
        saved = json.loads(state_file.read_text("utf-8")).get("stock")
        if saved:
            theme = saved.get("theme", theme)
    except (OSError, json.JSONDecodeError):
        pass
    try:
        dev.set_theme(theme)
        ok(f"Screen returned to theme {theme}")
    except DeviceError as exc:
        warn(f"Could not change the theme: {exc}")


def clean_cache() -> None:
    print("\n-- Local files --")
    cache = ROOT / ".cache"
    if cache.is_dir():
        shutil.rmtree(cache, ignore_errors=True)
        ok("Local cache removed")
    print("  backup/ and config.json are left in place.")


def main() -> int:
    print("\n  Uninstalling GeekMagic AI Status\n")
    print("  This removes the hooks, the status line, the autostart entry and the")
    print("  frames uploaded to the device. Backup and configuration are kept.\n")
    if not ask("Proceed?", "n"):
        print("  Cancelled.")
        return 0

    print("\n  If the daemon is running, close it before continuing.")
    clean_claude()
    clean_autostart()
    try:
        clean_device()
    except Exception as exc:  # the device may simply be off: not a failure
        warn(f"Device not cleaned: {exc}")
    clean_cache()

    print("\n  Done. Restart Claude Code to apply the changes.\n")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        print("\n  Cancelled.")
        code = 1
    if os.name == "nt" and sys.stdin.isatty():
        input("  Press Enter to close. ")
    raise SystemExit(code)
