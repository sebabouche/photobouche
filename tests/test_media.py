from __future__ import annotations

import shutil
import subprocess
import threading

import pytest
from PIL import Image

from photobouche.booth.agent import BoothServer
from photobouche.media import check_stream, plan, prepare, slug, split
from tests.conftest import make_jpeg

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def photo(path, *, seed=0, size=(3000, 2000), exif_time=None, model=None):
    """A picture with structure (so thumbnails differ between seeds)."""
    import numpy as np

    rng = np.random.default_rng(seed)
    small = rng.integers(0, 255, (6, 9, 3), dtype=np.uint8)
    im = Image.fromarray(small).resize(size, Image.Resampling.BICUBIC)
    exif = Image.Exif()
    if model:
        exif[0x0110] = model
    if exif_time:
        exif.get_ifd(0x8769)[0x9003] = exif_time
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "JPEG", quality=92, exif=exif.tobytes())
    return path


def whatsapp_copy(original, path):
    with Image.open(original) as im:
        im = im.copy()
    im.thumbnail((1600, 1600))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "JPEG", quality=60)  # no exif: WhatsApp strips it
    return path


def test_slug_and_split():
    assert slug("Photo booth export/Zoé/IMG 1") == "photo_booth_export_zoe_img_1"
    assert split(30, 45) == [(0.0, 30)]
    parts = split(355, 45)
    assert len(parts) == 8 and all(abs(length - 355 / 8) < 1e-9 for _, length in parts)


def test_whatsapp_copy_loses_to_the_original_with_exif(tmp_path):
    src = tmp_path / "shared"
    orig = photo(src / "alice" / "IMG_0001.jpeg", seed=1, exif_time="2026:01:01 12:00:00", model="iPhone")
    whatsapp_copy(orig, src / "bob" / "IMG-20260101-WA0001.jpg")
    photo(src / "bob" / "IMG-20260101-WA0002.jpg", seed=2)  # unrelated, no exif: kept
    jobs = {j.src.relative_to(src).as_posix(): j for j in plan(src)}
    assert jobs["alice/IMG_0001.jpeg"].outputs == ["alice_img_0001.jpg"]
    wa = jobs["bob/IMG-20260101-WA0001.jpg"]
    assert wa.skipped and "same picture as alice/IMG_0001.jpeg" in wa.note
    assert not jobs["bob/IMG-20260101-WA0002.jpg"].skipped


def test_burst_shots_with_their_own_capture_time_are_not_duplicates(tmp_path):
    src = tmp_path / "shared"
    a = photo(src / "booth" / "0007.jpg", seed=3, exif_time="2026:01:01 10:00:00", model="5D")
    b = src / "booth" / "0009.jpg"
    with Image.open(a) as im:  # practically the same picture, five seconds later
        exif = im.getexif()
        exif.get_ifd(0x8769)[0x9003] = "2026:01:01 10:00:05"
        im.save(b, "JPEG", quality=92, exif=exif.tobytes())
    assert all(not j.skipped for j in plan(src))


def test_lookalikes_in_one_folder_are_a_burst_not_a_repost(tmp_path):
    src = tmp_path / "shared"
    a = photo(src / "booth" / "0052.jpg", seed=5, exif_time="2026:01:01 11:00:00", model="5D")
    with Image.open(a) as im:  # same second, same camera: the 5D has no sub-seconds
        im.save(src / "booth" / "0053.jpg", "JPEG", quality=80, exif=im.getexif().tobytes())
    assert all(not j.skipped for j in plan(src))


def test_identical_files_keep_the_shallowest_copy(tmp_path):
    src = tmp_path / "shared"
    orig = photo(src / "carol" / "IMG_1.jpeg", seed=4, exif_time="2026:01:01 09:00:00")
    (src / "Photo booth export" / "carol").mkdir(parents=True)
    shutil.copy(orig, src / "Photo booth export" / "carol" / "IMG_1.jpeg")
    jobs = {j.src.relative_to(src).as_posix(): j for j in plan(src)}
    assert not jobs["carol/IMG_1.jpeg"].skipped
    assert "identical file" in jobs["Photo booth export/carol/IMG_1.jpeg"].note


def test_thumbnails_too_small_are_dropped(tmp_path):
    src = tmp_path / "shared"
    src.mkdir()
    make_jpeg(src / "tiny.jpg", size=(250, 426))
    (job,) = plan(src)
    assert job.skipped and "too small" in job.note


def _clip(path, *, seconds, size="1920x1080", codec="libx264", extra=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440", "-t", str(seconds),
         "-c:v", codec, *extra, "-c:a", "aac", str(path)],
        check=True,
    )
    return path


@needs_ffmpeg
def test_every_clip_is_reencoded_and_long_ones_are_cut(tmp_path):
    src = tmp_path / "shared"
    _clip(src / "carol" / "hdr.mov", seconds=3, size="3840x2160", codec="libx265",
          extra=("-pix_fmt", "yuv420p10le", "-color_trc", "arib-std-b67", "-tag:v", "hvc1"))
    _clip(src / "dave" / "long.mp4", seconds=7)
    prepare(src, tmp_path / "out", max_clip=3)
    outs = sorted(p.name for p in (tmp_path / "out" / "clips").iterdir())
    assert outs == ["carol_hdr.mp4", "dave_long_part1of3.mp4", "dave_long_part2of3.mp4", "dave_long_part3of3.mp4"]
    from photobouche.media import probe

    hdr = probe(str(tmp_path / "out" / "clips" / "carol_hdr.mp4"))
    assert (hdr.codec, hdr.pix_fmt, hdr.transfer) == ("h264", "yuv420p", "bt709")
    assert hdr.display_size == (1920, 1080)
    assert hdr.problems == []
    part = probe(str(tmp_path / "out" / "clips" / "dave_long_part2of3.mp4"))
    assert abs(part.duration - 7 / 3) < 0.25


@needs_ffmpeg
def test_whatsapp_reencode_of_a_clip_is_a_duplicate(tmp_path):
    src = tmp_path / "shared"
    orig = _clip(src / "carol" / "IMG_0002.mov", seconds=4,
                 extra=("-metadata", "com.apple.quicktime.creationdate=2026-01-01T12:00:00+0100"))
    wa = src / "bob" / "VID-20260101-WA0001.mp4"
    wa.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(orig), "-vf", "scale=1024:576",
                    "-c:v", "libx264", "-crf", "30", "-map_metadata", "-1", str(wa)], check=True)
    jobs = {j.src.relative_to(src).as_posix(): j for j in plan(src)}
    assert not jobs["carol/IMG_0002.mov"].skipped
    assert "same clip as carol/IMG_0002.mov" in jobs["bob/VID-20260101-WA0001.mp4"].note


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
