"""Host-prepared Vision input and replay boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import pytest
from git import Repo
from sqlmodel import Session, select

from adapters.git.repository_probe import GitPythonRepositoryProbe
from models.product_definition import (
    VisionEvidenceSnapshot,
    VisionInterviewTurn,
    VisionRevisionIntent,
)
from repositories.workflow import select_vision_input
from services.contracts.vision import (
    VisionAgentInput,
    VisionClarificationInput,
    VisionRevisionInput,
)
from services.contracts.vision_evidence import VisionEvidenceBundle
from services.node_attempt_replay import NodeAttemptReplayQuery
from services.vision_evidence import (
    VisionEvidenceCollectionError,
    VisionEvidenceErrorCode,
)
from services.vision_input import VisionInputService
from tests.services.test_vision_evidence import _add_project, _bind_repository
from tests.workflow.test_vision_interview_transitions import (
    COMPONENTS,
    _basis,
    _decision,
    _domain,
    _question,
    _record,
    _RecordRequest,
    _review_vision,
    _start,
    _VisionReview,
)
from workflow.contracts import WorkflowErrorCode
from workflow.requests import BeginVisionRevision, GenerateVisionBootstrap

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from services.repository_probe import RepositoryProbeResult
    from workflow.contracts import JsonObject


MUTATING_PROBE_AFTER_READ_CALL = 2


@dataclass
class _MutatingProbe:
    """Mutate a repository just before the after-read probe."""

    repository: Path
    calls: int = 0

    def inspect(self, path: Path | str) -> RepositoryProbeResult:
        self.calls += 1
        if self.calls == MUTATING_PROBE_AFTER_READ_CALL:
            (self.repository / "README.md").write_text("changed\n", encoding="utf-8")
        return GitPythonRepositoryProbe().inspect(path)


def _service(engine: Engine) -> VisionInputService:
    return VisionInputService(
        engine=engine,
        repository_probe=GitPythonRepositoryProbe(),
    )


def _repository(tmp_path: Path, *, readme: str = "initial\n") -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    with Repo.init(root) as repo:
        with repo.config_writer() as config:
            config.set_value("user", "name", "Vision Input Test")
            config.set_value("user", "email", "vision-input@example.com")
        (root / "README.md").write_text(readme, encoding="utf-8")
        repo.index.add(["README.md"])
        repo.index.commit("initial evidence")
    return root


def _commit(repository: Path, *, readme: str, message: str) -> None:
    with Repo(repository) as repo:
        (repository / "README.md").write_text(readme, encoding="utf-8")
        repo.index.add(["README.md"])
        repo.index.commit(message)


def _bootstrap_with_evidence(
    engine: Engine,
    project_id: int,
    evidence: JsonObject,
    *,
    complete: bool = False,
) -> None:
    domain = _domain(engine)
    bundle = VisionEvidenceBundle.model_validate(evidence)
    start, attempt = _start(domain, project_id, "service-bootstrap")
    attempt_id = attempt.output["attempt_id"]
    attempt_fingerprint = attempt.output["attempt_fingerprint"]
    assert isinstance(attempt_id, int)
    assert isinstance(attempt_fingerprint, str)
    result = domain.transition(
        GenerateVisionBootstrap(
            project_id=project_id,
            graph_version=start.graph_version,
            fact_fingerprint=start.fact_fingerprint,
            decision_fingerprint=start.decision_fingerprint,
            idempotency_key="service-bootstrap-record",
            actor="operator@example.com",
            operation="bootstrap",
            evidence=evidence,
            evidence_fingerprint=bundle.evidence_fingerprint,
            evidence_warnings=tuple(
                warning.model_dump(mode="json") for warning in bundle.warnings
            ),
            repository_binding_id=None,
            updated_components=COMPONENTS,
            project_vision_statement="A trusted workflow tool.",
            is_complete=complete,
            clarifying_questions=() if complete else (_question(),),
            component_basis=_basis(COMPONENTS),
            assumptions=(),
            conflicts=(),
            attempt_id=attempt_id,
            attempt_fingerprint=attempt_fingerprint,
        )
    )
    assert result.ok


def test_vision_input_selection_has_no_interview_legacy_name() -> None:
    """The grounded selector must not retain the removed interview API name."""
    assert select_vision_input.__name__ == "select_vision_input"


def test_project_only_bootstrap_requires_no_human_response(engine: Engine) -> None:
    """Build bootstrap input from Project facts without a human response."""
    project_id = _add_project(engine, name="Project-only Vision")
    domain = _domain(engine)
    payload = _service(engine).build_bootstrap(
        project_id=project_id,
        decision=_decision(domain, project_id, "vision.bootstrap"),
    )

    request = VisionAgentInput.model_validate(payload).request

    assert request.operation == "bootstrap"
    assert "human_response" not in request.model_dump(mode="json")
    assert request.evidence.items[0].evidence_id == "project:metadata"


def test_repository_bootstrap_includes_bounded_repository_evidence(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Include deterministic bounded evidence when a repository is attached."""
    repository = _repository(tmp_path, readme="# Repository Vision\n")
    project_id = _add_project(engine, name="Repository Vision")
    _bind_repository(engine, project_id=project_id, repository=repository)
    domain = _domain(engine)

    payload = _service(engine).build_bootstrap(
        project_id=project_id,
        decision=_decision(domain, project_id, "vision.bootstrap"),
    )

    request = VisionAgentInput.model_validate(payload).request
    kinds = {item.kind for item in request.evidence.items}
    assert {"project_metadata", "repository_provenance", "readme"} <= kinds
    assert str(tmp_path) not in str(payload)


def test_revision_bootstrap_uses_revision_contract(engine: Engine) -> None:
    """Build a revision request from accepted Vision and an open intent."""
    project_id = _add_project(engine, name="Revision Vision")
    domain = _domain(engine)
    start, attempt = _start(domain, project_id, "revision-input-seed")
    initial = _record(
        engine,
        domain,
        start,
        attempt,
        request=_RecordRequest(complete=True, key="revision-input-record"),
    )
    artifact_id = initial.output["vision_artifact_id"]
    fingerprint = initial.output["vision_fingerprint"]
    assert isinstance(artifact_id, int)
    assert isinstance(fingerprint, str)
    assert _review_vision(
        domain,
        project_id,
        _VisionReview(
            artifact_id=artifact_id,
            fingerprint=fingerprint,
            decision="accepted",
            rationale="Accept before revision.",
            idempotency_key="revision-input-accept",
        ),
    ).ok
    position = domain.position(project_id)
    revision = _decision(domain, project_id, "vision.revision.start")
    assert domain.transition(
        BeginVisionRevision(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=revision.decision_fingerprint,
            idempotency_key="revision-input-open",
            actor="operator@example.com",
            source_vision_artifact_id=artifact_id,
            source_vision_fingerprint=fingerprint,
            reason="Revise the market direction.",
        )
    ).ok

    payload = _service(engine).build_bootstrap(
        project_id=project_id,
        decision=_decision(domain, project_id, "vision.bootstrap"),
    )

    request = VisionAgentInput.model_validate(payload).request
    assert isinstance(request, VisionRevisionInput)
    assert request.operation == "revision"
    assert request.revision_reason == "Revise the market direction."
    with Session(engine) as session:
        for turn in session.exec(
            select(VisionInterviewTurn).where(
                VisionInterviewTurn.operation == "revision"
            )
        ).all():
            turn.revision_intent_id = None
            session.add(turn)
        for intent in session.exec(select(VisionRevisionIntent)).all():
            session.delete(intent)
        session.commit()


def test_clarification_binds_host_derived_question_ids(engine: Engine) -> None:
    """Derive clarification question IDs from the active persisted draft."""
    project_id = _add_project(engine, name="Clarification Vision")
    domain = _domain(engine)
    start, attempt = _start(domain, project_id, "clarification-input-seed")
    assert _record(
        engine,
        domain,
        start,
        attempt,
        request=_RecordRequest(complete=False, key="clarification-input-record"),
    ).ok

    payload = _service(engine).build_clarification(
        project_id=project_id,
        decision=_decision(domain, project_id, "vision.interview"),
        user_text="Operators use it.",
    )

    parsed = VisionAgentInput.model_validate(payload)
    assert isinstance(parsed.request, VisionClarificationInput)
    assert parsed.request.addressed_question_ids == ("question:target-user",)
    assert parsed.request.human_response == "Operators use it."


def test_feedback_clarification_allows_empty_addressed_question_ids(
    engine: Engine,
) -> None:
    """Permit human review feedback when no clarifying question remains."""
    project_id = _add_project(engine, name="Feedback Vision")
    domain = _domain(engine)
    start, attempt = _start(domain, project_id, "feedback-input-seed")
    complete = _record(
        engine,
        domain,
        start,
        attempt,
        request=_RecordRequest(complete=True, key="feedback-input-record"),
    )
    artifact_id = complete.output["vision_artifact_id"]
    fingerprint = complete.output["vision_fingerprint"]
    assert isinstance(artifact_id, int)
    assert isinstance(fingerprint, str)
    assert _review_vision(
        domain,
        project_id,
        _VisionReview(
            artifact_id=artifact_id,
            fingerprint=fingerprint,
            decision="feedback",
            rationale="Clarify positioning.",
            idempotency_key="feedback-input-review",
        ),
    ).ok

    payload = _service(engine).build_clarification(
        project_id=project_id,
        decision=_decision(domain, project_id, "vision.interview"),
        user_text="Make the positioning narrower.",
    )

    request = VisionAgentInput.model_validate(payload).request
    assert isinstance(request, VisionClarificationInput)
    assert request.addressed_question_ids == ()


@pytest.mark.parametrize("review_decision", ["feedback", "rejected"])
def test_revised_review_clarification_uses_revised_source_lineage(
    engine: Engine,
    review_decision: Literal["feedback", "rejected"],
) -> None:
    """Bind revised feedback and rejection to the revised turn and snapshot."""
    project_id = _add_project(engine, name=f"Revised {review_decision} Vision")
    domain = _domain(engine)
    initial_start, initial_attempt = _start(domain, project_id, "review-initial")
    initial = _record(
        engine,
        domain,
        initial_start,
        initial_attempt,
        request=_RecordRequest(complete=True, key="review-initial-record"),
    )
    initial_artifact_id = initial.output["vision_artifact_id"]
    initial_fingerprint = initial.output["vision_fingerprint"]
    assert isinstance(initial_artifact_id, int)
    assert isinstance(initial_fingerprint, str)
    accepted = _review_vision(
        domain,
        project_id,
        _VisionReview(
            artifact_id=initial_artifact_id,
            fingerprint=initial_fingerprint,
            decision="accepted",
            rationale="Accept before revision.",
            idempotency_key="review-initial-accept",
        ),
    )
    assert accepted.ok
    revision = _decision(domain, project_id, "vision.revision.start")
    position = domain.position(project_id)
    opened = domain.transition(
        BeginVisionRevision(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=revision.decision_fingerprint,
            idempotency_key="review-revision-open",
            actor="operator@example.com",
            source_vision_artifact_id=initial_artifact_id,
            source_vision_fingerprint=initial_fingerprint,
            reason="Change the differentiator.",
        )
    )
    assert opened.ok
    revised_components = COMPONENTS | {"differentiator": "Revised differentiator"}
    revision_start, revision_attempt = _start(
        domain,
        project_id,
        "review-revision",
        operation="revision",
    )
    revised = _record(
        engine,
        domain,
        revision_start,
        revision_attempt,
        request=_RecordRequest(
            complete=True,
            key="review-revision-record",
            operation="revision",
            components=revised_components,
            statement="A revised Vision statement.",
        ),
    )
    revised_artifact_id = revised.output["vision_artifact_id"]
    revised_fingerprint = revised.output["vision_fingerprint"]
    assert isinstance(revised_artifact_id, int)
    assert isinstance(revised_fingerprint, str)
    reviewed = _review_vision(
        domain,
        project_id,
        _VisionReview(
            artifact_id=revised_artifact_id,
            fingerprint=revised_fingerprint,
            decision=review_decision,
            rationale="Refine the revised candidate.",
            idempotency_key=f"review-revised-{review_decision}",
        ),
    )
    assert reviewed.ok

    payload = _service(engine).build_clarification(
        project_id=project_id,
        decision=_decision(domain, project_id, "vision.interview"),
        user_text="Refine this revised direction.",
    )
    request = VisionAgentInput.model_validate(payload).request
    assert isinstance(request, VisionClarificationInput)
    with Session(engine) as session:
        turns = session.exec(
            select(VisionInterviewTurn).where(
                VisionInterviewTurn.project_id == project_id
            )
        ).all()
        initial_turn = next(turn for turn in turns if turn.operation == "bootstrap")
        revised_turn = next(turn for turn in turns if turn.operation == "revision")
        revised_snapshot_id = revised_turn.vision_evidence_snapshot_id
        initial_snapshot_id = initial_turn.vision_evidence_snapshot_id
        for turn in turns:
            if turn.revision_intent_id is not None:
                turn.revision_intent_id = None
                session.add(turn)
        session.flush()
        for intent in session.exec(select(VisionRevisionIntent)).all():
            session.delete(intent)
        session.commit()

    assert request.vision_evidence_snapshot_id == revised_snapshot_id
    assert request.vision_evidence_snapshot_id != initial_snapshot_id
    assert request.current_components.differentiator == "Revised differentiator"
    assert request.current_statement == "A revised Vision statement."
    assert request.current_questions == ()


def test_clarification_reuses_snapshot_and_observes_current_evidence(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Keep stored draft evidence while recording fresh preflight evidence."""
    repository = _repository(tmp_path)
    project_id = _add_project(engine, name="Snapshot Reuse Vision")
    _bind_repository(engine, project_id=project_id, repository=repository)
    domain = _domain(engine)
    bootstrap_payload = _service(engine).build_bootstrap(
        project_id=project_id,
        decision=_decision(domain, project_id, "vision.bootstrap"),
    )
    bootstrap_input = VisionAgentInput.model_validate(bootstrap_payload)
    _bootstrap_with_evidence(
        engine,
        project_id,
        bootstrap_input.request.evidence.model_dump(mode="json"),
    )

    payload = _service(engine).build_clarification(
        project_id=project_id,
        decision=_decision(domain, project_id, "vision.interview"),
        user_text="Operators use it.",
    )

    parsed = VisionAgentInput.model_validate(payload)
    assert parsed.preflight is not None
    assert parsed.preflight.expected_evidence_fingerprint == (
        parsed.request.evidence.evidence_fingerprint
    )
    assert parsed.preflight.observed_evidence.evidence_fingerprint == (
        parsed.request.evidence.evidence_fingerprint
    )


def test_same_key_replays_and_different_input_conflicts(engine: Engine) -> None:
    """Replay identical requests and reject changed input for one key."""
    project_id = _add_project(engine, name="Replay Vision")
    domain = _domain(engine)
    start, attempt = _start(domain, project_id, "replay-input")
    assert _record(
        engine,
        domain,
        start,
        attempt,
        request=_RecordRequest(complete=True, key="replay-input-record"),
    ).ok
    service = _service(engine)

    replay = service.replay(
        NodeAttemptReplayQuery(
            project_id=project_id,
            graph_version=start.graph_version,
            fact_fingerprint=start.fact_fingerprint,
            decision_fingerprint=start.decision_fingerprint,
            node_id="vision.bootstrap",
            idempotency_key=start.idempotency_key,
            actor=start.actor,
            user_text=None,
        )
    )
    conflict = service.replay(
        NodeAttemptReplayQuery(
            project_id=project_id,
            graph_version=start.graph_version,
            fact_fingerprint=start.fact_fingerprint,
            decision_fingerprint=start.decision_fingerprint,
            node_id="vision.bootstrap",
            idempotency_key=start.idempotency_key,
            actor=start.actor,
            user_text="different",
        )
    )

    assert replay is not None
    assert replay.replayed
    assert conflict is not None
    assert conflict.error is not None
    assert conflict.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT


def test_stale_binding_prevents_bootstrap_input(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Block bootstrap collection when repository provenance has drifted."""
    repository = _repository(tmp_path)
    project_id = _add_project(engine, name="Stale Binding Vision")
    _bind_repository(engine, project_id=project_id, repository=repository)
    (repository / "README.md").write_text("dirty\n", encoding="utf-8")
    domain = _domain(engine)

    with pytest.raises(VisionEvidenceCollectionError) as caught:
        _service(engine).build_bootstrap(
            project_id=project_id,
            decision=_decision(domain, project_id, "vision.bootstrap"),
        )

    assert caught.value.code is VisionEvidenceErrorCode.REPOSITORY_PROVENANCE_STALE


def test_file_mutation_during_collection_prevents_input(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Block bootstrap input when a selected file changes during collection."""
    repository = _repository(tmp_path)
    project_id = _add_project(engine, name="Mutation Vision")
    _bind_repository(engine, project_id=project_id, repository=repository)
    domain = _domain(engine)
    service = VisionInputService(
        engine=engine,
        repository_probe=_MutatingProbe(repository=repository),
    )

    with pytest.raises(VisionEvidenceCollectionError) as caught:
        service.build_bootstrap(
            project_id=project_id,
            decision=_decision(domain, project_id, "vision.bootstrap"),
        )

    assert (
        caught.value.code
        is VisionEvidenceErrorCode.REPOSITORY_CHANGED_DURING_EVIDENCE_COLLECTION
    )


def test_unchanged_after_refresh_proceeds(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Allow clarification after a repository refresh with unchanged evidence."""
    repository = _repository(tmp_path)
    project_id = _add_project(engine, name="Unchanged Refresh Vision")
    _bind_repository(engine, project_id=project_id, repository=repository)
    domain = _domain(engine)
    bootstrap_payload = _service(engine).build_bootstrap(
        project_id=project_id,
        decision=_decision(domain, project_id, "vision.bootstrap"),
    )
    bootstrap_input = VisionAgentInput.model_validate(bootstrap_payload)
    _bootstrap_with_evidence(
        engine,
        project_id,
        bootstrap_input.request.evidence.model_dump(mode="json"),
    )
    _bind_repository(engine, project_id=project_id, repository=repository)

    payload = _service(engine).build_clarification(
        project_id=project_id,
        decision=_decision(domain, project_id, "vision.interview"),
        user_text="Still true.",
    )

    parsed = VisionAgentInput.model_validate(payload)
    assert parsed.preflight is not None
    assert parsed.preflight.expected_evidence_fingerprint == (
        parsed.preflight.observed_evidence.evidence_fingerprint
    )


def test_changed_evidence_after_refresh_is_marked_stale(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Mark clarification preflight stale after refreshed evidence changes."""
    repository = _repository(tmp_path, readme="old\n")
    project_id = _add_project(engine, name="Changed Refresh Vision")
    _bind_repository(engine, project_id=project_id, repository=repository)
    domain = _domain(engine)
    bootstrap_payload = _service(engine).build_bootstrap(
        project_id=project_id,
        decision=_decision(domain, project_id, "vision.bootstrap"),
    )
    bootstrap_input = VisionAgentInput.model_validate(bootstrap_payload)
    _bootstrap_with_evidence(
        engine,
        project_id,
        bootstrap_input.request.evidence.model_dump(mode="json"),
    )
    _commit(repository, readme="new\n", message="changed evidence")
    _bind_repository(engine, project_id=project_id, repository=repository)

    payload = _service(engine).build_clarification(
        project_id=project_id,
        decision=_decision(domain, project_id, "vision.interview"),
        user_text="Still true.",
    )

    parsed = VisionAgentInput.model_validate(payload)
    assert parsed.preflight is not None
    assert parsed.preflight.expected_evidence_fingerprint != (
        parsed.preflight.observed_evidence.evidence_fingerprint
    )
    with Session(engine) as session:
        assert len(session.exec(select(VisionEvidenceSnapshot)).all()) == 1
