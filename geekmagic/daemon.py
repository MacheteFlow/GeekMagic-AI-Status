"""Local daemon: collects AI session states and shows them on the SmallTV.

Hooks are throwaway processes that must exit immediately, so they never talk to
the device: they POST to this daemon on localhost and die. The daemon buffers,
coalesces rapid changes and manages the cache of frames already on the device.

How it behaves:
  * with several sessions open the most urgent state wins: waiting > working > idle
  * a frame is uploaded once; later uses only call /set?img=, so in steady
    state the flash is never rewritten
  * when no session has been active for idle_grace_seconds, the device's
    original theme (the weather station) and its settings are restored
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .apps import BUILTIN_APPS, AppDetector, rules_from_config
from .device import THEME_PHOTO_ALBUM, DeviceError, SmallTVUltra
from .render import frame_key, render_bytes
from .usage import AccountUsage
from .watcher import TranscriptWatcher

log = logging.getLogger("geekmagic")

# Increasing priority: with several sessions, the highest value wins.
STATUS_RANK = {"idle": 0, "working": 1, "error": 2, "waiting": 3}

# Tie-break between sources reporting the same status. Applications are inferred
# from file activity, which is a guess; a hook is the assistant telling us
# directly. On equal status the better-evidenced one should be on screen.
SOURCE_RANK = {"app": 0, "watch": 1, "hook": 2}

IMAGE_DIR = "/image"
FRAME_PREFIX = "ai_"


@dataclass
class Session:
    provider: str = ""
    model: str = ""
    status: str = "idle"
    # rate_limits exactly as Claude Code reports it: {"five_hour": {...}, ...}
    usage: dict = field(default_factory=dict)
    updated: float = field(default_factory=time.time)
    # "hook" when a Claude Code event reported it, "watch" when it was inferred
    # from the transcript. Hooks are more accurate and win over the watcher.
    source: str = "hook"
    # True while the editor process is still running. An open session never
    # times out: you can read an answer for ten minutes and it is still open.
    open: bool = False
    # True once a session we had seen open is gone: the editor was closed, so
    # the session is over and there is no point waiting out the idle timeout.
    ended: bool = False


@dataclass
class StockState:
    """How the device looked before we started: it must be put back identical."""
    theme: int = 1
    autoplay: int = 1
    interval: int = 10


class Controller:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.dev = SmallTVUltra(
            cfg["device_host"],
            timeout=cfg.get("timeout", 8.0),
            retries=cfg.get("retries", 2),
        )
        self.sessions: dict[str, Session] = {}
        # Sessions inferred from transcripts, kept apart so hooks always win.
        self.watched: dict[str, Session] = {}
        self.last_watch = 0.0
        self.watcher = (
            TranscriptWatcher(
                root=cfg.get("transcript_dir") or None,
                working_window=cfg.get("watch_working_seconds", 12.0),
                active_window=cfg.get("session_ttl_seconds", 3600),
                sessions_dir=cfg.get("sessions_dir") or None,
            )
            if cfg.get("watch_transcripts", True) else None
        )
        self.account_usage = (
            AccountUsage(cfg.get("usage_file") or None)
            if cfg.get("read_account_usage", True) else None
        )
        # Applications that publish no events: detected by process and by the
        # files they touch while replying.
        self.apps: dict[str, Session] = {}
        self.app_detector = (
            AppDetector(
                rules=list(BUILTIN_APPS) + rules_from_config(cfg.get("extra_apps")),
                linger=cfg.get("app_linger_seconds", 25.0),
                require_window=cfg.get("app_require_window", True),
            )
            if cfg.get("detect_apps", True) else None
        )
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopping = threading.Event()

        self.stock: StockState | None = None
        self.in_ai_mode = False
        self.shown_frame: str | None = None
        self.last_push = 0.0
        self.idle_since: float | None = None

        self.state_file = Path(cfg["state_file"])
        saved = self._load_state()
        self.known_frames: dict[str, float] = saved.get("frames", {})
        # Anything here means the previous run died without restoring, so the
        # stock state recorded back then is the one to trust.
        self.pending_stock: dict | None = saved.get("stock")

    # ------------------------------------------------------------ local cache

    def _load_state(self) -> dict:
        try:
            data = json.loads(self.state_file.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_known(self) -> None:
        """Persist the frame cache and the stock state.

        The stock state on disk is the only thing that lets us put the screen
        back if the daemon is killed outright while it is driving it.
        """
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            payload: dict = {"frames": self.known_frames}
            if self.in_ai_mode and self.stock is not None:
                payload["stock"] = vars(self.stock)
            self.state_file.write_text(json.dumps(payload, indent=2), "utf-8")
        except OSError as exc:
            log.warning("could not save state: %s", exc)

    def _sync_known_from_device(self) -> None:
        """Realign the cache with what is actually on the device."""
        try:
            names = {f["name"] for f in self.dev.list_files(IMAGE_DIR)}
        except DeviceError as exc:
            log.warning("file listing failed: %s", exc)
            return
        self.known_frames = {
            n: self.known_frames.get(n, time.time())
            for n in names if n.startswith(FRAME_PREFIX)
        }
        self._save_known()

    # ------------------------------------------------------------ public API

    def update(self, session_id: str, provider: str, model: str, status: str,
               usage: dict | None = None) -> None:
        status = status.lower().strip() or "idle"
        with self.lock:
            previous = self.sessions.get(session_id)
            # Usage percentages only come from the status line and may be
            # missing on any single event; keep the last known ones then.
            if not usage and previous:
                usage = previous.usage
            self.sessions[session_id] = Session(provider, model, status, usage or {})
        self.wake.set()

    def clear(self, session_id: str) -> None:
        with self.lock:
            self.sessions.pop(session_id, None)
        self.wake.set()

    def snapshot(self) -> dict:
        with self.lock:
            winner = self._winner()
            return {
                "sessions": {
                    k: vars(v) | {"age_s": round(time.time() - v.updated, 1)}
                    for k, v in self.sessions.items()
                },
                "effective": vars(winner) if winner else None,
                "ai_mode": self.in_ai_mode,
                "shown_frame": self.shown_frame,
                "cached_frames": len(self.known_frames),
            }

    # ------------------------------------------------------------ logic

    def _live_sessions(self) -> dict[str, Session]:
        """Hook-reported sessions, plus watched ones that no hook is covering.

        When both sources describe the same session the hook wins, because it
        knows about WAITING and the watcher cannot: from the outside a session
        asking you a question looks exactly like an idle one.
        """
        ttl = self.cfg.get("session_ttl_seconds", 3600)
        hook_priority = self.cfg.get("hook_priority_seconds", 120)
        now = time.time()

        live = {
            k: s for k, s in self.sessions.items()
            if s.open or now - s.updated < ttl
        }
        for key, session in self.watched.items():
            covering = live.get(key)
            if covering and now - covering.updated < hook_priority:
                continue
            live[key] = session
        # Applications carry their own expiry: the detector only reports them
        # while their activity is recent, so anything still here is relevant.
        live.update(self.apps)
        return live

    def _winner(self) -> Session | None:
        """The session that decides what you see.

        Most urgent first, so a question waiting for you is never hidden by
        something merely busy. Then the best-evidenced source, so a guess made
        from file activity does not push aside a session we are following
        properly. Recency settles the rest.
        """
        live = list(self._live_sessions().values())
        if not live:
            return None
        return max(live, key=lambda s: (
            STATUS_RANK.get(s.status, 0),
            SOURCE_RANK.get(s.source, 0),
            s.updated,
        ))

    def _poll_apps(self, now: float) -> None:
        """Refresh the applications detected by process and file activity."""
        if self.app_detector is None:
            return
        try:
            found = self.app_detector.poll()
        except OSError as exc:
            log.warning("application scan failed: %s", exc)
            return
        with self.lock:
            self.apps = {
                key: Session(
                    provider=info["provider"], model=info["model"],
                    status=info["status"],
                    usage=self._usage_for(key, info["provider"]),
                    updated=now - info["age"], source="app",
                )
                for key, info in found.items()
            }

    def _poll_watcher(self) -> None:
        """Refresh the sessions inferred from transcripts and applications."""
        now = time.time()
        if now - self.last_watch < self.cfg.get("watch_interval", 2.0):
            return
        self.last_watch = now
        self._poll_apps(now)
        if self.watcher is None:
            return
        try:
            found = self.watcher.poll()
        except OSError as exc:
            log.warning("transcript scan failed: %s", exc)
            return
        with self.lock:
            watched = {}
            for key, info in found.items():
                is_open = info.get("open", False)
                watched[key] = Session(
                    provider=info["provider"], model=info["model"],
                    status=info["status"],
                    usage=self._usage_for(key),
                    updated=now - info["age"], source="watch",
                    open=is_open,
                    ended=info.get("ended", False),
                )
            self.watched = watched

            # The two sources know different things about the same session and
            # have to be merged rather than have one replace the other: only the
            # hook knows about WAITING, only the watcher knows whether the
            # editor is still open, and the usage figures may have landed in the
            # cache after the hook already ran.
            for key, session in self.sessions.items():
                sibling = watched.get(key)
                # Assigned outright, never left at its previous value: a session
                # the watcher has stopped reporting is not open any more, and a
                # stale True here would keep the screen lit forever.
                session.open = sibling.open if sibling is not None else False
                session.ended = sibling.ended if sibling is not None else False
                # Refreshed unconditionally, not just when missing: the figures
                # climb while you work, and a hook only reports them once.
                session.usage = self._usage_for(key) or session.usage

    def _usage_for(self, session_id: str, provider: str = "anthropic") -> dict:
        """Current usage percentages, from the freshest source available.

        The account-wide figures Claude Code keeps in ~/.claude.json are
        preferred: they are refreshed as it works and need neither hooks nor a
        restarted status line. The per-session cache written by the status line
        is the fallback. If neither has anything, no bars get drawn, which beats
        drawing a stale number.

        The figures describe an Anthropic subscription, so they are never shown
        against another provider's model: a ChatGPT window must not display
        Claude's remaining quota.
        """
        if provider and provider.lower() != "anthropic":
            return {}
        if self.account_usage is not None:
            account = self.account_usage.read()
            if account:
                return account
        path = self.state_file.parent / f"session-{session_id}.json"
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        usage = data.get("usage")
        return usage if isinstance(usage, dict) else {}

    def _capture_stock(self) -> StockState:
        """Read the configuration to put back when the session ends.

        It has to be read before switching to Photo Album, otherwise we would
        record our own mode as the starting point. If that is exactly what we
        find (after an unclean shutdown, say) fall back to the configured theme.
        """
        forced = self.cfg.get("stock_theme")
        try:
            album = self.dev.get_album()
            theme = self.dev.get_theme()
        except DeviceError as exc:
            log.warning("could not read the stock state: %s", exc)
            return StockState(theme=forced or 1)

        if theme == THEME_PHOTO_ALBUM:
            fallback = forced or 1
            log.warning("device already in Photo Album: using theme %s as stock",
                        fallback)
            theme = fallback
        return StockState(
            theme=forced or theme,
            autoplay=int(album.get("autoplay", 1)),
            interval=int(album.get("i_i", 10)),
        )

    def _enter_ai_mode(self) -> None:
        if self.in_ai_mode:
            return
        if self.stock is None:
            self.stock = self._capture_stock()
            log.info("stock state recorded: %s", vars(self.stock))
        # The automatic slideshow would overwrite our frame.
        self.dev.set_autoplay(False, self.stock.interval)
        self.dev.set_theme(THEME_PHOTO_ALBUM)
        self.in_ai_mode = True
        self._save_known()  # persist the stock state: needed if killed right now

    def _restore_stock(self) -> None:
        """Put the weather station back exactly as it was."""
        if not self.in_ai_mode:
            return
        stock = self.stock or StockState()
        try:
            self.dev.set_autoplay(bool(stock.autoplay), stock.interval)
            self.dev.set_theme(stock.theme)
            log.info("stock theme restored (theme=%s)", stock.theme)
        except DeviceError as exc:
            log.warning("restore failed: %s", exc)
            return
        self.in_ai_mode = False
        self.shown_frame = None
        # Next session should read the stock state fresh from the device.
        self.stock = None
        self._save_known()

    def _recover_after_crash(self) -> None:
        """If the previous run never restored, fix it at startup."""
        if not self.pending_stock:
            return
        self.stock = StockState(**self.pending_stock)
        self.in_ai_mode = True
        log.info("pending restore from the previous run: %s", self.pending_stock)
        self.pending_stock = None
        self._restore_stock()

    def _ensure_frame(self, provider: str, model: str, status: str,
                      usage: dict | None = None) -> str:
        """Return the device path of the frame for this state, uploading if needed."""
        name = frame_key(provider, model, status, usage)
        path = f"{IMAGE_DIR}/{name}"
        if name not in self.known_frames:
            # An animated status comes back as a looping GIF, which the device
            # plays by itself; everything else is a still JPEG.
            blob = render_bytes(
                provider, model, status, usage,
                self.cfg.get("font_path") or None,
                quality=self.cfg.get("jpeg_quality", 88),
            )
            log.info("uploading frame %s (%d B) %s/%s/%s",
                     name, len(blob), provider, model, status)
            self.dev.upload(IMAGE_DIR, name, blob)
            self.known_frames[name] = time.time()
            self._gc_frames()
            self._save_known()
        else:
            self.known_frames[name] = time.time()
        return path

    def _gc_frames(self) -> None:
        """Keep disk use in check: drop the least recently used frames."""
        limit = self.cfg.get("max_cached_frames", 24)
        if len(self.known_frames) <= limit:
            return
        oldest = sorted(self.known_frames.items(), key=lambda kv: kv[1])
        for name, _ in oldest[: len(self.known_frames) - limit]:
            try:
                self.dev.delete(f"{IMAGE_DIR}/{name}")
                log.info("removed stale frame: %s", name)
            except DeviceError as exc:
                log.warning("delete %s failed: %s", name, exc)
            self.known_frames.pop(name, None)

    def _tick(self) -> None:
        with self.lock:
            winner = self._winner()

        grace = self.cfg.get("idle_grace_seconds", 180)
        now = time.time()

        # Back to the stock weather when there is no session at all, when the
        # editor that was running one has been closed, or when the last session
        # has been quiet long enough. A session whose editor is still open never
        # expires: sitting and reading an answer is not "no AI".
        closed_grace = self.cfg.get("closed_grace_seconds", 8)
        ended = winner is not None and winner.ended and (
            now - winner.updated >= closed_grace
        )
        if winner is None or ended or (
            not winner.open and winner.status == "idle" and grace >= 0
            and now - winner.updated >= grace
        ):
            if self.idle_since is None:
                self.idle_since = now
            self._restore_stock()
            return
        self.idle_since = None

        min_interval = self.cfg.get("min_push_interval", 0.6)
        if now - self.last_push < min_interval:
            return

        try:
            self._enter_ai_mode()
            path = self._ensure_frame(
                winner.provider, winner.model, winner.status, winner.usage
            )
            if path != self.shown_frame:
                self.dev.show_image(path)
                self.shown_frame = path
                self.last_push = time.time()
                log.info("screen -> %s %s [%s]",
                         winner.provider, winner.model, winner.status)
        except DeviceError as exc:
            log.warning("push failed: %s", exc)
            self.last_push = time.time()

    def run(self) -> None:
        self._recover_after_crash()
        if self.cfg.get("sync_on_start", True):
            self._sync_known_from_device()
        poll = self.cfg.get("poll_interval", 0.5)
        while not self.stopping.is_set():
            self.wake.wait(timeout=poll)
            self.wake.clear()
            try:
                self._poll_watcher()
                self._tick()
            except Exception:  # a network hiccup must not kill the daemon
                log.exception("error in the main loop")

    def shutdown(self) -> None:
        self.stopping.set()
        self._restore_stock()


# ---------------------------------------------------------------- HTTP server


def make_handler(ctrl: Controller):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # silence the default access log
            pass

        def handle_one_request(self):
            # Hooks close the connection as soon as they have the reply; the
            # reset that follows is expected, not an error worth printing.
            try:
                super().handle_one_request()
            except (ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True

        def _reply(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/status"):
                self._reply(200, ctrl.snapshot())
            elif self.path.startswith("/health"):
                self._reply(200, {"ok": True})
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._reply(400, {"error": "invalid json"})

            session = str(data.get("session") or "default")
            if self.path.startswith("/clear"):
                ctrl.clear(session)
                return self._reply(200, {"ok": True})
            if self.path.startswith("/state"):
                usage = data.get("usage")
                ctrl.update(
                    session,
                    str(data.get("provider", "")),
                    str(data.get("model", "")),
                    str(data.get("status", "idle")),
                    usage if isinstance(usage, dict) else None,
                )
                return self._reply(200, {"ok": True})
            self._reply(404, {"error": "not found"})

    return Handler


def serve(cfg: dict) -> None:
    logging.basicConfig(
        level=getattr(logging, cfg.get("log_level", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    ctrl = Controller(cfg)
    threading.Thread(target=ctrl.run, name="pusher", daemon=True).start()

    host = cfg.get("listen_host", "127.0.0.1")
    port = int(cfg.get("listen_port", 8787))
    httpd = ThreadingHTTPServer((host, port), make_handler(ctrl))
    log.info("daemon listening on http://%s:%d  (device %s)",
             host, port, cfg["device_host"])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("shutdown requested")
    finally:
        ctrl.shutdown()
        httpd.server_close()
