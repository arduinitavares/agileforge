"""Transaction-bound live-source checks for Specification acceptance."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from sqlmodel import Session, SQLModel, col, create_engine, select

import workflow.domain as domain_module
from adapters.git.repository_probe import GitPythonRepositoryProbe
from models.product_definition import SpecificationCandidate, SpecificationDecision
from models.specs import SpecRegistry
from models.workflow import WorkflowNodeAttempt, WorkflowNodeAttemptOutcome
from services.contracts.specification_authoring import SpecificationAuthoringInput
from services.specification_authoring_input import SpecificationAuthoringInputService
from tests.workflow.test_product_discovery_transitions import (
    NOW,
    _accept_request,
    _author,
    _domain,
    _payload,
    _seed_accepted_goal,
)
from workflow.contracts import JsonObject, WorkflowError, WorkflowErrorCode
from workflow.domain import SpecificationSourceCheck, WorkflowDomain

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from workflow.contracts import NodeDecision, TransitionResult
    from workflow.requests import DecideSpecification


def _review_domain(
    engine: Engine,
    *,
    source_check: SpecificationSourceCheck,
    at: datetime = NOW + timedelta(seconds=1),
) -> WorkflowDomain:
    baseline = _domain(engine, at=at)
    return WorkflowDomain(
        engine=engine,
        graph=baseline._graph,
        clock=baseline._clock,
        adk_recipe_registry=baseline._adk_recipe_registry,
        specification_source_check=source_check,
    )


def _authored_candidate(engine: Engine, *, key: str) -> tuple[int, WorkflowDomain]:
    project_id, *_lineage = _seed_accepted_goal(engine, name=key)
    domain = _domain(engine)
    authored = _author(
        engine,
        domain,
        project_id=project_id,
        payload=_payload(),
        key=key,
    )
    assert authored.ok
    return project_id, domain


def test_completion_source_drift_obsoletes_attempt_without_candidate(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A final stale source probe cannot enter candidate persistence."""
    project_id, *_lineage = _seed_accepted_goal(engine, name="completion-stale")
    checked_inputs: list[JsonObject] = []
    source_error = WorkflowError(
        code=WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
        message="Repository source changed before candidate persistence.",
    )

    def stale_source(
        checked_project_id: int,
        persisted_input: JsonObject,
        /,
    ) -> WorkflowError | None:
        assert checked_project_id == project_id
        checked_inputs.append(persisted_input)
        return source_error

    def unexpected_handler(*_args: object, **_kwargs: object) -> TransitionResult:
        message = "candidate handler must not run after final source drift"
        raise AssertionError(message)

    monkeypatch.setattr(
        "workflow.domain.execute_complete_specification_authoring",
        unexpected_handler,
    )
    domain = _review_domain(engine, source_check=stale_source, at=NOW)

    result = _author(
        engine,
        domain,
        project_id=project_id,
        payload=_payload(),
        key="completion-stale",
    )

    assert not result.ok
    assert result.error == source_error
    assert len(checked_inputs) == 1
    with Session(engine) as session:
        attempt = session.exec(
            select(WorkflowNodeAttempt).where(
                col(WorkflowNodeAttempt.project_id) == project_id,
                col(WorkflowNodeAttempt.node_id) == "specification.author",
            )
        ).one()
        expected_input = SpecificationAuthoringInput.model_validate_json(
            attempt.normalized_input_json
        ).model_dump(mode="json")
        assert checked_inputs == [expected_input]
        assert not session.exec(
            select(SpecificationCandidate).where(
                col(SpecificationCandidate.project_id) == project_id
            )
        ).all()
        outcome = session.exec(
            select(WorkflowNodeAttemptOutcome)
            .join(
                WorkflowNodeAttempt,
                col(WorkflowNodeAttempt.workflow_node_attempt_id)
                == col(WorkflowNodeAttemptOutcome.workflow_node_attempt_id),
            )
            .where(
                col(WorkflowNodeAttemptOutcome.project_id) == project_id,
                col(WorkflowNodeAttempt.node_id) == "specification.author",
            )
        ).one()
        assert outcome.status == "obsolete"


def test_acceptance_checks_exact_attempt_input_immediately_before_handler(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The transaction owns the final live-source check and handler ordering."""
    project_id, _author_domain = _authored_candidate(engine, key="accept-current")
    events: list[str] = []
    checked_inputs: list[JsonObject] = []

    def source_check(
        checked_project_id: int,
        persisted_input: JsonObject,
        /,
    ) -> WorkflowError | None:
        assert checked_project_id == project_id
        events.append("source-check")
        checked_inputs.append(persisted_input)
        return None

    original = domain_module.execute_decide_specification

    def record_handler(
        session: Session,
        request: DecideSpecification,
        decision: NodeDecision,
        evaluated_at: datetime,
    ) -> TransitionResult:
        events.append("handler")
        return original(session, request, decision, evaluated_at)

    monkeypatch.setattr(domain_module, "execute_decide_specification", record_handler)
    domain = _review_domain(engine, source_check=source_check)
    result = domain.transition(
        _accept_request(domain, project_id=project_id, key="accept-current-review")
    )

    assert result.ok
    assert events == ["source-check", "handler"]
    with Session(engine) as session:
        candidate = session.exec(
            select(SpecificationCandidate).where(
                col(SpecificationCandidate.project_id) == project_id
            )
        ).one()
        attempt = session.get(
            WorkflowNodeAttempt,
            candidate.workflow_node_attempt_id,
        )
        assert attempt is not None
        expected = SpecificationAuthoringInput.model_validate_json(
            attempt.normalized_input_json
        ).model_dump(mode="json")
    assert checked_inputs == [expected]


def test_acceptance_source_drift_stops_handler_and_business_writes(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale final probe cannot create a decision or accepted Specification."""
    project_id, _author_domain = _authored_candidate(engine, key="accept-stale")
    source_error = WorkflowError(
        code=WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
        message="Repository source changed before acceptance.",
    )

    def source_check(
        checked_project_id: int,
        persisted_input: JsonObject,
        /,
    ) -> WorkflowError | None:
        assert checked_project_id == project_id
        assert persisted_input["project_id"] == project_id
        return source_error

    def unexpected_handler(*_args: object, **_kwargs: object) -> TransitionResult:
        message = "acceptance handler must not run after source drift"
        raise AssertionError(message)

    monkeypatch.setattr(
        "workflow.domain.execute_decide_specification",
        unexpected_handler,
    )
    domain = _review_domain(engine, source_check=source_check)
    result = domain.transition(
        _accept_request(domain, project_id=project_id, key="accept-stale-review")
    )

    assert not result.ok
    assert result.error == source_error
    with Session(engine) as session:
        assert not session.exec(
            select(SpecificationDecision).where(
                col(SpecificationDecision.project_id) == project_id
            )
        ).all()
        assert not session.exec(
            select(SpecRegistry).where(col(SpecRegistry.project_id) == project_id)
        ).all()


def test_production_source_checker_runs_inside_file_backed_transaction(
    tmp_path: Path,
) -> None:
    """The existing repository checker composes with the domain transaction."""
    file_engine = create_engine(f"sqlite:///{tmp_path / 'acceptance.sqlite'}")
    SQLModel.metadata.create_all(file_engine)
    try:
        project_id, _author_domain = _authored_candidate(
            file_engine,
            key="accept-production-checker",
        )
        input_service = SpecificationAuthoringInputService(
            engine=file_engine,
            repository_probe=GitPythonRepositoryProbe(),
        )
        domain = _review_domain(
            file_engine,
            source_check=input_service.revalidate_sources,
        )

        result = domain.transition(
            _accept_request(
                domain,
                project_id=project_id,
                key="accept-production-checker-review",
            )
        )

        assert result.ok
        with Session(file_engine) as session:
            assert session.exec(
                select(SpecificationDecision).where(
                    col(SpecificationDecision.project_id) == project_id
                )
            ).one().decision == "accepted"
    finally:
        file_engine.dispose()


@pytest.mark.parametrize("review_decision", ["rejected", "feedback"])
def test_non_acceptance_review_remains_available_when_sources_drift(
    engine: Engine,
    review_decision: str,
) -> None:
    """Repository drift cannot deadlock rejection or revision feedback."""
    project_id, _author_domain = _authored_candidate(
        engine,
        key=f"review-{review_decision}",
    )
    source_checks: list[str] = []

    def stale_source(
        _project_id: int,
        _persisted_input: JsonObject,
        /,
    ) -> WorkflowError | None:
        source_checks.append("checked")
        return WorkflowError(
            code=WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
            message="Repository source changed before review.",
        )

    domain = _review_domain(engine, source_check=stale_source)
    request = _accept_request(
        domain,
        project_id=project_id,
        key=f"review-{review_decision}-decision",
    ).model_copy(
        update={
            "decision": review_decision,
            "rationale": "Refresh repository-backed requirements.",
        }
    )

    result = domain.transition(request)

    assert result.ok
    assert source_checks == []
    with Session(engine) as session:
        decision = session.exec(
            select(SpecificationDecision).where(
                col(SpecificationDecision.project_id) == project_id
            )
        ).one()
        assert decision.decision == review_decision
        assert not session.exec(
            select(SpecRegistry).where(col(SpecRegistry.project_id) == project_id)
        ).all()
