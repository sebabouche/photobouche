"""What plays next. Pure logic: no clock, no I/O, a seeded RNG — so it is testable.

Two queues:

* **fresh** — shots that just came off the camera. They jump the line, in the
  order they were taken, each for `fresh_seconds`.
* **rotation** — everything else, picked at random with weights: recent live
  shots come up more often, a clip comes every `clip_every` images, and nothing
  shown in the last `no_repeat` picks comes back.
"""

from __future__ import annotations

import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Media:
    id: str
    """`collection/name`: unique across both Pis."""

    kind: str
    """`image` or `video`."""

    source: str
    """Where the player reads it: a booth URL or a local path."""

    mtime: float = 0.0
    live: bool = False
    """Taken by the booth camera (as opposed to making-of material)."""


@dataclass
class Pick:
    media: Media
    duration: float | None
    """Seconds to show it. None: until the clip ends."""

    fresh: bool


class Scheduler:
    def __init__(
        self,
        *,
        fresh_seconds: float = 8.0,
        image_seconds: float = 6.0,
        clip_every: int = 5,
        recent_seconds: float = 2 * 3600,
        recent_weight: float = 3.0,
        no_repeat: int = 20,
        seed: int | None = None,
    ) -> None:
        self.fresh_seconds = fresh_seconds
        self.image_seconds = image_seconds
        self.clip_every = clip_every
        self.recent_seconds = recent_seconds
        self.recent_weight = recent_weight
        self.no_repeat = no_repeat
        self.rng = random.Random(seed)
        self.items: dict[str, Media] = {}
        self.fresh: deque[str] = deque()
        self.history: deque[str] = deque(maxlen=max(no_repeat, 1))
        self.images_since_clip = 0

    # ---------------------------------------------------------------- catalog

    def add(self, media: Media, *, fresh: bool = False) -> None:
        self.items[media.id] = media
        if fresh and media.kind == "image" and media.id not in self.fresh:
            self.fresh.append(media.id)

    def remove(self, media_id: str) -> None:
        self.items.pop(media_id, None)
        if media_id in self.fresh:
            self.fresh.remove(media_id)

    def replace_source(self, prefix: str, keep: set[str]) -> None:
        """Drop every item under `prefix` not in `keep` (after a booth resync)."""
        for media_id in [m for m in self.items if m.startswith(prefix) and m not in keep]:
            self.remove(media_id)

    @property
    def has_fresh(self) -> bool:
        return bool(self.fresh)

    # ------------------------------------------------------------------ picks

    def next(self, now: float, usable: Callable[[Media], bool] = lambda m: True) -> Pick | None:
        """The next thing to play, or None when nothing usable is known."""
        while self.fresh:
            media = self.items.get(self.fresh.popleft())
            if media is not None and usable(media):
                self._played(media)
                return Pick(media, self.fresh_seconds, fresh=True)

        pool = [m for m in self.items.values() if usable(m)]
        if not pool:
            return None
        images = [m for m in pool if m.kind == "image"]
        videos = [m for m in pool if m.kind == "video"]

        want_clip = bool(videos) and (
            not images or (self.clip_every > 0 and self.images_since_clip >= self.clip_every)
        )
        candidates = videos if want_clip else images
        media = self._choose(candidates, now)
        self._played(media)
        if media.kind == "video":
            self.images_since_clip = 0
            return Pick(media, None, fresh=False)
        self.images_since_clip += 1
        return Pick(media, self.image_seconds, fresh=False)

    def _choose(self, candidates: list[Media], now: float) -> Media:
        recent = set(self.history)
        fresh_enough = [m for m in candidates if m.id not in recent]
        if not fresh_enough:
            # A small pool: anything but the very last one.
            last = self.history[-1] if self.history else None
            fresh_enough = [m for m in candidates if m.id != last] or candidates
        weights = [
            self.recent_weight if m.live and now - m.mtime < self.recent_seconds else 1.0
            for m in fresh_enough
        ]
        return self.rng.choices(fresh_enough, weights=weights, k=1)[0]

    def _played(self, media: Media) -> None:
        if media.id in self.history:
            self.history.remove(media.id)
        self.history.append(media.id)
