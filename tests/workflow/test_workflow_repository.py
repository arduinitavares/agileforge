"""Canonical workflow-fact repository tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, col, create_engine, select

from models.core import Product, Sprint, SprintStory, Task, Team, UserStory
from models.db import set_sqlite_pragma
from models.enums import SprintStatus, StoryStatus, TaskStatus
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry
from models.workflow import (
    ChallengeArtifact,
    DiscoveryRun,
    InitialScopeRegistration,
    PrdDecision,
    PrdVersion,
    SpecDraft,
    SpecDraftDecision,
    WorkflowNodeAttempt,
    WorkflowNodeAttemptOutcome,
    WorkflowTransitionReceipt,
)
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from utils.spec_schemas import (
    Invariant,
    InvariantType,
    RequiredFieldParams,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerOutput,
)
from workflow.facts import (
    AuthorityFact,
    ChallengeArtifactFact,
    DiscoveryRunFact,
    InitialScopeRegistrationFact,
    NodeAttemptFact,
    PrdVersionFact,
    ProjectFact,
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


def seed_complete_project(engine: Engine, *, name: str = "Repository Test") -> int:
    """Persist a complete canonical fact set for one Project."""
    recorded_at = datetime(2026, 8, 2, 12, tzinfo=UTC)
    with Session(engine) as session:
        project = Product(name=name, origin="brownfield", vision="legacy vision")
        team = Team(name=f"{name} team")
        session.add(project)
        session.add(team)
        session.flush()
        project_id = _id(project.product_id)

        initial_run = DiscoveryRun(
            project_id=project_id,
            purpose="initial",
            ordinal=1,
            created_at=recorded_at,
        )
        extension_run = DiscoveryRun(
            project_id=project_id,
            purpose="extension",
            ordinal=1,
            created_at=recorded_at + timedelta(seconds=1),
            closed_at=recorded_at + timedelta(minutes=1),
        )
        session.add(initial_run)
        session.add(extension_run)
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
                authority_fingerprint="sha256:authority",
            )
        )
        story = UserStory(
            product_id=project_id,
            title="Accepted backlog story",
            status=StoryStatus.ACCEPTED,
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


def test_load_maps_complete_canonical_snapshot_in_deterministic_order(
    tmp_path: Path,
) -> None:
    """Map named authoritative rows without leaking persistence records."""
    engine = sqlite_engine(tmp_path / "workflow.db")
    project_id = seed_complete_project(engine)

    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)

    assert isinstance(snapshot.project, ProjectFact)
    assert snapshot.project.project_id == project_id
    assert snapshot.project.origin == "brownfield"
    assert tuple(item.content_fingerprint for item in snapshot.challenge_artifacts) == (
        "sha256:challenge:first",
        "sha256:challenge:second",
    )
    assert all(
        isinstance(item, ChallengeArtifactFact) for item in snapshot.challenge_artifacts
    )
    assert all(isinstance(item, DiscoveryRunFact) for item in snapshot.discovery_runs)
    assert all(isinstance(item, PrdVersionFact) for item in snapshot.prd_versions)
    assert all(isinstance(item, SpecDraftFact) for item in snapshot.spec_drafts)
    assert all(
        isinstance(item, InitialScopeRegistrationFact)
        for item in snapshot.initial_registrations
    )
    assert all(isinstance(item, AuthorityFact) for item in snapshot.authorities)
    assert snapshot.authorities[0].status == "accepted"
    assert all(isinstance(item, SprintFact) for item in snapshot.sprints)
    assert all(isinstance(item, StoryFact) for item in snapshot.stories)
    assert all(isinstance(item, TaskFact) for item in snapshot.tasks)
    assert all(isinstance(item, NodeAttemptFact) for item in snapshot.node_attempts)
    assert snapshot.phase_artifacts == ()
    assert snapshot.post_sprint_triage == ()


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


def test_load_rejects_forced_cross_project_corruption(tmp_path: Path) -> None:
    """Reject rows linked to another Project even with SQLite FKs bypassed."""
    engine = sqlite_engine(tmp_path / "workflow.db")
    first_id = seed_complete_project(engine, name="First")
    second_id = seed_complete_project(engine, name="Second")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.execute(
            text(
                "UPDATE prd_versions SET project_id = :second_id "
                "WHERE project_id = :first_id"
            ),
            {"first_id": first_id, "second_id": second_id},
        )
        connection.commit()

    with (
        Session(engine) as session,
        pytest.raises(
            WorkflowFactLoadError,
            match="cross-project",
        ),
    ):
        WorkflowFactRepository(session).load(second_id)
