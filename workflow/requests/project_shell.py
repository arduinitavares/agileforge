"""Project-shell transition request contracts."""

from typing import ClassVar, Literal

from pydantic import Field

from workflow.contracts import FrozenModel
from workflow.requests.base import PositionedRequest


class OpenProjectShell(FrozenModel):
    """Create a new Project shell and its initial discovery run."""

    kind: Literal["open_project_shell"] = "open_project_shell"
    name: str = Field(min_length=1, max_length=200)
    origin: Literal["greenfield", "brownfield"]
    idempotency_key: str = Field(min_length=1, max_length=200)
    actor: str = Field(min_length=1, max_length=200)
    correlation_id: str | None = None


class AbandonProjectShell(PositionedRequest):
    """Record abandonment of a Project shell before accepted authority."""

    kind: Literal["abandon_project_shell"] = "abandon_project_shell"
    node_id: ClassVar[str] = "onboarding.abandon_shell"
    reason: str = Field(min_length=1)
