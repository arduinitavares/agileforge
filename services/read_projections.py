"""Durable non-routing projections for production transports."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from pydantic import TypeAdapter, ValidationError
from sqlmodel import Session, col, select

from models.core import Project, Sprint, Team, UserStory
from models.events import TaskExecutionLog
from models.product_definition import (
    ProductGoalArtifact,
    ProductGoalArtifactDecision,
    SpecificationCandidate,
    SpecificationDecision,
)
from models.repository import RepositoryBinding, repository_binding_fingerprint
from models.specs import SpecRegistry
from models.workflow import (
    BacklogArtifact,
    BacklogArtifactDecision,
    RoadmapArtifact,
    RoadmapArtifactDecision,
    SprintPlanArtifact,
    SprintPlanArtifactDecision,
    StoryArtifact,
    StoryArtifactDecision,
    WorkflowNodeAttempt,
    WorkflowNodeAttemptOutcome,
)
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from services.contracts.backlog import BacklogOutput
from services.contracts.specification_source import (
    SpecificationSourceBundle,
    SpecificationSourceDocument,
    source_bundle_fingerprint,
)
from services.contracts.story import STORY_POINTS_BY_EFFORT
from services.packet_renderer import PacketRenderError, render_packet
from services.phases.sprint_metrics import (
    build_durable_sprint_metrics,
    build_sprint_capacity_state,
)
from services.planning_artifact_content import (
    load_bound_sprint_plan_envelope,
    load_stored_backlog_planning_content,
    load_stored_planning_artifact_content,
    load_stored_roadmap_planning_content,
)
from services.planning_lineage import (
    ArtifactLineageNode,
    PlanningLineageCode,
    PlanningLineageError,
    select_current_accepted_artifact,
    select_physical_leaf,
    validate_artifact_lineage,
)
from services.planning_lineage import Decision as PlanningLineageDecision
from services.specs.accepted_specification import (
    AcceptedSpecification,
    AcceptedSpecificationIntegrityError,
    load_accepted_specification,
    load_current_accepted_specification,
)
from services.specs.candidate_contract import (
    load_candidate_contract,
    render_candidate_review_markdown,
)
from services.sprint_ownership import (
    SprintOwnerEvidenceError,
    SprintOwnerResolutionError,
    load_sprint_owner_evidence,
    resolve_sprint_owner,
    sprint_owner_projection,
)
from services.story_evidence_scope import structural_evidence_scope_payload
from utils.spec_schemas import ValidationEvidence
from workflow.contracts import JsonObject, JsonValue
from workflow.definitions.backlog import current_backlog_lineage
from workflow.definitions.planning import (
    candidate_set_fingerprint,
    selected_scope_stories,
)
from workflow.definitions.product_discovery import select_product_definition_state
from workflow.definitions.product_goal import (
    accepted_current_goal,
    accepted_current_vision,
    select_product_goal_interview_state,
)
from workflow.definitions.vision import select_vision_interview_state
from workflow.execution_integrity import ExecutionIntegrityError, execution_contract
from workflow.fingerprints import canonical_hash, canonical_json
from workflow.planning_integrity import current_task_content_fingerprint

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Never

    from sqlalchemy.engine import Engine

    from services.contracts.backlog import BacklogItem
    from services.contracts.roadmap import RoadmapBuilderOutput
    from services.contracts.story import CanonicalStoryOutput
    from workflow.facts import (
        PlanningArtifactFact,
        ProductGoalArtifactDecisionFact,
        ProductGoalArtifactFact,
        ProductGoalInterviewTurnFact,
        SpecificationCandidateFact,
        SpecificationDecisionFact,
        SpecificationSourceFact,
        SpecVersionFact,
        SprintFact,
        VisionArtifactDecisionFact,
        VisionArtifactFact,
        VisionInterviewTurnFact,
        WorkflowFactSnapshot,
    )

_JSON_OBJECT = TypeAdapter(JsonObject)
_STRING_LIST = TypeAdapter(list[str])
_SPECIFICATION_REVIEW_SCHEMA_VERSION = "agileforge.specification_review.v2"
_VISION_COMPONENT_NAMES: tuple[str, ...] = (
    "project_name",
    "target_user",
    "problem",
    "product_category",
    "key_benefit",
    "competitors",
    "differentiator",
)
_VISION_BASIS_SOURCE_KINDS: frozenset[str] = frozenset(
    {"human", "evidence", "inference"}
)


def _success(data: JsonObject) -> JsonObject:
    return {"ok": True, "data": data, "warnings": [], "errors": []}


def _error(code: str, message: str, **details: JsonValue) -> JsonObject:
    return {
        "ok": False,
        "data": details,
        "warnings": [],
        "errors": [{"code": code, "message": message, "details": details}],
    }


def _packet_read(packet: JsonObject, flavor: str | None) -> JsonObject:
    """Keep the canonical packet root exact and render only an explicit view."""
    if flavor is None:
        return _success(packet)
    try:
        rendered = render_packet(packet, flavor)
    except PacketRenderError as error:
        return _error(error.code, str(error))
    return _success({"packet": packet, "render": rendered})


def _validated(value: object) -> JsonObject:
    return _JSON_OBJECT.validate_python(value)


def _iso(value: object) -> str | None:
    isoformat = getattr(value, "isoformat", None)
    if not callable(isoformat):
        return None
    rendered = isoformat()
    return rendered if isinstance(rendered, str) else None


def _enum_value(value: object) -> JsonValue:
    candidate = getattr(value, "value", value)
    if candidate is None or isinstance(candidate, str | int | float | bool):
        return candidate
    return str(candidate)


def _result_data(result: JsonObject) -> JsonObject:
    data = result.get("data")
    return data if isinstance(data, dict) else {}


def _canonical_acceptance_criteria(raw: str) -> list[str]:
    """Parse one exact nonempty, nonblank canonical Story criteria array."""
    try:
        criteria = _STRING_LIST.validate_json(raw, strict=True)
    except ValidationError as error:
        message = "Stored Story acceptance criteria are invalid."
        raise ValueError(message) from error
    if (
        not criteria
        or any(not criterion.strip() for criterion in criteria)
        or canonical_json(criteria) != raw
    ):
        message = "Stored Story acceptance criteria are invalid."
        raise ValueError(message)
    return criteria


def _story_lineage_decision(
    status: Literal["pending_review", "accepted", "rejected", "feedback", "superseded"],
) -> PlanningLineageDecision:
    """Recover the terminal review outcome represented by a Story fact."""
    if status in {"superseded", "accepted"}:
        return "accepted"
    if status == "feedback":
        return "feedback"
    if status == "rejected":
        return "rejected"
    return None


def _current_story_artifacts(
    snapshot: WorkflowFactSnapshot,
    *,
    backlog_artifact_id: int,
    backlog_artifact_fingerprint: str,
) -> dict[tuple[int, str], PlanningArtifactFact]:
    """Select one valid Story leaf per item of the exact current Backlog root."""
    chains: dict[tuple[int, str], list[PlanningArtifactFact]] = {}
    for artifact in snapshot.planning_artifacts:
        artifact_backlog_id = artifact.backlog_artifact_id
        backlog_item_id = artifact.backlog_item_id
        if (
            artifact.artifact_type != "story"
            or artifact_backlog_id is None
            or artifact_backlog_id != backlog_artifact_id
            or artifact.backlog_artifact_fingerprint != backlog_artifact_fingerprint
            or backlog_item_id is None
        ):
            continue
        chains.setdefault((artifact_backlog_id, backlog_item_id), []).append(artifact)

    selected: dict[tuple[int, str], PlanningArtifactFact] = {}
    for key, artifacts in chains.items():
        nodes = tuple(
            ArtifactLineageNode(
                artifact_id=artifact.artifact_id,
                chain_key=key,
                version_number=artifact.version_number,
                supersedes_artifact_id=artifact.supersedes_artifact_id,
                decision=_story_lineage_decision(artifact.status),
            )
            for artifact in artifacts
        )
        physical_leaf = select_physical_leaf(nodes, chain_key=key)
        try:
            current = select_current_accepted_artifact(nodes, chain_key=key)
        except PlanningLineageError as error:
            if error.code is not PlanningLineageCode.ACCEPTED_LEAF_MISSING:
                raise
            current = physical_leaf
        selected[key] = next(
            artifact
            for artifact in artifacts
            if artifact.artifact_id == current.artifact_id
        )
    return selected


def _latest_resolved_goal(
    snapshot: WorkflowFactSnapshot,
) -> tuple[int, JsonObject] | None:
    """Return the latest exact accepted Goal/outcome pair without mutable state."""
    goals = {
        goal.product_goal_artifact_id: goal for goal in snapshot.product_goal_artifacts
    }
    accepted = {
        decision.product_goal_artifact_id: decision
        for decision in snapshot.product_goal_artifact_decisions
        if decision.decision == "accepted"
    }
    resolved: list[tuple[int, JsonObject]] = []
    for outcome in snapshot.product_goal_outcomes:
        goal = goals.get(outcome.product_goal_artifact_id)
        decision = accepted.get(outcome.product_goal_artifact_id)
        if (
            goal is None
            or decision is None
            or outcome.artifact_fingerprint != goal.content_fingerprint
            or decision.artifact_fingerprint != goal.content_fingerprint
        ):
            continue
        resolved.append(
            (
                goal.goal_number,
                {
                    "product_goal_artifact_id": goal.product_goal_artifact_id,
                    "fingerprint": goal.content_fingerprint,
                    "statement": goal.statement,
                    "goal_number": goal.goal_number,
                    "revision_number": goal.revision_number,
                    "outcome": outcome.outcome,
                    "rationale": outcome.rationale,
                    "decided_by": outcome.decided_by,
                },
            )
        )
    return max(resolved, key=lambda item: item[0]) if resolved else None


def _specification_review_data(
    decisions: list[SpecificationDecisionFact],
) -> JsonObject | None:
    """Project pending or one terminal candidate decision without ordering by time."""
    if not decisions:
        return {"state": "pending"}
    if len(decisions) != 1:
        return None
    decision = decisions[0]
    return {
        "state": decision.decision,
        "specification_decision_id": decision.specification_decision_id,
        "decision": decision.decision,
        "rationale": decision.rationale,
        "reviewer": decision.reviewer,
    }


def _vision_component_names(value: object) -> list[JsonValue]:
    """Keep only deliberate display names from strict Vision provenance."""
    if not isinstance(value, list | tuple):
        return []
    return [
        item
        for item in value
        if isinstance(item, str) and item in _VISION_COMPONENT_NAMES
    ]


def _vision_components_data(
    components: JsonObject,
    basis: Iterable[JsonObject],
) -> list[JsonValue]:
    """Join component values to whitelisted source kinds without raw references."""
    basis_by_component = {
        item.get("component"): item
        for item in basis
        if item.get("component") in _VISION_COMPONENT_NAMES
    }
    rendered: list[JsonValue] = []
    for name in _VISION_COMPONENT_NAMES:
        value = components.get(name)
        source_kinds = basis_by_component.get(name, {}).get("source_kinds", [])
        rendered.append(
            {
                "name": name,
                "value": value if isinstance(value, str) else None,
                "source_kinds": [
                    item
                    for item in source_kinds
                    if isinstance(item, str) and item in _VISION_BASIS_SOURCE_KINDS
                ]
                if isinstance(source_kinds, list | tuple)
                else [],
            }
        )
    return rendered


def _vision_assumptions_data(
    assumptions: Iterable[JsonObject],
) -> list[JsonValue]:
    """Project assumption prose and scope without persistence identities."""
    return [
        {
            "text": item.get("text"),
            "affected_components": _vision_component_names(
                item.get("affected_components")
            ),
        }
        for item in assumptions
        if isinstance(item.get("text"), str)
    ]


def _vision_conflicts_data(conflicts: Iterable[JsonObject]) -> list[JsonValue]:
    """Project reviewable conflict state without evidence or assumption IDs."""
    rendered: list[JsonValue] = []
    for item in conflicts:
        text = item.get("text")
        status = item.get("status")
        if not isinstance(text, str) or status not in {"resolved", "unresolved"}:
            continue
        resolution = item.get("resolution")
        rendered.append(
            {
                "text": text,
                "status": status,
                "affected_components": _vision_component_names(
                    item.get("affected_components")
                ),
                "resolution": resolution if isinstance(resolution, str) else None,
            }
        )
    return rendered


def _vision_questions_data(questions: Iterable[JsonObject]) -> list[JsonValue]:
    """Retain question identity for rendering only; omit internal references."""
    return [
        {
            "question_id": item.get("question_id"),
            "text": item.get("text"),
            "affected_components": _vision_component_names(
                item.get("affected_components")
            ),
        }
        for item in questions
        if isinstance(item.get("question_id"), str)
        and isinstance(item.get("text"), str)
    ]


def _vision_review_material(
    *,
    statement: str,
    components: JsonObject,
    provenance: tuple[
        Iterable[JsonObject],
        Iterable[JsonObject],
        Iterable[JsonObject],
    ],
    questions: Iterable[JsonObject] = (),
) -> JsonObject:
    """Build the sole display-safe Vision draft/candidate shape."""
    component_basis, assumptions, conflicts = provenance
    return {
        "statement": statement,
        "components": _vision_components_data(components, component_basis),
        "assumptions": _vision_assumptions_data(assumptions),
        "conflicts": _vision_conflicts_data(conflicts),
        "questions": _vision_questions_data(questions),
    }


def _vision_turn_draft_data(turn: VisionInterviewTurnFact) -> JsonObject:
    """Render the latest incomplete turn as display-safe draft material."""
    return _vision_review_material(
        statement=turn.vision_statement,
        components=turn.components,
        provenance=(turn.component_basis, turn.assumptions, turn.conflicts),
        questions=turn.clarifying_questions,
    )


def _vision_candidate_data(artifact: VisionArtifactFact) -> JsonObject:
    """Render review material plus the one browser-required concurrency binding."""
    return {
        **_vision_review_material(
            statement=artifact.statement,
            components=artifact.components,
            provenance=(
                artifact.component_basis,
                artifact.assumptions,
                artifact.conflicts,
            ),
        ),
        "review_fingerprint": artifact.content_fingerprint,
    }


def _vision_review_data(
    decision: VisionArtifactDecisionFact | None,
    *,
    pending: bool,
) -> JsonObject | None:
    """Render pending or exact terminal Vision review state."""
    if decision is None:
        return {"state": "pending", "rationale": None} if pending else None
    return {
        "state": decision.decision,
        "rationale": decision.rationale,
    }


def _goal_turn_data(turn: ProductGoalInterviewTurnFact) -> JsonObject:
    """Render one typed immutable Product Goal interview turn."""
    return {
        "product_goal_interview_turn_id": turn.product_goal_interview_turn_id,
        "vision_artifact_id": turn.vision_artifact_id,
        "vision_fingerprint": turn.vision_fingerprint,
        "goal_number": turn.goal_number,
        "revision_number": turn.revision_number,
        "prior_turn_id": turn.prior_turn_id,
        "user_text": turn.user_text,
        "statement": turn.goal_statement,
        "components": turn.components,
        "is_complete": turn.is_complete,
        "clarifying_questions": list(turn.clarifying_questions),
        "output_fingerprint": turn.output_fingerprint,
        "recorded_at": _iso(turn.recorded_at),
    }


def _goal_candidate_data(
    artifact: ProductGoalArtifactFact,
    snapshot: WorkflowFactSnapshot,
) -> JsonObject | None:
    """Render one Goal candidate with components from its exact source turn."""
    source = next(
        (
            item
            for item in snapshot.product_goal_interview_turns
            if item.product_goal_interview_turn_id == artifact.source_interview_turn_id
        ),
        None,
    )
    if source is None:
        return None
    return {
        "product_goal_artifact_id": artifact.product_goal_artifact_id,
        "vision_artifact_id": artifact.vision_artifact_id,
        "vision_fingerprint": artifact.vision_fingerprint,
        "goal_number": artifact.goal_number,
        "revision_number": artifact.revision_number,
        "fingerprint": artifact.content_fingerprint,
        "statement": artifact.statement,
        "components": source.components,
        "supersedes_product_goal_artifact_id": (
            artifact.supersedes_product_goal_artifact_id
        ),
        "source_interview_turn_id": artifact.source_interview_turn_id,
        "created_by": artifact.created_by,
        "created_at": _iso(artifact.created_at),
    }


def _goal_review_data(
    decision: ProductGoalArtifactDecisionFact | None,
    *,
    pending: bool,
) -> JsonObject | None:
    """Render pending or exact terminal Product Goal review state."""
    if decision is None:
        return {"state": "pending"} if pending else None
    return {
        "state": decision.decision,
        "product_goal_artifact_decision_id": (
            decision.product_goal_artifact_decision_id
        ),
        "decision": decision.decision,
        "rationale": decision.rationale,
        "reviewer": decision.reviewer,
        "decided_at": _iso(decision.decided_at),
    }


def _specification_candidate_data(
    candidate: SpecificationCandidate,
    *,
    decision_state: str,
) -> JsonObject:
    """Load and render one exact immutable v2 candidate review packet."""
    candidate_id = candidate.specification_candidate_id
    if candidate_id is None:
        message = "Specification candidate identity is unavailable."
        raise ValueError(message)
    payload, envelope = load_candidate_contract(
        candidate.canonical_envelope_json,
        expected_candidate_fingerprint=candidate.candidate_fingerprint,
    )
    persisted_envelope_values = (
        ("candidate kind", candidate.candidate_kind, envelope.candidate_kind.value),
        (
            "Vision identity",
            (candidate.vision_artifact_id, candidate.vision_fingerprint),
            (
                envelope.accepted_vision_id,
                envelope.accepted_vision_fingerprint,
            ),
        ),
        (
            "Product Goal identity",
            (
                candidate.product_goal_artifact_id,
                candidate.product_goal_fingerprint,
            ),
            (
                envelope.accepted_product_goal_id,
                envelope.accepted_product_goal_fingerprint,
            ),
        ),
        (
            "base Specification",
            (candidate.base_spec_version_id, candidate.base_spec_hash),
            (
                envelope.base_specification_id,
                envelope.base_payload_fingerprint,
            ),
        ),
        (
            "registered source fingerprint",
            candidate.specification_source_fingerprint,
            envelope.registered_source_fingerprint,
        ),
        (
            "source manifest fingerprint",
            candidate.source_manifest_fingerprint,
            envelope.source_manifest_fingerprint,
        ),
        (
            "producer input fingerprint",
            candidate.producer_input_fingerprint,
            envelope.producer_input_fingerprint,
        ),
        (
            "rendered view fingerprint",
            candidate.rendered_view_fingerprint,
            envelope.review_view_fingerprint,
        ),
        (
            "payload fingerprint",
            candidate.payload_fingerprint,
            envelope.payload_fingerprint,
        ),
        (
            "workflow attempt",
            (
                candidate.workflow_node_attempt_id,
                candidate.attempt_fingerprint,
            ),
            (
                envelope.workflow_node_attempt_id,
                envelope.attempt_fingerprint,
            ),
        ),
    )
    for label, persisted, contracted in persisted_envelope_values:
        if persisted != contracted:
            message = f"Specification candidate {label} changed."
            raise ValueError(message)
    amendment_diff = (
        None
        if envelope.amendment_diff is None
        else _validated(envelope.amendment_diff.model_dump(mode="json"))
    )
    return {
        "specification_candidate_id": candidate_id,
        "envelope_version": envelope.envelope_version,
        "candidate_kind": envelope.candidate_kind.value,
        "canonical_payload": _validated(payload.model_dump(mode="json")),
        "rendered_markdown": render_candidate_review_markdown(payload, envelope),
        "vision_artifact_id": envelope.accepted_vision_id,
        "vision_fingerprint": envelope.accepted_vision_fingerprint,
        "product_goal_artifact_id": envelope.accepted_product_goal_id,
        "product_goal_fingerprint": envelope.accepted_product_goal_fingerprint,
        "base_spec_version_id": envelope.base_specification_id,
        "base_spec_hash": envelope.base_payload_fingerprint,
        "specification_source_id": candidate.specification_source_id,
        "registered_source_fingerprint": envelope.registered_source_fingerprint,
        "source_producer_capability": envelope.source_producer_capability,
        "source_preparation_capability": (envelope.source_preparation_capability),
        "source_manifest": [
            _validated(item.model_dump(mode="json"))
            for item in envelope.source_manifest
        ],
        "source_manifest_fingerprint": envelope.source_manifest_fingerprint,
        "accepted_fact_fingerprint": envelope.accepted_fact_fingerprint,
        "producer_input_fingerprint": envelope.producer_input_fingerprint,
        "producer_capability": envelope.producer_capability,
        "producer_version": envelope.producer_version,
        "model_id": envelope.model_id,
        "model_configuration_fingerprint": (envelope.model_configuration_fingerprint),
        "prompt_fingerprint": envelope.prompt_fingerprint,
        "prompt_version": envelope.prompt_version,
        "workflow_node_attempt_id": envelope.workflow_node_attempt_id,
        "attempt_fingerprint": envelope.attempt_fingerprint,
        "correlation_id": envelope.correlation_id,
        "produced_at": _iso(envelope.produced_at),
        "payload_fingerprint": envelope.payload_fingerprint,
        "profile_version": envelope.profile_version,
        "renderer_version": envelope.renderer_version,
        "rendered_view_fingerprint": envelope.review_view_fingerprint,
        "amendment_diff": amendment_diff,
        "candidate_fingerprint": envelope.candidate_fingerprint,
        "supersedes_specification_candidate_id": (
            candidate.supersedes_specification_candidate_id
        ),
        "supersedes_candidate_fingerprint": (
            candidate.supersedes_candidate_fingerprint
        ),
        "decision_state": decision_state,
    }


def _source_document_metadata(
    document: SpecificationSourceDocument,
) -> JsonObject:
    """Project document identity without exposing captured source bytes."""
    return {
        "source_id": document.source_id,
        "relative_path": document.relative_path,
        "byte_length": document.byte_length,
        "content_fingerprint": document.content_fingerprint,
    }


def _specification_source_data(source: SpecificationSourceFact) -> JsonObject:
    """Project registered-source provenance as metadata and digests only."""
    bundle = SpecificationSourceBundle.model_validate(source.bundle)
    if source_bundle_fingerprint(bundle) != source.source_fingerprint:
        message = "Registered Specification source fingerprint changed."
        raise ValueError(message)
    if (
        source.repository_head_sha,
        source.repository_dirty,
        source.repository_status_fingerprint,
        source.vision_fingerprint,
        source.product_goal_fingerprint,
    ) != (
        bundle.repository_revision.head_sha,
        bundle.repository_revision.dirty,
        bundle.repository_revision.status_fingerprint,
        bundle.accepted_vision_fingerprint,
        bundle.accepted_product_goal_fingerprint,
    ):
        message = "Registered Specification source lineage changed."
        raise ValueError(message)
    context_document = bundle.context.document
    return {
        "schema_version": bundle.schema_version,
        "specification_source_id": source.specification_source_id,
        "source_fingerprint": source.source_fingerprint,
        "producer_capability": bundle.producer_capability,
        "preparation_capability": bundle.preparation_capability,
        "source": _source_document_metadata(bundle.source),
        "context": {
            "state": bundle.context.state,
            "document": (
                None
                if context_document is None
                else _source_document_metadata(context_document)
            ),
        },
        "adrs": [_source_document_metadata(document) for document in bundle.adrs],
        "repository": {
            "repository_binding_id": source.repository_binding_id,
            "head_sha": bundle.repository_revision.head_sha,
            "branch_name": bundle.repository_revision.branch_name,
            "detached_head": bundle.repository_revision.detached_head,
            "dirty": bundle.repository_revision.dirty,
            "status_fingerprint": bundle.repository_revision.status_fingerprint,
            "probe_version": bundle.repository_revision.probe_version,
        },
        "accepted_vision": {
            "vision_artifact_id": source.vision_artifact_id,
            "fingerprint": source.vision_fingerprint,
        },
        "active_product_goal": {
            "product_goal_artifact_id": source.product_goal_artifact_id,
            "fingerprint": source.product_goal_fingerprint,
        },
        "supersedes_specification_source_id": (
            source.supersedes_specification_source_id
        ),
        "supersedes_source_fingerprint": source.supersedes_source_fingerprint,
        "registered_by": source.registered_by,
        "registered_at": _iso(source.registered_at),
    }


def _specification_registry_data(
    spec: SpecRegistry,
    *,
    candidate: JsonObject,
) -> JsonObject:
    """Project one accepted row as a reference to its exact candidate bytes."""
    return {
        "spec_version_id": spec.spec_version_id,
        "spec_hash": spec.spec_hash,
        "status": spec.status,
        "created_at": _iso(spec.created_at),
        "source_specification_candidate_id": (spec.source_specification_candidate_id),
        "source_specification_candidate_fingerprint": (
            spec.source_specification_candidate_fingerprint
        ),
        "source_vision_artifact_id": spec.source_vision_artifact_id,
        "source_vision_fingerprint": spec.source_vision_fingerprint,
        "source_product_goal_artifact_id": (spec.source_product_goal_artifact_id),
        "source_product_goal_fingerprint": (spec.source_product_goal_fingerprint),
        "supersedes_spec_version_id": spec.supersedes_spec_version_id,
        "candidate": candidate,
    }


def _accepted_registry_candidate_payloads(
    *,
    project_id: int,
    specifications: list[SpecRegistry],
    candidates: list[SpecificationCandidate],
    decisions: list[SpecificationDecision],
) -> dict[int, JsonObject]:
    """Resolve every registry row to one exact accepted v2 candidate packet."""
    payloads: dict[int, JsonObject] = {}
    for spec in specifications:
        spec_version_id = spec.spec_version_id
        if spec_version_id is None:
            message = "Stored Specification registry identity is unavailable."
            raise ValueError(message)
        matches = [
            item
            for item in candidates
            if (
                item.specification_candidate_id
                == spec.source_specification_candidate_id
                and item.candidate_fingerprint
                == spec.source_specification_candidate_fingerprint
                and item.payload_fingerprint == spec.spec_hash
                and item.vision_artifact_id == spec.source_vision_artifact_id
                and item.vision_fingerprint == spec.source_vision_fingerprint
                and item.product_goal_artifact_id
                == spec.source_product_goal_artifact_id
                and item.product_goal_fingerprint
                == spec.source_product_goal_fingerprint
            )
        ]
        terminal_decisions = [
            item
            for item in decisions
            if item.specification_candidate_id == spec.source_specification_candidate_id
            and item.candidate_fingerprint
            == spec.source_specification_candidate_fingerprint
        ]
        if (
            len(matches) != 1
            or len(terminal_decisions) != 1
            or terminal_decisions[0].decision != "accepted"
        ):
            message = (
                "Stored Specification registry does not resolve one accepted "
                f"candidate for project {project_id}, version {spec_version_id}."
            )
            raise ValueError(message)
        payloads[spec_version_id] = _specification_candidate_data(
            matches[0],
            decision_state=terminal_decisions[0].decision,
        )
    return payloads


class _PlanningArtifactProjectionError(RuntimeError):
    """Closed Task 7 projection failure with one stable error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _BacklogReviewRecord:
    artifact: BacklogArtifact
    specification: AcceptedSpecification
    content: BacklogOutput
    decision: BacklogArtifactDecision | None


@dataclass(frozen=True)
class _RoadmapReviewRecord:
    artifact: RoadmapArtifact
    backlog: _BacklogReviewRecord
    content: RoadmapBuilderOutput
    decision: RoadmapArtifactDecision | None


@dataclass(frozen=True)
class _StoryReviewRecord:
    artifact: StoryArtifact
    roadmap: _RoadmapReviewRecord
    backlog_item: BacklogItem
    content: CanonicalStoryOutput
    invalid_fields: tuple[str, ...]
    decision: StoryArtifactDecision | None


def _raise_planning_failure(
    code: str,
    message: str,
    *,
    cause: Exception | None = None,
) -> Never:
    error = _PlanningArtifactProjectionError(code, message)
    if cause is None:
        raise error
    raise error from cause


def _backlog_terminal_decision(
    session: Session,
    *,
    artifact: BacklogArtifact,
) -> BacklogArtifactDecision | None:
    decisions = session.exec(
        select(BacklogArtifactDecision).where(
            col(BacklogArtifactDecision.project_id) == artifact.project_id,
            col(BacklogArtifactDecision.backlog_artifact_id)
            == artifact.backlog_artifact_id,
        )
    ).all()
    if not decisions:
        return None
    if len(decisions) != 1 or (
        decisions[0].artifact_fingerprint != artifact.content_fingerprint
        or decisions[0].decision not in {"accepted", "feedback", "rejected"}
    ):
        _raise_planning_failure(
            "PLANNING_ARTIFACT_LINEAGE_INVALID",
            "Backlog review decision does not match its exact artifact.",
        )
    return decisions[0]


def _roadmap_terminal_decision(
    session: Session,
    *,
    artifact: RoadmapArtifact,
) -> RoadmapArtifactDecision | None:
    decisions = session.exec(
        select(RoadmapArtifactDecision).where(
            col(RoadmapArtifactDecision.project_id) == artifact.project_id,
            col(RoadmapArtifactDecision.roadmap_artifact_id)
            == artifact.roadmap_artifact_id,
        )
    ).all()
    if not decisions:
        return None
    if len(decisions) != 1 or (
        decisions[0].artifact_fingerprint != artifact.content_fingerprint
        or decisions[0].decision not in {"accepted", "feedback", "rejected"}
    ):
        _raise_planning_failure(
            "PLANNING_ARTIFACT_LINEAGE_INVALID",
            "Roadmap review decision does not match its exact artifact.",
        )
    return decisions[0]


def _story_terminal_decision(
    session: Session,
    *,
    artifact: StoryArtifact,
) -> StoryArtifactDecision | None:
    decisions = session.exec(
        select(StoryArtifactDecision).where(
            col(StoryArtifactDecision.project_id) == artifact.project_id,
            col(StoryArtifactDecision.story_artifact_id) == artifact.story_artifact_id,
        )
    ).all()
    if not decisions:
        return None
    if len(decisions) != 1 or (
        decisions[0].artifact_fingerprint != artifact.content_fingerprint
        or decisions[0].decision not in {"accepted", "feedback", "rejected"}
    ):
        _raise_planning_failure(
            "PLANNING_ARTIFACT_LINEAGE_INVALID",
            "Story review decision does not match its exact artifact.",
        )
    return decisions[0]


def _load_backlog_review_record(
    session: Session,
    *,
    project_id: int,
    backlog_artifact_id: int,
) -> _BacklogReviewRecord:
    from services.agent_workbench.backlog_phase import (  # noqa: PLC0415
        _backlog_lineage_nodes,
    )
    from services.planning_lineage import (  # noqa: PLC0415
        PlanningLineageError,
        validate_artifact_lineage,
    )

    artifact = session.exec(
        select(BacklogArtifact).where(
            col(BacklogArtifact.project_id) == project_id,
            col(BacklogArtifact.backlog_artifact_id) == backlog_artifact_id,
        )
    ).one_or_none()
    if artifact is None:
        _raise_planning_failure(
            "BACKLOG_ARTIFACT_NOT_FOUND",
            f"Backlog artifact {backlog_artifact_id} was not found in this Project.",
        )
    specification = load_accepted_specification(
        session,
        project_id=project_id,
        spec_version_id=artifact.spec_version_id,
        spec_hash=artifact.spec_hash,
    )
    registry = session.get(SpecRegistry, artifact.spec_version_id)
    goal = session.exec(
        select(ProductGoalArtifact).where(
            col(ProductGoalArtifact.project_id) == project_id,
            col(ProductGoalArtifact.product_goal_artifact_id)
            == artifact.product_goal_artifact_id,
            col(ProductGoalArtifact.content_fingerprint)
            == artifact.product_goal_fingerprint,
        )
    ).one_or_none()
    goal_decision = session.exec(
        select(ProductGoalArtifactDecision).where(
            col(ProductGoalArtifactDecision.project_id) == project_id,
            col(ProductGoalArtifactDecision.product_goal_artifact_id)
            == artifact.product_goal_artifact_id,
            col(ProductGoalArtifactDecision.artifact_fingerprint)
            == artifact.product_goal_fingerprint,
            col(ProductGoalArtifactDecision.decision) == "accepted",
        )
    ).one_or_none()
    if (
        registry is None
        or (
            registry.project_id,
            registry.spec_hash,
            registry.source_product_goal_artifact_id,
            registry.source_product_goal_fingerprint,
        )
        != (
            project_id,
            artifact.spec_hash,
            artifact.product_goal_artifact_id,
            artifact.product_goal_fingerprint,
        )
        or goal is None
        or goal_decision is None
    ):
        _raise_planning_failure(
            "PLANNING_ARTIFACT_LINEAGE_INVALID",
            "Backlog artifact does not resolve its exact Specification and "
            "Goal lineage.",
        )
    try:
        _canonical_content, content = load_stored_backlog_planning_content(
            artifact.canonical_content_json,
            expected_fingerprint=artifact.content_fingerprint,
            specification=specification,
        )
    except (TypeError, ValidationError, ValueError) as error:
        _raise_planning_failure(
            "PLANNING_ARTIFACT_CONTENT_INVALID",
            "Backlog artifact canonical content is invalid.",
            cause=error,
        )
    try:
        validate_artifact_lineage(
            _backlog_lineage_nodes(session, project_id=project_id)
        )
    except (PlanningLineageError, ValueError) as error:
        _raise_planning_failure(
            "PLANNING_ARTIFACT_LINEAGE_INVALID",
            "Backlog artifact ancestry is invalid.",
            cause=error,
        )
    return _BacklogReviewRecord(
        artifact=artifact,
        specification=specification,
        content=content,
        decision=_backlog_terminal_decision(session, artifact=artifact),
    )


def _load_roadmap_review_record(
    session: Session,
    *,
    project_id: int,
    roadmap_artifact_id: int,
) -> _RoadmapReviewRecord:
    from services.agent_workbench.roadmap_phase import (  # noqa: PLC0415
        _roadmap_lineage_nodes,
    )
    from services.planning_lineage import (  # noqa: PLC0415
        PlanningLineageError,
        validate_artifact_lineage,
    )

    artifact = session.exec(
        select(RoadmapArtifact).where(
            col(RoadmapArtifact.project_id) == project_id,
            col(RoadmapArtifact.roadmap_artifact_id) == roadmap_artifact_id,
        )
    ).one_or_none()
    if artifact is None:
        _raise_planning_failure(
            "ROADMAP_ARTIFACT_NOT_FOUND",
            f"Roadmap artifact {roadmap_artifact_id} was not found in this Project.",
        )
    backlog = _load_backlog_review_record(
        session,
        project_id=project_id,
        backlog_artifact_id=artifact.backlog_artifact_id,
    )
    if (
        backlog.artifact.content_fingerprint != artifact.backlog_artifact_fingerprint
        or backlog.decision is None
        or backlog.decision.decision != "accepted"
    ):
        _raise_planning_failure(
            "PLANNING_ARTIFACT_LINEAGE_INVALID",
            "Roadmap artifact does not resolve one exact accepted Backlog parent.",
        )
    try:
        _canonical_content, content = load_stored_roadmap_planning_content(
            artifact.canonical_content_json,
            expected_fingerprint=artifact.content_fingerprint,
            parent_backlog_item_ids=tuple(
                item.backlog_item_id for item in backlog.content.backlog_items
            ),
        )
    except (TypeError, ValidationError, ValueError) as error:
        _raise_planning_failure(
            "PLANNING_ARTIFACT_CONTENT_INVALID",
            "Roadmap artifact canonical content or Backlog coverage is invalid.",
            cause=error,
        )
    try:
        validate_artifact_lineage(
            _roadmap_lineage_nodes(session, project_id=project_id)
        )
    except (PlanningLineageError, ValueError) as error:
        _raise_planning_failure(
            "PLANNING_ARTIFACT_LINEAGE_INVALID",
            "Roadmap artifact ancestry is invalid.",
            cause=error,
        )
    return _RoadmapReviewRecord(
        artifact=artifact,
        backlog=backlog,
        content=content,
        decision=_roadmap_terminal_decision(session, artifact=artifact),
    )


def _load_story_review_record(
    session: Session,
    *,
    project_id: int,
    story_artifact_id: int,
) -> _StoryReviewRecord:
    from services.agent_workbench.story_phase import (  # noqa: PLC0415
        _story_lineage_nodes,
        load_stored_story_planning_content_for_review,
    )
    from services.planning_lineage import (  # noqa: PLC0415
        PlanningLineageError,
        validate_artifact_lineage,
    )

    artifact = session.exec(
        select(StoryArtifact).where(
            col(StoryArtifact.project_id) == project_id,
            col(StoryArtifact.story_artifact_id) == story_artifact_id,
        )
    ).one_or_none()
    if artifact is None:
        _raise_planning_failure(
            "STORY_ARTIFACT_NOT_FOUND",
            f"Story artifact {story_artifact_id} was not found in this Project.",
        )
    roadmap = _load_roadmap_review_record(
        session,
        project_id=project_id,
        roadmap_artifact_id=artifact.roadmap_artifact_id,
    )
    backlog = roadmap.backlog
    if (
        artifact.roadmap_artifact_fingerprint != roadmap.artifact.content_fingerprint
        or roadmap.decision is None
        or roadmap.decision.decision != "accepted"
        or artifact.source_backlog_artifact_id != backlog.artifact.backlog_artifact_id
        or artifact.source_backlog_artifact_fingerprint
        != backlog.artifact.content_fingerprint
        or roadmap.artifact.backlog_artifact_id != artifact.source_backlog_artifact_id
        or roadmap.artifact.backlog_artifact_fingerprint
        != artifact.source_backlog_artifact_fingerprint
    ):
        _raise_planning_failure(
            "PLANNING_ARTIFACT_LINEAGE_INVALID",
            (
                "Story artifact does not resolve exact accepted Roadmap and "
                "Backlog parents."
            ),
        )
    backlog_items = tuple(
        item
        for item in backlog.content.backlog_items
        if item.backlog_item_id == artifact.backlog_item_id
    )
    occurrences = sum(
        release.backlog_item_ids.count(artifact.backlog_item_id)
        for release in roadmap.content.roadmap_releases
    )
    if len(backlog_items) != 1 or occurrences != 1:
        _raise_planning_failure(
            "PLANNING_ARTIFACT_LINEAGE_INVALID",
            "Story artifact Backlog item is missing from its exact Roadmap lineage.",
        )
    try:
        _canonical_content, content, invalid_fields = (
            load_stored_story_planning_content_for_review(
                artifact.canonical_content_json,
                expected_fingerprint=artifact.content_fingerprint,
                specification=backlog.specification,
                backlog_item=backlog_items[0],
            )
        )
    except (TypeError, ValidationError, ValueError) as error:
        _raise_planning_failure(
            "PLANNING_ARTIFACT_CONTENT_INVALID",
            "Story artifact canonical content is invalid.",
            cause=error,
        )
    item_ids = tuple(envelope.item.story_item_id for envelope in content.story_items)
    if artifact.story_item_ids_json != canonical_json(list(item_ids)):
        _raise_planning_failure(
            "PLANNING_ARTIFACT_CONTENT_INVALID",
            "Story artifact item IDs do not match its exact canonical content.",
        )
    try:
        validate_artifact_lineage(_story_lineage_nodes(session, project_id=project_id))
    except (PlanningLineageError, ValueError) as error:
        _raise_planning_failure(
            "PLANNING_ARTIFACT_LINEAGE_INVALID",
            "Story artifact ancestry is invalid.",
            cause=error,
        )
    return _StoryReviewRecord(
        artifact=artifact,
        roadmap=roadmap,
        backlog_item=backlog_items[0],
        content=content,
        invalid_fields=invalid_fields,
        decision=_story_terminal_decision(session, artifact=artifact),
    )


def _planning_review_data(
    decision: (
        BacklogArtifactDecision | RoadmapArtifactDecision | StoryArtifactDecision | None
    ),
) -> JsonObject:
    if decision is None:
        return {"state": "pending"}
    return {
        "state": decision.decision,
        "rationale": decision.rationale,
        "reviewer": decision.reviewer,
        "decided_at": _iso(decision.decided_at),
    }


def _specification_evidence(
    specification: AcceptedSpecification,
    spec_item_ids: tuple[str, ...],
) -> list[JsonValue]:
    items_by_id = {item.id: item for item in specification.payload.items}
    evidence: list[JsonValue] = []
    for item_id in spec_item_ids:
        item = items_by_id.get(item_id)
        if item is None:
            _raise_planning_failure(
                "PLANNING_ARTIFACT_CONTENT_INVALID",
                f"Planning artifact cites unknown Specification item {item_id}.",
            )
        evidence.append(
            {
                "spec_item_id": item.id,
                "title": item.title,
                "statement": item.statement,
                "level": None if item.level is None else item.level.value,
                "acceptance_criteria": list(item.acceptance),
                "verification_method": (
                    None if item.verification is None else item.verification.value
                ),
            }
        )
    return evidence


def _planning_backlog_item(
    item: BacklogItem,
    *,
    specification: AcceptedSpecification,
) -> JsonObject:
    data = _validated(item.model_dump(mode="json", exclude={"spec_item_ids"}))
    data["specification_evidence"] = _specification_evidence(
        specification, item.spec_item_ids
    )
    return data


def _backlog_review_projection(record: _BacklogReviewRecord) -> JsonObject:
    artifact = record.artifact
    return {
        "schema_version": "agileforge.planning-artifact-review.v1",
        "phase": "backlog",
        "project_id": artifact.project_id,
        "lineage": {
            "specification": {
                "spec_version_id": record.specification.spec_version_id,
                "spec_hash": record.specification.spec_hash,
                "status": record.specification.status,
            },
            "product_goal": {
                "product_goal_artifact_id": artifact.product_goal_artifact_id,
                "product_goal_fingerprint": artifact.product_goal_fingerprint,
            },
        },
        "candidate": {
            "backlog_artifact_id": artifact.backlog_artifact_id,
            "artifact_fingerprint": artifact.content_fingerprint,
            "version_number": artifact.version_number,
            "supersedes_backlog_artifact_id": (artifact.supersedes_backlog_artifact_id),
            "created_by": artifact.created_by,
            "created_at": _iso(artifact.created_at),
            "backlog_items": [
                _planning_backlog_item(
                    item,
                    specification=record.specification,
                )
                for item in record.content.backlog_items
            ],
            "is_complete": record.content.is_complete,
            "clarifying_questions": list(record.content.clarifying_questions),
        },
        "review": _planning_review_data(record.decision),
    }


def _roadmap_review_projection(record: _RoadmapReviewRecord) -> JsonObject:
    artifact = record.artifact
    backlog_items = {
        item.backlog_item_id: item for item in record.backlog.content.backlog_items
    }

    releases: list[JsonValue] = []
    for release in record.content.roadmap_releases:
        release_data = _validated(
            release.model_dump(mode="json", exclude={"backlog_item_ids"})
        )
        release_data["backlog_items"] = [
            _planning_backlog_item(
                backlog_items[item_id],
                specification=record.backlog.specification,
            )
            for item_id in release.backlog_item_ids
        ]
        releases.append(release_data)
    return {
        "schema_version": "agileforge.planning-artifact-review.v1",
        "phase": "roadmap",
        "project_id": artifact.project_id,
        "lineage": {
            "specification": {
                "spec_version_id": record.backlog.specification.spec_version_id,
                "spec_hash": record.backlog.specification.spec_hash,
                "status": record.backlog.specification.status,
            },
            "product_goal": {
                "product_goal_artifact_id": (
                    record.backlog.artifact.product_goal_artifact_id
                ),
                "product_goal_fingerprint": (
                    record.backlog.artifact.product_goal_fingerprint
                ),
            },
            "backlog": {
                "backlog_artifact_id": artifact.backlog_artifact_id,
                "backlog_artifact_fingerprint": (artifact.backlog_artifact_fingerprint),
            },
        },
        "candidate": {
            "roadmap_artifact_id": artifact.roadmap_artifact_id,
            "artifact_fingerprint": artifact.content_fingerprint,
            "version_number": artifact.version_number,
            "supersedes_roadmap_artifact_id": (artifact.supersedes_roadmap_artifact_id),
            "created_by": artifact.created_by,
            "created_at": _iso(artifact.created_at),
            "roadmap_releases": releases,
            "roadmap_summary": record.content.roadmap_summary,
            "is_complete": record.content.is_complete,
            "clarifying_questions": list(record.content.clarifying_questions),
        },
        "review": _planning_review_data(record.decision),
    }


def _story_review_projection(record: _StoryReviewRecord) -> JsonObject:
    artifact = record.artifact
    backlog = record.roadmap.backlog
    parent_priority = record.backlog_item.priority
    story_items: list[JsonValue] = []
    if not record.invalid_fields:
        for ordinal, envelope in enumerate(record.content.story_items, start=1):
            item = envelope.item
            item_data = _validated(
                item.model_dump(
                    mode="json",
                    exclude={"spec_item_ids"},
                )
            )
            item_data["specification_evidence"] = _specification_evidence(
                backlog.specification,
                item.spec_item_ids,
            )
            item_data["order"] = ordinal
            item_data["rank"] = str((parent_priority * 100) + ordinal)
            item_data["story_points"] = STORY_POINTS_BY_EFFORT[item.estimated_effort]
            story_items.append(item_data)
    return {
        "schema_version": "agileforge.planning-artifact-review.v1",
        "phase": "story",
        "project_id": artifact.project_id,
        "candidate_available": not record.invalid_fields,
        "invalid_fields": list(record.invalid_fields),
        "lineage": {
            "specification": {
                "spec_version_id": backlog.specification.spec_version_id,
                "spec_hash": backlog.specification.spec_hash,
                "status": backlog.specification.status,
            },
            "product_goal": {
                "product_goal_artifact_id": (backlog.artifact.product_goal_artifact_id),
                "product_goal_fingerprint": (backlog.artifact.product_goal_fingerprint),
            },
            "backlog": {
                "backlog_artifact_id": artifact.source_backlog_artifact_id,
                "backlog_artifact_fingerprint": (
                    artifact.source_backlog_artifact_fingerprint
                ),
            },
            "backlog_item": _planning_backlog_item(
                record.backlog_item,
                specification=backlog.specification,
            ),
            "roadmap": {
                "roadmap_artifact_id": artifact.roadmap_artifact_id,
                "roadmap_artifact_fingerprint": (artifact.roadmap_artifact_fingerprint),
            },
        },
        "candidate": {
            "story_artifact_id": artifact.story_artifact_id,
            "artifact_fingerprint": artifact.content_fingerprint,
            "version_number": artifact.version_number,
            "supersedes_story_artifact_id": artifact.supersedes_story_artifact_id,
            "created_by": artifact.created_by,
            "created_at": _iso(artifact.created_at),
            "story_items": story_items,
            "is_complete": record.content.is_complete and not record.invalid_fields,
            "clarifying_questions": (
                []
                if record.invalid_fields
                else list(record.content.clarifying_questions)
            ),
        },
        "review": _planning_review_data(record.decision),
    }


@dataclass(frozen=True)
class _ProjectReadContext:
    """One confirmed durable Project identity for a scoped read."""

    project_id: int
    project: Project


@dataclass(frozen=True)
class _ProjectReadFailure:
    """Typed missing-Project result shared by scoped projections."""

    error: JsonObject


@dataclass(frozen=True)
class _SpecificationReadProjection:
    """Exact candidate packet plus its selected accepted registry row, if any."""

    candidate: JsonObject
    registry: SpecRegistry | None


@dataclass(frozen=True)
class _SpecificationReadFailure:
    """Typed unavailable-candidate result for Specification reads."""

    error: JsonObject


class DurableReadProjectionService:
    """Read supported operator views without deriving workflow availability."""

    def __init__(self, *, engine: Engine) -> None:
        """Bind durable records used by read-only projections."""
        self._engine = engine

    def project_list(self) -> JsonObject:
        """Return durable Project identities and aggregate counts."""
        with Session(self._engine) as session:
            projects = session.exec(
                select(Project).order_by(col(Project.project_id))
            ).all()
            stories = session.exec(select(UserStory)).all()
            sprints = session.exec(select(Sprint)).all()
        items: list[JsonValue] = []
        for project in projects:
            project_id = project.project_id
            if project_id is None:
                continue
            items.append(
                {
                    "id": project_id,
                    "project_id": project_id,
                    "name": project.name,
                    "description": project.description,
                    "user_stories_count": sum(
                        1
                        for story in stories
                        if story.project_id == project_id and not story.is_superseded
                    ),
                    "sprint_count": sum(
                        1 for sprint in sprints if sprint.project_id == project_id
                    ),
                    "updated_at": _iso(project.updated_at),
                }
            )
        return _success({"items": items, "count": len(items)})

    def project_show(self, *, project_id: int) -> JsonObject:
        """Return one Project detail without routing state."""
        context = self._project(project_id)
        if isinstance(context, _ProjectReadFailure):
            return context.error
        project = context.project
        with Session(self._engine) as session:
            stories = session.exec(
                select(UserStory).where(col(UserStory.project_id) == project_id)
            ).all()
            sprints = session.exec(
                select(Sprint).where(col(Sprint.project_id) == project_id)
            ).all()
            snapshot = WorkflowFactRepository(session).load(project_id)
        vision = accepted_current_vision(snapshot)
        goal = accepted_current_goal(snapshot)
        return _success(
            {
                "id": project_id,
                "project_id": project_id,
                "name": project.name,
                "description": project.description,
                "accepted_vision": (
                    None
                    if vision is None
                    else {
                        "vision_artifact_id": vision.vision_artifact_id,
                        "fingerprint": vision.content_fingerprint,
                        "statement": vision.statement,
                    }
                ),
                "accepted_product_goal": (
                    None
                    if goal is None
                    else {
                        "product_goal_artifact_id": goal.product_goal_artifact_id,
                        "fingerprint": goal.content_fingerprint,
                        "statement": goal.statement,
                        "goal_number": goal.goal_number,
                    }
                ),
                "repository": self._repository_data(project),
                "structure_counts": {
                    "user_stories": sum(
                        1 for story in stories if not story.is_superseded
                    ),
                    "sprints": len(sprints),
                },
                "updated_at": _iso(project.updated_at),
            }
        )

    def repository_status(self, *, project_id: int) -> JsonObject:
        """Return active immutable repository provenance without graph routing state."""
        context = self._project(project_id)
        if isinstance(context, _ProjectReadFailure):
            return context.error
        return _success({"repository": self._repository_data(context.project)})

    def backlog_review(
        self,
        *,
        project_id: int,
        backlog_artifact_id: int,
    ) -> JsonObject:
        """Render one exact Backlog candidate with pinned Specification evidence."""
        context = self._project(project_id)
        if isinstance(context, _ProjectReadFailure):
            return context.error
        try:
            with Session(self._engine) as session:
                record = _load_backlog_review_record(
                    session,
                    project_id=project_id,
                    backlog_artifact_id=backlog_artifact_id,
                )
                return _success(_backlog_review_projection(record))
        except AcceptedSpecificationIntegrityError as error:
            return _error(
                error.code,
                str(error),
                project_id=project_id,
                backlog_artifact_id=backlog_artifact_id,
            )
        except _PlanningArtifactProjectionError as error:
            return _error(
                error.code,
                str(error),
                project_id=project_id,
                backlog_artifact_id=backlog_artifact_id,
            )

    def roadmap_review(
        self,
        *,
        project_id: int,
        roadmap_artifact_id: int,
    ) -> JsonObject:
        """Render one exact Roadmap candidate with resolved Backlog evidence."""
        context = self._project(project_id)
        if isinstance(context, _ProjectReadFailure):
            return context.error
        try:
            with Session(self._engine) as session:
                record = _load_roadmap_review_record(
                    session,
                    project_id=project_id,
                    roadmap_artifact_id=roadmap_artifact_id,
                )
                return _success(_roadmap_review_projection(record))
        except AcceptedSpecificationIntegrityError as error:
            return _error(
                error.code,
                str(error),
                project_id=project_id,
                roadmap_artifact_id=roadmap_artifact_id,
            )
        except _PlanningArtifactProjectionError as error:
            return _error(
                error.code,
                str(error),
                project_id=project_id,
                roadmap_artifact_id=roadmap_artifact_id,
            )

    def story_review(
        self,
        *,
        project_id: int,
        story_artifact_id: int,
    ) -> JsonObject:
        """Render one exact Story candidate with resolved pinned evidence."""
        context = self._project(project_id)
        if isinstance(context, _ProjectReadFailure):
            return context.error
        try:
            with Session(self._engine) as session:
                record = _load_story_review_record(
                    session,
                    project_id=project_id,
                    story_artifact_id=story_artifact_id,
                )
                return _success(_story_review_projection(record))
        except AcceptedSpecificationIntegrityError as error:
            return _error(
                error.code,
                str(error),
                project_id=project_id,
                story_artifact_id=story_artifact_id,
            )
        except _PlanningArtifactProjectionError as error:
            return _error(
                error.code,
                str(error),
                project_id=project_id,
                story_artifact_id=story_artifact_id,
            )

    def _repository_data(self, project: Project) -> JsonObject | None:
        if project.active_repository_binding_id is None:
            return None
        with Session(self._engine) as session:
            binding = session.get(
                RepositoryBinding,
                project.active_repository_binding_id,
            )
        if binding is None:
            return None
        try:
            status_entries = json.loads(binding.status_entries_json)
            remotes = json.loads(binding.remotes_json)
            warnings = json.loads(binding.warnings_json)
        except json.JSONDecodeError:
            return None
        if (
            not isinstance(status_entries, list)
            or not isinstance(remotes, list)
            or not isinstance(warnings, list)
        ):
            return None
        return {
            "repository_binding_id": binding.repository_binding_id,
            "binding_fingerprint": repository_binding_fingerprint(binding),
            "status_fingerprint": binding.status_fingerprint,
            "status_entries": status_entries,
            "worktree_path": binding.worktree_path,
            "common_git_dir": binding.common_git_dir,
            "head_sha": binding.head_sha,
            "branch_name": binding.branch_name,
            "detached_head": binding.detached_head,
            "dirty": binding.dirty,
            "remotes": remotes,
            "warnings": warnings,
            "probe_version": binding.probe_version,
            "inspected_at": _iso(binding.inspected_at),
            "recorded_by": binding.recorded_by,
            "supersedes_repository_binding_id": (
                binding.supersedes_repository_binding_id
            ),
        }

    def vision_status(self, *, project_id: int) -> JsonObject:
        """Project durable Vision review and active interview state."""
        snapshot = self._snapshot(project_id)
        if isinstance(snapshot, dict):
            return snapshot
        selection = select_vision_interview_state(snapshot)
        if selection.conflict:
            return _success(
                {
                    "bootstrap_available": False,
                    "current": None,
                    "draft": None,
                    "transcript": [],
                    "candidate": None,
                    "review": None,
                    "stale_reason": "VISION_FACT_CONFLICT",
                }
            )
        vision = accepted_current_vision(snapshot)
        transcript: list[JsonValue] = [
            {"user_text": item.user_text}
            for item in selection.transcript
            if item.user_text is not None
        ]
        latest_turn = selection.transcript[-1] if selection.transcript else None
        draft_data: JsonObject | None = (
            _vision_turn_draft_data(latest_turn)
            if latest_turn is not None and not latest_turn.is_complete
            else None
        )
        candidate = (
            selection.artifact
            if selection.decision is None
            or selection.decision.decision in {"feedback", "rejected"}
            else None
        )
        current_data: JsonObject | None = (
            None if vision is None else {"statement": vision.statement}
        )
        candidate_data: JsonObject | None = (
            None if candidate is None else _vision_candidate_data(candidate)
        )
        data: JsonObject = {
            "bootstrap_available": not selection.transcript
            and (selection.artifact is None or selection.open_revision is not None),
            "current": current_data,
            "draft": draft_data,
            "transcript": transcript,
            "candidate": candidate_data,
            "review": _vision_review_data(
                selection.decision,
                pending=candidate is not None and selection.decision is None,
            ),
            "stale_reason": None if vision is not None else "VISION_NOT_ACCEPTED",
        }
        return _success(data)

    def product_goal_status(self, *, project_id: int) -> JsonObject:
        """Project Goal review, interview, accepted Vision, and outcome state."""
        snapshot = self._snapshot(project_id)
        if isinstance(snapshot, dict):
            return snapshot
        selection = select_product_goal_interview_state(snapshot)
        accepted_vision = selection.vision
        accepted_vision_data: JsonObject | None = (
            None
            if accepted_vision is None
            else {
                "vision_artifact_id": accepted_vision.vision_artifact_id,
                "fingerprint": accepted_vision.content_fingerprint,
                "statement": accepted_vision.statement,
            }
        )
        if selection.conflict:
            return _success(
                {
                    "accepted_vision": accepted_vision_data,
                    "active": None,
                    "transcript": [],
                    "latest_questions": [],
                    "candidate": None,
                    "review": None,
                    "outcome": None,
                    "stale_reason": "PRODUCT_GOAL_FACT_CONFLICT",
                }
            )
        goal = selection.active
        transcript: list[JsonValue] = [
            _goal_turn_data(item) for item in selection.transcript
        ]
        latest_questions: list[JsonValue] = (
            []
            if not selection.transcript
            else list(selection.transcript[-1].clarifying_questions)
        )
        candidate_data = (
            None
            if selection.candidate is None
            else _goal_candidate_data(selection.candidate, snapshot)
        )
        if selection.candidate is not None and candidate_data is None:
            return _success(
                {
                    "accepted_vision": accepted_vision_data,
                    "active": None,
                    "transcript": [],
                    "latest_questions": [],
                    "candidate": None,
                    "review": None,
                    "outcome": None,
                    "stale_reason": "PRODUCT_GOAL_FACT_CONFLICT",
                }
            )
        resolved = _latest_resolved_goal(snapshot)
        active_data: JsonObject | None = (
            None
            if goal is None
            else {
                "product_goal_artifact_id": goal.product_goal_artifact_id,
                "fingerprint": goal.content_fingerprint,
                "statement": goal.statement,
                "goal_number": goal.goal_number,
                "revision_number": goal.revision_number,
            }
        )
        outcome_data: JsonObject | None = (
            None if goal is not None or resolved is None else resolved[1]
        )
        data: JsonObject = {
            "accepted_vision": accepted_vision_data,
            "active": active_data,
            "transcript": transcript,
            "latest_questions": latest_questions,
            "candidate": candidate_data,
            "review": _goal_review_data(
                selection.decision,
                pending=(
                    selection.candidate is not None and selection.decision is None
                ),
            ),
            "outcome": outcome_data,
            "stale_reason": (
                None
                if goal is not None
                else ("GOAL_NOT_ACTIVE" if resolved is None else "GOAL_RESOLVED")
            ),
        }
        return _success(data)

    def specification_status(self, *, project_id: int) -> JsonObject:
        """Project one complete current v2 review packet and accepted row."""
        snapshot = self._snapshot(project_id)
        if isinstance(snapshot, dict):
            return snapshot
        selection = select_product_definition_state(snapshot)
        source = (
            None
            if selection.specification_source is None
            else _specification_source_data(selection.specification_source)
        )
        candidate = selection.specification_candidate
        spec = selection.accepted_spec
        if candidate is None:
            current: JsonObject | None = None
            if spec is not None:
                accepted_candidate = next(
                    (
                        item
                        for item in snapshot.specification_candidates
                        if (
                            item.specification_candidate_id,
                            item.candidate_fingerprint,
                        )
                        == (
                            spec.source_specification_candidate_id,
                            spec.source_specification_candidate_fingerprint,
                        )
                    ),
                    None,
                )
                if accepted_candidate is None:
                    return _error(
                        "SPECIFICATION_CANDIDATE_UNAVAILABLE",
                        "Accepted Specification candidate is unavailable.",
                        project_id=project_id,
                        spec_version_id=spec.spec_version_id,
                    )
                accepted_projection = self._specification_projection(
                    project_id=project_id,
                    candidate=accepted_candidate,
                    spec=spec,
                    decision_state="accepted",
                )
                if isinstance(accepted_projection, _SpecificationReadFailure):
                    return accepted_projection.error
                if accepted_projection.registry is not None:
                    current = _specification_registry_data(
                        accepted_projection.registry,
                        candidate=accepted_projection.candidate,
                    )
            return _success(
                {
                    "schema_version": _SPECIFICATION_REVIEW_SCHEMA_VERSION,
                    "source": source,
                    "current": current,
                    "candidate": None,
                    "review": None,
                    "stale_reason": (
                        "SPECIFICATION_FACT_CONFLICT"
                        if (
                            selection.specification_candidate_conflict
                            or selection.accepted_spec_conflict
                        )
                        else (
                            "SPECIFICATION_SOURCE_NOT_REGISTERED"
                            if source is None
                            else "SPECIFICATION_NOT_STRUCTURED"
                        )
                    ),
                }
            )
        decisions = [
            decision
            for decision in snapshot.specification_decisions
            if decision.specification_candidate_id
            == candidate.specification_candidate_id
        ]
        review = _specification_review_data(decisions)
        decision_state = (
            "conflict" if review is None else str(review.get("state", "pending"))
        )
        projection = self._specification_projection(
            project_id=project_id,
            candidate=candidate,
            spec=(
                spec
                if spec is not None
                and (
                    spec.source_specification_candidate_id,
                    spec.source_specification_candidate_fingerprint,
                )
                == (
                    candidate.specification_candidate_id,
                    candidate.candidate_fingerprint,
                )
                else None
            ),
            decision_state=decision_state,
        )
        if isinstance(projection, _SpecificationReadFailure):
            return projection.error
        return _success(
            {
                "schema_version": _SPECIFICATION_REVIEW_SCHEMA_VERSION,
                "source": source,
                "current": (
                    None
                    if projection.registry is None
                    else _specification_registry_data(
                        projection.registry,
                        candidate=projection.candidate,
                    )
                ),
                "candidate": projection.candidate,
                "review": review,
                "stale_reason": (
                    "SPECIFICATION_FACT_CONFLICT"
                    if selection.accepted_spec_conflict
                    else (None if spec is not None else "SPECIFICATION_NOT_APPROVED")
                ),
            }
        )

    def specification_review(self, *, project_id: int) -> JsonObject:
        """Project pending or terminal state over the complete v2 review packet."""
        snapshot = self._snapshot(project_id)
        if isinstance(snapshot, dict):
            return snapshot
        selection = select_product_definition_state(snapshot)
        source = (
            None
            if selection.specification_source is None
            else _specification_source_data(selection.specification_source)
        )
        candidate = selection.specification_candidate
        if candidate is None:
            return _success(
                {
                    "schema_version": _SPECIFICATION_REVIEW_SCHEMA_VERSION,
                    "source": source,
                    "candidate": None,
                    "review": None,
                    "stale_reason": (
                        "SPECIFICATION_FACT_CONFLICT"
                        if selection.specification_candidate_conflict
                        else (
                            "SPECIFICATION_SOURCE_NOT_REGISTERED"
                            if source is None
                            else "SPECIFICATION_NOT_STRUCTURED"
                        )
                    ),
                }
            )
        decisions = [
            decision
            for decision in snapshot.specification_decisions
            if (
                decision.specification_candidate_id
                == candidate.specification_candidate_id
            )
        ]
        review = _specification_review_data(decisions)
        decision_state = (
            "conflict" if review is None else str(review.get("state", "pending"))
        )
        projection = self._specification_projection(
            project_id=project_id,
            candidate=candidate,
            spec=(
                selection.accepted_spec
                if selection.accepted_spec is not None
                and (
                    selection.accepted_spec.source_specification_candidate_id,
                    selection.accepted_spec.source_specification_candidate_fingerprint,
                )
                == (
                    candidate.specification_candidate_id,
                    candidate.candidate_fingerprint,
                )
                else None
            ),
            decision_state=decision_state,
        )
        if isinstance(projection, _SpecificationReadFailure):
            return projection.error
        return _success(
            {
                "schema_version": _SPECIFICATION_REVIEW_SCHEMA_VERSION,
                "source": source,
                "candidate": projection.candidate,
                "review": review,
                "stale_reason": (
                    "SPECIFICATION_REVIEW_CONFLICT"
                    if review is None
                    else (
                        None if review["state"] == "pending" else "CANDIDATE_REVIEWED"
                    )
                ),
            }
        )

    def artifact_history(
        self,
        *,
        project_id: int,
        node_id: str,
        instance_key: str | None = None,
    ) -> JsonObject:
        """Return durable attempt and outcome history for one exact node."""
        project_or_error = self._project(project_id)
        if isinstance(project_or_error, _ProjectReadFailure):
            return project_or_error.error
        with Session(self._engine) as session:
            statement = select(WorkflowNodeAttempt).where(
                col(WorkflowNodeAttempt.project_id) == project_id,
                col(WorkflowNodeAttempt.node_id) == node_id,
            )
            if instance_key is not None:
                statement = statement.where(
                    col(WorkflowNodeAttempt.instance_key) == instance_key
                )
            attempts = session.exec(
                statement.order_by(
                    col(WorkflowNodeAttempt.workflow_node_attempt_id).desc()
                )
            ).all()
            outcomes = session.exec(
                select(WorkflowNodeAttemptOutcome).where(
                    col(WorkflowNodeAttemptOutcome.project_id) == project_id
                )
            ).all()
        outcome_by_attempt = {item.workflow_node_attempt_id: item for item in outcomes}
        items: list[JsonValue] = []
        for attempt in attempts:
            attempt_id = attempt.workflow_node_attempt_id
            outcome = (
                outcome_by_attempt.get(attempt_id) if attempt_id is not None else None
            )
            output: JsonObject | None = None
            if outcome is not None and outcome.output_json is not None:
                output = _JSON_OBJECT.validate_json(outcome.output_json)
            items.append(
                {
                    "attempt_id": attempt_id,
                    "node_id": attempt.node_id,
                    "instance_key": attempt.instance_key,
                    "decision_fingerprint": attempt.decision_fingerprint,
                    "input_fingerprint": attempt.input_fingerprint,
                    "model_id": attempt.model_id,
                    "actor": attempt.actor,
                    "correlation_id": attempt.correlation_id,
                    "started_at": _iso(attempt.started_at),
                    "lease_expires_at": _iso(attempt.lease_expires_at),
                    "status": outcome.status if outcome is not None else "in_progress",
                    "output": output,
                    "output_fingerprint": (
                        outcome.output_fingerprint if outcome is not None else None
                    ),
                    "failure_code": (
                        outcome.failure_code if outcome is not None else None
                    ),
                    "failure_message": (
                        outcome.failure_message if outcome is not None else None
                    ),
                }
            )
        return _success(
            {
                "project_id": project_id,
                "node_id": node_id,
                "instance_key": instance_key,
                "items": items,
                "count": len(items),
            }
        )

    def story_show(self, *, story_id: int) -> JsonObject:
        """Return one durable Story record with canonical readiness facts."""
        with Session(self._engine) as session:
            story = session.get(UserStory, story_id)
            if story is None:
                return _error(
                    "STORY_NOT_FOUND",
                    f"Story {story_id} was not found.",
                    story_id=story_id,
                )
            artifact = session.get(StoryArtifact, story.source_story_artifact_id)
            validation_evidence: JsonObject | None = None
            if story.validation_evidence is not None:
                try:
                    evidence = ValidationEvidence.model_validate_json(
                        story.validation_evidence,
                        strict=True,
                    )
                    if (
                        canonical_json(evidence.model_dump(mode="json"))
                        == story.validation_evidence
                    ):
                        validation_evidence = evidence.model_dump(mode="json")
                except ValidationError:
                    validation_evidence = None
        snapshot_or_error = self._snapshot(story.project_id)
        if isinstance(snapshot_or_error, dict):
            return snapshot_or_error
        fact = next(
            (item for item in snapshot_or_error.stories if item.story_id == story_id),
            None,
        )
        if fact is None:
            return _error(
                "PROJECT_FACTS_UNAVAILABLE",
                "Stored Story is unavailable from the canonical workflow projection.",
                story_id=story_id,
            )
        try:
            acceptance_criteria = _canonical_acceptance_criteria(
                story.acceptance_criteria_json
            )
        except ValueError:
            return _error(
                "ACCEPTANCE_CRITERIA_INVALID",
                "Stored Story acceptance criteria are invalid.",
                story_id=story_id,
            )
        acceptance_criteria_value: list[JsonValue] = list(acceptance_criteria)
        return _success(
            {
                "story_id": story_id,
                "project_id": story.project_id,
                "title": story.title,
                "description": story.story_description,
                "acceptance_criteria": acceptance_criteria_value,
                "spec_item_ids": list(fact.spec_item_ids),
                "status": fact.status,
                "story_points": story.story_points,
                "rank": story.rank,
                "source_story_item_id": story.source_story_item_id,
                "source_story_artifact_id": story.source_story_artifact_id,
                "backlog_item_id": (
                    artifact.backlog_item_id if artifact is not None else None
                ),
                "is_superseded": story.is_superseded,
                "validation_evidence": validation_evidence,
                "structurally_eligible": fact.structurally_eligible,
                "structural_eligibility_status": fact.structural_eligibility_status,
                "structural_failures": list(fact.validation_failures),
                "sprint_selection_state": fact.sprint_selection_state,
                "sprint_selection_state_fingerprint": (
                    fact.sprint_selection_state_fingerprint
                ),
                "sprint_selection_event_id": fact.sprint_selection_event_id,
                "sprint_selection_event_fingerprint": (
                    fact.sprint_selection_event_fingerprint
                ),
                "selected_scope_fingerprint": fact.selected_scope_fingerprint,
                "dependency_safe": fact.dependency_safe,
                "sprint_candidate": fact.sprint_candidate,
                "readiness_blockers": list(fact.readiness_blockers),
                "structural_evidence_scope": structural_evidence_scope_payload(),
                "updated_at": _iso(story.updated_at),
            }
        )

    def _backlog_item_requirements(
        self,
        backlog_artifact_id: int,
        *,
        expected_fingerprint: str,
    ) -> dict[str, str]:
        with Session(self._engine) as session:
            backlog_artifact = session.get(BacklogArtifact, backlog_artifact_id)
            if backlog_artifact is None:
                return {}
            _parsed, content = load_stored_planning_artifact_content(
                backlog_artifact.canonical_content_json,
                expected_fingerprint=expected_fingerprint,
                content_type=BacklogOutput,
            )
            return {
                item.backlog_item_id: item.requirement for item in content.backlog_items
            }

    def story_pending(self, *, project_id: int) -> JsonObject:
        """List accepted Backlog requirements and their durable Story coverage."""
        snapshot_or_error = self._snapshot(project_id)
        if isinstance(snapshot_or_error, dict):
            return snapshot_or_error
        snapshot = snapshot_or_error
        backlog_lineage = current_backlog_lineage(snapshot)
        if backlog_lineage.conflict:
            return _error(
                "PROJECT_FACTS_UNAVAILABLE",
                "Stored Backlog artifact lineage is invalid.",
                project_id=project_id,
                reason="BACKLOG_LINEAGE_INVALID",
            )
        backlog = backlog_lineage.backlog
        if backlog is None:
            return _success(
                {
                    "project_id": project_id,
                    "items": [],
                    "count": 0,
                    "pending_count": 0,
                }
            )
        try:
            story_artifacts = _current_story_artifacts(
                snapshot,
                backlog_artifact_id=backlog.artifact_id,
                backlog_artifact_fingerprint=backlog.artifact_fingerprint,
            )
        except PlanningLineageError as error:
            return _error(
                "PROJECT_FACTS_UNAVAILABLE",
                "Stored Story artifact lineage is invalid.",
                project_id=project_id,
                reason=error.code.value,
            )
        try:
            requirement_text_by_id = self._backlog_item_requirements(
                backlog.artifact_id,
                expected_fingerprint=backlog.artifact_fingerprint,
            )
        except (TypeError, ValidationError, ValueError):
            return _error(
                "PROJECT_FACTS_UNAVAILABLE",
                "Stored Backlog artifact canonical content is invalid.",
                project_id=project_id,
                reason="BACKLOG_CONTENT_INVALID",
            )
        items: list[JsonValue] = []
        pending_count = 0
        for requirement in snapshot.backlog_items:
            if (
                requirement.backlog_artifact_id != backlog.artifact_id
                or requirement.backlog_artifact_fingerprint
                != backlog.artifact_fingerprint
            ):
                continue
            artifact = story_artifacts.get(
                (requirement.backlog_artifact_id, requirement.backlog_item_id)
            )
            status = artifact.status if artifact is not None else "pending"
            if status != "accepted":
                pending_count += 1
            items.append(
                {
                    "backlog_item_id": requirement.backlog_item_id,
                    "backlog_artifact_id": requirement.backlog_artifact_id,
                    "requirement": requirement_text_by_id.get(
                        requirement.backlog_item_id, ""
                    ),
                    "spec_item_ids": list(requirement.spec_item_ids),
                    "priority": requirement.priority,
                    "status": status,
                    "story_artifact_id": (
                        artifact.artifact_id if artifact is not None else None
                    ),
                    "story_item_ids": (
                        list(artifact.story_item_ids) if artifact is not None else []
                    ),
                }
            )
        return _success(
            {
                "project_id": project_id,
                "items": items,
                "count": len(items),
                "pending_count": pending_count,
            }
        )

    def story_dependencies_inspect(self, *, project_id: int) -> JsonObject:
        """Return durable dependency edges and reviewed sets."""
        snapshot_or_error = self._snapshot(project_id)
        if isinstance(snapshot_or_error, dict):
            return snapshot_or_error
        snapshot = snapshot_or_error
        active_stories = tuple(
            item for item in snapshot.stories if not item.is_superseded
        )
        selected = selected_scope_stories(snapshot)
        scope_fingerprints = {
            item.selected_scope_fingerprint for item in active_stories
        }
        if len(scope_fingerprints) > 1 or None in scope_fingerprints:
            return _error(
                "PROJECT_FACTS_UNAVAILABLE",
                "Current selected Story scope fingerprint is unavailable.",
                project_id=project_id,
            )
        selected_scope_fingerprint = (
            None if not scope_fingerprints else next(iter(scope_fingerprints))
        )
        return _success(
            {
                "project_id": project_id,
                "edges": [
                    _validated(item.model_dump(mode="json"))
                    for item in snapshot.story_dependencies
                ],
                "reviews": [
                    _validated(item.model_dump(mode="json"))
                    for item in snapshot.story_dependency_reviews
                ],
                "stories": [
                    _validated(item.model_dump(mode="json"))
                    for item in active_stories
                ],
                "selected_story_ids": [item.story_id for item in selected],
                "selected_scope_fingerprint": selected_scope_fingerprint,
                "structural_evidence_scope": structural_evidence_scope_payload(),
            }
        )

    def sprint_candidates(self, *, project_id: int) -> JsonObject:
        """Return Story facts currently eligible for Sprint planning."""
        with Session(self._engine) as session:
            try:
                owner = resolve_sprint_owner(
                    session,
                    project_id=project_id,
                    team_name=None,
                )
            except SprintOwnerResolutionError as error:
                return _error(
                    error.code.value,
                    str(error),
                    project_id=project_id,
                )
        snapshot_or_error = self._snapshot(project_id)
        if isinstance(snapshot_or_error, dict):
            return snapshot_or_error
        capacity = build_sprint_capacity_state(
            build_durable_sprint_metrics(snapshot_or_error)
        )
        items: list[JsonValue] = [
            _validated(item.model_dump(mode="json"))
            for item in snapshot_or_error.stories
            if item.sprint_candidate
        ]
        return _success(
            {
                "project_id": project_id,
                "items": items,
                "count": len(items),
                "capacity": capacity,
                "sprint_owner": {
                    **sprint_owner_projection(owner, project_id=project_id),
                    "named_team_override_allowed": True,
                },
                "structural_evidence_scope": structural_evidence_scope_payload(),
            }
        )

    def sprint_plan_review(  # noqa: C901, PLR0911
        self,
        *,
        project_id: int,
        sprint_plan_artifact_id: int,
    ) -> JsonObject:
        """Return one immutable Sprint-plan review with pinned evidence."""
        with Session(self._engine) as session:
            if session.get(Project, project_id) is None:
                return _error(
                    "PROJECT_NOT_FOUND",
                    "Project was not found.",
                    project_id=project_id,
                )
            artifact = session.get(SprintPlanArtifact, sprint_plan_artifact_id)
            if artifact is None or artifact.project_id != project_id:
                return _error(
                    "SPRINT_PLAN_ARTIFACT_NOT_FOUND",
                    "Sprint plan artifact was not found.",
                    project_id=project_id,
                    sprint_plan_artifact_id=sprint_plan_artifact_id,
                )
            try:
                envelope = load_bound_sprint_plan_envelope(
                    artifact.canonical_task_plan_json,
                    expected_fingerprint=artifact.plan_fingerprint,
                    spec_version_id=artifact.spec_version_id,
                    spec_hash=artifact.spec_hash,
                    candidate_set_fingerprint=artifact.candidate_set_fingerprint,
                    selected_story_ids_json=artifact.selected_story_ids_json,
                )
                owner = load_sprint_owner_evidence(
                    session,
                    artifact=artifact,
                    owner_label=envelope.team_name,
                )
                specification = load_accepted_specification(
                    session,
                    project_id=project_id,
                    spec_version_id=artifact.spec_version_id,
                    spec_hash=artifact.spec_hash,
                )
            except (
                AcceptedSpecificationIntegrityError,
                SprintOwnerEvidenceError,
                ValidationError,
                ValueError,
            ):
                return _error(
                    "PLANNING_ARTIFACT_CONTENT_INVALID",
                    "Sprint plan artifact content is invalid.",
                    project_id=project_id,
                    sprint_plan_artifact_id=sprint_plan_artifact_id,
                )

            artifacts = session.exec(
                select(SprintPlanArtifact).where(
                    col(SprintPlanArtifact.project_id) == project_id
                )
            ).all()
            decisions = {
                row.sprint_plan_artifact_id: row
                for row in session.exec(
                    select(SprintPlanArtifactDecision).where(
                        col(SprintPlanArtifactDecision.project_id) == project_id
                    )
                ).all()
            }
            try:
                lineage_decisions: dict[int, PlanningLineageDecision] = {
                    artifact_id: cast("PlanningLineageDecision", row.decision)
                    for artifact_id, row in decisions.items()
                }
                validate_artifact_lineage(
                    tuple(
                        ArtifactLineageNode(
                            artifact_id=int(row.sprint_plan_artifact_id),
                            chain_key=(
                                row.project_id,
                                row.spec_version_id,
                                row.spec_hash,
                                row.sprint_plan_stream_id,
                            ),
                            version_number=row.version_number,
                            supersedes_artifact_id=(
                                row.supersedes_sprint_plan_artifact_id
                            ),
                            decision=lineage_decisions.get(
                                int(row.sprint_plan_artifact_id)
                            ),
                        )
                        for row in artifacts
                        if row.sprint_plan_artifact_id is not None
                    )
                )
            except PlanningLineageError:
                return _error(
                    "PLANNING_ARTIFACT_LINEAGE_INVALID",
                    "Sprint plan artifact lineage is invalid.",
                    project_id=project_id,
                    sprint_plan_artifact_id=sprint_plan_artifact_id,
                )

            decision = decisions.get(sprint_plan_artifact_id)
            if decision is None:
                current_spec = load_current_accepted_specification(
                    session,
                    project_id=project_id,
                )
                if (
                    current_spec is None
                    or current_spec.spec_version_id != artifact.spec_version_id
                    or current_spec.spec_hash != artifact.spec_hash
                ):
                    return _error(
                        "STALE_SPECIFICATION",
                        (
                            "Sprint plan review requires the current accepted "
                            "Specification."
                        ),
                        project_id=project_id,
                        sprint_plan_artifact_id=sprint_plan_artifact_id,
                    )
                try:
                    snapshot = WorkflowFactRepository(session).load(project_id)
                except WorkflowFactLoadError:
                    return _error(
                        "PLANNING_ARTIFACT_LINEAGE_INVALID",
                        "Sprint plan artifact lineage is invalid.",
                        project_id=project_id,
                        sprint_plan_artifact_id=sprint_plan_artifact_id,
                    )
                candidates = tuple(
                    item for item in snapshot.stories if item.sprint_candidate
                )
                if (
                    candidate_set_fingerprint(
                        candidates,
                        snapshot.story_dependencies,
                    )
                    != artifact.candidate_set_fingerprint
                ):
                    return _error(
                        "SPRINT_PLAN_REVIEW_SOURCE_STALE",
                        "Sprint plan review source changed. Draft a new Sprint plan.",
                        project_id=project_id,
                        sprint_plan_artifact_id=sprint_plan_artifact_id,
                    )

            story_rows = {
                row.story_id: row
                for row in session.exec(
                    select(UserStory).where(col(UserStory.project_id) == project_id)
                ).all()
            }
            selected_stories: list[JsonValue] = []
            try:
                for selected in envelope.planner_output.selected_stories:
                    story = story_rows[selected.story_id]
                    if (
                        story.source_story_item_id != selected.story_item_id
                        or story.source_story_artifact_id is None
                        or story.source_story_artifact_fingerprint is None
                        or story.source_story_item_fingerprint is None
                    ):
                        message = "Selected Story identity changed."
                        raise ValueError(message)  # noqa: TRY301
                    story_record = _load_story_review_record(
                        session,
                        project_id=project_id,
                        story_artifact_id=story.source_story_artifact_id,
                    )
                    source_specification = story_record.roadmap.backlog.specification
                    source_items = tuple(
                        item
                        for item in story_record.content.story_items
                        if item.item.story_item_id == selected.story_item_id
                    )
                    if (
                        story_record.artifact.content_fingerprint
                        != story.source_story_artifact_fingerprint
                        or story_record.decision is None
                        or story_record.decision.decision != "accepted"
                        or source_specification.spec_version_id
                        != artifact.spec_version_id
                        or source_specification.spec_hash != artifact.spec_hash
                        or len(source_items) != 1
                        or source_items[0].item_fingerprint
                        != story.source_story_item_fingerprint
                    ):
                        message = "Selected Story immutable source changed."
                        raise ValueError(message)  # noqa: TRY301
                    source_item = source_items[0].item
                    story_evidence = _specification_evidence(
                        specification,
                        source_item.spec_item_ids,
                    )
                    tasks: list[JsonValue] = [
                        {
                            "description": task.description,
                            "task_kind": task.task_kind,
                            "artifact_targets": list(task.artifact_targets),
                            "workstream_tags": list(task.workstream_tags),
                            "checklist_items": list(task.checklist_items),
                            "specification_evidence": _specification_evidence(
                                specification,
                                task.relevant_spec_item_ids,
                            ),
                        }
                        for task in selected.tasks
                    ]
                    selected_stories.append(
                        {
                            "story_id": selected.story_id,
                            "story_artifact_id": story.source_story_artifact_id,
                            "story_artifact_fingerprint": (
                                story.source_story_artifact_fingerprint
                            ),
                            "story_item_id": story.source_story_item_id,
                            "title": source_item.story_title,
                            "statement": source_item.statement,
                            "persona": source_item.persona,
                            "acceptance_criteria": list(
                                source_item.acceptance_criteria
                            ),
                            "invest_assessment": (
                                source_item.invest_assessment.model_dump(mode="json")
                            ),
                            "specification_evidence": story_evidence,
                            "reason_for_selection": selected.reason_for_selection,
                            "tasks": tasks,
                        }
                    )
            except (
                KeyError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
                _PlanningArtifactProjectionError,
            ):
                return _error(
                    "PLANNING_ARTIFACT_CONTENT_INVALID",
                    "Sprint plan artifact content is invalid.",
                    project_id=project_id,
                    sprint_plan_artifact_id=sprint_plan_artifact_id,
                )

            review: JsonObject = {
                "state": "pending" if decision is None else decision.decision,
                "rationale": None if decision is None else decision.rationale,
                "reviewer": None if decision is None else decision.reviewer,
                "decided_at": None if decision is None else _iso(decision.decided_at),
                "activated_sprint_id": (
                    None if decision is None else decision.activated_sprint_id
                ),
            }
            return _success(
                {
                    "schema_version": "agileforge.planning-artifact-review.v2",
                    "phase": "sprint_plan",
                    "project_id": project_id,
                    "lineage": {
                        "specification": {
                            "spec_version_id": specification.spec_version_id,
                            "spec_hash": specification.spec_hash,
                            "status": specification.status,
                        },
                        "sprint_plan": {
                            "sprint_plan_stream_id": artifact.sprint_plan_stream_id,
                            "version_number": artifact.version_number,
                            "supersedes_sprint_plan_artifact_id": (
                                artifact.supersedes_sprint_plan_artifact_id
                            ),
                        },
                    },
                    "candidate": {
                        "sprint_plan_artifact_id": sprint_plan_artifact_id,
                        "artifact_fingerprint": artifact.plan_fingerprint,
                        "candidate_set_fingerprint": (
                            artifact.candidate_set_fingerprint
                        ),
                        "created_by": artifact.created_by,
                        "created_at": _iso(artifact.created_at),
                        "team_name": envelope.team_name,
                        "sprint_owner": sprint_owner_projection(
                            owner,
                            project_id=project_id,
                        ),
                        "sprint_goal": envelope.planner_output.sprint_goal,
                        "selected_stories": selected_stories,
                    },
                    "review": review,
                }
            )

    def sprint_history(self, *, project_id: int) -> JsonObject:
        """Combine durable Sprint-plan attempts with execution lifecycle facts."""
        attempts = self.artifact_history(
            project_id=project_id,
            node_id="planning.sprint.plan",
        )
        if attempts.get("ok") is not True:
            return attempts
        snapshot_or_error = self._snapshot(project_id)
        if isinstance(snapshot_or_error, dict):
            return snapshot_or_error
        return _success(
            {
                "project_id": project_id,
                "attempts": _result_data(attempts).get("items", []),
                "sprints": [
                    _validated(item.model_dump(mode="json"))
                    for item in snapshot_or_error.sprints
                ],
            }
        )

    def sprint_metrics(self, *, project_id: int) -> JsonObject:
        """Return deterministic Sprint metrics and capacity recommendation."""
        snapshot_or_error = self._snapshot(project_id)
        if isinstance(snapshot_or_error, dict):
            return snapshot_or_error
        snapshot = snapshot_or_error
        metrics = _validated(build_durable_sprint_metrics(snapshot))
        metrics.update(
            {
                "sprint_count": len(snapshot.sprints),
                "completed_sprint_count": sum(
                    item.status == "completed" for item in snapshot.sprints
                ),
                "task_count": len(snapshot.tasks),
                "completed_task_count": len(snapshot.task_completions),
                "story_completion_count": len(snapshot.story_completions),
            }
        )
        return _success(metrics)

    def sprint_status(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        """Return one selected Sprint and its durable execution facts."""
        snapshot_or_error = self._snapshot(project_id)
        if isinstance(snapshot_or_error, dict):
            return snapshot_or_error
        snapshot = snapshot_or_error
        sprint = self._select_sprint(snapshot.sprints, sprint_id)
        if sprint is None:
            return _error(
                "SPRINT_NOT_FOUND",
                "No matching Sprint was found.",
                project_id=project_id,
                sprint_id=sprint_id,
            )
        selected_id = sprint.sprint_id
        accepted_plan = self._accepted_sprint_plan_status(
            project_id=project_id,
            sprint=sprint,
            snapshot=snapshot,
        )
        if accepted_plan.get("ok") is not True:
            return accepted_plan
        return _success(
            {
                "project_id": project_id,
                "sprint": _validated(sprint.model_dump(mode="json")),
                "accepted_plan": _result_data(accepted_plan),
                "start": next(
                    (
                        _validated(item.model_dump(mode="json"))
                        for item in snapshot.sprint_starts
                        if item.sprint_id == selected_id
                    ),
                    None,
                ),
                "tasks": [
                    {
                        **_validated(item.model_dump(mode="json")),
                        "fact_fingerprint": canonical_hash(
                            item.model_dump(mode="json")
                        ),
                    }
                    for item in snapshot.tasks
                    if item.sprint_id == selected_id
                ],
                "review": next(
                    (
                        _validated(item.model_dump(mode="json"))
                        for item in snapshot.sprint_reviews
                        if item.sprint_id == selected_id
                    ),
                    None,
                ),
                "closure": next(
                    (
                        _validated(item.model_dump(mode="json"))
                        for item in snapshot.sprint_closures
                        if item.sprint_id == selected_id
                    ),
                    None,
                ),
            }
        )

    def _accepted_sprint_plan_status(  # noqa: C901, PLR0911, PLR0912, PLR0915
        self,
        *,
        project_id: int,
        sprint: SprintFact,
        snapshot: WorkflowFactSnapshot,
    ) -> JsonObject:
        """Project one accepted plan only when all Sprint lineage still agrees."""
        sprint_id = sprint.sprint_id
        starts = tuple(
            item for item in snapshot.sprint_starts if item.sprint_id == sprint_id
        )
        if sprint.status == "planned":
            plans = tuple(
                item
                for item in snapshot.planning_artifacts
                if item.artifact_type == "sprint_plan"
                and item.activated_sprint_id == sprint_id
                and item.status == "accepted"
            )
            if len(plans) != 1 or starts:
                return self._sprint_status_inconsistent(project_id, sprint_id)
            plan = plans[0]
            decisions = tuple(
                item
                for item in snapshot.review_decisions
                if item.artifact_type == "sprint"
                and item.artifact_id == plan.artifact_id
                and item.artifact_fingerprint == plan.artifact_fingerprint
                and item.decision == "accepted"
            )
            if len(decisions) != 1:
                return self._sprint_status_inconsistent(project_id, sprint_id)
            decision = decisions[0]
            if (
                current_task_content_fingerprint(
                    snapshot.tasks,
                    sprint_id=sprint_id,
                    story_ids=plan.selected_story_ids,
                )
                != plan.task_content_fingerprint
            ):
                return self._sprint_status_inconsistent(project_id, sprint_id)
        else:
            if len(starts) != 1:
                return self._sprint_status_inconsistent(project_id, sprint_id)
            try:
                contract = execution_contract(snapshot, sprint_id)
            except ExecutionIntegrityError:
                return self._sprint_status_inconsistent(project_id, sprint_id)
            plan = contract.plan
            decision = contract.decision
        if (
            plan.candidate_set_fingerprint is None
            or plan.task_content_fingerprint is None
            or not plan.selected_story_ids
        ):
            return self._sprint_status_inconsistent(project_id, sprint_id)

        review_result = self.sprint_plan_review(
            project_id=project_id,
            sprint_plan_artifact_id=plan.artifact_id,
        )
        if review_result.get("ok") is not True:
            return self._sprint_status_inconsistent(project_id, sprint_id)
        review_data = _result_data(review_result)
        candidate = review_data.get("candidate")
        review = review_data.get("review")
        if not isinstance(candidate, dict) or not isinstance(review, dict):
            return self._sprint_status_inconsistent(project_id, sprint_id)

        selected = candidate.get("selected_stories")
        owner = candidate.get("sprint_owner")
        goal = candidate.get("sprint_goal")
        if (
            candidate.get("sprint_plan_artifact_id") != plan.artifact_id
            or candidate.get("artifact_fingerprint") != plan.artifact_fingerprint
            or candidate.get("candidate_set_fingerprint")
            != plan.candidate_set_fingerprint
            or review.get("state") != "accepted"
            or review.get("activated_sprint_id") != sprint_id
            or not isinstance(goal, str)
            or not goal.strip()
            or not isinstance(owner, dict)
            or not isinstance(selected, list)
        ):
            return self._sprint_status_inconsistent(project_id, sprint_id)

        with Session(self._engine) as session:
            sprint_row = session.get(Sprint, sprint_id)
            team_row = (
                None
                if sprint_row is None
                else session.get(Team, sprint_row.team_id)
            )
        if (
            sprint_row is None
            or sprint_row.project_id != project_id
            or sprint_row.goal != goal
            or team_row is None
            or team_row.name != owner.get("label")
        ):
            return self._sprint_status_inconsistent(project_id, sprint_id)

        stories_by_id = {item.story_id: item for item in snapshot.stories}
        tasks_by_story: dict[int, int] = {}
        for task in snapshot.tasks:
            if task.sprint_id == sprint_id:
                tasks_by_story[task.story_id] = tasks_by_story.get(task.story_id, 0) + 1
        summaries: list[JsonValue] = []
        selected_ids: list[int] = []
        total_points = 0
        total_tasks = 0
        for item in selected:
            if not isinstance(item, dict):
                return self._sprint_status_inconsistent(project_id, sprint_id)
            story_id = item.get("story_id")
            title = item.get("title")
            item_id = item.get("story_item_id")
            planned_tasks = item.get("tasks")
            story = stories_by_id.get(story_id) if isinstance(story_id, int) else None
            task_count = tasks_by_story.get(story.story_id, 0) if story else 0
            if (
                story is None
                or sprint_id not in story.sprint_ids
                or story.source_story_item_id != item_id
                or not isinstance(title, str)
                or not title.strip()
                or not isinstance(planned_tasks, list)
                or len(planned_tasks) != task_count
                or not isinstance(story.story_points, int)
                or story.story_points <= 0
            ):
                return self._sprint_status_inconsistent(project_id, sprint_id)
            selected_ids.append(story.story_id)
            total_points += story.story_points
            total_tasks += task_count
            summaries.append(
                {
                    "story_id": story.story_id,
                    "story_item_id": story.source_story_item_id,
                    "title": title,
                    "story_points": story.story_points,
                    "task_count": task_count,
                }
            )
        if tuple(selected_ids) != plan.selected_story_ids:
            return self._sprint_status_inconsistent(project_id, sprint_id)

        return _success(
            {
                "sprint_id": sprint_id,
                "status": sprint.status,
                "goal": goal,
                "owner": owner,
                "sprint_plan_artifact_id": plan.artifact_id,
                "sprint_plan_artifact_decision_id": decision.decision_id,
                "plan_fingerprint": plan.artifact_fingerprint,
                "candidate_set_fingerprint": plan.candidate_set_fingerprint,
                "task_content_fingerprint": plan.task_content_fingerprint,
                "acceptance": {
                    "rationale": review.get("rationale"),
                    "reviewer": review.get("reviewer"),
                    "decided_at": review.get("decided_at"),
                },
                "selected_stories": summaries,
                "total_points": total_points,
                "task_count": total_tasks,
            }
        )

    @staticmethod
    def _sprint_status_inconsistent(project_id: int, sprint_id: int) -> JsonObject:
        return _error(
            "SPRINT_STATUS_INCONSISTENT",
            "Sprint status projection is inconsistent.",
            project_id=project_id,
            sprint_id=sprint_id,
        )

    def sprint_tasks(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        """Return task tickets for one selected Sprint."""
        status = self.sprint_status(project_id=project_id, sprint_id=sprint_id)
        if status.get("ok") is not True:
            return status
        data = _result_data(status)
        sprint = data.get("sprint")
        selected_id = sprint.get("sprint_id") if isinstance(sprint, dict) else None
        tasks = data.get("tasks")
        items = tasks if isinstance(tasks, list) else []
        return _success(
            {
                "project_id": project_id,
                "sprint_id": selected_id,
                "items": items,
                "count": len(items),
            }
        )

    def sprint_task_show(
        self,
        *,
        project_id: int,
        task_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        """Return one durable task ticket and completion evidence."""
        snapshot_or_error = self._snapshot(project_id)
        if isinstance(snapshot_or_error, dict):
            return snapshot_or_error
        snapshot = snapshot_or_error
        task = next(
            (
                item
                for item in snapshot.tasks
                if item.task_id == task_id
                and (sprint_id is None or item.sprint_id == sprint_id)
            ),
            None,
        )
        if task is None:
            return _error(
                "TASK_NOT_FOUND",
                f"Task {task_id} was not found in the selected Sprint.",
                project_id=project_id,
                sprint_id=sprint_id,
                task_id=task_id,
            )
        completion = next(
            (item for item in snapshot.task_completions if item.task_id == task_id),
            None,
        )
        return _success(
            {
                "project_id": project_id,
                "task": _validated(task.model_dump(mode="json")),
                "completion": (
                    _validated(completion.model_dump(mode="json"))
                    if completion is not None
                    else None
                ),
            }
        )

    def sprint_task_history(
        self,
        *,
        project_id: int,
        task_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        """Return retained task logs plus graph completion evidence."""
        detail = self.sprint_task_show(
            project_id=project_id,
            task_id=task_id,
            sprint_id=sprint_id,
        )
        if detail.get("ok") is not True:
            return detail
        with Session(self._engine) as session:
            statement = select(TaskExecutionLog).where(
                col(TaskExecutionLog.task_id) == task_id
            )
            if sprint_id is not None:
                statement = statement.where(
                    col(TaskExecutionLog.sprint_id) == sprint_id
                )
            logs = session.exec(
                statement.order_by(col(TaskExecutionLog.changed_at).desc())
            ).all()
        items: list[JsonValue] = [
            {
                "log_id": item.log_id,
                "task_id": item.task_id,
                "sprint_id": item.sprint_id,
                "old_status": _enum_value(item.old_status),
                "new_status": _enum_value(item.new_status),
                "outcome_summary": item.outcome_summary,
                "artifact_refs_json": item.artifact_refs_json,
                "acceptance_result": _enum_value(item.acceptance_result),
                "notes": item.notes,
                "changed_by": item.changed_by,
                "changed_at": _iso(item.changed_at),
            }
            for item in logs
        ]
        return _success(
            {
                "project_id": project_id,
                "task": _result_data(detail).get("task"),
                "completion": _result_data(detail).get("completion"),
                "items": items,
                "count": len(items),
            }
        )

    def sprint_review(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        """Return review, closure, and triage facts for one Sprint."""
        status = self.sprint_status(project_id=project_id, sprint_id=sprint_id)
        if status.get("ok") is not True:
            return status
        data = _result_data(status)
        sprint = data.get("sprint")
        selected_id = sprint.get("sprint_id") if isinstance(sprint, dict) else None
        snapshot_or_error = self._snapshot(project_id)
        if isinstance(snapshot_or_error, dict):
            return snapshot_or_error
        triage: list[JsonValue] = [
            _validated(item.model_dump(mode="json"))
            for item in snapshot_or_error.post_sprint_triage
            if item.sprint_id == selected_id
        ]
        return _success(
            {
                "project_id": project_id,
                "sprint": sprint,
                "review": data.get("review"),
                "closure": data.get("closure"),
                "triage": triage,
            }
        )

    def task_packet(
        self,
        *,
        project_id: int,
        sprint_id: int,
        task_id: int,
        flavor: str | None = None,
    ) -> JsonObject:
        """Return canonical task_packet.v4 from exact accepted delivery lineage."""
        from services.packets.canonical import (  # noqa: PLC0415
            CanonicalPacketError,
            build_task_packet,
        )

        try:
            with Session(self._engine) as session:
                packet = build_task_packet(
                    session,
                    project_id=project_id,
                    sprint_id=sprint_id,
                    task_id=task_id,
                )
        except CanonicalPacketError as error:
            return _error(error.code, str(error), **error.details)
        return _packet_read(packet, flavor)

    def story_packet(
        self,
        *,
        project_id: int,
        sprint_id: int,
        story_id: int,
        flavor: str | None = None,
    ) -> JsonObject:
        """Return canonical story_packet.v3 from exact accepted delivery lineage."""
        from services.packets.canonical import (  # noqa: PLC0415
            CanonicalPacketError,
            build_story_packet,
        )

        try:
            with Session(self._engine) as session:
                packet = build_story_packet(
                    session,
                    project_id=project_id,
                    sprint_id=sprint_id,
                    story_id=story_id,
                )
        except CanonicalPacketError as error:
            return _error(error.code, str(error), **error.details)
        return _packet_read(packet, flavor)

    def context_pack(self, *, project_id: int, phase: str) -> JsonObject:
        """Return bounded non-routing context for retained automation readers."""
        project = self.project_show(project_id=project_id)
        if project.get("ok") is not True:
            return project
        return _success(
            {
                "schema_version": "agileforge.context_pack.v1",
                "phase": phase,
                "project": _result_data(project),
            }
        )

    def status(self, *, project_id: int) -> JsonObject:
        """Return non-routing Project orientation for operators."""
        project = self.project_show(project_id=project_id)
        if project.get("ok") is not True:
            return project
        sprint = self.sprint_status(project_id=project_id)
        return _success(
            {
                "project": _result_data(project),
                "sprint": _result_data(sprint) if sprint.get("ok") is True else None,
            }
        )

    def _specification_projection(
        self,
        *,
        project_id: int,
        candidate: SpecificationCandidateFact,
        spec: SpecVersionFact | None,
        decision_state: str,
    ) -> _SpecificationReadProjection | _SpecificationReadFailure:
        """Resolve the selected fact to one candidate, via registry if accepted."""
        with Session(self._engine) as session:
            registry: SpecRegistry | None = None
            if spec is not None:
                registry = session.get(SpecRegistry, spec.spec_version_id)
                registry_fact = (
                    None
                    if registry is None
                    else (
                        registry.project_id,
                        registry.spec_hash,
                        registry.status,
                        registry.source_specification_candidate_id,
                        registry.source_specification_candidate_fingerprint,
                        registry.source_vision_artifact_id,
                        registry.source_vision_fingerprint,
                        registry.source_product_goal_artifact_id,
                        registry.source_product_goal_fingerprint,
                    )
                )
                selected_fact = (
                    project_id,
                    spec.spec_hash,
                    spec.status,
                    spec.source_specification_candidate_id,
                    spec.source_specification_candidate_fingerprint,
                    spec.source_vision_artifact_id,
                    spec.source_vision_fingerprint,
                    spec.source_product_goal_artifact_id,
                    spec.source_product_goal_fingerprint,
                )
                if registry_fact != selected_fact:
                    return _SpecificationReadFailure(
                        _error(
                            "SPECIFICATION_CANDIDATE_UNAVAILABLE",
                            "Selected Specification registry row changed.",
                            project_id=project_id,
                            spec_version_id=spec.spec_version_id,
                        )
                    )
            statement = select(SpecificationCandidate).where(
                col(SpecificationCandidate.project_id) == project_id,
                col(SpecificationCandidate.specification_candidate_id)
                == candidate.specification_candidate_id,
                col(SpecificationCandidate.candidate_fingerprint)
                == candidate.candidate_fingerprint,
                col(SpecificationCandidate.specification_source_id)
                == candidate.specification_source_id,
                col(SpecificationCandidate.specification_source_fingerprint)
                == candidate.specification_source_fingerprint,
            )
            if registry is not None:
                statement = statement.where(
                    col(SpecificationCandidate.payload_fingerprint)
                    == registry.spec_hash,
                    col(SpecificationCandidate.vision_artifact_id)
                    == registry.source_vision_artifact_id,
                    col(SpecificationCandidate.vision_fingerprint)
                    == registry.source_vision_fingerprint,
                    col(SpecificationCandidate.product_goal_artifact_id)
                    == registry.source_product_goal_artifact_id,
                    col(SpecificationCandidate.product_goal_fingerprint)
                    == registry.source_product_goal_fingerprint,
                )
            persisted = session.exec(statement).one_or_none()
        if persisted is None:
            return _SpecificationReadFailure(
                _error(
                    "SPECIFICATION_CANDIDATE_UNAVAILABLE",
                    "Selected Specification does not resolve one persisted candidate.",
                    project_id=project_id,
                    specification_candidate_id=(candidate.specification_candidate_id),
                )
            )
        try:
            candidate_data = _specification_candidate_data(
                persisted,
                decision_state=decision_state,
            )
        except (TypeError, ValueError) as error:
            return _SpecificationReadFailure(
                _error(
                    "SPECIFICATION_CANDIDATE_UNAVAILABLE",
                    "Stored Specification candidate contract is unavailable.",
                    project_id=project_id,
                    specification_candidate_id=(candidate.specification_candidate_id),
                    reason=str(error),
                )
            )
        return _SpecificationReadProjection(
            candidate=candidate_data,
            registry=registry,
        )

    def _snapshot(self, project_id: int) -> WorkflowFactSnapshot | JsonObject:
        project_or_error = self._project(project_id)
        if isinstance(project_or_error, _ProjectReadFailure):
            return project_or_error.error
        with Session(self._engine) as session:
            try:
                return WorkflowFactRepository(session).load(project_id)
            except WorkflowFactLoadError as error:
                return _error(
                    "PROJECT_FACTS_UNAVAILABLE",
                    str(error),
                    project_id=project_id,
                )

    def _project(self, project_id: int) -> _ProjectReadContext | _ProjectReadFailure:
        """Establish one Project identity before any project-scoped read."""
        with Session(self._engine) as session:
            project = session.get(Project, project_id)
        if project is None:
            return _ProjectReadFailure(
                error=_error(
                    "PROJECT_NOT_FOUND",
                    f"Project {project_id} was not found.",
                    project_id=project_id,
                )
            )
        return _ProjectReadContext(project_id=project_id, project=project)

    @staticmethod
    def _select_sprint(
        sprints: Iterable[SprintFact],
        sprint_id: int | None,
    ) -> SprintFact | None:
        items = tuple(sprints)
        if sprint_id is not None:
            return next((item for item in items if item.sprint_id == sprint_id), None)
        priorities = {"active": 0, "planned": 1, "completed": 2}
        return min(
            items,
            key=lambda item: (priorities[item.status], -item.sprint_id),
            default=None,
        )


__all__ = ["DurableReadProjectionService"]
