"""Canonical task/story packet regressions over current durable records."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING

from fastapi.testclient import TestClient
from pydantic import TypeAdapter
from sqlmodel import Session, col, select

import api as api_module
from models.core import Project, Sprint, SprintStory, Task, Team, UserStory
from models.enums import SprintStatus
from models.product_definition import VisionArtifact
from models.specs import (
    CompiledSpecAuthority,
    SpecAuthorityAcceptance,
    SpecRegistry,
)
from services.read_projections import DurableReadProjectionService
from services.specs.authority_selection import pending_authority_fingerprint
from services.specs.story_validation_service import compute_story_input_hash
from tests.vision_lineage_fixtures import seed_accepted_vision
from tests.workflow.lifecycle_fixtures import seed_accepted_specification
from utils.spec_schemas import (
    AlignmentFinding,
    Invariant,
    InvariantType,
    RequiredFieldParams,
    SourceMapEntry,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerOutput,
    ValidationEvidence,
    ValidationFailure,
)
from utils.task_metadata import TaskMetadata, serialize_task_metadata
from workflow.contracts import JsonObject, JsonValue

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.engine import Engine

_JSON_OBJECT = TypeAdapter(JsonObject)
_JSON_LIST = TypeAdapter(list[JsonValue])
_SHA256_HEX_LENGTH = 64
_INVARIANT_ID = "INV-0123456789abcdef"
_ACCEPTED_VISION_STATEMENT = "Deliver one verified product increment."


@dataclass(frozen=True)
class _PacketSeed:
    project_id: int
    sprint_id: int
    story_id: int
    task_id: int
    spec_version_id: int | None
    authority_id: int | None


class _PacketApplication:
    """Expose only the retained read projection to API packet routes."""

    def __init__(self, reads: DurableReadProjectionService) -> None:
        self.reads = reads


def _required_id(value: int | None, label: str) -> int:
    assert value is not None, label
    return value


def _object(value: object) -> JsonObject:
    return _JSON_OBJECT.validate_python(value)


def _list(value: object) -> list[JsonValue]:
    return _JSON_LIST.validate_python(value)


def _data(result: JsonObject) -> JsonObject:
    assert result.get("ok") is True, result
    return _object(result.get("data"))


def _error_code(result: JsonObject) -> str:
    assert result.get("ok") is False, result
    errors = _list(result.get("errors"))
    first = _object(errors[0])
    code = first.get("code")
    assert isinstance(code, str)
    return code


def _seed_packet_context(
    session: Session,
    *,
    pinned: bool = True,
    task_metadata: TaskMetadata | None = None,
) -> _PacketSeed:
    project = Project(name="Task Packet Project")
    team = Team(name="Packet Team")
    session.add(project)
    session.add(team)
    session.flush()
    project_id = _required_id(project.project_id, "project_id")
    team_id = _required_id(team.team_id, "team_id")

    story = UserStory(
        project_id=project_id,
        title="Payload Validation Story",
        story_description=(
            "As a developer, I want payload validation so that requests are safe."
        ),
        acceptance_criteria="- include user_id\n- reject invalid payloads",
        persona="Developer",
        story_points=3,
        rank="1",
        source_requirement="api_payload_validation",
    )
    session.add(story)
    session.flush()
    story_id = _required_id(story.story_id, "story_id")

    task = Task(
        description="Implement payload validation for incoming requests",
        story_id=story_id,
        metadata_json=serialize_task_metadata(
            task_metadata
            or TaskMetadata(
                task_kind="implementation",
                artifact_targets=["payload validator", "request contract tests"],
                workstream_tags=["backend", "api"],
                relevant_invariant_ids=[_INVARIANT_ID],
                checklist_items=[
                    "Validate user_id inputs",
                    "Cover invalid payload cases",
                ],
            )
        ),
    )
    sprint = Sprint(
        goal="Ship a trustworthy task packet API",
        start_date=date(2026, 4, 1),
        end_date=date(2026, 4, 14),
        status=SprintStatus.PLANNED,
        project_id=project_id,
        team_id=team_id,
    )
    session.add(task)
    session.add(sprint)
    session.flush()
    task_id = _required_id(task.task_id, "task_id")
    sprint_id = _required_id(sprint.sprint_id, "sprint_id")
    session.add(SprintStory(sprint_id=sprint_id, story_id=story_id))

    spec_version_id: int | None = None
    authority_id: int | None = None
    if pinned:
        lineage = seed_accepted_specification(
            session,
            project_id=project_id,
            content=json.dumps(
                {"requirements": ["Requests must include user_id."]},
                separators=(",", ":"),
            ),
        )
        spec = lineage.spec
        spec_version_id = _required_id(spec.spec_version_id, "spec_version_id")
        invariant = Invariant(
            id=_INVARIANT_ID,
            type=InvariantType.REQUIRED_FIELD,
            parameters=RequiredFieldParams(field_name="user_id"),
        )
        artifact = SpecAuthorityCompilationSuccess(
            scope_themes=["API"],
            domain="api",
            invariants=[invariant],
            eligible_feature_rules=[],
            gaps=[],
            assumptions=[],
            source_map=[
                SourceMapEntry(
                    invariant_id=_INVARIANT_ID,
                    excerpt="Requests must include user_id.",
                    location="Spec section 1",
                )
            ],
            compiler_version="3.0.0",
            prompt_hash="0" * 64,
        )
        authority = CompiledSpecAuthority(
            spec_version_id=spec_version_id,
            compiler_version=artifact.compiler_version,
            prompt_hash=artifact.prompt_hash,
            scope_themes='["API"]',
            invariants='["REQUIRED_FIELD:user_id"]',
            eligible_feature_ids="[]",
            rejected_features="[]",
            spec_gaps="[]",
            compiled_artifact_json=SpecAuthorityCompilerOutput(
                root=artifact
            ).model_dump_json(),
        )
        session.add(authority)
        session.flush()
        authority_id = _required_id(authority.authority_id, "authority_id")
        session.add(
            SpecAuthorityAcceptance(
                project_id=project_id,
                spec_version_id=spec_version_id,
                status="accepted",
                policy="test",
                decided_by="packet-test",
                compiler_version=authority.compiler_version,
                prompt_hash=authority.prompt_hash,
                spec_hash=spec.spec_hash,
                pending_authority_id=authority_id,
                authority_fingerprint=pending_authority_fingerprint(authority),
            )
        )
        story.accepted_spec_version_id = spec_version_id
        story.validation_evidence = ValidationEvidence(
            spec_version_id=spec_version_id,
            validated_at=datetime.now(UTC),
            passed=True,
            rules_checked=["SPEC_VERSION_EXISTS", "SPEC_PROJECT_MATCH"],
            invariants_checked=["REQUIRED_FIELD:user_id"],
            evaluated_invariant_ids=[_INVARIANT_ID],
            finding_invariant_ids=[_INVARIANT_ID],
            failures=[],
            warnings=["Double-check payload casing."],
            alignment_warnings=[
                AlignmentFinding(
                    code="REQUIRED_FIELD_MISSING",
                    invariant=_INVARIANT_ID,
                    capability=None,
                    message="Payload coverage needs explicit review.",
                    severity="warning",
                    created_at=datetime.now(UTC),
                )
            ],
            alignment_failures=[],
            validator_version="1.0.0",
            input_hash=compute_story_input_hash(story),
        ).model_dump_json()
        session.add(story)
    else:
        seed_accepted_vision(
            session,
            project_id=project_id,
            statement=_ACCEPTED_VISION_STATEMENT,
        )

    session.commit()
    return _PacketSeed(
        project_id=project_id,
        sprint_id=sprint_id,
        story_id=story_id,
        task_id=task_id,
        spec_version_id=spec_version_id,
        authority_id=authority_id,
    )


def test_task_and_story_packets_restore_canonical_project_shape(
    engine: Engine,
    session: Session,
) -> None:
    """Restore canonical packets with pinned authority and execution constraints."""
    seed = _seed_packet_context(session)
    reads = DurableReadProjectionService(engine=engine)

    task_packet = _data(
        reads.task_packet(
            project_id=seed.project_id,
            sprint_id=seed.sprint_id,
            task_id=seed.task_id,
        )
    )
    story_packet = _data(
        reads.story_packet(
            project_id=seed.project_id,
            sprint_id=seed.sprint_id,
            story_id=seed.story_id,
        )
    )

    assert set(task_packet) == {
        "schema_version",
        "metadata",
        "source_snapshot",
        "task",
        "context",
        "constraints",
    }
    assert task_packet["schema_version"] == "task_packet.v2"
    task_metadata = _object(task_packet["metadata"])
    assert str(task_metadata["packet_id"]).startswith("tp_")
    assert task_metadata["generator_version"] == "v2"
    assert len(str(task_metadata["source_fingerprint"])) == _SHA256_HEX_LENGTH

    task_snapshot = _object(task_packet["source_snapshot"])
    assert task_snapshot["project_id"] == seed.project_id
    assert task_snapshot["accepted_spec_version_id"] == seed.spec_version_id
    assert task_snapshot["compiled_authority_id"] == seed.authority_id
    assert len(str(task_snapshot["task_metadata_hash"])) == _SHA256_HEX_LENGTH

    task = _object(task_packet["task"])
    assert task == {
        "task_id": seed.task_id,
        "label": "Implement payload validation for incoming requests",
        "description": "Implement payload validation for incoming requests",
        "status": "To Do",
        "assignee_member_id": None,
        "assignee_name": None,
        "task_kind": "implementation",
        "artifact_targets": ["payload validator", "request contract tests"],
        "workstream_tags": ["backend", "api"],
        "checklist_items": [
            "Validate user_id inputs",
            "Cover invalid payload cases",
        ],
        "is_executable": True,
    }

    task_context = _object(task_packet["context"])
    project_context = _object(task_context["project"])
    assert project_context == {
        "project_id": seed.project_id,
        "name": "Task Packet Project",
        "vision_excerpt": _ACCEPTED_VISION_STATEMENT,
    }
    assert _object(task_context["story"])["story_id"] == seed.story_id
    assert _object(task_context["sprint"])["sprint_id"] == seed.sprint_id

    task_constraints = _object(task_packet["constraints"])
    assert _object(task_constraints["spec_binding"]) == {
        "mode": "pinned_story_authority",
        "binding_status": "pinned",
        "spec_version_id": seed.spec_version_id,
        "authority_artifact_status": "available",
    }
    assert _object(task_constraints["validation"])["freshness_status"] == "current"
    expected_constraint = {
        "invariant_id": _INVARIANT_ID,
        "type": "REQUIRED_FIELD",
        "parameters": {"field_name": "user_id"},
        "source_excerpt": "Requests must include user_id.",
        "source_location": "Spec section 1",
    }
    assert _list(task_constraints["task_hard_constraints"]) == [expected_constraint]
    assert _list(task_constraints["story_compliance_boundaries"]) == [
        expected_constraint
    ]
    assert {
        _object(item)["source"] for item in _list(task_constraints["findings"])
    } == {"validation_warning", "alignment_warning"}

    assert story_packet["schema_version"] == "story_packet.v1"
    assert set(story_packet) == {
        "schema_version",
        "metadata",
        "source_snapshot",
        "story",
        "task_plan",
        "context",
        "constraints",
    }
    story_metadata = _object(story_packet["metadata"])
    assert str(story_metadata["packet_id"]).startswith("sp_")
    assert story_metadata["generator_version"] == "v1"
    story_snapshot = _object(story_packet["source_snapshot"])
    assert story_snapshot["project_id"] == seed.project_id
    assert len(str(story_snapshot["task_plan_hash"])) == _SHA256_HEX_LENGTH
    story = _object(story_packet["story"])
    assert story["story_id"] == seed.story_id
    task_plan = _object(story_packet["task_plan"])
    assert _object(_list(task_plan["tasks"])[0])["id"] == seed.task_id
    story_context = _object(story_packet["context"])
    assert _object(story_context["project"])["project_id"] == seed.project_id
    story_constraints = _object(story_packet["constraints"])
    assert story_constraints["story_acceptance_criteria_items"] == [
        "include user_id",
        "reject invalid payloads",
    ]
    assert "task_hard_constraints" not in story_constraints


def test_packets_fail_closed_on_malformed_durable_vision_lineage(
    engine: Engine,
    session: Session,
) -> None:
    """Reject a packet when durable Vision content no longer matches its hash."""
    seed = _seed_packet_context(session, pinned=False)
    vision = session.exec(
        select(VisionArtifact).where(col(VisionArtifact.project_id) == seed.project_id)
    ).one()
    vision.components_json = '{"purpose":"tampered"}'
    session.add(vision)
    session.commit()

    result = DurableReadProjectionService(engine=engine).task_packet(
        project_id=seed.project_id,
        sprint_id=seed.sprint_id,
        task_id=seed.task_id,
    )

    assert _error_code(result) == "VISION_LINEAGE_INVALID"


def test_packet_fingerprints_track_task_metadata_and_story_task_plan(
    engine: Engine,
    session: Session,
) -> None:
    """Include task metadata and the complete task plan in packet freshness."""
    seed = _seed_packet_context(session)
    reads = DurableReadProjectionService(engine=engine)
    first_task = _data(
        reads.task_packet(
            project_id=seed.project_id,
            sprint_id=seed.sprint_id,
            task_id=seed.task_id,
        )
    )
    first_story = _data(
        reads.story_packet(
            project_id=seed.project_id,
            sprint_id=seed.sprint_id,
            story_id=seed.story_id,
        )
    )

    task = session.get(Task, seed.task_id)
    assert task is not None
    task.metadata_json = serialize_task_metadata(
        TaskMetadata(
            task_kind="testing",
            artifact_targets=["contract suite"],
            workstream_tags=["qa"],
            checklist_items=["Run invalid payload cases"],
        )
    )
    session.add(task)
    session.commit()

    second_task = _data(
        reads.task_packet(
            project_id=seed.project_id,
            sprint_id=seed.sprint_id,
            task_id=seed.task_id,
        )
    )
    second_story = _data(
        reads.story_packet(
            project_id=seed.project_id,
            sprint_id=seed.sprint_id,
            story_id=seed.story_id,
        )
    )

    assert (
        _object(first_task["source_snapshot"])["task_metadata_hash"]
        != _object(second_task["source_snapshot"])["task_metadata_hash"]
    )
    assert (
        _object(first_task["metadata"])["source_fingerprint"]
        != _object(second_task["metadata"])["source_fingerprint"]
    )
    assert (
        _object(first_story["source_snapshot"])["task_plan_hash"]
        != _object(second_story["source_snapshot"])["task_plan_hash"]
    )
    assert (
        _object(first_story["metadata"])["source_fingerprint"]
        != _object(second_story["metadata"])["source_fingerprint"]
    )


def test_packet_fingerprints_cover_complete_canonical_validation_evidence(
    engine: Engine,
    session: Session,
) -> None:
    """Fingerprint every persisted validation input that shapes either packet."""
    seed = _seed_packet_context(session)
    reads = DurableReadProjectionService(engine=engine)
    story = session.get(UserStory, seed.story_id)
    assert story is not None
    assert story.validation_evidence is not None
    original_updated_at = story.updated_at
    original = ValidationEvidence.model_validate_json(story.validation_evidence)

    def packets() -> tuple[JsonObject, JsonObject]:
        return (
            _data(
                reads.task_packet(
                    project_id=seed.project_id,
                    sprint_id=seed.sprint_id,
                    task_id=seed.task_id,
                )
            ),
            _data(
                reads.story_packet(
                    project_id=seed.project_id,
                    sprint_id=seed.sprint_id,
                    story_id=seed.story_id,
                )
            ),
        )

    def store(raw_evidence: str) -> None:
        stored_story = session.get(UserStory, seed.story_id)
        assert stored_story is not None
        stored_story.validation_evidence = raw_evidence
        stored_story.updated_at = original_updated_at
        session.add(stored_story)
        session.commit()
        session.expire_all()

    baseline_packets = packets()
    baseline_hashes = tuple(
        str(_object(packet["source_snapshot"])["validation_evidence_hash"])
        for packet in baseline_packets
    )
    baseline_fingerprints = tuple(
        str(_object(packet["metadata"])["source_fingerprint"])
        for packet in baseline_packets
    )
    assert baseline_hashes[0] == baseline_hashes[1]
    assert len(baseline_hashes[0]) == _SHA256_HEX_LENGTH

    mutations = (
        original.model_copy(
            update={"warnings": [*original.warnings, "A new warning."]}
        ),
        original.model_copy(
            update={
                "failures": [
                    *original.failures,
                    ValidationFailure(
                        rule="PAYLOAD_CASE",
                        expected="lowercase",
                        actual="mixed case",
                        message="Payload casing is invalid.",
                    ),
                ]
            }
        ),
        original.model_copy(
            update={"rules_checked": [*original.rules_checked, "PAYLOAD_CASE"]}
        ),
        original.model_copy(update={"finding_invariant_ids": []}),
    )
    for changed_evidence in mutations:
        assert changed_evidence.validated_at == original.validated_at
        assert changed_evidence.input_hash == original.input_hash
        store(changed_evidence.model_dump_json())
        changed_packets = packets()
        for index, changed_packet in enumerate(changed_packets):
            changed_snapshot = _object(changed_packet["source_snapshot"])
            changed_metadata = _object(changed_packet["metadata"])
            assert (
                changed_snapshot["validation_evidence_hash"]
                != baseline_hashes[index]
            )
            assert (
                changed_metadata["source_fingerprint"]
                != baseline_fingerprints[index]
            )

    persisted = _object(json.loads(original.model_dump_json()))
    reordered = {key: persisted[key] for key in reversed(tuple(persisted))}
    store(json.dumps(reordered, ensure_ascii=True, separators=(",", ":")))
    canonical_packets = packets()
    for index, canonical_packet in enumerate(canonical_packets):
        assert (
            _object(canonical_packet["source_snapshot"])[
                "validation_evidence_hash"
            ]
            == baseline_hashes[index]
        )
        assert (
            _object(canonical_packet["metadata"])["source_fingerprint"]
            == baseline_fingerprints[index]
        )


def test_packet_validation_freshness_and_unpinned_authority_are_exact(
    engine: Engine,
    session: Session,
) -> None:
    """Mark changed Story input stale and never fall back from an unpinned Story."""
    seed = _seed_packet_context(session)
    reads = DurableReadProjectionService(engine=engine)
    story = session.get(UserStory, seed.story_id)
    assert story is not None
    story.acceptance_criteria = f"{story.acceptance_criteria or ''}\n- log failures"
    story.ac_updated_at = datetime.now(UTC)
    session.add(story)
    session.commit()

    stale_packet = _data(
        reads.task_packet(
            project_id=seed.project_id,
            sprint_id=seed.sprint_id,
            task_id=seed.task_id,
        )
    )
    stale_validation = _object(_object(stale_packet["constraints"])["validation"])
    assert stale_validation["freshness_status"] == "stale"
    assert stale_validation["input_hash_matches"] is False
    assert _object(stale_packet["source_snapshot"])["story_ac_updated_at"] is not None

    story.accepted_spec_version_id = None
    story.validation_evidence = None
    session.add(story)
    session.commit()
    unpinned_packet = _data(
        reads.task_packet(
            project_id=seed.project_id,
            sprint_id=seed.sprint_id,
            task_id=seed.task_id,
        )
    )
    constraints = _object(unpinned_packet["constraints"])
    assert _object(constraints["spec_binding"]) == {
        "mode": "pinned_story_authority",
        "binding_status": "unpinned",
        "spec_version_id": None,
        "authority_artifact_status": "missing",
    }
    assert _object(constraints["validation"])["freshness_status"] == "missing"
    assert constraints["task_hard_constraints"] == []
    assert constraints["story_compliance_boundaries"] == []


def test_packets_reject_unlinked_and_cross_project_records(
    engine: Engine,
    session: Session,
) -> None:
    """Fail closed for sprint linkage and task/Story Project ownership."""
    seed = _seed_packet_context(session, pinned=False)
    foreign_project = Project(name="Foreign Project")
    foreign_team = Team(name="Foreign Team")
    session.add(foreign_project)
    session.add(foreign_team)
    session.flush()
    foreign_sprint = Sprint(
        goal="Foreign work",
        project_id=_required_id(foreign_project.project_id, "foreign_project_id"),
        team_id=_required_id(foreign_team.team_id, "foreign_team_id"),
    )
    session.add(foreign_sprint)
    session.commit()
    foreign_sprint_id = _required_id(foreign_sprint.sprint_id, "foreign_sprint_id")
    reads = DurableReadProjectionService(engine=engine)

    unlinked = reads.task_packet(
        project_id=seed.project_id,
        sprint_id=foreign_sprint_id,
        task_id=seed.task_id,
    )
    cross_project = reads.story_packet(
        project_id=_required_id(foreign_project.project_id, "foreign_project_id"),
        sprint_id=seed.sprint_id,
        story_id=seed.story_id,
    )

    assert _error_code(unlinked) == "TASK_PACKET_CONTEXT_NOT_FOUND"
    assert _error_code(cross_project) == "STORY_NOT_FOUND"


def test_pinned_packet_rejects_foreign_spec_and_missing_acceptance(
    engine: Engine,
    session: Session,
) -> None:
    """Require current Project ownership and one exact accepted authority row."""
    seed = _seed_packet_context(session)
    assert seed.spec_version_id is not None
    reads = DurableReadProjectionService(engine=engine)
    foreign_project = Project(name="Foreign Spec Owner")
    session.add(foreign_project)
    session.flush()
    spec = session.get(SpecRegistry, seed.spec_version_id)
    assert spec is not None
    spec.project_id = _required_id(foreign_project.project_id, "foreign_project_id")
    session.add(spec)
    session.commit()

    foreign_owned = reads.task_packet(
        project_id=seed.project_id,
        sprint_id=seed.sprint_id,
        task_id=seed.task_id,
    )
    assert _error_code(foreign_owned) == "SPEC_VERSION_NOT_FOUND"

    spec.project_id = seed.project_id
    session.add(spec)
    acceptance = session.exec(
        select(SpecAuthorityAcceptance).where(
            col(SpecAuthorityAcceptance.project_id) == seed.project_id,
            col(SpecAuthorityAcceptance.spec_version_id) == seed.spec_version_id,
        )
    ).one()
    session.delete(acceptance)
    session.commit()

    missing_acceptance = reads.story_packet(
        project_id=seed.project_id,
        sprint_id=seed.sprint_id,
        story_id=seed.story_id,
    )
    assert _error_code(missing_acceptance) == "AUTHORITY_NOT_ACCEPTED"


def test_pinned_packet_rejects_acceptance_authority_mismatch(
    engine: Engine,
    session: Session,
) -> None:
    """Reject an acceptance that points at authority for a different spec."""
    seed = _seed_packet_context(session)
    assert seed.spec_version_id is not None
    second_lineage = seed_accepted_specification(
        session,
        project_id=seed.project_id,
        content=json.dumps(
            {"requirements": ["A different specification."]},
            separators=(",", ":"),
        ),
    )
    second_spec = second_lineage.spec
    second_authority = CompiledSpecAuthority(
        spec_version_id=_required_id(second_spec.spec_version_id, "second_spec_id"),
        compiler_version="3.0.0",
        prompt_hash="1" * 64,
        scope_themes="[]",
        invariants="[]",
        eligible_feature_ids="[]",
        rejected_features="[]",
        spec_gaps="[]",
    )
    session.add(second_authority)
    session.flush()
    acceptance = session.exec(
        select(SpecAuthorityAcceptance).where(
            col(SpecAuthorityAcceptance.project_id) == seed.project_id,
            col(SpecAuthorityAcceptance.spec_version_id) == seed.spec_version_id,
        )
    ).one()
    acceptance.pending_authority_id = _required_id(
        second_authority.authority_id,
        "second_authority_id",
    )
    session.add(acceptance)
    session.commit()

    result = DurableReadProjectionService(engine=engine).task_packet(
        project_id=seed.project_id,
        sprint_id=seed.sprint_id,
        task_id=seed.task_id,
    )

    assert _error_code(result) == "AUTHORITY_ACCEPTANCE_MISMATCH"


def test_packet_api_returns_canonical_render_and_not_found_contract(
    engine: Engine,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise canonical packets and renderer through the production API routes."""
    seed = _seed_packet_context(session)
    application = _PacketApplication(DurableReadProjectionService(engine=engine))
    monkeypatch.setattr(api_module, "_application", lambda: application)
    client = TestClient(api_module.app)

    task_response = client.get(
        f"/api/projects/{seed.project_id}/sprints/{seed.sprint_id}"
        f"/tasks/{seed.task_id}/packet?flavor=cursor"
    )
    story_response = client.get(
        f"/api/projects/{seed.project_id}/sprints/{seed.sprint_id}"
        f"/stories/{seed.story_id}/packet?flavor=human"
    )
    missing_response = client.get(
        f"/api/projects/{seed.project_id}/sprints/{seed.sprint_id}/tasks/999999/packet"
    )

    assert task_response.status_code == HTTPStatus.OK
    task_payload = _object(_object(task_response.json())["data"])
    assert task_payload["schema_version"] == "task_packet.v2"
    task_render = task_payload["render"]
    assert isinstance(task_render, str)
    assert "<task_kind>implementation</task_kind>" in task_render
    assert "Task Checklist" in task_render
    assert "Story Acceptance Criteria" not in task_render

    assert story_response.status_code == HTTPStatus.OK
    story_payload = _object(_object(story_response.json())["data"])
    assert story_payload["schema_version"] == "story_packet.v1"
    story_render = story_payload["render"]
    assert isinstance(story_render, str)
    assert "# Story: Payload Validation Story" in story_render
    assert "## Story Acceptance Criteria" in story_render

    assert missing_response.status_code == HTTPStatus.NOT_FOUND
    missing_detail = _object(_object(missing_response.json())["detail"])
    assert _error_code(missing_detail) == "TASK_PACKET_CONTEXT_NOT_FOUND"
