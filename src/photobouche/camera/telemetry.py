"""The camera daemon's local socket: live readings out, commands in.

JSON lines over a Unix socket. The daemon writes ~30 readings a second to every
connected screen; a screen writes commands back:

    {"cmd": "shoot"}                       fire now (same as the remote)
    {"cmd": "scream_mode", "value": true}
    {"cmd": "threshold", "delta": 1.0}     dB, relative to the current value

A slow or vanished client is dropped, never waited for: the camera comes first.
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import socket
import threading
from pathlib import Path

log = logging.getLogger(__name__)


class TelemetryServer:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.commands: queue.Queue[dict] = queue.Queue()
        self._clients: list[socket.socket] = []
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(path))
        self._sock.listen(4)
        self._closed = False
        threading.Thread(target=self._accept, daemon=True, name="telemetry").start()

    def _accept(self) -> None:
        while not self._closed:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            conn.settimeout(0.05)
            with self._lock:
                self._clients.append(conn)
            threading.Thread(target=self._read, args=(conn,), daemon=True).start()

    def _read(self, conn: socket.socket) -> None:
        buf = b""
        while not self._closed:
            try:
                data = conn.recv(4096)
            except TimeoutError:
                continue
            except OSError:
                break
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict) and "cmd" in msg:
                    self.commands.put(msg)
        self._drop(conn)

    def _drop(self, conn: socket.socket) -> None:
        with self._lock:
            if conn in self._clients:
                self._clients.remove(conn)
        with contextlib.suppress(OSError):
            conn.close()

    def broadcast(self, payload: dict) -> None:
        line = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        with self._lock:
            clients = list(self._clients)
        for conn in clients:
            try:
                conn.sendall(line)
            except OSError:
                self._drop(conn)

    def close(self) -> None:
        self._closed = True
        with contextlib.suppress(OSError):
            self._sock.close()
        for conn in list(self._clients):
            self._drop(conn)
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()


class TelemetryClient:
    """The screen's end. Keeps the latest reading; reconnects on its own."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.latest: dict = {}
        self.connected = False
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True, name="telemetry-client").start()

    def _run(self) -> None:
        import time

        while True:
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(str(self.path))
            except OSError:
                self.connected = False
                time.sleep(1.0)
                continue
            with self._lock:
                self._sock = sock
            self.connected = True
            buf = b""
            try:
                while True:
                    data = sock.recv(65536)
                    if not data:
                        break
                    buf += data
                    lines = buf.split(b"\n")
                    buf = lines.pop()
                    for line in reversed(lines):
                        with contextlib.suppress(ValueError):
                            self.latest = json.loads(line)
                            break
            except OSError:
                pass
            self.connected = False
            with self._lock:
                self._sock = None
            with contextlib.suppress(OSError):
                sock.close()
            time.sleep(0.5)

    def send(self, payload: dict) -> bool:
        with self._lock:
            sock = self._sock
        if sock is None:
            return False
        try:
            sock.sendall((json.dumps(payload) + "\n").encode())
            return True
        except OSError:
            return False
