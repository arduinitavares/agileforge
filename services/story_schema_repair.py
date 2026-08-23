"""Bounded provider-owned schema repair for Story generation."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.contracts.story import UserStoryWriterInput

MAX_STORY_SCHEMA_REPAIR_ATTEMPTS: int = 2
MAX_STORY_SCHEMA_REPAIR_FEEDBACK_CHARS: int = 4000


def with_story_schema_repair_feedback(
    payload: UserStoryWriterInput,
    *,
    error: str,
    validation_errors: object | None = None,
    targeted: bool,
) -> UserStoryWriterInput:
    """Append one bounded schema diagnostic without changing the trusted root."""
    details = ""
    if validation_errors is not None:
        details = json.dumps(validation_errors, sort_keys=True, default=str)
    cardinality = " Return exactly one user_stories item." if targeted else ""
    feedback = (
        "SYSTEM_FEEDBACK: Your previous User Story response failed schema or "
        "reference validation.\n"
        f"ERROR: {error}\n"
        f"VALIDATION_ERRORS: {details}\n"
        "Return JSON only. Match UserStoryWriterOutput exactly. Required fields "
        "are user_stories, is_complete, and clarifying_questions."
        f"{cardinality} Do not add wrapper fields."
    )[:MAX_STORY_SCHEMA_REPAIR_FEEDBACK_CHARS]
    user_input = feedback
    if payload.user_input is not None and payload.user_input.strip():
        user_input = f"{payload.user_input}\n\n{feedback}"
    return payload.model_copy(update={"user_input": user_input})
