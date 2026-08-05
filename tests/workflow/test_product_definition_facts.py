"""Workflow fact loading tests for durable product-definition records."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from google.adk.sessions import DatabaseSessionService
from sqlmodel import Session

from models.core import Project
from models.product_definition import (
    DiscoveryArtifact,
    ProductGoalArtifact,
    ProductGoalArtifactDecision,
    ProductGoalInterviewTurn,
    ProductGoalOutcome,
    SpecificationCandidate,
    SpecificationDecision,
    VisionInterviewTurn,
    VisionRevisionIntent,
)
from models.specs import CompiledSpecAuthority, SpecRegistry
from models.workflow import VisionArtifact, VisionArtifactDecision, WorkflowNodeAttempt
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from services.specs.authority_selection import pending_authority_fingerprint
from utils.runtime_config import (
    ADK_EXECUTION_TRACE_IDENTITY,
    clear_runtime_config_cache,
    get_adk_execution_trace_db_target,
)
from utils.spec_schemas import (
    Invariant,
    InvariantType,
    RequiredFieldParams,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerOutput,
)
from workflow.facts import WorkflowFactSnapshot
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from sqlalchemy.engine import Engine


def _id(value: int | None) -> int:
    """Narrow a flushed SQLModel identity for test fixtures."""
    assert value is not None
    return value


def _force_sql(
    session: Session,
    statement: str,
    params: dict[str, int | str] | None = None,
) -> None:
    """Execute test-only raw SQL that deliberately bypasses model safeguards."""
    session.connection().exec_driver_sql(statement, params)


def _vision_output_fingerprint(
    components: dict[str, str],
    vision_statement: str,
    is_complete: bool,
    clarifying_questions: list[str],
) -> str:
    """Hash only the canonical persisted Vision model output."""
    return canonical_hash(
        {
            "components_json": components,
            "vision_statement": vision_statement,
            "is_complete": is_complete,
            "clarifying_questions_json": clarifying_questions,
        }
    )


def _product_goal_output_fingerprint(
    components: Mapping[str, str | int],
    goal_statement: str,
    is_complete: bool,
    clarifying_questions: list[str],
) -> str:
    """Hash only the canonical persisted Product Goal model output."""
    return canonical_hash(
        {
            "components_json": dict(components),
            "goal_statement": goal_statement,
            "is_complete": is_complete,
            "clarifying_questions_json": clarifying_questions,
        }
    )


def _authority_json() -> str:
    """Build valid persisted compiled-authority content for the Vision parent."""
    return SpecAuthorityCompilerOutput(
        root=SpecAuthorityCompilationSuccess(
            scope_themes=["Durable product definitions"],
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


def _attempt(
    session: Session,
    project_id: int,
    recorded_at: datetime,
    *,
    node_id: str,
    key: str,
) -> int:
    """Persist one durable workflow attempt for interview provenance."""
    attempt = WorkflowNodeAttempt(
        project_id=project_id,
        node_id=node_id,
        instance_key=None,
        graph_version="agileforge.workflow.v1",
        fact_fingerprint="sha256:facts",
        business_fact_fingerprint="sha256:business",
        decision_fingerprint="sha256:decision",
        normalized_input_json="{}",
        input_fingerprint="sha256:input",
        model_id="test-model",
        execution_settings_json="{}",
        idempotency_key=f"{key}-attempt-{project_id}",
        actor="test",
        correlation_id=None,
        started_at=recorded_at,
        lease_expires_at=recorded_at + timedelta(minutes=5),
        attempt_fingerprint=f"sha256:attempt:{key}:{project_id}",
    )
    session.add(attempt)
    session.flush()
    return _id(attempt.workflow_node_attempt_id)


def _vision_artifact(
    session: Session,
    project_id: int,
    recorded_at: datetime,
    *,
    version_number: int = 1,
) -> tuple[int, str, int]:
    """Persist one staged Vision artifact backed by a valid authority row."""
    legacy_spec = SpecRegistry(
        project_id=project_id,
        spec_hash=f"sha256:legacy-spec:{project_id}",
        content="# Legacy specification",
        status="approved",
    )
    session.add(legacy_spec)
    session.flush()
    authority = CompiledSpecAuthority(
        spec_version_id=_id(legacy_spec.spec_version_id),
        compiler_version="3.0.0",
        prompt_hash="a" * 64,
        compiled_at=recorded_at,
        scope_themes="[]",
        invariants="[]",
        eligible_feature_ids="[]",
        rejected_features="[]",
        spec_gaps="[]",
        compiled_artifact_json=_authority_json(),
    )
    session.add(authority)
    session.flush()
    authority_fingerprint = pending_authority_fingerprint(authority)
    assert authority_fingerprint is not None
    content = {"vision": f"Vision {project_id} version {version_number}"}
    artifact = VisionArtifact(
        project_id=project_id,
        authority_id=_id(authority.authority_id),
        authority_fingerprint=authority_fingerprint,
        version_number=version_number,
        canonical_content_json=canonical_json(content),
        content_fingerprint=canonical_hash(content),
        supersedes_vision_artifact_id=None,
        created_by="test",
        created_at=recorded_at,
    )
    session.add(artifact)
    session.flush()
    session.add(
        VisionArtifactDecision(
            project_id=project_id,
            vision_artifact_id=_id(artifact.vision_artifact_id),
            artifact_fingerprint=artifact.content_fingerprint,
            decision="accepted",
            rationale="Required durable Vision parent.",
            reviewer="operator",
            idempotency_key=f"vision-review-{project_id}-{version_number}",
            decided_at=recorded_at,
        )
    )
    return (
        _id(artifact.vision_artifact_id),
        artifact.content_fingerprint,
        _id(legacy_spec.spec_version_id),
    )


def _add_initial_vision_turn(
    session: Session,
    project_id: int,
    recorded_at: datetime,
) -> int:
    """Persist the first immutable Vision interview in its initial chain."""
    attempt_id = _attempt(
        session,
        project_id,
        recorded_at,
        node_id="vision.interview",
        key="vision-initial",
    )
    components = {"constraint": "initial"}
    clarifying_questions: list[str] = []
    vision_statement = "An initial deterministic workflow."
    turn = VisionInterviewTurn(
        project_id=project_id,
        mode="initial",
        turn_number=1,
        revision_intent_id=None,
        prior_turn_id=None,
        user_text="Define the initial Vision.",
        components_json=canonical_json(components),
        vision_statement=vision_statement,
        is_complete=True,
        clarifying_questions_json=canonical_json(clarifying_questions),
        output_fingerprint=_vision_output_fingerprint(
            components,
            vision_statement,
            True,
            clarifying_questions,
        ),
        workflow_node_attempt_id=attempt_id,
        attempt_fingerprint=f"sha256:attempt:vision-initial:{project_id}",
        recorded_at=recorded_at,
    )
    session.add(turn)
    session.flush()
    return _id(turn.vision_interview_turn_id)


def _seed_product_definition(
    session: Session,
    name: str,
    *,
    create_goal_outcome: bool = True,
) -> dict[str, int | str]:
    """Seed one complete, loader-valid product-definition lineage."""
    recorded_at = datetime(2026, 8, 5, 12, tzinfo=UTC)
    project = Project(name=name)
    session.add(project)
    session.flush()
    project_id = _id(project.project_id)
    vision_id, vision_fingerprint, legacy_spec_id = _vision_artifact(
        session,
        project_id,
        recorded_at,
    )
    initial_turn_id = _add_initial_vision_turn(session, project_id, recorded_at)
    attempt_id = _attempt(
        session,
        project_id,
        recorded_at,
        node_id="vision.interview",
        key="vision-revision",
    )
    revision = VisionRevisionIntent(
        project_id=project_id,
        source_vision_artifact_id=vision_id,
        source_vision_fingerprint=vision_fingerprint,
        reason="Clarify delivery scope",
        initiated_by="operator",
        initiated_at=recorded_at,
    )
    session.add(revision)
    session.flush()
    vision_components = {"constraint": "deterministic"}
    vision_questions = ["Which durable records are required?"]
    vision_statement = "A deterministic workflow."
    turn = VisionInterviewTurn(
        project_id=project_id,
        mode="revision",
        turn_number=1,
        revision_intent_id=_id(revision.vision_revision_intent_id),
        prior_turn_id=None,
        user_text="Keep the workflow deterministic.",
        components_json=canonical_json(vision_components),
        vision_statement=vision_statement,
        is_complete=False,
        clarifying_questions_json=canonical_json(vision_questions),
        output_fingerprint=_vision_output_fingerprint(
            vision_components,
            vision_statement,
            False,
            vision_questions,
        ),
        workflow_node_attempt_id=attempt_id,
        attempt_fingerprint=f"sha256:attempt:vision-revision:{project_id}",
        recorded_at=recorded_at,
    )
    session.add(turn)
    session.flush()
    goal_attempt_id = _attempt(
        session,
        project_id,
        recorded_at,
        node_id="product_goal.interview",
        key="goal",
    )
    statement = "Deliver durable product definitions."
    goal_components = {"constraint": "durable"}
    goal_questions: list[str] = []
    goal_turn = ProductGoalInterviewTurn(
        project_id=project_id,
        vision_artifact_id=vision_id,
        vision_fingerprint=vision_fingerprint,
        goal_number=1,
        revision_number=1,
        prior_turn_id=None,
        user_text="Define the first durable product goal.",
        components_json=canonical_json(goal_components),
        goal_statement=statement,
        is_complete=True,
        clarifying_questions_json=canonical_json(goal_questions),
        output_fingerprint=_product_goal_output_fingerprint(
            goal_components,
            statement,
            True,
            goal_questions,
        ),
        workflow_node_attempt_id=goal_attempt_id,
        attempt_fingerprint=f"sha256:attempt:goal:{project_id}",
        recorded_at=recorded_at,
    )
    session.add(goal_turn)
    session.flush()
    goal = ProductGoalArtifact(
        project_id=project_id,
        vision_artifact_id=vision_id,
        vision_fingerprint=vision_fingerprint,
        goal_number=1,
        revision_number=1,
        statement=statement,
        content_fingerprint=canonical_hash({"statement": statement}),
        supersedes_product_goal_artifact_id=None,
        source_interview_turn_id=_id(goal_turn.product_goal_interview_turn_id),
        created_by="operator",
        created_at=recorded_at,
    )
    session.add(goal)
    session.flush()
    goal_id = _id(goal.product_goal_artifact_id)
    session.add(
        ProductGoalArtifactDecision(
            project_id=project_id,
            product_goal_artifact_id=goal_id,
            artifact_fingerprint=goal.content_fingerprint,
            decision="accepted",
            rationale="Required for the next durable record.",
            reviewer="operator",
            idempotency_key=f"goal-review-{project_id}",
            decided_at=recorded_at,
        )
    )
    outcome: ProductGoalOutcome | None = None
    if create_goal_outcome:
        outcome = ProductGoalOutcome(
            project_id=project_id,
            product_goal_artifact_id=goal_id,
            artifact_fingerprint=goal.content_fingerprint,
            outcome="fulfilled",
            rationale="The durable records are available.",
            decided_by="operator",
            idempotency_key=f"goal-outcome-{project_id}",
            decided_at=recorded_at,
        )
        session.add(outcome)
    discovery_content = {"discovery": "complete"}
    discovery = DiscoveryArtifact(
        project_id=project_id,
        vision_artifact_id=vision_id,
        vision_fingerprint=vision_fingerprint,
        product_goal_artifact_id=goal_id,
        product_goal_fingerprint=goal.content_fingerprint,
        canonical_content_json=canonical_json(discovery_content),
        content_fingerprint=canonical_hash(discovery_content),
        content_ref="discovery.md",
        producer="test",
        supersedes_discovery_artifact_id=None,
        recorded_by="operator",
        recorded_at=recorded_at,
    )
    session.add(discovery)
    session.flush()
    candidate_content = {"specification": "candidate"}
    candidate = SpecificationCandidate(
        project_id=project_id,
        vision_artifact_id=vision_id,
        vision_fingerprint=vision_fingerprint,
        product_goal_artifact_id=goal_id,
        product_goal_fingerprint=goal.content_fingerprint,
        discovery_artifact_id=_id(discovery.discovery_artifact_id),
        discovery_fingerprint=discovery.content_fingerprint,
        base_spec_version_id=None,
        base_spec_hash=None,
        canonical_content_json=canonical_json(candidate_content),
        content_fingerprint=canonical_hash(candidate_content),
        content_ref="spec.json",
        supersedes_specification_candidate_id=None,
        recorded_by="operator",
        recorded_at=recorded_at,
    )
    session.add(candidate)
    session.flush()
    candidate_id = _id(candidate.specification_candidate_id)
    session.add(
        SpecificationDecision(
            project_id=project_id,
            specification_candidate_id=candidate_id,
            artifact_fingerprint=candidate.content_fingerprint,
            decision="accepted",
            rationale="Ready for registration.",
            reviewer="operator",
            idempotency_key=f"specification-review-{project_id}",
            decided_at=recorded_at,
        )
    )
    registered_spec = SpecRegistry(
        project_id=project_id,
        spec_hash=f"sha256:registered-spec:{project_id}",
        content="# Registered specification",
        status="approved",
        source_specification_candidate_id=candidate_id,
        source_vision_artifact_id=vision_id,
        source_vision_fingerprint=vision_fingerprint,
        source_product_goal_artifact_id=goal_id,
        source_product_goal_fingerprint=goal.content_fingerprint,
        source_discovery_artifact_id=_id(discovery.discovery_artifact_id),
        source_discovery_fingerprint=discovery.content_fingerprint,
        supersedes_spec_version_id=None,
    )
    session.add(registered_spec)
    session.commit()
    return {
        "project_id": project_id,
        "vision_id": vision_id,
        "vision_fingerprint": vision_fingerprint,
        "initial_turn_id": initial_turn_id,
        "revision_id": _id(revision.vision_revision_intent_id),
        "turn_id": _id(turn.vision_interview_turn_id),
        "goal_turn_id": _id(goal_turn.product_goal_interview_turn_id),
        "goal_id": goal_id,
        "goal_fingerprint": goal.content_fingerprint,
        "outcome_id": 0 if outcome is None else _id(outcome.product_goal_outcome_id),
        "discovery_id": _id(discovery.discovery_artifact_id),
        "discovery_fingerprint": discovery.content_fingerprint,
        "candidate_id": candidate_id,
        "candidate_fingerprint": candidate.content_fingerprint,
        "legacy_spec_id": legacy_spec_id,
        "registered_spec_id": _id(registered_spec.spec_version_id),
    }


def _add_accepted_product_goal(
    session: Session,
    *,
    project_id: int,
    vision: tuple[int, str],
    goal_number: int,
    recorded_at: datetime,
) -> dict[str, int | str]:
    """Append one accepted, pending Product Goal under an accepted Vision."""
    vision_id, vision_fingerprint = vision
    statement = f"Durable Product Goal {goal_number}."
    components = {"goal_number": goal_number}
    questions: list[str] = []
    attempt_id = _attempt(
        session,
        project_id,
        recorded_at,
        node_id="product_goal.interview",
        key=f"goal-{goal_number}",
    )
    turn = ProductGoalInterviewTurn(
        project_id=project_id,
        vision_artifact_id=vision_id,
        vision_fingerprint=vision_fingerprint,
        goal_number=goal_number,
        revision_number=1,
        prior_turn_id=None,
        user_text=statement,
        components_json=canonical_json(components),
        goal_statement=statement,
        is_complete=True,
        clarifying_questions_json=canonical_json(questions),
        output_fingerprint=_product_goal_output_fingerprint(
            components,
            statement,
            True,
            questions,
        ),
        workflow_node_attempt_id=attempt_id,
        attempt_fingerprint=f"sha256:attempt:goal-{goal_number}:{project_id}",
        recorded_at=recorded_at,
    )
    session.add(turn)
    session.flush()
    goal = ProductGoalArtifact(
        project_id=project_id,
        vision_artifact_id=vision_id,
        vision_fingerprint=vision_fingerprint,
        goal_number=goal_number,
        revision_number=1,
        statement=statement,
        content_fingerprint=canonical_hash({"statement": statement}),
        supersedes_product_goal_artifact_id=None,
        source_interview_turn_id=_id(turn.product_goal_interview_turn_id),
        created_by="operator",
        created_at=recorded_at,
    )
    session.add(goal)
    session.flush()
    session.add(
        ProductGoalArtifactDecision(
            project_id=project_id,
            product_goal_artifact_id=_id(goal.product_goal_artifact_id),
            artifact_fingerprint=goal.content_fingerprint,
            decision="accepted",
            rationale="Track the remaining work.",
            reviewer="operator",
            idempotency_key=f"goal-review-{goal_number}-{project_id}",
            decided_at=recorded_at,
        )
    )
    return {
        "goal_turn_id": _id(turn.product_goal_interview_turn_id),
        "goal_id": _id(goal.product_goal_artifact_id),
        "goal_fingerprint": goal.content_fingerprint,
    }


def test_loader_retains_product_definition_identity_and_legacy_spec_lineage(
    engine: Engine,
) -> None:
    """Load product records while legacy specs retain nullable staged lineage."""
    with Session(engine) as session:
        seed = _seed_product_definition(session, "Product facts")
        snapshot = WorkflowFactRepository(session).load(int(seed["project_id"]))

    assert "repository_bindings" not in WorkflowFactSnapshot.model_fields
    assert snapshot.spec_versions[0].spec_version_id == seed["legacy_spec_id"]
    assert snapshot.spec_versions[0].source_specification_candidate_id is None
    assert {
        turn.vision_interview_turn_id for turn in snapshot.vision_interview_turns
    } == {seed["initial_turn_id"], seed["turn_id"]}
    assert (
        snapshot.product_goal_interview_turns[0].product_goal_interview_turn_id
        == seed["goal_turn_id"]
    )
    assert snapshot.product_goal_artifacts[0].vision_artifact_id == seed["vision_id"]
    assert (
        snapshot.product_goal_artifacts[0].vision_fingerprint
        == seed["vision_fingerprint"]
    )
    assert (
        snapshot.product_goal_artifacts[0].source_interview_turn_id
        == seed["goal_turn_id"]
    )
    assert (
        snapshot.product_goal_outcomes[0].product_goal_outcome_id == seed["outcome_id"]
    )
    assert snapshot.product_goal_outcomes[0].product_goal_artifact_id == seed["goal_id"]
    assert snapshot.discovery_artifacts[0].product_goal_artifact_id == seed["goal_id"]
    assert (
        snapshot.discovery_artifacts[0].product_goal_fingerprint
        == seed["goal_fingerprint"]
    )
    assert (
        snapshot.specification_candidates[0].discovery_artifact_id
        == seed["discovery_id"]
    )
    assert (
        snapshot.specification_candidates[0].discovery_fingerprint
        == seed["discovery_fingerprint"]
    )
    assert (
        snapshot.spec_versions[1].source_specification_candidate_id
        == seed["candidate_id"]
    )
    assert (
        snapshot.spec_versions[1].source_specification_candidate_fingerprint
        == seed["candidate_fingerprint"]
    )


def test_loader_loads_initial_and_revision_vision_chains_with_turn_one(
    engine: Engine,
) -> None:
    """Keep initial and revision chains independently numbered per Project."""
    with Session(engine) as session:
        seed = _seed_product_definition(session, "Initial then revision Vision")
        snapshot = WorkflowFactRepository(session).load(int(seed["project_id"]))

    assert {
        (turn.mode, turn.revision_intent_id, turn.turn_number)
        for turn in snapshot.vision_interview_turns
    } == {
        ("initial", None, 1),
        ("revision", seed["revision_id"], 1),
    }


async def _persist_trace_session(service: DatabaseSessionService) -> None:
    """Persist one ADK session in the configured trace store."""
    session = await service.create_session(
        app_name=ADK_EXECUTION_TRACE_IDENTITY.app_name,
        user_id=ADK_EXECUTION_TRACE_IDENTITY.user_id,
        session_id="product-definition-trace",
        state={"product_goal_interview_turn_id": 1},
    )
    assert session.id == "product-definition-trace"


def test_loader_keeps_interview_turn_after_configured_adk_trace_database_is_deleted(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delete actual configured ADK trace state without losing durable facts."""
    trace_database = tmp_path / "adk-execution-trace.sqlite3"
    monkeypatch.setenv(
        "AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL",
        f"sqlite:///{trace_database.as_posix()}",
    )
    clear_runtime_config_cache()
    try:
        with Session(engine) as session:
            seed = _seed_product_definition(session, "No trace dependency")
            target = get_adk_execution_trace_db_target()
            assert target.sqlite_path == trace_database
            service = DatabaseSessionService(db_url=target.async_sqlite_url)
            asyncio.run(_persist_trace_session(service))
            assert trace_database.exists()
            trace_database.unlink()
            snapshot = WorkflowFactRepository(session).load(int(seed["project_id"]))
    finally:
        clear_runtime_config_cache()

    assert not trace_database.exists()
    assert [
        turn.vision_interview_turn_id for turn in snapshot.vision_interview_turns
    ] == [seed["initial_turn_id"], seed["turn_id"]]


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM vision_artifact_decisions WHERE project_id = :project_id",
        "UPDATE vision_artifact_decisions SET decision = 'rejected' "
        "WHERE project_id = :project_id",
    ],
)
def test_loader_rejects_product_goal_lineage_without_accepted_vision(
    engine: Engine,
    statement: str,
) -> None:
    """Goal interviews and artifacts require their exact accepted Vision."""
    with Session(engine) as session:
        seed = _seed_product_definition(
            session,
            "Unaccepted Vision parent",
            create_goal_outcome=False,
        )
        _force_sql(session, statement, {"project_id": int(seed["project_id"])})
        session.commit()

        with pytest.raises(WorkflowFactLoadError):
            WorkflowFactRepository(session).load(int(seed["project_id"]))


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM product_goal_artifact_decisions WHERE project_id = :project_id",
        "UPDATE product_goal_artifact_decisions SET decision = 'rejected' "
        "WHERE project_id = :project_id",
    ],
)
def test_loader_rejects_discovery_without_accepted_active_product_goal(
    engine: Engine,
    statement: str,
) -> None:
    """Discovery requires its exact accepted, non-superseded Product Goal."""
    with Session(engine) as session:
        seed = _seed_product_definition(
            session,
            "Unaccepted Product Goal parent",
            create_goal_outcome=False,
        )
        _force_sql(session, statement, {"project_id": int(seed["project_id"])})
        session.commit()

        with pytest.raises(WorkflowFactLoadError):
            WorkflowFactRepository(session).load(int(seed["project_id"]))


def test_loader_rejects_discovery_of_a_superseded_product_goal(
    engine: Engine,
) -> None:
    """Discovery cannot continue from an accepted Goal replaced by a newer one."""
    recorded_at = datetime(2026, 8, 5, 13, tzinfo=UTC)
    with Session(engine) as session:
        seed = _seed_product_definition(session, "Superseded Product Goal parent")
        project_id = int(seed["project_id"])
        replacement = _add_accepted_product_goal(
            session,
            project_id=project_id,
            vision=(int(seed["vision_id"]), str(seed["vision_fingerprint"])),
            goal_number=2,
            recorded_at=recorded_at,
        )
        _force_sql(
            session,
            "UPDATE product_goal_artifacts "
            "SET supersedes_product_goal_artifact_id = :superseded_goal_id "
            "WHERE product_goal_artifact_id = :replacement_goal_id",
            {
                "superseded_goal_id": int(seed["goal_id"]),
                "replacement_goal_id": int(replacement["goal_id"]),
            },
        )
        session.commit()

        with pytest.raises(WorkflowFactLoadError):
            WorkflowFactRepository(session).load(project_id)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE product_goal_artifacts SET "
        "vision_artifact_id = :other_vision_id, "
        "vision_fingerprint = :other_vision_fingerprint "
        "WHERE product_goal_artifact_id = :goal_id",
        "UPDATE discovery_artifacts SET "
        "vision_artifact_id = :other_vision_id, "
        "vision_fingerprint = :other_vision_fingerprint "
        "WHERE discovery_artifact_id = :discovery_id",
        "UPDATE specification_candidates SET "
        "vision_artifact_id = :other_vision_id, "
        "vision_fingerprint = :other_vision_fingerprint "
        "WHERE specification_candidate_id = :candidate_id",
        "UPDATE specification_candidates SET "
        "product_goal_artifact_id = :other_goal_id, "
        "product_goal_fingerprint = :other_goal_fingerprint "
        "WHERE specification_candidate_id = :candidate_id",
    ],
)
def test_loader_rejects_same_project_product_definition_chain_swaps(
    engine: Engine,
    statement: str,
) -> None:
    """Reject matching parent IDs/fingerprints from a different valid chain."""
    recorded_at = datetime(2026, 8, 5, 13, tzinfo=UTC)
    with Session(engine) as session:
        seed = _seed_product_definition(session, "Product chain swap")
        project_id = int(seed["project_id"])
        other_vision_id, other_vision_fingerprint, _legacy_spec_id = _vision_artifact(
            session,
            project_id,
            recorded_at,
            version_number=2,
        )
        other_goal = _add_accepted_product_goal(
            session,
            project_id=project_id,
            vision=(other_vision_id, other_vision_fingerprint),
            goal_number=2,
            recorded_at=recorded_at,
        )
        _force_sql(session, "PRAGMA foreign_keys = OFF")
        _force_sql(
            session,
            statement,
            {
                "goal_id": int(seed["goal_id"]),
                "discovery_id": int(seed["discovery_id"]),
                "candidate_id": int(seed["candidate_id"]),
                "other_vision_id": other_vision_id,
                "other_vision_fingerprint": other_vision_fingerprint,
                "other_goal_id": int(other_goal["goal_id"]),
                "other_goal_fingerprint": str(other_goal["goal_fingerprint"]),
            },
        )
        session.commit()
        _force_sql(session, "PRAGMA foreign_keys = ON")

        with pytest.raises(WorkflowFactLoadError):
            WorkflowFactRepository(session).load(project_id)


@pytest.mark.parametrize(
    ("statement", "params"),
    [
        (
            "UPDATE vision_interview_turns SET output_fingerprint = :value "
            "WHERE project_id = :project_id",
            {"value": "sha256:tampered"},
        ),
        (
            "UPDATE product_goal_interview_turns SET output_fingerprint = :value "
            "WHERE project_id = :project_id",
            {"value": "sha256:tampered"},
        ),
        (
            "UPDATE product_goal_artifacts SET statement = :statement, "
            "content_fingerprint = :fingerprint WHERE project_id = :project_id",
            {
                "statement": "A different Goal statement.",
                "fingerprint": canonical_hash(
                    {"statement": "A different Goal statement."}
                ),
            },
        ),
        (
            "UPDATE vision_interview_turns SET revision_intent_id = :revision_id "
            "WHERE vision_interview_turn_id = :initial_turn_id",
            {},
        ),
        (
            "UPDATE vision_interview_turns SET revision_intent_id = NULL "
            "WHERE vision_interview_turn_id = :turn_id",
            {},
        ),
    ],
)
def test_loader_rejects_interview_and_goal_artifact_tampering(
    engine: Engine,
    statement: str,
    params: dict[str, str],
) -> None:
    """Reject output, mode/intent, and source-statement corruption directly."""
    with Session(engine) as session:
        seed = _seed_product_definition(session, f"Interview tamper {statement}")
        _force_sql(session, "PRAGMA foreign_keys = OFF")
        _force_sql(
            session,
            statement,
            {
                **params,
                "project_id": seed["project_id"],
                "initial_turn_id": seed["initial_turn_id"],
                "revision_id": seed["revision_id"],
                "turn_id": seed["turn_id"],
            },
        )
        session.commit()
        _force_sql(session, "PRAGMA foreign_keys = ON")

        with pytest.raises(WorkflowFactLoadError):
            WorkflowFactRepository(session).load(int(seed["project_id"]))


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE vision_interview_turns SET prior_turn_id = NULL "
        "WHERE vision_interview_turn_id = :vision_followup_id",
        "UPDATE vision_interview_turns SET turn_number = 3 "
        "WHERE vision_interview_turn_id = :vision_followup_id",
        "UPDATE vision_interview_turns SET revision_intent_id = :other_revision_id "
        "WHERE vision_interview_turn_id = :vision_followup_id",
        "UPDATE product_goal_interview_turns SET prior_turn_id = NULL "
        "WHERE product_goal_interview_turn_id = :goal_followup_id",
        "UPDATE product_goal_interview_turns SET goal_number = 2 "
        "WHERE product_goal_interview_turn_id = :goal_followup_id",
    ],
)
def test_loader_rejects_nonsequential_or_inconsistent_interview_chains(
    engine: Engine,
    statement: str,
) -> None:
    """Require immediate prior turns in the exact Vision or Goal chain."""
    recorded_at = datetime(2026, 8, 5, 13, tzinfo=UTC)
    with Session(engine) as session:
        seed = _seed_product_definition(session, "Interview chain tamper")
        project_id = int(seed["project_id"])
        first_vision_turn = session.get(VisionInterviewTurn, int(seed["turn_id"]))
        first_goal_turn = session.get(
            ProductGoalInterviewTurn,
            int(seed["goal_turn_id"]),
        )
        assert first_vision_turn is not None
        assert first_goal_turn is not None
        other_revision = VisionRevisionIntent(
            project_id=project_id,
            source_vision_artifact_id=int(seed["vision_id"]),
            source_vision_fingerprint=str(seed["vision_fingerprint"]),
            reason="Separate revision chain",
            initiated_by="operator",
            initiated_at=recorded_at,
        )
        session.add(other_revision)
        session.flush()
        vision_components = {"constraint": "followup"}
        vision_questions: list[str] = []
        vision_statement = "A deterministic follow-up workflow."
        vision_followup = VisionInterviewTurn(
            project_id=project_id,
            mode="revision",
            turn_number=2,
            revision_intent_id=first_vision_turn.revision_intent_id,
            prior_turn_id=int(seed["turn_id"]),
            user_text="Refine the first Vision interview.",
            components_json=canonical_json(vision_components),
            vision_statement=vision_statement,
            is_complete=True,
            clarifying_questions_json=canonical_json(vision_questions),
            output_fingerprint=_vision_output_fingerprint(
                vision_components,
                vision_statement,
                True,
                vision_questions,
            ),
            workflow_node_attempt_id=first_vision_turn.workflow_node_attempt_id,
            attempt_fingerprint=first_vision_turn.attempt_fingerprint,
            recorded_at=recorded_at,
        )
        session.add(vision_followup)
        goal_components = {"constraint": "followup"}
        goal_questions: list[str] = []
        goal_statement = str(first_goal_turn.goal_statement)
        goal_followup = ProductGoalInterviewTurn(
            project_id=project_id,
            vision_artifact_id=first_goal_turn.vision_artifact_id,
            vision_fingerprint=first_goal_turn.vision_fingerprint,
            goal_number=first_goal_turn.goal_number,
            revision_number=first_goal_turn.revision_number,
            prior_turn_id=int(seed["goal_turn_id"]),
            user_text="Refine the first Product Goal interview.",
            components_json=canonical_json(goal_components),
            goal_statement=goal_statement,
            is_complete=True,
            clarifying_questions_json=canonical_json(goal_questions),
            output_fingerprint=_product_goal_output_fingerprint(
                goal_components,
                goal_statement,
                True,
                goal_questions,
            ),
            workflow_node_attempt_id=first_goal_turn.workflow_node_attempt_id,
            attempt_fingerprint=first_goal_turn.attempt_fingerprint,
            recorded_at=recorded_at,
        )
        session.add(goal_followup)
        session.flush()
        _force_sql(session, "PRAGMA foreign_keys = OFF")
        _force_sql(
            session,
            statement,
            {
                "vision_followup_id": _id(vision_followup.vision_interview_turn_id),
                "goal_followup_id": _id(goal_followup.product_goal_interview_turn_id),
                "other_revision_id": _id(other_revision.vision_revision_intent_id),
            },
        )
        session.commit()
        _force_sql(session, "PRAGMA foreign_keys = ON")

        with pytest.raises(WorkflowFactLoadError):
            WorkflowFactRepository(session).load(project_id)


def test_loader_rejects_two_accepted_product_goals_without_outcomes(
    engine: Engine,
) -> None:
    """A Project may have only one accepted Product Goal awaiting an outcome."""
    recorded_at = datetime(2026, 8, 5, 12, tzinfo=UTC)
    with Session(engine) as session:
        seed = _seed_product_definition(session, "Two pending Product Goals")
        project_id = int(seed["project_id"])
        for goal_number in (2, 3):
            _add_accepted_product_goal(
                session,
                project_id=project_id,
                vision=(
                    int(seed["vision_id"]),
                    str(seed["vision_fingerprint"]),
                ),
                goal_number=goal_number,
                recorded_at=recorded_at,
            )
        session.commit()

        with pytest.raises(WorkflowFactLoadError):
            WorkflowFactRepository(session).load(project_id)


@pytest.mark.parametrize(
    ("statement", "params"),
    [
        (
            "UPDATE discovery_artifacts SET canonical_content_json = :value "
            "WHERE project_id = :project_id",
            {"value": canonical_json({"discovery": "tampered"})},
        ),
        (
            "UPDATE specification_candidates SET discovery_fingerprint = :value "
            "WHERE project_id = :project_id",
            {"value": "sha256:tampered"},
        ),
    ],
)
def test_loader_rejects_product_definition_content_or_parent_tampering(
    engine: Engine,
    statement: str,
    params: dict[str, str],
) -> None:
    """Fail closed when canonical content or a parent fingerprint changes."""
    with Session(engine) as session:
        seed = _seed_product_definition(session, f"Tamper {params['value']}")
        _force_sql(session, "PRAGMA foreign_keys = OFF")
        _force_sql(session, statement, {**params, "project_id": seed["project_id"]})
        session.commit()
        _force_sql(session, "PRAGMA foreign_keys = ON")

        with pytest.raises(WorkflowFactLoadError):
            WorkflowFactRepository(session).load(int(seed["project_id"]))


@pytest.mark.parametrize(
    ("statement", "foreign_key"),
    [
        (
            "UPDATE product_goal_artifacts "
            "SET source_interview_turn_id = :foreign_id "
            "WHERE project_id = :target_project",
            "goal_turn_id",
        ),
        (
            "UPDATE discovery_artifacts "
            "SET product_goal_artifact_id = :foreign_id "
            "WHERE project_id = :target_project",
            "goal_id",
        ),
        (
            "UPDATE specification_candidates "
            "SET discovery_artifact_id = :foreign_id "
            "WHERE project_id = :target_project",
            "discovery_id",
        ),
    ],
)
def test_loader_rejects_cross_project_product_definition_references(
    engine: Engine,
    statement: str,
    foreign_key: str,
) -> None:
    """Fail closed for corrupt Goal, discovery, or specification parents."""
    with Session(engine) as session:
        target = _seed_product_definition(session, "Target product facts")
        foreign = _seed_product_definition(session, "Foreign product facts")
        _force_sql(session, "PRAGMA foreign_keys = OFF")
        _force_sql(
            session,
            statement,
            {
                "foreign_id": int(foreign[foreign_key]),
                "target_project": target["project_id"],
            },
        )
        session.commit()
        _force_sql(session, "PRAGMA foreign_keys = ON")

        with pytest.raises(WorkflowFactLoadError):
            WorkflowFactRepository(session).load(int(target["project_id"]))
