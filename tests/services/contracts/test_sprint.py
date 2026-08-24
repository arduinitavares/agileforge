"""Tests for direct-Specification Sprint and task reference contracts."""
# ruff: noqa: D103

import inspect

import pytest
from pydantic import ValidationError

import utils.task_metadata as task_metadata_module
from services import planning_artifact_content
from services.contracts.specification_references import AcceptedSpecificationReference
from services.contracts.sprint import (
    SprintPlannerOutput,
    SprintPlannerSelectedStory,
    SprintPlannerStory,
    StructuredTaskSpec,
    validate_task_spec_references,
)
from services.planning_artifact_content import (
    build_sprint_plan_envelope,
    load_bound_sprint_plan_envelope,
)
from utils.agileforge_spec_profile_v2 import (
    RequirementLevel,
    SpecificationItem,
    SpecificationPayload,
    SpecItemType,
    VerificationMethod,
    canonical_spec_hash,
    canonical_spec_json,
)
from utils.task_metadata import (
    TaskMetadata,
    parse_task_metadata,
    serialize_task_metadata,
)
from workflow.fingerprints import canonical_hash


def _reference() -> AcceptedSpecificationReference:
    payload = SpecificationPayload(
        artifact_id="SPEC.sprint-contract",
        title="Sprint contract",
        summary="Task evidence is bounded",
        problem_statement="Tasks must cite Story evidence.",
        items=(
            SpecificationItem(
                id="REQ.alpha",
                type=SpecItemType.REQ,
                title="Alpha",
                statement="The system must support alpha.",
                level=RequirementLevel.MUST,
                verification=VerificationMethod.UNIT_TEST,
                acceptance=("Alpha passes.",),
            ),
        ),
    )
    return AcceptedSpecificationReference(
        spec_version_id=9,
        spec_hash=canonical_spec_hash(payload),
        canonical_specification_json=canonical_spec_json(payload),
        payload=payload,
    )


def test_task_references_are_a_nonempty_subset_of_the_parent_story() -> None:
    task = StructuredTaskSpec(
        description="Implement alpha behavior",
        relevant_spec_item_ids=("REQ.alpha",),
        task_kind="implementation",
        artifact_targets=("calculator",),
        workstream_tags=("backend",),
        checklist_items=("Run the alpha test.",),
    )

    assert validate_task_spec_references(
        _reference(), task, parent_story_spec_item_ids=("REQ.alpha",)
    ) == ("REQ.alpha",)


def test_task_references_reject_an_empty_parent_story_evidence_set() -> None:
    task = StructuredTaskSpec(
        description="Implement alpha behavior",
        relevant_spec_item_ids=("REQ.alpha",),
        task_kind="implementation",
        artifact_targets=("calculator",),
        workstream_tags=("backend",),
        checklist_items=("Run the alpha test.",),
    )

    with pytest.raises(ValueError, match="must not be empty"):
        validate_task_spec_references(_reference(), task, parent_story_spec_item_ids=())


def test_sprint_story_and_task_reject_invalid_canonical_content() -> None:
    story_payload = {
        "story_id": 1,
        "story_item_id": "US-0001",
        "story_title": "Calculate values",
        "statement": "As a student, I want calculations, so that I learn.",
        "persona": "student",
        "acceptance_criteria": ("Verify results.",),
        "spec_item_ids": ("REQ.alpha",),
    }
    assert SprintPlannerStory.model_validate(story_payload)

    with pytest.raises(ValidationError, match="Story item ID"):
        SprintPlannerStory.model_validate({**story_payload, "story_item_id": "US-9999"})
    with pytest.raises(ValidationError, match="persona"):
        SprintPlannerStory.model_validate({**story_payload, "persona": "reader"})
    with pytest.raises(ValidationError, match="acceptance criterion"):
        SprintPlannerStory.model_validate(
            {**story_payload, "acceptance_criteria": (" \t",)}
        )

    task_payload = {
        "description": "Implement alpha behavior",
        "relevant_spec_item_ids": ("REQ.beta", "REQ.alpha"),
        "task_kind": "implementation",
        "artifact_targets": ("calculator",),
        "workstream_tags": ("backend",),
        "checklist_items": ("Run the alpha test.",),
    }
    first = StructuredTaskSpec.model_validate(task_payload)
    second = StructuredTaskSpec.model_validate(
        {**task_payload, "relevant_spec_item_ids": ("REQ.alpha", "REQ.beta")}
    )
    assert first.relevant_spec_item_ids == ("REQ.alpha", "REQ.beta")
    assert first == second
    assert canonical_hash(first.model_dump(mode="json")) == canonical_hash(
        second.model_dump(mode="json")
    )

    with pytest.raises(ValidationError, match="duplicate Specification item ID"):
        StructuredTaskSpec.model_validate(
            {**task_payload, "relevant_spec_item_ids": ("REQ.alpha", "REQ.alpha")}
        )

    output = SprintPlannerOutput(
        sprint_goal="Ship alpha.",
        selected_stories=(
            SprintPlannerSelectedStory(
                story_id=1,
                story_item_id="US-0001",
                reason_for_selection="It is ready.",
                tasks=(first,),
            ),
        ),
    )
    assert output.selected_stories[0].tasks[0].relevant_spec_item_ids == (
        "REQ.alpha",
        "REQ.beta",
    )


def test_sprint_output_requires_tasks_and_checklist_items() -> None:
    with pytest.raises(ValidationError, match="at least 1 item"):
        SprintPlannerSelectedStory(
            story_id=1,
            story_item_id="US-0001",
            reason_for_selection="It is ready.",
            tasks=(),
        )

    with pytest.raises(ValidationError, match="at least 1 item"):
        StructuredTaskSpec(
            description="Implement alpha behavior",
            relevant_spec_item_ids=("REQ.alpha",),
            task_kind="implementation",
            artifact_targets=(),
            workstream_tags=(),
            checklist_items=(),
        )


def test_task_metadata_v2_is_exact_and_canonical() -> None:
    metadata = TaskMetadata(
        spec_version_id=9,
        spec_hash=_reference().spec_hash,
        sprint_plan_stream_id="SPS-0123456789abcdef0123456789abcdef",
        sprint_plan_artifact_id=7,
        sprint_plan_fingerprint="sha256:" + "a" * 64,
        relevant_spec_item_ids=("REQ.alpha",),
        task_kind="implementation",
        artifact_targets=(),
        workstream_tags=("backend",),
        checklist_items=("Run the alpha test.",),
    )
    canonical = serialize_task_metadata(metadata)

    assert parse_task_metadata(canonical) == metadata
    assert '"version":"task_metadata.v2"' in canonical

    for invalid in (
        "",
        "{}",
        '{"version":"task_metadata.v1"}',
        canonical.replace("{", "{ ", 1),
        canonical.replace("task_metadata.v2", "task_metadata.v1"),
    ):
        with pytest.raises(ValueError, match="metadata"):
            parse_task_metadata(invalid)


def test_task_metadata_parser_has_no_ignored_compatibility_parameters() -> None:
    assert tuple(inspect.signature(parse_task_metadata).parameters) == ("raw_value",)
    assert not hasattr(task_metadata_module, "StructuredTaskSpec")


def test_provider_models_are_owned_only_by_the_public_sprint_contract() -> None:
    assert SprintPlannerOutput.__module__ == "services.contracts.sprint"
    assert SprintPlannerSelectedStory.__module__ == "services.contracts.sprint"
    assert StructuredTaskSpec.__module__ == "services.contracts.sprint"
    assert "SprintPlannerOutput" not in planning_artifact_content.__all__
    assert not hasattr(planning_artifact_content, "SprintPlannerSelectedStory")
    assert not hasattr(planning_artifact_content, "StructuredTaskSpec")


def test_sprint_plan_envelope_has_exact_six_field_fingerprint_surface() -> None:
    output = SprintPlannerOutput(
        sprint_goal="Ship alpha.",
        selected_stories=(
            SprintPlannerSelectedStory(
                story_id=1,
                story_item_id="US-0001",
                reason_for_selection="It is ready.",
                tasks=(
                    StructuredTaskSpec(
                        description="Implement alpha behavior",
                        relevant_spec_item_ids=("REQ.alpha",),
                        task_kind="implementation",
                        artifact_targets=(),
                        workstream_tags=("backend",),
                        checklist_items=("Run the alpha test.",),
                    ),
                ),
            ),
        ),
    )
    envelope, canonical, fingerprint = build_sprint_plan_envelope(
        team_name="Platform",
        spec_version_id=9,
        spec_hash=_reference().spec_hash,
        candidate_set_fingerprint="sha256:" + "b" * 64,
        planner_output=output,
    )

    assert tuple(envelope.model_dump(mode="json")) == (
        "schema_version",
        "team_name",
        "spec_version_id",
        "spec_hash",
        "candidate_set_fingerprint",
        "planner_output",
    )
    assert canonical.startswith('{"candidate_set_fingerprint"')
    assert fingerprint == canonical_hash(envelope.model_dump(mode="json"))
    assert (
        load_bound_sprint_plan_envelope(
            canonical,
            expected_fingerprint=fingerprint,
            spec_version_id=9,
            spec_hash=_reference().spec_hash,
            candidate_set_fingerprint="sha256:" + "b" * 64,
            selected_story_ids_json="[1]",
        )
        == envelope
    )
    with pytest.raises(ValueError, match="columns"):
        load_bound_sprint_plan_envelope(
            canonical,
            expected_fingerprint=fingerprint,
            spec_version_id=9,
            spec_hash=_reference().spec_hash,
            candidate_set_fingerprint="sha256:" + "b" * 64,
            selected_story_ids_json="[2]",
        )
