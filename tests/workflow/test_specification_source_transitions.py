"""Transactional tests for immutable Specification source registration."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlmodel import Session, col, select

from models.core import Project
from models.product_definition import (
    ProductGoalArtifact,
    ProductGoalArtifactDecision,
    ProductGoalInterviewTurn,
    ProductGoalOutcome,
    SpecificationCandidate,
    SpecificationDecision,
    SpecificationSource,
)
from models.repository import RepositoryBinding, repository_binding_fingerprint
from models.specs import SpecRegistry
from models.workflow import WorkflowTransitionReceipt
from services.contracts.specification_source import (
    SPECIFICATION_SOURCE_PRIMARY_ID,
    SpecificationContextCapture,
    SpecificationRepositoryRevision,
    SpecificationSourceBundle,
    SpecificationSourceDocument,
    source_bundle_fingerprint,
)
from services.specification_source_registration import (
    PreparedSpecificationSourceRegistration,
    SpecificationSourceRegistrationError,
    SpecificationSourceRegistrationErrorCode,
)
from tests.workflow.lifecycle_fixtures import (
    _attempt,
    _complete_attempt,
    _required,
    _seed_accepted_vision_and_goal,
    seed_accepted_specification,
)
from workflow.clock import FixedClock
from workflow.contracts import GRAPH_VERSION, WorkflowErrorCode
from workflow.definitions.product_discovery import SPECIFICATION_NODES
from workflow.domain import WorkflowDomain
from workflow.fingerprints import canonical_json
from workflow.graph import ChildGraphSpec, WorkflowGraph
from workflow.requests import RegisterSpecificationSource

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.engine import Engine

NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)
EXPECTED_SOURCE_ROWS = 2


def _raw_fingerprint(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _seed_ready_project(
    engine: Engine,
) -> tuple[int, int, str, int, str, int, str]:
    with Session(engine) as session:
        project = Project(name="Registered source transition")
        session.add(project)
        session.flush()
        assert project.project_id is not None
        vision, goal = _seed_accepted_vision_and_goal(
            session,
            project_id=project.project_id,
            recorded_at=NOW.replace(hour=10),
        )
        binding = RepositoryBinding(
            project_id=project.project_id,
            worktree_path="/repository/specification-source",
            common_git_dir="/repository/specification-source/.git",
            head_sha="a" * 40,
            branch_name="main",
            detached_head=False,
            dirty=False,
            status_fingerprint="sha256:" + "b" * 64,
            status_entries_json="[]",
            remotes_json="[]",
            warnings_json="[]",
            probe_version="test",
            inspected_at=NOW,
            recorded_by="operator@example.test",
        )
        session.add(binding)
        session.flush()
        assert binding.repository_binding_id is not None
        binding_id = binding.repository_binding_id
        binding_fingerprint = repository_binding_fingerprint(binding)
        project.active_repository_binding_id = binding.repository_binding_id
        session.add(project)
        session.commit()
        assert vision.vision_artifact_id is not None
        assert goal.product_goal_artifact_id is not None
        return (
            project.project_id,
            vision.vision_artifact_id,
            vision.content_fingerprint,
            goal.product_goal_artifact_id,
            goal.content_fingerprint,
            binding_id,
            binding_fingerprint,
        )


def _bundle(  # noqa: PLR0913
    *,
    vision_fingerprint: str,
    goal_fingerprint: str,
    content: bytes = b"# Exact external specification\r\n",
    head_sha: str = "a" * 40,
    dirty: bool = False,
    status_fingerprint: str = "sha256:" + "b" * 64,
) -> SpecificationSourceBundle:
    return SpecificationSourceBundle(
        source=SpecificationSourceDocument(
            source_id=SPECIFICATION_SOURCE_PRIMARY_ID,
            relative_path="specification.md",
            content_base64=base64.b64encode(content).decode("ascii"),
            byte_length=len(content),
            content_fingerprint=_raw_fingerprint(content),
        ),
        context=SpecificationContextCapture(state="absent"),
        repository_revision=SpecificationRepositoryRevision(
            head_sha=head_sha,
            dirty=dirty,
            status_fingerprint=status_fingerprint,
        ),
        accepted_vision_fingerprint=vision_fingerprint,
        accepted_product_goal_fingerprint=goal_fingerprint,
    )


def _domain(
    engine: Engine,
    *,
    evaluated_at: datetime = NOW,
    registration_check: Callable[
        [PreparedSpecificationSourceRegistration],
        SpecificationSourceRegistrationError | None,
    ]
    | None = None,
) -> WorkflowDomain:
    return WorkflowDomain(
        engine=engine,
        graph=WorkflowGraph(
            graph_version=GRAPH_VERSION,
            root=ChildGraphSpec(
                child_graph_id="specification",
                nodes=SPECIFICATION_NODES,
            ),
        ),
        clock=FixedClock(now_value=evaluated_at),
        specification_registration_check=(
            (lambda _prepared: None)
            if registration_check is None
            else registration_check
        ),
    )


def _request(  # noqa: PLR0913
    domain: WorkflowDomain,
    *,
    project_id: int,
    vision_id: int,
    goal_id: int,
    binding_id: int,
    binding_fingerprint: str,
    bundle: SpecificationSourceBundle,
) -> RegisterSpecificationSource:
    position = domain.position(project_id)
    decision = next(
        item
        for item in position.decisions
        if item.node_id == "specification.source.register"
    )
    return RegisterSpecificationSource(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=decision.decision_fingerprint,
        idempotency_key="register-source-transition",
        actor="operator@example.test",
        correlation_id="source-correlation",
        accepted_vision_artifact_id=vision_id,
        accepted_product_goal_artifact_id=goal_id,
        repository_binding_id=binding_id,
        repository_binding_fingerprint=binding_fingerprint,
        capture_request_fingerprint="sha256:" + "c" * 64,
        source_fingerprint=source_bundle_fingerprint(bundle),
        bundle=bundle,
    )


def _seed_same_content_successor_goal(
    engine: Engine,
    *,
    project_id: int,
    prior_goal_id: int,
) -> int:
    """Resolve one Goal and accept a new Goal with identical semantic content."""
    with Session(engine) as session:
        prior_goal = session.get(ProductGoalArtifact, prior_goal_id)
        assert prior_goal is not None
        prior_turn = session.get(
            ProductGoalInterviewTurn,
            prior_goal.source_interview_turn_id,
        )
        assert prior_turn is not None
        session.add(
            ProductGoalOutcome(
                project_id=project_id,
                product_goal_artifact_id=prior_goal_id,
                artifact_fingerprint=prior_goal.content_fingerprint,
                outcome="fulfilled",
                rationale="The prior Goal is complete.",
                decided_by="operator@example.test",
                idempotency_key="same-content-prior-goal-fulfilled",
                decided_at=NOW + timedelta(minutes=1),
            )
        )
        attempt = _attempt(
            session,
            project_id=project_id,
            node_id="goal.interview",
            ordinal=3,
            started_at=NOW + timedelta(minutes=2),
        )
        turn = ProductGoalInterviewTurn(
            project_id=project_id,
            vision_artifact_id=prior_goal.vision_artifact_id,
            vision_fingerprint=prior_goal.vision_fingerprint,
            goal_number=prior_goal.goal_number + 1,
            revision_number=1,
            prior_turn_id=None,
            user_text="Reaffirm the exact Product Goal content.",
            components_json=prior_turn.components_json,
            goal_statement=prior_turn.goal_statement,
            is_complete=prior_turn.is_complete,
            clarifying_questions_json=prior_turn.clarifying_questions_json,
            output_fingerprint=prior_turn.output_fingerprint,
            workflow_node_attempt_id=_required(
                attempt.workflow_node_attempt_id,
                "same-content Goal attempt",
            ),
            attempt_fingerprint=attempt.attempt_fingerprint,
            recorded_at=NOW + timedelta(minutes=2, seconds=1),
        )
        session.add(turn)
        session.flush()
        successor = ProductGoalArtifact(
            project_id=project_id,
            vision_artifact_id=prior_goal.vision_artifact_id,
            vision_fingerprint=prior_goal.vision_fingerprint,
            goal_number=turn.goal_number,
            revision_number=turn.revision_number,
            statement=prior_goal.statement,
            content_fingerprint=prior_goal.content_fingerprint,
            supersedes_product_goal_artifact_id=None,
            source_interview_turn_id=_required(
                turn.product_goal_interview_turn_id,
                "same-content Goal turn",
            ),
            created_by="operator@example.test",
            created_at=NOW + timedelta(minutes=2, seconds=2),
        )
        session.add(successor)
        session.flush()
        successor_id = _required(
            successor.product_goal_artifact_id,
            "same-content Goal",
        )
        session.add(
            ProductGoalArtifactDecision(
                project_id=project_id,
                product_goal_artifact_id=successor_id,
                artifact_fingerprint=successor.content_fingerprint,
                decision="accepted",
                rationale="The new Goal intentionally retains the same semantics.",
                reviewer="operator@example.test",
                idempotency_key="same-content-successor-goal-accepted",
                decided_at=NOW + timedelta(minutes=2, seconds=3),
            )
        )
        _complete_attempt(
            session,
            project_id=project_id,
            attempt=attempt,
            recorded_at=NOW + timedelta(minutes=2, seconds=3),
        )
        session.commit()
        return successor_id


def test_registration_persists_exact_bundle_and_replays_from_receipt(
    engine: Engine,
) -> None:
    """One guarded command owns both immutable persistence and idempotency."""
    project_id, vision_id, vision_fp, goal_id, goal_fp, binding_id, binding_fp = (
        _seed_ready_project(engine)
    )
    domain = _domain(engine)
    bundle = _bundle(vision_fingerprint=vision_fp, goal_fingerprint=goal_fp)
    request = _request(
        domain,
        project_id=project_id,
        vision_id=vision_id,
        goal_id=goal_id,
        binding_id=binding_id,
        binding_fingerprint=binding_fp,
        bundle=bundle,
    )

    first = domain.transition(request)
    replay = domain.transition(request)

    assert first.ok is True
    assert first.applied_node_id == "specification.source.register"
    assert replay.ok is True
    assert replay.replayed is True
    assert replay.output == first.output
    with Session(engine) as session:
        sources = tuple(session.exec(select(SpecificationSource)).all())
        receipts = tuple(
            session.exec(
                select(WorkflowTransitionReceipt).where(
                    col(WorkflowTransitionReceipt.request_kind)
                    == "register_specification_source"
                )
            ).all()
        )
    assert len(sources) == 1
    assert len(receipts) == 1
    assert sources[0].source_bundle_json == canonical_json(
        bundle.model_dump(mode="json")
    )
    assert sources[0].source_fingerprint == source_bundle_fingerprint(bundle)
    assert sources[0].repository_binding_id == binding_id


def test_same_source_bytes_for_new_goal_identity_create_successor_source(
    engine: Engine,
) -> None:
    """Portable source equality must not erase exact current Goal lineage."""
    project_id, vision_id, _vision_fp, goal_id, goal_fp, binding_id, binding_fp = (
        _seed_ready_project(engine)
    )
    with Session(engine) as session:
        lineage = seed_accepted_specification(
            session,
            project_id=project_id,
            content='{"source":"same exact bytes"}',
            recorded_at=NOW,
        )
        candidate = session.get(
            SpecificationCandidate,
            lineage.specification_candidate_id,
        )
        assert candidate is not None
        prior_source = session.get(
            SpecificationSource,
            candidate.specification_source_id,
        )
        assert prior_source is not None
        prior_source_id = _required(
            prior_source.specification_source_id,
            "prior Specification source",
        )
        bundle = SpecificationSourceBundle.model_validate_json(
            prior_source.source_bundle_json
        )
        assert bundle.accepted_product_goal_fingerprint == goal_fp
    successor_goal_id = _seed_same_content_successor_goal(
        engine,
        project_id=project_id,
        prior_goal_id=goal_id,
    )
    domain = _domain(engine, evaluated_at=NOW + timedelta(minutes=3))
    before = domain.position(project_id)
    assert "specification.source.register" in before.available_nodes
    assert "specification.structure" not in before.available_nodes
    request = _request(
        domain,
        project_id=project_id,
        vision_id=vision_id,
        goal_id=successor_goal_id,
        binding_id=binding_id,
        binding_fingerprint=binding_fp,
        bundle=bundle,
    ).model_copy(update={"idempotency_key": "register-same-content-successor-goal"})

    result = domain.transition(request)

    assert result.ok is True
    assert result.output["created"] is True
    assert result.output["specification_source_id"] != prior_source_id
    with Session(engine) as session:
        sources = tuple(
            session.exec(
                select(SpecificationSource).order_by(
                    col(SpecificationSource.specification_source_id)
                )
            ).all()
        )
    assert len(sources) == EXPECTED_SOURCE_ROWS
    assert sources[0].source_fingerprint == sources[1].source_fingerprint
    assert sources[1].product_goal_artifact_id == successor_goal_id
    assert sources[1].supersedes_specification_source_id == prior_source_id
    assert sources[1].supersedes_source_fingerprint == sources[0].source_fingerprint
    after = domain.position(project_id)
    assert "specification.structure" in after.available_nodes
    structuring = next(
        item for item in after.decisions if item.node_id == "specification.structure"
    )
    source_reference = next(
        item
        for item in structuring.fact_references
        if item.fact_type == "specification_source"
    )
    assert source_reference.fact_id == str(sources[1].specification_source_id)


def test_registration_rechecks_exact_source_inside_transition_before_write(
    engine: Engine,
) -> None:
    """Post-capture source drift records stale and creates no source row."""
    project_id, vision_id, vision_fp, goal_id, goal_fp, binding_id, binding_fp = (
        _seed_ready_project(engine)
    )
    checked: list[PreparedSpecificationSourceRegistration] = []

    def stale(
        prepared: PreparedSpecificationSourceRegistration,
    ) -> SpecificationSourceRegistrationError:
        checked.append(prepared)
        return SpecificationSourceRegistrationError(
            SpecificationSourceRegistrationErrorCode.SOURCE_CHANGED_DURING_CAPTURE,
            "Source changed after preparation.",
        )

    domain = _domain(engine, registration_check=stale)
    bundle = _bundle(vision_fingerprint=vision_fp, goal_fingerprint=goal_fp)
    request = _request(
        domain,
        project_id=project_id,
        vision_id=vision_id,
        goal_id=goal_id,
        binding_id=binding_id,
        binding_fingerprint=binding_fp,
        bundle=bundle,
    )

    result = domain.transition(request)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.STALE_SPECIFICATION_INPUT
    assert len(checked) == 1
    assert checked[0].source_fingerprint == request.source_fingerprint
    with Session(engine) as session:
        assert session.exec(select(SpecificationSource)).first() is None


def test_registration_handler_rejects_mismatched_active_binding_identity(
    engine: Engine,
) -> None:
    """Exact graph guards cannot bypass the handler's durable binding check."""
    project_id, vision_id, vision_fp, goal_id, goal_fp, binding_id, binding_fp = (
        _seed_ready_project(engine)
    )
    domain = _domain(engine)
    bundle = _bundle(vision_fingerprint=vision_fp, goal_fingerprint=goal_fp)
    request = _request(
        domain,
        project_id=project_id,
        vision_id=vision_id,
        goal_id=goal_id,
        binding_id=binding_id,
        binding_fingerprint=binding_fp,
        bundle=bundle,
    )
    result = domain.transition(
        request.model_copy(
            update={"repository_binding_fingerprint": "sha256:" + "9" * 64}
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.STALE_SPECIFICATION_INPUT
    with Session(engine) as session:
        assert session.exec(select(SpecificationSource)).first() is None


def test_registration_appends_successor_after_rejected_candidate(
    engine: Engine,
) -> None:
    """A revised exact source supersedes the source that produced rejected work."""
    project_id, vision_id, vision_fp, goal_id, goal_fp, binding_id, binding_fp = (
        _seed_ready_project(engine)
    )
    domain = _domain(engine)
    with Session(engine) as session:
        active_binding = session.get(RepositoryBinding, binding_id)
        assert active_binding is not None
        active_binding.inspected_at = NOW.replace(hour=11, minute=40)
        session.add(active_binding)
        lineage = seed_accepted_specification(
            session,
            project_id=project_id,
            content=json.dumps(
                {
                    "schema_version": "agileforge.spec.v2",
                    "artifact_id": "SPEC.rejected-source",
                    "title": "Rejected source",
                    "summary": "Exercise source replacement.",
                    "problem_statement": ("The external source requires revision."),
                    "items": [
                        {
                            "id": "REQ.source.revision",
                            "type": "REQ",
                            "title": "Revise source",
                            "statement": (
                                "The source must be revised after rejection."
                            ),
                            "level": "MUST",
                            "verification": "integration-test",
                            "acceptance": ["A successor source is registered."],
                        }
                    ],
                }
            ),
            recorded_at=NOW.replace(hour=11, minute=50),
        )
        candidate = session.get(
            SpecificationCandidate,
            lineage.specification_candidate_id,
        )
        assert candidate is not None
        source = session.get(
            SpecificationSource,
            candidate.specification_source_id,
        )
        review = session.exec(
            select(SpecificationDecision).where(
                col(SpecificationDecision.specification_candidate_id)
                == lineage.specification_candidate_id
            )
        ).one()
        review.decision = "rejected"
        review.rationale = "Register revised external source."
        session.add(review)
        spec = session.get(SpecRegistry, lineage.spec.spec_version_id)
        assert spec is not None
        session.delete(spec)
        session.flush()
        assert source is not None
        assert source.specification_source_id is not None
        session.commit()
        original_id = source.specification_source_id
        original_fp = source.source_fingerprint
        binding_id = source.repository_binding_id
        binding = session.get(RepositoryBinding, binding_id)
        assert binding is not None
        binding_fp = repository_binding_fingerprint(binding)
        head_sha = binding.head_sha
        dirty = binding.dirty
        status_fingerprint = binding.status_fingerprint
    bundle = _bundle(
        vision_fingerprint=vision_fp,
        goal_fingerprint=goal_fp,
        content=b"# Revised after review\n",
        head_sha=head_sha,
        dirty=dirty,
        status_fingerprint=status_fingerprint,
    )
    request = _request(
        domain,
        project_id=project_id,
        vision_id=vision_id,
        goal_id=goal_id,
        binding_id=binding_id,
        binding_fingerprint=binding_fp,
        bundle=bundle,
    ).model_copy(update={"idempotency_key": "replacement-source"})

    result = domain.transition(request)

    assert result.ok is True
    with Session(engine) as session:
        sources = tuple(
            session.exec(
                select(SpecificationSource)
                .where(col(SpecificationSource.project_id) == project_id)
                .order_by(col(SpecificationSource.specification_source_id))
            ).all()
        )
    assert len(sources) == EXPECTED_SOURCE_ROWS
    replacement = sources[-1]
    assert replacement.specification_source_id != original_id
    assert replacement.supersedes_specification_source_id == original_id
    assert replacement.supersedes_source_fingerprint == original_fp
