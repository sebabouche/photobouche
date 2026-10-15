"""What is in the collections, and what changed since a given point.

Every change (a file appearing or disappearing) gets the next number in one
sequence. A screen remembers the last number it saw and asks for what came
after it, so it never misses a shot, even across its own restarts. The numbers
survive the booth restarting too: they are saved in `index.json`.

Polling, once a second: a few hundred files is nothing to `scandir`, and it
behaves the same on a Pi, a Mac and a USB stick, which inotify does not.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".m4v", ".webm"}

REMOVALS_KEPT = 2000


def kind_of(name: str) -> str | None:
    suffix = os.path.splitext(name)[1].lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    return None


@dataclass
class Item:
    collection: str
    name: str
    kind: str
    size: int
    mtime: float
    seq: int

    @property
    def id(self) -> str:
        return f"{self.collection}/{self.name}"


class Index:
    def __init__(self, collections: dict[str, Path], state_file: Path | None = None) -> None:
        self.collections = collections
        self.state_file = state_file
        self.seq = 0
        self.items: dict[str, Item] = {}
        self.removed: list[tuple[int, str]] = []
        self.floor = 0
        """Changes at or below this number are no longer known one by one."""
        self.cond = threading.Condition()
        self._load()

    # ------------------------------------------------------------ persistence

    def _load(self) -> None:
        if not self.state_file or not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text())
            self.seq = data["seq"]
            self.floor = data.get("floor", 0)
            self.items = {i["collection"] + "/" + i["name"]: Item(**i) for i in data["items"]}
            self.removed = [tuple(r) for r in data.get("removed", [])]
        except (ValueError, KeyError, TypeError, OSError) as exc:
            log.warning("index.json unreadable (%s): starting a new sequence", exc)
            self.seq, self.items, self.removed = 0, {}, []

    def _save(self) -> None:
        if not self.state_file:
            return
        data = {
            "seq": self.seq,
            "floor": self.floor,
            "items": [asdict(i) for i in self.items.values()],
            "removed": self.removed,
        }
        tmp = self.state_file.with_suffix(".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data))
        tmp.replace(self.state_file)

    # ------------------------------------------------------------------- scan

    def _list(self) -> dict[str, tuple[str, str, str, int, float]]:
        found = {}
        for collection, folder in self.collections.items():
            try:
                entries = list(os.scandir(folder))
            except FileNotFoundError:
                continue
            for entry in entries:
                if entry.name.startswith(".") or not entry.is_file():
                    continue
                kind = kind_of(entry.name)
                if kind is None:
                    continue
                st = entry.stat()
                found[f"{collection}/{entry.name}"] = (
                    collection,
                    entry.name,
                    kind,
                    st.st_size,
                    st.st_mtime,
                )
        return found

    def scan(self) -> bool:
        """Bring the index up to date with the disk. True if anything changed."""
        found = self._list()
        changed = False
        with self.cond:
            # Oldest first, so a burst of shots keeps the order it was taken in.
            for key in sorted(found.keys() - self.items.keys(), key=lambda k: found[k][4]):
                collection, name, kind, size, mtime = found[key]
                self.seq += 1
                self.items[key] = Item(collection, name, kind, size, mtime, self.seq)
                changed = True
            for key in self.items.keys() - found.keys():
                del self.items[key]
                self.seq += 1
                self.removed.append((self.seq, key))
                changed = True
            if len(self.removed) > REMOVALS_KEPT:
                drop = len(self.removed) - REMOVALS_KEPT
                self.floor = self.removed[drop - 1][0]
                self.removed = self.removed[drop:]
            if changed:
                self._save()
                self.cond.notify_all()
        return changed

    # ---------------------------------------------------------------- queries

    def changes(self, since: int) -> dict:
        """What happened after `since`. A full listing when the client is too far behind."""
        with self.cond:
            if since < 0 or since < self.floor or since > self.seq:
                # since < 0: first contact. since > seq: the booth's index was
                # reset. Either way the client gets the whole catalog. since == 0
                # is not first contact: it is a client that synced while the
                # booth was empty, and whatever came since is NEW.
                return {
                    "seq": self.seq,
                    "reset": True,
                    "added": [asdict(i) for i in sorted(self.items.values(), key=lambda i: i.seq)],
                    "removed": [],
                }
            return {
                "seq": self.seq,
                "reset": False,
                "added": [
                    asdict(i)
                    for i in sorted(self.items.values(), key=lambda i: i.seq)
                    if i.seq > since
                ],
                "removed": [key for seq, key in self.removed if seq > since],
            }

    def wait(self, since: int, timeout: float) -> dict:
        """Block until there is something after `since`, or `timeout` runs out."""
        deadline = time.monotonic() + timeout
        with self.cond:
            while self.seq == since:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.cond.wait(left)
        return self.changes(since)

    def path_of(self, collection: str, name: str) -> Path | None:
        """The file behind a media URL, or None. Only indexed files are served."""
        if f"{collection}/{name}" not in self.items:
            return None
        return self.collections[collection] / name


class Watcher(threading.Thread):
    def __init__(self, index: Index, interval: float = 1.0) -> None:
        super().__init__(daemon=True, name="watcher")
        self.index = index
        self.interval = interval
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.index.scan()
            except OSError as exc:
                log.warning("scan failed: %s", exc)
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
