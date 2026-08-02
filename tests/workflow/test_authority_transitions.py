"""Guarded authority transition and transaction-boundary tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypedDict

import pytest
from pydantic import TypeAdapter
from sqlmodel import Session, select

from models.authority_curation import AuthorityFeedbackAttempt
from models.core import Product
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry
from models.workflow import WorkflowTransitionReceipt
from services.agent_workbench.authority_decision import (
    record_authority_decision_in_session,
)
from services.agent_workbench.authority_review import (
    AuthorityReviewSnapshot,
    build_authority_review_snapshot_in_session,
)
from services.specs import compiler_service
from services.specs.compiler_service import (
    compile_spec_authority_for_version_in_session,
)
from utils.spec_schemas import (
    Invariant,
    InvariantType,
    RequiredFieldParams,
    SpecAuthorityCompilationSuccess,
)
from workflow.clock import FixedClock
from workflow.contracts import (
    NodeCategory,
    NodeDecision,
    WorkflowErrorCode,
    WorkflowPosition,
)
from workflow.definitions.authority import authority_graph
from workflow.domain import WorkflowDomain
from workflow.requests import (
    CompileAuthority,
    DecideAuthority,
    RecordAuthorityFeedback,
    RepairAuthority,
    TransitionRequest,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

EVALUATED_AT = datetime(2026, 8, 2, 12, tzinfo=UTC)
DEFAULT_MODEL = "openrouter/openai/gpt-5.6-luna"
EXPECTED_REPAIRED_AUTHORITY_COUNT = 2


class _RequestGuards(TypedDict):
    project_id: int
    graph_version: str
    fact_fingerprint: str
    decision_fingerprint: str
    instance_key: str | None
    actor: str
    correlation_id: str


def _success_artifact() -> SpecAuthorityCompilationSuccess:
    return SpecAuthorityCompilationSuccess(
        scope_themes=["Authority graph"],
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
        prompt_hash=compiler_service.compute_prompt_hash(
            compiler_service.SPEC_AUTHORITY_COMPILER_INSTRUCTIONS
        ),
    )


def _domain(engine: Engine) -> WorkflowDomain:
    return WorkflowDomain(
        engine=engine,
        graph=authority_graph(),
        clock=FixedClock(now_value=EVALUATED_AT),
    )


def _seed_current_spec(engine: Engine, spec_path: Path) -> tuple[int, int, str]:
    content = json.dumps(
        {
            "schema_version": "agileforge.spec.v1",
            "artifact_id": "SPEC.authority",
            "title": "Authority workflow",
            "status": "draft",
            "version": "0.1",
            "created_at": "2026-08-02",
            "updated_at": "2026-08-02",
            "summary": "Authority workflow scope.",
            "problem_statement": "Authority must be reviewed from durable facts.",
            "items": [
                {
                    "id": "REQ.authority.review",
                    "type": "REQ",
                    "status": "accepted",
                    "level": "MUST",
                    "title": "Review authority",
                    "statement": "Every Project MUST review compiled authority.",
                    "verification": "system-test",
                    "acceptance": ["A terminal review is stored."],
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
    )
    normalized = compiler_service.normalize_spec_content_for_registry(content)
    content = normalized.content
    spec_path.write_text(content, encoding="utf-8")
    with Session(engine) as session:
        project = Product(name=f"Authority {spec_path.stem}", origin="greenfield")
        session.add(project)
        session.flush()
        assert project.product_id is not None
        spec = SpecRegistry(
            product_id=project.product_id,
            spec_hash=normalized.spec_hash,
            content=content,
            content_ref=str(spec_path),
            status="approved",
            approved_at=EVALUATED_AT,
            approved_by="reviewer",
        )
        session.add(spec)
        session.commit()
        assert spec.spec_version_id is not None
        return project.product_id, spec.spec_version_id, spec.spec_hash


def _decision(position: WorkflowPosition, node_id: str) -> NodeDecision:
    return next(item for item in position.decisions if item.node_id == node_id)


def _guards(position: WorkflowPosition, node_id: str) -> _RequestGuards:
    decision = _decision(position, node_id)
    return {
        "project_id": position.project_id,
        "graph_version": position.graph_version,
        "fact_fingerprint": position.fact_fingerprint,
        "decision_fingerprint": decision.decision_fingerprint,
        "instance_key": decision.instance_key,
        "actor": "operator@example.com",
        "correlation_id": "task-9",
    }


def _compile_request(
    position: WorkflowPosition,
    *,
    spec_version_id: int,
    spec_hash: str,
    idempotency_key: str = "compile-authority",
) -> CompileAuthority:
    return CompileAuthority(
        **_guards(position, "authority.compile"),
        idempotency_key=idempotency_key,
        spec_version_id=spec_version_id,
        expected_spec_hash=spec_hash,
    )


def _install_fake_compiler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        compiler_service,
        "_invoke_compiler_for_version",
        lambda *_args, **_kwargs: compiler_service._CompilerInvocationResult(
            success=_success_artifact()
        ),
    )


def test_closed_request_union_adds_exact_authority_variants() -> None:
    """The closed request union validates exactly the four authority shapes."""
    common = {
        "project_id": 1,
        "graph_version": "graph-v1",
        "fact_fingerprint": "sha256:facts",
        "decision_fingerprint": "sha256:decision",
        "idempotency_key": "request-key",
        "actor": "operator@example.com",
    }
    payloads = (
        {
            **common,
            "kind": "compile_authority",
            "spec_version_id": 1,
            "expected_spec_hash": "sha256:spec",
        },
        {
            **common,
            "kind": "decide_authority",
            "pending_authority_id": 2,
            "authority_fingerprint": "sha256:authority",
            "review_fingerprint": "sha256:review",
            "decision": "accepted",
            "rationale": "Accepted.",
        },
        {
            **common,
            "kind": "record_authority_feedback",
            "pending_authority_id": 2,
            "authority_fingerprint": "sha256:authority",
            "feedback": {},
        },
        {
            **common,
            "kind": "repair_authority",
            "source_authority_id": 2,
            "source_authority_fingerprint": "sha256:authority",
        },
    )
    variants = {
        type(TypeAdapter(TransitionRequest).validate_python(payload))
        for payload in payloads
    }

    assert {
        CompileAuthority,
        DecideAuthority,
        RecordAuthorityFeedback,
        RepairAuthority,
    } <= variants
    assert CompileAuthority.node_id == "authority.compile"
    assert DecideAuthority.node_id == "authority.review"
    assert RecordAuthorityFeedback.node_id == "authority.feedback"
    assert RepairAuthority.node_id == "authority.repair"


def test_compile_request_has_exact_production_model_default() -> None:
    """CompileAuthority uses the approved production model by default."""
    fields = CompileAuthority.model_fields

    assert fields["compiler_model"].default == DEFAULT_MODEL


def test_compile_rejects_spec_identity_not_selected_by_graph(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compile rejects a spec ID not selected by the guarded node decision."""
    project_id, spec_version_id, spec_hash = _seed_current_spec(
        engine, tmp_path / "spec.md"
    )
    _install_fake_compiler(monkeypatch)
    domain = _domain(engine)
    before = domain.position(project_id)

    result = domain.transition(
        _compile_request(
            before,
            spec_version_id=spec_version_id + 1,
            spec_hash=spec_hash,
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    with Session(engine) as session:
        assert session.exec(select(CompiledSpecAuthority)).all() == []


def test_compile_persists_pending_authority_in_domain_transaction(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compile persists authority and receipt in one domain transaction."""
    project_id, spec_version_id, spec_hash = _seed_current_spec(
        engine, tmp_path / "spec.md"
    )
    _install_fake_compiler(monkeypatch)
    domain = _domain(engine)
    result = domain.transition(
        _compile_request(
            domain.position(project_id),
            spec_version_id=spec_version_id,
            spec_hash=spec_hash,
        )
    )

    assert result.ok is True
    assert result.applied_node_id == "authority.compile"
    assert result.position is not None
    assert result.position.waiting_nodes == ("authority.review",)
    with Session(engine) as session:
        authority = session.exec(select(CompiledSpecAuthority)).one()
        receipt = session.exec(select(WorkflowTransitionReceipt)).one()
        assert authority.spec_version_id == spec_version_id
        assert receipt.completed_at is not None
        assert receipt.completed_at.replace(tzinfo=UTC) == EVALUATED_AT


def test_compiler_failure_is_atomic_and_offers_compile_again(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compiler failure leaves no partial authority and remains retryable."""
    project_id, spec_version_id, spec_hash = _seed_current_spec(
        engine, tmp_path / "spec.md"
    )
    monkeypatch.setattr(
        compiler_service,
        "_invoke_compiler_for_version",
        lambda *_args, **_kwargs: compiler_service._CompilerInvocationResult(
            failure={"success": False, "error": "provider unavailable"}
        ),
    )
    domain = _domain(engine)

    result = domain.transition(
        _compile_request(
            domain.position(project_id),
            spec_version_id=spec_version_id,
            spec_hash=spec_hash,
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.EXTERNAL_EXECUTION_FAILED
    assert result.position is not None
    assert result.position.available_nodes == ("authority.compile",)
    with Session(engine) as session:
        assert session.exec(select(CompiledSpecAuthority)).all() == []


def test_decision_binds_exact_pending_authority_and_review_fingerprint(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision requires the exact pending authority and review fingerprint."""
    project_id, spec_version_id, spec_hash = _seed_current_spec(
        engine, tmp_path / "spec.md"
    )
    _install_fake_compiler(monkeypatch)
    domain = _domain(engine)
    compiled = domain.transition(
        _compile_request(
            domain.position(project_id),
            spec_version_id=spec_version_id,
            spec_hash=spec_hash,
        )
    )
    assert compiled.ok is True
    review_position = domain.position(project_id)
    review_decision = _decision(review_position, "authority.review")
    assert review_decision.category is NodeCategory.WAITING
    with Session(engine) as session:
        review = build_authority_review_snapshot_in_session(
            session,
            project_id=project_id,
        )
        assert isinstance(review, AuthorityReviewSnapshot)
        decision = record_authority_decision_in_session(
            session,
            snapshot=review,
            decision="rejected",
            rationale="Needs factual repair.",
            actor="test-actor",
            policy="test",
            review_fingerprint=review.coverage_summary_fingerprint,
            decided_at=EVALUATED_AT,
        )
        assert decision.id is not None
        assert review.pending_authority_id is not None
        assert review.authority_fingerprint is not None

    stale = domain.transition(
        DecideAuthority(
            **_guards(review_position, "authority.review"),
            idempotency_key="stale-review",
            pending_authority_id=review.pending_authority_id,
            authority_fingerprint=review.authority_fingerprint,
            review_fingerprint="sha256:wrong-review",
            decision="accepted",
            rationale="Reviewed and accepted.",
        )
    )
    assert stale.ok is False
    assert stale.error is not None
    assert stale.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT

    accepted = domain.transition(
        DecideAuthority(
            **_guards(review_position, "authority.review"),
            idempotency_key="accept-review",
            pending_authority_id=review.pending_authority_id,
            authority_fingerprint=review.authority_fingerprint,
            review_fingerprint=review.coverage_summary_fingerprint,
            decision="accepted",
            rationale="Reviewed and accepted.",
        )
    )
    assert accepted.ok is True
    assert accepted.position is not None
    assert accepted.position.available_nodes == ("vision.generate",)
    with Session(engine) as session:
        row = session.exec(select(SpecAuthorityAcceptance)).one()
        assert row.review_fingerprint == review.coverage_summary_fingerprint
        assert row.authority_fingerprint == review.authority_fingerprint


def test_rejection_feedback_and_repair_are_durable_factual_transitions(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejection, feedback, and repair append durable facts in order."""
    project_id, spec_version_id, spec_hash = _seed_current_spec(
        engine, tmp_path / "spec.md"
    )
    _install_fake_compiler(monkeypatch)
    domain = _domain(engine)
    assert domain.transition(
        _compile_request(
            domain.position(project_id),
            spec_version_id=spec_version_id,
            spec_hash=spec_hash,
        )
    ).ok
    review_position = domain.position(project_id)
    with Session(engine) as session:
        review = build_authority_review_snapshot_in_session(
            session,
            project_id=project_id,
        )
        assert isinstance(review, AuthorityReviewSnapshot)
        assert review.pending_authority_id is not None
        assert review.authority_fingerprint is not None
    rejected = domain.transition(
        DecideAuthority(
            **_guards(review_position, "authority.review"),
            idempotency_key="reject-review",
            pending_authority_id=review.pending_authority_id,
            authority_fingerprint=review.authority_fingerprint,
            review_fingerprint=review.coverage_summary_fingerprint,
            decision="rejected",
            rationale="Needs a narrower invariant.",
        )
    )
    assert rejected.ok is True
    assert rejected.position is not None
    assert rejected.position.available_nodes == ("authority.feedback",)

    feedback_position = rejected.position
    feedback = domain.transition(
        RecordAuthorityFeedback(
            **_guards(feedback_position, "authority.feedback"),
            idempotency_key="record-feedback",
            pending_authority_id=review.pending_authority_id,
            authority_fingerprint=review.authority_fingerprint,
            feedback={"summary": "Narrow the Project identity invariant."},
        )
    )
    assert feedback.ok is True
    assert feedback.position is not None
    assert feedback.position.available_nodes == ("authority.repair",)

    repair = domain.transition(
        RepairAuthority(
            **_guards(feedback.position, "authority.repair"),
            idempotency_key="repair-authority",
            source_authority_id=review.pending_authority_id,
            source_authority_fingerprint=review.authority_fingerprint,
        )
    )
    assert repair.ok is True
    assert repair.position is not None
    assert repair.position.waiting_nodes == ("authority.review",)
    with Session(engine) as session:
        assert len(session.exec(select(AuthorityFeedbackAttempt)).all()) == 1
        assert (
            len(session.exec(select(CompiledSpecAuthority)).all())
            == EXPECTED_REPAIRED_AUTHORITY_COUNT
        )


def test_low_level_authority_services_never_commit_or_rollback_caller_session(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Low-level authority services never own the caller transaction."""
    project_id, spec_version_id, _spec_hash = _seed_current_spec(
        engine, tmp_path / "spec.md"
    )
    _install_fake_compiler(monkeypatch)
    with Session(engine) as session:
        monkeypatch.setattr(
            session,
            "commit",
            lambda: pytest.fail("low-level compiler committed caller session"),
        )
        monkeypatch.setattr(
            session,
            "rollback",
            lambda: pytest.fail("low-level compiler rolled back caller session"),
        )
        result = compile_spec_authority_for_version_in_session(
            session,
            spec_version_id=spec_version_id,
            compiled_at=EVALUATED_AT,
            compiler_model=DEFAULT_MODEL,
        )
        assert result["success"] is True
        review = build_authority_review_snapshot_in_session(
            session,
            project_id=project_id,
        )
        assert isinstance(review, AuthorityReviewSnapshot)
