"""Pure canonical packet projections over current durable Project records."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

from pydantic import TypeAdapter, ValidationError
from sqlmodel import Session, col, select

from models.core import (
    Project,
    Sprint,
    SprintStory,
    Task,
    Team,
    TeamMember,
    UserStory,
)
from models.specs import CompiledSpecAuthority, SpecRegistry
from services.specs.authority_selection import (
    accepted_compiled_authority,
    latest_accepted_authority_decision,
)
from services.specs.compiler_service import load_compiled_artifact
from services.specs.story_validation_service import compute_story_input_hash
from services.vision_projection import (
    VisionLineageError,
    load_current_accepted_vision,
)
from utils.spec_schemas import SpecAuthorityCompilationSuccess, ValidationEvidence
from utils.task_metadata import TaskMetadata, hash_task_metadata, parse_task_metadata
from workflow.contracts import JsonObject, JsonValue

logger: logging.Logger = logging.getLogger(name=__name__)

_JSON_OBJECT = TypeAdapter(JsonObject)
_TASK_SCHEMA_VERSION = "task_packet.v2"
_STORY_SCHEMA_VERSION = "story_packet.v1"


class CanonicalPacketError(RuntimeError):
    """Typed packet projection failure for transport translation."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: JsonObject,
    ) -> None:
        """Store a transport-safe failure code and structured details."""
        super().__init__(message)
        self.code = code
        self.details = details


@dataclass(frozen=True)
class _AuthorityContext:
    authority: CompiledSpecAuthority | None
    artifact: SpecAuthorityCompilationSuccess | None


@dataclass(frozen=True)
class _StoryPacketContext:
    project: Project
    sprint: Sprint
    sprint_story: SprintStory
    story: UserStory
    team: Team | None
    evidence: ValidationEvidence | None
    current_story_input_hash: str
    validation_input_hash: str | None
    input_hash_matches: bool | None
    validation_freshness: str
    authority: CompiledSpecAuthority | None
    artifact: SpecAuthorityCompilationSuccess | None


def _object(value: object) -> JsonObject:
    return _JSON_OBJECT.validate_python(value)


def _error(
    code: str,
    message: str,
    **details: JsonValue,
) -> CanonicalPacketError:
    return CanonicalPacketError(code, message, details=details)


def _serialize_temporal(value: datetime | date | None) -> str | None:
    if isinstance(value, datetime):
        normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return normalized.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return value.isoformat() if isinstance(value, date) else None


def _hash_payload(payload: JsonValue) -> str:
    serialized = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _truncate_text(text: str, max_length: int) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= max_length:
        return normalized
    return f"{normalized[: max_length - 3].rstrip()}..."


def _task_label(description: str) -> str:
    return _truncate_text(description or "Task", 80) or "Task"


def _vision_excerpt(vision: str | None) -> str | None:
    if vision is None or not vision.strip():
        return None
    for paragraph in re.split(r"\n\s*\n", vision.strip()):
        normalized = " ".join(paragraph.split())
        if normalized:
            return _truncate_text(normalized, 500)
    return None


def _acceptance_criteria_items(text: str | None) -> list[str]:
    if text is None or not text.strip():
        return []
    items: list[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        normalized = re.sub(r"^\s*(?:[-*]+|\d+[.)])\s*", "", stripped).strip()
        if normalized:
            items.append(normalized)
    if items:
        return items
    collapsed = " ".join(text.split())
    return [collapsed] if collapsed else []


def _validation_evidence(raw_value: str | None) -> ValidationEvidence | None:
    if raw_value is None:
        return None
    try:
        return ValidationEvidence.model_validate_json(raw_value)
    except ValidationError as error:
        logger.warning("Ignoring invalid persisted validation evidence: %s", error)
        return None


def _validation_evidence_hash(evidence: ValidationEvidence | None) -> str | None:
    """Hash the complete normalized persisted validation evidence."""
    if evidence is None:
        return None
    return _hash_payload(_object(evidence.model_dump(mode="json")))


def _authority_context(
    session: Session,
    *,
    project_id: int,
    spec_version_id: int | None,
) -> _AuthorityContext:
    if spec_version_id is None:
        return _AuthorityContext(authority=None, artifact=None)

    spec = session.get(SpecRegistry, spec_version_id)
    if spec is None or spec.project_id != project_id:
        error_code = "SPEC_VERSION_NOT_FOUND"
        message = "The Story-pinned specification does not belong to this Project."
        raise _error(
            error_code,
            message,
            project_id=project_id,
            spec_version_id=spec_version_id,
        )
    acceptance = latest_accepted_authority_decision(
        session,
        project_id=project_id,
        spec_version_id=spec_version_id,
    )
    if acceptance is None:
        error_code = "AUTHORITY_NOT_ACCEPTED"
        message = "The Story-pinned specification has no accepted authority."
        raise _error(
            error_code,
            message,
            project_id=project_id,
            spec_version_id=spec_version_id,
        )
    authority = accepted_compiled_authority(
        session,
        project_id=project_id,
        spec_version_id=spec_version_id,
    )
    if authority is None:
        error_code = "AUTHORITY_ACCEPTANCE_MISMATCH"
        message = "The accepted authority no longer matches its pinned provenance."
        raise _error(
            error_code,
            message,
            project_id=project_id,
            spec_version_id=spec_version_id,
            accepted_authority_id=acceptance.pending_authority_id,
        )
    load_result = load_compiled_artifact(authority)
    artifact = load_result.artifact
    if not load_result.ok or artifact is None:
        error_code = "COMPILED_AUTHORITY_INVALID"
        message = "The accepted compiled authority artifact is unavailable or invalid."
        raise _error(
            error_code,
            message,
            project_id=project_id,
            spec_version_id=spec_version_id,
            authority_id=authority.authority_id,
            load_status=load_result.status,
            observed_schema_version=load_result.observed_schema_version,
        )
    return _AuthorityContext(authority=authority, artifact=artifact)


def _story_context(
    session: Session,
    *,
    project_id: int,
    sprint_id: int,
    story_id: int,
    missing_code: str,
) -> _StoryPacketContext:
    project = session.get(Project, project_id)
    if project is None:
        error_code = "PROJECT_NOT_FOUND"
        message = f"Project {project_id} was not found."
        raise _error(
            error_code,
            message,
            project_id=project_id,
        )
    story = session.get(UserStory, story_id)
    if story is None or story.project_id != project_id:
        error_code = "STORY_NOT_FOUND"
        message = f"Story {story_id} was not found for Project {project_id}."
        raise _error(
            error_code,
            message,
            project_id=project_id,
            story_id=story_id,
        )
    sprint = session.get(Sprint, sprint_id)
    if sprint is None or sprint.project_id != project_id:
        message = "The requested packet context is not linked to this Project Sprint."
        raise _error(
            missing_code,
            message,
            project_id=project_id,
            sprint_id=sprint_id,
            story_id=story_id,
        )
    sprint_story = session.exec(
        select(SprintStory).where(
            col(SprintStory.sprint_id) == sprint_id,
            col(SprintStory.story_id) == story_id,
        )
    ).first()
    if sprint_story is None:
        message = "The requested Story is not linked to this Sprint."
        raise _error(
            missing_code,
            message,
            project_id=project_id,
            sprint_id=sprint_id,
            story_id=story_id,
        )

    evidence = _validation_evidence(story.validation_evidence)
    current_story_input_hash = compute_story_input_hash(story)
    validation_input_hash = evidence.input_hash if evidence is not None else None
    input_hash_matches = (
        current_story_input_hash == validation_input_hash
        if validation_input_hash is not None
        else None
    )
    evidence_matches_pin = (
        evidence is not None
        and evidence.spec_version_id == story.accepted_spec_version_id
    )
    validation_freshness = (
        "missing"
        if evidence is None
        else "current"
        if input_hash_matches and evidence_matches_pin
        else "stale"
    )
    authority = _authority_context(
        session,
        project_id=project_id,
        spec_version_id=story.accepted_spec_version_id,
    )
    return _StoryPacketContext(
        project=project,
        sprint=sprint,
        sprint_story=sprint_story,
        story=story,
        team=session.get(Team, sprint.team_id),
        evidence=evidence,
        current_story_input_hash=current_story_input_hash,
        validation_input_hash=validation_input_hash,
        input_hash_matches=input_hash_matches,
        validation_freshness=validation_freshness,
        authority=authority.authority,
        artifact=authority.artifact,
    )


def _story_payload(story: UserStory) -> JsonObject:
    return {
        "story_id": story.story_id,
        "title": story.title,
        "persona": story.persona,
        "story_description": story.story_description,
        "status": story.status.value,
        "story_points": story.story_points,
        "rank": story.rank,
        "source_requirement": story.source_requirement,
    }


def _sprint_payload(context: _StoryPacketContext) -> JsonObject:
    sprint = context.sprint
    return {
        "sprint_id": sprint.sprint_id,
        "goal": sprint.goal,
        "status": sprint.status.value,
        "started_at": _serialize_temporal(sprint.started_at),
        "start_date": _serialize_temporal(sprint.start_date),
        "end_date": _serialize_temporal(sprint.end_date),
        "team_id": sprint.team_id,
        "team_name": context.team.name if context.team is not None else None,
    }


def _project_payload(session: Session, project: Project) -> JsonObject:
    project_id = project.project_id
    if project_id is None:
        error_code = "VISION_LINEAGE_INVALID"
        message = "Project Vision lineage requires a durable Project identity."
        raise _error(
            error_code,
            message,
        )
    try:
        vision = load_current_accepted_vision(session, project_id=project_id)
    except VisionLineageError as error:
        error_code = "VISION_LINEAGE_INVALID"
        message = "Project Vision lineage is invalid."
        raise _error(
            error_code,
            message,
            project_id=project_id,
        ) from error
    return {
        "project_id": project_id,
        "name": project.name,
        "vision_excerpt": (
            None if vision is None else _vision_excerpt(vision.statement)
        ),
    }


def _task_payload(
    task: Task,
    *,
    metadata: TaskMetadata,
    assignee: TeamMember | None,
) -> JsonObject:
    return {
        "task_id": task.task_id,
        "label": _task_label(task.description),
        "description": task.description,
        "status": task.status.value,
        "assignee_member_id": task.assigned_to_member_id,
        "assignee_name": assignee.name if assignee is not None else None,
        "task_kind": metadata.task_kind,
        "artifact_targets": list(metadata.artifact_targets),
        "workstream_tags": list(metadata.workstream_tags),
        "checklist_items": list(metadata.checklist_items),
        "is_executable": bool(metadata.checklist_items),
    }


def _task_plan(session: Session, *, story_id: int) -> list[JsonValue]:
    tasks = session.exec(select(Task).where(col(Task.story_id) == story_id)).all()
    serialized: list[JsonValue] = []
    for task in tasks:
        metadata = parse_task_metadata(
            task.metadata_json,
            logger=logger,
            task_id=task.task_id,
        )
        serialized.append(
            {
                "id": task.task_id,
                "description": task.description,
                "status": task.status.value,
                "task_kind": metadata.task_kind,
                "artifact_targets": list(metadata.artifact_targets),
                "workstream_tags": list(metadata.workstream_tags),
                "checklist_items": list(metadata.checklist_items),
                "is_executable": bool(metadata.checklist_items),
            }
        )
    return sorted(
        serialized,
        key=lambda item: (
            str(item.get("description", "")).lower() if isinstance(item, dict) else "",
            int(item.get("id", 0))
            if isinstance(item, dict) and isinstance(item.get("id"), int)
            else 0,
        ),
    )


def _findings(evidence: ValidationEvidence | None) -> list[JsonValue]:
    if evidence is None:
        return []
    findings: list[JsonValue] = [
        {
            "severity": "failure",
            "source": "validation_failure",
            "code": failure.rule,
            "message": failure.message,
            "invariant_id": None,
            "rule": failure.rule,
            "capability": None,
        }
        for failure in evidence.failures
    ]
    findings.extend(
        {
            "severity": "warning",
            "source": "validation_warning",
            "code": warning,
            "message": warning,
            "invariant_id": None,
            "rule": None,
            "capability": None,
        }
        for warning in evidence.warnings
    )
    findings.extend(
        {
            "severity": finding.severity,
            "source": "alignment_warning",
            "code": finding.code,
            "message": finding.message,
            "invariant_id": finding.invariant,
            "rule": None,
            "capability": finding.capability,
        }
        for finding in evidence.alignment_warnings
    )
    findings.extend(
        {
            "severity": finding.severity,
            "source": "alignment_failure",
            "code": finding.code,
            "message": finding.message,
            "invariant_id": finding.invariant,
            "rule": None,
            "capability": finding.capability,
        }
        for finding in evidence.alignment_failures
    )
    return findings


def _constraints_for_ids(
    artifact: SpecAuthorityCompilationSuccess | None,
    invariant_ids: list[str],
) -> list[JsonValue]:
    if artifact is None or not invariant_ids:
        return []
    invariant_by_id = {item.id: item for item in artifact.invariants}
    source_by_id = {item.invariant_id: item for item in artifact.source_map}
    constraints: list[JsonValue] = []
    for invariant_id in invariant_ids:
        invariant = invariant_by_id.get(invariant_id)
        if invariant is None:
            logger.warning(
                "Ignoring unknown invariant id %r while building a packet.",
                invariant_id,
            )
            continue
        source = source_by_id.get(invariant_id)
        constraints.append(
            {
                "invariant_id": invariant.id,
                "type": invariant.type.value,
                "parameters": _object(invariant.parameters.model_dump(mode="json")),
                "source_excerpt": source.excerpt if source is not None else None,
                "source_location": source.location if source is not None else None,
            }
        )
    return constraints


def _validation_payload(context: _StoryPacketContext) -> JsonObject:
    evidence = context.evidence
    return {
        "present": evidence is not None,
        "passed": evidence.passed if evidence is not None else None,
        "freshness_status": context.validation_freshness,
        "validated_at": _serialize_temporal(
            evidence.validated_at if evidence is not None else None
        ),
        "validator_version": evidence.validator_version
        if evidence is not None
        else None,
        "current_story_input_hash": context.current_story_input_hash,
        "validation_input_hash": context.validation_input_hash,
        "input_hash_matches": context.input_hash_matches,
        "rules_checked": list(evidence.rules_checked) if evidence is not None else [],
    }


def _spec_binding(context: _StoryPacketContext) -> JsonObject:
    spec_version_id = context.story.accepted_spec_version_id
    return {
        "mode": "pinned_story_authority",
        "binding_status": "pinned" if spec_version_id is not None else "unpinned",
        "spec_version_id": spec_version_id,
        "authority_artifact_status": (
            "available" if context.artifact is not None else "missing"
        ),
    }


def _story_boundaries(context: _StoryPacketContext) -> list[JsonValue]:
    evidence = context.evidence
    invariant_ids = list(evidence.finding_invariant_ids) if evidence is not None else []
    return _constraints_for_ids(context.artifact, invariant_ids)


def _base_snapshot(context: _StoryPacketContext) -> JsonObject:
    evidence = context.evidence
    authority = context.authority
    return {
        "project_id": context.project.project_id,
        "sprint_id": context.sprint.sprint_id,
        "story_id": context.story.story_id,
        "project_updated_at": _serialize_temporal(context.project.updated_at),
        "sprint_updated_at": _serialize_temporal(context.sprint.updated_at),
        "sprint_story_added_at": _serialize_temporal(context.sprint_story.added_at),
        "story_updated_at": _serialize_temporal(context.story.updated_at),
        "story_ac_updated_at": _serialize_temporal(context.story.ac_updated_at),
        "accepted_spec_version_id": context.story.accepted_spec_version_id,
        "validation_validated_at": _serialize_temporal(
            evidence.validated_at if evidence is not None else None
        ),
        "validation_input_hash": context.validation_input_hash,
        "validation_evidence_hash": _validation_evidence_hash(evidence),
        "compiled_authority_compiled_at": _serialize_temporal(
            authority.compiled_at if authority is not None else None
        ),
        "compiled_authority_id": (
            authority.authority_id if authority is not None else None
        ),
    }


def build_task_packet(
    session: Session,
    *,
    project_id: int,
    sprint_id: int,
    task_id: int,
) -> JsonObject:
    """Build task_packet.v2 from one exact current durable context."""
    if session.get(Project, project_id) is None:
        error_code = "PROJECT_NOT_FOUND"
        message = f"Project {project_id} was not found."
        raise _error(
            error_code,
            message,
            project_id=project_id,
        )
    task = session.get(Task, task_id)
    if task is None:
        error_code = "TASK_PACKET_CONTEXT_NOT_FOUND"
        message = "Task packet context not found."
        raise _error(
            error_code,
            message,
            project_id=project_id,
            sprint_id=sprint_id,
            task_id=task_id,
        )
    context = _story_context(
        session,
        project_id=project_id,
        sprint_id=sprint_id,
        story_id=task.story_id,
        missing_code="TASK_PACKET_CONTEXT_NOT_FOUND",
    )
    metadata = parse_task_metadata(
        task.metadata_json,
        logger=logger,
        task_id=task.task_id,
    )
    assignee = (
        session.get(TeamMember, task.assigned_to_member_id)
        if task.assigned_to_member_id is not None
        else None
    )
    source_snapshot = _base_snapshot(context)
    source_snapshot.update(
        {
            "task_id": task_id,
            "task_updated_at": _serialize_temporal(task.updated_at),
            "task_metadata_hash": hash_task_metadata(metadata),
        }
    )
    packet_id_hash = hashlib.sha256(
        f"{_TASK_SCHEMA_VERSION}:{sprint_id}:{task_id}".encode()
    ).hexdigest()[:16]
    constraints: JsonObject = {
        "spec_binding": _spec_binding(context),
        "validation": _validation_payload(context),
        "task_hard_constraints": _constraints_for_ids(
            context.artifact,
            list(metadata.relevant_invariant_ids),
        ),
        "story_compliance_boundaries": _story_boundaries(context),
        "findings": _findings(context.evidence),
    }
    return {
        "schema_version": _TASK_SCHEMA_VERSION,
        "metadata": {
            "packet_id": f"tp_{packet_id_hash}",
            "generated_at": _serialize_temporal(datetime.now(UTC)),
            "generator_version": "v2",
            "source_fingerprint": _hash_payload(source_snapshot),
        },
        "source_snapshot": source_snapshot,
        "task": _task_payload(task, metadata=metadata, assignee=assignee),
        "context": {
            "story": _story_payload(context.story),
            "sprint": _sprint_payload(context),
            "project": _project_payload(session, context.project),
        },
        "constraints": constraints,
    }


def build_story_packet(
    session: Session,
    *,
    project_id: int,
    sprint_id: int,
    story_id: int,
) -> JsonObject:
    """Build story_packet.v1 from one exact current durable context."""
    context = _story_context(
        session,
        project_id=project_id,
        sprint_id=sprint_id,
        story_id=story_id,
        missing_code="STORY_PACKET_CONTEXT_NOT_FOUND",
    )
    task_plan = _task_plan(session, story_id=story_id)
    source_snapshot = _base_snapshot(context)
    source_snapshot["task_plan_hash"] = _hash_payload(task_plan)
    packet_id_hash = hashlib.sha256(
        f"{_STORY_SCHEMA_VERSION}:{sprint_id}:{story_id}".encode()
    ).hexdigest()[:16]
    constraints = _object(
        {
            "story_acceptance_criteria_text": context.story.acceptance_criteria,
            "story_acceptance_criteria_items": _acceptance_criteria_items(
                context.story.acceptance_criteria
            ),
            "spec_binding": _spec_binding(context),
            "validation": _validation_payload(context),
            "story_compliance_boundaries": _story_boundaries(context),
            "findings": _findings(context.evidence),
        }
    )
    return {
        "schema_version": _STORY_SCHEMA_VERSION,
        "metadata": {
            "packet_id": f"sp_{packet_id_hash}",
            "generated_at": _serialize_temporal(datetime.now(UTC)),
            "generator_version": "v1",
            "source_fingerprint": _hash_payload(source_snapshot),
        },
        "source_snapshot": source_snapshot,
        "story": _story_payload(context.story),
        "task_plan": {"tasks": task_plan},
        "context": {
            "sprint": _sprint_payload(context),
            "project": _project_payload(session, context.project),
        },
        "constraints": constraints,
    }


__all__ = [
    "CanonicalPacketError",
    "build_story_packet",
    "build_task_packet",
]
