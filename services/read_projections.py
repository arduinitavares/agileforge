"""Durable non-routing projections for production transports."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from pydantic import TypeAdapter
from sqlmodel import Session, col, select

from models.core import Project, Sprint, UserStory
from models.events import TaskExecutionLog
from models.product_definition import SpecificationCandidate, SpecificationDecision
from models.repository import RepositoryBinding, repository_binding_fingerprint
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry
from models.workflow import WorkflowNodeAttempt, WorkflowNodeAttemptOutcome
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from services.agent_workbench.authority_projection import (
    AuthorityProjectionService,
    pending_authority_fingerprint,
)
from services.packet_renderer import render_packet
from services.packets.canonical import (
    CanonicalPacketError,
    build_story_packet,
    build_task_packet,
)
from services.phases.sprint_metrics import build_durable_sprint_metrics
from services.specs.candidate_contract import (
    load_candidate_contract,
    render_candidate_review_markdown,
)
from services.specs.compiler_service import load_compiled_artifact
from workflow.contracts import JsonObject, JsonValue
from workflow.definitions.product_discovery import select_product_definition_state
from workflow.definitions.product_goal import (
    accepted_current_goal,
    accepted_current_vision,
    select_product_goal_interview_state,
)
from workflow.definitions.vision import select_vision_interview_state

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.engine import Engine

    from workflow.facts import (
        ProductGoalArtifactDecisionFact,
        ProductGoalArtifactFact,
        ProductGoalInterviewTurnFact,
        SpecificationCandidateFact,
        SpecificationDecisionFact,
        SpecVersionFact,
        SprintFact,
        VisionArtifactDecisionFact,
        VisionArtifactFact,
        VisionInterviewTurnFact,
        WorkflowFactSnapshot,
    )

_JSON_OBJECT = TypeAdapter(JsonObject)
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
                    if isinstance(item, str)
                    and item in _VISION_BASIS_SOURCE_KINDS
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
        "model_configuration_fingerprint": (
            envelope.model_configuration_fingerprint
        ),
        "prompt_fingerprint": envelope.prompt_fingerprint,
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
        "approved_at": _iso(spec.approved_at),
        "approved_by": spec.approved_by,
        "source_specification_candidate_id": (
            spec.source_specification_candidate_id
        ),
        "source_specification_candidate_fingerprint": (
            spec.source_specification_candidate_fingerprint
        ),
        "source_vision_artifact_id": spec.source_vision_artifact_id,
        "source_vision_fingerprint": spec.source_vision_fingerprint,
        "source_product_goal_artifact_id": (
            spec.source_product_goal_artifact_id
        ),
        "source_product_goal_fingerprint": (
            spec.source_product_goal_fingerprint
        ),
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
            if item.specification_candidate_id
            == spec.source_specification_candidate_id
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


def _authority_decisions_by_id(
    decisions: list[SpecAuthorityAcceptance],
) -> dict[int, SpecAuthorityAcceptance]:
    """Index persisted Authority decisions that retain a pending identity."""
    indexed: dict[int, SpecAuthorityAcceptance] = {}
    for decision in decisions:
        authority_id = decision.pending_authority_id
        if authority_id is not None:
            indexed[authority_id] = decision
    return indexed


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


class _AuthorityReviewProjection(Protocol):
    """Facts-only authority review projection injected into retained reads."""

    def project(
        self,
        *,
        project: Project,
        include_spec: str,
    ) -> JsonObject: ...


class DurableAuthorityReviewProjection:
    """Project durable authority records without workflow recommendations."""

    def __init__(self, *, engine: Engine) -> None:
        """Bind the projection to durable authority records."""
        self._engine = engine

    def project(
        self,
        *,
        project: Project,
        include_spec: str,
    ) -> JsonObject:
        """Return accepted and pending authority facts for operator review."""
        if include_spec not in {"auto", "full", "summary"}:
            return _error(
                "INVALID_INPUT",
                f"Unsupported include_spec value: {include_spec}.",
                field="include_spec",
                value=include_spec,
                allowed=["auto", "full", "summary"],
            )
        project_id = project.project_id
        if project_id is None:
            return _error(
                "PROJECT_NOT_FOUND",
                "Project identity is unavailable.",
            )
        with Session(self._engine) as session:
            specifications = list(
                session.exec(
                    select(SpecRegistry)
                    .where(col(SpecRegistry.project_id) == project_id)
                    .order_by(col(SpecRegistry.spec_version_id))
                ).all()
            )
            specification_candidates = list(
                session.exec(
                    select(SpecificationCandidate)
                    .where(col(SpecificationCandidate.project_id) == project_id)
                    .order_by(
                        col(SpecificationCandidate.specification_candidate_id)
                    )
                ).all()
            )
            specification_decisions = list(
                session.exec(
                    select(SpecificationDecision)
                    .where(col(SpecificationDecision.project_id) == project_id)
                    .order_by(
                        col(SpecificationDecision.specification_decision_id)
                    )
                ).all()
            )
            authorities = list(
                session.exec(
                    select(CompiledSpecAuthority)
                    .join(
                        SpecRegistry,
                        col(CompiledSpecAuthority.spec_version_id)
                        == col(SpecRegistry.spec_version_id),
                    )
                    .where(col(SpecRegistry.project_id) == project_id)
                    .order_by(col(CompiledSpecAuthority.authority_id))
                ).all()
            )
            decisions = list(
                session.exec(
                    select(SpecAuthorityAcceptance)
                    .where(col(SpecAuthorityAcceptance.project_id) == project_id)
                    .order_by(
                        col(SpecAuthorityAcceptance.decided_at),
                        col(SpecAuthorityAcceptance.id),
                    )
                ).all()
            )

        specs_by_id = {
            spec.spec_version_id: spec
            for spec in specifications
            if spec.spec_version_id is not None
        }
        try:
            candidate_payloads = _accepted_registry_candidate_payloads(
                project_id=project_id,
                specifications=specifications,
                candidates=specification_candidates,
                decisions=specification_decisions,
            )
        except (TypeError, ValueError) as error:
            return _error(
                "SPECIFICATION_CANDIDATE_UNAVAILABLE",
                "Stored Specification candidate contract is unavailable.",
                project_id=project_id,
                reason=str(error),
            )
        decisions_by_authority = _authority_decisions_by_id(decisions)

        rendered: list[JsonValue] = []
        rendered_by_id: dict[int, JsonObject] = {}
        accepted_authority_id: int | None = None
        pending_authority_id: int | None = None
        for authority in authorities:
            authority_id = authority.authority_id
            spec = specs_by_id.get(authority.spec_version_id)
            if authority_id is None or spec is None:
                return _error(
                    "AUTHORITY_FACTS_UNAVAILABLE",
                    "Stored authority ownership is incomplete.",
                    project_id=project_id,
                )
            decision = decisions_by_authority.get(authority_id)
            status = self._authority_status(spec=spec, decision=decision)
            payload_or_error = self._authority_payload(
                authority=authority,
                spec=spec,
                candidate=candidate_payloads[authority.spec_version_id],
                decision=decision,
                status=status,
            )
            if payload_or_error.get("ok") is False:
                return payload_or_error
            rendered.append(payload_or_error)
            rendered_by_id[authority_id] = payload_or_error
            if status == "accepted":
                accepted_authority_id = authority_id
            elif status == "pending_review":
                pending_authority_id = authority_id

        accepted = rendered_by_id.get(accepted_authority_id or -1)
        pending = rendered_by_id.get(pending_authority_id or -1)
        findings = pending.get("findings", []) if pending is not None else []
        return _success(
            {
                "schema_version": "agileforge.authority_review_projection.v1",
                "project": {
                    "project_id": project_id,
                    "name": project.name,
                },
                "specifications": [
                    _specification_registry_data(
                        spec,
                        candidate=candidate_payloads[spec.spec_version_id],
                    )
                    for spec in specifications
                    if spec.spec_version_id is not None
                ],
                "authorities": rendered,
                "accepted_authority": accepted,
                "pending_authority": pending,
                "findings": findings,
            }
        )

    @staticmethod
    def _authority_status(
        *,
        spec: SpecRegistry,
        decision: SpecAuthorityAcceptance | None,
    ) -> str:
        if decision is not None and decision.status in {"accepted", "rejected"}:
            return decision.status
        return "stale" if spec.status == "superseded" else "pending_review"

    def _authority_payload(
        self,
        *,
        authority: CompiledSpecAuthority,
        spec: SpecRegistry,
        candidate: JsonObject,
        decision: SpecAuthorityAcceptance | None,
        status: str,
    ) -> JsonObject:
        load_result = load_compiled_artifact(authority)
        artifact = load_result.artifact
        if artifact is None:
            return _error(
                "AUTHORITY_ARTIFACT_UNAVAILABLE",
                load_result.message or "Stored authority artifact is unavailable.",
                authority_id=authority.authority_id,
                artifact_status=load_result.status,
            )
        findings: list[JsonValue] = [
            {
                "kind": "gap",
                "severity": "review",
                "message": gap,
            }
            for gap in artifact.gaps
        ]
        quality = artifact.authority_quality
        if quality is not None:
            findings.extend(
                {
                    "kind": "authority_quality",
                    "finding_id": group.group_id,
                    "severity": group.severity,
                    "message": group.reason,
                    "member_ids": list(group.member_ids),
                }
                for group in quality.review_groups
            )
        terminal_decision: JsonObject | None = None
        if decision is not None:
            terminal_decision = {
                "status": decision.status,
                "policy": decision.policy,
                "decided_by": decision.decided_by,
                "decided_at": _iso(decision.decided_at),
                "rationale": decision.rationale,
            }
        return {
            "authority_id": authority.authority_id,
            "authority_fingerprint": pending_authority_fingerprint(authority),
            "spec_version_id": authority.spec_version_id,
            "status": status,
            "compiler_version": authority.compiler_version,
            "prompt_hash": authority.prompt_hash,
            "compiled_at": _iso(authority.compiled_at),
            "terminal_decision": terminal_decision,
            "specification": _specification_registry_data(
                spec,
                candidate=candidate,
            ),
            "invariants": [
                _validated(invariant.model_dump(mode="json"))
                for invariant in artifact.invariants
            ],
            "findings": findings,
            "artifact": _validated(artifact.model_dump(mode="json")),
        }

class DurableReadProjectionService:
    """Read supported operator views without deriving workflow availability."""

    def __init__(
        self,
        *,
        engine: Engine,
        authority_review_projection: _AuthorityReviewProjection | None = None,
    ) -> None:
        """Bind durable records and injected read-only authority projections."""
        self._engine = engine
        self._authority = AuthorityProjectionService(engine=engine)
        self._authority_review = authority_review_projection or (
            DurableAuthorityReviewProjection(engine=engine)
        )

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
        latest_turn = (
            selection.transcript[-1] if selection.transcript else None
        )
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
            None
            if vision is None
            else {"statement": vision.statement}
        )
        candidate_data: JsonObject | None = (
            None if candidate is None else _vision_candidate_data(candidate)
        )
        data: JsonObject = {
            "bootstrap_available": not selection.transcript
            and (
                selection.artifact is None
                or selection.open_revision is not None
            ),
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
        candidate = selection.specification_candidate
        spec = selection.accepted_spec
        if candidate is None:
            return _success(
                {
                    "schema_version": _SPECIFICATION_REVIEW_SCHEMA_VERSION,
                    "current": None,
                    "candidate": None,
                    "review": None,
                    "stale_reason": (
                        "SPECIFICATION_FACT_CONFLICT"
                        if (
                            selection.specification_candidate_conflict
                            or selection.accepted_spec_conflict
                        )
                        else "SPECIFICATION_NOT_CURRENT"
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
            spec=spec,
            decision_state=decision_state,
        )
        if isinstance(projection, _SpecificationReadFailure):
            return projection.error
        return _success(
            {
                "schema_version": _SPECIFICATION_REVIEW_SCHEMA_VERSION,
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
        candidate = selection.specification_candidate
        if candidate is None:
            return _success(
                {
                    "schema_version": _SPECIFICATION_REVIEW_SCHEMA_VERSION,
                    "candidate": None,
                    "review": None,
                    "stale_reason": (
                        "SPECIFICATION_FACT_CONFLICT"
                        if selection.specification_candidate_conflict
                        else "NO_CURRENT_CANDIDATE"
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
            spec=selection.accepted_spec,
            decision_state=decision_state,
        )
        if isinstance(projection, _SpecificationReadFailure):
            return projection.error
        return _success(
            {
                "schema_version": _SPECIFICATION_REVIEW_SCHEMA_VERSION,
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

    def authority_status(self, *, project_id: int) -> JsonObject:
        """Delegate to the durable authority projection."""
        project_or_error = self._project(project_id)
        if isinstance(project_or_error, _ProjectReadFailure):
            return project_or_error.error
        return _validated(self._authority.status(project_id=project_id))

    def authority_invariants(
        self,
        *,
        project_id: int,
        spec_version_id: int | None = None,
    ) -> JsonObject:
        """Delegate to the durable invariant projection."""
        project_or_error = self._project(project_id)
        if isinstance(project_or_error, _ProjectReadFailure):
            return project_or_error.error
        return _validated(
            self._authority.invariants(
                project_id=project_id,
                spec_version_id=spec_version_id,
            )
        )

    def authority_review(
        self,
        *,
        project_id: int,
        include_spec: str = "auto",
    ) -> JsonObject:
        """Return facts-only accepted and pending authority inspection data."""
        project_or_error = self._project(project_id)
        if isinstance(project_or_error, _ProjectReadFailure):
            return project_or_error.error
        return self._authority_review.project(
            project=project_or_error.project,
            include_spec=include_spec,
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
        """Return one durable Story record."""
        with Session(self._engine) as session:
            story = session.get(UserStory, story_id)
        if story is None:
            return _error(
                "STORY_NOT_FOUND",
                f"Story {story_id} was not found.",
                story_id=story_id,
            )
        return _success(
            {
                "story_id": story_id,
                "project_id": story.project_id,
                "title": story.title,
                "description": story.story_description,
                "acceptance_criteria": story.acceptance_criteria,
                "status": _enum_value(story.status),
                "story_points": story.story_points,
                "rank": story.rank,
                "source_requirement": story.source_requirement,
                "is_refined": story.is_refined,
                "is_superseded": story.is_superseded,
                "updated_at": _iso(story.updated_at),
            }
        )

    def story_pending(self, *, project_id: int) -> JsonObject:
        """List accepted Backlog requirements and their durable Story coverage."""
        snapshot_or_error = self._snapshot(project_id)
        if isinstance(snapshot_or_error, dict):
            return snapshot_or_error
        snapshot = snapshot_or_error
        story_artifacts = {
            item.requirement_id: item
            for item in sorted(
                (
                    item
                    for item in snapshot.planning_artifacts
                    if item.artifact_type == "story"
                    and item.requirement_id is not None
                    and item.status != "superseded"
                ),
                key=lambda item: item.artifact_id,
            )
        }
        items: list[JsonValue] = []
        pending_count = 0
        for requirement in snapshot.backlog_requirements:
            artifact = story_artifacts.get(requirement.requirement_id)
            status = artifact.status if artifact is not None else "pending"
            if status != "accepted":
                pending_count += 1
            items.append(
                {
                    "requirement_id": requirement.requirement_id,
                    "requirement": requirement.requirement,
                    "rank": requirement.rank,
                    "status": status,
                    "story_artifact_id": (
                        artifact.artifact_id if artifact is not None else None
                    ),
                    "story_ids": (
                        list(artifact.story_ids) if artifact is not None else []
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
            }
        )

    def sprint_candidates(self, *, project_id: int) -> JsonObject:
        """Return Story facts currently eligible for Sprint planning."""
        snapshot_or_error = self._snapshot(project_id)
        if isinstance(snapshot_or_error, dict):
            return snapshot_or_error
        items: list[JsonValue] = [
            _validated(item.model_dump(mode="json"))
            for item in snapshot_or_error.stories
            if item.sprint_candidate
        ]
        return _success({"project_id": project_id, "items": items, "count": len(items)})

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
        return _success(
            {
                "project_id": project_id,
                "sprint": _validated(sprint.model_dump(mode="json")),
                "start": next(
                    (
                        _validated(item.model_dump(mode="json"))
                        for item in snapshot.sprint_starts
                        if item.sprint_id == selected_id
                    ),
                    None,
                ),
                "tasks": [
                    _validated(item.model_dump(mode="json"))
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
        """Return canonical task_packet.v2 from durable current records."""
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
        if flavor:
            packet["render"] = render_packet(packet, flavor)
        return _success(packet)

    def story_packet(
        self,
        *,
        project_id: int,
        sprint_id: int,
        story_id: int,
        flavor: str | None = None,
    ) -> JsonObject:
        """Return canonical story_packet.v1 from durable current records."""
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
        if flavor:
            packet["render"] = render_packet(packet, flavor)
        return _success(packet)

    def context_pack(self, *, project_id: int, phase: str) -> JsonObject:
        """Return bounded non-routing context for retained automation readers."""
        project = self.project_show(project_id=project_id)
        if project.get("ok") is not True:
            return project
        authority = self.authority_status(project_id=project_id)
        return _success(
            {
                "schema_version": "agileforge.context_pack.v1",
                "phase": phase,
                "project": _result_data(project),
                "authority": _result_data(authority),
            }
        )

    def status(self, *, project_id: int) -> JsonObject:
        """Return non-routing Project orientation for operators."""
        project = self.project_show(project_id=project_id)
        if project.get("ok") is not True:
            return project
        authority = self.authority_status(project_id=project_id)
        sprint = self.sprint_status(project_id=project_id)
        return _success(
            {
                "project": _result_data(project),
                "authority": _result_data(authority),
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
                registry_fact = None if registry is None else (
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
                    specification_candidate_id=(
                        candidate.specification_candidate_id
                    ),
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
                    specification_candidate_id=(
                        candidate.specification_candidate_id
                    ),
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

    def _story_record(self, story_id: int) -> JsonObject | None:
        with Session(self._engine) as session:
            story = session.get(UserStory, story_id)
        if story is None:
            return None
        return {
            "story_id": story_id,
            "project_id": story.project_id,
            "title": story.title,
            "description": story.story_description,
            "acceptance_criteria": story.acceptance_criteria,
            "status": _enum_value(story.status),
            "story_points": story.story_points,
            "source_requirement": story.source_requirement,
        }


__all__ = ["DurableAuthorityReviewProjection", "DurableReadProjectionService"]
