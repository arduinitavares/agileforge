"""Bind a durable Specification attempt check to its leaf-call context."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterator

    from workflow.contracts import TransitionResult


class SpecificationAttemptRevalidator(Protocol):
    """Callable authority check run at the Specification leaf boundary."""

    def __call__(
        self,
        phase: Literal["before_provider", "after_provider"],
        /,
    ) -> TransitionResult:
        """Return the current durable attempt authority result."""
        ...


_SPECIFICATION_ATTEMPT_REVALIDATOR: ContextVar[
    SpecificationAttemptRevalidator | None
] = ContextVar("specification_attempt_revalidator", default=None)


@contextmanager
def bind_specification_attempt_revalidator(
    revalidator: SpecificationAttemptRevalidator | None,
) -> Iterator[None]:
    """Bind one runner-local check and restore the prior async context."""
    token = _SPECIFICATION_ATTEMPT_REVALIDATOR.set(revalidator)
    try:
        yield
    finally:
        _SPECIFICATION_ATTEMPT_REVALIDATOR.reset(token)


def revalidate_specification_attempt(
    phase: Literal["before_provider", "after_provider"],
) -> TransitionResult | None:
    """Run the bound check, or allow direct provider-free recipe tests."""
    revalidator = _SPECIFICATION_ATTEMPT_REVALIDATOR.get()
    return None if revalidator is None else revalidator(phase)


__all__ = [
    "SpecificationAttemptRevalidator",
    "bind_specification_attempt_revalidator",
    "revalidate_specification_attempt",
]
