"""Guarded authority transition and transaction-boundary tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, TypedDict

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlmodel import Session, select

from models.agent_workbench import (
    DiscoveryChallengeArtifact,
    DiscoveryPrd,
    DiscoverySpecAmendmentDraft,
)
from models.authority_curation import AuthorityFeedbackAttempt
from models.core import Product
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry
from models.workflow import (
    WorkflowNodeAttempt,
    WorkflowNodeAttemptOutcome,
    WorkflowTransitionReceipt,
)
from services.agent_workbench import authority_decision as authority_decision_service
from services.agent_workbench import authority_review as authority_review_service
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
EXPECTED_COMPILE_AND_DECISION_RECEIPTS = 2


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
        compiled_authority=_success_artifact(),
    )


def _install_fake_compiler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        compiler_service,
        "_invoke_compiler_for_version",
        lambda *_args, **_kwargs: compiler_service._CompilerInvocationResult(
            success=_success_artifact()
        ),
    )


def _seed_compile_attempt(
    session: Session,
    *,
    project_id: int,
    position: WorkflowPosition,
    outcome: str | None,
) -> None:
    decision = _decision(position, "authority.compile")
    attempt = WorkflowNodeAttempt(
        project_id=project_id,
        node_id="authority.compile",
        instance_key=decision.instance_key,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        business_fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=decision.decision_fingerprint,
        normalized_input_json="{}",
        input_fingerprint="sha256:old-input",
        model_id=DEFAULT_MODEL,
        execution_settings_json="{}",
        idempotency_key=f"old-{outcome or 'active'}-attempt",
        actor="old-operator@example.com",
        correlation_id="task-9-old-spec",
        started_at=EVALUATED_AT,
        lease_expires_at=EVALUATED_AT + timedelta(minutes=5),
        attempt_fingerprint=f"sha256:old-{outcome or 'active'}-attempt",
    )
    session.add(attempt)
    session.flush()
    assert attempt.workflow_node_attempt_id is not None
    if outcome is None:
        return
    session.add(
        WorkflowNodeAttemptOutcome(
            project_id=project_id,
            workflow_node_attempt_id=attempt.workflow_node_attempt_id,
            status=outcome,
            output_fingerprint="sha256:old-output" if outcome == "success" else None,
            output_json="{}" if outcome == "success" else None,
            failure_code="OLD_COMPILE_FAILED" if outcome == "failure" else None,
            failure_message="Old spec failed." if outcome == "failure" else None,
            recorded_at=EVALUATED_AT + timedelta(minutes=1),
        )
    )


def _approve_replacement_spec(
    session: Session,
    *,
    project_id: int,
    old_spec_version_id: int,
) -> tuple[int, str]:
    old_spec = session.get(SpecRegistry, old_spec_version_id)
    assert old_spec is not None
    old_spec.status = "superseded"
    payload = json.loads(old_spec.content)
    payload["artifact_id"] = "SPEC.authority.replacement"
    payload["version"] = "0.2"
    payload["summary"] = "Replacement authority workflow scope."
    normalized = compiler_service.normalize_spec_content_for_registry(
        json.dumps(payload)
    )
    replacement = SpecRegistry(
        product_id=project_id,
        spec_hash=normalized.spec_hash,
        content=normalized.content,
        content_ref=None,
        status="approved",
        approved_at=EVALUATED_AT + timedelta(minutes=2),
        approved_by="replacement-reviewer",
    )
    session.add_all([old_spec, replacement])
    session.commit()
    assert replacement.spec_version_id is not None
    return replacement.spec_version_id, replacement.spec_hash


def _seed_scope_discovery_review_evidence(
    session: Session,
    *,
    project_id: int,
    spec_version_id: int,
    spec_hash: str,
    spec_path: Path,
) -> int:
    challenge = DiscoveryChallengeArtifact(
        project_id=project_id,
        producer="task-9-review",
        readiness="ready_for_prd",
        original_idea="Bind authority review to all evidence.",
        content_json=json.dumps(
            {
                "assumptions": ["Review evidence remains immutable."],
                "non_goals": [],
                "risks": ["Persisted evidence can change after review."],
                "evidence_conflicts": [],
                "open_questions": [],
                "glossary_changes": [],
            }
        ),
        artifact_fingerprint="sha256:challenge-review-evidence",
        request_hash="sha256:challenge-review-request",
        idempotency_key="challenge-review-evidence",
    )
    session.add(challenge)
    session.flush()
    assert challenge.challenge_artifact_id is not None
    prd = DiscoveryPrd(
        project_id=project_id,
        challenge_artifact_id=challenge.challenge_artifact_id,
        producer="task-9-review",
        status="accepted",
        version="1",
        title="Authority review evidence",
        content_json=json.dumps({"summary": "Bind every review input."}),
        artifact_fingerprint="sha256:prd-review-evidence",
        request_hash="sha256:prd-review-request",
        idempotency_key="prd-review-evidence",
        reviewed_by="reviewer",
        reviewed_at=EVALUATED_AT,
    )
    session.add(prd)
    session.flush()
    assert prd.prd_id is not None
    session.add(
        DiscoverySpecAmendmentDraft(
            project_id=project_id,
            prd_id=prd.prd_id,
            challenge_artifact_id=challenge.challenge_artifact_id,
            status="accepted",
            amendment_file=str(spec_path),
            content_json=spec_path.read_text(encoding="utf-8"),
            validation_json=json.dumps({"valid": True, "blocking_issues": []}),
            artifact_fingerprint="sha256:amendment-review-evidence",
            request_hash="sha256:amendment-review-request",
            idempotency_key="amendment-review-evidence",
            base_spec_version_id=spec_version_id,
            base_spec_hash=spec_hash,
            amended_spec_hash=spec_hash,
            reviewed_by="reviewer",
            reviewed_at=EVALUATED_AT,
        )
    )
    session.commit()
    return challenge.challenge_artifact_id


def _tamper_review_input(
    session: Session,
    *,
    target: str,
    project_id: int,
    authority_id: int,
    challenge_id: int,
) -> None:
    authority = session.get(CompiledSpecAuthority, authority_id)
    assert authority is not None
    if target == "pending_authority":
        authority.compiled_at = authority.compiled_at + timedelta(seconds=1)
        session.add(authority)
    elif target == "compiler_invariants":
        assert authority.compiled_artifact_json is not None
        artifact = json.loads(authority.compiled_artifact_json)
        artifact["invariants"][0]["parameters"]["field_name"] = "tampered_id"
        authority.compiled_artifact_json = json.dumps(artifact)
        session.add(authority)
    elif target == "compiler_coverage":
        authority.rejected_features = json.dumps(
            [{"id": "REJ-tampered", "text": "Changed review classification."}]
        )
        session.add(authority)
    elif target == "scope_discovery":
        challenge = session.get(DiscoveryChallengeArtifact, challenge_id)
        assert challenge is not None
        payload = json.loads(challenge.content_json)
        payload["risks"].append("Changed after the review token was issued.")
        challenge.content_json = json.dumps(payload)
        session.add(challenge)
    elif target == "project_review_context":
        project = session.get(Product, project_id)
        assert project is not None
        project.name = f"{project.name} changed"
        session.add(project)
    else:
        pytest.fail(f"Unknown review tamper target: {target}")
    session.commit()


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
            "compiled_authority": _success_artifact().model_dump(mode="json"),
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
            "compiled_authority": _success_artifact().model_dump(mode="json"),
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


@pytest.mark.parametrize(
    "old_outcome",
    ["success", "failure", None],
    ids=["old-success", "old-failure", "old-active"],
)
def test_persisted_old_compile_attempt_does_not_scope_replacement_spec(
    engine: Engine,
    tmp_path: Path,
    old_outcome: str | None,
) -> None:
    """Only attempts bound to the exact current spec affect compile state."""
    project_id, old_spec_version_id, old_spec_hash = _seed_current_spec(
        engine, tmp_path / "old-spec.md"
    )
    domain = _domain(engine)
    old_position = domain.position(project_id)
    old_decision = _decision(old_position, "authority.compile")
    assert old_decision.instance_key == (
        f"spec:{old_spec_version_id}:{old_spec_hash}"
    )
    with Session(engine) as session:
        _seed_compile_attempt(
            session,
            project_id=project_id,
            position=old_position,
            outcome=old_outcome,
        )
        session.commit()
        new_spec_version_id, new_spec_hash = _approve_replacement_spec(
            session,
            project_id=project_id,
            old_spec_version_id=old_spec_version_id,
        )

    replacement = domain.position(project_id)
    replacement_decision = _decision(replacement, "authority.compile")

    assert replacement.available_nodes == ("authority.compile",)
    assert replacement.invalid_nodes == ()
    assert replacement.waiting_nodes == ()
    assert replacement_decision.instance_key == (
        f"spec:{new_spec_version_id}:{new_spec_hash}"
    )
    assert replacement_decision.instance_key != old_decision.instance_key
    assert replacement_decision.category is NodeCategory.AVAILABLE
    assert replacement_decision.reason_code == "AUTHORITY_COMPILE_REQUIRED"


def test_compile_rejects_spec_identity_not_selected_by_graph(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Compile rejects a spec ID not selected by the guarded node decision."""
    project_id, spec_version_id, spec_hash = _seed_current_spec(
        engine, tmp_path / "spec.md"
    )
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

    def provider_must_not_run(*_args: object, **_kwargs: object) -> None:
        pytest.fail("authority completion invoked the provider inside transition")

    monkeypatch.setattr(
        compiler_service,
        "_invoke_compiler_for_version",
        provider_must_not_run,
    )
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


def test_compile_completion_rejects_non_success_artifact_before_transition(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Accept only the closed validated compiler-success artifact contract."""
    project_id, spec_version_id, spec_hash = _seed_current_spec(
        engine, tmp_path / "spec.md"
    )
    domain = _domain(engine)
    position = domain.position(project_id)

    invalid_payload: object = {
        **_guards(position, "authority.compile"),
        "idempotency_key": "invalid-compiler-failure",
        "spec_version_id": spec_version_id,
        "expected_spec_hash": spec_hash,
        "compiled_authority": {
            "error": "COMPILATION_FAILED",
            "reason": "provider unavailable",
            "blocking_gaps": ["No provider result."],
        },
    }
    with pytest.raises(ValidationError):
        CompileAuthority.model_validate(invalid_payload)

    with Session(engine) as session:
        assert session.exec(select(CompiledSpecAuthority)).all() == []


def test_decision_binds_exact_pending_authority_and_review_fingerprint(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Decision requires the exact pending authority and review fingerprint."""
    project_id, spec_version_id, spec_hash = _seed_current_spec(
        engine, tmp_path / "spec.md"
    )
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
            review_fingerprint=review.review_fingerprint,
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
            review_fingerprint=review.review_fingerprint,
            decision="accepted",
            rationale="Reviewed and accepted.",
        )
    )
    assert accepted.ok is True
    assert accepted.position is not None
    assert accepted.position.available_nodes == ("vision.generate",)
    with Session(engine) as session:
        row = session.exec(select(SpecAuthorityAcceptance)).one()
        assert row.review_fingerprint == review.review_fingerprint
        assert row.authority_fingerprint == review.authority_fingerprint


@pytest.mark.parametrize(
    "tamper_target",
    [
        "pending_authority",
        "compiler_invariants",
        "compiler_coverage",
        "scope_discovery",
        "project_review_context",
    ],
)
def test_decide_rejects_every_persisted_review_fingerprint_tamper(
    engine: Engine,
    tmp_path: Path,
    tamper_target: str,
) -> None:
    """Every persisted review input is bound before a decision receipt exists."""
    spec_path = tmp_path / f"{tamper_target}.md"
    project_id, spec_version_id, spec_hash = _seed_current_spec(engine, spec_path)
    domain = _domain(engine)
    compiled = domain.transition(
        _compile_request(
            domain.position(project_id),
            spec_version_id=spec_version_id,
            spec_hash=spec_hash,
        )
    )
    assert compiled.ok is True
    with Session(engine) as session:
        challenge_id = _seed_scope_discovery_review_evidence(
            session,
            project_id=project_id,
            spec_version_id=spec_version_id,
            spec_hash=spec_hash,
            spec_path=spec_path,
        )
        review = build_authority_review_snapshot_in_session(
            session,
            project_id=project_id,
        )
        assert isinstance(review, AuthorityReviewSnapshot)
        assert review.pending_authority_id is not None
        assert review.authority_fingerprint is not None
        assert review.review_fingerprint != review.coverage_summary_fingerprint
        authority_id = review.pending_authority_id

    review_position = domain.position(project_id)
    idempotency_key = f"tampered-review-{tamper_target}"
    request = DecideAuthority(
        **_guards(review_position, "authority.review"),
        idempotency_key=idempotency_key,
        pending_authority_id=authority_id,
        authority_fingerprint=review.authority_fingerprint,
        review_fingerprint=review.review_fingerprint,
        decision="accepted",
        rationale="Accept only the packet that was actually reviewed.",
    )
    with Session(engine) as session:
        _tamper_review_input(
            session,
            target=tamper_target,
            project_id=project_id,
            authority_id=authority_id,
            challenge_id=challenge_id,
        )

    result = domain.transition(request)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    with Session(engine) as session:
        assert session.exec(select(SpecAuthorityAcceptance)).all() == []
        receipts = session.exec(
            select(WorkflowTransitionReceipt).where(
                WorkflowTransitionReceipt.idempotency_key == idempotency_key
            )
        ).all()
        assert receipts == []


def test_rejection_feedback_and_repair_are_durable_factual_transitions(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejection, feedback, and repair append durable facts in order."""
    project_id, spec_version_id, spec_hash = _seed_current_spec(
        engine, tmp_path / "spec.md"
    )

    def provider_must_not_run(*_args: object, **_kwargs: object) -> None:
        pytest.fail("authority completion invoked the provider inside transition")

    monkeypatch.setattr(
        compiler_service,
        "_invoke_compiler_for_version",
        provider_must_not_run,
    )
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
            review_fingerprint=review.review_fingerprint,
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
            compiled_authority=_success_artifact(),
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


def test_low_level_authority_services_keep_exact_caller_session_open(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compiler, review, and decision use one open caller-owned session."""
    project_id, spec_version_id, _spec_hash = _seed_current_spec(
        engine, tmp_path / "spec.md"
    )
    _install_fake_compiler(monkeypatch)
    session = Session(engine)
    original_close = session.close

    def reject_lifecycle_call() -> None:
        pytest.fail("low-level authority service changed caller session lifecycle")

    def reject_replacement_session(*_args: object, **_kwargs: object) -> None:
        pytest.fail("low-level authority service constructed a replacement Session")

    try:
        with monkeypatch.context() as lifecycle:
            lifecycle.setattr(session, "commit", reject_lifecycle_call)
            lifecycle.setattr(session, "rollback", reject_lifecycle_call)
            lifecycle.setattr(session, "close", reject_lifecycle_call)
            lifecycle.setattr(compiler_service, "Session", reject_replacement_session)
            lifecycle.setattr(
                authority_review_service,
                "Session",
                reject_replacement_session,
            )
            lifecycle.setattr(
                authority_decision_service,
                "Session",
                reject_replacement_session,
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
            decision = record_authority_decision_in_session(
                session,
                snapshot=review,
                decision="rejected",
                rationale="Caller owns this transaction.",
                actor="test-actor",
                policy="test",
                review_fingerprint=review.review_fingerprint,
                decided_at=EVALUATED_AT,
            )
            assert decision.id is not None
            assert session.get(SpecAuthorityAcceptance, decision.id) is decision
    finally:
        original_close()


def test_post_flush_compile_failure_rolls_back_and_identical_retry_replays(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-write exception rolls back authority, cache, and receipt."""
    project_id, spec_version_id, spec_hash = _seed_current_spec(
        engine, tmp_path / "compile-rollback.md"
    )
    domain = _domain(engine)
    request = _compile_request(
        domain.position(project_id),
        spec_version_id=spec_version_id,
        spec_hash=spec_hash,
        idempotency_key="compile-post-flush",
    )
    complete_receipt = domain._complete_receipt

    def fail_after_compile_flush(
        session: Session,
        receipt: WorkflowTransitionReceipt,
        result: object,
        _evaluated_at: datetime,
    ) -> None:
        assert session.exec(select(CompiledSpecAuthority)).one()
        project = session.get(Product, project_id)
        assert project is not None
        assert project.compiled_authority_json is not None
        assert receipt.workflow_transition_receipt_id is not None
        assert result is not None
        msg = "injected after compile writes"
        raise RuntimeError(msg)

    monkeypatch.setattr(domain, "_complete_receipt", fail_after_compile_flush)
    with pytest.raises(RuntimeError, match="injected after compile writes"):
        domain.transition(request)
    monkeypatch.setattr(domain, "_complete_receipt", complete_receipt)

    with Session(engine) as session:
        assert session.exec(select(CompiledSpecAuthority)).all() == []
        project = session.get(Product, project_id)
        assert project is not None
        assert project.compiled_authority_json is None
        assert session.exec(select(WorkflowTransitionReceipt)).all() == []

    first = domain.transition(request)
    replay = domain.transition(request)

    assert first.ok is True
    assert first.replayed is False
    assert replay.ok is True
    assert replay.replayed is True
    with Session(engine) as session:
        assert len(session.exec(select(CompiledSpecAuthority)).all()) == 1
        assert len(session.exec(select(WorkflowTransitionReceipt)).all()) == 1


def test_post_flush_decision_failure_rolls_back_and_identical_retry_replays(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-write exception rolls back acceptance audit and its receipt."""
    project_id, spec_version_id, spec_hash = _seed_current_spec(
        engine, tmp_path / "decision-rollback.md"
    )
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
    request = DecideAuthority(
        **_guards(review_position, "authority.review"),
        idempotency_key="decision-post-flush",
        pending_authority_id=review.pending_authority_id,
        authority_fingerprint=review.authority_fingerprint,
        review_fingerprint=review.review_fingerprint,
        decision="accepted",
        rationale="The exact packet is accepted.",
    )
    complete_receipt = domain._complete_receipt

    def fail_after_decision_flush(
        session: Session,
        receipt: WorkflowTransitionReceipt,
        result: object,
        _evaluated_at: datetime,
    ) -> None:
        acceptance = session.exec(select(SpecAuthorityAcceptance)).one()
        assert acceptance.decided_by == "operator@example.com"
        assert acceptance.actor_mode == "workflow_domain"
        assert receipt.workflow_transition_receipt_id is not None
        assert result is not None
        msg = "injected after decision writes"
        raise RuntimeError(msg)

    monkeypatch.setattr(domain, "_complete_receipt", fail_after_decision_flush)
    with pytest.raises(RuntimeError, match="injected after decision writes"):
        domain.transition(request)
    monkeypatch.setattr(domain, "_complete_receipt", complete_receipt)

    with Session(engine) as session:
        assert session.exec(select(SpecAuthorityAcceptance)).all() == []
        decision_receipts = session.exec(
            select(WorkflowTransitionReceipt).where(
                WorkflowTransitionReceipt.idempotency_key
                == "decision-post-flush"
            )
        ).all()
        assert decision_receipts == []

    first = domain.transition(request)
    replay = domain.transition(request)

    assert first.ok is True
    assert first.replayed is False
    assert replay.ok is True
    assert replay.replayed is True
    with Session(engine) as session:
        acceptance = session.exec(select(SpecAuthorityAcceptance)).one()
        assert acceptance.decided_by == "operator@example.com"
        assert acceptance.provenance_source == "workflow_domain"
        receipts = session.exec(select(WorkflowTransitionReceipt)).all()
        assert len(receipts) == EXPECTED_COMPILE_AND_DECISION_RECEIPTS
