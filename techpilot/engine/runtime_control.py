"""Cancellation and budget controls shared by the Agent runtime."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Lock


class RuntimeCancelled(Exception):
    """Raised at a cooperative boundary after a caller cancels a turn."""


class RuntimeLimitExceeded(Exception):
    """Raised when an explicit runtime budget reaches its configured limit."""

    def __init__(self, limit: str, actual: float, maximum: float) -> None:
        self.limit = limit
        self.actual = actual
        self.maximum = maximum
        super().__init__(f"{limit} limit reached ({actual} / {maximum})")


class CancellationToken:
    """Thread-safe, cooperative cancellation signal for one Agent turn."""

    def __init__(self) -> None:
        self._event = Event()
        self._lock = Lock()
        self._reason = "cancelled"

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def cancel(self, reason: str = "cancelled") -> None:
        with self._lock:
            self._reason = reason
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise RuntimeCancelled(self.reason)


@dataclass(frozen=True)
class RuntimeLimits:
    """Optional limits. ``None`` preserves the existing unrestricted behavior."""

    max_provider_calls: int | None = None
    max_tool_rounds: int | None = None
    max_turn_seconds: float | None = None
    max_input_tokens: int | None = None
    max_total_tokens: int | None = None
    max_cost_usd: float | None = None

    def __post_init__(self) -> None:
        for name in (
            "max_provider_calls",
            "max_tool_rounds",
            "max_input_tokens",
            "max_total_tokens",
        ):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive")
        for name in ("max_turn_seconds", "max_cost_usd"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive")
