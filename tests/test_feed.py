"""Feed against a real booth server: the first shot of the night must arrive as new."""

from __future__ import annotations

import threading
import time

from photobouche.booth.agent import BoothServer
from photobouche.screen.feed import Feed


def test_feed_reports_reset_then_new_shots_then_removals(tmp_path):
    live = tmp_path / "display"
    live.mkdir()
    srv = BoothServer({"live": live}, tmp_path / "index.json", "127.0.0.1", 0)
    srv.watcher.interval = 0.05
    srv.watcher.start()
    threading.Thread(target=srv.httpd.serve_forever, daemon=True).start()
    events = []
    feed = Feed(
        f"http://127.0.0.1:{srv.port}",
        on_reset=lambda items: events.append(("reset", [i["name"] for i in items])),
        on_added=lambda items: events.append(("added", [i["name"] for i in items])),
        on_removed=lambda ids: events.append(("removed", ids)),
    )
    try:
        feed.poll_once(0)  # first contact while the booth is empty
        assert events == [("reset", [])]
        (live / "shot1.jpg").write_bytes(b"x")
        feed.poll_once(5)
        assert events[-1] == ("added", ["shot1.jpg"])
        (live / "shot1.jpg").unlink()
        t = time.monotonic()
        feed.poll_once(5)
        assert events[-1] == ("removed", ["live/shot1.jpg"])
        assert time.monotonic() - t < 2
    finally:
        srv.shutdown()
