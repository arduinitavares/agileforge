# services/planning_artifact_content.py
"""Strict loading for immutable planning artifact content."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from services.contracts import sprint as sprint_contracts  # noqa: TC001
from services.contracts.backlog import (
    BacklogAgentItem,
    BacklogOutput,
    canonicalize_backlog_items,
)
from services.contracts.roadmap import (
    RoadmapBuilderOutput,
    validate_roadmap_backlog_coverage,
)
from services.contracts.specification_references import (
    AcceptedSpecificationReference,
)
from workflow.contracts import JsonObject
from workflow.fingerprints import (
    canonical_hash,
    canonical_json,
    canonical_stored_json_hash,
)

_JSON_OBJECT = TypeAdapter(JsonObject)

if TYPE_CHECKING:
    from services.specs.accepted_specification import AcceptedSpecification


class SprintPlanEnvelope(BaseModel):
    """Exact host-owned immutable Sprint plan review payload."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = Field(pattern=r"^agileforge\.sprint-plan-envelope\.v1$")
    team_name: str = Field(min_length=1)
    spec_version_id: int = Field(gt=0)
    spec_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    candidate_set_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    planner_output: sprint_contracts.SprintPlannerOutput

    @field_validator("team_name")
    @classmethod
    def reject_blank_team_name(cls, value: str) -> str:
        """Reject whitespace-only reviewed Team identities."""
        if not value.strip():
            message = "Sprint Team name must not be whitespace-only."
            raise ValueError(message)
        return value


def build_sprint_plan_envelope(
    *,
    team_name: str,
    spec_version_id: int,
    spec_hash: str,
    candidate_set_fingerprint: str,
    planner_output: sprint_contracts.SprintPlannerOutput,
) -> tuple[SprintPlanEnvelope, str, str]:
    """Build and hash the exact six-field host Sprint envelope once."""
    envelope = SprintPlanEnvelope(
        schema_version="agileforge.sprint-plan-envelope.v1",
        team_name=team_name,
        spec_version_id=spec_version_id,
        spec_hash=spec_hash,
        candidate_set_fingerprint=candidate_set_fingerprint,
        planner_output=planner_output,
    )
    payload = envelope.model_dump(mode="json")
    canonical_content_json = canonical_json(payload)
    return envelope, canonical_content_json, canonical_hash(payload)


def load_stored_sprint_plan_envelope(
    canonical_content_json: str,
    *,
    expected_fingerprint: str,
) -> SprintPlanEnvelope:
    """Load exact canonical host-envelope bytes or fail closed."""
    envelope = SprintPlanEnvelope.model_validate_json(canonical_content_json)
    payload = envelope.model_dump(mode="json")
    if canonical_json(payload) != canonical_content_json:
        message = "Stored Sprint plan envelope is not canonical."
        raise ValueError(message)
    if canonical_hash(payload) != expected_fingerprint:
        message = "Stored Sprint plan envelope fingerprint changed."
        raise ValueError(message)
    return envelope


def load_bound_sprint_plan_envelope(  # noqa: PLR0913
    canonical_content_json: str,
    *,
    expected_fingerprint: str,
    spec_version_id: int,
    spec_hash: str,
    candidate_set_fingerprint: str,
    selected_story_ids_json: str,
) -> SprintPlanEnvelope:
    """Load canonical envelope bytes and bind every duplicated durable column."""
    envelope = load_stored_sprint_plan_envelope(
        canonical_content_json,
        expected_fingerprint=expected_fingerprint,
    )
    if (
        envelope.spec_version_id != spec_version_id
        or envelope.spec_hash != spec_hash
        or envelope.candidate_set_fingerprint != candidate_set_fingerprint
        or canonical_json(
            [item.story_id for item in envelope.planner_output.selected_stories]
        )
        != selected_story_ids_json
    ):
        message = "Sprint plan artifact columns do not match its canonical envelope."
        raise ValueError(message)
    return envelope


def validate_canonical_planning_content[ContentT: BaseModel](
    canonical_content: JsonObject,
    *,
    content_type: type[ContentT],
) -> ContentT:
    """Validate one canonical JSON object without coercing JSON scalar types."""
    canonical_content_json = canonical_json(canonical_content)
    return _strict_canonical_planning_content(
        canonical_content_json,
        content_type=content_type,
    )


def load_stored_planning_artifact_content[ContentT: BaseModel](
    canonical_content_json: str,
    *,
    expected_fingerprint: str,
    content_type: type[ContentT],
) -> tuple[JsonObject, ContentT]:
    """Load exact canonical bytes as one strict planning content model."""
    parsed = _JSON_OBJECT.validate_json(canonical_content_json)
    content = _strict_canonical_planning_content(
        canonical_content_json,
        content_type=content_type,
    )
    if canonical_stored_json_hash(canonical_content_json) != expected_fingerprint:
        message = "Stored planning artifact content fingerprint changed."
        raise ValueError(message)
    return parsed, content


def validate_backlog_planning_content(
    canonical_content: JsonObject,
    *,
    content_fingerprint: str,
    specification: AcceptedSpecification,
) -> BacklogOutput:
    """Validate strict complete Backlog content against its pinned Specification."""
    content = validate_canonical_planning_content(
        canonical_content,
        content_type=BacklogOutput,
    )
    if canonical_hash(canonical_content) != content_fingerprint:
        message = "Backlog content fingerprint does not match canonical content."
        raise ValueError(message)
    _validate_backlog_semantics(content, specification=specification)
    return content


def load_stored_backlog_planning_content(
    canonical_content_json: str,
    *,
    expected_fingerprint: str,
    specification: AcceptedSpecification,
) -> tuple[JsonObject, BacklogOutput]:
    """Load strict complete Backlog bytes against their pinned Specification."""
    canonical_content, content = load_stored_planning_artifact_content(
        canonical_content_json,
        expected_fingerprint=expected_fingerprint,
        content_type=BacklogOutput,
    )
    _validate_backlog_semantics(content, specification=specification)
    return canonical_content, content


def validate_roadmap_planning_content(
    canonical_content: JsonObject,
    *,
    content_fingerprint: str,
    parent_backlog_item_ids: tuple[str, ...],
) -> RoadmapBuilderOutput:
    """Validate strict complete Roadmap content against its exact Backlog."""
    content = validate_canonical_planning_content(
        canonical_content,
        content_type=RoadmapBuilderOutput,
    )
    if canonical_hash(canonical_content) != content_fingerprint:
        message = "Roadmap content fingerprint does not match canonical content."
        raise ValueError(message)
    _validate_roadmap_semantics(
        content,
        parent_backlog_item_ids=parent_backlog_item_ids,
    )
    return content


def load_stored_roadmap_planning_content(
    canonical_content_json: str,
    *,
    expected_fingerprint: str,
    parent_backlog_item_ids: tuple[str, ...],
) -> tuple[JsonObject, RoadmapBuilderOutput]:
    """Load strict complete Roadmap bytes against their exact Backlog."""
    canonical_content, content = load_stored_planning_artifact_content(
        canonical_content_json,
        expected_fingerprint=expected_fingerprint,
        content_type=RoadmapBuilderOutput,
    )
    _validate_roadmap_semantics(
        content,
        parent_backlog_item_ids=parent_backlog_item_ids,
    )
    return canonical_content, content


def _validate_backlog_semantics(
    content: BacklogOutput,
    *,
    specification: AcceptedSpecification,
) -> None:
    if (
        not content.is_complete
        or not content.backlog_items
        or content.clarifying_questions
    ):
        message = "Backlog output is incomplete and cannot enter review."
        raise ValueError(message)
    provider_items = tuple(
        BacklogAgentItem.model_validate(
            item.model_dump(mode="json", exclude={"backlog_item_id"})
        )
        for item in content.backlog_items
    )
    canonical_items = canonicalize_backlog_items(
        AcceptedSpecificationReference(
            spec_version_id=specification.spec_version_id,
            spec_hash=specification.spec_hash,
            canonical_specification_json=(specification.canonical_specification_json),
            payload=specification.payload,
        ),
        provider_items,
    )
    if canonical_items != content.backlog_items:
        message = "Backlog items do not match the exact host-minted canonical sequence."
        raise ValueError(message)


def _validate_roadmap_semantics(
    content: RoadmapBuilderOutput,
    *,
    parent_backlog_item_ids: tuple[str, ...],
) -> None:
    if (
        not content.is_complete
        or not content.roadmap_releases
        or content.clarifying_questions
    ):
        message = "Roadmap output is incomplete and cannot enter review."
        raise ValueError(message)
    validate_roadmap_backlog_coverage(content, parent_backlog_item_ids)


def _strict_canonical_planning_content[ContentT: BaseModel](
    canonical_content_json: str,
    *,
    content_type: type[ContentT],
) -> ContentT:
    content = content_type.model_validate_json(canonical_content_json, strict=True)
    if canonical_json(content.model_dump(mode="json")) != canonical_content_json:
        message = "Planning artifact content is not canonical after strict validation."
        raise ValueError(message)
    return content


__all__ = [
    "SprintPlanEnvelope",
    "build_sprint_plan_envelope",
    "load_bound_sprint_plan_envelope",
    "load_stored_backlog_planning_content",
    "load_stored_planning_artifact_content",
    "load_stored_roadmap_planning_content",
    "load_stored_sprint_plan_envelope",
    "validate_backlog_planning_content",
    "validate_canonical_planning_content",
    "validate_roadmap_planning_content",
]
