"""Transactional workflow transition handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from sqlmodel import Session

    from workflow.contracts import NodeDecision, TransitionResult
    from workflow.handlers.execution import ExecutionRequest

from workflow.handlers.attempts import (
    AttemptStartState,
    as_utc,
    execute_start_node_attempt,
    load_attempt,
    load_attempt_outcome,
    record_failure_outcome,
    record_obsolete_outcome,
    record_success_outcome,
)
from workflow.handlers.planning import (
    execute_planning_request,
    validate_planning_review,
)
from workflow.handlers.product_definition import (
    execute_decide_backlog,
    execute_record_backlog_draft,
    validate_decide_backlog_review,
)
from workflow.handlers.product_discovery import (
    execute_complete_specification_structuring,
    execute_decide_specification,
    execute_register_specification_source,
)
from workflow.handlers.product_goal import (
    execute_abandon_product_goal,
    execute_decide_product_goal_review,
    execute_fulfill_product_goal,
    execute_record_product_goal_interview_turn,
)
from workflow.handlers.project import (
    execute_create_project,
    execute_record_repository_binding,
)
from workflow.handlers.vision import (
    execute_begin_vision_revision,
    execute_decide_vision_review,
    execute_generate_vision_bootstrap,
    execute_record_vision_interview_turn,
)


def execute_execution_request(
    session: Session,
    request: ExecutionRequest,
    decision: NodeDecision,
    evaluated_at: datetime,
) -> TransitionResult:
    """Defer execution-service loading until an execution transition runs."""
    from workflow.handlers.execution import (  # noqa: PLC0415
        execute_execution_request as execute,
    )

    return execute(session, request, decision, evaluated_at)


__all__ = [
    "AttemptStartState",
    "as_utc",
    "execute_abandon_product_goal",
    "execute_begin_vision_revision",
    "execute_complete_specification_structuring",
    "execute_create_project",
    "execute_decide_backlog",
    "execute_decide_product_goal_review",
    "execute_decide_specification",
    "execute_decide_vision_review",
    "execute_execution_request",
    "execute_fulfill_product_goal",
    "execute_generate_vision_bootstrap",
    "execute_planning_request",
    "execute_record_backlog_draft",
    "execute_record_product_goal_interview_turn",
    "execute_record_repository_binding",
    "execute_record_vision_interview_turn",
    "execute_register_specification_source",
    "execute_start_node_attempt",
    "load_attempt",
    "load_attempt_outcome",
    "record_failure_outcome",
    "record_obsolete_outcome",
    "record_success_outcome",
    "validate_decide_backlog_review",
    "validate_planning_review",
]
