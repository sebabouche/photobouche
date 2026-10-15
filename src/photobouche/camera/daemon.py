"""`photobouche camera`: the one process that owns the 5D.

Two threads:

* the **camera thread** waits for camera events, downloads each shot and
  develops it into `display/` — a CR2 takes about a second over USB 2, and
  nothing else should stall while it does;
* the **main thread** listens to the microphone, decides when a scream fires,
  and streams readings to the control screen.

They meet through a queue (fire requests) and a flag (busy downloading).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path

from photobouche.camera.backends import CameraError, FakeCamera, Gphoto2Camera
from photobouche.camera.hook import Developer
from photobouche.camera.telemetry import TelemetryServer
from photobouche.camera.trigger import ScreamTrigger
from photobouche.config import Config

log = logging.getLogger(__name__)

TICK = 1 / 30


class CameraWorker(threading.Thread):
    def __init__(self, cfg: Config, *, fake: bool) -> None:
        super().__init__(daemon=True, name="camera")
        self.cfg = cfg
        self.fake = fake
        self.requests: queue.Queue[float] = queue.Queue()
        self.shots: queue.Queue[Path] = queue.Queue()
        self.busy = False
        self.connected = False
        self.last_latency: float | None = None
        self._stop = threading.Event()
        self.developer = Developer(
            cfg.paths.display, cfg.paths.archive, cfg.camera.display_size
        )
        self.download_dir = cfg.paths.root / "incoming"

    def _open(self):
        if self.fake:
            return FakeCamera(self.download_dir)
        return Gphoto2Camera(self.download_dir)

    def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                camera = self._open()
            except CameraError as exc:
                self.connected = False
                log.warning("%s — retrying in %.0fs", exc, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 10.0)
                continue
            backoff = 1.0
            self.connected = True
            try:
                self._loop(camera)
            except CameraError as exc:
                log.warning("camera lost: %s", exc)
            finally:
                self.connected = False
                camera.close()

    def _loop(self, camera) -> None:
        fired_at: float | None = None
        while not self._stop.is_set():
            try:
                requested = self.requests.get_nowait()
            except queue.Empty:
                requested = None
            if requested is not None:
                fired_at = time.monotonic()
                log.info("fire (requested %.0f ms ago)", (fired_at - requested) * 1000)
                camera.trigger()

            files = camera.poll(0.05)
            if not files:
                continue
            self.busy = True
            try:
                if fired_at is not None:
                    # Trigger to file-on-card: an upper bound on shutter lag, logged on
                    # every shot so a slow body shows up in the journal.
                    self.last_latency = time.monotonic() - fired_at
                    log.info("shot arrived %.0f ms after trigger", self.last_latency * 1000)
                    fired_at = None
                for f in files:
                    out = self.developer.develop(f)
                    if out is not None:
                        log.info("new shot %s", out.name)
                        self.shots.put(out)
            finally:
                self.busy = False

    def stop(self) -> None:
        self._stop.set()


def run_daemon(cfg: Config, *, fake: bool = False, mic: str | None = None) -> None:
    from photobouche.audio.detector import DetectorConfig

    worker = CameraWorker(cfg, fake=fake)
    worker.start()
    server = TelemetryServer(cfg.paths.socket)
    trigger = ScreamTrigger(
        fire_after=cfg.camera.fire_after,
        cooldown=cfg.camera.cooldown,
        enabled=cfg.camera.scream_mode,
    )

    mic = mic if mic is not None else cfg.camera.mic
    audio = None
    if mic:
        from photobouche.audio.live import LiveAudioSource

        audio = LiveAudioSource(
            mic,
            config=DetectorConfig(
                trigger_db=cfg.camera.trigger_db,
                release_db=max(cfg.camera.trigger_db - 7.0, 1.0),
            ),
        )
        audio.start()
        log.info("listening on %r", mic)
    else:
        log.warning("no microphone configured: scream mode unavailable")

    shot_count = 0
    last_shot_name = None
    try:
        while True:
            now = time.monotonic()
            for msg in _drain(server.commands):
                _command(msg, cfg, trigger, worker, audio)

            while not worker.shots.empty():
                last_shot_name = worker.shots.get_nowait().name
                shot_count += 1
                trigger.shot_taken(now)

            if audio is not None:
                active = audio.active_duration
                if trigger.update(now, active, busy=worker.busy):
                    worker.requests.put(now)
                det = audio.detector
                reading = {
                    "level": round(det.level, 3),
                    "bands": [round(b, 3) for b in det.bands],
                    "threshold": round(
                        det.config.trigger_db / det.config.level_range_db, 3
                    ),
                    "trigger_db": det.config.trigger_db,
                    "screaming": det.screaming,
                    "mic_silent": audio.silent,
                }
            else:
                trigger.update(now, 0.0, busy=worker.busy)
                reading = {"level": 0.0, "bands": [], "threshold": 0.0, "mic_silent": True}

            server.broadcast(
                {
                    **reading,
                    "state": trigger.state,
                    "scream_mode": trigger.enabled,
                    "camera": worker.connected,
                    "busy": worker.busy,
                    "shots": shot_count,
                    "last_shot": last_shot_name,
                    "latency": worker.last_latency,
                }
            )
            time.sleep(max(0.0, TICK - (time.monotonic() - now)))
    finally:
        worker.stop()
        server.close()
        if audio is not None:
            audio.stop()


def _drain(q: queue.Queue) -> list:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def _command(msg: dict, cfg: Config, trigger: ScreamTrigger, worker: CameraWorker, audio) -> None:
    cmd = msg.get("cmd")
    if cmd == "shoot":
        worker.requests.put(time.monotonic())
    elif cmd == "scream_mode":
        value = msg.get("value")
        trigger.enabled = (not trigger.enabled) if value is None else bool(value)
        cfg.camera.scream_mode = trigger.enabled
        cfg.save_state()
        log.info("scream mode %s", "on" if trigger.enabled else "off")
    elif cmd == "threshold" and audio is not None:
        det = audio.detector.config
        det.trigger_db = min(max(det.trigger_db + float(msg.get("delta", 0)), 3.0), 40.0)
        # Hysteresis follows, keeping the gap the detector was tuned with.
        det.release_db = max(det.trigger_db - 7.0, 1.0)
        cfg.camera.trigger_db = det.trigger_db
        cfg.save_state()
        log.info("trigger threshold %.0f dB", det.trigger_db)
