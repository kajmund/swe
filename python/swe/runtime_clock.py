"""Injectable monotonic clock for SWE timeout tests."""

from __future__ import annotations

import time
from dataclasses import dataclass


class WorkflowClock:
    def now_ms(self) -> int:
        return time.monotonic_ns() // 1_000_000


@dataclass
class FrozenClock:
    now: int = 0

    def now_ms(self) -> int:
        return self.now

    def advance(self, millis: int) -> None:
        self.now += millis
