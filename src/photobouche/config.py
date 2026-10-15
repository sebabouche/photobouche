"""Settings, read from one TOML file shared by every command.

Everything has a default so `photobouche <cmd> --root ./demo` works with no file at all.
Values tuned live on the control screen (scream threshold, scream mode) are not written
back into the TOML, which keeps its comments: they go to `<root>/state.json`, which
overrides the file.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import tempfile
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = Path("/etc/photobouche/config.toml")


@dataclass
class Paths:
    root: Path = Path("/srv/photobouche")

    @property
    def display(self) -> Path:
        """Display-size JPEGs of the booth shots: the `live` collection."""
        return self.root / "display"

    @property
    def archive(self) -> Path:
        """Originals (CR2 + camera JPEG), one folder per day."""
        return self.root / "archive"

    @property
    def rejected(self) -> Path:
        """Shots hidden from the control screen. Never deleted."""
        return self.root / "rejected"

    @property
    def making_of(self) -> Path:
        return self.root / "making_of"

    @property
    def clips(self) -> Path:
        return self.root / "clips"

    @property
    def socket(self) -> Path:
        """Camera daemon ⇄ control screen (telemetry out, commands in).

        Unix socket paths are capped near 104 bytes; a deep root gets a short
        path in the temp folder instead, derived from the root so both ends agree.
        """
        path = self.root / "run" / "live.sock"
        if len(str(path)) < 100:
            return path
        digest = hashlib.sha1(str(self.root.resolve()).encode()).hexdigest()[:10]
        return Path(tempfile.gettempdir()) / f"photobouche-{digest}.sock"

    @property
    def state(self) -> Path:
        return self.root / "state.json"


@dataclass
class BoothConfig:
    host: str = "0.0.0.0"
    port: int = 8765


@dataclass
class CameraConfig:
    fake: bool = False
    """Copy a sample image instead of driving a camera: for a laptop with no 5D."""

    mic: str = ""
    """Microphone, by name (see `photobouche mics`). Empty: no scream mode."""

    scream_mode: bool = True
    trigger_db: float = 15.0
    """dB above the ambient floor that counts as a scream (the line on the meter)."""

    fire_after: float = 0.25
    """Seconds into a scream before the shutter fires: mouth wide open, not opening."""

    cooldown: float = 3.0
    """Seconds after a shot before a scream can fire again (flash recycle, download)."""

    display_size: int = 1920


@dataclass
class ShowConfig:
    booth_url: str = "http://raspi4-1.local:8765"
    cache_dir: Path = Path.home() / ".cache" / "photobouche"
    cache_mb: int = 500
    fresh_seconds: float = 8.0
    image_seconds: float = 6.0
    clip_every: int = 5
    """Play one clip after this many images in the rotation. 0: never."""

    recent_hours: float = 2.0
    recent_weight: float = 3.0
    """Live shots younger than `recent_hours` are this many times more likely."""

    no_repeat: int = 20
    local_clips: Path | None = None
    """Clips stored on the slideshow Pi itself."""

    audio_device: str = ""
    """mpv --audio-device value, e.g. 'alsa/hdmi:CARD=vc4hdmi0'. Empty: mpv's default."""

    music_dir: Path | None = None
    """Background music, played under images and ducked during clips. None: off."""

    music_volume: int = 60
    music_duck_volume: int = 15
    fullscreen: bool = True
    seed: int | None = None


@dataclass
class BoothScreenConfig:
    mode: str = "cycle"
    """`latest` holds the last shot; `cycle` shows a new one, then cycles recent ones."""

    fresh_seconds: float = 8.0
    cycle_seconds: float = 4.0
    cycle_count: int = 12
    fullscreen: bool = True


@dataclass
class Config:
    paths: Paths = field(default_factory=Paths)
    booth: BoothConfig = field(default_factory=BoothConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    show: ShowConfig = field(default_factory=ShowConfig)
    booth_screen: BoothScreenConfig = field(default_factory=BoothScreenConfig)

    def save_state(self) -> None:
        """Persist the values the control screen can change."""
        state = {
            "camera": {
                "trigger_db": self.camera.trigger_db,
                "scream_mode": self.camera.scream_mode,
            },
            "booth_screen": {"mode": self.booth_screen.mode},
        }
        self.paths.state.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.paths.state.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(self.paths.state)


_SECTIONS = {
    "paths": Paths,
    "booth": BoothConfig,
    "camera": CameraConfig,
    "show": ShowConfig,
    "booth_screen": BoothScreenConfig,
}


def _apply(cfg: Config, data: dict[str, Any]) -> None:
    for section, values in data.items():
        if section not in _SECTIONS:
            raise ValueError(f"Unknown config section [{section}]")
        target = getattr(cfg, section)
        known = {f.name: f for f in fields(target)}
        for key, value in values.items():
            if key not in known:
                raise ValueError(f"Unknown setting {section}.{key}")
            current = getattr(target, key)
            path_like = isinstance(current, Path) or "Path" in str(known[key].type)
            if path_like and isinstance(value, str):
                value = Path(value).expanduser() if value else None
            setattr(target, key, value)


def load(path: Path | None = None, *, root: Path | None = None) -> Config:
    """Read `path` (or the default file if it exists), then state.json, then `root`."""
    cfg = Config()
    if path is None and DEFAULT_CONFIG.exists():
        path = DEFAULT_CONFIG
    if path is not None:
        with open(path, "rb") as f:
            _apply(cfg, tomllib.load(f))
    if root is not None:
        cfg.paths.root = root
    if cfg.paths.state.exists():
        # A corrupt state file must not stop the show.
        with contextlib.suppress(ValueError, OSError):
            _apply(cfg, json.loads(cfg.paths.state.read_text()))
    return cfg


def as_dict(cfg: Config) -> dict[str, Any]:
    return asdict(cfg)
