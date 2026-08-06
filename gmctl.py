"""Utility CLI: diagnostics, previews and manual commands.

Useful both to try the project out without waiting for a Claude Code event, and
as the hook-in point for other tools (opencode, scripts, anything) that want to
show their own model: just call `gmctl state`.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from PIL import Image  # noqa: E402

from geekmagic import config  # noqa: E402
from geekmagic.device import DeviceError, SmallTVUltra  # noqa: E402
from geekmagic.render import (  # noqa: E402
    frame_key, is_animated, render, render_bytes, render_frames,
)


def daemon_post(cfg: dict, path: str, payload: dict) -> None:
    url = f"http://127.0.0.1:{cfg['listen_port']}{path}"
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        print(urllib.request.urlopen(req, timeout=3).read().decode())
    except (urllib.error.URLError, OSError) as exc:
        print(f"daemon unreachable at {url}: {exc}", file=sys.stderr)
        raise SystemExit(1)


def cmd_info(cfg, args):
    dev = SmallTVUltra(cfg["device_host"])
    print("version    ", dev.version())
    print("space      ", dev.space())
    print("theme      ", dev.get_theme())
    print("brightness ", dev.get_brightness())
    print("album      ", dev.get_album())
    print("rotation   ", dev.theme_list())
    print("\nfiles in /image:")
    for f in dev.list_files("/image"):
        print(f"  {f['name']:<32} {f['kb']:>5} KB")


def usage_from_args(args) -> dict:
    """Build the rate_limits block from --five-hour / --seven-day."""
    usage = {}
    if getattr(args, "five_hour", None) is not None:
        usage["five_hour"] = {"used_percentage": args.five_hour}
    if getattr(args, "seven_day", None) is not None:
        usage["seven_day"] = {"used_percentage": args.seven_day}
    return usage


def cmd_preview(cfg, args):
    out = Path(args.out)
    usage = usage_from_args(args)
    font = cfg.get("font_path") or None
    name = frame_key(args.provider, args.model, args.status, usage)
    blob = render_bytes(args.provider, args.model, args.status, usage, font)

    if is_animated(args.status):
        # Write the real animation, and the frames side by side to inspect.
        gif_path = out.with_suffix(".gif")
        gif_path.write_bytes(blob)
        frames = render_frames(args.provider, args.model, args.status, usage, font)
        width = frames[0].width
        sheet = Image.new("RGB", (width * len(frames), frames[0].height))
        for i, frame in enumerate(frames):
            sheet.paste(frame, (i * width, 0))
        sheet.save(out)
        print(f"animation written to {gif_path}  ({len(blob)} B, {len(frames)} frames)")
        print(f"frames side by side in {out}")
    else:
        render(args.provider, args.model, args.status, usage, font).save(out)
        print(f"preview written to {out}  ({len(blob)} B)")
    print(f"name on the device: {name}")


def cmd_show(cfg, args):
    """Bypass the daemon and write straight to the device."""
    dev = SmallTVUltra(cfg["device_host"], timeout=20.0, retries=4)
    usage = usage_from_args(args)
    font = cfg.get("font_path") or None
    name = frame_key(args.provider, args.model, args.status, usage)
    blob = render_bytes(args.provider, args.model, args.status, usage, font)
    existing = {f["name"] for f in dev.list_files("/image")}
    if name not in existing:
        print(f"uploading {name} ({len(blob)} B) ...")
        dev.upload("/image", name, blob)
    dev.set_autoplay(False)
    dev.set_theme(3)
    dev.show_image(f"/image/{name}")
    print(f"showing /image/{name}")


def cmd_state(cfg, args):
    daemon_post(cfg, "/state", {
        "session": args.session, "provider": args.provider,
        "model": args.model, "status": args.status,
        "usage": usage_from_args(args),
    })


def cmd_clear(cfg, args):
    daemon_post(cfg, "/clear", {"session": args.session})


def cmd_status(cfg, args):
    url = f"http://127.0.0.1:{cfg['listen_port']}/status"
    try:
        print(json.dumps(json.loads(urllib.request.urlopen(url, timeout=3).read()),
                         indent=2, ensure_ascii=False))
    except (urllib.error.URLError, OSError) as exc:
        print(f"daemon unreachable: {exc}", file=sys.stderr)
        raise SystemExit(1)


def cmd_stock(cfg, args):
    """Put the screen back to the weather station right away."""
    dev = SmallTVUltra(cfg["device_host"])
    dev.set_theme(args.theme)
    print(f"theme set to {args.theme}")


def cmd_gc(cfg, args):
    """Delete every frame this project generated from the device."""
    dev = SmallTVUltra(cfg["device_host"])
    removed = 0
    for f in dev.list_files("/image"):
        if f["name"].startswith("ai_"):
            dev.delete(f["path"])
            print(f"removed {f['name']}")
            removed += 1
    print(f"{removed} frames removed; your own images were left alone")


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", help="path to an alternative config.json")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="device status").set_defaults(fn=cmd_info)

    def add_frame_args(sp):
        sp.add_argument("--provider", default="anthropic")
        sp.add_argument("--model", default="claude-opus-5")
        sp.add_argument("--status", default="working",
                        choices=["working", "waiting", "idle"])
        sp.add_argument("--five-hour", type=float, default=None,
                        help="5-hour window usage, 0-100")
        sp.add_argument("--seven-day", type=float, default=None,
                        help="weekly window usage, 0-100")

    sp = sub.add_parser("preview", help="render a PNG preview locally")
    add_frame_args(sp)
    sp.add_argument("--out", default="preview.png")
    sp.set_defaults(fn=cmd_preview)

    sp = sub.add_parser("show", help="show a frame on the device, without the daemon")
    add_frame_args(sp)
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("state", help="send a state to the daemon")
    add_frame_args(sp)
    sp.add_argument("--session", default="manual")
    sp.set_defaults(fn=cmd_state)

    sp = sub.add_parser("clear", help="close a session in the daemon")
    sp.add_argument("--session", default="manual")
    sp.set_defaults(fn=cmd_clear)

    sub.add_parser("status", help="daemon internals").set_defaults(fn=cmd_status)

    sp = sub.add_parser("stock", help="go back to the stock theme")
    sp.add_argument("--theme", type=int, default=1)
    sp.set_defaults(fn=cmd_stock)

    sub.add_parser("gc", help="remove ai_*.jpg frames from the device").set_defaults(fn=cmd_gc)
    return p


if __name__ == "__main__":
    args = build_parser().parse_args()
    cfg = config.load(args.config)
    try:
        args.fn(cfg, args)
    except DeviceError as exc:
        print(f"device error: {exc}", file=sys.stderr)
        raise SystemExit(2)
