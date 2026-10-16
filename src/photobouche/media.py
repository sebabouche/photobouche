"""Making-of material, made fit for the Pis before it goes on the booth.

Material from a shared drive is whatever phones and cameras produced: 48 MP
photos, 4K, 10-bit HDR HEVC, Dolby Vision, 28 Mb/s, six-minute takes, the same
file in two folders. The slideshow Pi has little RAM and one hardware decoder
that is at home with exactly one thing: 8-bit H.264 up to 1920x1080. So
`prepare` makes everything that:

* **images** → upright 1920px JPEGs (the same code as the booth's own shots);
  thumbnails too small to show are dropped;
* **every clip** is re-encoded, even one that would play: one known format means
  the Pi never meets a surprise on show night — 8-bit H.264 High@4.0, BT.709,
  rotation baked in, inside a 1920x1080 box (portrait too: 1080x1920 is past
  the decoder's limit), at most 30 fps and 8 Mb/s, AAC stereo, moov up front for
  streaming; clips longer than `max_clip` are cut into equal parts;
* **duplicates** are skipped, and the best copy kept. Many people posted the
  same material, often forwarded through WhatsApp, which re-compresses, shrinks
  and strips the metadata — so files are matched by what they SHOW, not by their
  bytes (see `duplicates`). The copy kept is the one with real camera metadata,
  then the largest, then not named like a WhatsApp file;
* **names** become lowercase ASCII, prefixed with the folders they came from.

HDR clips (HLG, Dolby Vision's HLG base) are converted by matrix only, not tone-
mapped: HLG is built to look right on an SDR screen, and checked frames do.

`plan()` decides, `prepare()` executes: `photobouche prepare --dry-run` shows
the plan without encoding anything.

`check_stream` runs on the slideshow Pi: it measures how fast clips come off the
booth against how fast they play.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import shutil
import subprocess
import time
import unicodedata
import urllib.request
from dataclasses import dataclass, field
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
BOX = (1920, 1080)
MAX_FPS = 30
MAX_RATE = "8M"
HDR_TRANSFERS = {"arib-std-b67", "smpte2084"}
TWIN_TOLERANCE = 0.5
"""Seconds: two same-name clips this close in duration are one clip exported twice."""


# --------------------------------------------------------------------- probe


@dataclass
class ClipInfo:
    codec: str
    width: int
    height: int
    duration: float
    bitrate: float
    """bits per second, whole file."""

    pix_fmt: str = ""
    transfer: str = ""
    rotation: int = 0
    fps: float = 0.0
    has_audio: bool = True

    @property
    def display_size(self) -> tuple[int, int]:
        """Width and height as seen, after the phone's rotation flag."""
        if abs(self.rotation) % 180 == 90:
            return self.height, self.width
        return self.width, self.height

    @property
    def problems(self) -> list[str]:
        """What would trouble a Pi 4, for reports. `prepare` re-encodes regardless."""
        out = []
        if self.codec != "h264":
            out.append(f"{self.codec}")
        if "10" in self.pix_fmt or "12" in self.pix_fmt:
            out.append("10-bit")
        if self.transfer in HDR_TRANSFERS:
            out.append("HDR")
        w, h = self.display_size
        if w > BOX[0] or h > BOX[1]:
            out.append(f"{w}x{h}")
        if self.fps > MAX_FPS + 1:
            out.append(f"{self.fps:.0f} fps")
        if self.bitrate > 12e6:
            out.append(f"{self.bitrate / 1e6:.0f} Mb/s")
        return out


def _rate(text: str) -> float:
    try:
        num, _, den = text.partition("/")
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(source: str) -> ClipInfo | None:
    """ffprobe a file or URL. None when ffprobe is missing or fails."""
    if not shutil.which("ffprobe"):
        return None
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", source],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return None
    data = json.loads(out.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        return None
    rotation = 0
    for side in video.get("side_data_list", []):
        if "rotation" in side:
            rotation = int(side["rotation"])
    rotation = rotation or int(video.get("tags", {}).get("rotate", 0) or 0)
    fmt = data.get("format", {})
    return ClipInfo(
        codec=video.get("codec_name", "?"),
        width=int(video.get("width", 0)),
        height=int(video.get("height", 0)),
        duration=float(fmt.get("duration", 0) or 0),
        bitrate=float(fmt.get("bit_rate", 0) or 0),
        pix_fmt=video.get("pix_fmt", ""),
        transfer=video.get("color_transfer", ""),
        rotation=rotation,
        fps=_rate(video.get("avg_frame_rate", "0/1")),
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


# ---------------------------------------------------------------------- plan


def slug(text: str) -> str:
    """'Photo booth export/Zoé/IMG 1' → 'photo_booth_export_zoe_img_1'."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", ascii_text.lower()).strip("_")


@dataclass
class Job:
    src: Path
    kind: str
    """`image` or `clip`."""

    outputs: list[str] = field(default_factory=list)
    """File names under making_of/ or clips/. Empty: skipped."""

    note: str = ""
    """Why it is skipped, or what is wrong with the source."""

    info: ClipInfo | None = None
    parts: list[tuple[float, float]] = field(default_factory=list)
    """(start, length) per output, for clips."""

    @property
    def skipped(self) -> bool:
        return not self.outputs


def split(duration: float, max_clip: float) -> list[tuple[float, float]]:
    """Equal parts no longer than `max_clip` — never a 3-second tail."""
    if max_clip <= 0 or duration <= max_clip:
        return [(0.0, duration)]
    n = math.ceil(duration / max_clip)
    part = duration / n
    return [(i * part, part) for i in range(n)]


WHATSAPP_NAME = re.compile(r"-WA\d+", re.IGNORECASE)
IMAGE_MATCH = 0.15
"""Mean difference of two normalised 32x32 thumbnails below which they show the same
picture. Measured on this event's data: WhatsApp copies 0.008-0.086."""

CLIP_MATCH = 0.20
CLIP_DURATION_SLACK = 1.5


@dataclass
class Source:
    """What duplicate detection knows about one file."""

    path: Path
    kind: str
    digest: str
    pixels: int = 0
    aspect: float = 0.0
    captured: str | None = None
    """Capture time from the camera, if the file still has it."""

    model: str | None = None
    thumb: object = None
    """numpy 32x32, normalised; for clips, three of them."""

    info: ClipInfo | None = None

    @property
    def metadata_score(self) -> int:
        return 2 * bool(self.captured) + bool(self.model)

    def rank(self, src: Path) -> tuple:
        """Higher is better: real metadata, then size, then not WhatsApp, then shallow."""
        rel = self.path.relative_to(src)
        return (
            self.metadata_score,
            self.pixels,
            not WHATSAPP_NAME.search(self.path.name),
            -len(rel.parts),
            str(rel).lower(),
        )


def _digest(path: Path) -> str:
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha1").hexdigest()


def _normalised(gray) -> object:
    import numpy as np

    a = np.asarray(gray, dtype=np.float32)
    return (a - a.mean()) / (a.std() + 1e-6)


def _image_source(path: Path) -> Source:
    from PIL import Image

    src = Source(path, "image", _digest(path))
    if path.suffix.lower() not in RAW_SUFFIXES:
        with Image.open(path) as im:
            exif = im.getexif()
            sub = exif.get_ifd(0x8769)
            src.captured = sub.get(0x9003) or None
            src.model = exif.get(0x0110) or None
    else:
        src.captured, src.model = "raw", "raw"  # straight off a camera
    im = open_display_image(path, 4096)
    src.pixels = im.width * im.height
    src.aspect = im.width / im.height
    src.thumb = _normalised(im.convert("L").resize((32, 32), 3))  # BOX
    return src


def _clip_source(path: Path) -> Source:
    src = Source(path, "clip", _digest(path))
    src.info = probe(str(path))
    if src.info is None:
        return src
    tags = _format_tags(path)
    src.captured = tags.get("com.apple.quicktime.creationdate") or None
    src.model = tags.get("com.apple.quicktime.model") or tags.get("com.android.model") or None
    w, h = src.info.display_size
    src.pixels, src.aspect = w * h, (w / h if h else 0.0)
    return src


def _format_tags(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", str(path)],
        capture_output=True,
        text=True,
    )
    try:
        return json.loads(out.stdout).get("format", {}).get("tags", {})
    except ValueError:
        return {}


def _clip_frames(src: Source) -> list:
    """Three tiny grey frames at 20/50/80 % — rotation applied, so copies line up."""
    if src.thumb is None:
        frames = []
        for at in (0.2, 0.5, 0.8):
            out = subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-ss", f"{src.info.duration * at:.2f}",
                    "-i", str(src.path), "-frames:v", "1",
                    "-vf", "scale=32:32,format=gray", "-f", "rawvideo", "-",
                ],
                capture_output=True,
            )
            if len(out.stdout) == 32 * 32:
                import numpy as np

                frames.append(_normalised(np.frombuffer(out.stdout, np.uint8).reshape(32, 32)))
        src.thumb = frames
    return src.thumb


def _diff(a, b) -> float:
    import numpy as np

    return float(np.abs(a - b).mean())


def same_picture(a: Source, b: Source) -> bool:
    if abs(a.aspect - b.aspect) > 0.05:
        return False
    # Two shots that both kept their capture time and differ are two shots, however
    # alike: a burst in front of the mouth looks the same at any thumbnail size.
    if a.captured and b.captured and a.captured != b.captured and "raw" not in (a.captured, b.captured):
        return False
    return _diff(a.thumb, b.thumb) <= IMAGE_MATCH


def same_clip(a: Source, b: Source) -> bool:
    if a.info is None or b.info is None:
        return False
    if abs(a.info.duration - b.info.duration) > CLIP_DURATION_SLACK:
        return False
    if a.captured and b.captured and a.captured[:19] != b.captured[:19]:
        return False
    # One clip exported twice under the same name — possibly cropped, so frames differ.
    if a.path.stem.lower() == b.path.stem.lower() and abs(a.info.duration - b.info.duration) < 0.5:
        return True
    if abs(a.aspect - b.aspect) > 0.05:
        return False
    fa, fb = _clip_frames(a), _clip_frames(b)
    if len(fa) != 3 or len(fb) != 3:
        return False
    return sum(_diff(x, y) for x, y in zip(fa, fb, strict=True)) / 3 <= CLIP_MATCH


def duplicates(sources: list[Source], src: Path) -> dict[Path, tuple[Path, str]]:
    """Every file that is a copy of another: path -> (the copy kept, why)."""
    parent = list(range(len(sources)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        parent[find(j)] = find(i)

    for i in range(len(sources)):
        for j in range(i + 1, len(sources)):
            a, b = sources[i], sources[j]
            if a.kind != b.kind:
                continue
            # Reposts come from other people, so a look-alike must come from another
            # folder. Within one folder look-alikes are bursts: two 5D frames in the
            # same second share a timestamp and a thumbnail, and are two photos.
            other_folder = a.path.parent != b.path.parent
            if a.digest == b.digest or (other_folder and (
                (a.kind == "image" and same_picture(a, b))
                or (a.kind == "clip" and same_clip(a, b))
            )):
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(len(sources)):
        groups.setdefault(find(i), []).append(i)
    out: dict[Path, tuple[Path, str]] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        best = max(members, key=lambda i: sources[i].rank(src))
        for i in members:
            if i == best:
                continue
            if sources[i].digest == sources[best].digest:
                reason = "identical file"
            else:
                reason = "same picture" if sources[i].kind == "image" else "same clip"
            out[sources[i].path] = (sources[best].path, reason)
    return out


def plan(src: Path, *, max_clip: float = 45.0, min_image: int = 800) -> list[Job]:
    files = sorted(
        (p for p in src.rglob("*") if p.is_file() and not p.name.startswith(".")),
        key=lambda p: str(p.relative_to(src)).lower(),
    )
    jobs: list[Job] = []
    sources: list[Source] = []
    for path in files:
        suffix = path.suffix.lower()
        if suffix in IMAGE_IN:
            try:
                sources.append(_image_source(path))
            except (HookError, OSError) as exc:
                jobs.append(Job(path, "image", note=f"unreadable: {exc}"))
        elif suffix in VIDEO_SUFFIXES:
            source = _clip_source(path)
            if source.info is None:
                jobs.append(Job(path, "clip", note="unreadable by ffprobe"))
            else:
                sources.append(source)
        else:
            jobs.append(Job(path, "other", note="not an image or a clip (HEIC: export as JPEG)"))

    dupes = duplicates(sources, src)
    names: set[str] = set()
    for s in sources:
        job = Job(s.path, s.kind, info=s.info)
        jobs.append(job)
        if s.path in dupes:
            kept, reason = dupes[s.path]
            job.note = f"{reason} as {kept.relative_to(src)}"
            continue
        base = slug(str(s.path.relative_to(src).with_suffix("")))
        if s.kind == "image":
            w = round((s.pixels * s.aspect) ** 0.5)
            h = round(s.pixels / w) if w else 0
            if max(w, h) < min_image:
                job.note = f"too small to show ({w}x{h})"
                continue
            job.outputs = [_unique(f"{base}.jpg", names)]
        else:
            job.parts = split(s.info.duration, max_clip)
            n = len(job.parts)
            job.outputs = (
                [_unique(f"{base}.mp4", names)]
                if n == 1
                else [_unique(f"{base}_part{i + 1}of{n}.mp4", names) for i in range(n)]
            )
    jobs.sort(key=lambda j: str(j.src).lower())
    return jobs


def _unique(name: str, taken: set[str]) -> str:
    stem, dot, ext = name.rpartition(".")
    candidate, i = name, 2
    while candidate in taken:
        candidate = f"{stem}_{i}{dot}{ext}"
        i += 1
    taken.add(candidate)
    return candidate


# ------------------------------------------------------------------- execute


def encode_command(src: Path, dest: Path, info: ClipInfo, start: float, length: float) -> list[str]:
    w, h = BOX
    vf = (
        # ffmpeg applies the rotation flag itself before this filter (autorotate).
        f"scale=w={w}:h={h}:force_original_aspect_ratio=decrease:force_divisible_by=2"
        ":out_color_matrix=bt709:out_range=tv,format=yuv420p,"
        # Tag the frames themselves: the encoder writes what the frames say, and a
        # clip left tagged HLG would send the Pi's mpv into HDR conversion.
        "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv"
    )
    cmd = ["ffmpeg", "-y", "-v", "error", "-nostdin"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(src), "-t", f"{length:.3f}", "-map", "0:v:0"]
    cmd += ["-map", "0:a:0"] if info.has_audio else []
    cmd += [
        "-vf", vf, "-fpsmax", str(MAX_FPS),
        "-c:v", "libx264", "-preset", "medium", "-crf", "21",
        "-profile:v", "high", "-level:v", "4.0",
        "-maxrate", MAX_RATE, "-bufsize", "16M",
    ]
    if info.has_audio:
        cmd += ["-c:a", "aac", "-b:a", "160k", "-ac", "2", "-ar", "48000"]
    cmd += ["-map_metadata", "-1", "-movflags", "+faststart", str(dest)]
    return cmd


def _up_to_date(src: Path, dest: Path) -> bool:
    return dest.exists() and dest.stat().st_mtime >= src.stat().st_mtime


def prepare(
    src: Path, dest: Path, *, size: int = 1920, max_clip: float = 45.0, min_image: int = 800
) -> tuple[list[Job], dict]:
    """Plan, then write `dest/making_of/` (images) and `dest/clips/` (clips)."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is needed: brew install ffmpeg")
    jobs = plan(src, max_clip=max_clip, min_image=min_image)
    counts = {"images": 0, "clips": 0, "up_to_date": 0, "skipped": 0, "failed": 0}
    images, clips = dest / "making_of", dest / "clips"
    images.mkdir(parents=True, exist_ok=True)
    clips.mkdir(parents=True, exist_ok=True)
    todo = [j for j in jobs if not j.skipped]
    counts["skipped"] = len(jobs) - len(todo)
    for n, job in enumerate(todo, 1):
        folder = images if job.kind == "image" else clips
        outs = [folder / name for name in job.outputs]
        if all(_up_to_date(job.src, o) for o in outs):
            counts["up_to_date"] += 1
            continue
        log.info("[%d/%d] %s", n, len(todo), job.src.relative_to(src))
        if job.kind == "image":
            try:
                im = open_display_image(job.src, size)
            except (HookError, OSError) as exc:
                log.warning("failed %s: %s", job.src, exc)
                counts["failed"] += 1
                continue
            tmp = outs[0].with_name(f".{outs[0].name}.tmp")
            im.save(tmp, "JPEG", quality=88, optimize=True)
            tmp.replace(outs[0])
            counts["images"] += 1
            continue
        for out, (start, length) in zip(outs, job.parts, strict=True):
            tmp = out.with_name(f".{out.stem}.tmp.mp4")
            result = subprocess.run(encode_command(job.src, tmp, job.info, start, length))
            if result.returncode != 0:
                tmp.unlink(missing_ok=True)
                log.warning("failed %s", job.src)
                counts["failed"] += 1
                break
            tmp.replace(out)
            counts["clips"] += 1
    return jobs, counts


# -------------------------------------------------------------- check_stream


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
