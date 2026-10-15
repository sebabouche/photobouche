from __future__ import annotations

import threading

from PIL import Image

from photobouche.booth.agent import BoothServer
from photobouche.media import check_stream, prepare
from tests.conftest import make_jpeg


def test_prepare_resizes_images_copies_clips_and_ignores_the_rest(tmp_path):
    src = tmp_path / "shared"
    (src / "day1").mkdir(parents=True)
    (src / "day2").mkdir()
    make_jpeg(src / "day1" / "IMG_0001.jpg", size=(6000, 4000))
    make_jpeg(src / "day2" / "IMG_0001.jpg", size=(4000, 6000))  # same name, other folder
    (src / "day1" / "clip.mp4").write_bytes(b"\x00" * 100)
    (src / "day1" / "phone.HEIC").write_bytes(b"x")
    (src / ".DS_Store").write_bytes(b"x")

    dest = tmp_path / "prepared"
    counts = prepare(src, dest)
    assert counts["images"] == 2
    assert counts["clips"] == 1
    assert counts["ignored"] == 1
    names = sorted(p.name for p in (dest / "making_of").iterdir())
    assert names == ["day1_IMG_0001.jpg", "day2_IMG_0001.jpg"]
    with Image.open(dest / "making_of" / "day2_IMG_0001.jpg") as im:
        assert im.size == (1280, 1920)
    assert (dest / "clips" / "day1_clip.mp4").exists()

    again = prepare(src, dest)
    assert again["images"] == 0 and again["clips"] == 0 and again["skipped"] == 3


def test_check_stream_measures_every_clip(tmp_path):
    clips = tmp_path / "clips"
    clips.mkdir()
    (clips / "a b.mp4").write_bytes(b"\x00" * 3_000_000)
    srv = BoothServer({"clips": clips}, tmp_path / "index.json", "127.0.0.1", 0)
    threading.Thread(target=srv.httpd.serve_forever, daemon=True).start()
    try:
        results = check_stream(f"http://127.0.0.1:{srv.port}", sample_mb=1)
    finally:
        srv.shutdown()
    assert [r.name for r in results] == ["clips/a b.mp4"]
    assert results[0].mbps > 1
