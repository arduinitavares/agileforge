"""Guarded transactional entry point for the domain workflow graph."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, assert_never

from pydantic import TypeAdapter
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, col, select

from models.product_definition import SpecificationCandidate
from models.workflow import (
    WorkflowNodeAttempt,
    WorkflowNodeAttemptOutcome,
    WorkflowTransitionReceipt,
)
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from services.contracts.specification_authoring import (
    SPECIFICATION_ACTIVE_REPOSITORY_SOURCE_ID,
    SpecificationAuthoringInput,
    specification_authoring_input_fingerprint,
)
from workflow.contracts import (
    JsonObject,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
    WorkflowPosition,
)
from workflow.fingerprints import (
    business_fact_fingerprint,
    canonical_hash,
    canonical_json,
)
from workflow.handlers import (
    AttemptStartState,
    as_utc,
    execute_abandon_product_goal,
    execute_begin_vision_revision,
    execute_compile_authority,
    execute_complete_specification_authoring,
    execute_create_project,
    execute_decide_authority,
    execute_decide_backlog,
    execute_decide_product_goal_review,
    execute_decide_specification,
    execute_decide_vision_review,
    execute_execution_request,
    execute_fulfill_product_goal,
    execute_generate_vision_bootstrap,
    execute_planning_request,
    execute_record_authority_feedback,
    execute_record_backlog_draft,
    execute_record_product_goal_interview_turn,
    execute_record_repository_binding,
    execute_record_vision_interview_turn,
    execute_repair_authority,
    execute_start_node_attempt,
    load_attempt,
    load_attempt_outcome,
    record_failure_outcome,
    record_obsolete_outcome,
    record_success_outcome,
    validate_decide_authority_review,
    validate_decide_backlog_review,
    validate_planning_review,
)
from workflow.requests import (
    AbandonProductGoal,
    ApplyStoryDependencies,
    BeginVisionRevision,
    CloseSprint,
    CloseStory,
    CompileAuthority,
    CompleteSpecificationAuthoring,
    CompleteTask,
    CreateProject,
    DecideAuthority,
    DecideBacklog,
    DecideProductGoalReview,
    DecideRoadmap,
    DecideSpecification,
    DecideSprintPlan,
    DecideStory,
    DecideVisionReview,
    FailNodeAttempt,
    FulfillProductGoal,
    GenerateVisionBootstrap,
    ObsoleteNodeAttempt,
    RecordAuthorityFeedback,
    RecordBacklogDraft,
    RecordPostSprintTriage,
    RecordProductGoalInterviewTurn,
    RecordRepositoryBinding,
    RecordRoadmapDraft,
    RecordSprintPlan,
    RecordStoryDraft,
    RecordVisionInterviewTurn,
    RepairAuthority,
    RepairStoryReadiness,
    RevalidateNodeAttempt,
    ReviewSprint,
    StartNodeAttempt,
    StartSprint,
    TransitionRequest,
)

_JSON_OBJECT = TypeAdapter(JsonObject)

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

    from sqlalchemy.engine import Engine

    from workflow.clock import Clock
    from workflow.facts import WorkflowFactSnapshot
    from workflow.graph import WorkflowGraph
    from workflow.requests.base import PositionedRequest


class AdkRecipeRegistryProtocol(Protocol):
    """Domain-facing recipe lookup without importing the ADK adapter."""

    def require(self, node_id: str) -> object:
        """Return a recipe or raise LookupError when the node is unregistered."""


class SpecificationSourceCheck(Protocol):
    """Re-probe exact persisted Specification sources at a mutation boundary."""

    def __call__(
        self,
        project_id: int,
        persisted_input: JsonObject,
        /,
    ) -> WorkflowError | None:
        """Return stale-source detail, or None while sources remain current."""


_SQLITE_BUSY_TIMEOUT_MS = 1_000
_SQLITE_LOCK_MESSAGES = ("database is locked", "database table is locked")

type _AuthorityRequest = (
    CompileAuthority | DecideAuthority | RecordAuthorityFeedback | RepairAuthority
)
type _ProductGoalRequest = (
    RecordProductGoalInterviewTurn
    | DecideProductGoalReview
    | FulfillProductGoal
    | AbandonProductGoal
)
type _ProductDiscoveryRequest = (
    CompleteSpecificationAuthoring | DecideSpecification
)
type _VisionRequest = (
    GenerateVisionBootstrap
    | RecordVisionInterviewTurn
    | DecideVisionReview
    | BeginVisionRevision
)
type _BacklogRequest = RecordBacklogDraft | DecideBacklog
type _PlanningRequest = (
    RecordRoadmapDraft
    | DecideRoadmap
    | RecordStoryDraft
    | DecideStory
    | ApplyStoryDependencies
    | RepairStoryReadiness
    | RecordSprintPlan
    | DecideSprintPlan
    | StartSprint
)
type _ExecutionRequest = (
    CompleteTask | CloseStory | ReviewSprint | CloseSprint | RecordPostSprintTriage
)
type _PositionedTransitionRequest = (
    _AuthorityRequest
    | GenerateVisionBootstrap
    | RecordVisionInterviewTurn
    | DecideVisionReview
    | BeginVisionRevision
    | _ProductGoalRequest
    | _ProductDiscoveryRequest
    | RecordBacklogDraft
    | DecideBacklog
    | _PlanningRequest
    | _ExecutionRequest
)


@dataclass(frozen=True)
class _ReceiptClaim:
    """Result of claiming or replaying one canonical transition receipt."""

    receipt: WorkflowTransitionReceipt | None = None
    immediate_result: TransitionResult | None = None


def _set_sqlite_busy_timeout(
    dbapi_connection: sqlite3.Connection,
    _connection_record: object,
) -> None:
    """Bound how long each newly opened SQLite connection waits for a writer."""
    cursor = dbapi_connection.cursor()
    cursor.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
    cursor.close()


class WorkflowDomain:
    """Expose the only workflow position read and transition mutation APIs."""

    def __init__(
        self,
        *,
        engine: Engine,
        graph: WorkflowGraph,
        clock: Clock,
        adk_recipe_registry: AdkRecipeRegistryProtocol | None = None,
        specification_source_check: SpecificationSourceCheck | None = None,
    ) -> None:
        """Retain explicit persistence, graph, and time dependencies."""
        self._engine = engine
        self._graph = graph
        self._clock = clock
        self._adk_recipe_registry = adk_recipe_registry
        self._specification_source_check = specification_source_check
        self._configure_busy_timeout()

    def position(self, project_id: int) -> WorkflowPosition:
        """Derive one position from complete durable facts and the injected clock."""
        evaluated_at = self._clock.now()
        with Session(self._engine) as session:
            return self._position_in_session(session, project_id, evaluated_at)

    def replay_project_transition(
        self,
        *,
        request_kind: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> TransitionResult | None:
        """Replay or conflict on a claimed Project lifecycle key before probing."""
        with Session(self._engine) as session:
            claim = self._existing_receipt_claim_for_fingerprint(
                session,
                request_kind=request_kind,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
        return None if claim is None else claim.immediate_result

    def load_persisted_attempt_input(
        self,
        *,
        project_id: int,
        attempt_id: int,
        attempt_fingerprint: str,
    ) -> JsonObject:
        """Load validated normalized input by the assigned durable attempt identity."""
        with Session(self._engine) as session:
            attempt = load_attempt(
                session,
                project_id=project_id,
                attempt_id=attempt_id,
            )
            if attempt is None or attempt.attempt_fingerprint != attempt_fingerprint:
                message = "The durable node attempt identity is invalid."
                raise ValueError(message)
            normalized_input = json.loads(attempt.normalized_input_json)
            return _JSON_OBJECT.validate_python(normalized_input)

    def transition(self, request: TransitionRequest) -> TransitionResult:
        """Guard and apply one request inside its receipt transaction."""
        evaluated_at = self._clock.now()
        with Session(self._engine) as session:
            try:
                result = self.transition_in_session(session, request, evaluated_at)
                session.commit()
            except OperationalError as error:
                session.rollback()
                if self._is_sqlite_lock_timeout(error):
                    return self._fact_conflict(
                        "Another workflow transition holds the Project fact lock."
                    )
                raise
            except WorkflowFactLoadError as error:
                session.rollback()
                if isinstance(
                    request,
                    CompleteTask
                    | CloseStory
                    | ReviewSprint
                    | CloseSprint
                    | RecordPostSprintTriage,
                ):
                    return self._fact_conflict(str(error))
                raise
            except Exception:
                session.rollback()
                raise
            else:
                return result

    def transition_in_session(
        self,
        session: Session,
        request: TransitionRequest,
        evaluated_at: datetime | None = None,
    ) -> TransitionResult:
        """Apply one transition in a caller-owned transaction without committing it."""
        return self._transition_in_session(
            session,
            request,
            self._clock.now() if evaluated_at is None else evaluated_at,
        )

    def _transition_in_session(
        self,
        session: Session,
        request: TransitionRequest,
        evaluated_at: datetime,
    ) -> TransitionResult:
        """Own receipt claim, handler facts, and completion in one transaction."""
        self._begin_write(session)
        if isinstance(
            request,
            DecideAuthority
            | DecideVisionReview
            | DecideBacklog
            | DecideRoadmap
            | DecideStory
            | DecideSprintPlan,
        ):
            existing = self._existing_receipt_claim(session, request)
            if existing is not None:
                if existing.immediate_result is None:
                    msg = "An existing receipt claim did not produce a result."
                    raise RuntimeError(msg)
                return existing.immediate_result
            if isinstance(request, DecideAuthority):
                review_failure = validate_decide_authority_review(session, request)
            elif isinstance(request, DecideVisionReview):
                review_failure = None
            elif isinstance(request, DecideBacklog):
                review_failure = validate_decide_backlog_review(session, request)
            else:
                review_failure = validate_planning_review(session, request)
            if review_failure is not None:
                return review_failure
        claim = self._claim_receipt(session, request, evaluated_at)
        if claim.immediate_result is not None:
            return claim.immediate_result
        receipt = self._required_receipt(claim)
        result = self._execute_request(session, request, evaluated_at)
        self._complete_receipt(session, receipt, result, evaluated_at)
        return result

    def _configure_busy_timeout(self) -> None:
        """Configure current and future SQLite connections before transitions."""
        if self._engine.dialect.name != "sqlite":
            return
        if not event.contains(self._engine, "connect", _set_sqlite_busy_timeout):
            event.listen(self._engine, "connect", _set_sqlite_busy_timeout)
        with self._engine.connect() as connection:
            connection.exec_driver_sql(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")

    def _begin_write(self, session: Session) -> None:
        """Acquire the SQLite writer lock as the transition's first statement."""
        if self._engine.dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            return
        session.begin()

    @staticmethod
    def _required_receipt(claim: _ReceiptClaim) -> WorkflowTransitionReceipt:
        """Narrow a new claim to its required persisted receipt row."""
        if claim.receipt is None:
            msg = "A new receipt claim did not retain its receipt row."
            raise RuntimeError(msg)
        return claim.receipt

    def _claim_receipt(
        self,
        session: Session,
        request: TransitionRequest,
        evaluated_at: datetime,
    ) -> _ReceiptClaim:
        """Claim a canonical request key or return its persisted result."""
        existing = self._existing_receipt_claim(session, request)
        if existing is not None:
            return existing
        request_payload = request.model_dump(mode="json")
        request_json = canonical_json(request_payload)
        request_hash = self._request_fingerprint(request)

        receipt = WorkflowTransitionReceipt(
            request_kind=request.kind,
            idempotency_key=request.idempotency_key,
            request_fingerprint=request_hash,
            request_json=request_json,
            started_at=evaluated_at,
        )
        session.add(receipt)
        session.flush()
        return _ReceiptClaim(receipt=receipt)

    @classmethod
    def _existing_receipt_claim(
        cls,
        session: Session,
        request: TransitionRequest,
    ) -> _ReceiptClaim | None:
        """Return the immutable result for an existing idempotency key."""
        return cls._existing_receipt_claim_for_fingerprint(
            session,
            request_kind=request.kind,
            idempotency_key=request.idempotency_key,
            request_fingerprint=cls._request_fingerprint(request),
        )

    @staticmethod
    def _existing_receipt_claim_for_fingerprint(
        session: Session,
        *,
        request_kind: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> _ReceiptClaim | None:
        """Return one stored result for an exact semantic request identity."""
        receipt = session.exec(
            select(WorkflowTransitionReceipt).where(
                col(WorkflowTransitionReceipt.request_kind) == request_kind,
                col(WorkflowTransitionReceipt.idempotency_key) == idempotency_key,
            )
        ).one_or_none()
        if receipt is None:
            return None
        if receipt.request_fingerprint != request_fingerprint:
            return _ReceiptClaim(
                immediate_result=WorkflowDomain._fact_conflict(
                    "The idempotency key was already used for different input."
                )
            )
        if receipt.result_json is None or receipt.completed_at is None:
            return _ReceiptClaim(
                immediate_result=WorkflowDomain._fact_conflict(
                    "The idempotency receipt is incomplete."
                )
            )
        persisted = TransitionResult.model_validate_json(receipt.result_json)
        return _ReceiptClaim(
            immediate_result=persisted.model_copy(update={"replayed": True})
        )

    @staticmethod
    def _request_fingerprint(request: TransitionRequest) -> str:
        """Hash semantic Project input and exact payloads for all other requests."""
        if isinstance(request, CreateProject | RecordRepositoryBinding):
            return request.semantic_fingerprint()
        return canonical_hash(request.model_dump(mode="json"))

    def _execute_request(
        self,
        session: Session,
        request: TransitionRequest,
        evaluated_at: datetime,
    ) -> TransitionResult:
        """Dispatch only after the receipt claim and all position guards."""
        if isinstance(request, CreateProject | RecordRepositoryBinding):
            return self._execute_project_request(session, request, evaluated_at)
        if isinstance(request, StartNodeAttempt):
            return self._execute_start_attempt(session, request, evaluated_at)
        if isinstance(request, RevalidateNodeAttempt | ObsoleteNodeAttempt):
            return (
                self._execute_revalidate_attempt(session, request, evaluated_at)
                if isinstance(request, RevalidateNodeAttempt)
                else self._execute_obsolete_attempt(session, request, evaluated_at)
            )
        if isinstance(request, FailNodeAttempt):
            return self._execute_failed_attempt(session, request, evaluated_at)
        if request.attempt_id is not None:
            return self._execute_attempt_continuation(
                session,
                request,
                evaluated_at,
            )
        return self._execute_positioned(session, request, evaluated_at)

    def _execute_project_request(
        self,
        session: Session,
        request: CreateProject | RecordRepositoryBinding,
        evaluated_at: datetime,
    ) -> TransitionResult:
        """Dispatch the two non-positioned Project aggregate mutations."""
        if isinstance(request, CreateProject):
            return execute_create_project(session, request, self._graph, evaluated_at)
        return self._execute_repository_binding(session, request, evaluated_at)

    def _execute_repository_binding(
        self,
        session: Session,
        request: RecordRepositoryBinding,
        evaluated_at: datetime,
    ) -> TransitionResult:
        """Guard an orthogonal repository mutation against the current graph facts."""
        position = self._position_in_session(session, request.project_id, evaluated_at)
        if request.graph_version != position.graph_version:
            return self._stale(position, "The workflow graph version changed.")
        if request.fact_fingerprint != position.fact_fingerprint:
            return self._stale(position, "The complete Project facts changed.")
        return execute_record_repository_binding(
            session,
            request,
            self._graph,
            evaluated_at,
        )

    def _execute_start_attempt(
        self,
        session: Session,
        request: StartNodeAttempt,
        evaluated_at: datetime,
    ) -> TransitionResult:
        """Guard and persist a currently available registry-backed decision."""
        position = self._position_in_session(session, request.project_id, evaluated_at)
        decision_or_failure = self._start_decision(position, request, evaluated_at)
        if isinstance(decision_or_failure, TransitionResult):
            return decision_or_failure
        decision = decision_or_failure
        if not self._graph.is_agentic_node(request.target_node_id):
            return TransitionResult(
                ok=False,
                position=position,
                error=WorkflowError(
                    code=WorkflowErrorCode.TRANSITION_NOT_AVAILABLE,
                    message="The requested node is not classified for agent execution.",
                ),
            )
        if not self._has_registered_recipe(request.target_node_id):
            return TransitionResult(
                ok=False,
                position=position,
                error=WorkflowError(
                    code=WorkflowErrorCode.TRANSITION_NOT_AVAILABLE,
                    message="The requested node has no registered ADK recipe.",
                ),
            )
        snapshot = WorkflowFactRepository(session).load(request.project_id)
        state = AttemptStartState(
            business_fingerprint=business_fact_fingerprint(snapshot),
            expired_attempt_id=self._expired_attempt_id(
                snapshot,
                request,
                decision,
                evaluated_at,
            ),
        )
        result = execute_start_node_attempt(
            session,
            request,
            decision,
            evaluated_at,
            state,
        )
        return result.model_copy(
            update={
                "position": self._position_in_session(
                    session,
                    request.project_id,
                    evaluated_at,
                )
            }
        )

    def _execute_revalidate_attempt(
        self,
        session: Session,
        request: RevalidateNodeAttempt,
        evaluated_at: datetime,
    ) -> TransitionResult:
        """Recheck Specification attempt authority before external execution."""
        row = load_attempt(
            session,
            project_id=request.project_id,
            attempt_id=request.attempt_id,
        )
        outcome = load_attempt_outcome(
            session,
            project_id=request.project_id,
            attempt_id=request.attempt_id,
        )
        if (
            row is not None
            and outcome is not None
            and row.attempt_fingerprint == request.attempt_fingerprint
            and row.node_id == request.target_node_id
        ):
            return self._replay_attempt_start_receipt(session, row)
        snapshot = WorkflowFactRepository(session).load(request.project_id)
        position = self._graph.evaluate(snapshot, evaluated_at)
        latest_attempt = max(
            (
                attempt
                for attempt in snapshot.node_attempts
                if row is not None
                and attempt.node_id == row.node_id
                and attempt.instance_key == row.instance_key
            ),
            key=lambda attempt: attempt.attempt_id,
            default=None,
        )
        decision_is_current = row is not None and any(
            decision.node_id == row.node_id
            and decision.instance_key == row.instance_key
            and decision.category is NodeCategory.WAITING
            for decision in position.decisions
        )
        mismatch = (
            row is None
            or outcome is not None
            or row.attempt_fingerprint != request.attempt_fingerprint
            or row.node_id != request.target_node_id
            or row.graph_version != self._graph.graph_version
            or evaluated_at >= as_utc(row.lease_expires_at)
            or row.business_fact_fingerprint != business_fact_fingerprint(snapshot)
            or not self._attempt_input_matches(row)
            or latest_attempt is None
            or latest_attempt.attempt_id != row.workflow_node_attempt_id
            or not decision_is_current
        )
        if mismatch:
            return self._obsolete_attempt(
                session,
                project_id=request.project_id,
                attempt_id=request.attempt_id,
                evaluated_at=evaluated_at,
                error=WorkflowError(
                    code=WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
                    message=(
                        "Specification authoring input changed before provider "
                        "invocation."
                    ),
                ),
            )
        return TransitionResult(
            ok=True,
            applied_node_id=row.node_id,
            output={"attempt_id": request.attempt_id, "status": "authoritative"},
            position=position,
        )

    @staticmethod
    def _attempt_input_matches(row: WorkflowNodeAttempt | None) -> bool:
        """Validate the persisted normalized input against its captured hash."""
        if row is None:
            return False
        try:
            normalized = _JSON_OBJECT.validate_python(
                json.loads(row.normalized_input_json)
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            return False
        return (
            canonical_json(normalized) == row.normalized_input_json
            and canonical_hash(normalized) == row.input_fingerprint
        )

    def _execute_obsolete_attempt(
        self,
        session: Session,
        request: ObsoleteNodeAttempt,
        evaluated_at: datetime,
    ) -> TransitionResult:
        """Close a live exact Specification attempt after stale host evidence."""
        row = load_attempt(
            session,
            project_id=request.project_id,
            attempt_id=request.attempt_id,
        )
        outcome = load_attempt_outcome(
            session,
            project_id=request.project_id,
            attempt_id=request.attempt_id,
        )
        if (
            row is not None
            and outcome is not None
            and row.attempt_fingerprint == request.attempt_fingerprint
            and row.node_id == "specification.author"
        ):
            return self._replay_attempt_start_receipt(session, row)
        if (
            row is None
            or outcome is not None
            or row.attempt_fingerprint != request.attempt_fingerprint
            or row.node_id != "specification.author"
        ):
            return self._obsolete_attempt(
                session,
                project_id=request.project_id,
                attempt_id=request.attempt_id,
                evaluated_at=evaluated_at,
            )
        return self._obsolete_attempt(
            session,
            project_id=request.project_id,
            attempt_id=request.attempt_id,
            evaluated_at=evaluated_at,
            error=WorkflowError(
                code=request.error_code,
                message=request.error_message,
            ),
        )

    @staticmethod
    def _start_decision(
        position: WorkflowPosition,
        request: StartNodeAttempt,
        evaluated_at: datetime,
    ) -> NodeDecision | TransitionResult:
        """Validate full guards and availability for an attempt start."""
        stale_message: str | None = None
        if request.graph_version != position.graph_version:
            stale_message = "The workflow graph version changed."
        elif request.fact_fingerprint != position.fact_fingerprint:
            stale_message = "The complete Project facts changed."
        decision = next(
            (
                item
                for item in position.decisions
                if item.node_id == request.target_node_id
                and item.instance_key == request.target_instance_key
            ),
            None,
        )
        if stale_message is None and decision is not None:
            if request.decision_fingerprint != decision.decision_fingerprint:
                stale_message = "The exact node decision changed."
            elif (
                decision.valid_until is not None
                and evaluated_at >= decision.valid_until
            ):
                stale_message = "The node decision expired."
        if stale_message is not None:
            return WorkflowDomain._stale(position, stale_message)
        if decision is None or decision.category is not NodeCategory.AVAILABLE:
            return TransitionResult(
                ok=False,
                position=position,
                error=WorkflowError(
                    code=WorkflowErrorCode.TRANSITION_NOT_AVAILABLE,
                    message="The requested node is not currently available.",
                ),
            )
        return decision

    def _has_registered_recipe(self, node_id: str) -> bool:
        """Return whether the injected execution registry owns a node."""
        if self._adk_recipe_registry is None:
            return False
        try:
            self._adk_recipe_registry.require(node_id)
        except LookupError:
            return False
        return True

    @staticmethod
    def _expired_attempt_id(
        snapshot: WorkflowFactSnapshot,
        request: StartNodeAttempt,
        decision: NodeDecision,
        evaluated_at: datetime,
    ) -> int | None:
        """Return the exact expired attempt replaced by a recovery decision."""
        if decision.recommendation_kind is not RecommendationKind.RECOVERY:
            return None
        expired = tuple(
            item
            for item in snapshot.node_attempts
            if item.node_id == request.target_node_id
            and item.instance_key == request.target_instance_key
            and item.outcome is None
            and evaluated_at >= item.lease_expires_at
        )
        if not expired:
            return None
        return max(item.attempt_id for item in expired)

    def _execute_attempt_continuation(
        self,
        session: Session,
        request: _PositionedTransitionRequest,
        evaluated_at: datetime,
    ) -> TransitionResult:
        """Apply one live attempt output without requiring public availability."""
        if request.attempt_id is None or request.attempt_fingerprint is None:
            msg = "Attempt continuation identity is incomplete."
            raise RuntimeError(msg)
        row = load_attempt(
            session,
            project_id=request.project_id,
            attempt_id=request.attempt_id,
        )
        snapshot = WorkflowFactRepository(session).load(request.project_id)
        mismatch = (
            row is None
            or load_attempt_outcome(
                session,
                project_id=request.project_id,
                attempt_id=request.attempt_id,
            )
            is not None
            or row.attempt_fingerprint != request.attempt_fingerprint
            or row.node_id != request.decision_node_id()
            or row.instance_key != request.decision_instance_key()
            or row.graph_version != request.graph_version
            or row.graph_version != self._graph.graph_version
            or row.fact_fingerprint != request.fact_fingerprint
            or row.decision_fingerprint != request.decision_fingerprint
            or evaluated_at >= as_utc(row.lease_expires_at)
            or row.business_fact_fingerprint != business_fact_fingerprint(snapshot)
        )
        if mismatch:
            return self._obsolete_attempt(
                session,
                project_id=request.project_id,
                attempt_id=request.attempt_id,
                evaluated_at=evaluated_at,
            )
        position = self._graph.evaluate(snapshot, evaluated_at)
        decision = self._decision(position, request)
        if decision is None:
            return self._obsolete_attempt(
                session,
                project_id=request.project_id,
                attempt_id=request.attempt_id,
                evaluated_at=evaluated_at,
            )
        if isinstance(request, CompleteSpecificationAuthoring):
            source_failure = self._revalidate_specification_completion_sources(
                session,
                request=request,
                attempt=row,
                evaluated_at=evaluated_at,
            )
            if source_failure is not None:
                return source_failure
        result = self._dispatch_positioned(
            session,
            request,
            decision,
            evaluated_at,
        )
        if result.ok:
            record_success_outcome(
                session,
                project_id=request.project_id,
                attempt_id=request.attempt_id,
                output=result.output,
                evaluated_at=evaluated_at,
            )
        elif isinstance(request, CompleteSpecificationAuthoring):
            error = result.error
            if error is None:
                message = "Failed Specification authoring has no structured error."
                raise RuntimeError(message)
            session.add(
                WorkflowNodeAttemptOutcome(
                    project_id=request.project_id,
                    workflow_node_attempt_id=request.attempt_id,
                    status="failure",
                    failure_code=error.code.value,
                    failure_message=error.message,
                    recorded_at=evaluated_at,
                )
            )
            session.flush()
        terminal_result = result.model_copy(
            update={
                "position": self._position_in_session(
                    session,
                    request.project_id,
                    evaluated_at,
                )
            }
        )
        self._complete_attempt_start_receipt(
            session,
            row,
            terminal_result,
            evaluated_at,
        )
        return terminal_result

    def _revalidate_specification_completion_sources(
        self,
        session: Session,
        *,
        request: CompleteSpecificationAuthoring,
        attempt: WorkflowNodeAttempt | None,
        evaluated_at: datetime,
    ) -> TransitionResult | None:
        """Re-probe exact persisted sources immediately before candidate write."""
        if attempt is None or not self._attempt_input_matches(attempt):
            return self._obsolete_attempt(
                session,
                project_id=request.project_id,
                attempt_id=request.attempt_id,
                evaluated_at=evaluated_at,
                error=WorkflowError(
                    code=WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
                    message="The pending Specification source input is invalid.",
                ),
            )
        try:
            persisted_input = _JSON_OBJECT.validate_json(
                attempt.normalized_input_json
            )
            contract = SpecificationAuthoringInput.model_validate(persisted_input)
        except (TypeError, ValueError):
            return self._obsolete_attempt(
                session,
                project_id=request.project_id,
                attempt_id=request.attempt_id,
                evaluated_at=evaluated_at,
                error=WorkflowError(
                    code=WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
                    message="The pending Specification source input is invalid.",
                ),
            )
        serialized_input = contract.model_dump(mode="json")
        if (
            contract.project_id != request.project_id
            or canonical_json(serialized_input) != attempt.normalized_input_json
        ):
            return self._obsolete_attempt(
                session,
                project_id=request.project_id,
                attempt_id=request.attempt_id,
                evaluated_at=evaluated_at,
                error=WorkflowError(
                    code=WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
                    message="The pending Specification source input changed.",
                ),
            )
        active_repository_source = next(
            (
                entry
                for entry in contract.source_manifest
                if entry.source_id == SPECIFICATION_ACTIVE_REPOSITORY_SOURCE_ID
            ),
            None,
        )
        source_check = self._specification_source_check
        if source_check is None:
            source_error = (
                None
                if active_repository_source is None
                else WorkflowError(
                    code=WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
                    message="Live Specification source validation is unavailable.",
                )
            )
        else:
            source_error = source_check(request.project_id, serialized_input)
        if source_error is None:
            return None
        return self._obsolete_attempt(
            session,
            project_id=request.project_id,
            attempt_id=request.attempt_id,
            evaluated_at=evaluated_at,
            error=source_error,
        )

    def _execute_failed_attempt(
        self,
        session: Session,
        request: FailNodeAttempt,
        evaluated_at: datetime,
    ) -> TransitionResult:
        """Record failure only while the exact attempt lease remains authoritative."""
        row = load_attempt(
            session,
            project_id=request.project_id,
            attempt_id=request.attempt_id,
        )
        snapshot = WorkflowFactRepository(session).load(request.project_id)
        mismatch = (
            row is None
            or load_attempt_outcome(
                session,
                project_id=request.project_id,
                attempt_id=request.attempt_id,
            )
            is not None
            or row.attempt_fingerprint != request.attempt_fingerprint
            or row.graph_version != self._graph.graph_version
            or evaluated_at >= as_utc(row.lease_expires_at)
            or row.business_fact_fingerprint != business_fact_fingerprint(snapshot)
        )
        if mismatch:
            return self._obsolete_attempt(
                session,
                project_id=request.project_id,
                attempt_id=request.attempt_id,
                evaluated_at=evaluated_at,
            )
        record_failure_outcome(session, request, evaluated_at)
        position = self._position_in_session(
            session,
            request.project_id,
            evaluated_at,
        )
        precise_failure_codes = {
            WorkflowErrorCode.VISION_EVIDENCE_STALE,
            WorkflowErrorCode.INVALID_SPECIFICATION_PAYLOAD,
            WorkflowErrorCode.UNSUPPORTED_SPECIFICATION_SCHEMA,
            WorkflowErrorCode.SPECIFICATION_PRODUCER_FAILED,
        }
        try:
            requested_code = WorkflowErrorCode(request.failure_code)
        except ValueError:
            requested_code = WorkflowErrorCode.EXTERNAL_EXECUTION_FAILED
        error = (
            WorkflowError(
                code=requested_code,
                message=request.failure_message,
            )
            if requested_code in precise_failure_codes
            else WorkflowError(
                code=WorkflowErrorCode.EXTERNAL_EXECUTION_FAILED,
                message="ADK recipe execution or output validation failed.",
            )
        )
        command_result = TransitionResult(
            ok=False,
            position=position,
            error=error,
        )
        self._complete_attempt_start_receipt(
            session,
            row,
            command_result,
            evaluated_at,
        )
        return TransitionResult(
            ok=True,
            applied_node_id=row.node_id,
            output={"attempt_id": request.attempt_id, "status": "failure"},
            position=position,
        )

    def _obsolete_attempt(
        self,
        session: Session,
        *,
        project_id: int,
        attempt_id: int,
        evaluated_at: datetime,
        error: WorkflowError | None = None,
    ) -> TransitionResult:
        """Record obsolescence when possible and deny business mutation."""
        row = load_attempt(
            session,
            project_id=project_id,
            attempt_id=attempt_id,
        )
        outcome = load_attempt_outcome(
            session,
            project_id=project_id,
            attempt_id=attempt_id,
        )
        if row is not None and outcome is None:
            record_obsolete_outcome(
                session,
                project_id=project_id,
                attempt_id=attempt_id,
                evaluated_at=evaluated_at,
            )
        result = TransitionResult(
            ok=False,
            position=self._position_in_session(session, project_id, evaluated_at),
            error=error
            or WorkflowError(
                code=WorkflowErrorCode.ATTEMPT_OBSOLETE,
                message="The node attempt is no longer authoritative.",
            ),
        )
        if row is not None and outcome is None:
            self._complete_attempt_start_receipt(
                session,
                row,
                result,
                evaluated_at,
            )
        return result

    def _execute_positioned(
        self,
        session: Session,
        request: _PositionedTransitionRequest,
        evaluated_at: datetime,
    ) -> TransitionResult:
        """Re-derive and guard a positioned request before handler dispatch."""
        decision_or_failure = self._guarded_decision(
            session,
            request,
            evaluated_at,
        )
        if isinstance(decision_or_failure, TransitionResult):
            return decision_or_failure

        result = self._dispatch_positioned(
            session,
            request,
            decision_or_failure,
            evaluated_at,
        )
        position = self._position_in_session(
            session,
            request.project_id,
            evaluated_at,
        )
        return result.model_copy(update={"position": position})

    def _dispatch_positioned(
        self,
        session: Session,
        request: _PositionedTransitionRequest,
        decision: NodeDecision,
        evaluated_at: datetime,
    ) -> TransitionResult:
        """Dispatch one request through its retained lifecycle workstream."""
        if isinstance(
            request,
            CompleteTask
            | CloseStory
            | ReviewSprint
            | CloseSprint
            | RecordPostSprintTriage,
        ):
            result = execute_execution_request(session, request, decision, evaluated_at)
        elif isinstance(
            request,
            RecordProductGoalInterviewTurn
            | DecideProductGoalReview
            | FulfillProductGoal
            | AbandonProductGoal,
        ):
            result = self._dispatch_product_goal(
                session, request, decision, evaluated_at
            )
        elif isinstance(
            request,
            CompleteSpecificationAuthoring | DecideSpecification,
        ):
            result = self._dispatch_product_discovery(
                session, request, decision, evaluated_at
            )
        elif isinstance(
            request,
            CompileAuthority
            | DecideAuthority
            | RecordAuthorityFeedback
            | RepairAuthority,
        ):
            result = self._dispatch_authority(session, request, decision, evaluated_at)
        elif isinstance(
            request,
            GenerateVisionBootstrap
            | RecordVisionInterviewTurn
            | DecideVisionReview
            | BeginVisionRevision,
        ):
            result = self._dispatch_vision(session, request, decision, evaluated_at)
        elif isinstance(request, RecordBacklogDraft | DecideBacklog):
            result = self._dispatch_backlog(session, request, decision, evaluated_at)
        elif isinstance(
            request,
            RecordRoadmapDraft
            | DecideRoadmap
            | RecordStoryDraft
            | DecideStory
            | ApplyStoryDependencies
            | RepairStoryReadiness
            | RecordSprintPlan
            | DecideSprintPlan
            | StartSprint,
        ):
            result = execute_planning_request(session, request, decision, evaluated_at)
        else:
            assert_never(request)
        return result

    @staticmethod
    def _dispatch_product_goal(
        session: Session,
        request: _ProductGoalRequest,
        decision: NodeDecision,
        evaluated_at: datetime,
    ) -> TransitionResult:
        if isinstance(request, RecordProductGoalInterviewTurn):
            return execute_record_product_goal_interview_turn(
                session, request, decision, evaluated_at
            )
        if isinstance(request, DecideProductGoalReview):
            return execute_decide_product_goal_review(
                session, request, decision, evaluated_at
            )
        if isinstance(request, FulfillProductGoal):
            return execute_fulfill_product_goal(
                session, request, decision, evaluated_at
            )
        if isinstance(request, AbandonProductGoal):
            return execute_abandon_product_goal(
                session, request, decision, evaluated_at
            )
        assert_never(request)

    def _dispatch_product_discovery(
        self,
        session: Session,
        request: _ProductDiscoveryRequest,
        decision: NodeDecision,
        evaluated_at: datetime,
    ) -> TransitionResult:
        if isinstance(request, CompleteSpecificationAuthoring):
            return execute_complete_specification_authoring(
                session, request, decision, evaluated_at
            )
        if isinstance(request, DecideSpecification):
            prepared = self._revalidate_specification_acceptance(session, request)
            if isinstance(prepared, TransitionResult):
                return prepared
            return execute_decide_specification(
                session, prepared, decision, evaluated_at
            )
        assert_never(request)

    def _revalidate_specification_acceptance(  # noqa: PLR0911
        self,
        session: Session,
        request: DecideSpecification,
    ) -> DecideSpecification | TransitionResult:
        """Re-probe the exact candidate attempt immediately before acceptance."""
        if request.decision != "accepted":
            return request
        if request.repository_source_fingerprint is not None:
            return self._stale_specification_source(
                "Specification acceptance source identity is host-owned."
            )
        candidate = session.get(
            SpecificationCandidate,
            request.specification_candidate_id,
        )
        if (
            candidate is None
            or candidate.project_id != request.project_id
            or candidate.candidate_fingerprint != request.candidate_fingerprint
        ):
            return self._stale_specification_source(
                "The pending Specification candidate source is unavailable."
            )
        attempt = session.get(
            WorkflowNodeAttempt,
            candidate.workflow_node_attempt_id,
        )
        outcome = load_attempt_outcome(
            session,
            project_id=request.project_id,
            attempt_id=candidate.workflow_node_attempt_id,
        )
        if (
            attempt is None
            or attempt.project_id != request.project_id
            or attempt.node_id != "specification.author"
            or attempt.attempt_fingerprint != candidate.attempt_fingerprint
            or outcome is None
            or outcome.status != "success"
            or not self._attempt_input_matches(attempt)
        ):
            return self._stale_specification_source(
                "The pending Specification authoring attempt is stale."
            )
        try:
            persisted_input = _JSON_OBJECT.validate_json(
                attempt.normalized_input_json
            )
            contract = SpecificationAuthoringInput.model_validate(persisted_input)
        except (TypeError, ValueError):
            return self._stale_specification_source(
                "The pending Specification source input is invalid."
            )
        serialized_input = contract.model_dump(mode="json")
        if (
            contract.project_id != request.project_id
            or canonical_json(serialized_input) != attempt.normalized_input_json
            or specification_authoring_input_fingerprint(contract)
            != candidate.producer_input_fingerprint
        ):
            return self._stale_specification_source(
                "The pending Specification source input changed."
            )
        active_repository_source = next(
            (
                entry
                for entry in contract.source_manifest
                if entry.source_id == SPECIFICATION_ACTIVE_REPOSITORY_SOURCE_ID
            ),
            None,
        )
        prepared = request.model_copy(
            update={
                "repository_source_fingerprint": (
                    None
                    if active_repository_source is None
                    else active_repository_source.fingerprint
                )
            }
        )
        source_check = self._specification_source_check
        if source_check is None:
            if active_repository_source is not None:
                return self._stale_specification_source(
                    "Live Specification source validation is unavailable."
                )
        else:
            source_error = source_check(request.project_id, serialized_input)
            if source_error is not None:
                return TransitionResult(ok=False, error=source_error)
        return prepared

    @staticmethod
    def _stale_specification_source(message: str) -> TransitionResult:
        """Return one bounded rejection for an untrusted acceptance source."""
        return TransitionResult(
            ok=False,
            error=WorkflowError(
                code=WorkflowErrorCode.STALE_SPECIFICATION_INPUT,
                message=message,
            ),
        )

    @staticmethod
    def _dispatch_authority(
        session: Session,
        request: _AuthorityRequest,
        decision: NodeDecision,
        evaluated_at: datetime,
    ) -> TransitionResult:
        if isinstance(request, CompileAuthority):
            return execute_compile_authority(session, request, decision, evaluated_at)
        if isinstance(request, DecideAuthority):
            return execute_decide_authority(session, request, decision, evaluated_at)
        if isinstance(request, RecordAuthorityFeedback):
            return execute_record_authority_feedback(
                session, request, decision, evaluated_at
            )
        if isinstance(request, RepairAuthority):
            return execute_repair_authority(session, request, decision, evaluated_at)
        assert_never(request)

    @staticmethod
    def _dispatch_vision(
        session: Session,
        request: _VisionRequest,
        decision: NodeDecision,
        evaluated_at: datetime,
    ) -> TransitionResult:
        if isinstance(request, GenerateVisionBootstrap):
            return execute_generate_vision_bootstrap(
                session, request, decision, evaluated_at
            )
        if isinstance(request, RecordVisionInterviewTurn):
            return execute_record_vision_interview_turn(
                session, request, decision, evaluated_at
            )
        if isinstance(request, DecideVisionReview):
            return execute_decide_vision_review(
                session, request, decision, evaluated_at
            )
        if isinstance(request, BeginVisionRevision):
            return execute_begin_vision_revision(
                session, request, decision, evaluated_at
            )
        assert_never(request)

    @staticmethod
    def _dispatch_backlog(
        session: Session,
        request: _BacklogRequest,
        decision: NodeDecision,
        evaluated_at: datetime,
    ) -> TransitionResult:
        if isinstance(request, RecordBacklogDraft):
            return execute_record_backlog_draft(
                session, request, decision, evaluated_at
            )
        if isinstance(request, DecideBacklog):
            return execute_decide_backlog(session, request, decision, evaluated_at)
        assert_never(request)

    def _guarded_decision(
        self,
        session: Session,
        request: _PositionedTransitionRequest,
        evaluated_at: datetime,
    ) -> NodeDecision | TransitionResult:
        """Return the exact current decision or its failed position guard."""
        before = self._position_in_session(
            session,
            request.project_id,
            evaluated_at,
        )
        failure = self._guard_failure(request, before, evaluated_at)
        if failure is not None:
            return failure
        return self._available_decision(before, request)

    def _position_in_session(
        self,
        session: Session,
        project_id: int,
        evaluated_at: datetime,
    ) -> WorkflowPosition:
        """Evaluate complete typed facts within the caller-owned session."""
        snapshot = WorkflowFactRepository(session).load(project_id)
        return self._graph.evaluate(snapshot, evaluated_at)

    def _guard_failure(
        self,
        request: PositionedRequest,
        position: WorkflowPosition,
        evaluated_at: datetime,
    ) -> TransitionResult | None:
        """Validate graph, full facts, exact decision, and validity window."""
        stale_message: str | None = None
        if request.graph_version != position.graph_version:
            stale_message = "The workflow graph version changed."
        elif request.fact_fingerprint != position.fact_fingerprint:
            stale_message = "The complete Project facts changed."
        decision = self._decision(position, request)
        if stale_message is None and decision is not None:
            if request.decision_fingerprint != decision.decision_fingerprint:
                stale_message = "The exact node decision changed."
            elif (
                decision.valid_until is not None
                and evaluated_at >= decision.valid_until
            ):
                stale_message = "The node decision expired."
        if stale_message is not None:
            return self._stale(position, stale_message)
        if decision is not None and decision.request_kind != request.kind:
            return self._fact_conflict(
                "The available node does not accept this request kind.",
                position=position,
            )
        return None

    def _available_decision(
        self,
        position: WorkflowPosition,
        request: PositionedRequest,
    ) -> NodeDecision | TransitionResult:
        """Require the exact node instance to be currently available."""
        decision = self._decision(position, request)
        if decision is None:
            return TransitionResult(
                ok=False,
                position=position,
                error=WorkflowError(
                    code=WorkflowErrorCode.TRANSITION_NOT_AVAILABLE,
                    message="The requested node instance is not available.",
                ),
            )
        if decision.category is NodeCategory.INVALID:
            return self._fact_conflict(
                "The requested node is invalid for the current Project facts.",
                position=position,
            )
        human_review_waiting = isinstance(
            request,
            DecideAuthority
            | DecideVisionReview
            | DecideBacklog
            | DecideRoadmap
            | DecideStory
            | DecideSprintPlan
            | DecideProductGoalReview
            | DecideSpecification
            | ReviewSprint,
        ) and (decision.category is NodeCategory.WAITING)
        if decision.category is not NodeCategory.AVAILABLE and not human_review_waiting:
            return TransitionResult(
                ok=False,
                position=position,
                error=WorkflowError(
                    code=WorkflowErrorCode.TRANSITION_NOT_AVAILABLE,
                    message="The requested node is not currently available.",
                    blockers=decision.blockers,
                ),
            )
        return decision

    @staticmethod
    def _decision(
        position: WorkflowPosition,
        request: PositionedRequest,
    ) -> NodeDecision | None:
        """Find one decision by the exact stable node and instance pair."""
        node_id = request.decision_node_id()
        instance_key = request.decision_instance_key()
        return next(
            (
                decision
                for decision in position.decisions
                if decision.node_id == node_id and decision.instance_key == instance_key
            ),
            None,
        )

    @staticmethod
    def _complete_receipt(
        session: Session,
        receipt: WorkflowTransitionReceipt,
        result: TransitionResult,
        evaluated_at: datetime,
    ) -> None:
        """Persist the immutable result before the transaction commit."""
        receipt.result_json = canonical_json(result.model_dump(mode="json"))
        receipt.completed_at = evaluated_at
        session.add(receipt)
        session.flush()

    @staticmethod
    def _replay_attempt_start_receipt(
        session: Session,
        attempt: WorkflowNodeAttempt,
    ) -> TransitionResult:
        """Return terminal attempt truth without rewriting its start receipt."""
        receipt = session.exec(
            select(WorkflowTransitionReceipt).where(
                col(WorkflowTransitionReceipt.request_kind) == "start_node_attempt",
                col(WorkflowTransitionReceipt.idempotency_key)
                == attempt.idempotency_key,
            )
        ).one_or_none()
        if (
            receipt is None
            or receipt.result_json is None
            or receipt.completed_at is None
        ):
            msg = "A terminal node attempt has no completed start receipt."
            raise RuntimeError(msg)
        persisted = TransitionResult.model_validate_json(receipt.result_json)
        return persisted.model_copy(update={"replayed": True})

    @staticmethod
    def _complete_attempt_start_receipt(
        session: Session,
        attempt: WorkflowNodeAttempt,
        result: TransitionResult,
        evaluated_at: datetime,
    ) -> None:
        """Persist the terminal command outcome under the transport start key."""
        receipt = session.exec(
            select(WorkflowTransitionReceipt).where(
                col(WorkflowTransitionReceipt.request_kind) == "start_node_attempt",
                col(WorkflowTransitionReceipt.idempotency_key)
                == attempt.idempotency_key,
            )
        ).one_or_none()
        if receipt is None:
            msg = "A durable node attempt has no start transition receipt."
            raise RuntimeError(msg)
        receipt.result_json = canonical_json(result.model_dump(mode="json"))
        receipt.completed_at = evaluated_at
        session.add(receipt)
        session.flush()

    @staticmethod
    def _stale(position: WorkflowPosition, message: str) -> TransitionResult:
        """Return the newly derived position for a stale guarded request."""
        return TransitionResult(
            ok=False,
            position=position,
            error=WorkflowError(
                code=WorkflowErrorCode.STALE_POSITION,
                message=message,
            ),
        )

    @staticmethod
    def _fact_conflict(
        message: str,
        *,
        position: WorkflowPosition | None = None,
    ) -> TransitionResult:
        """Return a typed workflow-fact conflict without mutation."""
        return TransitionResult(
            ok=False,
            position=position,
            error=WorkflowError(
                code=WorkflowErrorCode.WORKFLOW_FACT_CONFLICT,
                message=message,
            ),
        )

    def _is_sqlite_lock_timeout(self, error: OperationalError) -> bool:
        """Recognize finite SQLite busy-timeout exhaustion."""
        if self._engine.dialect.name != "sqlite":
            return False
        message = str(error).lower()
        return any(marker in message for marker in _SQLITE_LOCK_MESSAGES)
