from dataclasses import dataclass, field


@dataclass
class CircuitBreaker:
    """Tracks consecutive failures and opens when the threshold is reached.

    Every retry loop that can fail MUST be guarded by a CircuitBreaker.
    When open, the caller should stop retrying and surface an error.
    """

    name: str
    max_failures: int
    consecutive_failures: int = field(default=0)

    def record_failure(self) -> bool:
        """Record a failure. Returns True if the breaker just opened."""
        self.consecutive_failures += 1
        return self.is_open

    def record_success(self) -> None:
        """Reset the failure counter."""
        self.consecutive_failures = 0

    @property
    def is_open(self) -> bool:
        """True when the breaker has tripped and operations should be halted."""
        return self.consecutive_failures >= self.max_failures
