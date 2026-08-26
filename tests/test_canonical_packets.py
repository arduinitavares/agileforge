"""Canonical direct-Spec Story and Task packet contracts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from sqlmodel import Session, select

from models.core import Project, Sprint, Task, Team, UserStory
from models.enums import SprintStatus
from models.specs import SpecRegistry
from models.workflow import SprintStart
from repositories.workflow import WorkflowFactRepository
from services.contracts.sprint import SprintPlannerOutput
from services.packets.canonical import (
    CanonicalPacketError,
    build_story_packet,
    build_task_packet,
    validate_canonical_packet,
)
from services.specs.story_validation_service import (
    story_validation_input_fingerprint,
    story_validation_input_payload,
)
from services.sprint_ownership import SprintOwnerEvidenceError
from tests.workflow.execution_fixtures import seed_started_execution
from tests.workflow.test_planning_transitions import (
    _domain as _planning_domain,
)
from tests.workflow.test_planning_transitions import (
    _guards as _planning_guards,
)
from tests.workflow.test_planning_transitions import (
    _record_and_accept_roadmap,
    _record_and_accept_story,
    _record_sprint_plan_draft,
    _seed_accepted_backlog,
)
from workflow.definitions.product_discovery import accepted_current_spec
from workflow.fingerprints import canonical_hash, canonical_json
from workflow.requests import DecideSprintPlan, RecordSprintPlan

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from workflow.contracts import JsonObject, JsonValue


def _seed(engine: Engine) -> tuple[int, int, int, int]:
    return seed_started_execution(engine)


def _object(value: JsonValue) -> JsonObject:
    assert isinstance(value, dict)
    return value


def _items(value: JsonValue) -> list[JsonValue]:
    assert isinstance(value, list)
    return value


def _rehashed_packet(packet: JsonObject) -> JsonObject:
    """Return a packet mutation with its caller-recomputable hash refreshed."""
    mutated = cast("JsonObject", json.loads(json.dumps(packet)))
    metadata = _object(mutated["metadata"])
    metadata["source_fingerprint"] = canonical_hash(
        {key: mutated[key] for key in ("lineage", "context", "evidence", "work")}
    )
    return mutated


def _refresh_validation_story_fingerprints(packet: JsonObject) -> None:
    """Refresh validation fields that intentionally duplicate Story evidence."""
    context = _object(packet["context"])
    lineage = _object(packet["lineage"])
    evidence = _object(packet["evidence"])
    work = _object(packet["work"])
    specification = _object(lineage["specification"])
    backlog = _object(lineage["backlog"])
    story = _object(lineage["story"])
    story_item = _object(evidence["story_item"])
    validation = _object(evidence["story_validation"])
    work_story = _object(work["story"])
    validation["source_story_item_fingerprint"] = canonical_hash(story_item)
    validation["story_validation_input_fingerprint"] = (
        story_validation_input_fingerprint(
            project_id=cast("int", _object(context["project"])["project_id"]),
            story_id=cast("int", story["story_id"]),
            source_story_artifact_id=cast("int", story["story_artifact_id"]),
            source_story_artifact_fingerprint=cast(
                "str", story["artifact_fingerprint"]
            ),
            source_story_item_id=cast("str", story["story_item_id"]),
            source_story_item_fingerprint=canonical_hash(story_item),
            source_backlog_artifact_id=cast("int", backlog["backlog_artifact_id"]),
            source_backlog_artifact_fingerprint=cast(
                "str", backlog["artifact_fingerprint"]
            ),
            source_backlog_item_id=cast("str", backlog["backlog_item_id"]),
            spec_version_id=cast("int", specification["spec_version_id"]),
            spec_hash=cast("str", specification["spec_hash"]),
            spec_item_ids=tuple(
                cast("str", item) for item in _items(story_item["spec_item_ids"])
            ),
            title=cast("str", work_story["title"]),
            statement=cast("str", work_story["statement"]),
            persona=cast("str", work_story["persona"]),
            acceptance_criteria=tuple(
                cast("str", criterion)
                for criterion in _items(work_story["acceptance_criteria"])
            ),
            story_points=cast("int | None", work_story["story_points"]),
            rank=cast("str | None", work_story["rank"]),
        )
    )


def _owned_story_validation_input_for_test(
    *,
    title: str,
    spec_item_ids: tuple[str, ...],
    rank: str | None,
) -> tuple[JsonObject, str]:
    """Invoke both public pure validation-input owners with one fixed source."""
    payload = story_validation_input_payload(
        project_id=1,
        story_id=2,
        source_story_artifact_id=3,
        source_story_artifact_fingerprint="sha256:" + "1" * 64,
        source_story_item_id="US-000001",
        source_story_item_fingerprint="sha256:" + "2" * 64,
        source_backlog_artifact_id=4,
        source_backlog_artifact_fingerprint="sha256:" + "3" * 64,
        source_backlog_item_id="PBI-000001",
        spec_version_id=5,
        spec_hash="sha256:" + "4" * 64,
        spec_item_ids=spec_item_ids,
        title=title,
        statement="As a member, I want access so that I can work.",
        persona="Member",
        acceptance_criteria=("Access is granted.",),
        story_points=3,
        rank=rank,
    )
    fingerprint = story_validation_input_fingerprint(
        project_id=1,
        story_id=2,
        source_story_artifact_id=3,
        source_story_artifact_fingerprint="sha256:" + "1" * 64,
        source_story_item_id="US-000001",
        source_story_item_fingerprint="sha256:" + "2" * 64,
        source_backlog_artifact_id=4,
        source_backlog_artifact_fingerprint="sha256:" + "3" * 64,
        source_backlog_item_id="PBI-000001",
        spec_version_id=5,
        spec_hash="sha256:" + "4" * 64,
        spec_item_ids=spec_item_ids,
        title=title,
        statement="As a member, I want access so that I can work.",
        persona="Member",
        acceptance_criteria=("Access is granted.",),
        story_points=3,
        rank=rank,
    )
    return payload, fingerprint


def _append_specification_item(packet: JsonObject, item_id: str) -> None:
    """Append a shape-valid forged Specification item for packet mutation tests."""
    evidence = _object(packet["evidence"])
    items = _items(_object(evidence["specification"])["items"])
    forged = dict(_object(items[0]))
    forged["spec_item_id"] = item_id
    items.append(forged)
    items.sort(key=lambda item: cast("str", _object(item)["spec_item_id"]))


def _replace_projected_specification_id(
    packet: JsonObject, *, original: str, replacement: str
) -> None:
    """Keep every packet reference coherent while forging one projected ID."""
    evidence = _object(packet["evidence"])
    work = _object(packet["work"])

    def replace_ids(value: JsonValue) -> list[str]:
        return sorted(
            replacement if item == original else cast("str", item)
            for item in _items(value)
        )

    for item in _items(_object(evidence["specification"])["items"]):
        projected = _object(item)
        if projected["spec_item_id"] == original:
            projected["spec_item_id"] = replacement
    _items(_object(evidence["specification"])["items"]).sort(
        key=lambda item: cast("str", _object(item)["spec_item_id"])
    )
    _object(evidence["backlog_item"])["spec_item_ids"] = cast(
        "JsonValue", replace_ids(_object(evidence["backlog_item"])["spec_item_ids"])
    )
    _object(evidence["story_item"])["spec_item_ids"] = cast(
        "JsonValue", replace_ids(_object(evidence["story_item"])["spec_item_ids"])
    )
    validation = _object(evidence["story_validation"])
    validation["referenced_spec_item_ids"] = cast(
        "JsonValue", replace_ids(validation["referenced_spec_item_ids"])
    )
    for finding in _items(validation["semantic_findings"]):
        item = _object(finding)
        if item["spec_item_id"] == original:
            item["spec_item_id"] = replacement
    selected_story = _object(evidence["sprint_plan_story"])
    for proposal in _items(selected_story["tasks"]):
        _object(proposal)["relevant_spec_item_ids"] = cast(
            "JsonValue",
            replace_ids(_object(proposal)["relevant_spec_item_ids"]),
        )
    work_tasks = (
        [_object(work["task"])]
        if "task" in work
        else [_object(task) for task in _items(work["tasks"])]
    )
    for task in work_tasks:
        _object(task["metadata"])["relevant_spec_item_ids"] = cast(
            "JsonValue",
            replace_ids(_object(task["metadata"])["relevant_spec_item_ids"]),
        )
    _refresh_validation_story_fingerprints(packet)


def _make_informative_requirement_references(packet: JsonObject) -> None:
    """Keep packet references coherent while removing qualifying evidence."""
    evidence = _object(packet["evidence"])
    work = _object(packet["work"])
    specification = _object(evidence["specification"])
    projected_requirement = next(
        _object(item)
        for item in _items(specification["items"])
        if cast("str", _object(item)["spec_item_id"]).startswith("REQ.")
    )
    requirement_id = cast("str", projected_requirement["spec_item_id"])
    projected_requirement["level"] = "INFORMATIVE"

    def only_requirement() -> JsonValue:
        return cast("JsonValue", [requirement_id])

    specification["items"] = cast("JsonValue", [projected_requirement])
    _object(evidence["backlog_item"])["spec_item_ids"] = only_requirement()
    _object(evidence["story_item"])["spec_item_ids"] = only_requirement()
    validation = _object(evidence["story_validation"])
    validation["semantic_findings"] = cast("JsonValue", [])
    validation["referenced_spec_item_ids"] = only_requirement()
    selected_story = _object(evidence["sprint_plan_story"])
    for proposal in _items(selected_story["tasks"]):
        _object(proposal)["relevant_spec_item_ids"] = only_requirement()
    work_tasks = (
        [_object(work["task"])]
        if "task" in work
        else [_object(task) for task in _items(work["tasks"])]
    )
    for task in work_tasks:
        _object(task["metadata"])["relevant_spec_item_ids"] = only_requirement()
    _refresh_validation_story_fingerprints(packet)


def _add_informative_requirement(packet: JsonObject) -> tuple[str, str]:
    """Add one valid informative REQ beside the packet's qualifying REQ."""
    specification = _object(_object(packet["evidence"])["specification"])
    items = _items(specification["items"])
    qualifying_requirement = next(
        _object(item)
        for item in items
        if cast("str", _object(item)["spec_item_id"]).startswith("REQ.")
    )
    qualifying_id = cast("str", qualifying_requirement["spec_item_id"])
    informative_id = "REQ.packet-informative"
    assert all(_object(item)["spec_item_id"] != informative_id for item in items)
    informative_requirement = dict(qualifying_requirement)
    informative_requirement["spec_item_id"] = informative_id
    informative_requirement["level"] = "INFORMATIVE"
    items.append(informative_requirement)
    items.sort(key=lambda item: cast("str", _object(item)["spec_item_id"]))
    return qualifying_id, informative_id


def _set_packet_planning_reference_sets(
    packet: JsonObject,
    *,
    backlog_ids: tuple[str, ...],
    story_ids: tuple[str, ...],
    task_ids: tuple[str, ...],
) -> None:
    """Apply coherent canonical planning evidence references to one packet."""
    evidence = _object(packet["evidence"])
    work = _object(packet["work"])

    def canonical_ids(ids: tuple[str, ...]) -> JsonValue:
        return cast("JsonValue", sorted(ids))

    _object(evidence["backlog_item"])["spec_item_ids"] = canonical_ids(backlog_ids)
    story_item = _object(evidence["story_item"])
    story_item["spec_item_ids"] = canonical_ids(story_ids)
    validation = _object(evidence["story_validation"])
    validation["semantic_findings"] = cast("JsonValue", [])
    validation["referenced_spec_item_ids"] = canonical_ids(story_ids)
    for proposal in _items(_object(evidence["sprint_plan_story"])["tasks"]):
        _object(proposal)["relevant_spec_item_ids"] = canonical_ids(task_ids)
    work_tasks = (
        [_object(work["task"])]
        if "task" in work
        else [_object(task) for task in _items(work["tasks"])]
    )
    for task in work_tasks:
        _object(task["metadata"])["relevant_spec_item_ids"] = canonical_ids(task_ids)
    _refresh_validation_story_fingerprints(packet)


def _seed_replaced_planned_execution(engine: Engine) -> tuple[int, int, int, int]:
    """Persist accepted A then accepted replacement C on one unstarted Sprint."""
    project_id = _seed_accepted_backlog(engine)
    domain = _planning_domain(engine)
    _record_and_accept_roadmap(domain, project_id)
    _story_artifact_id, story_id = _record_and_accept_story(
        engine,
        domain,
        project_id,
    )
    plan_a_id, _candidate, plan, plan_a_fingerprint = _record_sprint_plan_draft(
        engine,
        domain,
        project_id,
        story_id,
        team_name="Packet replacement team",
        idempotency_key="packet-replacement-a",
    )
    accepted_a = domain.transition(
        DecideSprintPlan(
            **_planning_guards(domain.position(project_id), "planning.sprint.review"),
            idempotency_key="packet-accept-a",
            sprint_plan_artifact_id=plan_a_id,
            plan_fingerprint=plan_a_fingerprint,
            decision="accepted",
            rationale="Accept initial packet plan.",
        )
    )
    assert accepted_a.ok is True
    sprint_id = cast("int", accepted_a.output["activated_sprint_id"])
    with Session(engine) as session:
        specification = accepted_current_spec(
            WorkflowFactRepository(session).load(project_id)
        )
    assert specification is not None
    plan["sprint_goal"] = "Replacement packet goal."
    recorded_c = domain.transition(
        RecordSprintPlan(
            **_planning_guards(domain.position(project_id), "planning.sprint.plan"),
            idempotency_key="packet-replacement-c",
            team_name="Packet replacement team",
            spec_version_id=specification.spec_version_id,
            spec_hash=specification.spec_hash,
            planner_output=SprintPlannerOutput.model_validate(plan),
        )
    )
    assert recorded_c.ok is True
    plan_c_id = cast("int", recorded_c.output["sprint_plan_artifact_id"])
    accepted_c = domain.transition(
        DecideSprintPlan(
            **_planning_guards(domain.position(project_id), "planning.sprint.review"),
            idempotency_key="packet-accept-c",
            sprint_plan_artifact_id=plan_c_id,
            plan_fingerprint=cast("str", recorded_c.output["plan_fingerprint"]),
            decision="accepted",
            rationale="Accept replacement packet plan.",
        )
    )
    assert accepted_c.ok is True
    assert accepted_c.output["activated_sprint_id"] == sprint_id
    return project_id, sprint_id, story_id, plan_c_id


def test_packets_have_exact_versions_order_and_deterministic_metadata(
    engine: Engine,
) -> None:
    """Packets contain no clock and identical durable state yields identical bytes."""
    project_id, sprint_id, story_id, task_id = _seed(engine)
    with Session(engine) as session:
        story = build_story_packet(
            session,
            project_id=project_id,
            sprint_id=sprint_id,
            story_id=story_id,
        )
        story_again = build_story_packet(
            session,
            project_id=project_id,
            sprint_id=sprint_id,
            story_id=story_id,
        )
        task = build_task_packet(
            session,
            project_id=project_id,
            sprint_id=sprint_id,
            task_id=task_id,
        )

    assert story == story_again
    assert canonical_json(story) == canonical_json(story_again)
    assert list(story) == [
        "schema_version",
        "packet_kind",
        "metadata",
        "lineage",
        "context",
        "evidence",
        "work",
    ]
    assert story["schema_version"] == "story_packet.v3"
    assert story["packet_kind"] == "story"
    assert task["schema_version"] == "task_packet.v4"
    assert task["packet_kind"] == "task"
    for packet in (story, task):
        sprint = _object(_object(packet["context"])["sprint"])
        assert list(sprint) == [
            "goal",
            "status",
            "team_name",
            "owner_kind",
            "owner_key",
            "started_at",
            "start_date",
            "end_date",
        ]
        assert sprint["team_name"] == "Task 12 normalized execution team"
        assert sprint["owner_kind"] == "legacy_named_team"
        assert sprint["owner_key"] == (
            "agileforge:sprint-owner:legacy-named-team:v1:sha256:"
            "23cef5eb59c7cd9bc96df3dfbac45c03d245dd9c6f778d050a80e31109c424fb"
        )
    metadata = _object(story["metadata"])
    assert list(metadata) == ["packet_id", "source_fingerprint"]
    source = {key: story[key] for key in ("lineage", "context", "evidence", "work")}
    assert metadata["source_fingerprint"] == canonical_hash(source)
    assert "generated_at" not in canonical_json(story)


def test_packet_evidence_and_work_are_exact_direct_spec_contracts(
    engine: Engine,
) -> None:
    """Packet evidence follows accepted Spec/Backlog/Roadmap/Story/plan order."""
    project_id, sprint_id, story_id, task_id = _seed(engine)
    with Session(engine) as session:
        story = build_story_packet(
            session,
            project_id=project_id,
            sprint_id=sprint_id,
            story_id=story_id,
        )
        task = build_task_packet(
            session,
            project_id=project_id,
            sprint_id=sprint_id,
            task_id=task_id,
        )

    story_lineage = _object(story["lineage"])
    task_lineage = _object(task["lineage"])
    evidence = _object(story["evidence"])
    story_work = _object(story["work"])
    task_work = _object(task["work"])
    assert list(story_lineage) == [
        "specification",
        "backlog",
        "roadmap",
        "story",
        "sprint_plan",
        "sprint",
    ]
    assert list(task_lineage)[-1] == "task"
    assert list(evidence) == [
        "specification",
        "backlog_item",
        "roadmap_release",
        "story_item",
        "sprint_plan_story",
        "story_validation",
    ]
    specification_evidence = _object(evidence["specification"])
    story_validation = _object(evidence["story_validation"])
    assert specification_evidence["currentness"] == "current"
    assert story_validation["schema_version"] == (
        "agileforge.story-validation-evidence.v3"
    )
    accepted = _object(evidence["story_item"])
    operational = _object(story_work["story"])
    assert operational["title"] == accepted["story_title"]
    assert operational["statement"] == accepted["statement"]
    assert operational["acceptance_criteria"] == accepted["acceptance_criteria"]
    story_tasks = _items(story_work["tasks"])
    first_story_task = _object(story_tasks[0])
    story_task_metadata = _object(first_story_task["metadata"])
    task_value = _object(task_work["task"])
    assert story_task_metadata["version"] == "task_metadata.v2"
    assert task_value["metadata"] == story_task_metadata


def test_packet_fails_closed_when_sprint_owner_evidence_is_invalid(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Packet creation refuses an artifact whose durable owner chain is invalid."""
    project_id, sprint_id, story_id, _task_id = _seed(engine)

    def _invalid_owner_evidence(_session: Session, **_kwargs: object) -> None:
        message = "forged owner evidence"
        raise SprintOwnerEvidenceError(message)

    monkeypatch.setattr(
        "services.packets.canonical.load_sprint_owner_evidence",
        _invalid_owner_evidence,
    )
    with Session(engine) as session, pytest.raises(CanonicalPacketError) as error:
        build_story_packet(
            session,
            project_id=project_id,
            sprint_id=sprint_id,
            story_id=story_id,
        )

    assert error.value.code == "PACKET_CONTENT_INVALID"


def test_packet_fails_closed_when_sprint_team_disagrees_with_owner_evidence(
    engine: Engine,
) -> None:
    """Activated Sprint carrier must retain the accepted artifact owner label."""
    project_id, sprint_id, story_id, _task_id = _seed(engine)
    with Session(engine) as session:
        sprint = session.get_one(Sprint, sprint_id)
        different = Team(name="Different operational team")
        session.add(different)
        session.flush()
        assert different.team_id is not None
        sprint.team_id = different.team_id
        session.add(sprint)
        session.commit()

    with Session(engine) as session, pytest.raises(CanonicalPacketError) as error:
        build_story_packet(
            session,
            project_id=project_id,
            sprint_id=sprint_id,
            story_id=story_id,
        )

    assert error.value.code == "PACKET_LINEAGE_INVALID"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("owner_kind", "named_team"),
        ("owner_key", "agileforge:sprint-owner:named-team:v1:sha256:" + "0" * 64),
        ("team_name", "Forged Sprint owner"),
    ],
)
def test_validator_rejects_rehashed_sprint_owner_context_tampering(
    engine: Engine,
    field: str,
    value: str,
) -> None:
    """A refreshed packet fingerprint cannot replace its owner evidence contract."""
    project_id, sprint_id, story_id, _task_id = _seed(engine)
    with Session(engine) as session:
        packet = build_story_packet(
            session,
            project_id=project_id,
            sprint_id=sprint_id,
            story_id=story_id,
        )

    mutated = _rehashed_packet(packet)
    _object(_object(mutated["context"])["sprint"])[field] = value
    mutated = _rehashed_packet(mutated)

    with pytest.raises(CanonicalPacketError) as error:
        validate_canonical_packet(mutated)

    assert error.value.code == "PACKET_CONTENT_INVALID"


@pytest.mark.parametrize(
    ("owner_kind", "owner_label"),
    [
        ("solo_project", "Forged solo owner"),
        ("named_team", "[agileforge:sprint-owner:forged] Team"),
        ("named_team", " Named team with padding "),
    ],
)
def test_validator_rejects_rehashed_forged_sprint_owner_triples(
    engine: Engine,
    owner_kind: str,
    owner_label: str,
) -> None:
    """A matching kind/key pair cannot legitimize a forged owner label."""
    project_id, sprint_id, story_id, _task_id = _seed(engine)
    with Session(engine) as session:
        packet = build_story_packet(
            session,
            project_id=project_id,
            sprint_id=sprint_id,
            story_id=story_id,
        )

    mutated = _rehashed_packet(packet)
    sprint = _object(_object(mutated["context"])["sprint"])
    sprint["owner_kind"] = owner_kind
    sprint["team_name"] = owner_label
    sprint["owner_key"] = (
        f"agileforge:sprint-owner:solo-project:v1:project:{project_id}"
        if owner_kind == "solo_project"
        else "agileforge:sprint-owner:named-team:v1:sha256:"
        + hashlib.sha256(owner_label.encode()).hexdigest()
    )
    mutated = _rehashed_packet(mutated)

    with pytest.raises(CanonicalPacketError) as error:
        validate_canonical_packet(mutated)

    assert error.value.code == "PACKET_CONTENT_INVALID"


@pytest.mark.parametrize(
    ("mutation", "packet_kind"),
    [
        ("work_story_title", "story"),
        ("work_story_statement", "story"),
        ("validation_backlog_fingerprint", "story"),
        ("duplicate_specification_item", "story"),
        ("missing_referenced_specification_item", "story"),
        ("task_metadata_spec_hash", "task"),
        ("root_key_order", "story"),
        ("nested_key_order", "story"),
    ],
)
def test_validator_rejects_rehashed_cross_object_and_order_mutations(
    engine: Engine, mutation: str, packet_kind: str
) -> None:
    """Reject rehashed contradictions, duplicates, and reordering."""
    project_id, sprint_id, story_id, task_id = _seed(engine)
    with Session(engine) as session:
        packet = (
            build_task_packet(
                session,
                project_id=project_id,
                sprint_id=sprint_id,
                task_id=task_id,
            )
            if packet_kind == "task"
            else build_story_packet(
                session,
                project_id=project_id,
                sprint_id=sprint_id,
                story_id=story_id,
            )
        )

    mutated = _rehashed_packet(packet)
    evidence = _object(mutated["evidence"])
    work = _object(mutated["work"])
    if mutation == "work_story_title":
        _object(work["story"])["title"] = "Forged story title"
    elif mutation == "work_story_statement":
        _object(work["story"])["statement"] = "Forged story statement"
    elif mutation == "validation_backlog_fingerprint":
        _object(evidence["story_validation"])["source_backlog_artifact_fingerprint"] = (
            "sha256:" + "0" * 64
        )
    elif mutation == "duplicate_specification_item":
        items = _items(_object(evidence["specification"])["items"])
        items.append(items[0])
    elif mutation == "missing_referenced_specification_item":
        _object(evidence["specification"])["items"] = []
    elif mutation == "task_metadata_spec_hash":
        _object(_object(work["task"])["metadata"])["spec_hash"] = "sha256:" + "0" * 64
    elif mutation == "root_key_order":
        mutated = {key: mutated[key] for key in reversed(tuple(mutated))}
    else:
        mutated["lineage"] = {
            key: _object(mutated["lineage"])[key]
            for key in reversed(tuple(_object(mutated["lineage"])))
        }
    mutated = _rehashed_packet(mutated)

    with pytest.raises(CanonicalPacketError) as error:
        validate_canonical_packet(mutated)

    assert error.value.code == "PACKET_CONTENT_INVALID"


def test_packet_vision_does_not_claim_removed_task_fields() -> None:
    """The public vision reflects the closed Task Packet v3 work contract."""
    text = Path("docs/task-packet-vision.md").read_text()

    assert "executability flag" not in text
    assert "constraints" not in text


@pytest.mark.parametrize(
    "mutation",
    [
        "story_outside_backlog",
        "validation_reference_not_derived",
        "plan_reference_outside_story",
    ],
)
def test_validator_rejects_inconsistent_reference_boundaries(
    engine: Engine, mutation: str
) -> None:
    """Reference sets stay inside their immutable parent evidence."""
    project_id, sprint_id, story_id, _task_id = _seed(engine)
    with Session(engine) as session:
        packet = build_story_packet(
            session,
            project_id=project_id,
            sprint_id=sprint_id,
            story_id=story_id,
        )

    mutated = _rehashed_packet(packet)
    evidence = _object(mutated["evidence"])
    forged_id = "FORGED-REFERENCE"
    _append_specification_item(mutated, forged_id)
    if mutation == "story_outside_backlog":
        _object(evidence["story_item"])["spec_item_ids"] = [forged_id]
        _object(evidence["story_validation"])["referenced_spec_item_ids"] = [forged_id]
        _refresh_validation_story_fingerprints(mutated)
    elif mutation == "validation_reference_not_derived":
        _object(evidence["story_validation"])["referenced_spec_item_ids"] = [forged_id]
    else:
        proposal = _object(_items(_object(evidence["sprint_plan_story"])["tasks"])[0])
        proposal["relevant_spec_item_ids"] = [forged_id]
        work_task = _object(_items(_object(mutated["work"])["tasks"])[0])
        _object(work_task["metadata"])["relevant_spec_item_ids"] = [forged_id]
    mutated = _rehashed_packet(mutated)

    with pytest.raises(CanonicalPacketError) as error:
        validate_canonical_packet(mutated)

    assert error.value.code == "PACKET_CONTENT_INVALID"


@pytest.mark.parametrize("packet_kind", ["story", "task"])
def test_validator_rejects_rehashed_informative_only_normative_references(
    engine: Engine,
    packet_kind: str,
) -> None:
    """Every durable planning reference needs qualifying normative evidence."""
    project_id, sprint_id, story_id, task_id = _seed(engine)
    with Session(engine) as session:
        packet = (
            build_task_packet(
                session,
                project_id=project_id,
                sprint_id=sprint_id,
                task_id=task_id,
            )
            if packet_kind == "task"
            else build_story_packet(
                session,
                project_id=project_id,
                sprint_id=sprint_id,
                story_id=story_id,
            )
        )

    mutated = _rehashed_packet(packet)
    _make_informative_requirement_references(mutated)
    mutated = _rehashed_packet(mutated)

    with pytest.raises(CanonicalPacketError) as error:
        validate_canonical_packet(mutated)

    assert error.value.code == "PACKET_CONTENT_INVALID"


@pytest.mark.parametrize("packet_kind", ["story", "task"])
def test_validator_rejects_informative_story_and_task_reference_sets(
    engine: Engine,
    packet_kind: str,
) -> None:
    """A qualifying Backlog cannot mask nonqualifying child evidence sets."""
    project_id, sprint_id, story_id, task_id = _seed(engine)
    with Session(engine) as session:
        packet = (
            build_task_packet(
                session,
                project_id=project_id,
                sprint_id=sprint_id,
                task_id=task_id,
            )
            if packet_kind == "task"
            else build_story_packet(
                session,
                project_id=project_id,
                sprint_id=sprint_id,
                story_id=story_id,
            )
        )

    mutated = _rehashed_packet(packet)
    qualifying_id, informative_id = _add_informative_requirement(mutated)
    _set_packet_planning_reference_sets(
        mutated,
        backlog_ids=(qualifying_id, informative_id),
        story_ids=(informative_id,),
        task_ids=(informative_id,),
    )
    mutated = _rehashed_packet(mutated)

    with pytest.raises(CanonicalPacketError) as error:
        validate_canonical_packet(mutated)

    assert error.value.code == "PACKET_CONTENT_INVALID"


@pytest.mark.parametrize("packet_kind", ["story", "task"])
def test_validator_rejects_informative_selected_task_reference_set(
    engine: Engine,
    packet_kind: str,
) -> None:
    """A qualifying Story cannot mask a nonqualifying selected Task."""
    project_id, sprint_id, story_id, task_id = _seed(engine)
    with Session(engine) as session:
        packet = (
            build_task_packet(
                session,
                project_id=project_id,
                sprint_id=sprint_id,
                task_id=task_id,
            )
            if packet_kind == "task"
            else build_story_packet(
                session,
                project_id=project_id,
                sprint_id=sprint_id,
                story_id=story_id,
            )
        )

    mutated = _rehashed_packet(packet)
    qualifying_id, informative_id = _add_informative_requirement(mutated)
    _set_packet_planning_reference_sets(
        mutated,
        backlog_ids=(qualifying_id, informative_id),
        story_ids=(qualifying_id, informative_id),
        task_ids=(informative_id,),
    )
    mutated = _rehashed_packet(mutated)

    with pytest.raises(CanonicalPacketError) as error:
        validate_canonical_packet(mutated)

    assert error.value.code == "PACKET_CONTENT_INVALID"


@pytest.mark.parametrize("packet_kind", ["story", "task"])
def test_validator_accepts_mixed_qualifying_planning_reference_sets(
    engine: Engine,
    packet_kind: str,
) -> None:
    """Each reference boundary may retain informative evidence beside a requirement."""
    project_id, sprint_id, story_id, task_id = _seed(engine)
    with Session(engine) as session:
        packet = (
            build_task_packet(
                session,
                project_id=project_id,
                sprint_id=sprint_id,
                task_id=task_id,
            )
            if packet_kind == "task"
            else build_story_packet(
                session,
                project_id=project_id,
                sprint_id=sprint_id,
                story_id=story_id,
            )
        )

    mutated = _rehashed_packet(packet)
    qualifying_id, informative_id = _add_informative_requirement(mutated)
    mixed_ids = (qualifying_id, informative_id)
    _set_packet_planning_reference_sets(
        mutated,
        backlog_ids=mixed_ids,
        story_ids=mixed_ids,
        task_ids=mixed_ids,
    )
    mutated = _rehashed_packet(mutated)

    assert validate_canonical_packet(mutated) == mutated


def test_task_packet_allows_indistinguishable_duplicate_plan_tasks(
    engine: Engine,
) -> None:
    """Task packets cannot manufacture an ordinal absent from their schema."""
    project_id, sprint_id, _story_id, task_id = _seed(engine)
    with Session(engine) as session:
        packet = build_task_packet(
            session,
            project_id=project_id,
            sprint_id=sprint_id,
            task_id=task_id,
        )

    mutated = _rehashed_packet(packet)
    selected_story = _object(_object(mutated["evidence"])["sprint_plan_story"])
    tasks = _items(selected_story["tasks"])
    tasks.append(dict(_object(tasks[0])))
    mutated = _rehashed_packet(mutated)

    assert validate_canonical_packet(mutated) == mutated


@pytest.mark.parametrize(
    "mutation",
    ["duplicate_unrelated_roadmap_id", "superseded_specification_on_planned_sprint"],
)
def test_validator_rejects_invalid_roadmap_and_historical_execution_states(
    engine: Engine, mutation: str
) -> None:
    """Canonical packets retain closed Roadmap coverage and execution state."""
    project_id, sprint_id, story_id, _task_id = _seed(engine)
    with Session(engine) as session:
        packet = build_story_packet(
            session,
            project_id=project_id,
            sprint_id=sprint_id,
            story_id=story_id,
        )

    mutated = _rehashed_packet(packet)
    if mutation == "duplicate_unrelated_roadmap_id":
        roadmap_release = _object(_object(mutated["evidence"])["roadmap_release"])
        roadmap_ids = _items(roadmap_release["backlog_item_ids"])
        roadmap_ids.extend(["PBI-999999", "PBI-999999"])
    else:
        _object(_object(mutated["evidence"])["specification"])["currentness"] = (
            "superseded"
        )
        _object(_object(mutated["context"])["sprint"])["status"] = "Planned"
    mutated = _rehashed_packet(mutated)

    with pytest.raises(CanonicalPacketError) as error:
        validate_canonical_packet(mutated)

    assert error.value.code == "PACKET_CONTENT_INVALID"


@pytest.mark.parametrize(
    ("packet_kind", "status_field"),
    [
        ("story", "sprint"),
        ("story", "story"),
        ("story", "story_task"),
        ("task", "task"),
    ],
)
def test_validator_rejects_unknown_execution_statuses(
    engine: Engine, packet_kind: str, status_field: str
) -> None:
    """Every execution status comes from the closed durable enum."""
    project_id, sprint_id, story_id, task_id = _seed(engine)
    with Session(engine) as session:
        packet = (
            build_task_packet(
                session,
                project_id=project_id,
                sprint_id=sprint_id,
                task_id=task_id,
            )
            if packet_kind == "task"
            else build_story_packet(
                session,
                project_id=project_id,
                sprint_id=sprint_id,
                story_id=story_id,
            )
        )

    mutated = _rehashed_packet(packet)
    if status_field == "sprint":
        _object(_object(mutated["context"])["sprint"])["status"] = "Unknown"
    elif status_field == "story":
        _object(_object(mutated["work"])["story"])["status"] = "Unknown"
    elif status_field == "story_task":
        task = _object(_items(_object(mutated["work"])["tasks"])[0])
        task["status"] = "Unknown"
    else:
        _object(_object(mutated["work"])["task"])["status"] = "Unknown"
    mutated = _rehashed_packet(mutated)

    with pytest.raises(CanonicalPacketError) as error:
        validate_canonical_packet(mutated)

    assert error.value.code == "PACKET_CONTENT_INVALID"


def test_validator_rejects_noncanonical_validation_evidence_timestamp(
    engine: Engine,
) -> None:
    """Embedded evidence keeps its exact canonical JSON representation."""
    project_id, sprint_id, story_id, _task_id = _seed(engine)
    with Session(engine) as session:
        packet = build_story_packet(
            session,
            project_id=project_id,
            sprint_id=sprint_id,
            story_id=story_id,
        )

    mutated = _rehashed_packet(packet)
    validation = _object(_object(mutated["evidence"])["story_validation"])
    validated_at = cast("str", validation["validated_at"])
    assert validated_at.endswith("Z")
    validation["validated_at"] = validated_at.removesuffix("Z") + "+00:00"
    mutated = _rehashed_packet(mutated)

    with pytest.raises(CanonicalPacketError) as error:
        validate_canonical_packet(mutated)

    assert error.value.code == "PACKET_CONTENT_INVALID"


@pytest.mark.parametrize(
    "mutation",
    [
        "normative_without_criterion",
        "normative_without_level",
        "normative_without_verification",
        "blank_criterion",
        "malformed_id",
        "blank_id",
        "blank_title",
        "blank_statement",
    ],
)
def test_validator_rejects_invalid_projected_specification_items(
    engine: Engine, mutation: str
) -> None:
    """Projected Specification evidence retains public profile semantics."""
    project_id, sprint_id, story_id, _task_id = _seed(engine)
    with Session(engine) as session:
        packet = build_story_packet(
            session,
            project_id=project_id,
            sprint_id=sprint_id,
            story_id=story_id,
        )

    mutated = _rehashed_packet(packet)
    items = _items(_object(_object(mutated["evidence"])["specification"])["items"])
    normative = next(
        _object(item)
        for item in items
        if cast("str", _object(item)["spec_item_id"]).split(".", maxsplit=1)[0]
        in {"REQ", "QUALITY", "CONSTRAINT", "INTERFACE", "DATA"}
    )
    assert normative["level"] == "MUST"
    if mutation == "normative_without_criterion":
        normative["acceptance_criteria"] = []
    elif mutation == "normative_without_level":
        normative["level"] = None
    elif mutation == "normative_without_verification":
        normative["verification_method"] = None
    elif mutation == "blank_criterion":
        normative["acceptance_criteria"] = ["   "]
    elif mutation == "malformed_id":
        original = cast("str", normative["spec_item_id"])
        _replace_projected_specification_id(
            mutated,
            original=original,
            replacement="REQ.not valid!",
        )
    elif mutation == "blank_id":
        original = cast("str", normative["spec_item_id"])
        _replace_projected_specification_id(
            mutated,
            original=original,
            replacement="",
        )
    elif mutation == "blank_title":
        normative["title"] = "   "
    else:
        normative["statement"] = "   "
    mutated = _rehashed_packet(mutated)

    with pytest.raises(CanonicalPacketError) as error:
        validate_canonical_packet(mutated)

    assert error.value.code == "PACKET_CONTENT_INVALID"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("started_at", "not-a-datetime"),
        ("started_at", "2026-08-21T12:00:00+00:00"),
        ("started_at", "2026-08-21T12:00:00.0Z"),
        ("start_date", "2026-2-3"),
        ("start_date", "2026-02-30"),
        ("end_date", "not-a-date"),
        ("end_date", "2026-08-21T00:00:00Z"),
    ],
)
def test_validator_rejects_noncanonical_context_temporals(
    engine: Engine, field: str, value: str
) -> None:
    """Snapshot temporal fields use the exact canonical projection forms."""
    project_id, sprint_id, story_id, _task_id = _seed(engine)
    with Session(engine) as session:
        packet = build_story_packet(
            session,
            project_id=project_id,
            sprint_id=sprint_id,
            story_id=story_id,
        )

    mutated = _rehashed_packet(packet)
    _object(_object(mutated["context"])["sprint"])[field] = value
    mutated = _rehashed_packet(mutated)

    with pytest.raises(CanonicalPacketError) as error:
        validate_canonical_packet(mutated)

    assert error.value.code == "PACKET_CONTENT_INVALID"


@pytest.mark.parametrize(
    ("title", "spec_item_ids", "rank"),
    [
        (
            "Changed account access.",
            ("QUALITY.latency", "REQ.account-access"),
            "100",
        ),
        (
            "Account access.",
            ("QUALITY.new-latency", "REQ.account-access"),
            "100",
        ),
        (
            "Account access.",
            ("QUALITY.latency", "REQ.account-access"),
            "200",
        ),
    ],
)
def test_story_validation_input_owner_controls_payload_and_fingerprint(
    title: str,
    spec_item_ids: tuple[str, ...],
    rank: str,
) -> None:
    """One pure owner defines the payload bytes and fingerprint together."""
    baseline_payload, baseline_fingerprint = _owned_story_validation_input_for_test(
        title="Account access.",
        spec_item_ids=("QUALITY.latency", "REQ.account-access"),
        rank="100",
    )
    payload, fingerprint = _owned_story_validation_input_for_test(
        title=title,
        spec_item_ids=spec_item_ids,
        rank=rank,
    )

    assert _items(payload["spec_item_ids"]) == sorted(spec_item_ids)
    assert fingerprint == canonical_hash(payload)
    assert payload != baseline_payload
    assert fingerprint != baseline_fingerprint


def test_started_packet_keeps_exact_superseded_specification(
    engine: Engine,
) -> None:
    """Older active execution remains pinned and is labelled superseded."""
    project_id, sprint_id, story_id, _task_id = _seed(engine)
    with Session(engine) as session:
        before = build_story_packet(
            session,
            project_id=project_id,
            sprint_id=sprint_id,
            story_id=story_id,
        )
        before_lineage = _object(before["lineage"])
        before_specification_lineage = _object(before_lineage["specification"])
        specification = session.get(
            SpecRegistry,
            cast("int", before_specification_lineage["spec_version_id"]),
        )
        assert specification is not None
        specification.status = "superseded"
        session.add(specification)
        session.commit()
        after = build_story_packet(
            session,
            project_id=project_id,
            sprint_id=sprint_id,
            story_id=story_id,
        )

    after_specification_lineage = _object(_object(after["lineage"])["specification"])
    after_specification_evidence = _object(_object(after["evidence"])["specification"])
    before_specification_evidence = _object(
        _object(before["evidence"])["specification"]
    )
    assert after_specification_lineage == before_specification_lineage
    assert (
        after_specification_evidence["items"] == before_specification_evidence["items"]
    )
    assert after_specification_evidence["currentness"] == "superseded"


def test_current_accepted_replacement_plan_builds_packet_for_same_planned_sprint(
    engine: Engine,
) -> None:
    """Accepted historical A cannot make the current accepted leaf C ambiguous."""
    project_id, sprint_id, story_id, plan_c_id = _seed_replaced_planned_execution(
        engine
    )

    with Session(engine) as session:
        packet = build_story_packet(
            session,
            project_id=project_id,
            sprint_id=sprint_id,
            story_id=story_id,
        )

    sprint_plan = _object(_object(packet["lineage"])["sprint_plan"])
    assert sprint_plan["sprint_plan_artifact_id"] == plan_c_id


def test_superseded_packet_rejects_planned_sprint_even_with_start_row(
    engine: Engine,
) -> None:
    """SprintStart is historical proof only while execution is active or terminal."""
    project_id, sprint_id, story_id, _task_id = _seed(engine)
    with Session(engine) as session:
        packet = build_story_packet(
            session,
            project_id=project_id,
            sprint_id=sprint_id,
            story_id=story_id,
        )
        specification = session.get(
            SpecRegistry,
            _object(_object(packet["lineage"])["specification"])["spec_version_id"],
        )
        sprint = session.get(Sprint, sprint_id)
        assert specification is not None
        assert sprint is not None
        specification.status = "superseded"
        sprint.status = SprintStatus.PLANNED
        session.add(specification)
        session.add(sprint)
        session.commit()

        with pytest.raises(CanonicalPacketError) as error:
            build_story_packet(
                session,
                project_id=project_id,
                sprint_id=sprint_id,
                story_id=story_id,
            )

    assert error.value.code == "PACKET_LINEAGE_INVALID"


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("plan_fingerprint", "sha256:" + "b" * 64),
        ("task_content_fingerprint", "sha256:" + "c" * 64),
        ("dependency_rows_fingerprint", "sha256:" + "d" * 64),
        ("decision_fingerprint", "sha256:" + "f" * 64),
    ],
)
def test_superseded_packet_rejects_corrupt_execution_contract_proof(
    engine: Engine,
    field: str,
    replacement: str,
) -> None:
    """Historical reads reject corrupt start, Task, dependency, or decision proof."""
    project_id, sprint_id, story_id, _task_id = _seed(engine)
    with Session(engine) as session:
        packet = build_story_packet(
            session,
            project_id=project_id,
            sprint_id=sprint_id,
            story_id=story_id,
        )
        specification = session.get(
            SpecRegistry,
            _object(_object(packet["lineage"])["specification"])["spec_version_id"],
        )
        start = session.exec(
            select(SprintStart).where(SprintStart.sprint_id == sprint_id)
        ).one()
        assert specification is not None
        specification.status = "superseded"
        setattr(start, field, replacement)
        session.add(specification)
        session.add(start)
        session.commit()

        with pytest.raises(CanonicalPacketError) as error:
            build_story_packet(
                session,
                project_id=project_id,
                sprint_id=sprint_id,
                story_id=story_id,
            )

    assert error.value.code == "PACKET_LINEAGE_INVALID"


def test_packet_reuses_deep_story_validation_evidence_owner(
    engine: Engine,
) -> None:
    """Canonical but source-mismatched v3 evidence cannot enter a packet."""
    project_id, sprint_id, story_id, _task_id = _seed(engine)
    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        evidence = json.loads(cast("str", story.validation_evidence))
        evidence["source_backlog_artifact_id"] += 10_000
        story.validation_evidence = canonical_json(evidence)
        session.add(story)
        session.commit()

        with pytest.raises(CanonicalPacketError) as error:
            build_story_packet(
                session,
                project_id=project_id,
                sprint_id=sprint_id,
                story_id=story_id,
            )

    assert error.value.code == "PACKET_LINEAGE_INVALID"


def test_packet_rejects_noncanonical_or_mismatched_task_metadata(
    engine: Engine,
) -> None:
    """No missing, reformatted, legacy, or identity-drifted Task metadata is read."""
    project_id, sprint_id, _story_id, task_id = _seed(engine)
    with Session(engine) as session:
        row = session.get(Task, task_id)
        assert row is not None
        payload = json.loads(row.metadata_json)
        payload["extra"] = True
        row.metadata_json = json.dumps(payload, sort_keys=True)
        session.add(row)
        session.commit()
        with pytest.raises(CanonicalPacketError) as error:
            build_task_packet(
                session,
                project_id=project_id,
                sprint_id=sprint_id,
                task_id=task_id,
            )
    assert error.value.code == "TASK_METADATA_INVALID"


def test_packet_rejects_canonical_metadata_from_different_specification(
    engine: Engine,
) -> None:
    """A well-formed v3 object must still equal the accepted plan identity."""
    project_id, sprint_id, _story_id, task_id = _seed(engine)
    with Session(engine) as session:
        row = session.get(Task, task_id)
        assert row is not None
        payload = json.loads(row.metadata_json)
        payload["spec_hash"] = "sha256:" + "f" * 64
        row.metadata_json = canonical_json(payload)
        session.add(row)
        session.commit()
        with pytest.raises(CanonicalPacketError) as error:
            build_task_packet(
                session,
                project_id=project_id,
                sprint_id=sprint_id,
                task_id=task_id,
            )
    assert error.value.code == "TASK_METADATA_INVALID"


def test_packet_rejects_mutable_story_substitution(
    engine: Engine,
) -> None:
    """Operational display fields cannot replace accepted canonical Story bytes."""
    project_id, sprint_id, story_id, _task_id = _seed(engine)
    with Session(engine) as session:
        row = session.get(UserStory, story_id)
        assert row is not None
        row.title = "Mutable substitute"
        session.add(row)
        session.commit()
        with pytest.raises(CanonicalPacketError) as error:
            build_story_packet(
                session,
                project_id=project_id,
                sprint_id=sprint_id,
                story_id=story_id,
            )
    assert error.value.code == "PACKET_LINEAGE_INVALID"


def test_packet_context_errors_are_closed(engine: Engine) -> None:
    """Unknown Project and missing packet contexts use only public closed codes."""
    project_id, sprint_id, story_id, task_id = _seed(engine)
    with Session(engine) as session:
        project = session.get(Project, project_id)
        assert project is not None
        with pytest.raises(CanonicalPacketError) as missing_project:
            build_story_packet(
                session,
                project_id=999_999,
                sprint_id=sprint_id,
                story_id=story_id,
            )
        with pytest.raises(CanonicalPacketError) as missing_story:
            build_story_packet(
                session,
                project_id=project_id,
                sprint_id=999_999,
                story_id=story_id,
            )
        with pytest.raises(CanonicalPacketError) as missing_task:
            build_task_packet(
                session,
                project_id=project_id,
                sprint_id=sprint_id,
                task_id=task_id + 999_999,
            )
    assert missing_project.value.code == "PROJECT_NOT_FOUND"
    assert missing_story.value.code == "STORY_PACKET_CONTEXT_NOT_FOUND"
    assert missing_task.value.code == "TASK_PACKET_CONTEXT_NOT_FOUND"
