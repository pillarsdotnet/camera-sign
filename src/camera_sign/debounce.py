# Copyright (c) 2026 Robert August Vincent II <pillarsdotnet@gmail.com>
# Co-author: Claude Code.
"""Decide whether the sign should be lit, ignoring brief blips.

Browsers open the camera for a moment just to list devices, and a call
can drop the camera for a second when switching between devices.  The
sign should light only once the camera has stayed open for a while, and
go dark only once it has stayed closed for a while.
"""

from dataclasses import dataclass, field


@dataclass
class Debouncer:
    """Turn raw "camera open" samples into a steady on/off decision."""

    on_delay: float = 2.0
    off_delay: float = 5.0
    lit: bool = False
    _since: float | None = field(default=None, repr=False)

    def update(self, in_use: bool, now: float) -> bool:
        """Record one sample taken at monotonic time `now`; return the decision."""
        if in_use == self.lit:
            self._since = None
            return self.lit
        if self._since is None:
            self._since = now
        delay = self.on_delay if in_use else self.off_delay
        if now - self._since >= delay:
            self.lit = in_use
            self._since = None
        return self.lit
