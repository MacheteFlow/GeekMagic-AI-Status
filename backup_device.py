"""Full backup of the GeekMagic SmallTV-Ultra, before anything is changed.

It saves three things:
  1. backup/config/  - every setting the firmware exposes as JSON
  2. backup/fs/      - every file readable over HTTP (images + web console assets)
  3. backup/MANIFEST.json - an inventory with sizes and SHA-256 hashes

WORTH KNOWING: ESP8266 OTA is one-way, write only. The firmware in flash cannot
be read back over the network; a byte-for-byte dump needs a serial UART
connection (esptool read_flash). This backup covers the filesystem and the
configuration, not the firmware image. One more reason not to touch the
firmware: the installed version (9.0.51) is not even published in the official
repository, so it could not be downloaded again.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from geekmagic.device import DeviceError, SmallTVUltra  # noqa: E402

ROOT = Path(__file__).parent
BACKUP = ROOT / "backup"

# Known configuration endpoints. Some answer 404 depending on which themes are
# enabled: that is normal, they are simply skipped.
CONFIG_ENDPOINTS = [
    "v.json", "app.json", "brt.json", "timebrt.json", "theme_list.json",
    "space.json", "config.json", "city.json", "tz.json", "timecolor.json",
    "unit.json", "colon.json", "font.json", "album.json", "delay.json",
    "ntp.json", "daytimer.json", "w_i.json", "hour12.json", "day.json",
    "key.json", "stock.json", "coin.json", "bili.json", "monitor.json",
]

# Static web console assets: this is the settings page we want to preserve.
WEB_ASSETS = [
    "index.html", "network.html", "weather.html", "time.html",
    "image.html", "settings.html",
    "css/style.css", "css/cropper.min.css",
    "js/settings.js", "js/jquery.min.js", "js/cropper.min.js",
]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save(path: Path, data: bytes, partial: bool = False) -> bool:
    """Write the file, but never replace a good copy with a truncated one.

    The ESP8266 web server occasionally closes the connection halfway through
    larger files. If an earlier backup got more bytes, that copy is worth more
    than the one we just downloaded.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if partial and path.is_file() and path.stat().st_size > len(data):
        return False
    path.write_bytes(data)
    return True


def is_gzip(data: bytes) -> bool:
    return data[:2] == b"\x1f\x8b"


def save_readable(name: str, data: bytes) -> bool:
    """The pages are stored pre-compressed in flash and served that way.

    backup/fs keeps the exact bytes; here we add an expanded copy under
    backup/fs-readable, handy for reading and diffing them.
    """
    if not is_gzip(data):
        return False
    try:
        save(BACKUP / "fs-readable" / name, gzip.decompress(data))
        return True
    except OSError:
        return False


def main(host: str) -> int:
    # Many retries: the first read of a big file often fails, but one of the
    # following attempts usually succeeds and gives us the whole file.
    dev = SmallTVUltra(host, timeout=15.0, retries=8)

    print(f"[*] Connecting to {host} ...")
    try:
        version = dev.version()
    except DeviceError as exc:
        print(f"[!] Device unreachable: {exc}")
        return 1
    print(f"[+] {version.get('m')} firmware {version.get('v')}")

    manifest: dict = {
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": host,
        "device": version,
        "config": {},
        "files": {},
        "skipped": [],
        "note": "Firmware in flash is not readable over HTTP: needs UART/esptool.",
    }

    # ---- 1. configuration
    print("[*] Saving configuration ...")
    for name in CONFIG_ENDPOINTS:
        try:
            raw = dev.get("/" + name)
        except DeviceError:
            manifest["skipped"].append(name)
            continue
        save(BACKUP / "config" / name, raw)
        text = raw.decode("utf-8", "replace")
        manifest["config"][name] = text
        print(f"    {name:<20} {text[:70]}")

    # ---- 2. web console assets
    print("[*] Saving web console pages ...")
    for name in WEB_ASSETS:
        dev.last_partial = False
        try:
            raw = dev.get("/" + name)
        except DeviceError:
            manifest["skipped"].append(name)
            continue
        save(BACKUP / "fs" / name, raw)
        entry = {"bytes": len(raw), "sha256": sha256(raw)}
        if dev.last_partial:
            entry["partial"] = True
        if save_readable(name, raw):
            entry["gzip"] = True
        manifest["files"]["/" + name] = entry
        notes = "  gzip" if entry.get("gzip") else ""
        notes += "  (TRUNCATED by the server)" if dev.last_partial else ""
        print(f"    {name:<24} {len(raw):>7} B{notes}")

    # ---- 3. user images
    print("[*] Saving images ...")
    listing = dev.list_files("/image")
    save(BACKUP / "fs" / "_filelist_image.html",
         dev.get("/filelist?dir=/image"))
    for entry in listing:
        dev.last_partial = False
        try:
            raw = dev.download(entry["path"])
        except DeviceError as exc:
            print(f"    [!] {entry['name']}: {exc}")
            manifest["skipped"].append(entry["path"])
            continue
        target = BACKUP / "fs" / entry["path"].lstrip("/")
        written = save(target, raw, partial=dev.last_partial)
        if not written:
            kept = target.stat().st_size
            manifest["files"][entry["path"]] = {
                "bytes": kept, "sha256": sha256(target.read_bytes()),
            }
            print(f"    {entry['name']:<28} {kept:>7} B  (kept earlier copy,"
                  f" download truncated at {len(raw)})")
            continue
        rec = {"bytes": len(raw), "sha256": sha256(raw)}
        if dev.last_partial:
            rec["partial"] = True
        manifest["files"][entry["path"]] = rec
        flag = "  (TRUNCATED by the server)" if dev.last_partial else ""
        print(f"    {entry['name']:<28} {len(raw):>7} B{flag}")

    space = dev.space()
    manifest["space"] = space

    (BACKUP / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    total = sum(f["bytes"] for f in manifest["files"].values())
    print()
    print(f"[+] Backup complete: {len(manifest['files'])} files, {total} bytes")
    print(f"[+] Flash: {space['free']} free of {space['total']}")
    print(f"[+] Manifest: {BACKUP / 'MANIFEST.json'}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        raise SystemExit(main(sys.argv[1]))
    # No address given: fall back to config.json, then to auto-discovery.
    sys.path.insert(0, str(ROOT))
    from geekmagic import config, discover

    host = config.load().get("device_host")
    if not host or not discover.probe(host, timeout=3.0):
        print("[*] Looking for the device on the network ...")
        found = discover.scan()
        if not found:
            print("[!] No device found. Pass the IP address as an argument.")
            raise SystemExit(1)
        host = found[0]["host"]
    raise SystemExit(main(host))
