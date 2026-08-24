"""Provider-free transactional tests for Specification structuring."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from git import Repo
from pydantic import ValidationError
from sqlmodel import Session, col, select

from adapters.git.repository_probe import GitPythonRepositoryProbe
from models.core import Project
from models.product_definition import (
    SpecificationCandidate,
    SpecificationDecision,
    SpecificationSource,
)
from models.repository import RepositoryBinding
from models.specs import SpecRegistry
from models.workflow import WorkflowNodeAttempt, WorkflowNodeAttemptOutcome
from services.contracts.specification_authoring import (
    SPECIFICATION_STRUCTURER_PROMPT_VERSION,
    SPECIFICATION_VISION_SOURCE_ID,
    SpecificationStructuringInput,
    specification_structuring_fact_fingerprint,
    specification_structuring_input_fingerprint,
)
from services.specification_authoring_input import SpecificationStructuringInputService
from services.specification_source_registration import (
    SpecificationSourceRegistrationRequest,
    SpecificationSourceRegistrationService,
)
from services.specs.candidate_contract import load_candidate_contract
from tests.workflow.lifecycle_fixtures import _seed_accepted_vision_and_goal
from utils.agileforge_spec_profile_v2 import SpecificationPayload
from workflow.clock import FixedClock
from workflow.contracts import GRAPH_VERSION, TransitionResult, WorkflowErrorCode
from workflow.definitions.product_discovery import SPECIFICATION_NODES
from workflow.domain import WorkflowDomain
from workflow.fingerprints import canonical_json
from workflow.graph import ChildGraphSpec, WorkflowGraph
from workflow.handlers.product_discovery import execute_decide_specification
from workflow.requests import (
    CompleteSpecificationStructuring,
    DecideSpecification,
    RegisterSpecificationSource,
    StartNodeAttempt,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from services.repository_probe import RepositoryProbe, RepositoryProbeResult

NOW = datetime(2026, 8, 11, 12, tzinfo=UTC)
EXPECTED_REVISION_CANDIDATES = 2


class _Registry:
    """Expose the sole recipe needed by these provider-free domain tests."""

    def require(self, node_id: str) -> object:
        if node_id != "specification.structure":
            raise LookupError(node_id)
        return object()


def _domain(
    engine: Engine,
    *,
    at: datetime = NOW,
    repository_probe: RepositoryProbe | None = None,
) -> WorkflowDomain:
    del repository_probe
    return WorkflowDomain(
        engine=engine,
        graph=WorkflowGraph(
            graph_version=GRAPH_VERSION,
            root=ChildGraphSpec(
                child_graph_id="specification",
                nodes=SPECIFICATION_NODES,
            ),
        ),
        clock=FixedClock(now_value=at),
        adk_recipe_registry=_Registry(),
        specification_source_check=lambda _project_id, _input: None,
        specification_registration_check=lambda _prepared: None,
    )


def _repository(tmp_path: Path, *, name: str) -> Path:
    repository = tmp_path / name
    repository.mkdir()
    (repository / "SPECIFICATION.md").write_bytes(
        b"# Exact external Specification\r\n\r\nPreserve these bytes.\r\n"
    )
    with Repo.init(repository) as repo:
        with repo.config_writer() as config:
            config.set_value("user", "name", "Specification Test")
            config.set_value("user", "email", "specification@example.test")
        repo.index.add(["SPECIFICATION.md"])
        repo.index.commit("register exact source")
    return repository


def _record_binding(
    engine: Engine,
    *,
    project_id: int,
    repository: Path,
    probe: RepositoryProbe,
    inspected_at: datetime = NOW - timedelta(hours=1),
) -> None:
    observed: RepositoryProbeResult = probe.inspect(repository)
    with Session(engine) as session:
        project = session.get(Project, project_id)
        assert project is not None
        prior_id = project.active_repository_binding_id
        binding = RepositoryBinding(
            project_id=project_id,
            worktree_path=observed.worktree_path,
            common_git_dir=observed.common_git_dir,
            head_sha=observed.head_sha,
            branch_name=observed.branch_name,
            detached_head=observed.detached_head,
            dirty=observed.dirty,
            status_fingerprint=observed.status_fingerprint,
            status_entries_json=canonical_json(
                [item.model_dump(mode="json") for item in observed.status_entries]
            ),
            remotes_json=canonical_json(list(observed.remotes)),
            warnings_json=canonical_json(
                [item.model_dump(mode="json") for item in observed.warnings]
            ),
            probe_version=observed.probe_version,
            inspected_at=inspected_at,
            supersedes_repository_binding_id=prior_id,
            recorded_by="operator",
        )
        session.add(binding)
        session.flush()
        assert binding.repository_binding_id is not None
        project.active_repository_binding_id = binding.repository_binding_id
        session.add(project)
        session.commit()


def _seed_accepted_goal(
    engine: Engine,
    *,
    name: str,
    repository: Path,
    repository_probe: RepositoryProbe | None = None,
) -> tuple[int, int, str, int, str]:
    probe = repository_probe or GitPythonRepositoryProbe()
    with Session(engine) as session:
        project = Project(name=name)
        session.add(project)
        session.flush()
        assert project.project_id is not None
        project_id = project.project_id
        vision, goal = _seed_accepted_vision_and_goal(
            session,
            project_id=project_id,
            recorded_at=NOW.replace(hour=10),
        )
        session.commit()
        assert vision.vision_artifact_id is not None
        assert goal.product_goal_artifact_id is not None
        lineage = (
            project_id,
            vision.vision_artifact_id,
            vision.content_fingerprint,
            goal.product_goal_artifact_id,
            goal.content_fingerprint,
        )
    _record_binding(
        engine,
        project_id=project_id,
        repository=repository,
        probe=probe,
    )
    return lineage


def _register_source(
    engine: Engine,
    domain: WorkflowDomain,
    *,
    project_id: int,
    repository_probe: RepositoryProbe,
    key: str,
) -> TransitionResult:
    semantic_request = SpecificationSourceRegistrationRequest(
        project_id=project_id,
        source_path="SPECIFICATION.md",
        preparation_capability="grill-with-docs",
        idempotency_key=f"{key}-prepare",
        actor="operator",
        correlation_id=f"{key}-correlation",
    )
    prepared = SpecificationSourceRegistrationService(
        engine=engine,
        repository_probe=repository_probe,
    ).prepare(semantic_request)
    position = domain.position(project_id)
    decision = next(
        item
        for item in position.decisions
        if item.node_id == "specification.source.register"
    )
    return domain.transition(
        RegisterSpecificationSource(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=decision.decision_fingerprint,
            idempotency_key=f"{key}-register",
            actor="operator",
            correlation_id=f"{key}-correlation",
            accepted_vision_artifact_id=prepared.accepted_vision_artifact_id,
            accepted_product_goal_artifact_id=(
                prepared.accepted_product_goal_artifact_id
            ),
            repository_binding_id=prepared.repository_binding_id,
            repository_binding_fingerprint=(prepared.repository_binding_fingerprint),
            capture_request_fingerprint=prepared.request_fingerprint,
            source_fingerprint=prepared.source_fingerprint,
            bundle=prepared.bundle,
        )
    )


def _ready_project(
    engine: Engine,
    tmp_path: Path,
    *,
    name: str,
) -> tuple[int, int, str, int, str, Path, RepositoryProbe]:
    repository = _repository(tmp_path, name=name)
    probe: RepositoryProbe = GitPythonRepositoryProbe()
    project_id, vision_id, vision_fp, goal_id, goal_fp = _seed_accepted_goal(
        engine,
        name=name,
        repository=repository,
        repository_probe=probe,
    )
    registered = _register_source(
        engine,
        _domain(
            engine,
            at=NOW - timedelta(seconds=10),
            repository_probe=probe,
        ),
        project_id=project_id,
        repository_probe=probe,
        key=f"{name}-source",
    )
    assert registered.ok
    return (
        project_id,
        vision_id,
        vision_fp,
        goal_id,
        goal_fp,
        repository,
        probe,
    )


def _payload(
    *,
    artifact_id: str = "SPEC.source-structuring",
    item_id: str = "REQ.persist-candidate",
    source_id: str | None = None,
) -> SpecificationPayload:
    source_notes: list[dict[str, str]] = []
    if source_id is not None:
        source_notes.append(
            {
                "source_id": source_id,
                "kind": "interview",
                "text": "Accepted product-definition evidence.",
            }
        )
    return SpecificationPayload.model_validate(
        {
            "schema_version": "agileforge.spec.v2",
            "artifact_id": artifact_id,
            "title": "Source structuring",
            "summary": "Persist one exact typed candidate.",
            "problem_statement": "External prose must be structured before review.",
            "items": [
                {
                    "id": item_id,
                    "type": "REQ",
                    "title": "Persist candidate",
                    "statement": "The host persists one immutable candidate.",
                    "level": "MUST",
                    "verification": "integration-test",
                    "acceptance": ["The exact candidate can be reviewed."],
                    "source_notes": source_notes,
                }
            ],
        }
    )


def _structure(  # noqa: PLR0913
    engine: Engine,
    domain: WorkflowDomain,
    *,
    project_id: int,
    payload: SpecificationPayload,
    key: str,
    repository_probe: RepositoryProbe | None = None,
) -> TransitionResult:
    probe = repository_probe or GitPythonRepositoryProbe()
    position = domain.position(project_id)
    decision = next(
        item for item in position.decisions if item.node_id == "specification.structure"
    )
    start = domain.transition(
        StartNodeAttempt(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=decision.decision_fingerprint,
            idempotency_key=f"{key}-start",
            actor="worker",
            correlation_id=f"{key}-correlation",
            target_node_id="specification.structure",
            target_instance_key=decision.instance_key,
            normalized_input=SpecificationStructuringInputService(
                engine=engine,
                repository_probe=probe,
            ).build(
                project_id=project_id,
                decision=decision,
            ),
            model_id="fake/specification-structurer",
            execution_settings={"temperature": 0},
            lease_seconds=60,
        )
    )
    assert start.ok
    attempt_id = start.output["attempt_id"]
    attempt_fingerprint = start.output["attempt_fingerprint"]
    assert isinstance(attempt_id, int)
    assert isinstance(attempt_fingerprint, str)
    return domain.transition(
        CompleteSpecificationStructuring(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=decision.decision_fingerprint,
            idempotency_key=f"{key}-complete",
            actor="worker",
            correlation_id=f"{key}-correlation",
            attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
            payload=payload,
        )
    )


def _accept_request(
    domain: WorkflowDomain,
    *,
    project_id: int,
    key: str,
) -> DecideSpecification:
    position = domain.position(project_id)
    review = next(
        item for item in position.decisions if item.node_id == "specification.review"
    )
    candidate = next(
        item
        for item in review.fact_references
        if item.fact_type == "specification_candidate"
    )
    return DecideSpecification(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=review.decision_fingerprint,
        idempotency_key=key,
        actor="operator",
        specification_candidate_id=int(candidate.fact_id),
        candidate_fingerprint=candidate.fingerprint,
        decision="accepted",
    )


def test_structuring_is_unavailable_until_source_registration(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """An accepted Goal exposes registration but not structuring by itself."""
    repository = _repository(tmp_path, name="source-required")
    project_id, *_lineage = _seed_accepted_goal(
        engine,
        name="Source required",
        repository=repository,
    )

    position = _domain(engine).position(project_id)

    assert "specification.source.register" in position.available_nodes
    assert "specification.structure" not in position.available_nodes


def test_completion_contract_rejects_provider_owned_envelope_metadata() -> None:
    """The completion boundary accepts semantics, not host lifecycle metadata."""
    with pytest.raises(ValidationError):
        CompleteSpecificationStructuring.model_validate(
            {
                "kind": "complete_specification_structuring",
                "project_id": 1,
                "graph_version": GRAPH_VERSION,
                "fact_fingerprint": "facts",
                "decision_fingerprint": "decision",
                "idempotency_key": "complete",
                "actor": "worker",
                "attempt_id": 1,
                "attempt_fingerprint": "attempt",
                "payload": _payload().model_dump(mode="json"),
                "producer_version": "forged",
            }
        )


def test_registered_source_structures_and_accepts_exact_candidate_without_rewrite(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Host metadata binds the exact model payload and review preserves it."""
    project_id, vision_id, _vision_fp, goal_id, _goal_fp, _repo, probe = _ready_project(
        engine, tmp_path, name="exact-candidate"
    )
    domain = _domain(engine, repository_probe=probe)
    source_id = SPECIFICATION_VISION_SOURCE_ID
    result = _structure(
        engine,
        domain,
        project_id=project_id,
        payload=_payload(source_id=source_id),
        key="initial",
        repository_probe=probe,
    )

    assert result.ok
    with Session(engine) as session:
        candidate = session.exec(
            select(SpecificationCandidate).where(
                col(SpecificationCandidate.project_id) == project_id
            )
        ).one()
        before = candidate.canonical_envelope_json
        payload, envelope = load_candidate_contract(
            before,
            expected_candidate_fingerprint=candidate.candidate_fingerprint,
        )
        assert payload == _payload(source_id=source_id)
        assert envelope.accepted_vision_id == vision_id
        assert envelope.accepted_product_goal_id == goal_id
        assert envelope.workflow_node_attempt_id == candidate.workflow_node_attempt_id
        assert envelope.model_id == "fake/specification-structurer"
        assert envelope.producer_capability == "specification-structurer"
        attempt = session.get(WorkflowNodeAttempt, candidate.workflow_node_attempt_id)
        assert attempt is not None
        contract = SpecificationStructuringInput.model_validate_json(
            attempt.normalized_input_json
        )
        assert envelope.registered_source_fingerprint == (
            contract.registered_source.source_fingerprint
        )
        assert envelope.accepted_fact_fingerprint == (
            specification_structuring_fact_fingerprint(contract)
        )
        assert envelope.producer_input_fingerprint == (
            specification_structuring_input_fingerprint(contract)
        )

    review_domain = _domain(
        engine,
        at=NOW + timedelta(seconds=1),
        repository_probe=probe,
    )
    accept_request = _accept_request(
        review_domain,
        project_id=project_id,
        key="accept-initial",
    )
    accepted = review_domain.transition(accept_request)
    replay = review_domain.transition(accept_request)
    assert accepted.ok
    assert replay.ok
    assert replay.replayed
    with Session(engine) as session:
        candidate = session.exec(
            select(SpecificationCandidate).where(
                col(SpecificationCandidate.project_id) == project_id
            )
        ).one()
        registry = session.exec(
            select(SpecRegistry).where(col(SpecRegistry.project_id) == project_id)
        ).one()
        assert candidate.canonical_envelope_json == before
        assert registry.spec_hash == candidate.payload_fingerprint
        assert registry.source_specification_candidate_fingerprint == (
            candidate.candidate_fingerprint
        )


def test_payload_source_note_must_exist_in_registered_manifest(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """The structurer cannot cite prose outside the exact captured bundle."""
    project_id, *_lineage, probe = _ready_project(
        engine,
        tmp_path,
        name="unknown-source-note",
    )
    result = _structure(
        engine,
        _domain(engine, repository_probe=probe),
        project_id=project_id,
        payload=_payload(source_id="SRC.external.missing"),
        key="unknown-source",
        repository_probe=probe,
    )

    assert not result.ok
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.STALE_SPECIFICATION_INPUT
    with Session(engine) as session:
        assert not session.exec(select(SpecificationCandidate)).all()
        outcome = session.exec(
            select(WorkflowNodeAttemptOutcome).where(
                col(WorkflowNodeAttemptOutcome.project_id) == project_id,
                col(WorkflowNodeAttemptOutcome.status) == "failure",
            )
        ).one()
        assert outcome.failure_code == "STALE_SPECIFICATION_INPUT"


def test_rejected_revision_requires_successor_source_and_supersedes_candidate(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Rejection requires external source revision before another model call."""
    project_id, *_lineage, repository, probe = _ready_project(
        engine,
        tmp_path,
        name="source-revision",
    )
    first_domain = _domain(engine, repository_probe=probe)
    first = _structure(
        engine,
        first_domain,
        project_id=project_id,
        payload=_payload(),
        key="first",
        repository_probe=probe,
    )
    assert first.ok
    review_domain = _domain(
        engine,
        at=NOW + timedelta(seconds=1),
        repository_probe=probe,
    )
    rejected_request = _accept_request(
        review_domain,
        project_id=project_id,
        key="reject-first",
    ).model_copy(
        update={
            "decision": "rejected",
            "rationale": "Clarify the normative title in the source.",
        }
    )
    assert review_domain.transition(rejected_request).ok
    rejected_position = review_domain.position(project_id)
    assert "specification.source.register" in rejected_position.available_nodes
    assert "specification.structure" not in rejected_position.available_nodes
    source_decision = next(
        item
        for item in rejected_position.decisions
        if item.node_id == "specification.source.register"
    )
    forged_retry = source_decision.model_copy(
        update={
            "node_id": "specification.structure",
            "reason_code": "SPECIFICATION_FEEDBACK_RETRY_AVAILABLE",
        }
    )
    with pytest.raises(
        ValueError,
        match="prior candidate lineage or source is stale",
    ):
        SpecificationStructuringInputService(
            engine=engine,
            repository_probe=probe,
        ).build(
            project_id=project_id,
            decision=forged_retry,
        )

    (repository / "SPECIFICATION.md").write_text(
        "# Revised exact external Specification\n",
        encoding="utf-8",
    )
    with Repo(repository) as repo:
        repo.index.add(["SPECIFICATION.md"])
        repo.index.commit("revise exact source")
    _record_binding(
        engine,
        project_id=project_id,
        repository=repository,
        probe=probe,
    )
    replacement_domain = _domain(
        engine,
        at=NOW + timedelta(seconds=2),
        repository_probe=probe,
    )
    replacement = _register_source(
        engine,
        replacement_domain,
        project_id=project_id,
        repository_probe=probe,
        key="replacement-source",
    )
    assert replacement.ok
    structuring_domain = _domain(
        engine,
        at=NOW + timedelta(seconds=3),
        repository_probe=probe,
    )
    second = _structure(
        engine,
        structuring_domain,
        project_id=project_id,
        payload=_payload(item_id="REQ.persist-revised-candidate"),
        key="second",
        repository_probe=probe,
    )
    assert second.ok

    with Session(engine) as session:
        candidates = session.exec(
            select(SpecificationCandidate)
            .where(col(SpecificationCandidate.project_id) == project_id)
            .order_by(col(SpecificationCandidate.specification_candidate_id))
        ).all()
        assert len(candidates) == EXPECTED_REVISION_CANDIDATES
        assert candidates[1].candidate_kind == "initial"
        assert candidates[1].supersedes_specification_candidate_id == (
            candidates[0].specification_candidate_id
        )
        assert candidates[1].supersedes_candidate_fingerprint == (
            candidates[0].candidate_fingerprint
        )
        assert candidates[1].specification_source_id != (
            candidates[0].specification_source_id
        )


def test_feedback_retries_unchanged_source_with_exact_pending_lineage(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """An explicit retry reuses exact source/feedback and appends one candidate."""
    project_id, *_lineage, probe = _ready_project(
        engine,
        tmp_path,
        name="same-source-feedback-retry",
    )
    first_domain = _domain(engine, repository_probe=probe)
    first = _structure(
        engine,
        first_domain,
        project_id=project_id,
        payload=_payload(),
        key="same-source-first",
        repository_probe=probe,
    )
    assert first.ok
    review_domain = _domain(
        engine,
        at=NOW + timedelta(seconds=1),
        repository_probe=probe,
    )
    feedback_rationale = "Restore the exact negative-number diagnostic contract."
    feedback_request = _accept_request(
        review_domain,
        project_id=project_id,
        key="same-source-feedback",
    ).model_copy(
        update={
            "decision": "feedback",
            "rationale": feedback_rationale,
        }
    )
    assert review_domain.transition(feedback_request).ok

    retry_position = review_domain.position(project_id)
    assert {
        "specification.source.register",
        "specification.structure",
    }.issubset(retry_position.available_nodes)
    retry_decision = next(
        item
        for item in retry_position.decisions
        if item.node_id == "specification.structure"
    )
    assert retry_decision.reason_code == "SPECIFICATION_FEEDBACK_RETRY_AVAILABLE"
    retry_input = SpecificationStructuringInput.model_validate(
        SpecificationStructuringInputService(
            engine=engine,
            repository_probe=probe,
        ).build(
            project_id=project_id,
            decision=retry_decision,
        )
    )
    assert retry_input.operation == "revision"
    assert retry_input.prior_candidate is not None
    assert retry_input.prior_candidate.decision == "feedback"
    assert retry_input.prior_candidate.rationale == feedback_rationale

    with Session(engine) as session:
        source_before = session.exec(
            select(SpecificationSource).where(
                col(SpecificationSource.project_id) == project_id
            )
        ).one()
        candidate_before = session.exec(
            select(SpecificationCandidate).where(
                col(SpecificationCandidate.project_id) == project_id
            )
        ).one()
        decision_before = session.exec(
            select(SpecificationDecision).where(
                col(SpecificationDecision.project_id) == project_id
            )
        ).one()
        immutable_before = (
            source_before.specification_source_id,
            source_before.source_fingerprint,
            candidate_before.candidate_fingerprint,
            candidate_before.canonical_envelope_json,
            decision_before.decision,
            decision_before.rationale,
        )

    same_source = _register_source(
        engine,
        _domain(
            engine,
            at=NOW + timedelta(seconds=2),
            repository_probe=probe,
        ),
        project_id=project_id,
        repository_probe=probe,
        key="same-source-noop",
    )
    assert same_source.ok
    assert (
        same_source.output["created"],
        same_source.output["specification_source_id"],
        same_source.output["source_fingerprint"],
    ) == (False, immutable_before[0], immutable_before[1])

    second = _structure(
        engine,
        _domain(
            engine,
            at=NOW + timedelta(seconds=3),
            repository_probe=probe,
        ),
        project_id=project_id,
        payload=_payload(
            artifact_id="SPEC.same-source-retry",
            item_id="REQ.persist-same-source-retry",
        ),
        key="same-source-second",
        repository_probe=probe,
    )
    assert second.ok

    with Session(engine) as session:
        sources = session.exec(
            select(SpecificationSource).where(
                col(SpecificationSource.project_id) == project_id
            )
        ).all()
        candidates = session.exec(
            select(SpecificationCandidate)
            .where(col(SpecificationCandidate.project_id) == project_id)
            .order_by(col(SpecificationCandidate.specification_candidate_id))
        ).all()
        decisions = session.exec(
            select(SpecificationDecision).where(
                col(SpecificationDecision.project_id) == project_id
            )
        ).all()
        registries = session.exec(
            select(SpecRegistry).where(col(SpecRegistry.project_id) == project_id)
        ).all()

    assert len(sources) == 1
    assert len(candidates) == EXPECTED_REVISION_CANDIDATES
    assert len(decisions) == 1
    assert registries == []
    assert (
        sources[0].specification_source_id,
        sources[0].source_fingerprint,
        candidates[0].candidate_fingerprint,
        candidates[0].canonical_envelope_json,
        decisions[0].decision,
        decisions[0].rationale,
    ) == immutable_before
    assert (
        candidates[1].specification_source_id == candidates[0].specification_source_id
    )
    assert candidates[1].specification_source_fingerprint == (
        candidates[0].specification_source_fingerprint
    )
    assert candidates[1].supersedes_specification_candidate_id == (
        candidates[0].specification_candidate_id
    )
    assert candidates[1].supersedes_candidate_fingerprint == (
        candidates[0].candidate_fingerprint
    )
    assert candidates[1].workflow_node_attempt_id != (
        candidates[0].workflow_node_attempt_id
    )
    _payload_after, envelope_after = load_candidate_contract(
        candidates[1].canonical_envelope_json,
        expected_candidate_fingerprint=candidates[1].candidate_fingerprint,
    )
    assert envelope_after.prompt_version == SPECIFICATION_STRUCTURER_PROMPT_VERSION
    assert envelope_after.model_id == "fake/specification-structurer"
    assert envelope_after.model_configuration_fingerprint
    pending = _domain(
        engine,
        at=NOW + timedelta(seconds=4),
        repository_probe=probe,
    ).position(project_id)
    assert "specification.review" in pending.waiting_nodes
    assert "specification.structure" not in pending.available_nodes


def test_acceptance_rejects_tampered_candidate_bytes(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """The review handler fails closed before registering changed bytes."""
    project_id, *_lineage, probe = _ready_project(
        engine,
        tmp_path,
        name="tampered-candidate",
    )
    domain = _domain(engine, repository_probe=probe)
    authored = _structure(
        engine,
        domain,
        project_id=project_id,
        payload=_payload(),
        key="tamper",
        repository_probe=probe,
    )
    assert authored.ok
    position = domain.position(project_id)
    review = next(
        item for item in position.decisions if item.node_id == "specification.review"
    )
    reference = next(
        item
        for item in review.fact_references
        if item.fact_type == "specification_candidate"
    )
    request = DecideSpecification(
        project_id=project_id,
        graph_version=position.graph_version,
        fact_fingerprint=position.fact_fingerprint,
        decision_fingerprint=review.decision_fingerprint,
        idempotency_key="accept-tampered",
        actor="operator",
        specification_candidate_id=int(reference.fact_id),
        candidate_fingerprint=reference.fingerprint,
        decision="accepted",
    )
    with Session(engine) as session:
        candidate = session.exec(
            select(SpecificationCandidate).where(
                col(SpecificationCandidate.project_id) == project_id
            )
        ).one()
        candidate.canonical_envelope_json = "{}"
        session.add(candidate)
        session.commit()
    with Session(engine) as session:
        accepted = execute_decide_specification(
            session,
            request,
            review,
            NOW + timedelta(seconds=1),
        )
    assert not accepted.ok
    assert accepted.error is not None
    assert accepted.error.code == "SPECIFICATION_CANDIDATE_CONFLICT"
    with Session(engine) as session:
        assert not session.exec(select(SpecRegistry)).all()
        assert not session.exec(select(SpecificationDecision)).all()


def test_acceptance_rejects_caller_owned_source_fingerprint(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Only the host may derive acceptance-time source identity."""
    project_id, *_lineage, probe = _ready_project(
        engine,
        tmp_path,
        name="host-owned-source",
    )
    domain = _domain(engine, repository_probe=probe)
    assert _structure(
        engine,
        domain,
        project_id=project_id,
        payload=_payload(),
        key="host-owned",
        repository_probe=probe,
    ).ok
    request = _accept_request(
        domain,
        project_id=project_id,
        key="accept-host-owned-source",
    ).model_copy(update={"repository_source_fingerprint": "sha256:" + ("f" * 64)})

    result = domain.transition(request)

    assert not result.ok
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.STALE_SPECIFICATION_INPUT
