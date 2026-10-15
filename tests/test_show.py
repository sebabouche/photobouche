"""The slideshow loop against a real booth server, with a stand-in for mpv."""

from __future__ import annotations

import threading
import time

from photobouche import config
from photobouche.booth.agent import BoothServer
from photobouche.screen import show as show_mod
from tests.conftest import make_jpeg


class FakePlayer:
    def __init__(self) -> None:
        self.loads: list[tuple[float, str]] = []
        self.eof = False
        self.error = False
        self.alive = True

    def load(self, source: str) -> None:
        self.loads.append((time.monotonic(), source))

    def set(self, *a) -> None:
        pass

    def close(self) -> None:
        self.alive = False


def test_new_shot_cuts_into_the_rotation(tmp_path, monkeypatch):
    live, making_of = tmp_path / "display", tmp_path / "making_of"
    live.mkdir()
    making_of.mkdir()
    for i in range(3):
        make_jpeg(making_of / f"m{i}.jpg")
    srv = BoothServer(
        {"live": live, "making_of": making_of}, tmp_path / "index.json", "127.0.0.1", 0
    )
    srv.watcher.interval = 0.05
    srv.watcher.start()
    threading.Thread(target=srv.httpd.serve_forever, daemon=True).start()

    cfg = config.load(None, root=tmp_path)
    cfg.show.booth_url = f"http://127.0.0.1:{srv.port}"
    cfg.show.cache_dir = tmp_path / "cache"
    cfg.show.image_seconds = 30.0  # long: only a cut can end it in time
    cfg.show.fresh_seconds = 1.0
    player = FakePlayer()
    monkeypatch.setattr(show_mod, "show_player", lambda **kw: player)
    show = show_mod.Show(cfg)
    threading.Thread(target=show.run, daemon=True).start()
    try:
        deadline = time.monotonic() + 5
        while not player.loads and time.monotonic() < deadline:
            time.sleep(0.05)
        assert player.loads, "nothing played"
        assert "making_of" in player.loads[0][1]

        landed = time.monotonic()
        make_jpeg(live / "20261016-210000_IMG_0001.jpg")
        while len(player.loads) < 2 and time.monotonic() < landed + 5:
            time.sleep(0.05)
        when, source = player.loads[1]
        assert "IMG_0001" in source
        assert source.startswith(str(cfg.show.cache_dir))  # shown from disk, not streamed
        assert when - landed < 2.0
    finally:
        player.alive = False
        show.feed.stop()
        srv.shutdown()
