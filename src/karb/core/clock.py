"""Time, injected.

Everything that asks what time it is -- tradeability cut-offs, opportunity lifetimes, the
rate limiter -- asks a ``Clock``, so tests can hold time still or move it on demand.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

__all__ = ["Clock", "FakeClock", "SystemClock"]


class Clock(Protocol):
    def now(self) -> datetime:
        """Timezone-aware UTC wall time."""
        ...

    def monotonic(self) -> float:
        """Seconds on a clock that never goes backwards. Only differences are meaningful."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


@dataclass
class FakeClock:
    wall: datetime
    mono: float = 0.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += timedelta(seconds=seconds)
