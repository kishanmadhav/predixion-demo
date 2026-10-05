"""Retry policy: bounded attempts, exponential backoff with full jitter.

Full jitter (delay ~ U(0, min(cap, base * 2^n))) is used rather than fixed
exponential backoff so that many calls failing at the same instant do not retry
in lock-step and hammer a recovering provider together.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from voice_agent.providers.base import ProviderError


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.1
    max_delay: float = 2.0
    rng: random.Random = field(default_factory=random.Random, compare=False)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay < 0:
            raise ValueError("base_delay must be >= 0")
        if self.max_delay < self.base_delay:
            raise ValueError("max_delay must be >= base_delay")

    def should_retry(self, *, attempt_number: int, error: ProviderError) -> bool:
        """`attempt_number` is 1-based: the attempt that just failed."""
        return error.retryable and attempt_number < self.max_attempts

    def backoff(self, attempt_index: int, error: ProviderError) -> float:
        """Seconds to wait before the next attempt. `attempt_index` is 0-based."""
        if error.retry_after is not None:
            # The provider told us when it expects to be ready; trust it, within reason.
            return min(self.max_delay, max(0.0, error.retry_after))
        ceiling = min(self.max_delay, self.base_delay * (2**attempt_index))
        return self.rng.uniform(0.0, ceiling)
