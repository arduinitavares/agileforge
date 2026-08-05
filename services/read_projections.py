"""Durable non-routing projections for production transports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from typing import TYPE_CHECKING, Protocol

from pydantic import TypeAdapter, ValidationError
from sqlmodel import Session, col, select

from models.core import Project, Sprint, UserStory
from models.events import TaskExecutionLog
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry
from models.workflow import (
    DiscoveryRun,
    SpecDraft,
    WorkflowNodeAttempt,
    WorkflowNodeAttemptOutcome,
)
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
from services.specs.compiler_service import load_compiled_artifact
from workflow.contracts import JsonObject, JsonValue
from workflow.definitions.product_discovery import (
    accepted_current_spec,
    current_discovery,
    current_specification_candidate,
)
from workflow.definitions.product_goal import (
    accepted_current_goal,
    accepted_current_vision,
)
from workflow.fingerprints import canonical_hash

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.engine import Engine

    from workflow.facts import SprintFact, WorkflowFactSnapshot

_JSON_OBJECT = TypeAdapter(JsonObject)
_AUTO_SPEC_CONTENT_LIMIT_BYTES = 64_000


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


def _active_initial_spec_draft(drafts: list[SpecDraft]) -> SpecDraft | None:
    """Return the sole leaf of one complete valid initial-draft chain."""
    if any(
        draft.spec_draft_id is None
        or draft.kind != "initial"
        or draft.base_spec_version_id is not None
        or draft.base_spec_hash is not None
        for draft in drafts
    ):
        return None
    by_id = {
        draft.spec_draft_id: draft
        for draft in drafts
        if draft.spec_draft_id is not None
    }
    if len(by_id) != len(drafts):
        return None
    referenced = {
        draft.supersedes_spec_draft_id
        for draft in drafts
        if draft.supersedes_spec_draft_id is not None
    }
    leaves = [draft for draft in drafts if draft.spec_draft_id not in referenced]
    if not referenced <= by_id.keys() or len(leaves) != 1:
        return None
    active = leaves[0]
    visited: set[int] = set()
    current: SpecDraft | None = active
    while current is not None:
        current_id = current.spec_draft_id
        if current_id is None or current_id in visited:
            return None
        visited.add(current_id)
        parent_id = current.supersedes_spec_draft_id
        current = by_id.get(parent_id) if parent_id is not None else None
    return active if len(visited) == len(drafts) else None


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
class _InitialSpecReadContext:
    """The sole active initial draft and its discovery identity."""

    discovery_run_id: int
    draft: SpecDraft


@dataclass(frozen=True)
class _InitialSpecReadFailure:
    """Typed initial-spec selection failure."""

    error: JsonObject


def _load_active_initial_spec(
    engine: Engine,
    *,
    project_id: int,
) -> _InitialSpecReadContext | _InitialSpecReadFailure:
    """Load one unambiguous initial draft chain from durable records."""
    with Session(engine) as session:
        runs = list(
            session.exec(
                select(DiscoveryRun).where(
                    col(DiscoveryRun.project_id) == project_id,
                    col(DiscoveryRun.purpose) == "initial",
                )
            ).all()
        )
        if len(runs) > 1:
            return _InitialSpecReadFailure(
                _error(
                    "INITIAL_SPEC_DRAFT_AMBIGUOUS",
                    "The Project has an ambiguous initial discovery run.",
                    project_id=project_id,
                )
            )
        if not runs or runs[0].discovery_run_id is None:
            return _InitialSpecReadFailure(
                _error(
                    "INITIAL_SPEC_DRAFT_NOT_FOUND",
                    "The Project has no active initial specification draft.",
                    project_id=project_id,
                )
            )
        run_id = runs[0].discovery_run_id
        drafts = list(
            session.exec(
                select(SpecDraft)
                .where(
                    col(SpecDraft.project_id) == project_id,
                    col(SpecDraft.discovery_run_id) == run_id,
                )
                .order_by(
                    col(SpecDraft.version_number),
                    col(SpecDraft.spec_draft_id),
                )
            ).all()
        )
    if not drafts:
        return _InitialSpecReadFailure(
            _error(
                "INITIAL_SPEC_DRAFT_NOT_FOUND",
                "The Project has no active initial specification draft.",
                project_id=project_id,
                discovery_run_id=run_id,
            )
        )
    active = _active_initial_spec_draft(drafts)
    if active is None:
        return _InitialSpecReadFailure(
            _error(
                "INITIAL_SPEC_DRAFT_AMBIGUOUS",
                "The Project's initial specification draft chain is ambiguous.",
                project_id=project_id,
                discovery_run_id=run_id,
                draft_count=len(drafts),
            )
        )
    return _InitialSpecReadContext(discovery_run_id=run_id, draft=active)


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
        decisions_by_authority: dict[int, SpecAuthorityAcceptance] = {}
        for decision in decisions:
            authority_id = decision.pending_authority_id
            if authority_id is not None:
                decisions_by_authority[authority_id] = decision

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
                decision=decision,
                status=status,
                include_spec=include_spec,
            )
            if payload_or_error.get("ok") is False:
                return payload_or_error
            rendered.append(payload_or_error)
            rendered_by_id[authority_id] = payload_or_error
            if status == "accepted":
                accepted_authority_id = authority_id
            elif status == "pending_review":
                pending_authority_id = authority_id

        accepted = (
            rendered_by_id.get(accepted_authority_id)
            if accepted_authority_id is not None
            else None
        )
        pending = (
            rendered_by_id.get(pending_authority_id)
            if pending_authority_id is not None
            else None
        )
        findings = pending.get("findings", []) if pending is not None else []
        return _success(
            {
                "schema_version": "agileforge.authority_review_projection.v1",
                "project": {
                    "project_id": project_id,
                    "name": project.name,
                },
                "specifications": [
                    self._specification_payload(spec, include_spec=include_spec)
                    for spec in specifications
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
        decision: SpecAuthorityAcceptance | None,
        status: str,
        include_spec: str,
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
            "specification": self._specification_payload(
                spec,
                include_spec=include_spec,
            ),
            "invariants": [
                _validated(invariant.model_dump(mode="json"))
                for invariant in artifact.invariants
            ],
            "findings": findings,
            "artifact": _validated(artifact.model_dump(mode="json")),
        }

    @staticmethod
    def _specification_payload(
        spec: SpecRegistry,
        *,
        include_spec: str,
    ) -> JsonObject:
        size_bytes = len(spec.content.encode("utf-8"))
        content_included = include_spec == "full" or (
            include_spec == "auto" and size_bytes <= _AUTO_SPEC_CONTENT_LIMIT_BYTES
        )
        return {
            "spec_version_id": spec.spec_version_id,
            "spec_hash": spec.spec_hash,
            "status": spec.status,
            "content_ref": spec.content_ref,
            "approved_at": _iso(spec.approved_at),
            "approved_by": spec.approved_by,
            "size_bytes": size_bytes,
            "content_included": content_included,
            "content": spec.content if content_included else None,
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
                    "origin": project.origin,
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
        return _success(
            {
                "id": project_id,
                "project_id": project_id,
                "name": project.name,
                "origin": project.origin,
                "description": project.description,
                "vision_present": bool(project.vision),
                "roadmap_present": bool(project.roadmap),
                "spec_file_path": project.spec_file_path,
                "structure_counts": {
                    "user_stories": sum(
                        1 for story in stories if not story.is_superseded
                    ),
                    "sprints": len(sprints),
                },
                "updated_at": _iso(project.updated_at),
            }
        )

    def project_initial_spec(self, *, project_id: int) -> JsonObject:
        """Return the sole active immutable initial draft for human review."""
        context = self._project(project_id)
        if isinstance(context, _ProjectReadFailure):
            return context.error
        active_context = _load_active_initial_spec(self._engine, project_id=project_id)
        if isinstance(active_context, _InitialSpecReadFailure):
            return active_context.error
        active = active_context.draft
        try:
            canonical_content = _JSON_OBJECT.validate_json(
                active.canonical_content_json
            )
        except ValidationError:
            return _error(
                "INITIAL_SPEC_DRAFT_INVALID",
                "The active initial specification content is not canonical JSON.",
                project_id=project_id,
                spec_draft_id=active.spec_draft_id,
            )
        if canonical_hash(canonical_content) != active.content_fingerprint:
            return _error(
                "INITIAL_SPEC_DRAFT_INVALID",
                "The active initial specification fingerprint does not match content.",
                project_id=project_id,
                spec_draft_id=active.spec_draft_id,
            )
        created_at = active.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        timestamp = created_at.isoformat()
        return _success(
            {
                "schema_version": "agileforge.initial_spec_projection.v1",
                "project": {
                    "project_id": project_id,
                    "name": context.project.name,
                },
                "active_draft": {
                    "spec_draft_id": active.spec_draft_id,
                    "discovery_run_id": active_context.discovery_run_id,
                    "kind": active.kind,
                    "version_number": active.version_number,
                    "canonical_content": canonical_content,
                    "content_fingerprint": active.content_fingerprint,
                    "provenance_path": active.provenance_path,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                },
            }
        )

    def vision_status(self, *, project_id: int) -> JsonObject:
        """Project the current durable Vision without consulting mutable caches."""
        snapshot = self._snapshot(project_id)
        if isinstance(snapshot, dict):
            return snapshot
        vision = accepted_current_vision(snapshot)
        if vision is None:
            return _success({"current": None, "stale_reason": "VISION_NOT_ACCEPTED"})
        return _success(
            {
                "current": {
                    "vision_artifact_id": vision.vision_artifact_id,
                    "fingerprint": vision.content_fingerprint,
                    "statement": vision.statement,
                },
                "stale_reason": None,
            }
        )

    def product_goal_status(self, *, project_id: int) -> JsonObject:
        """Project accepted Goal and durable outcome state from workflow facts."""
        snapshot = self._snapshot(project_id)
        if isinstance(snapshot, dict):
            return snapshot
        goal = accepted_current_goal(snapshot)
        if goal is None:
            return _success(
                {
                    "active": None,
                    "outcome": None,
                    "stale_reason": "GOAL_NOT_ACTIVE",
                }
            )
        return _success(
            {
                "active": {
                    "product_goal_artifact_id": goal.product_goal_artifact_id,
                    "fingerprint": goal.content_fingerprint,
                    "statement": goal.statement,
                    "goal_number": goal.goal_number,
                    "revision_number": goal.revision_number,
                },
                "outcome": None,
                "stale_reason": None,
            }
        )

    def discovery_status(self, *, project_id: int) -> JsonObject:
        """Project the current durable discovery artifact and exact parents."""
        snapshot = self._snapshot(project_id)
        if isinstance(snapshot, dict):
            return snapshot
        discovery = current_discovery(snapshot)
        if discovery is None:
            return _success(
                {"current": None, "stale_reason": "DISCOVERY_NOT_CURRENT"}
            )
        return _success(
            {
                "current": {
                    "discovery_artifact_id": discovery.discovery_artifact_id,
                    "fingerprint": discovery.content_fingerprint,
                    "content_ref": discovery.content_ref,
                    "vision_artifact_id": discovery.vision_artifact_id,
                    "product_goal_artifact_id": discovery.product_goal_artifact_id,
                },
                "stale_reason": None,
            }
        )

    def specification_status(self, *, project_id: int) -> JsonObject:
        """Project the graph-selected approved registry identity only."""
        snapshot = self._snapshot(project_id)
        if isinstance(snapshot, dict):
            return snapshot
        spec = accepted_current_spec(snapshot)
        if spec is None:
            return _success(
                {
                    "current": None,
                    "stale_reason": "SPECIFICATION_NOT_APPROVED",
                }
            )
        return _success(
            {
                "current": {
                    "spec_version_id": spec.spec_version_id,
                    "spec_hash": spec.spec_hash,
                    "status": spec.status,
                    "source_specification_candidate_id": (
                        spec.source_specification_candidate_id
                    ),
                },
                "stale_reason": None,
            }
        )

    def specification_review(self, *, project_id: int) -> JsonObject:
        """Project the pending or terminal durable candidate review state."""
        snapshot = self._snapshot(project_id)
        if isinstance(snapshot, dict):
            return snapshot
        candidate = current_specification_candidate(snapshot)
        if candidate is None:
            return _success(
                {
                    "candidate": None,
                    "review": None,
                    "stale_reason": "NO_CURRENT_CANDIDATE",
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
        review: JsonObject | None = None
        if len(decisions) == 1:
            decision = decisions[0]
            review = {
                "specification_decision_id": decision.specification_decision_id,
                "decision": decision.decision,
                "rationale": decision.rationale,
                "reviewer": decision.reviewer,
            }
        return _success(
            {
                "candidate": {
                    "specification_candidate_id": candidate.specification_candidate_id,
                    "fingerprint": candidate.content_fingerprint,
                    "content_ref": candidate.content_ref,
                },
                "review": review,
                "stale_reason": None if review is None else "CANDIDATE_REVIEWED",
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
        """Return deterministic counts from durable Sprint execution facts."""
        snapshot_or_error = self._snapshot(project_id)
        if isinstance(snapshot_or_error, dict):
            return snapshot_or_error
        snapshot = snapshot_or_error
        completed_ids = {
            item.sprint_id for item in snapshot.sprints if item.status == "completed"
        }
        return _success(
            {
                "project_id": project_id,
                "sprint_count": len(snapshot.sprints),
                "completed_sprint_count": len(completed_ids),
                "task_count": len(snapshot.tasks),
                "completed_task_count": len(snapshot.task_completions),
                "story_completion_count": len(snapshot.story_completions),
            }
        )

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
