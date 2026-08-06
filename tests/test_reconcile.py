"""The screen must recover when the device changes underneath us.

Frames are pushed only when the state changes; the rest of the time what is on
screen is remembered rather than checked. A device that restarts comes back on
its own saved theme while the daemon still believes its frame is up, and without
reconciliation the two never agree again -- silently, since nothing failed.

Also covers the listing guard: scraping the firmware's HTML table cannot tell a
markup change from an empty directory on its own, and mistaking one for the
other makes the daemon re-upload every frame it already has.

    python tests/test_reconcile.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from geekmagic.daemon import Controller, Session  # noqa: E402
from geekmagic.device import DeviceError, SmallTVUltra  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label:<48} got={got!r}")
    if not ok:
        print(f"       {'':<48} want={want!r}")
        failures.append(label)


class FakeDevice:
    """Records what it is told, and can be rebooted out from under the daemon."""

    def __init__(self):
        self.theme = 1
        self.shown: list[str] = []
        self.uploaded: list[str] = []
        self.files: set[str] = set()

    def reboot_to(self, theme: int) -> None:
        """What a power cut looks like: back on the saved theme, nobody told us."""
        self.theme = theme

    def get_theme(self):
        return self.theme

    def set_theme(self, theme):
        self.theme = int(theme)

    def get_album(self):
        return {"autoplay": 1, "i_i": 10}

    def set_autoplay(self, enabled, interval_s=30):
        pass

    def list_files(self, directory="/image"):
        return [{"path": f"/image/{n}", "name": n, "kb": 7} for n in sorted(self.files)]

    def upload(self, directory, filename, data):
        self.files.add(filename)
        self.uploaded.append(filename)

    def delete(self, path):
        self.files.discard(path.rsplit("/", 1)[-1])

    def show_image(self, path):
        self.shown.append(path)


def build(tmp: Path):
    ctrl = Controller({
        "device_host": "127.0.0.1",
        "state_file": str(tmp / "frames.json"),
        "watch_transcripts": False,
        "detect_apps": False,
        "read_account_usage": False,
        "min_push_interval": 0.0,
        "reconcile_interval": 0.0001,   # so a single tick is enough
        "sync_on_start": False,
    })
    ctrl.dev = FakeDevice()
    ctrl.sessions["s1"] = Session(
        provider="anthropic", model="claude-opus-5", status="working", open=True,
    )
    return ctrl


def main() -> int:
    import tempfile

    tmp = Path(tempfile.mkdtemp())

    print("\nA device that reboots while we are driving it")
    ctrl = build(tmp)
    ctrl._tick()
    check("our frame is up", ctrl.dev.theme, 3)
    first = list(ctrl.dev.shown)
    check("and it was pushed once", len(first), 1)

    ctrl._tick()
    check("a quiet tick does not push again", len(ctrl.dev.shown), 1)

    ctrl.dev.reboot_to(1)                # power cut: back to the weather
    ctrl.last_reconcile = 0.0
    ctrl._tick()
    check("the drift is noticed and the theme reset", ctrl.dev.theme, 3)
    check("and the frame is shown again", len(ctrl.dev.shown), 2)
    check("without re-uploading it", len(ctrl.dev.uploaded), 1)

    print("\nA device wiped from its own web page")
    ctrl.dev.reboot_to(1)
    ctrl.dev.files.clear()               # images deleted behind our back
    ctrl.last_reconcile = 0.0
    ctrl._tick()
    check("the frame is uploaded again", len(ctrl.dev.uploaded), 2)
    check("and back on screen", ctrl.dev.theme, 3)

    print("\nThe listing guard")
    dev = SmallTVUltra("127.0.0.1")
    empty = "<table id='list'><tbody><tr><th>#</th></tr> </tbody></table>"
    # The shape a firmware update would plausibly take: same information, double
    # quotes instead of single. Entries are plainly there; the pattern misses
    # them all.
    changed = '<table><tr><td><a href="/image/x.jpg">x.jpg</a></td><td>7</td></tr></table>'
    good = "<td><a href='/image/x.jpg'>x.jpg</a></td><td>7</td>"

    dev.get_text = lambda path: empty
    check("an empty directory is just empty", dev.list_files(), [])
    dev.get_text = lambda path: good
    check("a listing we can read comes through",
          [f["name"] for f in dev.list_files()], ["x.jpg"])
    dev.get_text = lambda path: changed
    try:
        dev.list_files()
        check("markup we cannot parse raises", "no error", "DeviceError")
    except DeviceError:
        check("markup we cannot parse raises", "DeviceError", "DeviceError")

    print()
    if failures:
        print(f"  {len(failures)} check(s) failed\n")
        return 1
    print("  all checks passed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
