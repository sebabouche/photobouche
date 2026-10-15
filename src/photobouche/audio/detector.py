# Copied from treek 0.20.5 (src/treek/audio/detector.py). Changes: Scream inlined,
# `bands` (equalizer) and `active_duration` (fire rule) added. Keep in sync by hand.
"""Deciding whether a noise is a scream.

A forest is not quiet: wind, rustling, traffic, people talking. Loudness alone
fires on all of it, so a scream is recognised by three things together:

* **loud relative to the ambient floor**, not loud in absolute terms — the floor
  adapts, so the installation works in a windy clearing and a still one;
* **bright** — screams put energy high in the spectrum, where wind and footsteps
  do not (a high spectral centroid);
* **sustained** — a door slam is loud and bright but over in 50 ms.

Then hysteresis and a refractory period stop one long shout firing twenty times.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class Scream:
    """A detected scream."""

    time: float
    """Seconds since the detector started when it began."""

    loudness: float
    """0-1, how far above the ambient noise floor."""

    duration: float
    """Seconds the shout was sustained."""


@dataclass
class DetectorConfig:
    """Tuning. The defaults are a starting point, not gospel — a real site wants
    a pass with `photobouche listen` to see what the ambient floor actually is."""

    samplerate: int = 48000

    trigger_db: float = 15.0
    """How far above the noise floor counts as loud (dB)."""

    release_db: float = 8.0
    """Hysteresis: fall this far above the floor to end a scream. Lower than
    trigger_db so a wavering voice does not chop into several events."""

    min_centroid_hz: float = 1200.0
    """Screams are bright. Wind and rumble are not."""

    min_duration: float = 0.30
    """A slam is loud and bright but brief."""

    max_duration: float = 8.0
    """Beyond this it is a siren or a fault, not a scream."""

    refractory: float = 1.0
    """Ignore new screams for this long after one ends."""

    onset: float = 0.06
    """How long a loud, bright sound must last before ``screaming`` turns on.
    Short on purpose: ``screaming`` is for light that follows the voice. A
    door slam can open it for a moment; the ``Scream`` event, which needs
    ``min_duration``, still filters slams out."""

    max_dark_blocks: int = 3
    """Abandon a scream after this many consecutive blocks below the centroid
    threshold. Guards against a bright transient opening on a dark sound."""

    floor_rise: float = 0.02
    """How fast the noise floor rises, per block (slow: do not adapt to a voice)."""

    floor_fall: float = 0.7
    """How fast the noise floor falls, per block (fast: track a room going quiet)."""

    level_smoothing: float = 0.25
    """Smoothing for ctx.level. Lower is smoother."""

    level_range_db: float = 35.0
    """dB above the floor that maps to ctx.level == 1.0."""

    tilt_neutral_db: float = -27.0
    """Spectral tilt (treble dB minus bass dB) of a sound that leans neither way.

    Measured on a voice at a metre or so: a shriek sat near -20 dB and a chest
    shout near -33 dB, so the middle is about -27."""

    tilt_spread_db: float = 11.0
    """How many dB of tilt it takes to go from 'leans low' to 'leans high'."""

    silence_db: float = -90.0
    """Below this, a block is a gap in the stream rather than a quiet room.

    One step of a 16-bit sample is -96 dBFS, so nothing a microphone actually
    captured sits below this; a real room measured -42 dB in a silent forest.
    Gated inputs measured -120 and -188 dB, and one path sat at -97 dB — a
    muted 16-bit stream wobbling by a single LSB — which is why the threshold
    is above -96 rather than at the very bottom of the scale."""

    bass_hz: tuple[float, float] = (150.0, 700.0)
    """The low band behind ``ctx.bass``: the chest register of a voice.

    Not 40-250 Hz, which sounds like the right answer and is not: measured on a
    busy workshop, a band down there hears the room's rumble and a shout barely
    moves it (0.69 against 0.65 for the ambience). Moved onto the voice's own
    fundamental it reaches 1.00 on the same shouts."""

    treble_hz: tuple[float, float] = (3000.0, 9000.0)
    """The high band behind ``ctx.treble``: shriek, hiss, sibilance.

    A loud low shout is broadband and lifts this band too; raising the floor of
    it is the obvious cure and it backfires, because real shouts have little
    left above 4 kHz — measured, the same shouts fell to 0.78 at 4-11 kHz and
    0.50 at 5-12 kHz. This is the compromise.

    Deliberately above ``min_centroid_hz``: the centroid decides whether a sound
    is a scream at all, these two only describe its colour."""

    eq_bands: int = 16
    """Number of equalizer bars on the control screen."""

    eq_hz: tuple[float, float] = (80.0, 12000.0)
    """Range covered by the equalizer, split into log-spaced bands."""


@dataclass
class _ScreamInProgress:
    start_time: float
    peak_excess: float = 0.0
    frames: int = 0
    dark_frames: int = 0
    """Consecutive blocks that were loud but not bright. A rumble's onset is
    briefly broadband enough to look like a scream; its body is not."""


@dataclass
class _BandFollower:
    """One frequency band's own adaptive floor and its 0-1 value.

    Each band needs its OWN floor rather than sharing the broadband one. Wind is
    almost entirely low frequency, so in a forest the bass floor sits far above
    the treble floor; judged against a single number the bass would read as a
    permanent shout and the treble as permanent silence.
    """

    floor_db: float | None = None
    value: float = 0.0

    def update(self, db: float, cfg: DetectorConfig) -> float:
        # Same asymmetry as the broadband floor: fall fast to follow a site
        # going quiet, rise slowly, and freeze while the sound is clearly above.
        if self.floor_db is None:
            self.floor_db = db
        elif db > self.floor_db:
            if db - self.floor_db < cfg.release_db:
                self.floor_db += (db - self.floor_db) * cfg.floor_rise
        else:
            self.floor_db += (db - self.floor_db) * cfg.floor_fall

        excess = db - self.floor_db
        target = float(np.clip(excess / max(cfg.level_range_db, 1e-6), 0.0, 1.0))
        self.value += (target - self.value) * cfg.level_smoothing
        return self.value


@dataclass
class ScreamDetector:
    """Feed it audio blocks, get screams out.

    Stateless between runs, deterministic for a given input — which is what makes
    it testable against recorded WAVs rather than only against live shouting.
    """

    config: DetectorConfig = field(default_factory=DetectorConfig)

    def __post_init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.time = 0.0
        self.level = 0.0
        self.bass = 0.0
        self.treble = 0.0
        self.tilt_db = 0.0
        self.silent_blocks = 0
        """Consecutive blocks of exact digital silence — a gated or dead input."""
        self._bass = _BandFollower()
        self._treble = _BandFollower()
        self._eq = [_BandFollower() for _ in range(self.config.eq_bands)]
        self.bands = [0.0] * self.config.eq_bands
        """Equalizer: one 0-1 value per log-spaced band, each against its own floor."""
        self._floor_db: float | None = None
        self._active: _ScreamInProgress | None = None
        self._quiet_until = 0.0
        self._pending: list[Scream] = []
        self.last_db = -120.0
        self.last_centroid = 0.0

    # ------------------------------------------------------------------ maths

    def _rms_db(self, block: np.ndarray) -> float:
        rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64))))
        return 20.0 * np.log10(max(rms, 1e-10))

    def _spectrum(self, block: np.ndarray) -> tuple[np.ndarray | None, np.ndarray | None]:
        """One windowed FFT, shared by the centroid and the two bands."""
        if block.size < 32:
            return None, None
        windowed = block * np.hanning(block.size)
        spectrum = np.abs(np.fft.rfft(windowed))
        freqs = np.fft.rfftfreq(block.size, 1.0 / self.config.samplerate)
        return spectrum, freqs

    def _centroid_hz(self, block: np.ndarray) -> float:
        """Spectral centroid — the 'brightness' of the sound."""
        spectrum, freqs = self._spectrum(block)
        if spectrum is None:
            return 0.0
        return self._centroid_of(spectrum, freqs)

    @staticmethod
    def _centroid_of(spectrum: np.ndarray, freqs: np.ndarray) -> float:
        total = spectrum.sum()
        if total <= 1e-12:
            return 0.0
        return float((freqs * spectrum).sum() / total)

    @staticmethod
    def _band_db(spectrum: np.ndarray, freqs: np.ndarray, lo: float, hi: float) -> float:
        """Level inside one frequency band, in dB."""
        selected = spectrum[(freqs >= lo) & (freqs < hi)]
        if selected.size == 0:
            return -120.0
        power = float(np.mean(np.square(selected, dtype=np.float64)))
        return 10.0 * np.log10(max(power, 1e-20))

    # ------------------------------------------------------------------ input

    def process(self, block: np.ndarray) -> list[Scream]:
        """Process one block of mono float audio. Returns screams that just ended."""
        cfg = self.config
        block = np.asarray(block, dtype=np.float32).reshape(-1)
        if block.size == 0:
            return []

        duration = block.size / cfg.samplerate
        self.time += duration

        db = self._rms_db(block)

        # Below `silence_db` a block is a gap in the stream, not a quiet room.
        # Windows noise suppression, webcam drivers and "AI" mic utilities gate
        # to near-zero between sounds, and the floor falls fast by design — so a
        # single gated block drags it to the bottom of the scale, where it then
        # climbs back at floor_rise (slowly). Until it recovers, every rustle
        # sits 50 dB "above the floor" and reads as a permanent scream.
        # Measured on seven input paths of this project's own hardware: six
        # gated this way and pinned ctx.level near 1.0 in a silent room, with
        # floors at -120 and -188 dB. Note they are not EXACTLY zero — testing
        # for that catches nothing — which is why the threshold is a level.
        # Skipping leaves the floor, the level and any scream exactly as it was.
        if db <= cfg.silence_db:
            self.silent_blocks += 1
            return []
        self.silent_blocks = 0

        spectrum, freqs = self._spectrum(block)
        if spectrum is None:
            centroid = 0.0
        else:
            centroid = self._centroid_of(spectrum, freqs)
            # The colour of the sound, each band against its own floor. Computed
            # every block, whether or not a scream is in progress: `ctx.bass` and
            # `ctx.treble` describe the room continuously, like `level`.
            bass_db = self._band_db(spectrum, freqs, *cfg.bass_hz)
            treble_db = self._band_db(spectrum, freqs, *cfg.treble_hz)
            bass = self._bass.update(bass_db, cfg)
            treble = self._treble.update(treble_db, cfg)

            # A shout is BROADBAND: measured on its own, each band rises for any
            # loud sound, so a chest roar lit the treble and a shriek lit the
            # bass. What tells them apart is not how much energy sits in a band
            # but WHERE THE SPECTRUM LEANS — measured on a real voice, 10 to
            # 15 dB of tilt between a shriek and a roar, while the loudness of
            # the two barely differed.
            tilt = treble_db - bass_db
            share = (tilt - cfg.tilt_neutral_db) / max(cfg.tilt_spread_db, 1e-6) + 0.5
            share = float(np.clip(share, 0.0, 1.0))
            # A sound that leans neither way leaves both bands untouched, so a
            # full-throated shout can still light both at once; only a clearly
            # one-sided sound silences the other band.
            self.bass = bass * min(2.0 * (1.0 - share), 1.0)
            self.treble = treble * min(2.0 * share, 1.0)
            self.tilt_db = tilt
            self._update_bands(spectrum, freqs)
        self.last_db, self.last_centroid = db, centroid

        # Adaptive noise floor. Falls fast so the installation follows a room
        # going quiet; rises slowly, and not at all while the sound is clearly
        # above it. Without that freeze, 2 s of screaming lifted the floor ~85% of
        # the way to the scream and ctx.level sank while the person was still
        # screaming — invisible for an on/off effect, fatal for a gauge.
        if self._floor_db is None:
            self._floor_db = db
        elif db > self._floor_db:
            if db - self._floor_db < cfg.release_db:
                self._floor_db += (db - self._floor_db) * cfg.floor_rise
        else:
            self._floor_db += (db - self._floor_db) * cfg.floor_fall

        excess = db - self._floor_db

        target = float(np.clip(excess / max(cfg.level_range_db, 1e-6), 0.0, 1.0))
        self.level += (target - self.level) * cfg.level_smoothing

        loud_enough = excess >= cfg.trigger_db
        still_loud = excess >= cfg.release_db
        bright_enough = centroid >= cfg.min_centroid_hz

        out: list[Scream] = []

        if self._active is None:
            if loud_enough and bright_enough and self.time >= self._quiet_until:
                self._active = _ScreamInProgress(start_time=self.time - duration)
                self._active.peak_excess = excess
                self._active.frames = 1
        else:
            self._active.peak_excess = max(self._active.peak_excess, excess)
            self._active.frames += 1
            elapsed = self.time - self._active.start_time

            # Brightness must persist. Checking it only at onset lets the sharp
            # transient of a truck or a gust open a scream that its dark body
            # then sustains.
            if bright_enough:
                self._active.dark_frames = 0
            else:
                self._active.dark_frames += 1
            gone_dark = self._active.dark_frames >= cfg.max_dark_blocks

            if gone_dark:
                self._active = None
                return out

            if not still_loud or elapsed >= cfg.max_duration:
                if cfg.min_duration <= elapsed <= cfg.max_duration:
                    out.append(
                        Scream(
                            time=self._active.start_time,
                            loudness=float(
                                np.clip(self._active.peak_excess / cfg.level_range_db, 0.0, 1.0)
                            ),
                            duration=elapsed,
                        )
                    )
                    self._quiet_until = self.time + cfg.refractory
                self._active = None

        return out

    @property
    def screaming(self) -> bool:
        """True right now, while a scream is in progress past ``onset``.

        ``process()`` reports a ``Scream`` only once it has ENDED, so light
        driven by that event starts after the voice stops — measured in the
        workshop as a lag. This is the live signal: on ~60 ms into the scream,
        off the block it ends.
        """
        if self._active is None:
            return False
        return self.time - self._active.start_time >= self.config.onset

    def _update_bands(self, spectrum: np.ndarray, freqs: np.ndarray) -> None:
        lo, hi = self.config.eq_hz
        edges = np.geomspace(lo, hi, len(self._eq) + 1)
        for i, follower in enumerate(self._eq):
            # Low bands can be narrower than one FFT bin; widen to the nearest bin
            # rather than reading an empty band as silence.
            db = self._band_db(spectrum, freqs, edges[i], edges[i + 1])
            if db <= -120.0:
                nearest = int(np.argmin(np.abs(freqs - edges[i])))
                db = self._band_db(spectrum, freqs, freqs[nearest], freqs[nearest] + 1e-6)
            self.bands[i] = follower.update(db, self.config)

    @property
    def active_duration(self) -> float:
        """Seconds the scream in progress has lasted, 0 when there is none.

        The photo fires on this, part-way through the scream while the mouth is
        open: ``Scream`` events only arrive once the scream has ended.
        """
        if self._active is None:
            return 0.0
        return self.time - self._active.start_time

    @property
    def floor_db(self) -> float:
        """Current ambient noise floor, for `photobouche listen`."""
        return self._floor_db if self._floor_db is not None else -120.0
