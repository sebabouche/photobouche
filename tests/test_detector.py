"""The copied treek detector, plus what PhotoBouche added: bands and active_duration."""

from __future__ import annotations

import numpy as np

from photobouche.audio.detector import DetectorConfig, ScreamDetector

SR = 48000
BLOCK = 1024


def blocks(signal):
    for i in range(0, len(signal) - BLOCK + 1, BLOCK):
        yield signal[i : i + BLOCK]


def noise(seconds, amp=0.003, seed=0):
    return np.random.default_rng(seed).normal(0, amp, int(SR * seconds)).astype(np.float32)


def shriek(seconds, amp=0.4, seed=1):
    t = np.arange(int(SR * seconds)) / SR
    rng = np.random.default_rng(seed)
    tone = sum(np.sin(2 * np.pi * f * t) for f in (1800, 2700, 3600))
    return (amp * tone / 3 + rng.normal(0, amp * 0.01, t.size)).astype(np.float32)


def thud(seconds, amp=0.5):
    t = np.arange(int(SR * seconds)) / SR
    return (amp * np.sin(2 * np.pi * 90 * t)).astype(np.float32)


def feed(det, signal):
    durations, screams = [], []
    for b in blocks(signal):
        screams += det.process(b)
        durations.append(det.active_duration)
    return durations, screams


def test_scream_grows_active_duration_then_reports():
    det = ScreamDetector(DetectorConfig(samplerate=SR))
    feed(det, noise(2.0))
    durations, screams = feed(det, np.concatenate([shriek(1.0), noise(1.0)]))
    assert max(durations) >= 0.9
    assert len(screams) == 1
    assert det.active_duration == 0.0


def test_low_rumble_never_arms():
    det = ScreamDetector(DetectorConfig(samplerate=SR))
    feed(det, noise(2.0))
    durations, screams = feed(det, np.concatenate([thud(1.0), noise(1.0)]))
    assert max(durations) < 0.25
    assert screams == []


def test_bands_light_up_where_the_energy_is():
    cfg = DetectorConfig(samplerate=SR)
    det = ScreamDetector(cfg)
    assert len(det.bands) == cfg.eq_bands
    feed(det, noise(2.0))
    feed(det, shriek(0.5))
    edges = np.geomspace(*cfg.eq_hz, cfg.eq_bands + 1)
    hi = [i for i in range(cfg.eq_bands) if edges[i] >= 1500 and edges[i + 1] <= 4000]
    lo = [i for i in range(cfg.eq_bands) if edges[i + 1] <= 300]
    assert min(det.bands[i] for i in hi) > 0.3
    assert max(det.bands[i] for i in lo) < min(det.bands[i] for i in hi)
