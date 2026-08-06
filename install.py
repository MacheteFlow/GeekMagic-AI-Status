"""Guided installer, meant to be run by anyone.

It does everything that is needed, explaining each step and asking before it
touches anything:

  1. checks Python and installs the only dependency (Pillow)
  2. finds the device on the network by itself, no IP address needed
  3. saves the configuration
  4. backs up the device
  5. connects Claude Code (hooks + status line), keeping your own settings
  6. optionally starts the daemon at logon, restarted by Windows if it stops
  7. checks the daemon is actually running, since nothing moves without it
  8. shows a test frame and asks whether you saw it

Run it by double-clicking "Install.bat", or with:

    python install.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

CLAUDE_DIR = Path.home() / ".claude"
SETTINGS = CLAUDE_DIR / "settings.json"

# Claude Code event -> status shown on the screen.
HOOK_EVENTS = {
    "UserPromptSubmit": "working",
    "Notification": "waiting",
    "Stop": "idle",
    "SessionEnd": "clear",
}


# ---------------------------------------------------------------- console helpers

def title(step: str, text: str) -> None:
    print(f"\n{'=' * 62}\n  {step}  {text}\n{'=' * 62}")


def ok(text: str) -> None:
    print(f"  [OK]  {text}")


def warn(text: str) -> None:
    print(f"  [!]   {text}")


def ask(question: str, default: str = "y") -> bool:
    """Yes/no question. Pressing Enter accepts the default."""
    hint = "Y/n" if default == "y" else "y/N"
    while True:
        try:
            answer = input(f"  {question} [{hint}] ").strip().lower()
        except EOFError:
            return default == "y"
        if not answer:
            return default == "y"
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def ask_text(question: str, default: str = "") -> str:
    try:
        answer = input(f"  {question}{f' [{default}]' if default else ''} ").strip()
    except EOFError:
        return default
    return answer or default


def quote(path) -> str:
    text = str(path).replace("\\", "/")
    return f'"{text}"' if " " in text else text


# ---------------------------------------------------------------- steps

def step_disclaimer() -> bool:
    title("", "Before you start")
    print("""  This is an unofficial hobby project, not affiliated with or endorsed by
  GeekMagic, Anthropic or anyone else. It is provided AS IS, with NO WARRANTY
  of any kind, under the MIT licence. You use it at your own risk.

  What it will do to your machine and your device:
    - upload small images to your SmallTV and select which one is shown,
      using only the device's own documented web interface
    - it does NOT modify, flash or replace firmware
    - it will add hooks to your Claude Code settings.json, after making a
      timestamped backup of it
    - uninstall.py reverses all of the above

  Adding software to a device may affect its warranty; that is between you
  and your vendor. Full text in README.md and LICENSE.
""")
    return ask("Do you understand and want to continue?", "n")


def step_python() -> bool:
    title("1/8", "Checking Python and dependencies")
    print(f"  Python {sys.version.split()[0]} at {sys.executable}")
    if sys.version_info < (3, 10):
        warn("Python 3.10 or newer is required. Please upgrade and try again.")
        return False
    try:
        import PIL  # noqa: F401
        ok("Pillow is already installed")
        return True
    except ImportError:
        pass
    print("  Pillow is missing. It is the only library needed (it draws the frames).")
    if not ask("Install it now?"):
        warn("Without Pillow the program cannot run.")
        return False
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "Pillow"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        warn("Installation failed:")
        print(result.stdout[-800:], result.stderr[-800:])
        return False
    ok("Pillow installed")
    return True


def step_device() -> str | None:
    title("2/8", "Looking for the device on your network")
    from geekmagic import discover

    nets = discover.candidate_networks()
    print(f"  Networks to scan: {', '.join(str(n) for n in nets)}")
    print("  This takes a few seconds...")
    found = discover.scan(nets)

    if len(found) == 1:
        dev = found[0]
        ok(f"Found {dev['model']} {dev['version']} at {dev['host']}")
        return dev["host"]

    if len(found) > 1:
        print("  Several devices found:")
        for i, dev in enumerate(found, 1):
            print(f"    {i}) {dev['host']}  {dev['model']} {dev['version']}")
        choice = ask_text("Which one do you want to use? (number)", "1")
        try:
            return found[int(choice) - 1]["host"]
        except (ValueError, IndexError):
            return found[0]["host"]

    warn("No device found automatically.")
    print("  Make sure it is powered on and joined to the same WiFi as this computer.")
    print("  The device shows its IP address on screen when it starts up.")
    host = ask_text("Type the IP address manually (empty to cancel):")
    if not host:
        return None
    info = discover.probe(host, timeout=4.0)
    if info:
        ok(f"Found {info['model']} {info['version']} at {host}")
        return host
    warn(f"Nothing at {host} answers like a SmallTV. Please check the address.")
    return host if ask("Use it anyway?", "n") else None


def step_config(host: str) -> None:
    title("3/8", "Saving the configuration")
    path = ROOT / "config.json"
    config = {}
    if path.is_file():
        try:
            config = json.loads(path.read_text("utf-8"))
        except json.JSONDecodeError:
            config = {}
    config["device_host"] = host
    config.setdefault("idle_grace_seconds", 180)
    path.write_text(json.dumps(config, indent=2) + "\n", "utf-8")
    ok(f"Wrote {path}")


def step_backup(host: str) -> None:
    title("4/8", "Backing up the device")
    print("  Saves settings and images into backup/, so you can always go back.")
    print("  Nothing on the device is modified: this step only reads.")
    if not ask("Run the backup now?"):
        warn("Skipped. You can do it later with: python backup_device.py")
        return
    result = subprocess.run(
        [sys.executable, str(ROOT / "backup_device.py"), host],
        cwd=str(ROOT),
    )
    if result.returncode == 0:
        ok("Backup written to backup/")
    else:
        warn("Backup failed. You can retry with: python backup_device.py")


def hook_command(status: str) -> str:
    return f"{quote(sys.executable)} {quote(ROOT / 'hooks' / 'gm_hook.py')} {status}"


def statusline_command() -> str:
    return f"{quote(sys.executable)} {quote(ROOT / 'hooks' / 'statusline.py')}"


def step_claude() -> None:
    title("5/8", "Connecting Claude Code")
    if not CLAUDE_DIR.is_dir():
        warn(f"{CLAUDE_DIR} not found: Claude Code does not seem to be installed.")
        print("  You can still drive the screen manually with gmctl.py.")
        return

    settings = {}
    if SETTINGS.is_file():
        try:
            settings = json.loads(SETTINGS.read_text("utf-8"))
        except json.JSONDecodeError:
            warn(f"{SETTINGS} is not valid JSON. Leaving it alone.")
            return
        backup = SETTINGS.with_name(
            f"settings.json.backup-{datetime.now():%Y%m%d-%H%M%S}"
        )
        shutil.copy2(SETTINGS, backup)
        ok(f"Your settings were backed up to {backup.name}")

    # --- hooks: add ours without disturbing any you already have
    hooks = settings.setdefault("hooks", {})
    for event, status in HOOK_EVENTS.items():
        entries = hooks.setdefault(event, [])
        # Drop older copies of this same hook so we never duplicate it.
        for entry in list(entries):
            inner = entry.get("hooks", []) if isinstance(entry, dict) else []
            if any("gm_hook.py" in str(h.get("command", "")) for h in inner):
                entries.remove(entry)
        entries.append({
            "hooks": [{"type": "command", "command": hook_command(status)}]
        })
    ok(f"Hooks configured: {', '.join(HOOK_EVENTS)}")

    # --- status line: the only source of the usage percentages
    current = settings.get("statusLine")
    mine = "statusline.py" in json.dumps(current or {})
    if current and not mine:
        print("\n  You already have a custom status line:")
        print(f"    {json.dumps(current)}")
        print("  The status line is the only place where Claude Code exposes the")
        print("  5-hour and 7-day usage windows: without it, the bars never show.")
        if ask("Replace it with the one from this project?", "n"):
            settings["statusLine"] = {
                "type": "command", "command": statusline_command()
            }
            ok("Status line replaced")
        else:
            warn("Status line left as it was: no usage bars.")
    else:
        settings["statusLine"] = {"type": "command", "command": statusline_command()}
        ok("Status line configured")

    SETTINGS.write_text(json.dumps(settings, indent=2) + "\n", "utf-8")
    ok(f"Wrote {SETTINGS}")
    print("  Restart Claude Code for the changes to take effect.")


TASK_NAME = "GeekMagic AI Status"


def startup_dir() -> Path | None:
    if os.name != "nt":
        return None
    return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"


def register_scheduled_task(exe: Path, repeat_minutes: int = 5) -> bool:
    """Register a task that starts the daemon at logon and keeps it alive.

    A shortcut in the Startup folder only ever starts it once. If the process
    then dies, nothing brings it back and the screen sits on whatever it last
    showed until somebody notices.

    Two triggers do the work. One at logon, and one that simply runs the task
    again every few minutes. With MultipleInstances set to IgnoreNew, a repeat
    while the daemon is healthy is discarded, and a repeat after it has died
    starts it again. This is deliberately not the scheduler's own "restart on
    failure" setting: that applies only to triggered runs and depends on the
    exit code, and testing showed a killed daemon was not restarted by it. A
    repeating trigger cannot miss, because it never asks why the process is
    gone -- only whether it is.

    Registered for the current user, so no administrator rights are needed.
    """
    script = ROOT / "statusd.py"
    ps = f"""
$ErrorActionPreference = 'Stop'
$action = New-ScheduledTaskAction -Execute '{exe}' -Argument '"{script}"' -WorkingDirectory '{ROOT}'
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes {repeat_minutes}) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $action `
    -Trigger $atLogon, $repeat -Settings $settings -Force -User $env:USERNAME | Out-Null
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        warn("Could not register the scheduled task:")
        print((result.stderr or result.stdout or "").strip()[:400])
        return False
    return True


def step_autostart() -> None:
    title("6/8", "Start on boot")
    if os.name != "nt":
        print("  Automatic startup is only handled on Windows here.")
        print("  On Linux/macOS run the daemon under systemd --user or launchd,")
        print("  with a restart-on-failure policy: python statusd.py")
        return
    print("  The daemon has to be running for the screen to change.")
    print("  It can start at every logon, with no window, and be started again")
    print("  within a few minutes if it ever stops.")
    if not ask("Start it automatically?"):
        print("  No problem: launch it when you need it with 'Start daemon.bat'.")
        return

    # pythonw opens no console window.
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.is_file() else Path(sys.executable)

    if register_scheduled_task(exe):
        ok(f"Scheduled task '{TASK_NAME}' registered, with restart on failure")
        print("  Remove it with uninstall.py, or from Task Scheduler.")
        return

    # Fall back to the Startup folder. It starts the daemon but cannot restart
    # it, so say so rather than let it look equivalent.
    folder = startup_dir()
    vbs = folder / "GeekMagic AI Status.vbs"
    vbs.write_text(
        'Set s = CreateObject("WScript.Shell")\n'
        f's.CurrentDirectory = "{ROOT}"\n'
        f's.Run """{exe}"" ""{ROOT / "statusd.py"}""", 0, False\n',
        encoding="utf-8",
    )
    ok(f"Created {vbs}")
    warn("This starts the daemon at logon but will not restart it if it stops.")


def daemon_alive(port: int = 8787, timeout: float = 2.0) -> bool:
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=timeout
        ) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def step_daemon() -> None:
    """Nothing on screen changes unless this is running, so check that it is.

    Testing the device alone was misleading: setup could end saying everything
    works while the part that drives the screen had never started.
    """
    title("7/8", "The daemon")
    if daemon_alive():
        ok("Already running")
        return

    print("  Not running yet. Starting it to confirm it works...")
    creation = 0x00000008 if os.name == "nt" else 0     # detached, no console
    try:
        subprocess.Popen(
            [sys.executable, str(ROOT / "statusd.py")],
            cwd=str(ROOT), creationflags=creation,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        warn(f"Could not start it: {exc}")
        return

    for _ in range(15):
        time.sleep(1)
        if daemon_alive():
            ok("Running and answering on 127.0.0.1:8787")
            return
    warn("It did not come up. Run 'python statusd.py' by hand to see why.")


def step_test(host: str) -> None:
    title("8/8", "Final check")
    print("  I will show a test frame on the device and then put the weather back.")
    if not ask("Go ahead?"):
        return

    from geekmagic.device import DeviceError, SmallTVUltra
    from geekmagic.render import frame_key, render_bytes

    dev = SmallTVUltra(host, timeout=20.0, retries=4)
    try:
        theme_before = dev.get_theme()
        album = dev.get_album()
        usage = {"five_hour": {"used_percentage": 40}}
        name = frame_key("anthropic", "install-test", "working", usage)
        blob = render_bytes("anthropic", "install-test", "working", usage)
        if name not in {f["name"] for f in dev.list_files("/image")}:
            dev.upload("/image", name, blob)
        dev.set_autoplay(False, int(album.get("i_i", 10)))
        dev.set_theme(3)
        dev.show_image(f"/image/{name}")
    except DeviceError as exc:
        warn(f"Test failed: {exc}")
        return

    print()
    seen = ask("Do you see an orange screen with a spinning mark and a bar?")
    time.sleep(1)
    try:
        dev.delete(f"/image/{name}")
        dev.set_autoplay(bool(album.get("autoplay", 1)), int(album.get("i_i", 10)))
        dev.set_theme(theme_before)
        ok(f"Screen put back to the theme it had ({theme_before})")
    except DeviceError as exc:
        warn(f"Could not restore the theme: {exc}")

    if seen:
        ok("Everything works.")
    else:
        warn("If you saw nothing, check that the device is powered on and")
        warn("reachable, then try again with: python gmctl.py info")


def main() -> int:
    print(r"""
   ____           _    __  __             _
  / ___| ___  ___| | _|  \/  | __ _  __ _(_) ___
 | |  _ / _ \/ _ \ |/ / |\/| |/ _` |/ _` | |/ __|
 | |_| |  __/  __/   <| |  | | (_| | (_| | | (__
  \____|\___|\___|_|\_\_|  |_|\__,_|\__, |_|\___|
                                    |___/
  Show which AI model is working, on your SmallTV screen.
  The original firmware is never modified.
""")
    if not step_disclaimer():
        return 1
    if not step_python():
        return 1
    host = step_device()
    if not host:
        warn("Setup stopped: no device found.")
        return 1
    step_config(host)
    step_backup(host)
    step_claude()
    step_autostart()
    step_daemon()
    step_test(host)

    print(f"\n{'=' * 62}")
    print("  Done. From now on:")
    print("    - start the daemon ('Start daemon.bat') unless it runs on boot")
    print("    - restart Claude Code")
    print("    - while it works you see the model, otherwise the weather station")
    print(f"{'=' * 62}\n")
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
