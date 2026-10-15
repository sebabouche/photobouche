from __future__ import annotations

from collections import Counter
from itertools import pairwise

from photobouche.screen.scheduler import Media, Scheduler

NOW = 1_000_000.0


def img(name, live=False, age=10_000.0):
    return Media(f"{'live' if live else 'making_of'}/{name}", "image", name, NOW - age, live)


def vid(name):
    return Media(f"clips/{name}", "video", name)


def sched(**kw):
    kw.setdefault("seed", 1)
    return Scheduler(**kw)


def test_empty_returns_none():
    assert sched().next(NOW) is None


def test_fresh_shots_jump_the_line_in_order():
    s = sched()
    for i in range(5):
        s.add(img(f"m{i}"))
    s.add(img("new1", live=True), fresh=True)
    s.add(img("new2", live=True), fresh=True)
    a, b, c = s.next(NOW), s.next(NOW), s.next(NOW)
    assert (a.media.id, a.fresh, a.duration) == ("live/new1", True, 8.0)
    assert b.media.id == "live/new2"
    assert not c.fresh


def test_removed_fresh_shot_is_skipped():
    s = sched()
    s.add(img("bad", live=True), fresh=True)
    s.add(img("m"))
    s.remove("live/bad")
    assert s.next(NOW).media.id == "making_of/m"


def test_clip_every_n_images():
    s = sched(clip_every=3)
    for i in range(10):
        s.add(img(f"m{i}"))
    s.add(vid("c1"))
    s.add(vid("c2"))
    kinds = [s.next(NOW).media.kind for _ in range(12)]
    assert kinds == ["image"] * 3 + ["video"] + ["image"] * 3 + ["video"] + ["image"] * 3 + ["video"]


def test_clip_duration_is_open_ended():
    s = sched()
    s.add(vid("c"))
    assert s.next(NOW).duration is None


def test_no_repeat_window():
    s = sched(no_repeat=5, clip_every=0)
    for i in range(6):
        s.add(img(f"m{i}"))
    picks = [s.next(NOW).media.id for _ in range(60)]
    for i in range(len(picks) - 5):
        assert len(set(picks[i : i + 6])) == 6


def test_small_pool_never_repeats_back_to_back():
    s = sched(no_repeat=20, clip_every=0)
    s.add(img("a"))
    s.add(img("b"))
    picks = [s.next(NOW).media.id for _ in range(20)]
    assert all(x != y for x, y in pairwise(picks))


def test_recent_live_shots_are_favoured():
    s = sched(no_repeat=1, recent_weight=4.0, clip_every=0)
    for i in range(10):
        s.add(img(f"old{i}", live=True, age=10 * 3600))
    for i in range(10):
        s.add(img(f"new{i}", live=True, age=60))
    counts = Counter("new" in s.next(NOW).media.id for _ in range(4000))
    assert counts[True] / counts[False] > 2.5


def test_unusable_items_are_skipped():
    s = sched()
    s.add(img("cached"))
    s.add(vid("remote"))
    for _ in range(10):
        pick = s.next(NOW, usable=lambda m: m.kind == "image")
        assert pick.media.id == "making_of/cached"
