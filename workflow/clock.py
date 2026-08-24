"""Clock contracts for deterministic workflow graph evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """Source of evaluation time for the workflow domain."""

    def now(self) -> datetime:
        """Return the current evaluation time."""
        ...


class SystemClock:
    """Clock backed by the system UTC time."""

    def now(self) -> datetime:
        """Return the current UTC time."""
        return datetime.now(UTC)


@dataclass(frozen=True)
class FixedClock:
    """Clock that always returns one configured time."""

    now_value: datetime

    def now(self) -> datetime:
        """Return the configured fixed time."""
        return self.now_value
