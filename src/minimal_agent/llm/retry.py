"""Bounded retry policy for LLM requests."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_retries: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 2.0

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays must not be negative")

    @property
    def max_attempts(self) -> int:
        return self.max_retries + 1

    def delay_for_retry(self, retry_number: int) -> float:
        if retry_number < 1:
            raise ValueError("retry_number must be at least 1")
        return min(self.base_delay_seconds * (2 ** (retry_number - 1)), self.max_delay_seconds)
