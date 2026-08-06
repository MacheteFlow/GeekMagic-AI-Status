"""Restores to the SmallTV-Ultra what backup_device.py saved.

Settings and images go back through the very same endpoints the web console
uses: no firmware is written, these are exactly the operations you would
perform by hand from the configuration page.

    python restore_device.py --dry-run --all    # show what it would do
    python restore_device.py --settings         # settings only
    python restore_device.py --images           # missing images only
    python restore_device.py --all              # everything

What cannot be restored:
  * the WiFi password, which the firmware returns masked ("****")
  * the web console pages: /doUpload only accepts JPG/GIF into /image
  * the firmware: OTA is one-way, a dump would need a UART connection
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from geekmagic.device import DeviceError, SmallTVUltra  # noqa: E402

ROOT = Path(__file__).parent
BACKUP = ROOT / "backup"


def load_cfg(name: str) -> dict:
    path = BACKUP / "config" / name
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError:
        return {}


def build_settings() -> list[tuple[str, dict]]:
    """Translate the backed-up JSON into the /set? queries the firmware expects.

    The parameter names do not always match the ones returned when reading:
    they were taken from the JavaScript of the original pages.
    """
    steps: list[tuple[str, dict]] = []

    brt = load_cfg("brt.json")
    if brt.get("brt") is not None:
        steps.append(("brightness", {"brt": brt["brt"]}))

    night = load_cfg("timebrt.json")
    if night:
        steps.append(("night brightness", {
            "t1": night.get("t1", 23), "t2": night.get("t2", 10),
            "b1": 50, "b2": night.get("b2", -10),
            "en": night.get("en", 0),
        }))

    tl = load_cfg("theme_list.json")
    if tl:
        steps.append(("theme rotation", {
            "theme_list": tl.get("list", "0,0,0,0,0,0,0"),
            "sw_en": tl.get("sw_en", 0),
            "theme_interval": tl.get("sw_i", 15),
        }))

    album = load_cfg("album.json")
    if album:
        steps.append(("image slideshow", {
            "i_i": album.get("i_i", 10), "autoplay": album.get("autoplay", 0),
        }))

    tz = load_cfg("tz.json")
    if tz:
        steps.append(("time zone", {
            "tz_auto": tz.get("tz_auto", 1), "tz_offset": tz.get("tz_off", 0),
        }))

    tc = load_cfg("timecolor.json")
    if tc:
        steps.append(("clock colours", {
            "hc": tc.get("hc", "#ffffff"), "mc": tc.get("mc", "#ffffff"),
            "sc": tc.get("sc", "#ffffff"),
        }))

    unit = load_cfg("unit.json")
    if unit:
        steps.append(("units", {
            "w_u": unit.get("w_u", "km/h"), "t_u": unit.get("t_u", "°C"),
            "p_u": unit.get("p_u", "kPa"),
        }))

    city = load_cfg("city.json")
    if city.get("cd"):
        steps.append(("weather city", {"cd1": city["cd"], "cd2": 1000}))

    colon = load_cfg("colon.json")
    if colon.get("colon") is not None:
        steps.append(("colon blink", {"colon": colon["colon"]}))

    font = load_cfg("font.json")
    if font.get("font") is not None:
        steps.append(("clock font", {"font": font["font"]}))

    # The theme goes last: it decides what you actually see on screen.
    app = load_cfg("app.json")
    if app.get("theme") is not None:
        steps.append(("active theme", {"theme": app["theme"]}))

    return steps


def restore_settings(dev: SmallTVUltra, dry_run: bool) -> None:
    print("== settings ==")
    for label, params in build_settings():
        query = urllib.parse.urlencode(params)
        print(f"  {label:<20} /set?{query}")
        if dry_run:
            continue
        try:
            dev.get("/set?" + query)
        except DeviceError as exc:
            print(f"    [!] failed: {exc}")


def restore_images(dev: SmallTVUltra, dry_run: bool, overwrite: bool) -> None:
    print("== images ==")
    src = BACKUP / "fs" / "image"
    if not src.is_dir():
        print("  no images in the backup")
        return
    try:
        present = {f["name"] for f in dev.list_files("/image")}
    except DeviceError as exc:
        print(f"  [!] could not read the file list: {exc}")
        return

    for path in sorted(src.iterdir()):
        if not path.is_file():
            continue
        if path.name in present and not overwrite:
            print(f"  {path.name:<30} already there, skipped")
            continue
        print(f"  {path.name:<30} {path.stat().st_size:>7} B  -> upload")
        if dry_run:
            continue
        try:
            dev.upload("/image", path.name, path.read_bytes())
        except DeviceError as exc:
            print(f"    [!] failed: {exc}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default=None, help="device IP address")
    p.add_argument("--dry-run", action="store_true", help="show without writing")
    p.add_argument("--settings", action="store_true")
    p.add_argument("--images", action="store_true")
    p.add_argument("--all", action="store_true")
    p.add_argument("--overwrite", action="store_true",
                   help="re-upload images that are already present")
    args = p.parse_args()

    if not (args.settings or args.images or args.all):
        p.error("choose at least one of --settings, --images or --all")

    manifest_path = BACKUP / "MANIFEST.json"
    if not manifest_path.is_file():
        print("[!] No backup found: run backup_device.py first")
        return 1
    manifest = json.loads(manifest_path.read_text("utf-8"))
    host = args.host or manifest.get("host")
    print(f"[*] backup from {manifest.get('created_utc')} taken at {manifest.get('host')}")
    print(f"[*] target: {host}{'  (DRY RUN)' if args.dry_run else ''}\n")

    dev = SmallTVUltra(host, timeout=15.0, retries=5)
    if not args.dry_run and not dev.ping():
        print(f"[!] {host} is unreachable")
        return 1

    if args.settings or args.all:
        restore_settings(dev, args.dry_run)
    if args.images or args.all:
        restore_images(dev, args.dry_run, args.overwrite)

    print("\n[+] done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
