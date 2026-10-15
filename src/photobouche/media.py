"""Making-of material, made fit for the Pis before it goes on the booth.

Photos from a shared drive are often 20+ MP and clips are often 4K or HEVC. The
slideshow Pi has little RAM, and its hardware decoder is at home with H.264 up
to 1080p. So:

* images become 1920px JPEGs, upright (the same code as the booth's shots);
* clips are copied, and checked with ffprobe — anything mpv on a Pi 4 may
  struggle with is reported, and re-encoded with `--transcode`.

`check_stream` runs on the slideshow Pi: it measures how fast clips come off the
booth against how fast they play.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from photobouche.booth.watcher import VIDEO_SUFFIXES
from photobouche.camera.hook import (
    JPEG_SUFFIXES,
    OTHER_IMAGE_SUFFIXES,
    RAW_SUFFIXES,
    HookError,
    open_display_image,
)

log = logging.getLogger(__name__)

IMAGE_IN = JPEG_SUFFIXES | RAW_SUFFIXES | OTHER_IMAGE_SUFFIXES
MAX_HEIGHT = 1080
GOOD_CODECS = {"h264"}


@dataclass
class ClipInfo:
    codec: str
    width: int
    height: int
    duration: float
    bitrate: float
    """bits per second, whole file."""

    @property
    def problems(self) -> list[str]:
        out = []
        if self.codec not in GOOD_CODECS:
            out.append(f"codec {self.codec} (want h264)")
        if self.height > MAX_HEIGHT:
            out.append(f"{self.width}x{self.height} (want ≤ {MAX_HEIGHT}p)")
        return out


def probe(source: str) -> ClipInfo | None:
    """ffprobe a file or URL. None when ffprobe is missing or fails."""
    if not shutil.which("ffprobe"):
        return None
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height:format=duration,bit_rate",
            "-of", "json", source,
        ],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return None
    data = json.loads(out.stdout)
    streams = data.get("streams") or [{}]
    fmt = data.get("format", {})
    s = streams[0]
    return ClipInfo(
        codec=s.get("codec_name", "?"),
        width=int(s.get("width", 0)),
        height=int(s.get("height", 0)),
        duration=float(fmt.get("duration", 0) or 0),
        bitrate=float(fmt.get("bit_rate", 0) or 0),
    )


def transcode(src: Path, dest: Path) -> bool:
    if not shutil.which("ffmpeg"):
        log.warning("ffmpeg missing: cannot transcode %s", src.name)
        return False
    tmp = dest.with_name(f".{dest.stem}.tmp.mp4")
    cmd = [
        "ffmpeg", "-y", "-v", "error", "-i", str(src),
        "-vf", f"scale=-2:'min({MAX_HEIGHT},ih)'",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(tmp),
    ]
    if subprocess.run(cmd).returncode != 0:
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(dest)
    return True


def _up_to_date(src: Path, dest: Path) -> bool:
    return dest.exists() and dest.stat().st_mtime >= src.stat().st_mtime


def prepare(src: Path, dest: Path, *, size: int = 1920, do_transcode: bool = False) -> dict:
    """Walk `src`; write `dest/making_of/` (images) and `dest/clips/` (videos)."""
    counts = {"images": 0, "clips": 0, "skipped": 0, "problems": 0, "ignored": 0}
    images, clips = dest / "making_of", dest / "clips"
    images.mkdir(parents=True, exist_ok=True)
    clips.mkdir(parents=True, exist_ok=True)
    for path in sorted(p for p in src.rglob("*") if p.is_file() and not p.name.startswith(".")):
        suffix = path.suffix.lower()
        # Folder names become part of the file name: two "IMG_0001.jpg" from two
        # cameras must not overwrite each other.
        stem = "_".join(path.relative_to(src).with_suffix("").parts)
        if suffix in IMAGE_IN:
            out = images / f"{stem}.jpg"
            if _up_to_date(path, out):
                counts["skipped"] += 1
                continue
            try:
                im = open_display_image(path, size)
            except (HookError, OSError) as exc:
                log.warning("skip %s: %s", path, exc)
                counts["problems"] += 1
                continue
            tmp = out.with_name(f".{out.name}.tmp")
            im.save(tmp, "JPEG", quality=88, optimize=True)
            tmp.replace(out)
            counts["images"] += 1
        elif suffix in VIDEO_SUFFIXES:
            info = probe(str(path))
            issues = info.problems if info else []
            if issues and do_transcode:
                out = clips / f"{stem}.mp4"
                if not _up_to_date(path, out):
                    log.info("transcoding %s (%s)", path.name, ", ".join(issues))
                    if not transcode(path, out):
                        counts["problems"] += 1
                        continue
                # An earlier run without --transcode may have copied the original.
                if suffix != ".mp4":
                    (clips / f"{stem}{suffix}").unlink(missing_ok=True)
                counts["clips"] += 1
                continue
            out = clips / f"{stem}{suffix}"
            if _up_to_date(path, out):
                counts["skipped"] += 1
                continue
            shutil.copy2(path, out)
            counts["clips"] += 1
            if info is None:
                log.info("%s copied (no ffprobe: not checked)", path.name)
            elif issues:
                counts["problems"] += 1
                log.warning(
                    "%s: %s — may stutter on the Pi; rerun with --transcode", path.name, ", ".join(issues)
                )
        else:
            # HEIC from phones lands here: export as JPEG first.
            log.info("ignored %s (not a format the show plays)", path.relative_to(src))
            counts["ignored"] += 1
    return counts


@dataclass
class StreamResult:
    name: str
    mbps: float
    """Measured download speed, megabits per second."""

    needed_mbps: float | None
    """The clip's own bitrate, if ffprobe could read it."""

    info: ClipInfo | None

    @property
    def headroom(self) -> float | None:
        if not self.needed_mbps:
            return None
        return self.mbps / self.needed_mbps


def check_stream(base_url: str, sample_mb: int = 16) -> list[StreamResult]:
    """Download the first `sample_mb` of every clip on the booth and time it."""
    with urllib.request.urlopen(base_url.rstrip("/") + "/api/changes?since=-1", timeout=10) as r:
        items = json.load(r)["added"]
    results = []
    for item in items:
        if item["kind"] != "video":
            continue
        url = f"{base_url.rstrip('/')}/media/{item['collection']}/{urllib.request.quote(item['name'])}"
        req = urllib.request.Request(url, headers={"Range": f"bytes=0-{sample_mb * 1024 * 1024 - 1}"})
        start = time.monotonic()
        got = 0
        with urllib.request.urlopen(req, timeout=30) as r:
            while chunk := r.read(256 * 1024):
                got += len(chunk)
        seconds = max(time.monotonic() - start, 1e-6)
        info = probe(url)
        results.append(
            StreamResult(
                name=f"{item['collection']}/{item['name']}",
                mbps=got * 8 / seconds / 1e6,
                needed_mbps=info.bitrate / 1e6 if info and info.bitrate else None,
                info=info,
            )
        )
    return results
