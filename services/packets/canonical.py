"""Exact canonical execution packets over accepted direct-Spec planning lineage."""

# ruff: noqa: C901, D107, EM101, PLR0912, PLR0913

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Literal, Never, cast

from pydantic import TypeAdapter, ValidationError
from sqlmodel import Session, col, select

from models.core import Project, Sprint, SprintStory, Task, Team, TeamMember, UserStory
from models.enums import SprintStatus, StoryStatus, TaskStatus
from models.workflow import (
    BacklogArtifact,
    BacklogArtifactDecision,
    RoadmapArtifact,
    RoadmapArtifactDecision,
    SprintPlanArtifact,
    SprintPlanArtifactDecision,
    StoryArtifact,
    StoryArtifactDecision,
)
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from services.agent_workbench.story_phase import load_stored_story_planning_content
from services.contracts.backlog import BacklogItem
from services.contracts.roadmap import RoadmapRelease
from services.contracts.specification_references import (
    derived_referenced_spec_item_ids,
    has_qualifying_normative_evidence,
)
from services.contracts.sprint import SprintPlannerSelectedStory, StructuredTaskSpec
from services.contracts.story import CanonicalStoryItem
from services.planning_artifact_content import (
    load_bound_sprint_plan_envelope,
    load_stored_backlog_planning_content,
    load_stored_roadmap_planning_content,
)
from services.planning_lineage import (
    ArtifactLineageNode,
    PlanningLineageError,
    select_current_accepted_artifact,
)
from services.planning_lineage import Decision as PlanningLineageDecision
from services.specs.accepted_specification import (
    AcceptedSpecification,
    AcceptedSpecificationIntegrityError,
    load_accepted_specification,
)
from services.specs.story_validation_service import (
    StoryValidationReadinessError,
    require_story_validation_evidence,
    story_validation_input_fingerprint,
)
from services.sprint_ownership import (
    SprintOwnerEvidence,
    SprintOwnerEvidenceError,
    load_sprint_owner_evidence,
    validate_sprint_owner_identity,
)
from utils.agileforge_spec_profile_v2 import SpecificationItem, SpecItemType
from utils.spec_schemas import ValidationEvidence
from utils.task_metadata import (
    TaskMetadata,
    metadata_from_structured_task,
    parse_task_metadata,
)
from workflow.contracts import JsonObject, JsonValue
from workflow.execution_integrity import ExecutionIntegrityError, execution_contract
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from collections.abc import Sequence

_JSON_OBJECT = TypeAdapter(JsonObject)
_TASK_SCHEMA_VERSION = "task_packet.v4"
_STORY_SCHEMA_VERSION = "story_packet.v3"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_PACKET_ID = re.compile(r"^[st]p_[0-9a-f]{16}$")
_SPRINT_PLAN_STREAM_ID = re.compile(r"^SPS-[0-9a-f]{32}$")
_SPRINT_STATUS_VALUES: frozenset[str] = frozenset(
    status.value for status in SprintStatus
)
_STORY_STATUS_VALUES: frozenset[str] = frozenset(status.value for status in StoryStatus)
_TASK_STATUS_VALUES: frozenset[str] = frozenset(status.value for status in TaskStatus)


class CanonicalPacketError(RuntimeError):
    """Typed closed packet projection failure for transport translation."""

    def __init__(self, code: str, message: str, *, details: JsonObject) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


@dataclass(frozen=True)
class _PacketContext:
    project: Project
    sprint: Sprint
    story: UserStory
    owner_kind: Literal["solo_project", "named_team", "legacy_named_team"]
    owner_key: str
    owner_label: str
    specification: AcceptedSpecification
    backlog: BacklogArtifact
    backlog_item: BacklogItem
    roadmap: RoadmapArtifact
    roadmap_release: RoadmapRelease
    story_artifact: StoryArtifact
    story_item: CanonicalStoryItem
    plan: SprintPlanArtifact
    selected_story: SprintPlannerSelectedStory
    tasks: tuple[Task, ...]
    task_metadata: tuple[TaskMetadata, ...]
    validation: ValidationEvidence


def _error(code: str, message: str, **details: JsonValue) -> CanonicalPacketError:
    return CanonicalPacketError(code, message, details=details)


_SPEC_ITEM_SHAPE: dict[str, object] = {
    "spec_item_id": str,
    "title": str,
    "statement": str,
    "level": (str, type(None)),
    "acceptance_criteria": [str],
    "verification_method": (str, type(None)),
}
_BACKLOG_ITEM_SHAPE: dict[str, object] = {
    "backlog_item_id": str,
    "priority": int,
    "requirement": str,
    "spec_item_ids": [str],
    "value_driver": str,
    "justification": str,
    "estimated_effort": str,
    "technical_note": (str, type(None)),
}
_ROADMAP_RELEASE_SHAPE: dict[str, object] = {
    "release_name": str,
    "theme": str,
    "focus_area": str,
    "backlog_item_ids": [str],
    "reasoning": str,
}
_INVEST_DIMENSION_SHAPE: dict[str, object] = {
    "result": str,
    "rationale": str,
    "evidence": str,
}
_INVEST_ASSESSMENT_SHAPE: dict[str, object] = {
    "independent": _INVEST_DIMENSION_SHAPE,
    "negotiable": _INVEST_DIMENSION_SHAPE,
    "valuable": _INVEST_DIMENSION_SHAPE,
    "estimable": _INVEST_DIMENSION_SHAPE,
    "small": _INVEST_DIMENSION_SHAPE,
    "testable": _INVEST_DIMENSION_SHAPE,
}
_STORY_ITEM_SHAPE: dict[str, object] = {
    "story_item_id": str,
    "story_title": str,
    "statement": str,
    "persona": str,
    "acceptance_criteria": [str],
    "spec_item_ids": [str],
    "invest_assessment": _INVEST_ASSESSMENT_SHAPE,
    "estimated_effort": str,
    "effort_rationale": str,
    "order_rationale": str,
    "produced_artifacts": [str],
    "research_caveats": [str],
    "dependency_candidates": [
        {"prerequisite_ref": str, "reason": str, "confidence": str}
    ],
}
_TASK_SPEC_SHAPE: dict[str, object] = {
    "description": str,
    "relevant_spec_item_ids": [str],
    "task_kind": str,
    "artifact_targets": [str],
    "workstream_tags": [str],
    "checklist_items": [str],
}
_SELECTED_STORY_SHAPE: dict[str, object] = {
    "story_id": int,
    "story_item_id": str,
    "tasks": [_TASK_SPEC_SHAPE],
    "reason_for_selection": str,
}
_VALIDATION_SHAPE: dict[str, object] = {
    "schema_version": str,
    "project_id": int,
    "story_id": int,
    "source_story_artifact_id": int,
    "source_story_artifact_fingerprint": str,
    "source_story_item_id": str,
    "source_story_item_fingerprint": str,
    "source_backlog_artifact_id": int,
    "source_backlog_artifact_fingerprint": str,
    "source_backlog_item_id": str,
    "spec_version_id": int,
    "spec_hash": str,
    "validated_at": str,
    "story_validation_input_fingerprint": str,
    "validator_version": str,
    "mode": str,
    "structurally_eligible": bool,
    "structural_failures": [{"code": str, "message": str}],
    "structural_warnings": [object],
    "semantic_review_state": str,
    "semantic_findings": [
        {
            "code": str,
            "spec_item_id": str,
            "message": str,
            "suggested_change": (str, type(None)),
        }
    ],
    "referenced_spec_item_ids": [str],
}
_TASK_METADATA_SHAPE: dict[str, object] = {
    "version": str,
    "spec_version_id": int,
    "spec_hash": str,
    "sprint_plan_stream_id": str,
    "sprint_plan_artifact_id": int,
    "sprint_plan_fingerprint": str,
    "relevant_spec_item_ids": [str],
    "task_kind": str,
    "artifact_targets": [str],
    "workstream_tags": [str],
    "checklist_items": [str],
}
_STORY_WORK_SHAPE: dict[str, object] = {
    "title": str,
    "statement": str,
    "persona": str,
    "acceptance_criteria": [str],
    "status": str,
    "story_points": (int, type(None)),
    "rank": (str, type(None)),
}
_LINEAGE_SHAPE: dict[str, object] = {
    "specification": {"spec_version_id": int, "spec_hash": str},
    "backlog": {
        "backlog_artifact_id": int,
        "artifact_fingerprint": str,
        "backlog_item_id": str,
    },
    "roadmap": {"roadmap_artifact_id": int, "artifact_fingerprint": str},
    "story": {
        "story_id": int,
        "story_artifact_id": int,
        "artifact_fingerprint": str,
        "story_item_id": str,
    },
    "sprint_plan": {
        "sprint_plan_stream_id": str,
        "sprint_plan_artifact_id": int,
        "plan_fingerprint": str,
    },
    "sprint": {"sprint_id": int},
}
_CONTEXT_SHAPE: dict[str, object] = {
    "project": {"project_id": int, "name": str},
    "sprint": {
        "goal": str,
        "status": str,
        "team_name": str,
        "owner_kind": str,
        "owner_key": str,
        "started_at": (str, type(None)),
        "start_date": (str, type(None)),
        "end_date": (str, type(None)),
    },
}
_EVIDENCE_SHAPE: dict[str, object] = {
    "specification": {"currentness": str, "items": [_SPEC_ITEM_SHAPE]},
    "backlog_item": _BACKLOG_ITEM_SHAPE,
    "roadmap_release": _ROADMAP_RELEASE_SHAPE,
    "story_item": _STORY_ITEM_SHAPE,
    "sprint_plan_story": _SELECTED_STORY_SHAPE,
    "story_validation": _VALIDATION_SHAPE,
}


def _validate_shape(value: object, schema: object, *, path: str) -> None:
    """Validate exact keys and Python JSON types without normalizing input."""
    if isinstance(schema, dict):
        schema_mapping = cast("dict[str, object]", schema)
        if not isinstance(value, dict) or list(value) != list(schema_mapping):
            message = f"{path} does not match the closed packet object shape."
            raise ValueError(message)
        mapping = cast("dict[str, object]", value)
        for key, child_schema in schema_mapping.items():
            _validate_shape(mapping[key], child_schema, path=f"{path}.{key}")
        return
    if isinstance(schema, list):
        if not isinstance(value, list):
            message = f"{path} must be an array."
            raise TypeError(message)
        if schema:
            for index, item in enumerate(value):
                _validate_shape(item, schema[0], path=f"{path}[{index}]")
        elif value:
            message = f"{path} must be empty."
            raise ValueError(message)
        return
    allowed = schema if isinstance(schema, tuple) else (schema,)
    if object in allowed:
        return
    if type(value) not in allowed:
        message = f"{path} has an invalid packet value type."
        raise TypeError(message)


def _packet_shape(kind: Literal["story", "task"]) -> dict[str, object]:
    lineage = dict(_LINEAGE_SHAPE)
    if kind == "task":
        lineage["task"] = {"task_id": int}
        work: dict[str, object] = {
            "story": _STORY_WORK_SHAPE,
            "task": {
                "description": str,
                "status": str,
                "assignee_name": (str, type(None)),
                "metadata": _TASK_METADATA_SHAPE,
            },
        }
    else:
        work = {
            "story": _STORY_WORK_SHAPE,
            "tasks": [
                {
                    "description": str,
                    "status": str,
                    "metadata": _TASK_METADATA_SHAPE,
                }
            ],
        }
    return {
        "schema_version": str,
        "packet_kind": str,
        "metadata": {"packet_id": str, "source_fingerprint": str},
        "lineage": lineage,
        "context": _CONTEXT_SHAPE,
        "evidence": _EVIDENCE_SHAPE,
        "work": work,
    }


def _require_packet_hashes(value: object) -> None:
    if isinstance(value, dict):
        for key, item in cast("dict[str, object]", value).items():
            if key.endswith(("fingerprint", "_hash")) and (
                not isinstance(item, str) or _SHA256.fullmatch(item) is None
            ):
                message = f"Packet field {key} is not a canonical SHA-256 value."
                raise ValueError(message)
            _require_packet_hashes(item)
    elif isinstance(value, list):
        for item in value:
            _require_packet_hashes(item)


def _invalid_packet_content() -> Never:
    raise ValueError


def _validate_projected_specification_item(item: JsonObject) -> SpecificationItem:
    """Apply the public Specification-item contract to packet evidence."""
    item_id = cast("str", item["spec_item_id"])
    item_type = SpecItemType(item_id.split(".", maxsplit=1)[0])
    return SpecificationItem.model_validate(
        {
            "id": item_id,
            "type": item_type,
            "title": item["title"],
            "statement": item["statement"],
            "level": item["level"],
            "acceptance": item["acceptance_criteria"],
            "verification": item["verification_method"],
        }
    )


def _require_canonical_context_temporals(context: JsonObject) -> None:
    """Require exact builder-rendered UTC datetime and ISO-date bytes."""
    sprint = cast("JsonObject", context["sprint"])
    started_at = sprint["started_at"]
    if started_at is not None:
        started_at_text = cast("str", started_at)
        parsed_started_at = datetime.fromisoformat(started_at_text)
        if (
            not started_at_text.endswith("Z")
            or _temporal(parsed_started_at) != started_at_text
        ):
            _invalid_packet_content()
    for field_name in ("start_date", "end_date"):
        value = sprint[field_name]
        if value is not None:
            value_text = cast("str", value)
            if date.fromisoformat(value_text).isoformat() != value_text:
                _invalid_packet_content()


def _require_canonical_sprint_owner_context(context: JsonObject) -> None:
    """Require the explicit owner kind, key, and retained label to agree."""
    project = cast("JsonObject", context["project"])
    sprint = cast("JsonObject", context["sprint"])
    owner = SprintOwnerEvidence.model_validate(
        {
            "kind": sprint["owner_kind"],
            "key": sprint["owner_key"],
            "label": sprint["team_name"],
        }
    )
    validate_sprint_owner_identity(
        owner,
        project_id=cast("int", project["project_id"]),
    )


def _require_canonical_agreement(
    *,
    kind: Literal["story", "task"],
    lineage: JsonObject,
    context: JsonObject,
    evidence: JsonObject,
    work: JsonObject,
    selected_story: SprintPlannerSelectedStory,
    task_metadata: tuple[TaskMetadata, ...],
    projected_specification_items: tuple[SpecificationItem, ...],
) -> None:
    """Require all duplicated packet evidence to describe one durable contract."""
    specification = cast("JsonObject", lineage["specification"])
    backlog = cast("JsonObject", lineage["backlog"])
    story = cast("JsonObject", lineage["story"])
    sprint_plan = cast("JsonObject", lineage["sprint_plan"])
    backlog_item = cast("JsonObject", evidence["backlog_item"])
    roadmap_release = cast("JsonObject", evidence["roadmap_release"])
    specification_evidence = cast("JsonObject", evidence["specification"])
    story_item = cast("JsonObject", evidence["story_item"])
    selected_story_evidence = cast("JsonObject", evidence["sprint_plan_story"])
    validation = cast("JsonObject", evidence["story_validation"])
    work_story = cast("JsonObject", work["story"])
    sprint_context = cast("JsonObject", context["sprint"])
    project = cast("JsonObject", context["project"])
    validation_matches_lineage = (
        validation["project_id"] == project["project_id"]
        and validation["story_id"] == story["story_id"]
        and validation["source_story_artifact_id"] == story["story_artifact_id"]
        and validation["source_story_artifact_fingerprint"]
        == story["artifact_fingerprint"]
        and validation["source_story_item_id"] == story["story_item_id"]
        and validation["source_story_item_fingerprint"] == canonical_hash(story_item)
        and validation["source_backlog_artifact_id"] == backlog["backlog_artifact_id"]
        and validation["source_backlog_artifact_fingerprint"]
        == backlog["artifact_fingerprint"]
        and validation["source_backlog_item_id"] == backlog["backlog_item_id"]
        and validation["spec_version_id"] == specification["spec_version_id"]
        and validation["spec_hash"] == specification["spec_hash"]
        and validation["structurally_eligible"] is True
    )
    story_matches_lineage = (
        story_item["story_item_id"] == story["story_item_id"]
        and selected_story_evidence["story_id"] == story["story_id"]
        and selected_story_evidence["story_item_id"] == story["story_item_id"]
        and work_story["title"] == story_item["story_title"]
        and work_story["statement"] == story_item["statement"]
        and work_story["persona"] == story_item["persona"]
        and work_story["acceptance_criteria"] == story_item["acceptance_criteria"]
    )
    story_spec_item_ids = cast("list[str]", story_item["spec_item_ids"])
    roadmap_item_ids = cast("list[str]", roadmap_release["backlog_item_ids"])
    validation_references = derived_referenced_spec_item_ids(
        story_spec_item_ids,
        tuple(
            cast("str", finding["spec_item_id"])
            for finding in cast("list[JsonObject]", validation["semantic_findings"])
        ),
    )
    if (
        backlog_item["backlog_item_id"] != backlog["backlog_item_id"]
        or cast("list[JsonValue]", roadmap_release["backlog_item_ids"]).count(
            backlog["backlog_item_id"]
        )
        != 1
        or len(roadmap_item_ids) != len(set(roadmap_item_ids))
        or not set(story_spec_item_ids).issubset(
            cast("list[str]", backlog_item["spec_item_ids"])
        )
        or (
            specification_evidence["currentness"] == "superseded"
            and sprint_context["status"] == SprintStatus.PLANNED.value
        )
        or cast("str", sprint_context["status"]) not in _SPRINT_STATUS_VALUES
        or cast("str", work_story["status"]) not in _STORY_STATUS_VALUES
        or not validation_matches_lineage
        or not story_matches_lineage
        or tuple(cast("list[str]", validation["referenced_spec_item_ids"]))
        != validation_references
        or validation["story_validation_input_fingerprint"]
        != story_validation_input_fingerprint(
            project_id=cast("int", project["project_id"]),
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
            spec_item_ids=tuple(story_spec_item_ids),
            title=cast("str", work_story["title"]),
            statement=cast("str", work_story["statement"]),
            persona=cast("str", work_story["persona"]),
            acceptance_criteria=tuple(
                cast("list[str]", work_story["acceptance_criteria"])
            ),
            story_points=cast("int | None", work_story["story_points"]),
            rank=cast("str | None", work_story["rank"]),
        )
    ):
        _invalid_packet_content()

    proposals = selected_story.tasks
    if any(
        not set(proposal.relevant_spec_item_ids).issubset(story_spec_item_ids)
        for proposal in proposals
    ):
        _invalid_packet_content()
    task_values = (
        [cast("JsonObject", work["task"])]
        if kind == "task"
        else cast("list[JsonObject]", work["tasks"])
    )
    if any(
        cast("str", task["status"]) not in _TASK_STATUS_VALUES for task in task_values
    ):
        _invalid_packet_content()

    def matches_proposal(
        task: JsonObject,
        observed_metadata: TaskMetadata,
        proposal: StructuredTaskSpec,
    ) -> bool:
        expected_metadata = metadata_from_structured_task(
            proposal,
            spec_version_id=cast("int", specification["spec_version_id"]),
            spec_hash=cast("str", specification["spec_hash"]),
            sprint_plan_stream_id=cast("str", sprint_plan["sprint_plan_stream_id"]),
            sprint_plan_artifact_id=cast("int", sprint_plan["sprint_plan_artifact_id"]),
            sprint_plan_fingerprint=cast("str", sprint_plan["plan_fingerprint"]),
        )
        return (
            task["description"] == proposal.description
            and observed_metadata == expected_metadata
        )

    if kind == "story":
        if len(task_values) != len(proposals) or any(
            not matches_proposal(task, metadata, proposal)
            for task, metadata, proposal in zip(
                task_values, task_metadata, proposals, strict=True
            )
        ):
            _invalid_packet_content()
    elif not any(
        matches_proposal(task_values[0], task_metadata[0], proposal)
        for proposal in proposals
    ):
        _invalid_packet_content()

    referenced = set(cast("list[str]", backlog_item["spec_item_ids"]))
    referenced.update(story_spec_item_ids)
    referenced.update(cast("list[str]", validation["referenced_spec_item_ids"]))
    for proposal in proposals:
        referenced.update(proposal.relevant_spec_item_ids)
    if tuple(item.id for item in projected_specification_items) != tuple(
        sorted(referenced)
    ):
        _invalid_packet_content()
    specification_by_id = {item.id: item for item in projected_specification_items}
    planning_reference_sets = (
        tuple(cast("list[str]", backlog_item["spec_item_ids"])),
        tuple(story_spec_item_ids),
        *(proposal.relevant_spec_item_ids for proposal in proposals),
    )
    if any(
        not has_qualifying_normative_evidence(
            specification_by_id[item_id] for item_id in reference_set
        )
        for reference_set in planning_reference_sets
    ):
        _invalid_packet_content()


def validate_canonical_packet(packet: JsonObject) -> JsonObject:
    """Validate one complete closed current packet without coercing its values."""
    schema = packet.get("schema_version")
    kind = packet.get("packet_kind")
    expected = {"story": _STORY_SCHEMA_VERSION, "task": _TASK_SCHEMA_VERSION}
    if not isinstance(kind, str) or expected.get(kind) != schema:
        raise _error(
            "PACKET_SCHEMA_UNSUPPORTED",
            "Packet schema or kind is unsupported.",
        )
    typed_kind = cast("Literal['story', 'task']", kind)
    try:
        _validate_shape(packet, _packet_shape(typed_kind), path="packet")
        _require_packet_hashes(packet)
        metadata = cast("JsonObject", packet["metadata"])
        lineage = cast("JsonObject", packet["lineage"])
        evidence = cast("JsonObject", packet["evidence"])
        work = cast("JsonObject", packet["work"])
        specification = cast("JsonObject", evidence["specification"])
        if specification["currentness"] not in {"current", "superseded"}:
            _invalid_packet_content()
        projected_specification_items = tuple(
            _validate_projected_specification_item(item)
            for item in cast("list[JsonObject]", specification["items"])
        )
        _require_canonical_context_temporals(cast("JsonObject", packet["context"]))
        _require_canonical_sprint_owner_context(
            cast("JsonObject", packet["context"])
        )
        backlog_item = BacklogItem.model_validate(evidence["backlog_item"])
        roadmap_release = RoadmapRelease.model_validate(evidence["roadmap_release"])
        story_item = CanonicalStoryItem.model_validate(evidence["story_item"])
        selected_story = SprintPlannerSelectedStory.model_validate(
            evidence["sprint_plan_story"]
        )
        validation = ValidationEvidence.model_validate(evidence["story_validation"])
        if (
            backlog_item.model_dump(mode="json") != evidence["backlog_item"]
            or roadmap_release.model_dump(mode="json") != evidence["roadmap_release"]
            or story_item.model_dump(mode="json") != evidence["story_item"]
            or selected_story.model_dump(mode="json") != evidence["sprint_plan_story"]
            or validation.model_dump(mode="json") != evidence["story_validation"]
            or validation.model_dump(mode="json")["structural_warnings"]
        ):
            _invalid_packet_content()
        task_values = (
            [cast("JsonObject", work["task"])]
            if typed_kind == "task"
            else cast("list[JsonObject]", work["tasks"])
        )
        task_metadata: list[TaskMetadata] = []
        for task_value in task_values:
            parsed_metadata = TaskMetadata.model_validate(
                task_value["metadata"], strict=False
            )
            if parsed_metadata.model_dump(mode="json") != task_value["metadata"]:
                _invalid_packet_content()
            task_metadata.append(parsed_metadata)
        _require_canonical_agreement(
            kind=typed_kind,
            lineage=lineage,
            context=cast("JsonObject", packet["context"]),
            evidence=evidence,
            work=work,
            selected_story=selected_story,
            task_metadata=tuple(task_metadata),
            projected_specification_items=projected_specification_items,
        )
        source = {
            "lineage": lineage,
            "context": packet["context"],
            "evidence": evidence,
            "work": work,
        }
        identity = canonical_hash({"schema_version": schema, "lineage": lineage})
        expected_packet_id = (
            f"{typed_kind[0]}p_{hashlib.sha256(identity.encode()).hexdigest()[:16]}"
        )
        if (
            metadata["source_fingerprint"] != canonical_hash(source)
            or metadata["packet_id"] != expected_packet_id
            or _PACKET_ID.fullmatch(cast("str", metadata["packet_id"])) is None
            or _SPRINT_PLAN_STREAM_ID.fullmatch(
                cast(
                    "str",
                    cast("JsonObject", lineage["sprint_plan"])["sprint_plan_stream_id"],
                )
            )
            is None
        ):
            _invalid_packet_content()
    except (TypeError, ValueError, ValidationError) as exc:
        raise _error("PACKET_CONTENT_INVALID", "Packet content is invalid.") from exc
    return packet


def _required_id(value: int | None, label: str) -> int:
    if value is None:
        raise _error("PACKET_LINEAGE_INVALID", f"{label} has no durable identity.")
    return value


def _one[RowT](rows: Sequence[RowT], label: str) -> RowT:
    if len(rows) != 1:
        raise _error("PACKET_LINEAGE_INVALID", f"Packet requires exactly one {label}.")
    return rows[0]


def _temporal(value: datetime | date | None) -> str | None:
    if isinstance(value, datetime):
        normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return normalized.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return None if value is None else value.isoformat()


def _accepted_decision[DecisionT](rows: Sequence[DecisionT], label: str) -> DecisionT:
    decision = _one(rows, label)
    if getattr(decision, "decision", None) != "accepted":
        raise _error("PACKET_LINEAGE_INVALID", f"{label} is not accepted.")
    return decision


def _load_validation(
    session: Session,
    story: UserStory,
    *,
    require_current_spec: bool,
) -> ValidationEvidence:
    """Translate the Task 9 deep validation seam into packet error codes."""
    try:
        return require_story_validation_evidence(
            session,
            story=story,
            require_current_spec=require_current_spec,
        )
    except StoryValidationReadinessError as exc:
        raise _error(
            "PACKET_LINEAGE_INVALID",
            "Story validation evidence is absent, invalid, failed, or stale.",
        ) from exc


def _current_sprint_plan(
    session: Session,
    *,
    project_id: int,
    sprint_id: int,
) -> tuple[SprintPlanArtifact, SprintPlanArtifactDecision]:
    """Resolve one accepted Sprint-plan leaf without discarding accepted history."""
    artifacts = tuple(
        session.exec(
            select(SprintPlanArtifact)
            .where(col(SprintPlanArtifact.project_id) == project_id)
            .order_by(col(SprintPlanArtifact.sprint_plan_artifact_id))
        ).all()
    )
    decisions = tuple(
        session.exec(
            select(SprintPlanArtifactDecision)
            .where(col(SprintPlanArtifactDecision.project_id) == project_id)
            .order_by(col(SprintPlanArtifactDecision.sprint_plan_artifact_decision_id))
        ).all()
    )
    artifacts_by_id = {
        artifact.sprint_plan_artifact_id: artifact
        for artifact in artifacts
        if artifact.sprint_plan_artifact_id is not None
    }
    decisions_by_id = {
        decision.sprint_plan_artifact_id: decision for decision in decisions
    }
    if len(artifacts_by_id) != len(artifacts) or len(decisions_by_id) != len(decisions):
        raise _error("PACKET_LINEAGE_INVALID", "Sprint-plan lineage is ambiguous.")
    activated_keys: set[tuple[object, ...]] = set()
    for decision in decisions:
        if decision.decision != "accepted" or decision.activated_sprint_id != sprint_id:
            continue
        artifact = artifacts_by_id.get(decision.sprint_plan_artifact_id)
        if artifact is None or artifact.plan_fingerprint != decision.plan_fingerprint:
            raise _error("PACKET_LINEAGE_INVALID", "Sprint-plan activation is invalid.")
        activated_keys.add(
            (
                artifact.project_id,
                artifact.spec_version_id,
                artifact.spec_hash,
                artifact.sprint_plan_stream_id,
            )
        )
    selected: list[tuple[SprintPlanArtifact, SprintPlanArtifactDecision]] = []
    try:
        for chain_key in activated_keys:
            chain = tuple(
                artifact
                for artifact in artifacts
                if (
                    artifact.project_id,
                    artifact.spec_version_id,
                    artifact.spec_hash,
                    artifact.sprint_plan_stream_id,
                )
                == chain_key
            )
            nodes = tuple(
                ArtifactLineageNode(
                    artifact_id=_required_id(
                        artifact.sprint_plan_artifact_id,
                        "Sprint plan artifact",
                    ),
                    chain_key=chain_key,
                    version_number=artifact.version_number,
                    supersedes_artifact_id=(
                        artifact.supersedes_sprint_plan_artifact_id
                    ),
                    decision=(
                        None
                        if (
                            decision := decisions_by_id.get(
                                _required_id(
                                    artifact.sprint_plan_artifact_id,
                                    "Sprint plan artifact",
                                )
                            )
                        )
                        is None
                        else cast("PlanningLineageDecision", decision.decision)
                    ),
                )
                for artifact in chain
            )
            leaf = select_current_accepted_artifact(nodes, chain_key=chain_key)
            artifact = artifacts_by_id[leaf.artifact_id]
            decision = decisions_by_id.get(leaf.artifact_id)
            if (
                decision is not None
                and decision.decision == "accepted"
                and decision.activated_sprint_id == sprint_id
                and decision.plan_fingerprint == artifact.plan_fingerprint
            ):
                selected.append((artifact, decision))
    except (KeyError, PlanningLineageError) as exc:
        raise _error(
            "PACKET_LINEAGE_INVALID", "Sprint-plan lineage is invalid."
        ) from exc
    return _one(selected, "current accepted Sprint-plan activation")


def _require_historical_execution(
    session: Session,
    *,
    project_id: int,
    sprint_id: int,
    story: UserStory,
    plan: SprintPlanArtifact,
    plan_decision: SprintPlanArtifactDecision,
    tasks: tuple[Task, ...],
) -> None:
    """Require Task 10's complete frozen execution contract for old lineage."""
    try:
        contract = execution_contract(
            WorkflowFactRepository(session).load(project_id),
            sprint_id,
        )
    except (ExecutionIntegrityError, WorkflowFactLoadError) as exc:
        raise _error(
            "PACKET_LINEAGE_INVALID", "Historical execution contract is invalid."
        ) from exc
    story_fact = next(
        (item for item in contract.stories if item.story_id == story.story_id),
        None,
    )
    contract_tasks = tuple(
        item for item in contract.tasks if item.story_id == story.story_id
    )
    exact_story = bool(
        story_fact is not None
        and story_fact.source_story_artifact_id == story.source_story_artifact_id
        and story_fact.source_story_artifact_fingerprint
        == story.source_story_artifact_fingerprint
        and story_fact.source_story_item_id == story.source_story_item_id
        and story_fact.source_story_item_fingerprint
        == story.source_story_item_fingerprint
        and story_fact.accepted_spec_version_id == story.accepted_spec_version_id
        and story_fact.accepted_spec_hash == story.accepted_spec_hash
        and sprint_id in story_fact.sprint_ids
    )
    exact_tasks = tuple(
        (item.task_id, item.story_id, item.description, item.metadata_json)
        for item in contract_tasks
    ) == tuple(
        (
            _required_id(item.task_id, "Task"),
            item.story_id,
            item.description,
            item.metadata_json,
        )
        for item in tasks
    )
    if (
        contract.plan.artifact_id != plan.sprint_plan_artifact_id
        or contract.plan.artifact_fingerprint != plan.plan_fingerprint
        or contract.plan.spec_version_id != plan.spec_version_id
        or contract.plan.spec_hash != plan.spec_hash
        or contract.plan.sprint_plan_stream_id != plan.sprint_plan_stream_id
        or contract.decision.decision_id
        != plan_decision.sprint_plan_artifact_decision_id
        or contract.decision.artifact_id != plan.sprint_plan_artifact_id
        or contract.decision.artifact_fingerprint != plan.plan_fingerprint
        or not exact_story
        or not exact_tasks
    ):
        raise _error(
            "PACKET_LINEAGE_INVALID", "Historical execution contract is invalid."
        )


def _load_context(  # noqa: PLR0915
    session: Session,
    *,
    project_id: int,
    sprint_id: int,
    story_id: int,
    missing_code: Literal[
        "STORY_PACKET_CONTEXT_NOT_FOUND", "TASK_PACKET_CONTEXT_NOT_FOUND"
    ],
) -> _PacketContext:
    project = session.get(Project, project_id)
    if project is None:
        raise _error("PROJECT_NOT_FOUND", f"Project {project_id} was not found.")
    sprint = session.get(Sprint, sprint_id)
    story = session.get(UserStory, story_id)
    membership = session.exec(
        select(SprintStory).where(
            col(SprintStory.sprint_id) == sprint_id,
            col(SprintStory.story_id) == story_id,
        )
    ).one_or_none()
    if (
        sprint is None
        or sprint.project_id != project_id
        or story is None
        or story.project_id != project_id
        or membership is None
    ):
        raise _error(missing_code, "The requested packet context was not found.")

    plan, plan_decision = _current_sprint_plan(
        session,
        project_id=project_id,
        sprint_id=sprint_id,
    )
    if (
        plan.project_id != project_id
        or plan.plan_fingerprint != plan_decision.plan_fingerprint
    ):
        raise _error("PACKET_LINEAGE_INVALID", "Sprint-plan activation is invalid.")
    try:
        envelope = load_bound_sprint_plan_envelope(
            plan.canonical_task_plan_json,
            expected_fingerprint=plan.plan_fingerprint,
            spec_version_id=plan.spec_version_id,
            spec_hash=plan.spec_hash,
            candidate_set_fingerprint=plan.candidate_set_fingerprint,
            selected_story_ids_json=plan.selected_story_ids_json,
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise _error(
            "PACKET_CONTENT_INVALID", "Sprint-plan content is invalid."
        ) from exc
    try:
        owner = load_sprint_owner_evidence(
            session,
            artifact=plan,
            owner_label=envelope.team_name,
        )
    except SprintOwnerEvidenceError as exc:
        raise _error(
            "PACKET_CONTENT_INVALID", "Sprint owner evidence is invalid."
        ) from exc
    team = session.get(Team, sprint.team_id)
    if team is None or team.name != owner.label:
        raise _error(
            "PACKET_LINEAGE_INVALID",
            "Activated Sprint owner projection is invalid.",
        )
    selected_story = _one(
        tuple(
            item
            for item in envelope.planner_output.selected_stories
            if item.story_id == story_id
        ),
        "selected Sprint-plan Story",
    )
    if selected_story.story_item_id != story.source_story_item_id:
        raise _error("PACKET_LINEAGE_INVALID", "Sprint-plan Story identity is invalid.")

    try:
        specification = load_accepted_specification(
            session,
            project_id=project_id,
            spec_version_id=plan.spec_version_id,
            spec_hash=plan.spec_hash,
        )
    except AcceptedSpecificationIntegrityError as exc:
        code = (
            "PACKET_CONTENT_INVALID"
            if "CANONICAL_BYTES" in exc.code
            else "PACKET_LINEAGE_INVALID"
        )
        raise _error(code, "Accepted Specification source is invalid.") from exc
    if (
        story.accepted_spec_version_id != specification.spec_version_id
        or story.accepted_spec_hash != specification.spec_hash
    ):
        raise _error(
            "PACKET_LINEAGE_INVALID",
            "Story and Sprint plan use different Specifications.",
        )
    story_artifact = session.get(StoryArtifact, story.source_story_artifact_id)
    if (
        story_artifact is None
        or story_artifact.project_id != project_id
        or story_artifact.content_fingerprint != story.source_story_artifact_fingerprint
    ):
        raise _error("PACKET_LINEAGE_INVALID", "Story artifact lineage is invalid.")
    _accepted_decision(
        session.exec(
            select(StoryArtifactDecision).where(
                col(StoryArtifactDecision.project_id) == project_id,
                col(StoryArtifactDecision.story_artifact_id)
                == story_artifact.story_artifact_id,
                col(StoryArtifactDecision.artifact_fingerprint)
                == story_artifact.content_fingerprint,
            )
        ).all(),
        "Story artifact decision",
    )
    backlog = session.get(BacklogArtifact, story_artifact.source_backlog_artifact_id)
    roadmap = session.get(RoadmapArtifact, story_artifact.roadmap_artifact_id)
    if (
        backlog is None
        or roadmap is None
        or backlog.project_id != project_id
        or roadmap.project_id != project_id
        or backlog.content_fingerprint
        != story_artifact.source_backlog_artifact_fingerprint
        or roadmap.content_fingerprint != story_artifact.roadmap_artifact_fingerprint
        or roadmap.backlog_artifact_id != backlog.backlog_artifact_id
        or roadmap.backlog_artifact_fingerprint != backlog.content_fingerprint
        or backlog.spec_version_id != specification.spec_version_id
        or backlog.spec_hash != specification.spec_hash
    ):
        raise _error("PACKET_LINEAGE_INVALID", "Planning parent lineage is invalid.")
    _accepted_decision(
        session.exec(
            select(BacklogArtifactDecision).where(
                col(BacklogArtifactDecision.project_id) == project_id,
                col(BacklogArtifactDecision.backlog_artifact_id)
                == backlog.backlog_artifact_id,
                col(BacklogArtifactDecision.artifact_fingerprint)
                == backlog.content_fingerprint,
            )
        ).all(),
        "Backlog artifact decision",
    )
    _accepted_decision(
        session.exec(
            select(RoadmapArtifactDecision).where(
                col(RoadmapArtifactDecision.project_id) == project_id,
                col(RoadmapArtifactDecision.roadmap_artifact_id)
                == roadmap.roadmap_artifact_id,
                col(RoadmapArtifactDecision.artifact_fingerprint)
                == roadmap.content_fingerprint,
            )
        ).all(),
        "Roadmap artifact decision",
    )
    try:
        _, backlog_content = load_stored_backlog_planning_content(
            backlog.canonical_content_json,
            expected_fingerprint=backlog.content_fingerprint,
            specification=specification,
        )
        backlog_item = _one(
            tuple(
                item
                for item in backlog_content.backlog_items
                if item.backlog_item_id == story_artifact.backlog_item_id
            ),
            "Backlog item",
        )
        _, roadmap_content = load_stored_roadmap_planning_content(
            roadmap.canonical_content_json,
            expected_fingerprint=roadmap.content_fingerprint,
            parent_backlog_item_ids=tuple(
                item.backlog_item_id for item in backlog_content.backlog_items
            ),
        )
        roadmap_release = _one(
            tuple(
                release
                for release in roadmap_content.roadmap_releases
                if backlog_item.backlog_item_id in release.backlog_item_ids
            ),
            "Roadmap release",
        )
        _, story_content = load_stored_story_planning_content(
            story_artifact.canonical_content_json,
            expected_fingerprint=story_artifact.content_fingerprint,
            specification=specification,
            backlog_item=backlog_item,
        )
        story_envelope = _one(
            tuple(
                item
                for item in story_content.story_items
                if item.item.story_item_id == story.source_story_item_id
            ),
            "Story item",
        )
    except CanonicalPacketError:
        raise
    except (TypeError, ValueError, ValidationError) as exc:
        raise _error(
            "PACKET_CONTENT_INVALID", "Immutable planning content is invalid."
        ) from exc
    story_item = story_envelope.item
    if (
        story.source_story_item_fingerprint != story_envelope.item_fingerprint
        or selected_story.story_item_id != story_item.story_item_id
        or story.title != story_item.story_title
        or story.story_description != story_item.statement
        or story.persona != story_item.persona
        or story.acceptance_criteria_json
        != canonical_json(list(story_item.acceptance_criteria))
        or story.spec_item_ids_json != canonical_json(list(story_item.spec_item_ids))
    ):
        raise _error(
            "PACKET_LINEAGE_INVALID", "Operational Story differs from accepted content."
        )

    materialized = tuple(
        session.exec(select(Task).where(col(Task.story_id) == story_id)).all()
    )
    if len(materialized) != len(selected_story.tasks):
        raise _error(
            "PACKET_LINEAGE_INVALID", "Materialized Tasks differ from the Sprint plan."
        )
    tasks = tuple(sorted(materialized, key=lambda item: item.task_id or 0))
    metadata: list[TaskMetadata] = []
    for task, proposed in zip(tasks, selected_story.tasks, strict=True):
        try:
            observed = parse_task_metadata(task.metadata_json)
        except ValueError as exc:
            raise _error("TASK_METADATA_INVALID", "Task metadata is invalid.") from exc
        expected = metadata_from_structured_task(
            proposed,
            spec_version_id=plan.spec_version_id,
            spec_hash=plan.spec_hash,
            sprint_plan_stream_id=plan.sprint_plan_stream_id,
            sprint_plan_artifact_id=_required_id(
                plan.sprint_plan_artifact_id, "Sprint plan artifact"
            ),
            sprint_plan_fingerprint=plan.plan_fingerprint,
        )
        if task.description != proposed.description or observed != expected:
            raise _error(
                "TASK_METADATA_INVALID", "Task metadata differs from the Sprint plan."
            )
        metadata.append(observed)
    if specification.status == "superseded":
        _require_historical_execution(
            session,
            project_id=project_id,
            sprint_id=sprint_id,
            story=story,
            plan=plan,
            plan_decision=plan_decision,
            tasks=tasks,
        )
    return _PacketContext(
        project=project,
        sprint=sprint,
        story=story,
        owner_kind=owner.kind,
        owner_key=owner.key,
        owner_label=owner.label,
        specification=specification,
        backlog=backlog,
        backlog_item=backlog_item,
        roadmap=roadmap,
        roadmap_release=roadmap_release,
        story_artifact=story_artifact,
        story_item=story_item,
        plan=plan,
        selected_story=selected_story,
        tasks=tasks,
        task_metadata=tuple(metadata),
        validation=_load_validation(
            session,
            story,
            require_current_spec=specification.status == "approved",
        ),
    )


def _spec_item(item: SpecificationItem) -> JsonObject:
    verification = item.verification
    level = item.level
    return _JSON_OBJECT.validate_python(
        {
            "spec_item_id": item.id,
            "title": item.title,
            "statement": item.statement,
            "level": None if level is None else level.value,
            "acceptance_criteria": list(item.acceptance),
            "verification_method": (
                None if verification is None else verification.value
            ),
        }
    )


def _lineage(context: _PacketContext, task: Task | None) -> JsonObject:
    value: JsonObject = {
        "specification": {
            "spec_version_id": context.specification.spec_version_id,
            "spec_hash": context.specification.spec_hash,
        },
        "backlog": {
            "backlog_artifact_id": context.backlog.backlog_artifact_id,
            "artifact_fingerprint": context.backlog.content_fingerprint,
            "backlog_item_id": context.backlog_item.backlog_item_id,
        },
        "roadmap": {
            "roadmap_artifact_id": context.roadmap.roadmap_artifact_id,
            "artifact_fingerprint": context.roadmap.content_fingerprint,
        },
        "story": {
            "story_id": context.story.story_id,
            "story_artifact_id": context.story_artifact.story_artifact_id,
            "artifact_fingerprint": context.story_artifact.content_fingerprint,
            "story_item_id": context.story_item.story_item_id,
        },
        "sprint_plan": {
            "sprint_plan_stream_id": context.plan.sprint_plan_stream_id,
            "sprint_plan_artifact_id": context.plan.sprint_plan_artifact_id,
            "plan_fingerprint": context.plan.plan_fingerprint,
        },
        "sprint": {"sprint_id": context.sprint.sprint_id},
    }
    if task is not None:
        value["task"] = {"task_id": task.task_id}
    return value


def _snapshot_context(context: _PacketContext) -> JsonObject:
    return _JSON_OBJECT.validate_python(
        {
            "project": {
                "project_id": context.project.project_id,
                "name": context.project.name,
            },
            "sprint": {
                "goal": context.sprint.goal,
                "status": context.sprint.status.value,
                "team_name": context.owner_label,
                "owner_kind": context.owner_kind,
                "owner_key": context.owner_key,
                "started_at": _temporal(context.sprint.started_at),
                "start_date": _temporal(context.sprint.start_date),
                "end_date": _temporal(context.sprint.end_date),
            },
        }
    )


def _evidence(context: _PacketContext) -> JsonObject:
    referenced = set(context.backlog_item.spec_item_ids) | set(
        context.story_item.spec_item_ids
    )
    for metadata in context.task_metadata:
        referenced.update(metadata.relevant_spec_item_ids)
    items = [
        _spec_item(item)
        for item in context.specification.payload.items
        if item.id in referenced
    ]
    if {item["spec_item_id"] for item in items} != referenced:
        raise _error("PACKET_CONTENT_INVALID", "Specification evidence is incomplete.")
    return _JSON_OBJECT.validate_python(
        {
            "specification": {
                "currentness": (
                    "current"
                    if context.specification.status == "approved"
                    else "superseded"
                ),
                "items": items,
            },
            "backlog_item": context.backlog_item.model_dump(mode="json"),
            "roadmap_release": context.roadmap_release.model_dump(mode="json"),
            "story_item": context.story_item.model_dump(mode="json"),
            "sprint_plan_story": context.selected_story.model_dump(mode="json"),
            "story_validation": context.validation.model_dump(mode="json"),
        }
    )


def _story_value(context: _PacketContext) -> JsonObject:
    return {
        "title": context.story_item.story_title,
        "statement": context.story_item.statement,
        "persona": context.story_item.persona,
        "acceptance_criteria": list(context.story_item.acceptance_criteria),
        "status": context.story.status.value,
        "story_points": context.story.story_points,
        "rank": context.story.rank,
    }


def _story_work(context: _PacketContext) -> JsonObject:
    return {
        "story": _story_value(context),
        "tasks": [
            {
                "description": task.description,
                "status": task.status.value,
                "metadata": metadata.model_dump(mode="json"),
            }
            for task, metadata in zip(context.tasks, context.task_metadata, strict=True)
        ],
    }


def _task_work(
    session: Session, context: _PacketContext, task: Task, metadata: TaskMetadata
) -> JsonObject:
    assignee = (
        None
        if task.assigned_to_member_id is None
        else session.get(TeamMember, task.assigned_to_member_id)
    )
    return {
        "story": _story_value(context),
        "task": {
            "description": task.description,
            "status": task.status.value,
            "assignee_name": None if assignee is None else assignee.name,
            "metadata": metadata.model_dump(mode="json"),
        },
    }


def _packet(
    *,
    schema_version: str,
    packet_kind: Literal["story", "task"],
    lineage: JsonObject,
    context: JsonObject,
    evidence: JsonObject,
    work: JsonObject,
) -> JsonObject:
    source: JsonObject = {
        "lineage": lineage,
        "context": context,
        "evidence": evidence,
        "work": work,
    }
    identity = canonical_hash({"schema_version": schema_version, "lineage": lineage})
    packet_id = (
        f"{packet_kind[0]}p_{hashlib.sha256(identity.encode()).hexdigest()[:16]}"
    )
    packet: JsonObject = {
        "schema_version": schema_version,
        "packet_kind": packet_kind,
        "metadata": {
            "packet_id": packet_id,
            "source_fingerprint": canonical_hash(source),
        },
        **source,
    }
    return validate_canonical_packet(packet)


def build_story_packet(
    session: Session, *, project_id: int, sprint_id: int, story_id: int
) -> JsonObject:
    """Build one deterministic story_packet.v3 from accepted plan activation."""
    context = _load_context(
        session,
        project_id=project_id,
        sprint_id=sprint_id,
        story_id=story_id,
        missing_code="STORY_PACKET_CONTEXT_NOT_FOUND",
    )
    return _packet(
        schema_version=_STORY_SCHEMA_VERSION,
        packet_kind="story",
        lineage=_lineage(context, None),
        context=_snapshot_context(context),
        evidence=_evidence(context),
        work=_story_work(context),
    )


def build_task_packet(
    session: Session, *, project_id: int, sprint_id: int, task_id: int
) -> JsonObject:
    """Build one deterministic task_packet.v4 from accepted plan activation."""
    if session.get(Project, project_id) is None:
        raise _error("PROJECT_NOT_FOUND", f"Project {project_id} was not found.")
    task = session.get(Task, task_id)
    if task is None:
        raise _error(
            "TASK_PACKET_CONTEXT_NOT_FOUND", "Task packet context was not found."
        )
    context = _load_context(
        session,
        project_id=project_id,
        sprint_id=sprint_id,
        story_id=task.story_id,
        missing_code="TASK_PACKET_CONTEXT_NOT_FOUND",
    )
    try:
        index = next(
            index for index, item in enumerate(context.tasks) if item.task_id == task_id
        )
    except StopIteration as exc:
        raise _error(
            "TASK_PACKET_CONTEXT_NOT_FOUND", "Task packet context was not found."
        ) from exc
    return _packet(
        schema_version=_TASK_SCHEMA_VERSION,
        packet_kind="task",
        lineage=_lineage(context, task),
        context=_snapshot_context(context),
        evidence=_evidence(context),
        work=_task_work(session, context, task, context.task_metadata[index]),
    )


__all__ = [
    "CanonicalPacketError",
    "build_story_packet",
    "build_task_packet",
    "validate_canonical_packet",
]
