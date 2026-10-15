"""The booth's HTTP server: what changed, and the files themselves.

    GET /api/changes?since=<seq>[&wait=25]   long-poll; JSON {seq, reset, added, removed}
                                             since=-1 on first contact: full catalog
    GET /media/<collection>/<name>           the file, with Range support (mpv seeks clips)
    GET /api/health

Standard library only: two endpoints do not need a framework, and the Pi has
better uses for its memory.
"""

from __future__ import annotations

import json
import logging
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from photobouche.booth.watcher import Index, Watcher

log = logging.getLogger(__name__)

MAX_WAIT = 30.0
CHUNK = 256 * 1024
CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
}
_RANGE = re.compile(r"bytes=(\d*)-(\d*)$")


def make_handler(index: Index) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "photobouche"

        def log_message(self, fmt, *args) -> None:
            log.debug("%s " + fmt, self.address_string(), *args)

        def _json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            try:
                if url.path == "/api/changes":
                    q = parse_qs(url.query)
                    since = int(q.get("since", ["-1"])[0])
                    wait = min(float(q.get("wait", ["0"])[0]), MAX_WAIT)
                    self._json(index.wait(since, wait) if wait > 0 else index.changes(since))
                elif url.path == "/api/health":
                    self._json({"ok": True, "seq": index.seq, "items": len(index.items)})
                elif url.path.startswith("/media/"):
                    self._media(url.path[len("/media/") :], head=False)
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except ValueError:
                self.send_error(HTTPStatus.BAD_REQUEST)

        def do_HEAD(self) -> None:
            url = urlparse(self.path)
            if url.path.startswith("/media/"):
                self._media(url.path[len("/media/") :], head=True)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def _media(self, rest: str, *, head: bool) -> None:
            parts = unquote(rest).split("/")
            # Only names the index knows are served: no path is ever built from the URL.
            path = index.path_of(parts[0], parts[1]) if len(parts) == 2 else None
            if path is None or not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            size = path.stat().st_size
            start, end = 0, size - 1
            status = HTTPStatus.OK
            header = self.headers.get("Range")
            if header:
                m = _RANGE.match(header.strip())
                if not m or (not m.group(1) and not m.group(2)):
                    self._unsatisfiable(size)
                    return
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                else:  # suffix range: the last N bytes
                    start = max(size - int(m.group(2)), 0)
                if start >= size or start > end:
                    self._unsatisfiable(size)
                    return
                status = HTTPStatus.PARTIAL_CONTENT

            self.send_response(status)
            self.send_header(
                "Content-Type", CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
            )
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if head:
                return
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(CHUNK, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

        def _unsatisfiable(self, size: int) -> None:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


class BoothServer:
    def __init__(
        self, collections: dict[str, Path], state_file: Path, host: str, port: int
    ) -> None:
        self.index = Index(collections, state_file)
        self.index.scan()
        self.watcher = Watcher(self.index)
        self.httpd = ThreadingHTTPServer((host, port), make_handler(self.index))
        self.httpd.daemon_threads = True

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def serve_forever(self) -> None:
        self.watcher.start()
        try:
            self.httpd.serve_forever()
        finally:
            self.watcher.stop()

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.watcher.stop()
