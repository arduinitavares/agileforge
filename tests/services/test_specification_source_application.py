"""Application-boundary tests for host-owned Specification source capture."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Never

import pytest
from sqlmodel import Session, SQLModel, create_engine

import services.specification_source_registration as registration_module
from adapters.git.repository_probe import GitPythonRepositoryProbe
from models.core import Project
from services.application import AgileForgeApplication, RepositoryRefreshRequest
from services.contracts.specification_source import (
    SPECIFICATION_SOURCE_PRIMARY_ID,
    SpecificationContextCapture,
    SpecificationRepositoryRevision,
    SpecificationSourceBundle,
    SpecificationSourceDocument,
    source_bundle_fingerprint,
)
from services.project_lifecycle import ProjectLifecycleService
from services.read_projections import DurableReadProjectionService
from services.specification_source_registration import (
    PreparedSpecificationSourceRegistration,
    SpecificationSourceCapturePreview,
    SpecificationSourceRegistrationError,
    SpecificationSourceRegistrationRequest,
    SpecificationSourceRegistrationService,
)
from tests.workflow.test_product_discovery_transitions import (
    NOW as DISCOVERY_NOW,
)
from tests.workflow.test_product_discovery_transitions import (
    _accept_request,
    _domain,
    _payload,
    _ready_project,
    _register_source,
    _structure,
)
from tests.workflow.test_specification_rebinding import _accepted_rows
from workflow.clock import FixedClock
from workflow.contracts import (
    GRAPH_VERSION,
    FactReference,
    JsonObject,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
    TransitionResult,
    WorkflowErrorCode,
    WorkflowPosition,
)
from workflow.definitions.product_discovery import SPECIFICATION_NODES
from workflow.domain import WorkflowDomain
from workflow.graph import ChildGraphSpec, WorkflowGraph

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from services.node_attempt_replay import TransitionReplayQuery
    from services.repository_probe import RepositoryProbeResult
    from workflow.requests import RegisterSpecificationSource, TransitionRequest

NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)
PROJECT_ID = 7
REPOSITORY_BINDING_ID = 17
_EXPECTED_FINAL_VERIFICATION_PROBES = 4
_EXPECTED_STALE_REPLACEMENT_PROBES = 2


@pytest.fixture
def source_recovery_engine(tmp_path: Path) -> Iterator[Engine]:
    """Separate connections keep live final verification inside a real transaction."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'source-recovery.db'}",
        connect_args={"check_same_thread": False},
    )
    try:
        SQLModel.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()


class _FixedCountingProbe:
    """Inspect the disposable repository with a coherent fixture timestamp."""

    def __init__(self) -> None:
        self.calls = 0
        self.inspected_at = DISCOVERY_NOW + timedelta(seconds=2)

    def inspect(self, path: Path | str) -> RepositoryProbeResult:
        self.calls += 1
        return (
            GitPythonRepositoryProbe()
            .inspect(path)
            .model_copy(update={"inspected_at": self.inspected_at})
        )


@dataclass
class _SourceApplicationFixture:
    application: AgileForgeApplication
    domain: WorkflowDomain
    registration: SpecificationSourceRegistrationService
    probe: _FixedCountingProbe
    project_id: int
    repository: Path
    binding_id: int


def _active_binding_id(engine: Engine, project_id: int) -> int:
    with Session(engine) as session:
        project = session.get(Project, project_id)
        assert project is not None
        assert project.active_repository_binding_id is not None
        return project.active_repository_binding_id


def _registered_source_fixture(
    engine: Engine, tmp_path: Path
) -> _SourceApplicationFixture:
    """Keep registration, provenance, workflow checks, and durable reads real."""
    project_id, *_lineage, repository, _probe = _ready_project(
        engine, tmp_path, name="source-recovery"
    )
    probe = _FixedCountingProbe()
    registration = SpecificationSourceRegistrationService(
        engine=engine, repository_probe=probe
    )
    domain = WorkflowDomain(
        engine=engine,
        graph=WorkflowGraph(
            graph_version=GRAPH_VERSION,
            root=ChildGraphSpec(
                child_graph_id="specification", nodes=SPECIFICATION_NODES
            ),
        ),
        clock=FixedClock(now_value=DISCOVERY_NOW + timedelta(seconds=3)),
        specification_registration_check=registration.verify_prepared,
    )
    application = AgileForgeApplication(
        workflow_domain=domain,
        specification_source_registration=registration,
        read_projection=DurableReadProjectionService(engine=engine),
    )
    application.set_project_lifecycle(
        ProjectLifecycleService(
            engine=engine, workflow_domain=domain, repository_probe=probe
        )
    )
    return _SourceApplicationFixture(
        application,
        domain,
        registration,
        probe,
        project_id,
        repository,
        _active_binding_id(engine, project_id),
    )


def _accepted_replacement_fixture(
    engine: Engine, tmp_path: Path
) -> _SourceApplicationFixture:
    """Retain accepted evidence and register a current replacement source."""
    fixture = _registered_source_fixture(engine, tmp_path)
    structured = _structure(
        engine,
        _domain(engine),
        project_id=fixture.project_id,
        payload=_payload(),
        key="accepted-recovery",
    )
    assert structured.ok, structured.error
    review_domain = _domain(engine, at=DISCOVERY_NOW + timedelta(seconds=1))
    accepted = review_domain.transition(
        _accept_request(
            review_domain, project_id=fixture.project_id, key="accept-recovery"
        )
    )
    assert accepted.ok, accepted.error
    refreshed = fixture.application.refresh_repository(
        RepositoryRefreshRequest(
            project_id=fixture.project_id,
            idempotency_key="replacement-binding",
            actor="operator",
        )
    )
    assert refreshed.ok, refreshed.error
    registered = _register_source(
        engine,
        fixture.domain,
        project_id=fixture.project_id,
        repository_probe=fixture.probe,
        key="replacement-source",
    )
    assert registered.ok, registered.error
    fixture.binding_id = _active_binding_id(engine, fixture.project_id)
    fixture.probe.inspected_at += timedelta(seconds=1)
    fixture.probe.calls = 0
    return fixture


def _expected_stale_recovery(binding_id: int) -> dict[str, object]:
    return {
        "reason_code": "REPOSITORY_PROVENANCE_STALE",
        "cause": "WORKTREE_CHANGED",
        "recorded_binding_id": binding_id,
        "recorded_dirty": False,
        "observed_dirty": True,
        "changed_fields": ["working_tree_status"],
        "action": "refresh_repository_binding",
    }


def _source_request(
    fixture: _SourceApplicationFixture,
) -> SpecificationSourceRegistrationRequest:
    return SpecificationSourceRegistrationRequest(
        project_id=fixture.project_id,
        source_path="SPECIFICATION.md",
        preparation_capability="grill-with-docs",
        idempotency_key="retry-source",
        actor="operator",
    )


def _block_source_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_capture(**_kwargs: object) -> Never:
        pytest.fail("stale preflight unexpectedly captured source bytes")  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(
        registration_module, "_capture_selected_documents", forbidden_capture
    )


def _bundle() -> SpecificationSourceBundle:
    content = b"# Prepared source\n"
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    return SpecificationSourceBundle(
        source=SpecificationSourceDocument(
            source_id=SPECIFICATION_SOURCE_PRIMARY_ID,
            relative_path="specification.md",
            content_base64=base64.b64encode(content).decode("ascii"),
            byte_length=len(content),
            content_fingerprint=digest,
        ),
        context=SpecificationContextCapture(state="absent"),
        repository_revision=SpecificationRepositoryRevision(
            head_sha="a" * 40,
            dirty=False,
            status_fingerprint="sha256:" + "b" * 64,
        ),
        accepted_vision_fingerprint="sha256:" + "c" * 64,
        accepted_product_goal_fingerprint="sha256:" + "d" * 64,
    )


class _RegistrationService:
    def __init__(self, prepared: PreparedSpecificationSourceRegistration) -> None:
        self.prepared = prepared
        self.requests: list[SpecificationSourceRegistrationRequest] = []

    def prepare(
        self,
        request: SpecificationSourceRegistrationRequest,
    ) -> PreparedSpecificationSourceRegistration:
        self.requests.append(request)
        return self.prepared

    def preview(
        self,
        request: SpecificationSourceRegistrationRequest,
    ) -> SpecificationSourceCapturePreview:
        self.requests.append(request)
        return SpecificationSourceCapturePreview.from_prepared(self.prepared)


class _Domain:
    def __init__(self, position: WorkflowPosition) -> None:
        self.current_position = position
        self.position_calls = 0
        self.requests: list[RegisterSpecificationSource] = []

    def position(self, project_id: int) -> WorkflowPosition:
        assert project_id == PROJECT_ID
        self.position_calls += 1
        return self.current_position

    def transition(self, request: TransitionRequest) -> TransitionResult:
        from workflow.requests import RegisterSpecificationSource  # noqa: PLC0415

        assert isinstance(request, RegisterSpecificationSource)
        self.requests.append(request)
        return TransitionResult(ok=True, applied_node_id=request.node_id)

    def load_persisted_attempt_input(
        self,
        *,
        project_id: int,
        attempt_id: int,
        attempt_fingerprint: str,
    ) -> JsonObject:
        del project_id, attempt_id, attempt_fingerprint
        message = "Source registration never loads agent input."
        raise AssertionError(message)


class _Replay:
    def __init__(self, result: TransitionResult | None = None) -> None:
        self.result = result
        self.queries: list[TransitionReplayQuery] = []

    def replay(self, query: TransitionReplayQuery) -> TransitionResult | None:
        self.queries.append(query)
        return self.result


def test_application_prepares_exact_bundle_then_submits_host_only_command() -> None:
    """Callers select paths; host capture supplies every durable identity and byte."""
    bundle = _bundle()
    prepared = PreparedSpecificationSourceRegistration(
        project_id=PROJECT_ID,
        accepted_vision_artifact_id=11,
        accepted_product_goal_artifact_id=13,
        repository_binding_id=REPOSITORY_BINDING_ID,
        repository_binding_fingerprint="sha256:" + "e" * 64,
        request_fingerprint="sha256:" + "f" * 64,
        source_fingerprint=source_bundle_fingerprint(bundle),
        bundle=bundle,
    )
    decision = NodeDecision(
        node_id="specification.source.register",
        child_graph_id="specification",
        request_kind="register_specification_source",
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="SPECIFICATION_SOURCE_REQUIRED",
        fact_references=(
            FactReference(fact_type="vision", fact_id="11", fingerprint="v"),
            FactReference(fact_type="product_goal", fact_id="13", fingerprint="g"),
        ),
        decision_fingerprint="sha256:decision",
    )
    position = WorkflowPosition(
        project_id=PROJECT_ID,
        graph_version="agileforge.workflow.v2",
        fact_fingerprint="sha256:facts",
        evaluated_at=NOW,
        available_nodes=("specification.source.register",),
        waiting_nodes=(),
        blocked_nodes=(),
        invalid_nodes=(),
        terminal=False,
        decisions=(decision,),
    )
    domain = _Domain(position)
    registration = _RegistrationService(prepared)
    replay = _Replay()
    application = AgileForgeApplication(
        workflow_domain=domain,
        specification_source_registration=registration,
        specification_source_replay=replay,
    )
    semantic_request = SpecificationSourceRegistrationRequest(
        project_id=PROJECT_ID,
        source_path="specification.md",
        preparation_capability="grill-with-docs",
        adr_paths=("docs/adr/0002.md", "docs/adr/0001.md"),
        idempotency_key="register-source",
        actor="operator@example.test",
        correlation_id="source-correlation",
    )

    result = application.register_specification_source(semantic_request)

    assert result.ok is True
    assert registration.requests == [semantic_request]
    assert set(semantic_request.model_dump(mode="json")) == {
        "project_id",
        "source_path",
        "preparation_capability",
        "adr_paths",
        "idempotency_key",
        "actor",
        "correlation_id",
    }
    assert len(domain.requests) == 1
    command = domain.requests[0]
    assert command.bundle == bundle
    assert command.source_fingerprint == source_bundle_fingerprint(bundle)
    assert command.repository_binding_id == REPOSITORY_BINDING_ID
    assert command.capture_request_fingerprint == prepared.request_fingerprint
    assert replay.queries[0].operator_input == {
        "capture_request_fingerprint": semantic_request.semantic_fingerprint()
    }


def test_application_rejects_stale_source_choice_before_capture() -> None:
    """A changed rendered choice cannot capture or register against new state."""
    bundle = _bundle()
    prepared = PreparedSpecificationSourceRegistration(
        project_id=PROJECT_ID,
        accepted_vision_artifact_id=11,
        accepted_product_goal_artifact_id=13,
        repository_binding_id=REPOSITORY_BINDING_ID,
        repository_binding_fingerprint="sha256:" + "e" * 64,
        request_fingerprint="sha256:" + "f" * 64,
        source_fingerprint=source_bundle_fingerprint(bundle),
        bundle=bundle,
    )
    decision = NodeDecision(
        node_id="specification.source.register",
        child_graph_id="specification",
        request_kind="register_specification_source",
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.OPTIONAL_REENTRY,
        reason_code="SPECIFICATION_FEEDBACK_SOURCE_REVISION_AVAILABLE",
        decision_fingerprint="sha256:current-source-choice",
    )
    position = WorkflowPosition(
        project_id=PROJECT_ID,
        graph_version="agileforge.workflow.v2",
        fact_fingerprint="sha256:facts",
        evaluated_at=NOW,
        available_nodes=(decision.node_id,),
        waiting_nodes=(),
        blocked_nodes=(),
        invalid_nodes=(),
        terminal=False,
        decisions=(decision,),
    )
    domain = _Domain(position)
    registration = _RegistrationService(prepared)
    application = AgileForgeApplication(
        workflow_domain=domain,
        specification_source_registration=registration,
        specification_source_replay=_Replay(),
    )
    request = SpecificationSourceRegistrationRequest(
        project_id=PROJECT_ID,
        source_path="specification.md",
        preparation_capability="grill-with-docs",
        expected_decision_fingerprint="sha256:stale-source-choice",
        idempotency_key="stale-source-choice",
        actor="operator@example.test",
    )

    result = application.register_specification_source(request)

    assert not result.ok
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.STALE_POSITION
    assert registration.requests == []
    assert domain.requests == []


def test_application_replays_receipt_before_position_or_capture() -> None:
    """A successful retry survives source drift because receipt replay happens first."""
    bundle = _bundle()
    prepared = PreparedSpecificationSourceRegistration(
        project_id=PROJECT_ID,
        accepted_vision_artifact_id=11,
        accepted_product_goal_artifact_id=13,
        repository_binding_id=REPOSITORY_BINDING_ID,
        repository_binding_fingerprint="sha256:" + "e" * 64,
        request_fingerprint="sha256:" + "f" * 64,
        source_fingerprint=source_bundle_fingerprint(bundle),
        bundle=bundle,
    )
    domain = _Domain(
        WorkflowPosition(
            project_id=PROJECT_ID,
            graph_version="agileforge.workflow.v2",
            fact_fingerprint="sha256:facts",
            evaluated_at=NOW,
            available_nodes=(),
            waiting_nodes=(),
            blocked_nodes=(),
            invalid_nodes=(),
            terminal=False,
            decisions=(),
        )
    )
    registration = _RegistrationService(prepared)
    replayed = TransitionResult(ok=True, replayed=True)
    replay = _Replay(replayed)
    application = AgileForgeApplication(
        workflow_domain=domain,
        specification_source_registration=registration,
        specification_source_replay=replay,
    )
    request = SpecificationSourceRegistrationRequest(
        project_id=PROJECT_ID,
        source_path="specification.md",
        preparation_capability="grill-with-docs",
        idempotency_key="register-source-replay",
        actor="operator@example.test",
    )

    result = application.register_specification_source(request)

    assert result is replayed
    assert registration.requests == []
    assert domain.position_calls == 0
    assert domain.requests == []


def test_stale_registration_result_contains_recovery_without_a_write(
    source_recovery_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preparation drift returns sanitized recovery before capture or transition."""
    engine = source_recovery_engine
    fixture = _registered_source_fixture(engine, tmp_path)
    original_rows = _accepted_rows(engine)
    (fixture.repository / "SPECIFICATION.md").write_text("Changed source\n")
    _block_source_capture(monkeypatch)

    def forbidden_transition(_request: TransitionRequest) -> Never:
        pytest.fail("stale preparation unexpectedly dispatched a transition")  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(fixture.application, "transition", forbidden_transition)
    result = fixture.application.register_specification_source(_source_request(fixture))

    assert not result.ok
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.REPOSITORY_PROVENANCE_STALE
    assert result.model_dump(mode="json")["output"] == {
        "repository_recovery": _expected_stale_recovery(fixture.binding_id)
    }
    assert fixture.probe.calls == 1
    assert _accepted_rows(engine) == original_rows


def test_unrelated_registration_failure_keeps_empty_output(
    source_recovery_engine: Engine,
    tmp_path: Path,
) -> None:
    """An unrelated capture failure cannot acquire repository recovery guidance."""
    engine = source_recovery_engine
    fixture = _registered_source_fixture(engine, tmp_path)
    (fixture.repository / "SPECIFICATION.md").write_bytes(b"")
    refreshed = fixture.application.refresh_repository(
        RepositoryRefreshRequest(
            project_id=fixture.project_id,
            idempotency_key="empty-source-binding",
            actor="operator",
        )
    )
    assert refreshed.ok, refreshed.error
    original_rows = _accepted_rows(engine)
    result = fixture.application.register_specification_source(_source_request(fixture))

    assert not result.ok
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.STALE_SPECIFICATION_INPUT
    assert result.output == {}
    assert _accepted_rows(engine) == original_rows


def test_final_source_verification_stale_failure_keeps_generic_output(
    source_recovery_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drift at the final workflow check retains its existing generic failure."""
    engine = source_recovery_engine
    fixture = _registered_source_fixture(engine, tmp_path)
    original_rows = _accepted_rows(engine)
    verification_failures: list[SpecificationSourceRegistrationError] = []

    def changed_before_write(
        prepared: PreparedSpecificationSourceRegistration,
    ) -> SpecificationSourceRegistrationError | None:
        (fixture.repository / "SPECIFICATION.md").write_text(
            "Changed after preparation\n"
        )
        failure = fixture.registration.verify_prepared(prepared)
        assert failure is not None
        verification_failures.append(failure)
        return failure

    monkeypatch.setattr(
        fixture.domain, "_specification_registration_check", changed_before_write
    )
    result = fixture.application.register_specification_source(_source_request(fixture))

    assert not result.ok
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.STALE_SPECIFICATION_INPUT
    assert result.output == {}
    assert len(verification_failures) == 1
    assert verification_failures[0].recovery is not None
    assert verification_failures[0].recovery.model_dump(mode="json") == (
        _expected_stale_recovery(fixture.binding_id)
    )
    assert (
        fixture.probe.calls == _EXPECTED_FINAL_VERIFICATION_PROBES
    )  # Three preparation checks and the final stale check.
    assert _accepted_rows(engine) == original_rows


def test_refresh_current_source_replacement_preserves_acceptance_and_enables_retry(
    source_recovery_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit refresh changes source eligibility while accepted evidence survives."""
    engine = source_recovery_engine
    fixture = _accepted_replacement_fixture(engine, tmp_path)
    before = fixture.application.reads.specification_status(
        project_id=fixture.project_id
    )["data"]
    assert isinstance(before, dict)
    before_source_binding = before["source_binding"]
    assert isinstance(before_source_binding, dict)
    assert before_source_binding["state"] == "current"
    original_rows = _accepted_rows(engine)
    assert original_rows["registry"]
    assert original_rows["decisions"]
    source_bytes = b"Changed replacement requirements\n"
    (fixture.repository / "SPECIFICATION.md").write_bytes(source_bytes)
    with monkeypatch.context() as blocked:
        _block_source_capture(blocked)
        with pytest.raises(SpecificationSourceRegistrationError) as caught:
            fixture.application.specification_source_capability(
                project_id=fixture.project_id
            )
        assert caught.value.recovery is not None
        stale = fixture.application.register_specification_source(
            _source_request(fixture)
        )
    assert not stale.ok
    assert stale.model_dump(mode="json")["output"] == {
        "repository_recovery": _expected_stale_recovery(fixture.binding_id)
    }
    assert fixture.probe.calls == _EXPECTED_STALE_REPLACEMENT_PROBES
    assert _accepted_rows(engine) == original_rows

    refreshed = fixture.application.refresh_repository(
        RepositoryRefreshRequest(
            project_id=fixture.project_id,
            idempotency_key="refresh-stale-replacement",
            actor="operator",
        )
    )
    assert refreshed.ok, refreshed.error
    next_binding_id = _active_binding_id(engine, fixture.project_id)
    assert next_binding_id > fixture.binding_id
    after = fixture.application.reads.specification_status(
        project_id=fixture.project_id
    )["data"]
    assert isinstance(after, dict)
    assert after["source"] is None
    assert after["current"] == before["current"]
    assert after["source_binding"] == {
        "state": "different_binding",
        "active_repository_binding_id": next_binding_id,
        "accepted_source": before_source_binding["accepted_source"],
    }
    assert _accepted_rows(engine) == original_rows
    assert (fixture.repository / "SPECIFICATION.md").read_bytes() == source_bytes

    monkeypatch.setattr(
        fixture.domain,
        "_clock",
        FixedClock(now_value=DISCOVERY_NOW + timedelta(seconds=4)),
    )
    preview = fixture.application.preview_specification_source(_source_request(fixture))
    decision = next(
        item
        for item in fixture.domain.position(fixture.project_id).decisions
        if item.request_kind == "register_specification_source"
    )
    retry = fixture.application.register_specification_source(
        _source_request(fixture).model_copy(
            update={
                "expected_source_fingerprint": preview.source_fingerprint,
                "expected_decision_fingerprint": decision.decision_fingerprint,
            }
        )
    )
    assert retry.ok, retry.error
    assert retry.position is not None
    assert any(
        item.request_kind == "structure_specification"
        and item.category is NodeCategory.AVAILABLE
        for item in retry.position.decisions
    )
    retried_rows = _accepted_rows(engine)
    assert retried_rows["sources"][:-1] == original_rows["sources"]
    assert retried_rows["sources"][-1]["repository_binding_id"] == next_binding_id
    for slot in ("candidates", "decisions", "registry"):
        assert retried_rows[slot] == original_rows[slot]
