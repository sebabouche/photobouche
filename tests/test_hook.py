from __future__ import annotations

from PIL import Image

from photobouche.camera.hook import Developer, tiff_ifd0_jpeg
from tests.conftest import make_cr2, make_jpeg


def test_reads_the_jpeg_inside_a_cr2(tmp_path, jpeg_bytes):
    cr2 = make_cr2(tmp_path / "IMG_0001.CR2", jpeg_bytes, orientation=6)
    data, orientation = tiff_ifd0_jpeg(cr2)
    assert data == jpeg_bytes
    assert orientation == 6


def test_cr2_becomes_upright_display_jpeg_and_is_archived(tmp_path, jpeg_bytes):
    dl = tmp_path / "incoming"
    dl.mkdir()
    cr2 = make_cr2(dl / "IMG_0001.CR2", jpeg_bytes, orientation=6)  # rotated 90° CW
    dev = Developer(tmp_path / "display", tmp_path / "archive", size=1920)
    out = dev.develop(cr2, when=1_700_000_000)
    assert out is not None and out.exists()
    with Image.open(out) as im:
        assert max(im.size) == 1920
        assert im.size[1] > im.size[0]  # portrait after rotation
    assert not cr2.exists()
    assert len(list((tmp_path / "archive").rglob("IMG_0001.CR2"))) == 1


def test_raw_plus_jpeg_makes_one_display_image(tmp_path, jpeg_bytes):
    dl = tmp_path / "incoming"
    dl.mkdir()
    dev = Developer(tmp_path / "display", tmp_path / "archive")
    first = dev.develop(make_cr2(dl / "IMG_0002.CR2", jpeg_bytes), when=1_700_000_000)
    second = dev.develop(make_jpeg(dl / "IMG_0002.JPG"), when=1_700_000_001)
    assert first is not None
    assert second is None
    assert len(list((tmp_path / "display").iterdir())) == 1
    assert len(list((tmp_path / "archive").rglob("IMG_0002.*"))) == 2


def test_jpeg_exif_orientation_applied(tmp_path):
    dl = tmp_path / "incoming"
    dl.mkdir()
    src = make_jpeg(dl / "IMG_0003.JPG", size=(800, 400), orientation=8)
    out = Developer(tmp_path / "display", tmp_path / "archive").develop(src)
    with Image.open(out) as im:
        assert im.size == (400, 800)


def test_display_written_atomically_no_temp_left(tmp_path):
    dl = tmp_path / "incoming"
    dl.mkdir()
    Developer(tmp_path / "display", tmp_path / "archive").develop(make_jpeg(dl / "a.jpg"))
    assert [p.name for p in (tmp_path / "display").iterdir() if p.name.startswith(".")] == []


def test_unreadable_file_is_still_archived(tmp_path):
    dl = tmp_path / "incoming"
    dl.mkdir()
    bad = dl / "IMG_0004.CR2"
    bad.write_bytes(b"not a raw at all")
    out = Developer(tmp_path / "display", tmp_path / "archive").develop(bad)
    assert out is None
    assert len(list((tmp_path / "archive").rglob("IMG_0004.CR2"))) == 1
