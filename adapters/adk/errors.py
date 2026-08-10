"""Typed ADK execution errors that preserve workflow failure codes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from workflow.contracts import WorkflowErrorCode


@dataclass
class VisionAgenticPreflightError(RuntimeError):
    """Raised before any Vision provider call when trusted preflight fails."""

    code: WorkflowErrorCode
    message: str

    def __str__(self) -> str:
        """Render the durable preflight message for ADK error propagation."""
        return self.message


__all__ = ["VisionAgenticPreflightError"]
