"""The camera: the real 5D over USB, or a fake one for a laptop.

Both answer the same three calls: `poll()` waits briefly and returns the files
that just came off the camera, `trigger()` fires the shutter, `close()`.
"""

from __future__ import annotations

import contextlib
import logging
import random
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageDraw

log = logging.getLogger(__name__)


class CameraError(Exception):
    """The camera is gone or refused: reconnect."""


class Gphoto2Camera:
    """A tethered camera through libgphoto2.

    Shots fired by the camera itself (the remote, the shutter button) arrive as
    FILE_ADDED events exactly like shots fired from here, so one loop handles both.
    """

    def __init__(self, download_dir: Path) -> None:
        try:
            import gphoto2 as gp
        except ImportError as exc:
            raise CameraError(
                "python-gphoto2 missing: pip install 'photobouche[booth]' "
                "(needs libgphoto2-dev on the Pi)"
            ) from exc
        self.gp = gp
        self.download_dir = download_dir
        self.camera = gp.Camera()
        try:
            self.camera.init()
        except gp.GPhoto2Error as exc:
            raise CameraError(f"no camera: {exc}") from exc
        self._configure()

    def _set(self, name: str, value: str | None = None, choice: int | None = None) -> bool:
        gp = self.gp
        try:
            cfg = self.camera.get_config()
            node = cfg.get_child_by_name(name)
            node.set_value(node.get_choice(choice) if choice is not None else value)
            self.camera.set_config(cfg)
            return True
        except gp.GPhoto2Error as exc:
            log.debug("could not set %s: %s", name, exc)
            return False

    def _configure(self) -> None:
        # 1 = memory card: the CF card keeps every original, whatever happens to the Pi.
        if not self._set("capturetarget", choice=1):
            log.warning("could not set capturetarget: shots may stay in camera RAM only")
        summary = str(self.camera.get_summary())
        log.info("camera ready: %s", summary.splitlines()[0] if summary else "?")

    def poll(self, timeout: float) -> list[Path]:
        gp = self.gp
        out: list[Path] = []
        try:
            event, data = self.camera.wait_for_event(int(timeout * 1000))
            while event != gp.GP_EVENT_TIMEOUT:
                if event == gp.GP_EVENT_FILE_ADDED:
                    out.append(self._download(data.folder, data.name))
                # Drain what is already queued (RAW + JPEG come as two events).
                event, data = self.camera.wait_for_event(10)
        except gp.GPhoto2Error as exc:
            raise CameraError(str(exc)) from exc
        return out

    def _download(self, folder: str, name: str) -> Path:
        gp = self.gp
        self.download_dir.mkdir(parents=True, exist_ok=True)
        dest = self.download_dir / name
        cam_file = self.camera.file_get(folder, name, gp.GP_FILE_TYPE_NORMAL)
        cam_file.save(str(dest))
        return dest

    def trigger(self) -> None:
        gp = self.gp
        try:
            self.camera.trigger_capture()
            return
        except gp.GPhoto2Error as exc:
            log.debug("trigger_capture failed (%s), trying eosremoterelease", exc)
        # Older EOS bodies: press and release through the remote-release setting.
        if not (
            self._set("eosremoterelease", "Immediate")
            and self._set("eosremoterelease", "Release Full")
        ):
            raise CameraError("camera refused to fire")

    def close(self) -> None:
        with contextlib.suppress(self.gp.GPhoto2Error):
            self.camera.exit()


class FakeCamera:
    """Makes up a photo shortly after each trigger. For working without the 5D."""

    def __init__(self, download_dir: Path | None = None, delay: float = 0.4) -> None:
        self.download_dir = download_dir or Path(tempfile.mkdtemp(prefix="photobouche-fake-"))
        self.delay = delay
        self.count = 0
        self._pending: list[float] = []

    def trigger(self) -> None:
        self._pending.append(time.monotonic() + self.delay)

    def poll(self, timeout: float) -> list[Path]:
        time.sleep(timeout)
        now = time.monotonic()
        ready = [t for t in self._pending if t <= now]
        self._pending = [t for t in self._pending if t > now]
        return [self._make() for _ in ready]

    def _make(self) -> Path:
        self.count += 1
        rng = random.Random(self.count)
        a = tuple(rng.randrange(256) for _ in range(3))
        b = tuple(rng.randrange(256) for _ in range(3))
        w, h = 1500, 1000
        im = Image.linear_gradient("L").resize((w, h))
        im = Image.composite(Image.new("RGB", (w, h), a), Image.new("RGB", (w, h), b), im)
        draw = ImageDraw.Draw(im)
        draw.text((60, 60), f"FAKE {self.count:04d}  {time.strftime('%H:%M:%S')}", fill="white")
        draw.ellipse((w / 2 - 200, h / 2 - 150, w / 2 + 200, h / 2 + 250), fill="black")
        self.download_dir.mkdir(parents=True, exist_ok=True)
        path = self.download_dir / f"IMG_{self.count:04d}.JPG"
        im.save(path, quality=90)
        return path

    def close(self) -> None:
        pass
