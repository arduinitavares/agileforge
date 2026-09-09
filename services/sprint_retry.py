"""Read-only retry preview and atomic retry/start persistence operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlmodel import Session, col, select

from models.repository import RepositoryBinding, repository_binding_fingerprint
from models.sprint_retry import (
    SprintRetryAttempt,
    SprintRetryStart,
    SprintRetryStoryState,
    SprintRetryTaskState,
)
from workflow.fingerprints import canonical_hash
from workflow.sprint_retry_eligibility import (
    SprintRetryBlocker,
    SprintRetryEligibility,
    evaluate_sprint_retry_eligibility,
    source_sprint_contract_is_current,
    sprint_retry_eligibility_payload,
)

if TYPE_CHECKING:
    from datetime import datetime

    from workflow.facts import WorkflowFactSnapshot
    from workflow.requests.sprint_retry import RetrySprint, StartSprintRetry


@dataclass(frozen=True)
class SprintRetryPreview:
    """Frozen retry display and mutation binding for one exact Sprint."""

    project_id: int
    sprint_id: int
    predecessor_retry_attempt_id: int | None
    next_ordinal: int | None
    story_ids: tuple[int, ...]
    task_ids: tuple[int, ...]
    preserved_history: str
    repository_provenance: dict[str, object] | None
    blockers: tuple[SprintRetryBlocker, ...]
    expected_state_fingerprint: str


@dataclass(frozen=True)
class SprintRetryResult:
    """Durable retry identity and lifecycle state returned by a transition."""

    retry_attempt_id: int
    status: str


def _repository_provenance(
    session: Session,
    snapshot: WorkflowFactSnapshot,
) -> dict[str, object] | None:
    """Load display provenance from persisted rows without caller autoflush."""
    binding_id = snapshot.project.active_repository_binding_id
    if binding_id is None:
        return None
    with session.no_autoflush:
        binding = session.exec(
            select(RepositoryBinding).where(
                col(RepositoryBinding.repository_binding_id) == binding_id
            ),
            execution_options={"autoflush": False, "identity_token": object()},
        ).one_or_none()
    if binding is None or binding.project_id != snapshot.project.project_id:
        return {"state": "invalid", "repository_binding_id": binding_id}
    return {
        "state": "bound",
        "repository_binding_id": binding_id,
        "worktree_path": binding.worktree_path,
        "branch_name": binding.branch_name,
        "head_sha": binding.head_sha,
        "fingerprint": repository_binding_fingerprint(binding),
    }


def _preview_fingerprint(
    eligibility: SprintRetryEligibility,
    snapshot: WorkflowFactSnapshot,
    repository_provenance: dict[str, object] | None,
) -> str:
    target_id = eligibility.sprint_id
    return canonical_hash(
        {
            "project_id": snapshot.project.project_id,
            "eligibility": sprint_retry_eligibility_payload(eligibility),
            "repository_provenance": repository_provenance,
            "generation_guards": sorted(
                (
                    item.model_dump(mode="json")
                    for item in snapshot.sprint_plan_generation_guards
                ),
                key=canonical_hash,
            ),
            "provider_generation_guards": sorted(
                (
                    item.model_dump(mode="json")
                    for item in snapshot.provider_generation_guards
                ),
                key=canonical_hash,
            ),
            "incomplete_transitions": sorted(
                (
                    item.model_dump(mode="json")
                    for item in snapshot.incomplete_transitions
                ),
                key=canonical_hash,
            ),
            "target_lifecycle": {
                "sprints": [
                    item.model_dump(mode="json")
                    for item in snapshot.sprints
                    if item.sprint_id == target_id
                ],
                "starts": [
                    item.model_dump(mode="json")
                    for item in snapshot.sprint_starts
                    if item.sprint_id == target_id
                ],
                "reviews": [
                    item.model_dump(mode="json")
                    for item in snapshot.sprint_reviews
                    if item.sprint_id == target_id
                ],
                "closures": [
                    item.model_dump(mode="json")
                    for item in snapshot.sprint_closures
                    if item.sprint_id == target_id
                ],
                "triage": [
                    item.model_dump(mode="json")
                    for item in snapshot.post_sprint_triage
                    if item.sprint_id == target_id
                ],
                "retries": [
                    item.model_dump(mode="json")
                    for item in snapshot.sprint_retries
                    if item.sprint_id == target_id
                ],
            },
        }
    )


def build_sprint_retry_preview(
    session: Session, *, snapshot: WorkflowFactSnapshot, sprint_id: int
) -> SprintRetryPreview:
    """Build a read-only exact-target preview from the caller-owned snapshot."""
    eligibility = evaluate_sprint_retry_eligibility(snapshot, sprint_id=sprint_id)
    provenance = _repository_provenance(session, snapshot)
    return SprintRetryPreview(
        project_id=snapshot.project.project_id,
        sprint_id=sprint_id,
        predecessor_retry_attempt_id=eligibility.predecessor_retry_attempt_id,
        next_ordinal=eligibility.next_ordinal,
        story_ids=eligibility.story_ids,
        task_ids=eligibility.task_ids,
        preserved_history="Original execution evidence remains immutable.",
        repository_provenance=provenance,
        blockers=eligibility.blockers,
        expected_state_fingerprint=_preview_fingerprint(
            eligibility, snapshot, provenance
        ),
    )


def retry_sprint_in_session(
    session: Session,
    *,
    request: RetrySprint,
    snapshot: WorkflowFactSnapshot,
    now: datetime,
) -> SprintRetryResult:
    """Insert one planned retry and exact To Do state after a preview recheck."""
    preview = build_sprint_retry_preview(
        session, snapshot=snapshot, sprint_id=request.sprint_id
    )
    if (
        preview.blockers
        or request.expected_state_fingerprint != preview.expected_state_fingerprint
    ):
        message = "Sprint retry preview is stale or blocked."
        raise ValueError(message)
    attempt = SprintRetryAttempt(
        project_id=preview.project_id,
        sprint_id=preview.sprint_id,
        ordinal=preview.next_ordinal,
        predecessor_retry_attempt_id=preview.predecessor_retry_attempt_id,
        contract_fingerprint=next(
            item.contract_fingerprint
            for item in [
                evaluate_sprint_retry_eligibility(snapshot, sprint_id=preview.sprint_id)
            ]
            if item.contract_fingerprint is not None
        ),
        created_by=request.actor.strip(),
        rationale=request.rationale.strip(),
        creation_fingerprint=canonical_hash(request.model_dump(mode="json")),
        creation_receipt_key=request.idempotency_key,
        created_at=now,
        status="Planned",
    )
    session.add(attempt)
    session.flush()
    if attempt.retry_attempt_id is None:
        message = "Retry attempt has no durable identity."
        raise ValueError(message)
    for story_id in preview.story_ids:
        session.add(
            SprintRetryStoryState(
                project_id=preview.project_id,
                sprint_id=preview.sprint_id,
                retry_attempt_id=attempt.retry_attempt_id,
                story_id=story_id,
                status="To Do",
            )
        )
    for task_id in preview.task_ids:
        session.add(
            SprintRetryTaskState(
                project_id=preview.project_id,
                sprint_id=preview.sprint_id,
                retry_attempt_id=attempt.retry_attempt_id,
                task_id=task_id,
                status="To Do",
            )
        )
    session.flush()
    return SprintRetryResult(
        retry_attempt_id=attempt.retry_attempt_id, status="Planned"
    )


def start_sprint_retry_in_session(
    session: Session,
    *,
    request: StartSprintRetry,
    snapshot: WorkflowFactSnapshot,
    now: datetime,
) -> SprintRetryResult:
    """Start the exact planned retry while preserving the source Sprint."""
    rows = session.exec(
        select(SprintRetryAttempt).where(
            SprintRetryAttempt.retry_attempt_id == request.retry_attempt_id
        )
    ).all()
    if len(rows) != 1:
        message = "Retry attempt is missing or ambiguous."
        raise ValueError(message)
    retry = rows[0]
    if (
        retry.project_id != request.project_id
        or retry.sprint_id != request.sprint_id
        or retry.status != "Planned"
    ):
        message = "Retry start no longer matches the original contract."
        raise ValueError(message)
    _require_live_start_guard(snapshot, retry)
    retry.status = "Active"
    retry.started_at = now
    session.add(retry)
    session.add(
        SprintRetryStart(
            project_id=retry.project_id,
            sprint_id=retry.sprint_id,
            retry_attempt_id=request.retry_attempt_id,
            contract_fingerprint=retry.contract_fingerprint,
            decision_fingerprint=request.decision_fingerprint,
            started_by=request.actor.strip(),
            started_at=now,
        )
    )
    session.flush()
    return SprintRetryResult(retry_attempt_id=request.retry_attempt_id, status="Active")


def _require_live_start_guard(
    snapshot: WorkflowFactSnapshot,
    retry: SprintRetryAttempt,
) -> None:
    """Use the same current-source contract proof as retry eligibility."""
    if not source_sprint_contract_is_current(snapshot, sprint_id=retry.sprint_id):
        message = "Retry start selected requirements or dependencies changed."
        raise ValueError(message)
