"""Configuration loading, with sensible defaults."""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict = {
    # --- device
    "device_host": "192.168.1.42",
    "timeout": 8.0,
    "retries": 2,

    # --- local daemon
    "listen_host": "127.0.0.1",
    "listen_port": 8787,
    "state_file": str(ROOT / ".cache" / "frames.json"),
    "log_level": "INFO",

    # --- screen behaviour
    # Seconds of inactivity before going back to the stock weather station.
    # 0 = return to the weather as soon as the AI stops working.
    "idle_grace_seconds": 180,
    # Theme to return to when the session ends. null = whatever was read from
    # the device before we started. 1 = Weather Clock Today, 2 = Weather Forecast.
    "stock_theme": None,
    # Minimum gap between two screen changes, so we do not chase very rapid
    # state flips (working -> waiting -> working).
    "min_push_interval": 0.6,
    "poll_interval": 0.5,
    "session_ttl_seconds": 3600,

    # --- frames
    # Monospace font to use; empty picks the first one available on the system.
    "font_path": "",
    "jpeg_quality": 88,
    # How many frames to keep on the device before deleting the oldest.
    # With the usage bars the combinations grow (model x status x bucket), so
    # some headroom is needed: roughly 12 KB each against 1.2 MB free.
    "max_cached_frames": 60,
    "sync_on_start": True,
}

CONFIG_PATH = Path(os.environ.get("GEEKMAGIC_CONFIG", ROOT / "config.json"))


def load(path: Path | str | None = None) -> dict:
    cfg = dict(DEFAULTS)
    p = Path(path) if path else CONFIG_PATH
    if p.is_file():
        cfg.update(json.loads(p.read_text("utf-8")))
    # Environment variables win: handy inside hooks.
    if os.environ.get("GEEKMAGIC_HOST"):
        cfg["device_host"] = os.environ["GEEKMAGIC_HOST"]
    if os.environ.get("GEEKMAGIC_PORT"):
        cfg["listen_port"] = int(os.environ["GEEKMAGIC_PORT"])
    return cfg
