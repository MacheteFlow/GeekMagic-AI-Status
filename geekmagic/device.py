"""HTTP client for the GeekMagic SmallTV-Ultra (stock firmware, ESP8266).

Standard library only: the hooks have to start in a few milliseconds and cannot
depend on installed packages.

Every endpoint used here is one the original firmware already exposes: nothing
on the device is modified and its web server is never replaced.
"""

from __future__ import annotations

import http.client
import json
import mimetypes
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid

# The ESP8266 web server handles one connection at a time, so serialise.
_LOCK = threading.RLock()

# Stock firmware themes, numbered as on the Settings page.
THEME_PHOTO_ALBUM = 3


class DeviceError(RuntimeError):
    pass


class SmallTVUltra:
    def __init__(self, host: str, timeout: float = 8.0, retries: int = 2):
        self.host = host.strip().rstrip("/")
        if not self.host.startswith("http"):
            self.host = "http://" + self.host
        self.timeout = timeout
        self.retries = retries
        # Raised by _request when the server truncated the response.
        self.last_partial = False

    # ---------------------------------------------------------------- low level

    def _request(self, req: urllib.request.Request) -> bytes:
        """Perform the request with retries.

        On larger files (js/jquery.min.js) the ESP8266 server sometimes closes
        the connection before sending the Content-Length it announced. When that
        happens we keep the bytes we did receive instead of losing everything,
        and flag them as partial for the caller to record.
        """
        last = None
        best_partial = b""
        for _ in range(self.retries + 1):
            try:
                with _LOCK:
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        return resp.read()
            except http.client.IncompleteRead as exc:
                last = exc
                if len(exc.partial) > len(best_partial):
                    best_partial = exc.partial
            except (urllib.error.URLError, OSError) as exc:  # timeouts included
                last = exc
        if best_partial:
            self.last_partial = True
            return best_partial
        raise DeviceError(f"{req.full_url}: {last}") from last

    def get(self, path: str) -> bytes:
        if not path.startswith("/"):
            path = "/" + path
        return self._request(urllib.request.Request(self.host + path))

    def get_text(self, path: str) -> str:
        return self.get(path).decode("utf-8", "replace")

    def get_json(self, path: str) -> dict:
        raw = self.get_text(path)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DeviceError(f"{path}: non-JSON response: {raw[:120]!r}") from exc

    # ---------------------------------------------------------------- info

    def version(self) -> dict:
        """{'m': 'SmallTV-Ultra', 'v': 'Ultra-V9.0.51'}"""
        return self.get_json("/v.json")

    def space(self) -> dict:
        """{'total': ..., 'free': ...} in bytes."""
        return self.get_json("/space.json")

    def ping(self) -> bool:
        try:
            return "m" in self.version()
        except DeviceError:
            return False

    # ---------------------------------------------------------------- filesystem

    _FILE_RE = re.compile(r"href='([^']+)'>([^<]+)</a></td><td>(\d+)</td>")

    def list_files(self, directory: str = "/image") -> list[dict]:
        """List files. The firmware answers with an HTML table, not JSON.

        Scraping markup is brittle by nature: a firmware update that changes the
        table would make the pattern match nothing, and an empty list is a
        perfectly ordinary answer, so the failure would pass silently. Callers
        would conclude the device holds no frames and upload every one again,
        every time -- exactly the flash wear the caching exists to avoid.

        So an answer that clearly lists files but yields none is treated as a
        failure rather than as an empty directory.
        """
        html = self.get_text("/filelist?dir=" + directory)
        out = []
        for path, name, size_kb in self._FILE_RE.findall(html):
            out.append({"path": path, "name": name, "kb": int(size_kb)})
        if not out and "href=" in html:
            raise DeviceError(
                f"/filelist?dir={directory}: the listing has entries but none "
                f"could be read; the firmware's markup may have changed"
            )
        return out

    def download(self, path: str) -> bytes:
        return self.get(path)

    def delete(self, path: str) -> None:
        self.get("/delete?file=" + urllib.parse.quote(path, safe=""))

    def upload(self, directory: str, filename: str, data: bytes) -> None:
        """Multipart POST to /doUpload, exactly like the Pictures page does."""
        if not directory.endswith("/"):
            directory += "/"
        boundary = "----gmai" + uuid.uuid4().hex
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        body = b"".join([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
            f"Content-Type: {ctype}\r\n\r\n".encode(),
            data,
            f"\r\n--{boundary}--\r\n".encode(),
        ])
        url = self.host + "/doUpload?dir=" + urllib.parse.quote(directory)
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            },
        )
        self._request(req)

    # ---------------------------------------------------------------- screen

    def show_image(self, path: str) -> None:
        """Display a file already present on the device, full screen."""
        self.get("/set?img=" + urllib.parse.quote(path, safe=""))

    def set_theme(self, theme: int) -> None:
        self.get(f"/set?theme={int(theme)}")

    def get_theme(self) -> int:
        return int(self.get_json("/app.json").get("theme", 1))

    def set_brightness(self, value: int) -> None:
        self.get(f"/set?brt={max(0, min(100, int(value)))}")

    def get_brightness(self) -> int:
        return int(self.get_json("/brt.json").get("brt", 100))

    def set_autoplay(self, enabled: bool, interval_s: int = 30) -> None:
        """The slideshow must be off, or it would overwrite our frame."""
        self.get(f"/set?i_i={int(interval_s)}&autoplay={1 if enabled else 0}")

    def get_album(self) -> dict:
        return self.get_json("/album.json")

    def theme_list(self) -> dict:
        """Automatic theme rotation: must be off while we are driving the screen."""
        return self.get_json("/theme_list.json")

    def set_theme_rotation(self, enabled: bool, interval_s: int | None = None) -> None:
        current = self.theme_list()
        params = {
            "theme_list": current.get("list", "0,0,0,0,0,0,0"),
            "sw_en": 1 if enabled else 0,
            "sw_i": interval_s if interval_s is not None else current.get("sw_i", 15),
        }
        self.get("/set?" + urllib.parse.urlencode(params))
