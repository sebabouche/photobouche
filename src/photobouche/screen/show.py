"""`photobouche screen --profile show`: the slideshow, with sound.

Sources: the booth (live shots, making-of photos and clips, over HTTP) and,
optionally, clips stored on this Pi. A new shot cuts in within a second or two
of the feed reporting it, whatever is playing.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from photobouche.booth.watcher import kind_of
from photobouche.config import Config
from photobouche.screen.cache import ImageCache
from photobouche.screen.feed import Feed
from photobouche.screen.mpv import Mpv, MpvError, music_player, show_player
from photobouche.screen.scheduler import Media, Pick, Scheduler

log = logging.getLogger(__name__)

LIVE = "live"
LOCAL = "local"
MAX_CLIP_SECONDS = 15 * 60
"""A clip that never reports its end (a stalled stream) is abandoned after this."""


class Show:
    def __init__(self, cfg: Config) -> None:
        sc = cfg.show
        self.cfg = cfg
        self.scheduler = Scheduler(
            fresh_seconds=sc.fresh_seconds,
            image_seconds=sc.image_seconds,
            clip_every=sc.clip_every,
            recent_seconds=sc.recent_hours * 3600,
            recent_weight=sc.recent_weight,
            no_repeat=sc.no_repeat,
            seed=sc.seed,
        )
        # The feed thread edits the catalog while the main thread picks from it.
        self.lock = threading.Lock()
        self.cache = ImageCache(sc.cache_dir, sc.cache_mb)
        self.feed = Feed(
            sc.booth_url,
            on_reset=self._on_reset,
            on_added=self._on_added,
            on_removed=self._on_removed,
        )
        self._load_local_clips()

    # ------------------------------------------------------------ catalog

    def _media(self, item: dict) -> Media:
        collection, name = item["collection"], item["name"]
        return Media(
            id=f"{collection}/{name}",
            kind=item["kind"],
            source=self.feed.media_url(collection, name),
            mtime=item["mtime"],
            live=collection == LIVE,
        )

    def _on_reset(self, items: list[dict]) -> None:
        with self.lock:
            self._reset(items)

    def _reset(self, items: list[dict]) -> None:
        media = [self._media(i) for i in items]
        keep = {m.id for m in media}
        for prefix in {m.id.split("/", 1)[0] + "/" for m in self.scheduler.items.values()}:
            if not prefix.startswith(LOCAL + "/"):
                self.scheduler.replace_source(prefix, keep)
        for m in media:
            self.scheduler.add(m)
        log.info("booth catalog: %d items", len(media))

    def _on_added(self, items: list[dict]) -> None:
        for item in items:
            m = self._media(item)
            fresh = m.live
            if m.kind == "image":
                # Fetch now, so the cut happens on a picture already on disk.
                self.cache.prefetch(m.id, m.source)
            with self.lock:
                self.scheduler.add(m, fresh=fresh)
            log.info("%s %s", "new shot" if fresh else "added", m.id)

    def _on_removed(self, ids: list[str]) -> None:
        for media_id in ids:
            with self.lock:
                self.scheduler.remove(media_id)
            self.cache.discard(media_id)
            log.info("removed %s", media_id)

    def _load_local_clips(self) -> None:
        folder = self.cfg.show.local_clips
        if not folder or not Path(folder).is_dir():
            return
        for p in sorted(Path(folder).iterdir()):
            if not p.name.startswith(".") and kind_of(p.name):
                self.scheduler.add(
                    Media(
                        id=f"{LOCAL}/{p.name}",
                        kind=kind_of(p.name),
                        source=str(p),
                        mtime=p.stat().st_mtime,
                    )
                )

    def _usable(self, m: Media) -> bool:
        if m.id.startswith(LOCAL + "/"):
            return True
        if m.kind == "image":
            return self.feed.online or self.cache.has(m.id)
        return self.feed.online

    # --------------------------------------------------------------- play

    def _resolve(self, pick: Pick) -> str | None:
        m = pick.media
        if m.id.startswith(LOCAL + "/") or m.kind == "video":
            return m.source
        path = self.cache.get(m.id, m.source)
        return str(path) if path else None

    def run(self) -> None:
        sc = self.cfg.show
        self.feed.start()
        player = show_player(fullscreen=sc.fullscreen, audio_device=sc.audio_device)
        music: Mpv | None = None
        if sc.music_dir and Path(sc.music_dir).is_dir():
            music = music_player(Path(sc.music_dir), sc.music_volume, sc.audio_device)
        try:
            while player.alive:
                with self.lock:
                    pick = self.scheduler.next(time.time(), self._usable)
                if pick is None:
                    time.sleep(0.5)
                    continue
                source = self._resolve(pick)
                if source is None:
                    continue
                log.debug("play %s (%s)", pick.media.id, "fresh" if pick.fresh else "rotation")
                player.load(source)
                if music is not None:
                    music.set(
                        "volume", sc.music_duck_volume if pick.media.kind == "video" else sc.music_volume
                    )
                self._hold(player, pick)
        except MpvError as exc:
            log.error("%s", exc)
        finally:
            self.feed.stop()
            player.close()
            if music is not None:
                music.close()

    def _hold(self, player: Mpv, pick: Pick) -> None:
        start = time.monotonic()
        limit = pick.duration if pick.duration is not None else MAX_CLIP_SECONDS
        while player.alive:
            time.sleep(0.05)
            elapsed = time.monotonic() - start
            if pick.media.kind == "video" and (player.eof or player.error):
                return
            if elapsed >= limit:
                return
            # A new shot cuts in — except over another new shot, which keeps its time.
            if self.scheduler.has_fresh and not pick.fresh and elapsed > 0.3:
                return
