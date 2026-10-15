"""A long-lived mpv, driven over its JSON IPC socket.

mpv shows images and plays clips with the Pi 4's hardware H.264 decoder, so no
picture ever passes through Python. One process for the whole show: starting
mpv per item would flash the desktop between items.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)


class MpvError(Exception):
    pass


class Mpv:
    def __init__(self, args: list[str], *, name: str = "mpv") -> None:
        exe = shutil.which("mpv")
        if exe is None:
            raise MpvError("mpv not found: sudo apt install mpv (or brew install mpv)")
        self.name = name
        self._dir = tempfile.mkdtemp(prefix="photobouche-")
        self.socket_path = str(Path(self._dir) / f"{name}.sock")
        self.proc = subprocess.Popen(
            [exe, "--idle=yes", f"--input-ipc-server={self.socket_path}", *args],
            stdin=subprocess.DEVNULL,
        )
        self._sock = self._connect()
        self._lock = threading.Lock()
        self._req = 0
        self._observed = 0
        self.eof = False
        self.error = False
        self.properties: dict[str, object] = {}
        threading.Thread(target=self._read, daemon=True, name=f"{name}-ipc").start()

    def _connect(self) -> socket.socket:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise MpvError(f"mpv exited with code {self.proc.returncode}")
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(self.socket_path)
                return sock
            except OSError:
                sock.close()
                time.sleep(0.05)
        raise MpvError("mpv did not open its IPC socket")

    def _read(self) -> None:
        buf = b""
        while True:
            try:
                data = self._sock.recv(65536)
            except OSError:
                return
            if not data:
                return
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                self._event(msg)

    def _event(self, msg: dict) -> None:
        event = msg.get("event")
        if event == "property-change":
            self.properties[msg.get("name")] = msg.get("data")
            if msg.get("name") == "eof-reached" and msg.get("data") is True:
                self.eof = True
        elif event == "end-file" and msg.get("reason") == "error":
            log.warning("mpv could not play: %s", msg.get("file_error", "?"))
            self.error = True

    def command(self, *args) -> None:
        with self._lock:
            self._req += 1
            line = json.dumps({"command": list(args), "request_id": self._req}) + "\n"
            try:
                self._sock.sendall(line.encode())
            except OSError as exc:
                raise MpvError(f"mpv IPC lost: {exc}") from exc

    def observe(self, prop: str) -> None:
        self._observed += 1
        self.command("observe_property", self._observed, prop)

    def set(self, prop: str, value) -> None:
        self.command("set_property", prop, value)

    def load(self, source: str) -> None:
        self.eof = False
        self.error = False
        self.command("loadfile", source, "replace")

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def close(self) -> None:
        with contextlib.suppress(MpvError):
            self.command("quit")
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        with contextlib.suppress(OSError):
            self._sock.close()
        shutil.rmtree(self._dir, ignore_errors=True)


def show_player(*, fullscreen: bool, audio_device: str = "") -> Mpv:
    args = [
        "--force-window=yes",
        "--keep-open=yes",
        "--image-display-duration=inf",
        "--hwdec=auto-safe",
        "--background=color",
        "--background-color=#000000",
        "--osc=no",
        "--osd-level=0",
        "--cursor-autohide=always",
        "--no-input-default-bindings",
        "--input-vo-keyboard=no",
        "--no-terminal",
        # Clips stream from the booth: buffer a few seconds against Wi-Fi hiccups.
        "--cache=yes",
        "--demuxer-readahead-secs=5",
        "--title=photobouche",
    ]
    if fullscreen:
        args.append("--fs")
    if audio_device:
        args.append(f"--audio-device={audio_device}")
    player = Mpv(args, name="show")
    player.observe("eof-reached")
    return player


def music_player(folder: Path, volume: int, audio_device: str = "") -> Mpv:
    args = [
        "--no-video",
        "--shuffle",
        "--loop-playlist=inf",
        f"--volume={volume}",
        "--no-terminal",
        str(folder),
    ]
    if audio_device:
        args.append(f"--audio-device={audio_device}")
    return Mpv(args, name="music")
