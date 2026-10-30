"""Per-case time budget."""

from __future__ import annotations

import time
import math
from dataclasses import dataclass, field


@dataclass
class Deadline:
    seconds: float
    started: float = field(default_factory=time.monotonic)

    def remaining(self) -> float:
        return max(0.0, self.seconds - (time.monotonic() - self.started))

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def require(self, needed: float, label: str) -> None:
        if self.remaining() < needed:
            raise TimeoutError(f"not enough budget for {label}: {self.remaining():.1f}s left")

    def timeout(self, maximum: float, *, reserve: float = 0.0,
                minimum: float = 1.0, label: str = "operation") -> float:
        """Bound one operation by this deadline without spending reserved time."""
        if not math.isfinite(maximum) or maximum <= 0:
            raise ValueError("maximum timeout must be positive and finite")
        if not math.isfinite(reserve) or reserve < 0:
            raise ValueError("reserve must be non-negative and finite")
        available = min(maximum, self.remaining() - reserve)
        if available < minimum:
            raise TimeoutError(f"not enough budget for {label}: {max(0.0, available):.1f}s available")
        return available
