"""Starts the daemon that drives the GeekMagic screen."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from geekmagic import config, daemon  # noqa: E402

if __name__ == "__main__":
    cfg = config.load(sys.argv[1] if len(sys.argv) > 1 else None)
    daemon.serve(cfg)
