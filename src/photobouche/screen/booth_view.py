"""`photobouche screen --profile booth`: the control screen next to the booth.

The latest shot fills the top of the screen. Below it the microphone shows as an
equalizer and a level meter with the "SHOOT" line: people see their scream
climb toward it, and the screen flashes white when the shutter fires.

pygame (SDL2) and not pyglet: this is 2D blitting, and SDL runs on the Pi 4
whatever OpenGL it offers. pyglet 2 asks for desktop GL 3.3; the Pi 4 has 3.1.

Keys:  X hide shot · S scream mode · M latest/cycle · +/- threshold ·
       Space fire · Esc/Q quit
"""

from __future__ import annotations

import logging
import shutil
import time
from collections import OrderedDict
from pathlib import Path

from photobouche.booth.watcher import IMAGE_SUFFIXES
from photobouche.camera.telemetry import TelemetryClient
from photobouche.config import Config

log = logging.getLogger(__name__)

BLACK = (0, 0, 0)
WHITE = (240, 240, 240)
DIM = (70, 70, 70)
RED = (230, 30, 40)
AMBER = (255, 170, 0)

PHOTO_SHARE = 0.78
"""Fraction of the height the photo gets; the meter has the rest."""

FLASH_SECONDS = 0.6


class ShotList:
    """The display folder, newest last. Rescanned twice a second."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.files: list[Path] = []
        self.newest: Path | None = None
        self._primed = False
        self._next_scan = 0.0

    def rescan_soon(self) -> None:
        self._next_scan = 0.0

    def scan(self, now: float) -> bool:
        """True when a new shot appeared since the last scan. Never on the first scan."""
        if now < self._next_scan:
            return False
        self._next_scan = now + 0.5
        try:
            self.files = sorted(
                p
                for p in self.folder.iterdir()
                if p.suffix.lower() in IMAGE_SUFFIXES and not p.name.startswith(".")
            )
        except FileNotFoundError:
            self.files = []
        newest = self.files[-1] if self.files else None
        # Names start with the capture time, so "newer" is a string comparison. Hiding
        # the newest shot makes an older one newest: that is not a new shot.
        new = (
            self._primed
            and newest is not None
            and (self.newest is None or newest.name > self.newest.name)
        )
        self.newest = newest if newest is None or self.newest is None else max(
            newest, self.newest, key=lambda p: p.name
        )
        self._primed = True
        return new


class Selector:
    """Which shot to show. Pure, for tests: fed the list and the time."""

    def __init__(self, mode: str, fresh_seconds: float, cycle_seconds: float, count: int) -> None:
        self.mode = mode
        self.fresh_seconds = fresh_seconds
        self.cycle_seconds = cycle_seconds
        self.count = count
        self.fresh_until = 0.0
        self._cycle_start = 0.0

    def new_shot(self, now: float) -> None:
        self.fresh_until = now + self.fresh_seconds
        self._cycle_start = self.fresh_until

    def pick(self, files: list[Path], now: float) -> Path | None:
        if not files:
            return None
        if self.mode == "latest" or now < self.fresh_until:
            return files[-1]
        recent = files[-self.count :]
        # Newest first, round and round.
        step = int((now - self._cycle_start) // self.cycle_seconds)
        return recent[-1 - (step % len(recent))]


def run_booth_view(cfg: Config) -> None:
    import pygame

    bc = cfg.booth_screen
    pygame.init()
    flags = pygame.FULLSCREEN if bc.fullscreen else pygame.RESIZABLE
    screen = pygame.display.set_mode((0, 0) if bc.fullscreen else (1280, 800), flags)
    pygame.display.set_caption("photobouche — booth")
    pygame.mouse.set_visible(not bc.fullscreen)
    clock = pygame.time.Clock()

    shots = ShotList(cfg.paths.display)
    shots.scan(0.0)
    selector = Selector(bc.mode, bc.fresh_seconds, bc.cycle_seconds, bc.cycle_count)
    telemetry = TelemetryClient(cfg.paths.socket)
    images: OrderedDict[tuple[Path, tuple[int, int]], pygame.Surface] = OrderedDict()
    fonts = {}
    flash_until = 0.0
    last_state = ""
    current: Path | None = None

    def font(size: int) -> pygame.font.Font:
        if size not in fonts:
            fonts[size] = pygame.font.Font(None, size)
        return fonts[size]

    def photo(path: Path, box: tuple[int, int]) -> pygame.Surface | None:
        key = (path, box)
        if key in images:
            images.move_to_end(key)
            return images[key]
        try:
            raw = pygame.image.load(str(path)).convert()
        except (pygame.error, FileNotFoundError):
            return None
        w, h = raw.get_size()
        scale = min(box[0] / w, box[1] / h)
        surf = pygame.transform.smoothscale(raw, (max(1, int(w * scale)), max(1, int(h * scale))))
        images[key] = surf
        while len(images) > 6:
            images.popitem(last=False)
        return surf

    running = True
    while running:
        now = time.monotonic()
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                key = event.key
                if key in (pygame.K_ESCAPE, pygame.K_q):
                    running = False
                elif key == pygame.K_x and current is not None:
                    _reject(current, cfg.paths.rejected)
                    shots.rescan_soon()
                elif key == pygame.K_s:
                    telemetry.send({"cmd": "scream_mode"})
                elif key == pygame.K_m:
                    selector.mode = "cycle" if selector.mode == "latest" else "latest"
                    cfg.booth_screen.mode = selector.mode
                    cfg.save_state()
                elif key in (pygame.K_PLUS, pygame.K_EQUALS, pygame.K_KP_PLUS):
                    telemetry.send({"cmd": "threshold", "delta": 1.0})
                elif key in (pygame.K_MINUS, pygame.K_KP_MINUS):
                    telemetry.send({"cmd": "threshold", "delta": -1.0})
                elif key == pygame.K_SPACE:
                    telemetry.send({"cmd": "shoot"})

        if shots.scan(now):
            selector.new_shot(now)
        t = telemetry.latest if telemetry.connected else {}
        state = t.get("state", "")
        if state == "fired" and last_state != "fired":
            flash_until = now + FLASH_SECONDS
        last_state = state

        W, H = screen.get_size()
        screen.fill(BLACK)
        photo_h = int(H * PHOTO_SHARE)
        current = selector.pick(shots.files, now)
        if current is not None:
            surf = photo(current, (W, photo_h))
            if surf is not None:
                screen.blit(surf, ((W - surf.get_width()) // 2, (photo_h - surf.get_height()) // 2))

        _draw_meter(screen, font, t, pygame.Rect(0, photo_h, W, H - photo_h), now)

        if now < flash_until:
            alpha = int(255 * (flash_until - now) / FLASH_SECONDS)
            veil = pygame.Surface((W, H))
            veil.fill((255, 255, 255))
            veil.set_alpha(alpha)
            screen.blit(veil, (0, 0))

        pygame.display.flip()
        clock.tick(30)
    pygame.quit()


def _reject(path: Path, rejected: Path) -> None:
    """Hide a shot everywhere: the booth agent sees it leave `display/`."""
    rejected.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(path), rejected / path.name)
        log.info("rejected %s", path.name)
    except FileNotFoundError:
        pass


def _draw_meter(screen, font, t: dict, area, now: float) -> None:
    import pygame

    pad = max(8, area.height // 14)
    inner = area.inflate(-2 * pad, -2 * pad)
    state = t.get("state", "")
    hot = state in ("arming", "fired")
    level = float(t.get("level", 0.0))
    threshold = float(t.get("threshold", 0.0))

    # Level meter: one wide bar along the top of the panel, with the SHOOT line.
    meter = pygame.Rect(inner.x, inner.y, inner.width, max(10, inner.height // 5))
    pygame.draw.rect(screen, DIM, meter, width=2)
    fill = meter.inflate(-6, -6)
    fill.width = int(fill.width * min(level, 1.0))
    pygame.draw.rect(screen, RED if level >= threshold > 0 else AMBER if hot else WHITE, fill)
    if threshold > 0:
        x = meter.x + int(meter.width * min(threshold, 1.0))
        pygame.draw.line(screen, RED, (x, meter.y - 6), (x, meter.bottom + 6), 4)
        over = level >= threshold
        label = font(max(14, meter.height)).render("SHOOT", True, BLACK if over else RED)
        screen.blit(label, (x + 8, meter.y + (meter.height - label.get_height()) // 2))

    # Equalizer bars under it.
    bands = t.get("bands") or []
    bars = pygame.Rect(inner.x, meter.bottom + pad // 2, inner.width * 3 // 4, 0)
    bars.height = inner.bottom - bars.y
    if bands:
        gap = max(2, bars.width // (len(bands) * 6))
        bw = (bars.width - gap * (len(bands) - 1)) // len(bands)
        for i, v in enumerate(bands):
            h = int(bars.height * min(max(v, 0.0), 1.0))
            colour = RED if hot else WHITE
            rect = pygame.Rect(bars.x + i * (bw + gap), bars.bottom - h, bw, h)
            pygame.draw.rect(screen, colour, rect)

    # Right: what the booth is doing, in words.
    side = pygame.Rect(bars.right + pad, bars.y, inner.right - bars.right - pad, bars.height)
    if not t:
        lines = [("caméra hors ligne", AMBER)]
    else:
        lines = []
        if not t.get("camera"):
            lines.append(("appareil déconnecté", AMBER))
        if t.get("mic_silent"):
            lines.append(("micro muet", AMBER))
        if not t.get("scream_mode"):
            lines.append(("cri : OFF", DIM))
        elif state in ("idle", "arming") and int(now * 2) % 2 == 0:
            lines.append(("CRIEZ !", RED))
        elif state == "cooldown":
            lines.append(("...", DIM))
        lines.append((f"{t.get('trigger_db', 0):.0f} dB · {t.get('shots', 0)} photos", DIM))
    y = side.y
    for text, colour in lines:
        size = side.height // 2 if text == "CRIEZ !" else max(14, side.height // 6)
        img = font(size).render(text, True, colour)
        screen.blit(img, (side.x, y))
        y += img.get_height() + 4
