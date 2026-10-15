"""When a scream fires the shutter. Pure logic: fed numbers, returns decisions.

The detector says a scream has lasted so long; this decides whether that is the
moment. It fires once per scream, part-way in, then waits both for the scream
to end and for the cooldown before it can fire again. Without the first rule, a
four-second roar would fire twice; without the second, a group screaming in
turn would outrun the flashes.
"""

from __future__ import annotations

from dataclasses import dataclass

IDLE = "idle"
ARMING = "arming"
FIRED = "fired"
COOLDOWN = "cooldown"
OFF = "off"

FIRED_DISPLAY = 0.6
"""Seconds the state stays `fired`, so the screen has time to flash."""


@dataclass
class ScreamTrigger:
    fire_after: float = 0.25
    cooldown: float = 3.0
    enabled: bool = True

    def __post_init__(self) -> None:
        self.state = IDLE if self.enabled else OFF
        self._fired_at = -1e9
        self._last_shot = -1e9
        self._need_release = False

    def shot_taken(self, now: float) -> None:
        """A shot from any source (the remote too) starts the cooldown."""
        self._last_shot = now

    def update(self, now: float, active_duration: float, busy: bool = False) -> bool:
        """Advance to `now`. True means: fire the shutter now."""
        if active_duration <= 0.0:
            self._need_release = False

        fire = False
        if not self.enabled:
            self.state = OFF
        elif now - self._fired_at < FIRED_DISPLAY:
            self.state = FIRED
        elif now - self._last_shot < self.cooldown or busy:
            self.state = COOLDOWN
        elif active_duration <= 0.0 or self._need_release:
            self.state = IDLE
        elif active_duration < self.fire_after:
            self.state = ARMING
        else:
            fire = True
            self._fired_at = self._last_shot = now
            self._need_release = True
            self.state = FIRED
        return fire
