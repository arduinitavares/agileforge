"""Application-boundary tests for host-owned Specification source capture."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from services.application import AgileForgeApplication
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
    SpecificationSourceCapturePreview,
    SpecificationSourceRegistrationRequest,
)
from workflow.contracts import (
    FactReference,
    JsonObject,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
    TransitionResult,
    WorkflowErrorCode,
    WorkflowPosition,
)

if TYPE_CHECKING:
    from services.node_attempt_replay import TransitionReplayQuery
    from workflow.requests import RegisterSpecificationSource, TransitionRequest

NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)
PROJECT_ID = 7
REPOSITORY_BINDING_ID = 17


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
        return SpecificationSourceCapturePreview.from_bundle(self.prepared.bundle)


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
