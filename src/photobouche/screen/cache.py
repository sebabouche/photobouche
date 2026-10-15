"""Booth images, kept on the slideshow Pi's SD card.

Images are small (≈400 KB at 1920px) and shown again and again, so they are
downloaded once and kept: that is also what lets the show go on when the booth
drops off the network. Clips are not cached — they stream from the booth.

Least recently shown goes first once the folder passes its size limit.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import threading
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)


class ImageCache:
    def __init__(self, folder: Path, limit_mb: int = 500) -> None:
        self.folder = folder
        self.limit = limit_mb * 1024 * 1024
        folder.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._queue: queue.Queue[tuple[str, str]] = queue.Queue()
        threading.Thread(target=self._prefetcher, daemon=True, name="prefetch").start()

    def path_for(self, media_id: str) -> Path:
        collection, name = media_id.split("/", 1)
        return self.folder / collection / name

    def has(self, media_id: str) -> bool:
        return self.path_for(media_id).exists()

    def get(self, media_id: str, url: str, timeout: float = 10.0) -> Path | None:
        """The local file, downloading it if needed. None if that fails."""
        path = self.path_for(media_id)
        if path.exists():
            with contextlib.suppress(OSError):
                os.utime(path)  # mark as recently used
            return path
        try:
            self._download(url, path, timeout)
        except OSError as exc:
            log.info("could not fetch %s: %s", media_id, exc)
            return None
        self._evict()
        return path

    def prefetch(self, media_id: str, url: str) -> None:
        if not self.has(media_id):
            self._queue.put((media_id, url))

    def discard(self, media_id: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path_for(media_id).unlink()

    def _prefetcher(self) -> None:
        while True:
            media_id, url = self._queue.get()
            if not self.has(media_id):
                self.get(media_id, url)

    def _download(self, url: str, path: Path, timeout: float) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{threading.get_ident()}.tmp")
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp, open(tmp, "wb") as f:
                while chunk := resp.read(256 * 1024):
                    f.write(chunk)
            tmp.replace(path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()

    def _evict(self) -> None:
        with self._lock:
            files = [p for p in self.folder.rglob("*") if p.is_file() and not p.name.startswith(".")]
            total = sum(p.stat().st_size for p in files)
            if total <= self.limit:
                return
            for p in sorted(files, key=lambda p: p.stat().st_mtime):
                if total <= self.limit * 0.9:
                    break
                total -= p.stat().st_size
                p.unlink()
            log.info("cache trimmed to %.0f MB", total / 1e6)
