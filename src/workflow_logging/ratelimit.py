"""Log rate limiter to suppress noisy loggers."""
import time
from collections import defaultdict


class LogRateLimiter:
    """Token-bucket-like rate limiter for log messages per module."""

    def __init__(self, max_per_second: int = 0) -> None:
        self.max_per_second = max_per_second
        self._buckets: dict[str, tuple[int, float]] = defaultdict(lambda: (0, 0.0))

    def should_log(self, module: str) -> bool:
        if self.max_per_second <= 0:
            return True
        now = time.monotonic()
        count, window_start = self._buckets[module]
        if now - window_start > 1.0:
            count = 0
            window_start = now
        if count >= self.max_per_second:
            return False
        count += 1
        self._buckets[module] = (count, window_start)
        return True
