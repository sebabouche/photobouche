from __future__ import annotations

import struct
from pathlib import Path

import pytest
from PIL import Image


def make_jpeg(path: Path, size=(600, 400), colour=(200, 40, 40), orientation: int | None = None) -> Path:
    im = Image.new("RGB", size, colour)
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
        im.save(path, "JPEG", exif=exif.tobytes())
    else:
        im.save(path, "JPEG")
    return path


def make_cr2(path: Path, jpeg: bytes, orientation: int = 1) -> Path:
    """A minimal little-endian TIFF shaped like a CR2: IFD0 strip = full-size JPEG."""
    entries = 3
    ifd_offset = 16  # after the 8-byte header and the 8-byte 'CR' signature area
    data_offset = ifd_offset + 2 + entries * 12 + 4
    header = b"II" + struct.pack("<HI", 42, ifd_offset) + b"CR\x02\x00" + b"\x00" * 4
    ifd = struct.pack("<H", entries)
    ifd += struct.pack("<HHII", 0x0111, 4, 1, data_offset)
    ifd += struct.pack("<HHI", 0x0112, 3, 1) + struct.pack("<HH", orientation, 0)
    ifd += struct.pack("<HHII", 0x0117, 4, 1, len(jpeg))
    ifd += struct.pack("<I", 0)
    path.write_bytes(header + ifd + jpeg + b"\x00" * 1024)  # trailing "raw data"
    return path


@pytest.fixture
def jpeg_bytes(tmp_path) -> bytes:
    return make_jpeg(tmp_path / "src.jpg", size=(3000, 2000)).read_bytes()
