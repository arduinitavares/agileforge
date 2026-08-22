"""Tests for strict Task and Task-metadata contracts."""

import pytest
from pydantic import ValidationError

from services.contracts.sprint import StructuredTaskSpec
from utils.task_metadata import (
    TaskMetadata,
    parse_task_metadata,
    serialize_task_metadata,
)


def test_structured_task_spec_accepts_current_task_kind() -> None:
    """Accept the current strict task-kind vocabulary."""
    spec = StructuredTaskSpec.model_validate(
        {
            "description": "Add coverage",
            "relevant_spec_item_ids": ["REQ.coverage"],
            "task_kind": "test",
            "artifact_targets": [],
            "workstream_tags": [],
            "checklist_items": ["Coverage is added."],
        }
    )

    assert spec.task_kind == "test"


def _metadata() -> TaskMetadata:
    return TaskMetadata(
        spec_version_id=7,
        spec_hash="sha256:" + "a" * 64,
        sprint_plan_stream_id="SPS-" + "b" * 32,
        sprint_plan_artifact_id=11,
        sprint_plan_fingerprint="sha256:" + "c" * 64,
        relevant_spec_item_ids=("REQ.coverage",),
        task_kind="test",
        artifact_targets=(),
        workstream_tags=(),
        checklist_items=("Coverage is added.",),
    )


def test_parse_task_metadata_requires_exact_canonical_v2() -> None:
    """Round-trip only exact canonical task-metadata v2."""
    metadata = _metadata()

    assert parse_task_metadata(serialize_task_metadata(metadata)) == metadata


@pytest.mark.parametrize("task_kind", ["testing", "Review", "qa", "validation"])
def test_task_kind_rejects_retired_values(task_kind: str) -> None:
    """Reject task-kind values outside the current strict vocabulary."""
    with pytest.raises(ValidationError):
        StructuredTaskSpec.model_validate(
            {
                "description": "Add coverage",
                "relevant_spec_item_ids": ["REQ.coverage"],
                "task_kind": task_kind,
                "artifact_targets": [],
                "workstream_tags": [],
                "checklist_items": ["Coverage is added."],
            }
        )


def test_task_metadata_rejects_incomplete_or_noncanonical_payload() -> None:
    """Reject incomplete or noncanonical task-metadata payloads."""
    with pytest.raises(ValidationError):
        TaskMetadata.model_validate({"task_kind": "test"})
    with pytest.raises(ValueError, match="not canonical"):
        parse_task_metadata(serialize_task_metadata(_metadata()) + " ")
