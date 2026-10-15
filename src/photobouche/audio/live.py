# Copied from treek 0.20.5 (src/treek/audio/live.py). Changes: imports, `bands`,
# `active_duration`, error messages. Keep in sync by hand.
"""Live microphone input.

The device is chosen **by name**, never by index or by the system default. On
this machine the default input is a Bluetooth headset rather than the webcam mic
pointed at the scene, and on a Pi the ALSA card order changes across reboots.
"""

from __future__ import annotations

import contextlib
import queue
import threading

import numpy as np

from photobouche.audio.detector import DetectorConfig, Scream, ScreamDetector


class AudioError(Exception):
    """Raised when the microphone cannot be opened."""


def list_microphones() -> list[dict]:
    """Input devices, with the system default marked."""
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise AudioError("Needs the audio extra: pip install 'photobouche[booth]'") from exc

    default = sd.default.device[0]
    default_hostapi = sd.default.hostapi
    out = []
    for index, device in enumerate(sd.query_devices()):
        if device["max_input_channels"] > 0:
            out.append(
                {
                    "index": index,
                    "name": device["name"],
                    "channels": device["max_input_channels"],
                    "samplerate": int(device["default_samplerate"]),
                    "default": index == default,
                    "default_hostapi": device["hostapi"] == default_hostapi,
                }
            )
    return out


def resolve_microphone(name: str | int | None) -> int:
    """Name (or index) to a sounddevice index."""
    if isinstance(name, int):
        return name
    if name is None or str(name).strip() == "":
        raise AudioError(
            "No microphone named. Pass one from `photobouche mics` — never rely on the "
            "system default, which is often a headset rather than the mic pointed "
            "at the scene."
        )
    text = str(name).strip()
    if text.isdigit():
        return int(text)

    mics = list_microphones()
    lowered = text.lower()
    exact = [m for m in mics if lowered == m["name"].lower()]
    partial = [m for m in mics if lowered in m["name"].lower()]
    matches = exact or partial
    if len(matches) == 1:
        return matches[0]["index"]
    if len(matches) > 1 and _one_device(matches):
        # Windows lists each mic once per host API (MME, DirectSound, WASAPI,
        # WDM-KS). Any of them is the same mic; the default host API opens
        # most reliably.
        return min(matches, key=lambda m: (not m.get("default_hostapi"), m["index"]))["index"]
    if len(matches) > 1:
        options = ", ".join(f"{m['name']!r} ({m['index']})" for m in matches)
        raise AudioError(f"Microphone {name!r} is ambiguous: {options}.")
    available = ", ".join(repr(m["name"]) for m in mics) or "none found"
    raise AudioError(f"No microphone matching {name!r}. Available: {available}.")


def _one_device(mics: list[dict]) -> bool:
    """True when every entry names the same mic. MME cuts names at 31 characters."""
    names = [m["name"].lower() for m in mics]
    shortest = min(names, key=len)
    return all(n.startswith(shortest) for n in names)


class LiveAudioSource:
    """A microphone feeding the scream detector on a background thread."""

    def __init__(
        self,
        device: str | int | None = None,
        *,
        samplerate: int = 48000,
        blocksize: int = 1024,
        config: DetectorConfig | None = None,
    ) -> None:
        self.device = resolve_microphone(device)
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.detector = ScreamDetector(
            config or DetectorConfig(samplerate=samplerate)
        )
        self._stream = None
        self._blocks: queue.Queue = queue.Queue(maxsize=32)
        self._screams: list[Scream] = []
        self._lock = threading.Lock()
        self._silent_blocks = 0
        self.saw_audio = False
        # Other consumers of the same stream (the voice commands): each gets
        # every block, on the audio thread, and must not block.
        self._listeners: list = []

    def add_listener(self, listener) -> None:
        """Hand every captured block to ``listener.feed(block)`` as well."""
        self._listeners.append(listener)

    # ---------------------------------------------------------------- capture

    def _callback(self, indata, frames, time_info, status) -> None:
        # Dropping a block is better than stalling the audio thread; the
        # detector's floor tracking tolerates a gap.
        block = np.array(indata[:, 0], dtype=np.float32)
        with contextlib.suppress(queue.Full):
            self._blocks.put_nowait(block)
        for listener in self._listeners:
            listener.feed(block)

    def _drain(self) -> None:
        while True:
            try:
                block = self._blocks.get_nowait()
            except queue.Empty:
                break
            if np.abs(block).max() < 1e-6:
                self._silent_blocks += 1
            else:
                self.saw_audio = True
                self._silent_blocks = 0
            found = self.detector.process(block)
            if found:
                with self._lock:
                    self._screams.extend(found)

    # ----------------------------------------------------------------- source

    @property
    def level(self) -> float:
        self._drain()
        return self.detector.level

    @property
    def screaming(self) -> bool:
        self._drain()
        return self.detector.screaming

    @property
    def active_duration(self) -> float:
        self._drain()
        return self.detector.active_duration

    @property
    def bands(self) -> list[float]:
        self._drain()
        return list(self.detector.bands)

    @property
    def bass(self) -> float:
        self._drain()
        return self.detector.bass

    @property
    def treble(self) -> float:
        self._drain()
        return self.detector.treble

    def poll(self) -> list[Scream]:
        self._drain()
        with self._lock:
            out, self._screams = self._screams, []
        return out

    @property
    def silent(self) -> bool:
        """True once enough consecutive blocks have been digital silence.

        On macOS this usually means microphone permission was denied to the
        parent application — which fails with no error and no prompt.
        """
        return self._silent_blocks > 40 and not self.saw_audio

    def start(self) -> None:
        if self._stream is not None:
            return
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise AudioError("Needs the audio extra: pip install 'photobouche[booth]'") from exc

        try:
            self._stream = sd.InputStream(
                device=self.device,
                channels=1,
                samplerate=self.samplerate,
                blocksize=self.blocksize,
                dtype="float32",
                callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:
            raise AudioError(f"Could not open microphone {self.device}: {exc}") from exc

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def __enter__(self) -> LiveAudioSource:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
