"""Canonical workflow-fact repository tests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from sqlalchemy import event, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, col, create_engine, select

import repositories.workflow as workflow_repository_module
from models.core import Product, Sprint, SprintStory, Task, Team, UserStory
from models.db import set_sqlite_pragma
from models.enums import SprintStatus, StoryStatus, TaskStatus
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry
from models.workflow import (
    ChallengeArtifact,
    DiscoveryRun,
    DiscoveryRunAbandonment,
    InitialScopeRegistration,
    PrdDecision,
    PrdVersion,
    ProjectAbandonment,
    SpecDraft,
    SpecDraftDecision,
    WorkflowNodeAttempt,
    WorkflowNodeAttemptOutcome,
    WorkflowTransitionReceipt,
)
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from services.specs.authority_selection import pending_authority_fingerprint
from utils.spec_schemas import (
    Invariant,
    InvariantType,
    RequiredFieldParams,
    SpecAuthorityCompilationFailure,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerOutput,
)
from workflow.facts import (
    AuthorityFact,
    ChallengeArtifactFact,
    DiscoveryRunAbandonmentFact,
    DiscoveryRunFact,
    InitialScopeRegistrationFact,
    NodeAttemptFact,
    PrdVersionFact,
    ProjectAbandonmentFact,
    ProjectFact,
    ReviewDecisionFact,
    SpecDraftFact,
    SprintFact,
    StoryFact,
    TaskFact,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine


def sqlite_engine(path: Path) -> Engine:
    """Create a fresh file-backed SQLite engine with workflow tables."""
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    event.listen(engine, "connect", set_sqlite_pragma)
    SQLModel.metadata.create_all(engine)
    return engine


def _id(value: int | None) -> int:
    """Narrow a flushed SQLModel identity for test seeds."""
    assert value is not None
    return value


def _authority_json() -> str:
    """Build a valid persisted compiled-authority artifact."""
    return SpecAuthorityCompilerOutput(
        root=SpecAuthorityCompilationSuccess(
            scope_themes=["Workflow graph"],
            invariants=[
                Invariant(
                    id="INV-0123456789abcdef",
                    type=InvariantType.REQUIRED_FIELD,
                    parameters=RequiredFieldParams(field_name="project_id"),
                )
            ],
            eligible_feature_rules=[],
            gaps=[],
            assumptions=[],
            source_map=[],
            compiler_version="3.0.0",
            prompt_hash="a" * 64,
        )
    ).model_dump_json()


def _required_authority_fingerprint(authority: CompiledSpecAuthority) -> str:
    """Return the seeded authority fingerprint after identity assignment."""
    fingerprint = pending_authority_fingerprint(authority)
    assert fingerprint is not None
    return fingerprint


def _replace_first_authority_artifact(
    session: Session,
    compiled_artifact_json: str,
) -> None:
    """Replace canonical content while keeping its acceptance fingerprint current."""
    authority = session.exec(
        select(CompiledSpecAuthority).order_by(col(CompiledSpecAuthority.authority_id))
    ).first()
    assert authority is not None
    authority.compiled_artifact_json = compiled_artifact_json
    session.add(authority)
    session.flush()
    authority_id = _id(authority.authority_id)
    acceptance = session.exec(
        select(SpecAuthorityAcceptance).where(
            col(SpecAuthorityAcceptance.pending_authority_id) == authority_id
        )
    ).one()
    acceptance.authority_fingerprint = _required_authority_fingerprint(authority)
    session.add(acceptance)
    session.commit()


@dataclass(frozen=True)
class _OrderingSeed:
    """Persisted identities needed to add ordering rows."""

    project_id: int
    team_id: int
    initial_run_id: int
    spec_id: int
    spec_hash: str
    first_prd_id: int
    first_draft_id: int
    recorded_at: datetime


def _seed_additional_ordering_rows(
    session: Session,
    seed: _OrderingSeed,
) -> None:
    """Add second rows whose persisted order exercises every collection sort."""
    second_prd = PrdVersion(
        project_id=seed.project_id,
        discovery_run_id=seed.initial_run_id,
        version_number=2,
        canonical_content_json=json.dumps({"prd": "second"}),
        content_fingerprint="sha256:prd:second",
        supersedes_prd_version_id=seed.first_prd_id,
    )
    second_draft = SpecDraft(
        project_id=seed.project_id,
        discovery_run_id=seed.initial_run_id,
        kind="initial",
        version_number=2,
        canonical_content_json=json.dumps({"spec": "second"}),
        content_fingerprint="sha256:draft:second",
        base_spec_version_id=None,
        base_spec_hash=None,
        supersedes_spec_draft_id=seed.first_draft_id,
    )
    session.add_all([second_prd, second_draft])
    session.flush()
    session.add_all(
        [
            PrdDecision(
                project_id=seed.project_id,
                discovery_run_id=seed.initial_run_id,
                prd_version_id=_id(second_prd.prd_version_id),
                artifact_fingerprint=second_prd.content_fingerprint,
                decision="accepted",
                reviewer="reviewer",
                notes="accepted",
                idempotency_key="prd-review-second",
                decided_at=seed.recorded_at,
            ),
            SpecDraftDecision(
                project_id=seed.project_id,
                discovery_run_id=seed.initial_run_id,
                spec_draft_id=_id(second_draft.spec_draft_id),
                artifact_fingerprint=second_draft.content_fingerprint,
                decision="accepted",
                reviewer="reviewer",
                notes="accepted",
                idempotency_key="draft-review-second",
                decided_at=seed.recorded_at,
            ),
        ]
    )
    authority = CompiledSpecAuthority(
        spec_version_id=seed.spec_id,
        compiler_version="3.0.0",
        prompt_hash="a" * 64,
        compiled_at=seed.recorded_at + timedelta(seconds=1),
        scope_themes='["Workflow graph"]',
        invariants="[]",
        eligible_feature_ids="[]",
        rejected_features="[]",
        spec_gaps="[]",
        compiled_artifact_json=_authority_json(),
    )
    session.add(authority)
    session.flush()
    session.add(
        SpecAuthorityAcceptance(
            product_id=seed.project_id,
            spec_version_id=seed.spec_id,
            status="accepted",
            policy="test",
            decided_by="reviewer",
            decided_at=seed.recorded_at,
            compiler_version=authority.compiler_version,
            prompt_hash=authority.prompt_hash,
            spec_hash=seed.spec_hash,
            pending_authority_id=_id(authority.authority_id),
            authority_fingerprint=_required_authority_fingerprint(authority),
        )
    )
    story = UserStory(
        product_id=seed.project_id,
        title="Second accepted story",
        status=StoryStatus.ACCEPTED,
        rank="A",
        accepted_spec_version_id=seed.spec_id,
    )
    session.add(story)
    session.flush()
    sprint = Sprint(
        product_id=seed.project_id,
        team_id=seed.team_id,
        status=SprintStatus.COMPLETED,
        completed_at=seed.recorded_at - timedelta(minutes=1),
    )
    session.add(sprint)
    session.flush()
    session.add(
        SprintStory(
            sprint_id=_id(sprint.sprint_id),
            story_id=_id(story.story_id),
        )
    )
    session.add(
        Task(
            story_id=_id(story.story_id),
            description="Second ordered task",
            status=TaskStatus.TO_DO,
        )
    )
    attempt = WorkflowNodeAttempt(
        project_id=seed.project_id,
        node_id="discovery.challenge",
        instance_key="earlier",
        graph_version="agileforge.workflow.v1",
        fact_fingerprint="sha256:facts:earlier",
        business_fact_fingerprint="sha256:business:earlier",
        decision_fingerprint="sha256:decision:earlier",
        normalized_input_json="{}",
        input_fingerprint="sha256:input:earlier",
        model_id="test-model",
        execution_settings_json="{}",
        idempotency_key="attempt-earlier",
        actor="test",
        correlation_id=None,
        started_at=seed.recorded_at - timedelta(minutes=1),
        lease_expires_at=seed.recorded_at + timedelta(minutes=4),
        attempt_fingerprint="sha256:attempt:earlier",
    )
    session.add(attempt)
    session.flush()
    session.add(
        WorkflowNodeAttemptOutcome(
            project_id=seed.project_id,
            workflow_node_attempt_id=_id(attempt.workflow_node_attempt_id),
            status="obsolete",
            output_fingerprint=None,
            output_json=None,
            failure_code=None,
            failure_message=None,
            recorded_at=seed.recorded_at,
        )
    )


def seed_complete_project(engine: Engine, *, name: str = "Repository Test") -> int:
    """Persist a complete canonical fact set for one Project."""
    recorded_at = datetime(2026, 8, 2, 12, tzinfo=UTC)
    with Session(engine) as session:
        project = Product(name=name, origin="brownfield", vision="legacy vision")
        team = Team(name=f"{name} team")
        session.add_all([project, team])
        session.flush()
        project_id = _id(project.product_id)

        initial_run = DiscoveryRun(
            project_id=project_id,
            purpose="initial",
            ordinal=1,
            created_at=recorded_at,
        )
        session.add(initial_run)
        session.flush()
        initial_run_id = _id(initial_run.discovery_run_id)

        first_challenge = ChallengeArtifact(
            project_id=project_id,
            discovery_run_id=initial_run_id,
            version_number=1,
            canonical_content_json=json.dumps({"challenge": "first"}),
            content_fingerprint="sha256:challenge:first",
            supersedes_challenge_artifact_id=None,
            provenance_path="/missing/challenge.md",
        )
        session.add(first_challenge)
        session.flush()
        second_challenge = ChallengeArtifact(
            project_id=project_id,
            discovery_run_id=initial_run_id,
            version_number=2,
            canonical_content_json=json.dumps({"challenge": "second"}),
            content_fingerprint="sha256:challenge:second",
            supersedes_challenge_artifact_id=_id(first_challenge.challenge_artifact_id),
            provenance_path="/missing/challenge-v2.md",
        )
        prd = PrdVersion(
            project_id=project_id,
            discovery_run_id=initial_run_id,
            version_number=1,
            canonical_content_json=json.dumps({"prd": "accepted"}),
            content_fingerprint="sha256:prd",
            supersedes_prd_version_id=None,
            provenance_path="/missing/prd.md",
        )
        draft = SpecDraft(
            project_id=project_id,
            discovery_run_id=initial_run_id,
            kind="initial",
            version_number=1,
            canonical_content_json=json.dumps({"spec": "accepted"}),
            content_fingerprint="sha256:draft",
            base_spec_version_id=None,
            base_spec_hash=None,
            supersedes_spec_draft_id=None,
            provenance_path="/missing/spec.md",
        )
        spec = SpecRegistry(
            product_id=project_id,
            spec_hash="sha256:spec",
            content="# Canonical spec",
            status="approved",
        )
        session.add_all([second_challenge, prd, draft, spec])
        session.flush()
        prd_id = _id(prd.prd_version_id)
        draft_id = _id(draft.spec_draft_id)
        spec_id = _id(spec.spec_version_id)
        session.add(
            DiscoveryRun(
                project_id=project_id,
                purpose="extension",
                ordinal=1,
                base_spec_version_id=spec_id,
                base_spec_hash=spec.spec_hash,
                created_at=recorded_at + timedelta(seconds=1),
                closed_at=recorded_at + timedelta(minutes=1),
            )
        )
        session.flush()

        session.add(
            PrdDecision(
                project_id=project_id,
                discovery_run_id=initial_run_id,
                prd_version_id=prd_id,
                artifact_fingerprint=prd.content_fingerprint,
                decision="accepted",
                reviewer="reviewer",
                notes="accepted",
                idempotency_key="prd-review",
                decided_at=recorded_at,
            )
        )
        session.add(
            SpecDraftDecision(
                project_id=project_id,
                discovery_run_id=initial_run_id,
                spec_draft_id=draft_id,
                artifact_fingerprint=draft.content_fingerprint,
                decision="accepted",
                reviewer="reviewer",
                notes="accepted",
                idempotency_key="draft-review",
                decided_at=recorded_at,
            )
        )
        session.add(
            InitialScopeRegistration(
                project_id=project_id,
                discovery_run_id=initial_run_id,
                spec_draft_id=draft_id,
                spec_version_id=spec_id,
                spec_hash=spec.spec_hash,
                registered_by="reviewer",
                registered_at=recorded_at,
            )
        )
        authority = CompiledSpecAuthority(
            spec_version_id=spec_id,
            compiler_version="3.0.0",
            prompt_hash="a" * 64,
            compiled_at=recorded_at,
            scope_themes='["Workflow graph"]',
            invariants="[]",
            eligible_feature_ids="[]",
            rejected_features="[]",
            spec_gaps="[]",
            compiled_artifact_json=_authority_json(),
        )
        session.add(authority)
        session.flush()
        session.add(
            SpecAuthorityAcceptance(
                product_id=project_id,
                spec_version_id=spec_id,
                status="accepted",
                policy="test",
                decided_by="reviewer",
                decided_at=recorded_at,
                compiler_version="3.0.0",
                prompt_hash="a" * 64,
                spec_hash=spec.spec_hash,
                pending_authority_id=_id(authority.authority_id),
                authority_fingerprint=_required_authority_fingerprint(authority),
            )
        )
        story = UserStory(
            product_id=project_id,
            title="Accepted backlog story",
            status=StoryStatus.ACCEPTED,
            rank="B",
            accepted_spec_version_id=spec_id,
        )
        session.add(story)
        session.flush()
        story_id = _id(story.story_id)
        sprint = Sprint(
            product_id=project_id,
            team_id=_id(team.team_id),
            status=SprintStatus.COMPLETED,
            completed_at=recorded_at,
            close_snapshot_json=json.dumps({"post_sprint_triage": {"impact": "none"}}),
        )
        session.add(sprint)
        session.flush()
        sprint_id = _id(sprint.sprint_id)
        session.add(SprintStory(sprint_id=sprint_id, story_id=story_id))
        session.add(
            Task(
                story_id=story_id,
                description="Implement canonical facts",
                status=TaskStatus.DONE,
            )
        )
        attempt = WorkflowNodeAttempt(
            project_id=project_id,
            node_id="authority.compile",
            instance_key=None,
            graph_version="agileforge.workflow.v1",
            fact_fingerprint="sha256:facts",
            business_fact_fingerprint="sha256:business",
            decision_fingerprint="sha256:decision",
            normalized_input_json="{}",
            input_fingerprint="sha256:input",
            model_id="test-model",
            execution_settings_json="{}",
            idempotency_key="attempt",
            actor="test",
            correlation_id=None,
            started_at=recorded_at,
            lease_expires_at=recorded_at + timedelta(minutes=5),
            attempt_fingerprint="sha256:attempt",
        )
        session.add(attempt)
        session.flush()
        session.add(
            WorkflowNodeAttemptOutcome(
                project_id=project_id,
                workflow_node_attempt_id=_id(attempt.workflow_node_attempt_id),
                status="success",
                output_fingerprint="sha256:output",
                output_json="{}",
                failure_code=None,
                failure_message=None,
                recorded_at=recorded_at,
            )
        )
        _seed_additional_ordering_rows(
            session,
            _OrderingSeed(
                project_id=project_id,
                team_id=_id(team.team_id),
                initial_run_id=initial_run_id,
                spec_id=spec_id,
                spec_hash=spec.spec_hash,
                first_prd_id=prd_id,
                first_draft_id=draft_id,
                recorded_at=recorded_at,
            ),
        )
        session.add(
            WorkflowTransitionReceipt(
                request_kind="legacy.triage",
                idempotency_key=f"ignored-ledger:{name}",
                request_fingerprint="sha256:receipt",
                request_json=json.dumps(
                    {"post_sprint_triage": {"impact": "specification"}}
                ),
                result_json=None,
                started_at=recorded_at,
            )
        )
        session.commit()
    return project_id


@dataclass(frozen=True)
class _ForcedCorruption:
    """One FK-disabled relationship mutation expected to fail closed."""

    name: str
    statements: tuple[str, ...]


_FORCED_CORRUPTIONS: tuple[_ForcedCorruption, ...] = (
    _ForcedCorruption(
        "discovery_abandonment_run",
        (
            "INSERT INTO discovery_run_abandonments "
            "(project_id, discovery_run_id, reason, abandoned_by, abandoned_at) "
            "SELECT :target_id, discovery_run_id, 'corrupt', 'test', "
            "'2026-08-02 12:00:00' FROM discovery_runs "
            "WHERE project_id = :foreign_id ORDER BY discovery_run_id LIMIT 1",
        ),
    ),
    _ForcedCorruption(
        "challenge_run",
        (
            "UPDATE challenge_artifacts SET discovery_run_id = "
            "(SELECT discovery_run_id FROM discovery_runs "
            "WHERE project_id = :foreign_id ORDER BY discovery_run_id LIMIT 1) "
            "WHERE project_id = :target_id AND version_number = 1",
        ),
    ),
    _ForcedCorruption(
        "challenge_supersession",
        (
            "UPDATE challenge_artifacts SET supersedes_challenge_artifact_id = "
            "(SELECT challenge_artifact_id FROM challenge_artifacts "
            "WHERE project_id = :foreign_id ORDER BY challenge_artifact_id LIMIT 1) "
            "WHERE project_id = :target_id AND version_number = 2",
        ),
    ),
    _ForcedCorruption(
        "prd_run",
        (
            "UPDATE prd_versions SET discovery_run_id = "
            "(SELECT discovery_run_id FROM discovery_runs "
            "WHERE project_id = :foreign_id ORDER BY discovery_run_id LIMIT 1) "
            "WHERE project_id = :target_id",
        ),
    ),
    _ForcedCorruption(
        "prd_supersession",
        (
            "UPDATE prd_versions SET supersedes_prd_version_id = "
            "(SELECT prd_version_id FROM prd_versions "
            "WHERE project_id = :foreign_id ORDER BY prd_version_id LIMIT 1) "
            "WHERE project_id = :target_id",
        ),
    ),
    _ForcedCorruption(
        "prd_decision_run",
        (
            "UPDATE prd_decisions SET discovery_run_id = "
            "(SELECT discovery_run_id FROM discovery_runs "
            "WHERE project_id = :target_id AND purpose = 'extension') "
            "WHERE project_id = :target_id",
        ),
    ),
    _ForcedCorruption(
        "prd_decision_parent",
        (
            "UPDATE prd_decisions SET prd_version_id = "
            "(SELECT prd_version_id FROM prd_versions "
            "WHERE project_id = :foreign_id ORDER BY prd_version_id LIMIT 1) "
            "WHERE prd_decision_id = (SELECT prd_decision_id FROM prd_decisions "
            "WHERE project_id = :target_id ORDER BY prd_decision_id LIMIT 1)",
        ),
    ),
    _ForcedCorruption(
        "prd_decision_fingerprint",
        (
            "UPDATE prd_decisions SET artifact_fingerprint = 'sha256:corrupt' "
            "WHERE project_id = :target_id",
        ),
    ),
    _ForcedCorruption(
        "spec_draft_run",
        (
            "UPDATE spec_drafts SET discovery_run_id = "
            "(SELECT discovery_run_id FROM discovery_runs "
            "WHERE project_id = :foreign_id ORDER BY discovery_run_id LIMIT 1) "
            "WHERE project_id = :target_id",
        ),
    ),
    _ForcedCorruption(
        "spec_draft_base_spec",
        (
            "UPDATE spec_drafts SET kind = 'amendment', "
            "base_spec_version_id = (SELECT spec_version_id FROM spec_registry "
            "WHERE product_id = :foreign_id ORDER BY spec_version_id LIMIT 1), "
            "base_spec_hash = (SELECT spec_hash FROM spec_registry "
            "WHERE product_id = :foreign_id ORDER BY spec_version_id LIMIT 1) "
            "WHERE project_id = :target_id",
        ),
    ),
    _ForcedCorruption(
        "spec_draft_base_hash",
        (
            "UPDATE spec_drafts SET kind = 'amendment', "
            "base_spec_version_id = (SELECT spec_version_id FROM spec_registry "
            "WHERE product_id = :target_id ORDER BY spec_version_id LIMIT 1), "
            "base_spec_hash = 'sha256:corrupt' WHERE project_id = :target_id",
        ),
    ),
    _ForcedCorruption(
        "spec_draft_supersession",
        (
            "UPDATE spec_drafts SET supersedes_spec_draft_id = "
            "(SELECT spec_draft_id FROM spec_drafts "
            "WHERE project_id = :foreign_id ORDER BY spec_draft_id LIMIT 1) "
            "WHERE project_id = :target_id",
        ),
    ),
    _ForcedCorruption(
        "spec_decision_run",
        (
            "UPDATE spec_draft_decisions SET discovery_run_id = "
            "(SELECT discovery_run_id FROM discovery_runs "
            "WHERE project_id = :target_id AND purpose = 'extension') "
            "WHERE project_id = :target_id",
        ),
    ),
    _ForcedCorruption(
        "spec_decision_parent",
        (
            "UPDATE spec_draft_decisions SET spec_draft_id = "
            "(SELECT spec_draft_id FROM spec_drafts "
            "WHERE project_id = :foreign_id ORDER BY spec_draft_id LIMIT 1) "
            "WHERE spec_draft_decision_id = "
            "(SELECT spec_draft_decision_id FROM spec_draft_decisions "
            "WHERE project_id = :target_id "
            "ORDER BY spec_draft_decision_id LIMIT 1)",
        ),
    ),
    _ForcedCorruption(
        "spec_decision_fingerprint",
        (
            "UPDATE spec_draft_decisions "
            "SET artifact_fingerprint = 'sha256:corrupt' "
            "WHERE project_id = :target_id",
        ),
    ),
    _ForcedCorruption(
        "registration_run",
        (
            "UPDATE initial_scope_registrations SET discovery_run_id = "
            "(SELECT discovery_run_id FROM discovery_runs "
            "WHERE project_id = :target_id AND purpose = 'extension') "
            "WHERE project_id = :target_id",
        ),
    ),
    _ForcedCorruption(
        "registration_draft",
        (
            "DELETE FROM initial_scope_registrations WHERE project_id = :foreign_id",
            "UPDATE initial_scope_registrations SET spec_draft_id = "
            "(SELECT spec_draft_id FROM spec_drafts "
            "WHERE project_id = :foreign_id ORDER BY spec_draft_id LIMIT 1) "
            "WHERE project_id = :target_id",
        ),
    ),
    _ForcedCorruption(
        "registration_spec",
        (
            "DELETE FROM initial_scope_registrations WHERE project_id = :foreign_id",
            "UPDATE initial_scope_registrations SET "
            "spec_version_id = (SELECT spec_version_id FROM spec_registry "
            "WHERE product_id = :foreign_id ORDER BY spec_version_id LIMIT 1), "
            "spec_hash = (SELECT spec_hash FROM spec_registry "
            "WHERE product_id = :foreign_id ORDER BY spec_version_id LIMIT 1) "
            "WHERE project_id = :target_id",
        ),
    ),
    _ForcedCorruption(
        "registration_spec_hash",
        (
            "UPDATE initial_scope_registrations SET spec_hash = 'sha256:corrupt' "
            "WHERE project_id = :target_id",
        ),
    ),
    _ForcedCorruption(
        "compiled_authority_spec",
        (
            "UPDATE compiled_spec_authority SET spec_version_id = "
            "(SELECT spec_version_id FROM spec_registry "
            "WHERE product_id = :foreign_id ORDER BY spec_version_id LIMIT 1) "
            "WHERE authority_id = (SELECT pending_authority_id "
            "FROM spec_authority_acceptance WHERE product_id = :target_id)",
        ),
    ),
    _ForcedCorruption(
        "acceptance_spec",
        (
            "UPDATE spec_authority_acceptance SET "
            "spec_version_id = (SELECT spec_version_id FROM spec_registry "
            "WHERE product_id = :foreign_id ORDER BY spec_version_id LIMIT 1), "
            "spec_hash = (SELECT spec_hash FROM spec_registry "
            "WHERE product_id = :foreign_id ORDER BY spec_version_id LIMIT 1) "
            "WHERE product_id = :target_id",
        ),
    ),
    _ForcedCorruption(
        "acceptance_authority",
        (
            "UPDATE spec_authority_acceptance SET pending_authority_id = "
            "(SELECT authority_id FROM compiled_spec_authority "
            "WHERE spec_version_id = (SELECT spec_version_id FROM spec_registry "
            "WHERE product_id = :foreign_id ORDER BY spec_version_id LIMIT 1)) "
            "WHERE product_id = :target_id",
        ),
    ),
    _ForcedCorruption(
        "acceptance_missing_authority",
        (
            "UPDATE spec_authority_acceptance SET pending_authority_id = NULL "
            "WHERE product_id = :target_id",
        ),
    ),
    _ForcedCorruption(
        "acceptance_compiler",
        (
            "UPDATE spec_authority_acceptance SET compiler_version = 'corrupt' "
            "WHERE product_id = :target_id",
        ),
    ),
    _ForcedCorruption(
        "acceptance_prompt_hash",
        (
            "UPDATE spec_authority_acceptance SET prompt_hash = 'corrupt' "
            "WHERE product_id = :target_id",
        ),
    ),
    _ForcedCorruption(
        "acceptance_spec_hash",
        (
            "UPDATE spec_authority_acceptance SET spec_hash = 'sha256:corrupt' "
            "WHERE product_id = :target_id",
        ),
    ),
    _ForcedCorruption(
        "acceptance_authority_fingerprint",
        (
            "UPDATE spec_authority_acceptance "
            "SET authority_fingerprint = 'sha256:corrupt' "
            "WHERE product_id = :target_id",
        ),
    ),
    _ForcedCorruption(
        "story_accepted_spec",
        (
            "UPDATE user_stories SET accepted_spec_version_id = "
            "(SELECT spec_version_id FROM spec_registry "
            "WHERE product_id = :foreign_id ORDER BY spec_version_id LIMIT 1) "
            "WHERE product_id = :target_id",
        ),
    ),
    _ForcedCorruption(
        "story_supersession",
        (
            "UPDATE user_stories SET is_superseded = 1, "
            "superseded_by_story_id = (SELECT story_id FROM user_stories "
            "WHERE product_id = :foreign_id ORDER BY story_id LIMIT 1) "
            "WHERE product_id = :target_id",
        ),
    ),
    _ForcedCorruption(
        "dependency_dependent_story",
        (
            "INSERT INTO user_story_dependencies "
            "(product_id, dependent_story_id, prerequisite_story_id, "
            "status, source, confidence, created_at, updated_at) SELECT "
            ":target_id, foreign_story.story_id, target_story.story_id, "
            "'active', 'manual_review', 'reviewed', "
            "'2026-08-02 12:00:00', '2026-08-02 12:00:00' "
            "FROM user_stories AS foreign_story, user_stories AS target_story "
            "WHERE foreign_story.product_id = :foreign_id "
            "AND target_story.product_id = :target_id LIMIT 1",
        ),
    ),
    _ForcedCorruption(
        "dependency_prerequisite_story",
        (
            "INSERT INTO user_story_dependencies "
            "(product_id, dependent_story_id, prerequisite_story_id, "
            "status, source, confidence, created_at, updated_at) SELECT "
            ":target_id, target_story.story_id, foreign_story.story_id, "
            "'active', 'manual_review', 'reviewed', "
            "'2026-08-02 12:00:00', '2026-08-02 12:00:00' "
            "FROM user_stories AS foreign_story, user_stories AS target_story "
            "WHERE foreign_story.product_id = :foreign_id "
            "AND target_story.product_id = :target_id LIMIT 1",
        ),
    ),
    _ForcedCorruption(
        "attempt_outcome",
        (
            "DELETE FROM workflow_node_attempt_outcomes WHERE project_id = :foreign_id",
            "UPDATE workflow_node_attempt_outcomes "
            "SET workflow_node_attempt_id = "
            "(SELECT workflow_node_attempt_id FROM workflow_node_attempts "
            "WHERE project_id = :foreign_id "
            "ORDER BY workflow_node_attempt_id LIMIT 1) "
            "WHERE workflow_node_attempt_outcome_id = "
            "(SELECT workflow_node_attempt_outcome_id "
            "FROM workflow_node_attempt_outcomes WHERE project_id = :target_id "
            "ORDER BY workflow_node_attempt_outcome_id LIMIT 1)",
        ),
    ),
)


def _force_corruption(
    engine: Engine,
    corruption: _ForcedCorruption,
    *,
    target_id: int,
    foreign_id: int,
) -> None:
    """Apply one corruption while bypassing only foreign-key enforcement."""
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        for statement in corruption.statements:
            connection.execute(
                text(statement),
                {"target_id": target_id, "foreign_id": foreign_id},
            )
        connection.commit()


def test_load_maps_complete_canonical_snapshot_in_deterministic_order(
    tmp_path: Path,
) -> None:
    """Map named authoritative rows without leaking persistence records."""
    engine = sqlite_engine(tmp_path / "workflow.db")
    project_id = seed_complete_project(engine)
    recorded_at = datetime(2026, 8, 2, 12, tzinfo=UTC)
    persisted_at = recorded_at.replace(tzinfo=None)

    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)

    assert type(snapshot.project) is ProjectFact
    assert (
        snapshot.project.project_id,
        snapshot.project.name,
        snapshot.project.origin,
    ) == (project_id, "Repository Test", "brownfield")
    assert snapshot.project_abandonments == ()
    assert tuple(type(item) for item in snapshot.discovery_runs) == (
        DiscoveryRunFact,
        DiscoveryRunFact,
    )
    assert tuple(
        (
            item.purpose,
            item.ordinal,
            item.base_spec_version_id,
            item.base_spec_hash,
            item.created_at,
            item.closed_at,
        )
        for item in snapshot.discovery_runs
    ) == (
        ("initial", 1, None, None, persisted_at, None),
        (
            "extension",
            1,
            snapshot.spec_versions[0].spec_version_id,
            snapshot.spec_versions[0].spec_hash,
            persisted_at + timedelta(seconds=1),
            persisted_at + timedelta(minutes=1),
        ),
    )
    assert snapshot.discovery_run_abandonments == ()
    assert tuple(type(item) for item in snapshot.challenge_artifacts) == (
        ChallengeArtifactFact,
        ChallengeArtifactFact,
    )
    assert tuple(item.content_fingerprint for item in snapshot.challenge_artifacts) == (
        "sha256:challenge:first",
        "sha256:challenge:second",
    )
    assert snapshot.challenge_artifacts[0].supersedes_id is None
    assert (
        snapshot.challenge_artifacts[1].supersedes_id
        == snapshot.challenge_artifacts[0].challenge_artifact_id
    )
    assert tuple(type(item) for item in snapshot.prd_versions) == (
        PrdVersionFact,
        PrdVersionFact,
    )
    assert tuple(
        (item.discovery_run_id, item.content_fingerprint, item.supersedes_id)
        for item in snapshot.prd_versions
    ) == (
        (
            snapshot.discovery_runs[0].discovery_run_id,
            "sha256:prd",
            None,
        ),
        (
            snapshot.discovery_runs[0].discovery_run_id,
            "sha256:prd:second",
            snapshot.prd_versions[0].prd_version_id,
        ),
    )
    assert tuple(type(item) for item in snapshot.review_decisions) == (
        ReviewDecisionFact,
        ReviewDecisionFact,
        ReviewDecisionFact,
        ReviewDecisionFact,
        ReviewDecisionFact,
        ReviewDecisionFact,
    )
    assert tuple(
        (item.artifact_type, item.artifact_fingerprint, item.decision)
        for item in snapshot.review_decisions
    ) == (
        (
            "authority",
            snapshot.authorities[0].authority_fingerprint,
            "accepted",
        ),
        (
            "authority",
            snapshot.authorities[1].authority_fingerprint,
            "accepted",
        ),
        ("prd", "sha256:prd", "accepted"),
        ("prd", "sha256:prd:second", "accepted"),
        ("spec_draft", "sha256:draft", "accepted"),
        ("spec_draft", "sha256:draft:second", "accepted"),
    )
    assert tuple(type(item) for item in snapshot.spec_drafts) == (
        SpecDraftFact,
        SpecDraftFact,
    )
    assert tuple(
        (
            item.discovery_run_id,
            item.kind,
            item.content_fingerprint,
            item.base_spec_version_id,
            item.base_spec_hash,
            item.supersedes_id,
        )
        for item in snapshot.spec_drafts
    ) == (
        (
            snapshot.discovery_runs[0].discovery_run_id,
            "initial",
            "sha256:draft",
            None,
            None,
            None,
        ),
        (
            snapshot.discovery_runs[0].discovery_run_id,
            "initial",
            "sha256:draft:second",
            None,
            None,
            snapshot.spec_drafts[0].spec_draft_id,
        ),
    )
    assert tuple(type(item) for item in snapshot.initial_registrations) == (
        InitialScopeRegistrationFact,
    )
    assert tuple(
        (
            item.discovery_run_id,
            item.spec_draft_id,
            item.spec_version_id,
            item.spec_hash,
        )
        for item in snapshot.initial_registrations
    ) == (
        (
            snapshot.discovery_runs[0].discovery_run_id,
            snapshot.spec_drafts[0].spec_draft_id,
            snapshot.authorities[0].spec_version_id,
            "sha256:spec",
        ),
    )
    assert tuple(type(item) for item in snapshot.authorities) == (
        AuthorityFact,
        AuthorityFact,
    )
    assert tuple(
        (item.spec_version_id, item.status, item.decided_at)
        for item in snapshot.authorities
    ) == (
        (snapshot.initial_registrations[0].spec_version_id, "accepted", persisted_at),
        (snapshot.initial_registrations[0].spec_version_id, "accepted", persisted_at),
    )
    assert snapshot.phase_artifacts == ()
    assert tuple(type(item) for item in snapshot.sprints) == (
        SprintFact,
        SprintFact,
    )
    assert tuple((item.status, item.completed_at) for item in snapshot.sprints) == (
        ("completed", persisted_at - timedelta(minutes=1)),
        ("completed", persisted_at),
    )
    assert tuple(type(item) for item in snapshot.stories) == (
        StoryFact,
        StoryFact,
    )
    assert tuple(
        (item.status, item.sprint_candidate, item.readiness_blockers)
        for item in snapshot.stories
    ) == (
        (StoryStatus.ACCEPTED.value, True, ()),
        (StoryStatus.ACCEPTED.value, True, ()),
    )
    assert tuple(type(item) for item in snapshot.tasks) == (TaskFact, TaskFact)
    assert tuple(
        (
            item.sprint_id,
            item.story_id,
            item.status,
            item.dependencies_satisfied,
        )
        for item in snapshot.tasks
    ) == (
        (
            snapshot.sprints[1].sprint_id,
            snapshot.stories[1].story_id,
            TaskStatus.DONE.value,
            True,
        ),
        (
            snapshot.sprints[0].sprint_id,
            snapshot.stories[0].story_id,
            TaskStatus.TO_DO.value,
            True,
        ),
    )
    assert snapshot.post_sprint_triage == ()
    assert tuple(type(item) for item in snapshot.node_attempts) == (
        NodeAttemptFact,
        NodeAttemptFact,
    )
    assert tuple(
        (
            item.node_id,
            item.graph_version,
            item.input_fingerprint,
            item.fact_fingerprint,
            item.business_fact_fingerprint,
            item.decision_fingerprint,
            item.attempt_fingerprint,
            item.outcome,
        )
        for item in snapshot.node_attempts
    ) == (
        (
            "discovery.challenge",
            "agileforge.workflow.v1",
            "sha256:input:earlier",
            "sha256:facts:earlier",
            "sha256:business:earlier",
            "sha256:decision:earlier",
            "sha256:attempt:earlier",
            "obsolete",
        ),
        (
            "authority.compile",
            "agileforge.workflow.v1",
            "sha256:input",
            "sha256:facts",
            "sha256:business",
            "sha256:decision",
            "sha256:attempt",
            "success",
        ),
    )


def test_load_populates_abandonment_collections_in_deterministic_order(
    tmp_path: Path,
) -> None:
    """Map every authoritative abandonment row in persisted time order."""
    engine = sqlite_engine(tmp_path / "workflow.db")
    recorded_at = datetime(2026, 8, 2, 12, tzinfo=UTC)
    persisted_at = recorded_at.replace(tzinfo=None)
    with Session(engine) as session:
        project = Product(name="Abandoned", origin="greenfield")
        session.add(project)
        session.flush()
        project_id = _id(project.product_id)
        base_spec = SpecRegistry(
            product_id=project_id,
            spec_hash="sha256:abandoned-base",
            content="# Abandoned base",
            status="approved",
        )
        session.add(base_spec)
        session.flush()
        base_spec_id = _id(base_spec.spec_version_id)
        initial = DiscoveryRun(
            project_id=project_id,
            purpose="initial",
            ordinal=1,
            created_at=recorded_at,
        )
        extension = DiscoveryRun(
            project_id=project_id,
            purpose="extension",
            ordinal=1,
            base_spec_version_id=base_spec_id,
            base_spec_hash=base_spec.spec_hash,
            created_at=recorded_at + timedelta(seconds=1),
        )
        session.add_all([initial, extension])
        session.flush()
        session.add(
            ProjectAbandonment(
                project_id=project_id,
                reason="Project stopped",
                abandoned_by="reviewer",
                abandoned_at=recorded_at + timedelta(minutes=3),
            )
        )
        session.add_all(
            [
                DiscoveryRunAbandonment(
                    project_id=project_id,
                    discovery_run_id=_id(initial.discovery_run_id),
                    reason="Initial stopped later",
                    abandoned_by="reviewer",
                    abandoned_at=recorded_at + timedelta(minutes=2),
                ),
                DiscoveryRunAbandonment(
                    project_id=project_id,
                    discovery_run_id=_id(extension.discovery_run_id),
                    reason="Extension stopped first",
                    abandoned_by="reviewer",
                    abandoned_at=recorded_at + timedelta(minutes=1),
                ),
            ]
        )
        session.commit()

    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)

    assert tuple(type(item) for item in snapshot.project_abandonments) == (
        ProjectAbandonmentFact,
    )
    assert tuple(
        (item.reason, item.abandoned_by, item.abandoned_at)
        for item in snapshot.project_abandonments
    ) == (("Project stopped", "reviewer", persisted_at + timedelta(minutes=3)),)
    assert tuple(type(item) for item in snapshot.discovery_run_abandonments) == (
        DiscoveryRunAbandonmentFact,
        DiscoveryRunAbandonmentFact,
    )
    assert tuple(
        (item.discovery_run_id, item.reason, item.abandoned_at)
        for item in snapshot.discovery_run_abandonments
    ) == (
        (
            snapshot.discovery_runs[1].discovery_run_id,
            "Extension stopped first",
            persisted_at + timedelta(minutes=1),
        ),
        (
            snapshot.discovery_runs[0].discovery_run_id,
            "Initial stopped later",
            persisted_at + timedelta(minutes=2),
        ),
    )


def test_load_does_not_commit_or_rollback_the_caller_transaction(
    tmp_path: Path,
) -> None:
    """Keep transaction ownership with the caller-owned session."""
    engine = sqlite_engine(tmp_path / "workflow.db")
    with Session(engine) as session:
        project = Product(name="Uncommitted", origin="greenfield")
        session.add(project)
        session.flush()
        project_id = _id(project.product_id)

        snapshot = WorkflowFactRepository(session).load(project_id)
        assert snapshot.project.name == "Uncommitted"
        session.rollback()

    with Session(engine) as session:
        assert session.get(Product, project_id) is None


def test_load_never_owns_or_flushes_the_caller_session(tmp_path: Path) -> None:
    """Keep pending caller state untouched and emit no database mutation."""
    engine = sqlite_engine(tmp_path / "workflow.db")
    project_id = seed_complete_project(engine)
    with Session(engine) as setup_session:
        unrelated = Product(name="Persisted unrelated", origin="greenfield")
        setup_session.add(unrelated)
        setup_session.commit()
        unrelated_id = _id(unrelated.product_id)

    dml_statements: list[str] = []
    flush_events: list[str] = []

    def capture_dml(*args: object) -> None:
        statement = args[2]
        if isinstance(statement, str) and statement.lstrip().upper().startswith(
            ("INSERT", "UPDATE", "DELETE")
        ):
            dml_statements.append(statement)

    def capture_flush(*_args: object) -> None:
        flush_events.append("flush")

    with Session(engine) as session:
        project = session.get(Product, project_id)
        unrelated = session.get(Product, unrelated_id)
        assert project is not None
        assert unrelated is not None
        project.name = "Pending rename"
        session.delete(unrelated)
        pending = Product(name="Pending insert", origin="greenfield")
        session.add(pending)
        event.listen(engine, "before_cursor_execute", capture_dml)
        event.listen(session, "before_flush", capture_flush)
        try:
            with (
                patch.object(session, "commit") as commit,
                patch.object(session, "rollback") as rollback,
                patch.object(session, "close") as close,
                patch.object(workflow_repository_module, "Session") as constructor,
            ):
                snapshot = WorkflowFactRepository(session).load(project_id)
                commit.assert_not_called()
                rollback.assert_not_called()
                close.assert_not_called()
                constructor.assert_not_called()
        finally:
            event.remove(session, "before_flush", capture_flush)
            event.remove(engine, "before_cursor_execute", capture_dml)

        assert flush_events == []
        assert dml_statements == []
        assert snapshot.project.name == "Repository Test"
        assert project.name == "Pending rename"
        assert pending in session.new
        assert project in session.dirty
        assert unrelated in session.deleted


def test_load_rejects_malformed_canonical_artifact_json(tmp_path: Path) -> None:
    """Reject malformed canonical content instead of treating it as graph truth."""
    engine = sqlite_engine(tmp_path / "workflow.db")
    project_id = seed_complete_project(engine)
    with Session(engine) as session:
        artifact = session.exec(
            select(ChallengeArtifact).order_by(
                col(ChallengeArtifact.challenge_artifact_id)
            )
        ).first()
        assert artifact is not None
        artifact.canonical_content_json = "not-json"
        session.add(artifact)
        session.commit()

    with (
        Session(engine) as session,
        pytest.raises(
            WorkflowFactLoadError,
            match="canonical",
        ),
    ):
        WorkflowFactRepository(session).load(project_id)


@pytest.mark.parametrize(
    ("compiled_artifact_json", "remove_acceptance"),
    [
        pytest.param(None, False, id="accepted-null"),
        pytest.param("not-json", False, id="accepted-malformed"),
        pytest.param(None, True, id="pending-null"),
    ],
)
def test_load_rejects_authority_without_valid_canonical_artifact(
    tmp_path: Path,
    compiled_artifact_json: str | None,
    *,
    remove_acceptance: bool,
) -> None:
    """Require valid canonical compiled content for every authority fact."""
    engine = sqlite_engine(tmp_path / "workflow.db")
    project_id = seed_complete_project(engine)
    with Session(engine) as session:
        authority = session.exec(
            select(CompiledSpecAuthority).order_by(
                col(CompiledSpecAuthority.authority_id)
            )
        ).first()
        assert authority is not None
        authority.compiled_artifact_json = compiled_artifact_json
        session.add(authority)
        if remove_acceptance:
            acceptances = session.exec(
                select(SpecAuthorityAcceptance).where(
                    col(SpecAuthorityAcceptance.product_id) == project_id
                )
            ).all()
            for acceptance in acceptances:
                session.delete(acceptance)
        session.commit()

    with (
        Session(engine) as session,
        pytest.raises(
            WorkflowFactLoadError,
            match="canonical authority",
        ),
    ):
        WorkflowFactRepository(session).load(project_id)


def test_load_rejects_schema_valid_authority_compilation_failure(
    tmp_path: Path,
) -> None:
    """Accepted authority content must be a successful compilation payload."""
    engine = sqlite_engine(tmp_path / "workflow.db")
    project_id = seed_complete_project(engine)
    failure_json = SpecAuthorityCompilerOutput(
        root=SpecAuthorityCompilationFailure(
            error="COMPILATION_FAILED",
            reason="The source specification is incomplete.",
            blocking_gaps=["Missing workflow invariant"],
        )
    ).model_dump_json()
    with Session(engine) as session:
        _replace_first_authority_artifact(session, failure_json)

    with (
        Session(engine) as session,
        pytest.raises(WorkflowFactLoadError, match="canonical authority"),
    ):
        WorkflowFactRepository(session).load(project_id)


@pytest.mark.parametrize(
    ("metadata_field", "conflicting_value"),
    [
        pytest.param("compiler_version", "9.9.9", id="compiler-version"),
        pytest.param("prompt_hash", "b" * 64, id="prompt-hash"),
    ],
)
def test_load_rejects_authority_success_metadata_mismatch(
    tmp_path: Path,
    metadata_field: str,
    conflicting_value: str,
) -> None:
    """Canonical success provenance must match the authoritative row."""
    engine = sqlite_engine(tmp_path / "workflow.db")
    project_id = seed_complete_project(engine)
    artifact = SpecAuthorityCompilationSuccess.model_validate_json(_authority_json())
    if metadata_field == "compiler_version":
        artifact = artifact.model_copy(update={"compiler_version": conflicting_value})
    else:
        artifact = artifact.model_copy(update={"prompt_hash": conflicting_value})
    artifact_json = SpecAuthorityCompilationSuccess.model_validate_json(
        artifact.model_dump_json()
    ).model_dump_json()
    with Session(engine) as session:
        _replace_first_authority_artifact(session, artifact_json)

    with (
        Session(engine) as session,
        pytest.raises(WorkflowFactLoadError, match=metadata_field),
    ):
        WorkflowFactRepository(session).load(project_id)


@pytest.mark.parametrize(
    "membership",
    ["missing", "foreign"],
)
def test_load_rejects_invalid_task_sprint_relationship(
    tmp_path: Path,
    membership: str,
) -> None:
    """Every target Project task must map through a target Project sprint."""
    engine = sqlite_engine(tmp_path / "workflow.db")
    target_id = seed_complete_project(engine, name="Target")
    foreign_id = seed_complete_project(engine, name="Foreign")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        if membership == "missing":
            connection.execute(
                text(
                    "DELETE FROM sprint_stories WHERE story_id IN "
                    "(SELECT story_id FROM user_stories "
                    "WHERE product_id = :target_id)"
                ),
                {"target_id": target_id},
            )
        else:
            connection.execute(
                text(
                    "UPDATE sprint_stories SET sprint_id = "
                    "(SELECT sprint_id FROM sprints "
                    "WHERE product_id = :foreign_id ORDER BY sprint_id LIMIT 1) "
                    "WHERE story_id IN (SELECT story_id FROM user_stories "
                    "WHERE product_id = :target_id)"
                ),
                {"target_id": target_id, "foreign_id": foreign_id},
            )
        connection.commit()

    with (
        Session(engine) as session,
        pytest.raises(WorkflowFactLoadError, match="task sprint relationship"),
    ):
        WorkflowFactRepository(session).load(target_id)


@pytest.mark.parametrize(
    "corruption",
    _FORCED_CORRUPTIONS,
    ids=tuple(item.name for item in _FORCED_CORRUPTIONS),
)
def test_load_rejects_every_forced_relationship_corruption(
    tmp_path: Path,
    corruption: _ForcedCorruption,
) -> None:
    """Fail closed for every mapped parent, supersession, and fingerprint link."""
    engine = sqlite_engine(tmp_path / "workflow.db")
    target_id = seed_complete_project(engine, name="Target")
    foreign_id = seed_complete_project(engine, name="Foreign")
    _force_corruption(
        engine,
        corruption,
        target_id=target_id,
        foreign_id=foreign_id,
    )

    with (
        Session(engine) as session,
        pytest.raises(WorkflowFactLoadError, match="corruption"),
    ):
        WorkflowFactRepository(session).load(target_id)
