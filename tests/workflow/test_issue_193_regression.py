"""Issue 193 regression through persisted domain transitions."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlmodel import Session, col, select

from models.specs import CompiledSpecAuthority, SpecRegistry
from models.workflow import DiscoveryRun, ScopeExtensionRegistration
from services.agent_workbench.authority_review import (
    AuthorityReviewSnapshot,
    build_authority_review_snapshot_in_session,
)
from services.specs import compiler_service
from tests.workflow.test_authority_transitions import _success_artifact
from tests.workflow.test_scope_extension_transitions import (
    _current_spec,
    _decision,
    _guards,
    accept_amendment_draft,
    register_amendment,
    seed_terminal_project,
    start_extension,
)
from workflow.contracts import (
    FactReference,
    NodeCategory,
    RecommendationKind,
    WorkflowErrorCode,
)
from workflow.requests import (
    CompileAuthority,
    DecideAuthority,
    ReconcileScopeExtension,
    ScopeExtensionArtifactReference,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from sqlalchemy.engine import Engine


def _install_fake_compiler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        compiler_service,
        "_invoke_compiler_for_version",
        lambda *_args, **_kwargs: compiler_service._CompilerInvocationResult(
            success=_success_artifact()
        ),
    )


def _artifact_reference(reference: FactReference) -> ScopeExtensionArtifactReference:
    if reference.fact_type == "vision":
        artifact_type = "vision"
    elif reference.fact_type == "backlog":
        artifact_type = "backlog"
    elif reference.fact_type == "roadmap":
        artifact_type = "roadmap"
    elif reference.fact_type == "story":
        artifact_type = "story"
    else:
        message = f"Unsupported reconciliation fact type: {reference.fact_type}"
        raise ValueError(message)
    return ScopeExtensionArtifactReference(
        artifact_type=artifact_type,
        artifact_id=int(reference.fact_id),
        artifact_fingerprint=reference.fingerprint,
    )


def test_issue_193_old_extension_actions_are_stale_after_completed_run(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reject old advertised actions after the applied extension is closed."""
    domain, project_id = seed_terminal_project(engine)
    advertised_start = _decision(domain.position(project_id), "scope_extension.start")
    old_start_request, run_id = start_extension(domain, engine, project_id)
    draft_id, _content = accept_amendment_draft(
        domain,
        engine,
        project_id,
        run_id,
        provenance_path=tmp_path / "accepted-amendment.json",
    )
    old_registration_request = register_amendment(
        domain,
        project_id,
        run_id,
        draft_id,
    )

    _install_fake_compiler(monkeypatch)
    replacement = _current_spec(engine, project_id)
    assert replacement.spec_version_id is not None
    compiled = domain.transition(
        CompileAuthority(
            **_guards(
                domain,
                project_id,
                "authority.compile",
                f"spec:{replacement.spec_version_id}:{replacement.spec_hash}",
            ),
            idempotency_key="task-13-compile-replacement",
            spec_version_id=replacement.spec_version_id,
            expected_spec_hash=replacement.spec_hash,
        )
    )
    assert compiled.ok is True

    with Session(engine) as session:
        review = build_authority_review_snapshot_in_session(
            session,
            project_id=project_id,
        )
        assert isinstance(review, AuthorityReviewSnapshot)
        assert review.pending_authority_id is not None
        assert review.authority_fingerprint is not None
    accepted = domain.transition(
        DecideAuthority(
            **_guards(domain, project_id, "authority.review"),
            idempotency_key="task-13-accept-replacement-authority",
            pending_authority_id=review.pending_authority_id,
            authority_fingerprint=review.authority_fingerprint,
            review_fingerprint=review.review_fingerprint,
            decision="accepted",
            rationale="Replacement authority matches the accepted amendment.",
        )
    )
    assert accepted.ok is True

    reconciliation_position = domain.position(project_id)
    reconciliation = _decision(
        reconciliation_position,
        "scope_extension.reconciliation",
        f"run:{run_id}",
    )
    artifact_references = tuple(
        _artifact_reference(item)
        for item in reconciliation.fact_references
        if item.fact_type in {"vision", "backlog", "roadmap", "story"}
    )
    reconciled = domain.transition(
        ReconcileScopeExtension(
            **_guards(
                domain,
                project_id,
                "scope_extension.reconciliation",
                f"run:{run_id}",
            ),
            idempotency_key="task-13-reconcile",
            discovery_run_id=run_id,
            replacement_authority_id=review.pending_authority_id,
            replacement_authority_fingerprint=review.authority_fingerprint,
            artifact_references=artifact_references,
        )
    )
    assert reconciled.ok is True

    completed_position = domain.position(project_id)
    scope_required_or_recovery = tuple(
        item
        for item in completed_position.decisions
        if item.child_graph_id == "scope_extension"
        and item.recommendation_kind
        in {RecommendationKind.REQUIRED, RecommendationKind.RECOVERY}
    )
    assert scope_required_or_recovery == ()
    assert not any(
        item.node_id == "scope_extension.registration"
        and item.instance_key == f"run:{run_id}"
        for item in completed_position.decisions
    )
    fresh_start = _decision(completed_position, "scope_extension.start")
    assert completed_position.terminal is True, tuple(
        (item.node_id, item.category.value, item.reason_code)
        for item in completed_position.decisions
        if item.recommendation_kind
        in {RecommendationKind.REQUIRED, RecommendationKind.RECOVERY}
    )
    assert fresh_start.category is NodeCategory.AVAILABLE
    assert fresh_start.recommendation_kind is RecommendationKind.OPTIONAL_REENTRY
    assert fresh_start.decision_fingerprint != advertised_start.decision_fingerprint

    with Session(engine) as session:
        before_specs = len(
            session.exec(
                select(SpecRegistry).where(col(SpecRegistry.product_id) == project_id)
            ).all()
        )
        before_runs = len(
            session.exec(
                select(DiscoveryRun).where(col(DiscoveryRun.project_id) == project_id)
            ).all()
        )
        assert (
            len(
                session.exec(
                    select(ScopeExtensionRegistration).where(
                        col(ScopeExtensionRegistration.project_id) == project_id
                    )
                ).all()
            )
            == 1
        )
        assert (
            len(
                session.exec(
                    select(CompiledSpecAuthority).where(
                        col(CompiledSpecAuthority.spec_version_id)
                        == replacement.spec_version_id
                    )
                ).all()
            )
            == 1
        )

    stale_registration = domain.transition(
        old_registration_request.model_copy(
            update={"idempotency_key": "task-13-stale-registration"}
        )
    )
    stale_start = domain.transition(
        old_start_request.model_copy(update={"idempotency_key": "task-13-stale-start"})
    )
    for result in (stale_registration, stale_start):
        assert result.ok is False
        assert result.error is not None
        assert result.error.code is WorkflowErrorCode.STALE_POSITION

    with Session(engine) as session:
        assert (
            len(
                session.exec(
                    select(SpecRegistry).where(
                        col(SpecRegistry.product_id) == project_id
                    )
                ).all()
            )
            == before_specs
        )
        assert (
            len(
                session.exec(
                    select(DiscoveryRun).where(
                        col(DiscoveryRun.project_id) == project_id
                    )
                ).all()
            )
            == before_runs
        )
