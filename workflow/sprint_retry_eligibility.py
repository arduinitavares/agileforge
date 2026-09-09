"""Pure eligibility checks for a guarded retry of one exact Sprint."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from workflow.definitions.product_discovery import select_product_definition_state
from workflow.execution_integrity import (
    ExecutionIntegrityError,
    execution_contract,
    selected_story_dependency_snapshot,
)
from workflow.execution_scope import ExecutionScopeError, current_execution_scope
from workflow.fingerprints import business_fact_fingerprint

if TYPE_CHECKING:
    from workflow.facts import WorkflowFactSnapshot


def _as_utc(value: datetime) -> datetime:
    """Normalize SQLite timestamps without importing the handler package."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True)
class SprintRetryEligibility:
    """Exact retry target, preserved contract, and named blockers."""

    sprint_id: int
    predecessor_retry_attempt_id: int | None
    next_ordinal: int | None
    story_ids: tuple[int, ...]
    task_ids: tuple[int, ...]
    contract_fingerprint: str | None
    blockers: tuple[SprintRetryBlocker, ...]


@dataclass(frozen=True)
class SprintRetryBlocker:
    """Small typed explanation of one exact retry eligibility failure."""

    code: str
    reason: str
    subject_type: str
    subject_id: int | None


def sprint_retry_eligibility_payload(
    eligibility: SprintRetryEligibility,
) -> dict[str, object]:
    """Return a canonical, transport-safe representation of eligibility."""
    return asdict(eligibility)


def source_sprint_contract_is_current(
    snapshot: WorkflowFactSnapshot, *, sprint_id: int
) -> bool:
    """Prove current selected requirements still match the immutable start."""
    starts = tuple(
        item for item in snapshot.sprint_starts if item.sprint_id == sprint_id
    )
    if len(starts) != 1:
        return False
    start = starts[0]
    product_definition = select_product_definition_state(snapshot)
    current_spec = product_definition.accepted_spec
    if (
        product_definition.has_conflict
        or current_spec is None
        or current_spec.spec_version_id != start.spec_version_id
        or current_spec.spec_hash != start.spec_hash
    ):
        return False
    try:
        live_dependencies = selected_story_dependency_snapshot(
            snapshot,
            start.selected_story_ids,
            source_fingerprint=start.dependency_source_fingerprint,
        )
    except ExecutionIntegrityError:
        return False
    selected_ids = frozenset(start.selected_story_ids)
    selected = tuple(item for item in snapshot.stories if item.story_id in selected_ids)
    requirements_match = len(selected) == len(selected_ids) and all(
        not item.is_superseded
        and item.content_accepted
        and item.accepted_spec_version_id == start.spec_version_id
        and item.accepted_spec_hash == start.spec_hash
        for item in selected
    )
    return (
        requirements_match
        and live_dependencies.source_fingerprint == start.dependency_source_fingerprint
        and live_dependencies.dependency_fingerprint == start.dependency_fingerprint
        and live_dependencies.rows_fingerprint == start.dependency_rows_fingerprint
        and live_dependencies.rows == start.dependency_rows_snapshot
    )


def retry_start_contract_is_current(
    snapshot: WorkflowFactSnapshot,
    *,
    sprint_id: int,
    retry_contract_fingerprint: str,
) -> bool:
    """Return whether a planned retry still has the exact source contract."""
    if not source_sprint_contract_is_current(snapshot, sprint_id=sprint_id):
        return False
    try:
        current_contract = execution_contract(snapshot, sprint_id)
    except ExecutionIntegrityError:
        return False
    return current_contract.fingerprint == retry_contract_fingerprint


def retry_start_authority_is_current(
    snapshot: WorkflowFactSnapshot,
    *,
    sprint_id: int,
    retry_attempt_id: int | None,
    retry_contract_fingerprint: str,
) -> bool:
    """Prove that one exact planned retry still owns the current scope."""
    if retry_attempt_id is None or not retry_start_contract_is_current(
        snapshot,
        sprint_id=sprint_id,
        retry_contract_fingerprint=retry_contract_fingerprint,
    ):
        return False
    try:
        scope = current_execution_scope(snapshot)
    except ExecutionScopeError:
        return False
    return (
        scope is not None
        and scope.sprint_id == sprint_id
        and scope.retry_attempt_id == retry_attempt_id
        and scope.status == "planned"
    )


def evaluate_sprint_retry_eligibility(  # noqa: C901, PLR0912, PLR0915
    snapshot: WorkflowFactSnapshot, *, sprint_id: int
) -> SprintRetryEligibility:
    """Evaluate one requested Sprint without choosing a substitute target."""
    blockers: list[SprintRetryBlocker] = []
    add = blockers.append
    source = tuple(item for item in snapshot.sprints if item.sprint_id == sprint_id)
    if len(source) != 1 or source[0].status != "completed":
        add(
            SprintRetryBlocker(
                "SPRINT_NOT_COMPLETED",
                "The requested Sprint is not completed.",
                "sprint",
                sprint_id,
            )
        )
    try:
        contract = execution_contract(snapshot, sprint_id)
    except ExecutionIntegrityError:
        contract = None
        add(
            SprintRetryBlocker(
                "SOURCE_CONTRACT_INVALID",
                "The requested Sprint has no valid immutable execution contract.",
                "sprint",
                sprint_id,
            )
        )
    if contract is not None and not source_sprint_contract_is_current(
        snapshot, sprint_id=sprint_id
    ):
        add(
            SprintRetryBlocker(
                "SOURCE_CONTRACT_CHANGED",
                "Selected requirements or dependency rows changed after Sprint start.",
                "sprint",
                sprint_id,
            )
        )
    retries = tuple(
        item for item in snapshot.sprint_retries if item.sprint_id == sprint_id
    )
    if any(item.status in {"planned", "active"} for item in snapshot.sprint_retries):
        live = next(
            item
            for item in snapshot.sprint_retries
            if item.status in {"planned", "active"}
        )
        add(
            SprintRetryBlocker(
                "LIVE_RETRY_EXISTS",
                "A planned or active retry must finish before another retry starts.",
                "retry_attempt",
                live.retry_attempt_id,
            )
        )
    if any(item.status in {"planned", "active"} for item in snapshot.sprints):
        live_sprint = next(
            item for item in snapshot.sprints if item.status in {"planned", "active"}
        )
        add(
            SprintRetryBlocker(
                "LIVE_SPRINT_EXISTS",
                "A later Sprint is still planned or active.",
                "sprint",
                live_sprint.sprint_id,
            )
        )
    try:
        current = current_execution_scope(snapshot)
    except ExecutionScopeError:
        current = None
        add(
            SprintRetryBlocker(
                "EXECUTION_SCOPE_INVALID",
                "Current execution scope is ambiguous or invalid.",
                "project",
                snapshot.project.project_id,
            )
        )
    if (
        current is None
        or current.sprint_id != sprint_id
        or current.status != "completed"
    ):
        add(
            SprintRetryBlocker(
                "SPRINT_NOT_CURRENT",
                "The requested Sprint is not the latest eligible completed scope.",
                "sprint",
                sprint_id if current is None else current.sprint_id,
            )
        )
    if current is not None and not current.post_sprint_triage:
        add(
            SprintRetryBlocker(
                "TRIAGE_UNRESOLVED",
                "The requested Sprint still requires valid post-Sprint triage.",
                "sprint",
                sprint_id,
            )
        )
    if snapshot.incomplete_transitions:
        receipt = min(
            snapshot.incomplete_transitions,
            key=lambda item: -1 if item.receipt_id is None else item.receipt_id,
        )
        add(
            SprintRetryBlocker(
                "INCOMPLETE_TRANSITION",
                "A pending transition receipt must resolve before retry.",
                "transition_receipt",
                receipt.receipt_id,
            )
        )
    terminal_markers = tuple(
        item.completed_at
        for item in (
            *source,
            *(item for item in retries if item.status == "completed"),
        )
        if item.completed_at is not None
    )
    target_completed_at = max(map(_as_utc, terminal_markers), default=None)
    current_business_identity = business_fact_fingerprint(snapshot)
    attempts = {item.attempt_id: item for item in snapshot.node_attempts}
    guards = {item.attempt_id: item for item in snapshot.sprint_plan_generation_guards}

    def is_relevant(attempt_id: int) -> bool:
        """Keep historical provider noise out of a current retry decision."""
        guard = guards[attempt_id]
        attempt = attempts.get(attempt_id)
        return (
            attempt is None
            or attempt.business_fact_fingerprint == current_business_identity
            or target_completed_at is None
            or _as_utc(guard.started_at) >= target_completed_at
        )

    def is_proven_recovery(
        attempt_id: int, *, recovery_attempt_id: int | None = None
    ) -> bool:
        """Require exact identity plus canonical durable output for recovery."""
        attempt = attempts.get(attempt_id)
        if attempt is None:
            return False
        origin = guards.get(attempt_id)
        if origin is None:
            return False
        return any(
            (recovery_attempt_id is None or recovery.attempt_id == recovery_attempt_id)
            and recovery.node_id == attempt.node_id
            and recovery.instance_key == attempt.instance_key
            and recovery.business_fact_fingerprint == attempt.business_fact_fingerprint
            and recovery.input_fingerprint == attempt.input_fingerprint
            and recovery.outcome == "success"
            and guards[recovery.attempt_id].integrity == "linked"
            and _as_utc(guards[recovery.attempt_id].started_at)
            > _as_utc(origin.outcome_recorded_at or origin.started_at)
            and guards[recovery.attempt_id].generated_plan_artifact_id
            not in {
                item.artifact_id
                for item in snapshot.planning_artifacts
                if item.artifact_type == "sprint_plan"
                and item.activated_sprint_id == sprint_id
            }
            for recovery in snapshot.node_attempts
            if recovery.attempt_id in guards
        )

    def provider_recovery_is_proven(attempt_id: int) -> bool:
        """Require later canonical output for the same provider operation."""
        origin = next(
            (
                item
                for item in snapshot.provider_generation_guards
                if item.attempt_id == attempt_id
            ),
            None,
        )
        if origin is None or origin.outcome_recorded_at is None:
            return False
        return any(
            recovery.node_id == origin.node_id
            and recovery.instance_key == origin.instance_key
            and recovery.business_fact_fingerprint == origin.business_fact_fingerprint
            and recovery.input_fingerprint == origin.input_fingerprint
            and recovery.outcome == "success"
            and recovery.integrity == "canonical"
            and _as_utc(recovery.started_at) > _as_utc(origin.outcome_recorded_at)
            for recovery in snapshot.provider_generation_guards
        )

    for provider in snapshot.provider_generation_guards:
        if provider.business_fact_fingerprint != current_business_identity:
            continue
        if provider.outcome in {None, "failure", "obsolete"}:
            if provider_recovery_is_proven(provider.attempt_id):
                continue
            add(
                SprintRetryBlocker(
                    "UNRESOLVED_PROVIDER_GENERATION",
                    "A current provider generation attempt has not recovered.",
                    "workflow_node_attempt",
                    provider.attempt_id,
                )
            )
        elif provider.integrity != "canonical":
            add(
                SprintRetryBlocker(
                    "UNRESOLVED_PROVIDER_GENERATION",
                    "A current provider generation output has invalid provenance.",
                    "workflow_node_attempt",
                    provider.attempt_id,
                )
            )

    for guard in snapshot.sprint_plan_generation_guards:
        if not is_relevant(guard.attempt_id):
            continue
        attempt = attempts.get(guard.attempt_id)
        if attempt is None:
            code = "SPRINT_PLAN_GENERATION_UNRESOLVED"
            reason = "A Sprint-plan generation guard has no exact attempt identity."
        elif guard.outcome is None:
            code = "IN_FLIGHT_SPRINT_PLAN_GENERATION"
            reason = "A relevant Sprint-plan generation attempt is still in flight."
        elif guard.outcome in {"failure", "obsolete"}:
            if is_proven_recovery(guard.attempt_id):
                continue
            code = "UNRESOLVED_SPRINT_PLAN_GENERATION_FAILURE"
            reason = "A relevant Sprint-plan generation failure has not been recovered."
        elif guard.integrity == "linked":
            code = "LATER_SPRINT_PLAN_GENERATION"
            reason = "A relevant Sprint-plan generation produced a competing plan."
        elif guard.integrity in {"unlinked", "malformed"}:
            code = "SPRINT_PLAN_GENERATION_UNRESOLVED"
            reason = "A relevant Sprint-plan generation has unresolved provenance."
        else:
            continue
        add(
            SprintRetryBlocker(
                code,
                reason,
                "workflow_node_attempt",
                guard.attempt_id,
            )
        )
    predecessor = max(retries, key=lambda item: item.ordinal, default=None)
    ordinal = 2 if predecessor is None else predecessor.ordinal + 1
    return SprintRetryEligibility(
        sprint_id=sprint_id,
        predecessor_retry_attempt_id=(
            None if predecessor is None else predecessor.retry_attempt_id
        ),
        next_ordinal=ordinal,
        story_ids=()
        if contract is None
        else tuple(item.story_id for item in contract.stories),
        task_ids=()
        if contract is None
        else tuple(item.task_id for item in contract.tasks),
        contract_fingerprint=None if contract is None else contract.fingerprint,
        blockers=tuple(dict.fromkeys(blockers)),
    )
