from __future__ import annotations

from photobouche.camera.trigger import ARMING, COOLDOWN, FIRED, IDLE, OFF, ScreamTrigger


def run(trigger, samples, busy=False):
    """samples: (time, active_duration). Returns the times it fired."""
    return [t for t, d in samples if trigger.update(t, d, busy)]


def scream(start, length, step=0.02):
    n = int(length / step)
    return [(start + i * step, i * step + step) for i in range(n)] + [(start + length + step, 0.0)]


def test_fires_once_part_way_into_a_scream():
    tr = ScreamTrigger(fire_after=0.25, cooldown=3.0)
    fired = run(tr, scream(0.0, 4.0))
    assert len(fired) == 1
    assert 0.2 <= fired[0] <= 0.3


def test_short_sound_does_not_fire():
    tr = ScreamTrigger(fire_after=0.25)
    assert run(tr, scream(0.0, 0.15)) == []


def test_cooldown_then_next_scream_fires():
    tr = ScreamTrigger(fire_after=0.25, cooldown=3.0)
    fired = run(tr, scream(0.0, 0.5) + scream(1.0, 0.5) + scream(4.0, 0.5))
    assert len(fired) == 2
    assert fired[1] > 4.0


def test_remote_shot_starts_cooldown():
    tr = ScreamTrigger(fire_after=0.25, cooldown=3.0)
    tr.shot_taken(0.0)
    assert run(tr, scream(0.5, 1.0)) == []


def test_busy_blocks_firing():
    tr = ScreamTrigger(fire_after=0.25)
    assert run(tr, scream(0.0, 1.0), busy=True) == []


def test_disabled_never_fires():
    tr = ScreamTrigger(enabled=False)
    assert run(tr, scream(0.0, 2.0)) == []
    assert tr.state == OFF


def test_states_for_the_screen():
    tr = ScreamTrigger(fire_after=0.25, cooldown=3.0)
    tr.update(0.0, 0.0)
    assert tr.state == IDLE
    tr.update(0.1, 0.1)
    assert tr.state == ARMING
    tr.update(0.3, 0.3)
    assert tr.state == FIRED
    tr.update(1.0, 1.0)
    assert tr.state == COOLDOWN
