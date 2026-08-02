"""Guarded transactional entry point for the domain workflow graph."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, assert_never

from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, col, select

from models.workflow import WorkflowTransitionReceipt
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from workflow.contracts import (
    NodeCategory,
    NodeDecision,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
    WorkflowPosition,
)
from workflow.fingerprints import canonical_hash, canonical_json
from workflow.handlers import (
    execute_abandon_project_shell,
    execute_compile_authority,
    execute_decide_authority,
    execute_decide_backlog,
    execute_decide_brownfield_initial_spec,
    execute_decide_initial_spec_draft,
    execute_decide_prd,
    execute_decide_vision,
    execute_execution_request,
    execute_open_project_shell,
    execute_planning_request,
    execute_reconcile_backlog,
    execute_record_authority_feedback,
    execute_record_backlog_draft,
    execute_record_brownfield_spec_draft,
    execute_record_challenge_artifact,
    execute_record_initial_spec_draft,
    execute_record_prd_version,
    execute_record_repository_baseline,
    execute_record_repository_inventory,
    execute_record_vision_draft,
    execute_register_initial_scope,
    execute_repair_authority,
    execute_scope_extension_request,
    validate_decide_authority_review,
    validate_decide_backlog_review,
    validate_decide_vision_review,
    validate_planning_review,
)
from workflow.requests import (
    AbandonProjectShell,
    AbandonScopeExtension,
    ApplyStoryDependencies,
    CloseSprint,
    CloseStory,
    CompileAuthority,
    CompleteTask,
    DecideAmendmentSpecDraft,
    DecideAuthority,
    DecideBacklog,
    DecideBrownfieldInitialSpec,
    DecideExtensionPrd,
    DecideInitialSpecDraft,
    DecidePrd,
    DecideRoadmap,
    DecideSprintPlan,
    DecideStory,
    DecideVision,
    OpenProjectShell,
    ReconcileBacklog,
    ReconcileScopeExtension,
    RecordAmendmentSpecDraft,
    RecordAuthorityFeedback,
    RecordBacklogDraft,
    RecordBrownfieldSpecDraft,
    RecordChallengeArtifact,
    RecordExtensionChallenge,
    RecordExtensionPrd,
    RecordInitialSpecDraft,
    RecordPostSprintTriage,
    RecordPrdVersion,
    RecordRepositoryBaseline,
    RecordRepositoryInventory,
    RecordRoadmapDraft,
    RecordSprintPlan,
    RecordStoryDraft,
    RecordVisionDraft,
    RegisterInitialScope,
    RegisterScopeExtension,
    RepairAuthority,
    RepairStoryReadiness,
    ReviewSprint,
    StartScopeExtension,
    StartSprint,
    TransitionRequest,
)

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

    from sqlalchemy.engine import Engine

    from workflow.clock import Clock
    from workflow.graph import WorkflowGraph
    from workflow.requests.base import PositionedRequest

_SQLITE_BUSY_TIMEOUT_MS = 1_000
_SQLITE_LOCK_MESSAGES = ("database is locked", "database table is locked")

type _ExistingPositionedRequest = (
    AbandonProjectShell
    | RecordChallengeArtifact
    | RecordPrdVersion
    | DecidePrd
    | RecordInitialSpecDraft
    | DecideInitialSpecDraft
    | RegisterInitialScope
)
type _AuthorityRequest = (
    CompileAuthority | DecideAuthority | RecordAuthorityFeedback | RepairAuthority
)
type _ProductDefinitionRequest = (
    RecordVisionDraft
    | DecideVision
    | RecordBacklogDraft
    | DecideBacklog
    | ReconcileBacklog
)
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
type _ScopeExtensionRequest = (
    StartScopeExtension
    | RecordExtensionChallenge
    | RecordExtensionPrd
    | DecideExtensionPrd
    | RecordAmendmentSpecDraft
    | DecideAmendmentSpecDraft
    | RegisterScopeExtension
    | ReconcileScopeExtension
    | AbandonScopeExtension
)
type _PositionedTransitionRequest = (
    _ExistingPositionedRequest
    | RecordRepositoryBaseline
    | RecordRepositoryInventory
    | RecordBrownfieldSpecDraft
    | DecideBrownfieldInitialSpec
    | CompileAuthority
    | DecideAuthority
    | RecordAuthorityFeedback
    | RepairAuthority
    | RecordVisionDraft
    | DecideVision
    | RecordBacklogDraft
    | DecideBacklog
    | ReconcileBacklog
    | _PlanningRequest
    | _ExecutionRequest
    | _ScopeExtensionRequest
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

    def __init__(self, *, engine: Engine, graph: WorkflowGraph, clock: Clock) -> None:
        """Retain explicit persistence, graph, and time dependencies."""
        self._engine = engine
        self._graph = graph
        self._clock = clock
        self._configure_busy_timeout()

    def position(self, project_id: int) -> WorkflowPosition:
        """Derive one position from complete durable facts and the injected clock."""
        evaluated_at = self._clock.now()
        with Session(self._engine) as session:
            return self._position_in_session(session, project_id, evaluated_at)

    def transition(self, request: TransitionRequest) -> TransitionResult:
        """Guard and apply one request inside its receipt transaction."""
        evaluated_at = self._clock.now()
        with Session(self._engine) as session:
            try:
                result = self._transition_in_session(session, request, evaluated_at)
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
                    RegisterInitialScope
                    | RegisterScopeExtension
                    | ReconcileScopeExtension
                    | CompleteTask
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
            | DecideVision
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
            elif isinstance(request, DecideVision):
                review_failure = validate_decide_vision_review(session, request)
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
        request_hash = canonical_hash(request_payload)

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

    @staticmethod
    def _existing_receipt_claim(
        session: Session,
        request: TransitionRequest,
    ) -> _ReceiptClaim | None:
        """Return the immutable result for an existing idempotency key."""
        request_hash = canonical_hash(request.model_dump(mode="json"))
        receipt = session.exec(
            select(WorkflowTransitionReceipt).where(
                col(WorkflowTransitionReceipt.request_kind) == request.kind,
                col(WorkflowTransitionReceipt.idempotency_key)
                == request.idempotency_key,
            )
        ).one_or_none()
        if receipt is None:
            return None
        if receipt.request_fingerprint != request_hash:
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

    def _execute_request(
        self,
        session: Session,
        request: TransitionRequest,
        evaluated_at: datetime,
    ) -> TransitionResult:
        """Dispatch only after the receipt claim and all position guards."""
        if isinstance(request, OpenProjectShell):
            return execute_open_project_shell(
                session,
                request,
                self._graph,
                evaluated_at,
            )
        return self._execute_positioned(session, request, evaluated_at)

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

        result = self._execute_execution_or_scope(
            session,
            request,
            decision_or_failure,
            evaluated_at,
        )
        if result is None:
            result = self._execute_prior_positioned(
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

    def _execute_prior_positioned(
        self,
        session: Session,
        request: _PositionedTransitionRequest,
        decision_or_failure: NodeDecision,
        evaluated_at: datetime,
    ) -> TransitionResult:
        """Dispatch positioned request families implemented before Tasks 12-13."""
        if isinstance(
            request,
            CompleteTask
            | CloseStory
            | ReviewSprint
            | CloseSprint
            | RecordPostSprintTriage
            | StartScopeExtension
            | RecordExtensionChallenge
            | RecordExtensionPrd
            | DecideExtensionPrd
            | RecordAmendmentSpecDraft
            | DecideAmendmentSpecDraft
            | RegisterScopeExtension
            | ReconcileScopeExtension
            | AbandonScopeExtension,
        ):
            message = "A recent request reached the prior-family dispatcher."
            raise TypeError(message)
        if isinstance(
            request,
            CompileAuthority
            | DecideAuthority
            | RecordAuthorityFeedback
            | RepairAuthority,
        ):
            result = self._execute_authority_request(
                session,
                request,
                decision_or_failure,
                evaluated_at,
            )
        elif isinstance(
            request,
            RecordVisionDraft
            | DecideVision
            | RecordBacklogDraft
            | DecideBacklog
            | ReconcileBacklog,
        ):
            result = self._execute_product_definition_request(
                session,
                request,
                decision_or_failure,
                evaluated_at,
            )
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
            result = execute_planning_request(
                session,
                request,
                decision_or_failure,
                evaluated_at,
            )
        elif isinstance(request, RecordRepositoryBaseline):
            result = execute_record_repository_baseline(
                session,
                request,
                decision_or_failure,
                evaluated_at,
            )
        elif isinstance(request, RecordRepositoryInventory):
            result = execute_record_repository_inventory(
                session,
                request,
                decision_or_failure,
                evaluated_at,
            )
        elif isinstance(request, RecordBrownfieldSpecDraft):
            result = execute_record_brownfield_spec_draft(
                session,
                request,
                decision_or_failure,
                evaluated_at,
            )
        elif isinstance(request, DecideBrownfieldInitialSpec):
            result = execute_decide_brownfield_initial_spec(
                session,
                request,
                decision_or_failure,
                evaluated_at,
            )
        elif isinstance(
            request,
            AbandonProjectShell
            | RecordChallengeArtifact
            | RecordPrdVersion
            | DecidePrd
            | RecordInitialSpecDraft
            | DecideInitialSpecDraft
            | RegisterInitialScope,
        ):
            result = self._execute_existing_positioned(
                session,
                request,
                decision_or_failure,
                evaluated_at,
            )
        else:
            assert_never(request)
        return result

    @staticmethod
    def _execute_execution_or_scope(
        session: Session,
        request: _PositionedTransitionRequest,
        decision: NodeDecision,
        evaluated_at: datetime,
    ) -> TransitionResult | None:
        """Execute recent execution and scope-extension request families."""
        if isinstance(
            request,
            CompleteTask
            | CloseStory
            | ReviewSprint
            | CloseSprint
            | RecordPostSprintTriage,
        ):
            return execute_execution_request(
                session,
                request,
                decision,
                evaluated_at,
            )
        if isinstance(
            request,
            StartScopeExtension
            | RecordExtensionChallenge
            | RecordExtensionPrd
            | DecideExtensionPrd
            | RecordAmendmentSpecDraft
            | DecideAmendmentSpecDraft
            | RegisterScopeExtension
            | ReconcileScopeExtension
            | AbandonScopeExtension,
        ):
            return execute_scope_extension_request(
                session,
                request,
                decision,
                evaluated_at,
            )
        return None

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

    @staticmethod
    def _execute_authority_request(
        session: Session,
        request: _AuthorityRequest,
        decision: NodeDecision,
        evaluated_at: datetime,
    ) -> TransitionResult:
        """Dispatch the four closed authority transition variants."""
        if isinstance(request, CompileAuthority):
            return execute_compile_authority(
                session,
                request,
                decision,
                evaluated_at,
            )
        if isinstance(request, DecideAuthority):
            return execute_decide_authority(
                session,
                request,
                decision,
                evaluated_at,
            )
        if isinstance(request, RecordAuthorityFeedback):
            return execute_record_authority_feedback(
                session,
                request,
                decision,
                evaluated_at,
            )
        if isinstance(request, RepairAuthority):
            return execute_repair_authority(
                session,
                request,
                decision,
                evaluated_at,
            )
        assert_never(request)

    @staticmethod
    def _execute_product_definition_request(
        session: Session,
        request: _ProductDefinitionRequest,
        decision: NodeDecision,
        evaluated_at: datetime,
    ) -> TransitionResult:
        """Dispatch the five closed Vision and Backlog transition variants."""
        if isinstance(request, RecordVisionDraft):
            return execute_record_vision_draft(
                session,
                request,
                decision,
                evaluated_at,
            )
        if isinstance(request, DecideVision):
            return execute_decide_vision(session, request, decision, evaluated_at)
        if isinstance(request, RecordBacklogDraft):
            return execute_record_backlog_draft(
                session,
                request,
                decision,
                evaluated_at,
            )
        if isinstance(request, DecideBacklog):
            return execute_decide_backlog(session, request, decision, evaluated_at)
        if isinstance(request, ReconcileBacklog):
            return execute_reconcile_backlog(session, request, decision, evaluated_at)
        assert_never(request)

    @staticmethod
    def _execute_existing_positioned(
        session: Session,
        request: _ExistingPositionedRequest,
        decision: NodeDecision,
        evaluated_at: datetime,
    ) -> TransitionResult:
        """Dispatch established shell and greenfield requests."""
        if isinstance(request, AbandonProjectShell):
            result = execute_abandon_project_shell(
                session,
                request,
                decision,
                evaluated_at,
            )
        elif isinstance(request, RecordChallengeArtifact):
            result = execute_record_challenge_artifact(
                session,
                request,
                decision,
                evaluated_at,
            )
        elif isinstance(request, RecordPrdVersion):
            result = execute_record_prd_version(
                session,
                request,
                decision,
                evaluated_at,
            )
        elif isinstance(request, DecidePrd):
            result = execute_decide_prd(
                session,
                request,
                decision,
                evaluated_at,
            )
        elif isinstance(request, RecordInitialSpecDraft):
            result = execute_record_initial_spec_draft(
                session,
                request,
                decision,
                evaluated_at,
            )
        elif isinstance(request, DecideInitialSpecDraft):
            result = execute_decide_initial_spec_draft(
                session,
                request,
                decision,
                evaluated_at,
            )
        elif isinstance(request, RegisterInitialScope):
            result = execute_register_initial_scope(
                session,
                request,
                decision,
                evaluated_at,
            )
        else:
            assert_never(request)
        return result

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
            | DecideVision
            | DecideBacklog
            | DecideRoadmap
            | DecideStory
            | DecideSprintPlan
            | DecideExtensionPrd
            | DecideAmendmentSpecDraft
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
