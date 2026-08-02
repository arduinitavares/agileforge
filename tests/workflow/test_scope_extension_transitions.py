"""Persisted scope-extension transition tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypedDict, overload

from sqlmodel import Session, col, select

from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry
from models.workflow import (
    ChallengeArtifact,
    DiscoveryRun,
    DiscoveryRunAbandonment,
    InitialScopeRegistration,
    PrdDecision,
    PrdVersion,
    ScopeExtensionRegistration,
    SpecDraft,
    SpecDraftDecision,
    VisionArtifact,
    VisionArtifactDecision,
)
from tests.workflow.execution_fixtures import seed_started_execution
from workflow.clock import FixedClock
from workflow.contracts import (
    NodeDecision,
    TransitionResult,
    WorkflowErrorCode,
    WorkflowPosition,
)
from workflow.definitions.root import project_graph
from workflow.domain import WorkflowDomain
from workflow.fingerprints import canonical_hash, canonical_json
from workflow.requests import (
    AbandonScopeExtension,
    CloseSprint,
    CloseStory,
    CompleteTask,
    DecideAmendmentSpecDraft,
    DecideExtensionPrd,
    RecordAmendmentSpecDraft,
    RecordExtensionChallenge,
    RecordExtensionPrd,
    RecordPostSprintTriage,
    RegisterScopeExtension,
    ReviewSprint,
    StartScopeExtension,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from workflow.contracts import JsonObject

EVALUATED_AT = datetime(2026, 8, 2, 12, tzinfo=UTC)


class _GuardBase(TypedDict):
    project_id: int
    graph_version: str
    fact_fingerprint: str
    decision_fingerprint: str
    actor: str
    correlation_id: str


class _Guards(_GuardBase):
    instance_key: str | None


class _InstanceGuards(_GuardBase):
    instance_key: str


def _domain(engine: Engine) -> WorkflowDomain:
    return WorkflowDomain(
        engine=engine,
        graph=project_graph(),
        clock=FixedClock(now_value=EVALUATED_AT),
    )


def _decision(
    position: WorkflowPosition,
    node_id: str,
    instance_key: str | None = None,
) -> NodeDecision:
    return next(
        item
        for item in position.decisions
        if item.node_id == node_id and item.instance_key == instance_key
    )


@overload
def _guards(
    domain: WorkflowDomain,
    project_id: int,
    node_id: str,
    instance_key: str,
) -> _InstanceGuards: ...


@overload
def _guards(
    domain: WorkflowDomain,
    project_id: int,
    node_id: str,
    instance_key: None = None,
) -> _Guards: ...


def _guards(
    domain: WorkflowDomain,
    project_id: int,
    node_id: str,
    instance_key: str | None = None,
) -> _Guards | _InstanceGuards:
    position = domain.position(project_id)
    decision = _decision(position, node_id, instance_key)
    return {
        "project_id": project_id,
        "graph_version": position.graph_version,
        "fact_fingerprint": position.fact_fingerprint,
        "decision_fingerprint": decision.decision_fingerprint,
        "instance_key": instance_key,
        "actor": "operator@example.com",
        "correlation_id": "task-13",
    }


def _output_int(result: TransitionResult, key: str) -> int:
    value = result.output.get(key)
    assert isinstance(value, int)
    return value


def _output_str(result: TransitionResult, key: str) -> str:
    value = result.output.get(key)
    assert isinstance(value, str)
    return value


def _complete_current_scope(
    domain: WorkflowDomain,
    *,
    project_id: int,
    sprint_id: int,
    story_id: int,
    task_id: int,
) -> None:
    completed = domain.transition(
        CompleteTask(
            **_guards(
                domain,
                project_id,
                "execution.task.complete",
                f"task:{task_id}",
            ),
            idempotency_key="task-13-complete-task",
            task_id=task_id,
            outcome_summary="Completed current scope.",
            artifact_refs=("tests/workflow/test_scope_extension_transitions.py",),
            acceptance_result="fully_met",
            checklist_result={"Run focused tests": "passed"},
        )
    )
    assert completed.ok is True
    closed_story = domain.transition(
        CloseStory(
            **_guards(
                domain,
                project_id,
                "execution.story.close",
                f"story:{story_id}",
            ),
            idempotency_key="task-13-close-story",
            story_id=story_id,
            resolution="Completed",
            delivered="Current accepted scope.",
            evidence="Task completion is durable.",
            known_gaps="None.",
        )
    )
    assert closed_story.ok is True

    review_position = domain.position(project_id)
    review_decision = _decision(review_position, "execution.sprint.review")
    review_fingerprint = next(
        item.fingerprint
        for item in review_decision.fact_references
        if item.fact_type == "sprint_review"
    )
    reviewed = domain.transition(
        ReviewSprint(
            **_guards(domain, project_id, "execution.sprint.review"),
            idempotency_key="task-13-review-sprint",
            sprint_id=sprint_id,
            review_fingerprint=review_fingerprint,
        )
    )
    assert reviewed.ok is True
    closed = domain.transition(
        CloseSprint(
            **_guards(domain, project_id, "execution.sprint.close"),
            idempotency_key="task-13-close-sprint",
            sprint_id=sprint_id,
            review_fingerprint=review_fingerprint,
        )
    )
    assert closed.ok is True
    triaged = domain.transition(
        RecordPostSprintTriage(
            **_guards(
                domain,
                project_id,
                "execution.post_sprint_triage",
                f"sprint:{sprint_id}",
            ),
            idempotency_key="task-13-triage",
            sprint_id=sprint_id,
            impact="none",
            canonical_payload={"summary": "Current scope is exhausted."},
        )
    )
    assert triaged.ok is True


def _seed_completed_project_history(engine: Engine, project_id: int) -> None:
    """Add the accepted discovery and Vision facts preceding Tasks 11-12."""
    with Session(engine) as session:
        base = session.exec(
            select(SpecRegistry).where(
                col(SpecRegistry.product_id) == project_id,
                col(SpecRegistry.status) == "approved",
            )
        ).one()
        assert base.spec_version_id is not None
        acceptance = session.exec(
            select(SpecAuthorityAcceptance).where(
                col(SpecAuthorityAcceptance.product_id) == project_id,
                col(SpecAuthorityAcceptance.status) == "accepted",
            )
        ).one()
        assert acceptance.pending_authority_id is not None
        assert acceptance.authority_fingerprint is not None
        authority = session.get(
            CompiledSpecAuthority,
            acceptance.pending_authority_id,
        )
        assert authority is not None

        run = DiscoveryRun(
            project_id=project_id,
            purpose="initial",
            ordinal=1,
            created_at=EVALUATED_AT,
        )
        session.add(run)
        session.flush()
        assert run.discovery_run_id is not None

        challenge_payload = {"challenge": "Deliver the accepted current scope."}
        challenge = ChallengeArtifact(
            project_id=project_id,
            discovery_run_id=run.discovery_run_id,
            version_number=1,
            canonical_content_json=canonical_json(challenge_payload),
            content_fingerprint=canonical_hash(challenge_payload),
            created_at=EVALUATED_AT,
        )
        session.add(challenge)
        prd_payload = {"requirements": ["Deliver the accepted scope."]}
        prd = PrdVersion(
            project_id=project_id,
            discovery_run_id=run.discovery_run_id,
            version_number=1,
            canonical_content_json=canonical_json(prd_payload),
            content_fingerprint=canonical_hash(prd_payload),
            created_at=EVALUATED_AT,
        )
        session.add(prd)
        session.flush()
        assert prd.prd_version_id is not None
        prd_decision = PrdDecision(
            project_id=project_id,
            discovery_run_id=run.discovery_run_id,
            prd_version_id=prd.prd_version_id,
            artifact_fingerprint=prd.content_fingerprint,
            decision="accepted",
            reviewer="operator@example.com",
            notes="Accepted historical PRD.",
            idempotency_key="task-13-history-prd",
            decided_at=EVALUATED_AT,
        )
        session.add(prd_decision)

        draft = SpecDraft(
            project_id=project_id,
            discovery_run_id=run.discovery_run_id,
            kind="initial",
            version_number=1,
            canonical_content_json=base.content,
            content_fingerprint=base.spec_hash,
            created_at=EVALUATED_AT,
        )
        session.add(draft)
        session.flush()
        assert draft.spec_draft_id is not None
        session.add(
            SpecDraftDecision(
                project_id=project_id,
                discovery_run_id=run.discovery_run_id,
                spec_draft_id=draft.spec_draft_id,
                artifact_fingerprint=draft.content_fingerprint,
                decision="accepted",
                reviewer="operator@example.com",
                notes="Accepted historical initial spec.",
                idempotency_key="task-13-history-spec",
                decided_at=EVALUATED_AT,
            )
        )
        session.add(
            InitialScopeRegistration(
                project_id=project_id,
                discovery_run_id=run.discovery_run_id,
                spec_draft_id=draft.spec_draft_id,
                spec_version_id=base.spec_version_id,
                spec_hash=base.spec_hash,
                registered_by="operator@example.com",
                registered_at=EVALUATED_AT,
            )
        )

        vision_payload = {"vision": "Deliver the accepted current scope."}
        vision = VisionArtifact(
            vision_artifact_id=900_001,
            project_id=project_id,
            authority_id=acceptance.pending_authority_id,
            authority_fingerprint=acceptance.authority_fingerprint,
            version_number=1,
            canonical_content_json=canonical_json(vision_payload),
            content_fingerprint=canonical_hash(vision_payload),
            created_by="operator@example.com",
            created_at=EVALUATED_AT,
        )
        session.add(vision)
        session.flush()
        assert vision.vision_artifact_id is not None
        session.add(
            VisionArtifactDecision(
                project_id=project_id,
                vision_artifact_id=vision.vision_artifact_id,
                artifact_fingerprint=vision.content_fingerprint,
                decision="accepted",
                rationale="Accepted historical Vision.",
                reviewer="operator@example.com",
                idempotency_key="task-13-history-vision",
                decided_at=EVALUATED_AT,
            )
        )
        session.commit()


def seed_terminal_project(engine: Engine) -> tuple[WorkflowDomain, int]:
    """Persist one fully triaged Project through Tasks 9-12 transitions."""
    project_id, sprint_id, story_id, task_id = seed_started_execution(engine)
    _seed_completed_project_history(engine, project_id)
    domain = _domain(engine)
    _complete_current_scope(
        domain,
        project_id=project_id,
        sprint_id=sprint_id,
        story_id=story_id,
        task_id=task_id,
    )
    return domain, project_id


def _current_spec(engine: Engine, project_id: int) -> SpecRegistry:
    with Session(engine) as session:
        return session.exec(
            select(SpecRegistry).where(
                col(SpecRegistry.product_id) == project_id,
                col(SpecRegistry.status) == "approved",
            )
        ).one()


def amended_spec(engine: Engine, project_id: int) -> JsonObject:
    """Add one accepted requirement to the exact stored base specification."""
    base = _current_spec(engine, project_id)
    assert base.spec_version_id is not None
    base_payload = json.loads(base.content)
    assert isinstance(base_payload, dict)
    return {
        "schema_version": "agileforge.spec.v1",
        "artifact_id": "SPEC.scope-extension",
        "title": "Scope extension workflow",
        "status": "draft",
        "version": "0.2",
        "created_at": "2026-08-02",
        "updated_at": "2026-08-02",
        "summary": "Extend the accepted current scope.",
        "problem_statement": "One additional capability is required.",
        "items": [
            {
                "id": "REQ.scope.extension",
                "type": "REQ",
                "status": "accepted",
                "level": "MUST",
                "title": "Extend scope",
                "statement": "The Project MUST support one additional capability.",
                "verification": "system-test",
                "acceptance": ["The amended capability is registered."],
            }
        ],
        "relations": [],
        "controlled_terms": [],
        "external_references": [],
        "rendering": {
            "markdown_profile": "agileforge.spec_markdown.v1",
            "rendered_markdown_sha256": None,
        },
    }


def start_extension(
    domain: WorkflowDomain,
    engine: Engine,
    project_id: int,
    *,
    idempotency_key: str = "task-13-start",
) -> tuple[StartScopeExtension, int]:
    """Start one extension from the currently advertised optional decision."""
    base = _current_spec(engine, project_id)
    assert base.spec_version_id is not None
    request = StartScopeExtension(
        **_guards(domain, project_id, "scope_extension.start"),
        idempotency_key=idempotency_key,
        base_spec_version_id=base.spec_version_id,
        base_spec_hash=base.spec_hash,
    )
    result = domain.transition(request)
    assert result.ok is True
    run_id = _output_int(result, "discovery_run_id")
    return request, run_id


def accept_amendment_draft(
    domain: WorkflowDomain,
    engine: Engine,
    project_id: int,
    run_id: int,
    *,
    provenance_path: Path | None = None,
) -> tuple[int, JsonObject]:
    """Persist challenge, PRD, amendment, and both human decisions."""
    run_instance = f"run:{run_id}"
    challenge = domain.transition(
        RecordExtensionChallenge(
            **_guards(
                domain,
                project_id,
                "scope_extension.challenge",
                run_instance,
            ),
            idempotency_key="task-13-challenge",
            canonical_content={"challenge": "Which capability is newly required?"},
            provenance_path="task-13/challenge.json",
        )
    )
    assert challenge.ok is True
    challenge_id = _output_int(challenge, "challenge_artifact_id")
    prd = domain.transition(
        RecordExtensionPrd(
            **_guards(domain, project_id, "scope_extension.prd", run_instance),
            idempotency_key="task-13-prd",
            challenge_artifact_id=challenge_id,
            canonical_content={"title": "Scope extension", "accepted": True},
            provenance_path="task-13/prd.json",
        )
    )
    assert prd.ok is True
    prd_id = _output_int(prd, "prd_version_id")
    prd_fingerprint = _output_str(prd, "content_fingerprint")
    prd_review = domain.transition(
        DecideExtensionPrd(
            **_guards(
                domain,
                project_id,
                "scope_extension.prd_review",
                f"prd:{prd_id}",
            ),
            idempotency_key="task-13-prd-review",
            prd_version_id=prd_id,
            artifact_fingerprint=prd_fingerprint,
            decision="accepted",
            notes="Accepted amendment PRD.",
        )
    )
    assert prd_review.ok is True

    base = _current_spec(engine, project_id)
    assert base.spec_version_id is not None
    content = amended_spec(engine, project_id)
    if provenance_path is not None:
        provenance_path.write_text(canonical_json(content), encoding="utf-8")
    draft = domain.transition(
        RecordAmendmentSpecDraft(
            **_guards(domain, project_id, "scope_extension.spec", run_instance),
            idempotency_key="task-13-spec",
            prd_version_id=prd_id,
            canonical_content=content,
            base_spec_version_id=base.spec_version_id,
            base_spec_hash=base.spec_hash,
            provenance_path=(None if provenance_path is None else str(provenance_path)),
        )
    )
    assert draft.ok is True
    draft_id = _output_int(draft, "spec_draft_id")
    draft_fingerprint = _output_str(draft, "content_fingerprint")
    draft_review = domain.transition(
        DecideAmendmentSpecDraft(
            **_guards(
                domain,
                project_id,
                "scope_extension.spec_review",
                f"spec:{draft_id}",
            ),
            idempotency_key="task-13-spec-review",
            spec_draft_id=draft_id,
            artifact_fingerprint=draft_fingerprint,
            decision="accepted",
            notes="Accepted additive amendment.",
        )
    )
    assert draft_review.ok is True
    return draft_id, content


def register_amendment(
    domain: WorkflowDomain,
    project_id: int,
    run_id: int,
    draft_id: int,
) -> RegisterScopeExtension:
    """Register the accepted amendment from the currently advertised decision."""
    request = RegisterScopeExtension(
        **_guards(
            domain,
            project_id,
            "scope_extension.registration",
            f"run:{run_id}",
        ),
        idempotency_key="task-13-register",
        spec_draft_id=draft_id,
    )
    result = domain.transition(request)
    assert result.ok is True
    return request


def test_start_pins_current_base_and_rejects_old_advertisement(
    engine: Engine,
) -> None:
    domain, project_id = seed_terminal_project(engine)
    terminal = domain.position(project_id)
    optional_start = _decision(terminal, "scope_extension.start")
    assert terminal.terminal is True
    assert optional_start.recommendation_kind.value == "optional_reentry"
    old_request, run_id = start_extension(domain, engine, project_id)

    with Session(engine) as session:
        run = session.get(DiscoveryRun, run_id)
        assert run is not None
        base = session.get(SpecRegistry, run.base_spec_version_id)
        assert base is not None
        assert run.purpose == "extension"
        assert run.base_spec_hash == base.spec_hash
        assert run.closed_at is None
        assert (
            len(
                session.exec(
                    select(DiscoveryRun).where(
                        col(DiscoveryRun.project_id) == project_id,
                        col(DiscoveryRun.purpose) == "extension",
                        col(DiscoveryRun.closed_at).is_(None),
                    )
                ).all()
            )
            == 1
        )

    stale = domain.transition(
        old_request.model_copy(update={"idempotency_key": "task-13-stale-start"})
    )
    assert stale.ok is False
    assert stale.error is not None
    assert stale.error.code is WorkflowErrorCode.STALE_POSITION


def test_amendment_requires_the_run_pinned_base(engine: Engine) -> None:
    domain, project_id = seed_terminal_project(engine)
    _request, run_id = start_extension(domain, engine, project_id)
    run_instance = f"run:{run_id}"
    challenge = domain.transition(
        RecordExtensionChallenge(
            **_guards(
                domain,
                project_id,
                "scope_extension.challenge",
                run_instance,
            ),
            idempotency_key="task-13-base-challenge",
            canonical_content={"challenge": "Pinned base"},
        )
    )
    prd = domain.transition(
        RecordExtensionPrd(
            **_guards(domain, project_id, "scope_extension.prd", run_instance),
            idempotency_key="task-13-base-prd",
            challenge_artifact_id=_output_int(challenge, "challenge_artifact_id"),
            canonical_content={"title": "Pinned base"},
        )
    )
    prd_id = _output_int(prd, "prd_version_id")
    assert domain.transition(
        DecideExtensionPrd(
            **_guards(
                domain,
                project_id,
                "scope_extension.prd_review",
                f"prd:{prd_id}",
            ),
            idempotency_key="task-13-base-prd-review",
            prd_version_id=prd_id,
            artifact_fingerprint=_output_str(prd, "content_fingerprint"),
            decision="accepted",
            notes="Accepted.",
        )
    ).ok
    base = _current_spec(engine, project_id)
    result = domain.transition(
        RecordAmendmentSpecDraft(
            **_guards(domain, project_id, "scope_extension.spec", run_instance),
            idempotency_key="task-13-wrong-base",
            prd_version_id=prd_id,
            canonical_content=amended_spec(engine, project_id),
            base_spec_version_id=(base.spec_version_id or 0) + 1,
            base_spec_hash=base.spec_hash,
        )
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    with Session(engine) as session:
        assert (
                session.exec(
                    select(SpecDraft).where(
                        col(SpecDraft.project_id) == project_id,
                        col(SpecDraft.kind) == "amendment",
                    )
                ).all()
            == []
        )


def test_registration_uses_accepted_stored_json_and_supersedes_base(
    engine: Engine,
) -> None:
    domain, project_id = seed_terminal_project(engine)
    _request, run_id = start_extension(domain, engine, project_id)
    draft_id, _content = accept_amendment_draft(
        domain,
        engine,
        project_id,
        run_id,
    )
    base_id = _current_spec(engine, project_id).spec_version_id

    register_amendment(domain, project_id, run_id, draft_id)

    with Session(engine) as session:
        registration = session.exec(
            select(ScopeExtensionRegistration).where(
                col(ScopeExtensionRegistration.discovery_run_id) == run_id
            )
        ).one()
        replacement = session.get(SpecRegistry, registration.spec_version_id)
        base = session.get(SpecRegistry, base_id)
        draft = session.get(SpecDraft, draft_id)
        assert replacement is not None
        assert base is not None
        assert draft is not None
        assert replacement.content == draft.canonical_content_json
        assert replacement.spec_hash == draft.content_fingerprint
        assert replacement.status == "approved"
        assert base.status == "superseded"


def test_abandon_unaccepted_run_closes_it_and_allows_new_decision(
    engine: Engine,
) -> None:
    domain, project_id = seed_terminal_project(engine)
    old_start = _decision(domain.position(project_id), "scope_extension.start")
    _request, run_id = start_extension(domain, engine, project_id)
    abandoned = domain.transition(
        AbandonScopeExtension(
            **_guards(
                domain,
                project_id,
                "scope_extension.abandon",
                f"run:{run_id}",
            ),
            idempotency_key="task-13-abandon",
            discovery_run_id=run_id,
            reason="The extension is no longer required.",
        )
    )
    assert abandoned.ok is True

    with Session(engine) as session:
        run = session.get(DiscoveryRun, run_id)
        assert run is not None
        assert run.closed_at == EVALUATED_AT.replace(tzinfo=None)
        fact = session.exec(
            select(DiscoveryRunAbandonment).where(
                col(DiscoveryRunAbandonment.discovery_run_id) == run_id
            )
        ).one()
        assert fact.reason == "The extension is no longer required."

    fresh = _decision(domain.position(project_id), "scope_extension.start")
    assert fresh.decision_fingerprint != old_start.decision_fingerprint


__all__ = [
    "EVALUATED_AT",
    "_current_spec",
    "_decision",
    "_domain",
    "_guards",
    "accept_amendment_draft",
    "register_amendment",
    "seed_terminal_project",
    "start_extension",
]
