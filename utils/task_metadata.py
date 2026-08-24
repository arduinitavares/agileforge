"""Strict canonical metadata for Tasks activated from accepted Sprint plans."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from services.contracts.specification_references import (
    validate_canonical_spec_item_ids,
)
from workflow.fingerprints import canonical_json

if TYPE_CHECKING:
    from services.contracts.sprint import StructuredTaskSpec

TASK_METADATA_VERSION = "task_metadata.v2"
TaskKind = Literal["implementation", "test", "documentation", "research"]
_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"
_STREAM_PATTERN = r"^SPS-[0-9a-f]{32}$"


def _require_ordered_unique_nonblank(
    values: tuple[str, ...],
    *,
    field_name: str,
    require_nonempty: bool = False,
) -> tuple[str, ...]:
    if require_nonempty and not values:
        message = f"{field_name} must not be empty."
        raise ValueError(message)
    if any(not value or not value.strip() for value in values):
        message = f"{field_name} values must not be empty or whitespace-only."
        raise ValueError(message)
    if len(set(values)) != len(values):
        message = f"{field_name} values must be unique."
        raise ValueError(message)
    return values


class TaskMetadata(BaseModel):
    """Exact immutable plan and Specification identity persisted with a Task."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal["task_metadata.v2"] = TASK_METADATA_VERSION
    spec_version_id: Annotated[int, Field(gt=0)]
    spec_hash: Annotated[str, Field(pattern=_SHA256_PATTERN)]
    sprint_plan_stream_id: Annotated[str, Field(pattern=_STREAM_PATTERN)]
    sprint_plan_artifact_id: Annotated[int, Field(gt=0)]
    sprint_plan_fingerprint: Annotated[str, Field(pattern=_SHA256_PATTERN)]
    relevant_spec_item_ids: tuple[str, ...]
    task_kind: TaskKind
    artifact_targets: tuple[str, ...]
    workstream_tags: tuple[str, ...]
    checklist_items: tuple[str, ...]

    @field_validator("relevant_spec_item_ids")
    @classmethod
    def validate_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Require nonempty, sorted, unique Specification evidence."""
        return validate_canonical_spec_item_ids(value)

    @field_validator("artifact_targets", "workstream_tags")
    @classmethod
    def validate_optional_lists(
        cls,
        value: tuple[str, ...],
        info: ValidationInfo,
    ) -> tuple[str, ...]:
        """Require optional lists to remain ordered, unique, and nonblank."""
        return _require_ordered_unique_nonblank(
            value,
            field_name=info.field_name or "metadata list",
        )

    @field_validator("checklist_items")
    @classmethod
    def validate_checklist(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Require at least one exact, unique checklist item."""
        return _require_ordered_unique_nonblank(
            value,
            field_name="checklist_items",
            require_nonempty=True,
        )


def serialize_task_metadata(metadata: TaskMetadata) -> str:
    """Return canonical JSON bytes for one strict v2 metadata payload."""
    return canonical_json(metadata.model_dump(mode="json"))


def parse_task_metadata(raw_value: str | None) -> TaskMetadata:
    """Parse exact canonical v2 bytes or fail closed without a fallback."""
    if not raw_value:
        message = "Task metadata is required."
        raise ValueError(message)
    try:
        metadata = TaskMetadata.model_validate_json(raw_value)
    except (TypeError, ValueError) as exc:
        message = "Task metadata is invalid."
        raise ValueError(message) from exc
    if serialize_task_metadata(metadata) != raw_value:
        message = "Task metadata is not canonical."
        raise ValueError(message)
    return metadata


def metadata_from_structured_task(  # noqa: PLR0913
    task: StructuredTaskSpec,
    *,
    spec_version_id: int,
    spec_hash: str,
    sprint_plan_stream_id: str,
    sprint_plan_artifact_id: int,
    sprint_plan_fingerprint: str,
) -> TaskMetadata:
    """Project one accepted immutable plan Task into strict v2 metadata."""
    return TaskMetadata(
        spec_version_id=spec_version_id,
        spec_hash=spec_hash,
        sprint_plan_stream_id=sprint_plan_stream_id,
        sprint_plan_artifact_id=sprint_plan_artifact_id,
        sprint_plan_fingerprint=sprint_plan_fingerprint,
        relevant_spec_item_ids=task.relevant_spec_item_ids,
        task_kind=task.task_kind,
        artifact_targets=task.artifact_targets,
        workstream_tags=task.workstream_tags,
        checklist_items=task.checklist_items,
    )


def hash_task_metadata(metadata: TaskMetadata) -> str:
    """Return the stable packet hash shape over strict v2 bytes."""
    return hashlib.sha256(serialize_task_metadata(metadata).encode("utf-8")).hexdigest()


__all__ = [
    "TASK_METADATA_VERSION",
    "TaskMetadata",
    "hash_task_metadata",
    "metadata_from_structured_task",
    "parse_task_metadata",
    "serialize_task_metadata",
]
