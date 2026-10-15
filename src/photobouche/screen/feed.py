"""Follows the booth: long-polls `/api/changes` and reports what came and went.

Network trouble is normal here: the booth reboots, Wi-Fi drops. The feed keeps
retrying with backoff and exposes `online`, so the player can carry on from its
cache meanwhile.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from urllib.parse import quote

log = logging.getLogger(__name__)

LONG_POLL = 25


class Feed(threading.Thread):
    def __init__(
        self,
        base_url: str,
        *,
        on_reset: Callable[[list[dict]], None],
        on_added: Callable[[list[dict]], None],
        on_removed: Callable[[list[str]], None],
    ) -> None:
        super().__init__(daemon=True, name="feed")
        self.base_url = base_url.rstrip("/")
        self.on_reset = on_reset
        self.on_added = on_added
        self.on_removed = on_removed
        self.seq = -1
        """-1 until the first full catalog arrives."""
        self.online = False
        self._stop = threading.Event()

    def media_url(self, collection: str, name: str) -> str:
        return f"{self.base_url}/media/{quote(collection)}/{quote(name)}"

    def poll_once(self, wait: float) -> None:
        url = f"{self.base_url}/api/changes?since={self.seq}&wait={wait:g}"
        with urllib.request.urlopen(url, timeout=wait + 10) as resp:
            data = json.load(resp)
        self.online = True
        if data["reset"]:
            self.on_reset(data["added"])
        else:
            if data["removed"]:
                self.on_removed(data["removed"])
            if data["added"]:
                self.on_added(data["added"])
        self.seq = data["seq"]

    def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                # The first call returns at once (a full listing); then long-poll.
                self.poll_once(0 if self.seq < 0 else LONG_POLL)
                backoff = 1.0
            except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
                if self.online:
                    log.warning("booth unreachable: %s", exc)
                self.online = False
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 15.0)

    def stop(self) -> None:
        self._stop.set()


def wait_online(feed: Feed, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if feed.online:
            return True
        time.sleep(0.1)
    return False
