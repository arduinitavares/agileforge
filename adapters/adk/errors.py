"""Typed ADK execution errors that preserve workflow failure codes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from workflow.contracts import TransitionResult, WorkflowErrorCode


@dataclass
class AttemptRevalidationError(RuntimeError):
    """Carry a non-provider authority result out of ADK execution."""

    result: TransitionResult


class AttemptRevalidationInfrastructureError(RuntimeError):
    """Separate leaf-boundary host failures from Specification producer work."""

    def __init__(self) -> None:
        """Use one bounded durable diagnostic for every host-side cause."""
        super().__init__("Specification attempt revalidation failed.")


@dataclass
class VisionAgenticPreflightError(RuntimeError):
    """Raised before any Vision provider call when trusted preflight fails."""

    code: WorkflowErrorCode
    message: str

    def __str__(self) -> str:
        """Render the durable preflight message for ADK error propagation."""
        return self.message


@dataclass
class SpecificationAgenticExecutionError(RuntimeError):
    """Typed failure for a malformed Specification producer result."""

    code: WorkflowErrorCode
    message: str

    def __str__(self) -> str:
        """Render the bounded durable failure message."""
        return self.message


__all__ = [
    "AttemptRevalidationError",
    "AttemptRevalidationInfrastructureError",
    "SpecificationAgenticExecutionError",
    "VisionAgenticPreflightError",
]
