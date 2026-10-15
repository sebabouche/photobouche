"""From a file off the camera to a display JPEG.

A 5D Mk II CR2 is 20-25 MB, and decoding the raw data on a Pi takes seconds. It
never has to be decoded: a CR2 is a TIFF whose first directory (IFD0) holds a
full-size JPEG the camera rendered itself. Reading it is a few `seek`s. exiv2
and dcraw do the same job and are kept as fallbacks for files this parser does
not understand.

With the camera on RAW + JPEG, each shot arrives as two files with the same
stem. Whichever lands first makes the display image; the second is only archived.
"""

from __future__ import annotations

import io
import logging
import shutil
import struct
import subprocess
import tempfile
import time
from collections import deque
from pathlib import Path

from PIL import Image, ImageOps

log = logging.getLogger(__name__)

RAW_SUFFIXES = {".cr2", ".cr3", ".nef", ".arw", ".dng"}
JPEG_SUFFIXES = {".jpg", ".jpeg"}
OTHER_IMAGE_SUFFIXES = {".png", ".webp", ".tif", ".tiff"}

# EXIF orientation value -> PIL transpose, as in ImageOps.exif_transpose.
_ORIENT = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


class HookError(Exception):
    pass


def tiff_ifd0_jpeg(path: Path) -> tuple[bytes, int]:
    """The JPEG in a CR2's IFD0 and the orientation tag stored next to it."""
    with open(path, "rb") as f:
        header = f.read(8)
        if header[:2] == b"II":
            end = "<"
        elif header[:2] == b"MM":
            end = ">"
        else:
            raise HookError(f"{path.name}: not a TIFF-based raw")
        (magic, ifd) = struct.unpack(end + "HI", header[2:8])
        if magic != 42:
            raise HookError(f"{path.name}: not a TIFF-based raw")
        f.seek(ifd)
        (count,) = struct.unpack(end + "H", f.read(2))
        tags: dict[int, int] = {}
        for _ in range(count):
            tag, typ, n, value = struct.unpack(end + "HHII", f.read(12))
            if typ == 3 and n == 1:  # SHORT stored in the first two bytes of the value
                value = struct.unpack(end + "H", struct.pack(end + "I", value)[:2])[0]
            tags[tag] = value
        offset, length = tags.get(0x0111), tags.get(0x0117)  # StripOffsets/ByteCounts
        if not offset or not length:
            raise HookError(f"{path.name}: no preview in IFD0")
        f.seek(offset)
        data = f.read(length)
    if data[:2] != b"\xff\xd8":
        raise HookError(f"{path.name}: IFD0 strip is not a JPEG")
    return data, tags.get(0x0112, 1)


def _external_preview(path: Path) -> bytes:
    """exiv2 or dcraw, whichever is installed."""
    if shutil.which("exiv2"):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(
                ["exiv2", "-ep3", "-l", tmp, str(path)], check=False, capture_output=True
            )
            found = sorted(Path(tmp).glob("*-preview*.jpg"))
            if found:
                return found[-1].read_bytes()
    if shutil.which("dcraw"):
        out = subprocess.run(["dcraw", "-e", "-c", str(path)], check=False, capture_output=True)
        if out.returncode == 0 and out.stdout[:2] == b"\xff\xd8":
            return out.stdout
    raise HookError(f"{path.name}: no embedded preview found (install exiv2 or dcraw)")


def open_display_image(path: Path, size: int) -> Image.Image:
    """Any supported camera file, upright, no larger than `size` on its long edge."""
    suffix = path.suffix.lower()
    orientation = None
    if suffix in RAW_SUFFIXES:
        try:
            data, orientation = tiff_ifd0_jpeg(path)
        except (HookError, struct.error, OSError) as exc:
            log.info("%s; trying exiv2/dcraw", exc)
            data = _external_preview(path)
        im = Image.open(io.BytesIO(data))
    elif suffix in JPEG_SUFFIXES | OTHER_IMAGE_SUFFIXES:
        try:
            im = Image.open(path)
        except OSError as exc:
            raise HookError(f"{path.name}: unreadable image ({exc})") from exc
    else:
        raise HookError(f"{path.name}: not an image this hook handles")

    # JPEG draft mode decodes at 1/2, 1/4 or 1/8 scale: a 21 MP frame never sits
    # in memory at full size, which matters on a Pi.
    im.draft("RGB", (size, size))
    if orientation is None:
        im = ImageOps.exif_transpose(im)
    elif orientation in _ORIENT:
        im = im.transpose(_ORIENT[orientation])
    im = im.convert("RGB")
    im.thumbnail((size, size), Image.Resampling.LANCZOS)
    return im


class Developer:
    """Turns each downloaded file into an archived original and a display JPEG."""

    def __init__(self, display: Path, archive: Path, size: int = 1920) -> None:
        self.display = display
        self.archive = archive
        self.size = size
        self._recent: deque[str] = deque(maxlen=50)

    def develop(self, src: Path, *, when: float | None = None) -> Path | None:
        """Archive `src` and return the new display image, or None if the shot already has one.

        `src` is moved, not copied: it is a download in a temporary folder.
        """
        when = time.time() if when is None else when
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(when))
        day = time.strftime("%Y-%m-%d", time.localtime(when))
        key = f"{day}/{src.stem}"

        out = None
        if key not in self._recent:
            try:
                im = open_display_image(src, self.size)
            except HookError as exc:
                log.warning("%s", exc)
                im = None
            if im is not None:
                self.display.mkdir(parents=True, exist_ok=True)
                out = self.display / f"{stamp}_{src.stem}.jpg"
                # Written under a dot-name, then renamed: watchers only ever see whole files.
                tmp = self.display / f".{out.name}.tmp"
                im.save(tmp, "JPEG", quality=85, optimize=True)
                tmp.replace(out)
                self._recent.append(key)

        dest_dir = self.archive / day
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        if dest.exists():  # Canon numbering wraps at 9999
            dest = dest_dir / f"{stamp}_{src.name}"
        shutil.move(str(src), dest)
        return out
