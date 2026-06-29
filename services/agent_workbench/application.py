"""Agent workbench application facade."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from shlex import quote
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

from sqlmodel import Session, select

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.engine import Engine

    from services.agent_workbench.authority_curation import (
        AuthorityCurationRecoveryRequest,
        AuthorityCurationRequest,
        AuthorityFeedbackRecordRequest,
    )
    from services.agent_workbench.authority_decision import (
        AuthorityAcceptRequest,
        AuthorityRejectRequest,
    )
    from services.agent_workbench.authority_regenerate import AuthorityRegenerateRequest
    from services.agent_workbench.context_pack import ContextPackService

from services.agent_workbench.command_registry import (
    command_is_available,
    installed_command_names,
)
from services.agent_workbench.command_schema import (
    capabilities_payload,
    command_schema_payload,
)
from services.agent_workbench.diagnostics import doctor_payload, schema_check_payload
from services.agent_workbench.envelope import error_envelope, success_envelope
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.agent_workbench.fingerprints import canonical_hash
from services.agent_workbench.mutation_ledger import MutationLedgerRepository
from services.agent_workbench.post_sprint_triage import (
    current_triage_for_latest_sprint,
    post_sprint_triage_required,
)
from services.agent_workbench.project_setup import (
    AuthorityCompileRequest,
    ProjectCreateRequest,
    ProjectSetupMutationRunner,
    ProjectSetupRetryRequest,
)
from services.agent_workbench.project_setup_fingerprints import (
    setup_retry_context_fingerprint,
)
from services.agent_workbench.schema_readiness import (
    MUTATION_LEDGER_REQUIREMENTS,
    check_schema_readiness,
)
from services.agent_workbench.scope_discovery import (
    ChallengeArtifactRecordRequest,
    PrdDraftRecordRequest,
    PrdReviewRequest,
    SpecAmendmentDraftRecordRequest,
    SpecAmendmentReviewRequest,
)
from services.agent_workbench.scope_extension import (
    ScopeExtensionPreconditions,
    ScopeExtensionRunner,
    ScopeExtensionStartRequest,
    ScopeExtensionValidateRequest,
    evaluate_scope_extension_preconditions,
)
from services.specs.profile_content import SpecContentNormalizationError

STATUS_COMMAND: Final[str] = "agileforge status"
WORKFLOW_NEXT_COMMAND: Final[str] = "agileforge workflow next"
AUTHORITY_CURATE_COMMAND: Final[str] = "agileforge authority curate"
AUTHORITY_REGENERATE_COMMAND: Final[str] = "agileforge authority regenerate"


def get_engine() -> Engine:
    """Return the default engine without importing the DB layer at import time."""
    from models.db import get_engine as _get_engine  # noqa: PLC0415

    return _get_engine()


def _authority_curate_invalid_request(
    *,
    message: str,
    correlation_id: str | None,
) -> dict[str, Any]:
    """Return invalid authority curate argument envelope."""
    return error_envelope(
        command=AUTHORITY_CURATE_COMMAND,
        error=workbench_error(
            ErrorCode.INVALID_COMMAND,
            message=message,
            remediation=["Use either normal curation args or recovery args."],
        ),
        correlation_id=correlation_id,
    )


class _ReadProjection(Protocol):
    """Read projection methods exposed by the application facade."""

    def project_list(self) -> dict[str, Any]:
        """Return project list projection."""
        ...

    def project_show(self, *, project_id: int) -> dict[str, Any]:
        """Return project detail projection."""
        ...

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return workflow session projection."""
        ...

    def story_show(self, *, story_id: int) -> dict[str, Any]:
        """Return story detail projection."""
        ...

    def sprint_candidates(self, *, project_id: int) -> dict[str, Any]:
        """Return sprint candidate projection."""
        ...


class _AuthorityProjection(Protocol):
    """Authority projection methods exposed by the application facade."""

    def status(self, *, project_id: int) -> dict[str, Any]:
        """Return authority status projection."""
        ...

    def invariants(
        self,
        *,
        project_id: int,
        spec_version_id: int | None = None,
    ) -> dict[str, Any]:
        """Return authority invariants projection."""
        ...


class _ProjectSetupRunner(Protocol):
    """Project setup mutation runner methods exposed through the facade."""

    def create_project(self, request: ProjectCreateRequest) -> dict[str, Any]:
        """Create a project from a validated request."""
        ...

    def retry_setup(self, request: ProjectSetupRetryRequest) -> dict[str, Any]:
        """Retry setup from a validated request."""
        ...

    def compile_authority(self, request: AuthorityCompileRequest) -> dict[str, Any]:
        """Compile pending authority from a validated request."""
        ...


class _AuthorityReview(Protocol):
    """Authority review methods exposed through the facade."""

    def review(
        self,
        *,
        project_id: int,
        include_spec: str = "auto",
        output_format: str = "json",
    ) -> dict[str, Any]:
        """Return a pending authority review packet."""
        ...


class _AuthorityDecisionRunner(Protocol):
    """Authority decision methods exposed through the facade."""

    def accept(self, request: AuthorityAcceptRequest) -> dict[str, Any]:
        """Accept pending authority from a guarded request."""
        ...

    def reject(self, request: AuthorityRejectRequest) -> dict[str, Any]:
        """Reject pending authority from a guarded request."""
        ...


class _AuthorityRegenerateRunner(Protocol):
    """Authority regenerate methods exposed through the facade."""

    def regenerate(self, request: AuthorityRegenerateRequest) -> dict[str, Any]:
        """Regenerate authority for an approved spec version."""
        ...


class _AuthorityCurationRunner(Protocol):
    """Authority curation methods exposed through the facade."""

    def feedback_record(
        self,
        request: AuthorityFeedbackRecordRequest,
    ) -> dict[str, Any]:
        """Record structured feedback for pending authority."""
        ...

    def curate(self, request: AuthorityCurationRequest) -> dict[str, Any]:
        """Run bounded authority curation."""
        ...

    def recover(self, request: AuthorityCurationRecoveryRequest) -> dict[str, Any]:
        """Recover a published authority curation candidate."""
        ...


class _ScopeExtensionRunner(Protocol):
    """Scope-extension runner methods exposed through the facade."""

    def preconditions(
        self,
        *,
        project_id: int,
        workflow: dict[str, Any],
        sprint_candidate_count: int,
    ) -> ScopeExtensionPreconditions | None:
        """Return whether scope extension is available."""
        ...

    def validate(self, request: ScopeExtensionValidateRequest) -> dict[str, Any]:
        """Validate an amended spec against accepted authority."""
        ...

    def start(self, request: ScopeExtensionStartRequest) -> dict[str, Any]:
        """Start guarded scope extension."""
        ...


class _ScopeDiscoveryRunner(Protocol):
    """Scope discovery runner methods exposed through the facade."""

    def record_challenge_artifact(
        self,
        request: ChallengeArtifactRecordRequest,
    ) -> dict[str, Any]:
        """Record a Challenge Artifact."""
        ...

    def record_prd_draft(
        self,
        request: PrdDraftRecordRequest,
    ) -> dict[str, Any]:
        """Record a PRD draft."""
        ...

    def accept_prd(
        self,
        request: PrdReviewRequest,
    ) -> dict[str, Any]:
        """Accept a PRD."""
        ...

    def reject_prd(
        self,
        request: PrdReviewRequest,
    ) -> dict[str, Any]:
        """Reject a PRD."""
        ...

    def record_spec_amendment_draft(
        self,
        request: SpecAmendmentDraftRecordRequest,
    ) -> dict[str, Any]:
        """Record a Spec Amendment Draft."""
        ...

    def accept_spec_amendment(
        self,
        request: SpecAmendmentReviewRequest,
    ) -> dict[str, Any]:
        """Accept a Spec Amendment Draft."""
        ...

    def reject_spec_amendment(
        self,
        request: SpecAmendmentReviewRequest,
    ) -> dict[str, Any]:
        """Reject a Spec Amendment Draft."""
        ...


def _zero_scope_extension_sprint_candidate_count(_project_id: int) -> int:
    """Return the direct-runner default when no read projection is available."""
    return 0


class _DefaultScopeDiscoveryRunner:
    """Session-scoped adapter for scope discovery operations."""

    def record_challenge_artifact(
        self,
        request: ChallengeArtifactRecordRequest,
    ) -> dict[str, Any]:
        """Record a Challenge Artifact with a short-lived session."""
        from services.agent_workbench.scope_discovery import (  # noqa: PLC0415
            ScopeDiscoveryRunner,
        )

        with Session(get_engine()) as session:
            return ScopeDiscoveryRunner(session=session).record_challenge_artifact(
                request
            )

    def record_prd_draft(
        self,
        request: PrdDraftRecordRequest,
    ) -> dict[str, Any]:
        """Record a PRD draft with a short-lived session."""
        from services.agent_workbench.scope_discovery import (  # noqa: PLC0415
            ScopeDiscoveryRunner,
        )

        with Session(get_engine()) as session:
            return ScopeDiscoveryRunner(session=session).record_prd_draft(request)

    def accept_prd(
        self,
        request: PrdReviewRequest,
    ) -> dict[str, Any]:
        """Accept a PRD with a short-lived session."""
        from services.agent_workbench.scope_discovery import (  # noqa: PLC0415
            ScopeDiscoveryRunner,
        )

        with Session(get_engine()) as session:
            return ScopeDiscoveryRunner(session=session).accept_prd(request)

    def reject_prd(
        self,
        request: PrdReviewRequest,
    ) -> dict[str, Any]:
        """Reject a PRD with a short-lived session."""
        from services.agent_workbench.scope_discovery import (  # noqa: PLC0415
            ScopeDiscoveryRunner,
        )

        with Session(get_engine()) as session:
            return ScopeDiscoveryRunner(session=session).reject_prd(request)

    def record_spec_amendment_draft(
        self,
        request: SpecAmendmentDraftRecordRequest,
    ) -> dict[str, Any]:
        """Record a Spec Amendment Draft with a short-lived session."""
        from services.agent_workbench.scope_discovery import (  # noqa: PLC0415
            ScopeDiscoveryRunner,
        )

        with Session(get_engine()) as session:
            return ScopeDiscoveryRunner(session=session).record_spec_amendment_draft(
                request
            )

    def accept_spec_amendment(
        self,
        request: SpecAmendmentReviewRequest,
    ) -> dict[str, Any]:
        """Accept a Spec Amendment Draft with a short-lived session."""
        from services.agent_workbench.scope_discovery import (  # noqa: PLC0415
            ScopeDiscoveryRunner,
        )

        with Session(get_engine()) as session:
            return ScopeDiscoveryRunner(session=session).accept_spec_amendment(request)

    def reject_spec_amendment(
        self,
        request: SpecAmendmentReviewRequest,
    ) -> dict[str, Any]:
        """Reject a Spec Amendment Draft with a short-lived session."""
        from services.agent_workbench.scope_discovery import (  # noqa: PLC0415
            ScopeDiscoveryRunner,
        )

        with Session(get_engine()) as session:
            return ScopeDiscoveryRunner(session=session).reject_spec_amendment(request)


class _DefaultScopeExtensionRunner:
    """Session-scoped adapter for scope-extension operations."""

    def __init__(
        self,
        *,
        sprint_candidate_count_resolver: Callable[[int], int] | None = None,
    ) -> None:
        """Initialize default runner dependencies."""
        self._sprint_candidate_count_resolver = (
            sprint_candidate_count_resolver
            or _zero_scope_extension_sprint_candidate_count
        )

    def preconditions(
        self,
        *,
        project_id: int,
        workflow: dict[str, Any],
        sprint_candidate_count: int,
    ) -> ScopeExtensionPreconditions:
        """Evaluate scope-extension availability with a short-lived session."""
        state = workflow.get("state")
        workflow_state = state if isinstance(state, dict) else {}
        with Session(get_engine()) as session:
            return evaluate_scope_extension_preconditions(
                session=session,
                product_id=project_id,
                workflow_state=workflow_state,
                sprint_candidate_count=sprint_candidate_count,
            )

    def validate(self, request: ScopeExtensionValidateRequest) -> dict[str, Any]:
        """Validate scope extension through a short-lived runner."""
        from services.workflow import WorkflowService  # noqa: PLC0415

        with Session(get_engine()) as session:
            runner = ScopeExtensionRunner(
                session=session,
                workflow_service=WorkflowService(),
            )
            return runner.validate(request)

    def start(self, request: ScopeExtensionStartRequest) -> dict[str, Any]:
        """Start scope extension through a short-lived runner."""
        from services.workflow import WorkflowService  # noqa: PLC0415

        with Session(get_engine()) as session:
            runner = ScopeExtensionRunner(
                session=session,
                workflow_service=WorkflowService(),
                sprint_candidate_count_resolver=(
                    self._sprint_candidate_count_resolver
                ),
            )
            return runner.start(request)


class _VisionPhaseRunner(Protocol):
    """Vision phase commands exposed through the facade."""

    def generate(
        self,
        *,
        project_id: int,
        user_input: str | None = None,
    ) -> dict[str, Any]:
        """Generate or refine a Vision draft."""
        ...

    def history(self, *, project_id: int) -> dict[str, Any]:
        """Return Vision attempt history."""
        ...

    def save(self, *, project_id: int) -> dict[str, Any]:
        """Persist the current Vision draft."""
        ...


class _BacklogPhaseRunner(Protocol):
    """Backlog phase commands exposed through the facade."""

    def generate(
        self,
        *,
        project_id: int,
        user_input: str | None = None,
    ) -> dict[str, Any]:
        """Generate or refine a Backlog draft."""
        ...

    def preview(
        self,
        *,
        project_id: int,
        user_input: str | None = None,
    ) -> dict[str, Any]:
        """Generate a non-persisted Backlog preview."""
        ...

    def refine_preview(
        self,
        *,
        project_id: int,
        source_attempt_id: str | None = None,
        operations_file: str | None = None,
        source_artifact: str | None = None,
        user_input: str | None = None,
    ) -> dict[str, Any]:
        """Preview canonical Backlog refinement operations."""
        ...

    def refine_record(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        source_attempt_id: str,
        operations_file: str,
        expected_source_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        """Record canonical Backlog refinement operations."""
        ...

    def approve(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        approved_artifact_fingerprint: str,
        idempotency_key: str,
        source_attempt_id: str | None = None,
        attempt_id: str | None = None,
        operation_set_fingerprint: str | None = None,
        approved_operation_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Record host-mediated Backlog refinement approval."""
        ...

    def refine_import(
        self,
        *,
        project_id: int,
        source_artifact: str,
        edited_file: str,
        expected_source_fingerprint: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Fail closed until deterministic Backlog refinement import exists."""
        ...

    def history(self, *, project_id: int) -> dict[str, Any]:
        """Return Backlog attempt history."""
        ...

    def save(
        self,
        *,
        project_id: int,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Persist the current Backlog draft."""
        ...

    def reset_active(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        reset_reason: str,
        archive_all_active_stories: bool,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Install an approved refined attempt as the active backlog baseline."""
        ...

    def reconcile(
        self,
        *,
        project_id: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Repair legacy duplicate active Backlog seed rows."""
        ...


class _RoadmapPhaseRunner(Protocol):
    """Roadmap phase commands exposed through the facade."""

    def generate(
        self,
        *,
        project_id: int,
        user_input: str | None = None,
    ) -> dict[str, Any]:
        """Generate or refine a Roadmap draft."""
        ...

    def history(self, *, project_id: int) -> dict[str, Any]:
        """Return Roadmap attempt history."""
        ...

    def save(
        self,
        *,
        project_id: int,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Persist the current Roadmap draft."""
        ...


class _StoryPhaseRunner(Protocol):
    """Story phase commands exposed through the facade."""

    def pending(self, *, project_id: int) -> dict[str, Any]:
        """Return roadmap requirements grouped by Story completion status."""
        ...

    def generate(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        parent_requirement: str,
        user_input: str | None = None,
        force_feedback: bool = False,
        target_story_id: int | None = None,
        target_refinement_slot: int | None = None,
    ) -> dict[str, Any]:
        """Generate or refine a Story draft."""
        ...

    def retry(self, *, project_id: int, parent_requirement: str) -> dict[str, Any]:
        """Retry the latest retryable Story request."""
        ...

    def history(
        self,
        *,
        project_id: int,
        parent_requirement: str,
    ) -> dict[str, Any]:
        """Return Story attempt history."""
        ...

    def save(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        parent_requirement: str,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Persist the current Story draft."""
        ...

    def save_patch(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        parent_requirement: str,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
        target_story_id: int | None = None,
        target_refinement_slot: int | None = None,
    ) -> dict[str, Any]:
        """Persist one targeted Story draft item."""
        ...

    def complete(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        expected_state: str,
        idempotency_key: str,
        scope: str | None = None,
        scope_id: str | None = None,
        parent_requirements: list[str] | None = None,
    ) -> dict[str, Any]:
        """Complete the Story phase."""
        ...

    def reopen(
        self,
        *,
        project_id: int,
        parent_requirement: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Reopen one saved Story requirement before Sprint work exists."""
        ...

    def reconcile(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        story_id: int,
        action: str,
        reason: str,
        idempotency_key: str,
        changed_by: str = "cli-agent",
        evidence_links: list[str] | None = None,
        superseded_by_story_id: int | None = None,
    ) -> dict[str, Any]:
        """Record a Story reconciliation decision."""
        ...

    def requirement_reconcile(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        requirement: str,
        action: str,
        reason: str,
        idempotency_key: str,
        changed_by: str = "cli-agent",
        evidence_links: list[str] | None = None,
    ) -> dict[str, Any]:
        """Record a requirement reconciliation decision."""
        ...

    def repair_readiness(
        self,
        *,
        project_id: int,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Backfill Story planning metadata before Sprint work starts."""
        ...

    def repair_completion_scope(
        self,
        *,
        project_id: int,
        expected_state: str,
        expected_scope_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Clear stale Story completion scope before Sprint planning."""
        ...

    def dependency_inspect(self, *, project_id: int) -> dict[str, Any]:
        """Inspect Story dependency graph."""
        ...

    def dependency_propose(
        self,
        *,
        project_id: int,
        expected_state: str,
        idempotency_key: str,
        manual_edges: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a Story dependency proposal artifact."""
        ...

    def dependency_apply(
        self,
        *,
        project_id: int,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Apply a reviewed Story dependency artifact."""
        ...


class _SprintPhaseRunner(Protocol):
    """Sprint phase commands exposed through the facade."""

    def generate(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        user_input: str | None = None,
        selected_story_ids: list[int] | None = None,
        excluded_story_ids: list[int] | None = None,
        max_story_points: int | None = None,
        include_task_decomposition: bool = True,
    ) -> dict[str, Any]:
        """Generate or refine a Sprint draft."""
        ...

    def history(self, *, project_id: int) -> dict[str, Any]:
        """Return Sprint planner attempts and execution history."""
        ...

    def metrics(self, *, project_id: int) -> dict[str, Any]:
        """Return read-only Sprint performance metrics."""
        ...

    def save(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        team_name: str,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Persist the current Sprint draft."""
        ...

    def start(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Start a saved Sprint."""
        ...

    def status(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Return Sprint execution status."""
        ...

    def tasks(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Return Sprint execution tasks."""
        ...

    def task_next(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Return the next Sprint task ticket."""
        ...

    def task_show(
        self,
        *,
        project_id: int,
        task_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Return one Sprint task ticket."""
        ...

    def task_history(
        self,
        *,
        project_id: int,
        task_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Return one Sprint task execution history."""
        ...

    def task_update(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        task_id: int,
        status: str,
        expected_status: str,
        expected_task_fingerprint: str,
        idempotency_key: str,
        sprint_id: int | None = None,
        outcome_summary: str | None = None,
        artifact_refs: list[str] | None = None,
        checklist_result: str | None = None,
        validation_summary: str | None = None,
        notes: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Log Sprint task execution progress."""
        ...

    def story_readiness(
        self,
        *,
        project_id: int,
        story_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Return close readiness for one Sprint story."""
        ...

    def story_close(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        story_id: int,
        expected_status: str,
        expected_story_fingerprint: str,
        idempotency_key: str,
        resolution: str,
        completion_notes: str,
        evidence_links: list[str] | None = None,
        sprint_id: int | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Close one Sprint story."""
        ...

    def close_readiness(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Return close readiness for the active Sprint."""
        ...

    def close(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        expected_state: str,
        expected_status: str,
        expected_sprint_fingerprint: str,
        idempotency_key: str,
        completion_notes: str,
        follow_up_notes: str | None = None,
        sprint_id: int | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Close the active Sprint."""
        ...

    def review(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Return post-sprint review context."""
        ...

    def triage(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        expected_state: str,
        impact: str,
        learning_summary: str,
        decision_reason: str,
        idempotency_key: str,
        affected_requirements: list[str] | None = None,
        affected_task_ids: list[int] | None = None,
        affected_story_ids: list[int] | None = None,
        affected_backlog_item_ids: list[str] | None = None,
        affected_roadmap_item_ids: list[str] | None = None,
        affected_layers: list[str] | None = None,
        sprint_id: int | None = None,
        replace_existing: bool = False,
        expected_triage_fingerprint: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Record post-sprint triage metadata."""
        ...


class _EvidenceCollectionRunner(Protocol):
    """Evidence collection commands exposed through the facade."""

    def collect(
        self,
        *,
        project_id: int,
        repo_path: str | None,
        from_file: str | None,
        idempotency_key: str,
        include_generated_artifacts: bool = False,
    ) -> dict[str, Any]:
        """Collect or import evidence and cache it in workflow state."""
        ...


class _AsBuiltAssessmentRunner(Protocol):
    """As-Built Assessment command exposed through the facade."""

    def assess(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        repo_path: str,
        spec_file: str | None,
        spec_mode: str,
        user_input: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Assess implementation state and cache it in workflow state."""
        ...


class _BrownfieldCurationRunner(Protocol):
    """Brownfield curation commands exposed through the facade."""

    def source_import(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        source_file: str,
        source_kind: str = "source_file",
        idempotency_key: str,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Record a raw brownfield source artifact."""
        ...

    def scan(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        repo_path: str,
        source_attempt_id: str | None = None,
        idempotency_key: str,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Record a repository scan attempt."""
        ...

    def spec_draft(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        scan_attempt_id: str,
        user_input: str | None = None,
        idempotency_key: str,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Create a generated curated spec draft attempt."""
        ...

    def spec_import(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        curated_spec_file: str,
        expected_scan_fingerprint: str,
        parent_draft_attempt_id: str | None = None,
        idempotency_key: str,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Record a human-imported curated spec attempt."""
        ...

    def spec_approve(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        expected_setup_status: str,
        idempotency_key: str,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Approve a curated spec attempt into setup authority compilation."""
        ...


class AgentWorkbenchApplication:
    """Thin facade shared by CLI transport and future API parity paths."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        read_projection: _ReadProjection | None = None,
        authority_projection: _AuthorityProjection | None = None,
        project_setup_runner: _ProjectSetupRunner | None = None,
        authority_review: _AuthorityReview | None = None,
        authority_decision_runner: _AuthorityDecisionRunner | None = None,
        authority_regenerate_runner: _AuthorityRegenerateRunner | None = None,
        authority_curation_runner: _AuthorityCurationRunner | None = None,
        scope_discovery_runner: _ScopeDiscoveryRunner | None = None,
        scope_extension_runner: _ScopeExtensionRunner | None = None,
        vision_runner: _VisionPhaseRunner | None = None,
        backlog_runner: _BacklogPhaseRunner | None = None,
        roadmap_runner: _RoadmapPhaseRunner | None = None,
        story_runner: _StoryPhaseRunner | None = None,
        sprint_runner: _SprintPhaseRunner | None = None,
        evidence_runner: _EvidenceCollectionRunner | None = None,
        as_built_runner: _AsBuiltAssessmentRunner | None = None,
        brownfield_runner: _BrownfieldCurationRunner | None = None,
    ) -> None:
        """Initialize the facade with explicit projection dependencies."""
        self._read_projection = read_projection
        self._authority_projection = authority_projection
        self._project_setup_runner = project_setup_runner
        self._authority_review = authority_review
        self._authority_decision_runner = authority_decision_runner
        self._authority_regenerate_runner = authority_regenerate_runner
        self._authority_curation_runner = authority_curation_runner
        self._scope_discovery_runner = scope_discovery_runner
        self._scope_extension_runner = scope_extension_runner
        self._vision_runner = vision_runner
        self._backlog_runner = backlog_runner
        self._roadmap_runner = roadmap_runner
        self._story_runner = story_runner
        self._sprint_runner = sprint_runner
        self._evidence_runner = evidence_runner
        self._as_built_runner = as_built_runner
        self._brownfield_runner = brownfield_runner
        self._context_pack: ContextPackService | None = None

    def project_list(self) -> dict[str, Any]:
        """Return project list projection."""
        return self._get_read_projection().project_list()

    def project_show(self, *, project_id: int) -> dict[str, Any]:
        """Return project detail projection."""
        return self._get_read_projection().project_show(project_id=project_id)

    def workflow_state(self, *, project_id: int) -> dict[str, Any]:
        """Return workflow session projection."""
        return self._get_read_projection().workflow_state(project_id=project_id)

    def story_show(self, *, story_id: int) -> dict[str, Any]:
        """Return story detail projection."""
        return self._get_read_projection().story_show(story_id=story_id)

    def sprint_candidates(self, *, project_id: int) -> dict[str, Any]:
        """Return sprint candidate projection."""
        return self._get_read_projection().sprint_candidates(project_id=project_id)

    def context_pack(
        self,
        *,
        project_id: int,
        phase: str = "overview",
    ) -> dict[str, Any]:
        """Return a phase-scoped context pack."""
        return self._get_context_pack().pack(project_id=project_id, phase=phase)

    def status(self, *, project_id: int) -> dict[str, Any]:
        """Return project orientation status from read-only projections."""
        project = self.project_show(project_id=project_id)
        if not project.get("ok"):
            return project

        workflow = self.workflow_state(project_id=project_id)
        if not workflow.get("ok"):
            return workflow

        authority = self.authority_status(project_id=project_id)
        if not authority.get("ok"):
            return authority

        project_data = _envelope_data(project)
        workflow_data = _envelope_data(workflow)
        authority_data = _envelope_data(authority)
        data: dict[str, Any] = {
            "project": project_data,
            "workflow": workflow_data,
            "authority": authority_data,
        }
        data["source_fingerprint"] = canonical_hash(
            {
                "command": STATUS_COMMAND,
                "project_id": project_id,
                "project": _fingerprint_input(project_data),
                "workflow": _fingerprint_input(workflow_data),
                "authority": _fingerprint_input(authority_data),
            }
        )

        return {
            "ok": True,
            "data": data,
            "warnings": [
                *_section_warnings(
                    section="project",
                    source="project_show",
                    envelope=project,
                ),
                *_section_warnings(
                    section="workflow",
                    source="workflow_state",
                    envelope=workflow,
                ),
                *_section_warnings(
                    section="authority",
                    source="authority_status",
                    envelope=authority,
                ),
            ],
            "errors": [],
        }

    def workflow_next(self, *, project_id: int) -> dict[str, Any]:
        """Return installed next commands for the current workflow state."""
        workflow = self.workflow_state(project_id=project_id)
        if not workflow.get("ok"):
            return workflow

        setup_status = _setup_status(workflow)
        if _fsm_state_from_envelope(workflow) == "SETUP_REQUIRED" and setup_status in {
            "authority_compile_failed",
            "authority_compile_required",
            "authority_compiling",
            "authority_curating",
            "authority_pending_review",
            "authority_rejected",
            "brownfield_curation_required",
            "failed",
        }:
            authority = (
                self.authority_status(project_id=project_id)
                if setup_status
                in {
                    "authority_curating",
                    "authority_pending_review",
                    "authority_rejected",
                }
                else None
            )
            effective_setup_status = (
                "authority_pending_review"
                if setup_status != "authority_curating"
                and _authority_has_pending_review(authority)
                else setup_status
            )
            review = (
                self.authority_review(
                    project_id=project_id,
                    include_spec="summary",
                    output_format="json",
                )
                if effective_setup_status == "authority_pending_review"
                and (authority is None or authority.get("ok") is True)
                else None
            )
            return _setup_workflow_next(
                project_id=project_id,
                setup_status=effective_setup_status,
                workflow=workflow,
                authority=authority,
                review=review,
            )

        fsm_state = _fsm_state_from_envelope(workflow)
        if fsm_state == "SPRINT_COMPLETE":
            state = _envelope_data(workflow).get("state")
            state_data = state if isinstance(state, dict) else {}
            current_triage = current_triage_for_latest_sprint(state_data)
            sprint_candidates = (
                self.sprint_candidates(project_id=project_id)
                if current_triage is not None
                and current_triage.get("impact") == "none"
                and _stale_backlog_reason(workflow) is None
                else None
            )
            scope_extension_preconditions = None
            if (
                sprint_candidates is not None
                and _sprint_candidate_count(sprint_candidates) == 0
                and not _uncovered_story_requirements(workflow)
            ):
                scope_extension_runner = self._get_scope_extension_runner()
                scope_extension_preconditions = scope_extension_runner.preconditions(
                    project_id=project_id,
                    workflow=_envelope_data(workflow),
                    sprint_candidate_count=0,
                )
            return _sprint_complete_workflow_next(
                project_id=project_id,
                workflow=workflow,
                sprint_candidates=sprint_candidates,
                scope_extension_preconditions=scope_extension_preconditions,
            )

        sprint_candidates_for_setup = (
            self.sprint_candidates(project_id=project_id)
            if fsm_state == "SPRINT_SETUP"
            else None
        )
        story_pending_for_setup = (
            self.story_pending(project_id=project_id)
            if fsm_state == "SPRINT_SETUP"
            and _sprint_setup_stale_story_scope_blocker(sprint_candidates_for_setup)
            is None
            and _sprint_setup_story_refinement_blocker(
                sprint_candidates_for_setup,
                workflow=workflow,
            )
            is not None
            else None
        )
        phase_next_handlers = (
            _vision_workflow_next,
            _backlog_workflow_next,
            _roadmap_workflow_next,
            _story_workflow_next,
            _sprint_workflow_next,
            _uninstalled_phase_workflow_next,
        )
        for phase_next_handler in phase_next_handlers:
            phase_next = (
                phase_next_handler(
                    project_id=project_id,
                    workflow=workflow,
                    sprint_candidates=sprint_candidates_for_setup,
                    story_pending=story_pending_for_setup,
                )
                if phase_next_handler is _sprint_workflow_next
                else phase_next_handler(project_id=project_id, workflow=workflow)
            )
            if phase_next is not None:
                return phase_next

        pack = self.context_pack(project_id=project_id, phase="sprint-planning")
        if not pack.get("ok"):
            return pack

        pack_data = pack["data"]
        data = {
            "project_id": project_id,
            "next_valid_commands": pack_data["next_valid_commands"],
            "blocked_commands": pack_data["blocked_commands"],
            "blocked_future_commands": pack_data["blocked_future_commands"],
        }
        data["source_fingerprint"] = canonical_hash(
            {
                "command": WORKFLOW_NEXT_COMMAND,
                "project_id": project_id,
                "context_pack": pack_data.get("source_fingerprint"),
                "authority": pack_data.get("authority_fingerprint"),
                "installed_command_names": sorted(installed_command_names()),
                "next_valid_commands": data["next_valid_commands"],
                "blocked_commands": data["blocked_commands"],
                "blocked_future_commands": data["blocked_future_commands"],
            }
        )
        return {
            "ok": True,
            "data": data,
            "warnings": pack.get("warnings", []),
            "errors": [],
        }

    def doctor(
        self,
        *,
        business_engine: Engine | None = None,
        session_db_url: str | None = None,
    ) -> dict[str, Any]:
        """Return local diagnostics in an application envelope."""
        return _data_envelope(
            doctor_payload(
                business_engine=business_engine,
                session_db_url=session_db_url,
            )
        )

    def schema_check(
        self,
        *,
        business_engine: Engine | None = None,
        session_db_url: str | None = None,
    ) -> dict[str, Any]:
        """Return schema readiness diagnostics in an application envelope."""
        return _data_envelope(
            schema_check_payload(
                business_engine=business_engine,
                session_db_url=session_db_url,
            )
        )

    def capabilities(self) -> dict[str, Any]:
        """Return installed and pending command capabilities in an envelope."""
        return _data_envelope(capabilities_payload())

    def command_schema(self, command_name: str) -> dict[str, Any]:
        """Return one command schema in an application envelope."""
        try:
            payload = command_schema_payload(command_name)
        except ValueError as exc:
            error = workbench_error(
                ErrorCode.COMMAND_NOT_IMPLEMENTED,
                message=str(exc),
                details={"command_name": command_name},
                remediation=["agileforge capabilities"],
            )
            return {
                "ok": False,
                "data": {},
                "warnings": [],
                "errors": [error.to_dict()],
            }
        return _data_envelope(payload)

    def mutation_show(self, *, mutation_event_id: int) -> dict[str, Any]:
        """Return one mutation ledger event."""
        repo, error = _mutation_ledger_repository()
        if error is not None:
            return error
        repo = cast("MutationLedgerRepository", repo)
        return repo.show_event(mutation_event_id=mutation_event_id)

    def authority_curation_trace(
        self,
        *,
        mutation_event_id: int,
        project_id: int | None = None,
    ) -> dict[str, Any]:
        """Return bounded authority curation trace summary."""
        repo, error = _mutation_ledger_repository()
        if error is not None:
            return error
        repo = cast("MutationLedgerRepository", repo)
        shown = repo.show_event(
            mutation_event_id=mutation_event_id,
            include_trace_metadata=False,
        )
        if shown.get("ok") is not True:
            return shown
        data = shown.get("data")
        if not isinstance(data, dict):
            return shown
        if data.get("command") != AUTHORITY_CURATE_COMMAND:
            return error_envelope(
                command="agileforge authority curation trace",
                error=workbench_error(
                    ErrorCode.MUTATION_NOT_FOUND,
                    message="Authority curation trace was not found for mutation.",
                    details={"mutation_event_id": mutation_event_id},
                    remediation=["agileforge mutation list"],
                ),
            )
        if project_id is not None and data.get("project_id") != project_id:
            return error_envelope(
                command="agileforge authority curation trace",
                error=workbench_error(
                    ErrorCode.MUTATION_NOT_FOUND,
                    message="Authority curation trace was not found for project.",
                    details={
                        "mutation_event_id": mutation_event_id,
                        "project_id": project_id,
                    },
                    remediation=["agileforge mutation list"],
                ),
            )
        from utils.authority_curation_trace import summarize_trace  # noqa: PLC0415

        return success_envelope(
            command="agileforge authority curation trace",
            data=summarize_trace(mutation_event_id=mutation_event_id),
        )

    def mutation_list(
        self,
        *,
        project_id: int | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """Return mutation ledger events."""
        repo, error = _mutation_ledger_repository()
        if error is not None:
            return error
        repo = cast("MutationLedgerRepository", repo)
        return repo.list_events(project_id=project_id, status=status)

    def mutation_resume(
        self,
        *,
        mutation_event_id: int,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Acquire a guarded recovery lease for a mutation event."""
        repo, error = _mutation_ledger_repository()
        if error is not None:
            return error
        repo = cast("MutationLedgerRepository", repo)
        return repo.resume_event(
            mutation_event_id=mutation_event_id,
            correlation_id=correlation_id,
        )

    def project_create(  # noqa: PLR0913
        self,
        *,
        name: str,
        spec_file: str | None = None,
        setup_mode: str = "greenfield",
        idempotency_key: str | None = None,
        dry_run: bool = False,
        dry_run_id: str | None = None,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Create a project through the guarded setup mutation runner."""
        request = ProjectCreateRequest(
            name=name,
            spec_file=spec_file,
            setup_mode=setup_mode,
            idempotency_key=idempotency_key,
            dry_run=dry_run,
            dry_run_id=dry_run_id,
            correlation_id=correlation_id,
            changed_by=changed_by,
        )
        return self._get_project_setup_runner().create_project(request)

    def project_setup_retry(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        spec_file: str | None = None,
        setup_mode: str = "greenfield",
        expected_state: str,
        expected_context_fingerprint: str,
        recovery_mutation_event_id: int | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = False,
        dry_run_id: str | None = None,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Retry interrupted project setup through the guarded mutation runner."""
        request = ProjectSetupRetryRequest(
            project_id=project_id,
            spec_file=spec_file,
            setup_mode=setup_mode,
            expected_state=expected_state,
            expected_context_fingerprint=expected_context_fingerprint,
            recovery_mutation_event_id=recovery_mutation_event_id,
            idempotency_key=idempotency_key,
            dry_run=dry_run,
            dry_run_id=dry_run_id,
            correlation_id=correlation_id,
            changed_by=changed_by,
        )
        return self._get_project_setup_runner().retry_setup(request)

    def authority_compile(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        spec_version_id: int,
        expected_spec_hash: str,
        expected_state: str,
        expected_setup_status: str,
        compiler_model: str | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = False,
        dry_run_id: str | None = None,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Compile pending authority through the guarded mutation runner."""
        request = AuthorityCompileRequest(
            project_id=project_id,
            spec_version_id=spec_version_id,
            expected_spec_hash=expected_spec_hash,
            expected_state=expected_state,
            expected_setup_status=expected_setup_status,
            compiler_model=compiler_model,
            idempotency_key=idempotency_key,
            dry_run=dry_run,
            dry_run_id=dry_run_id,
            correlation_id=correlation_id,
            changed_by=changed_by,
        )
        return self._get_project_setup_runner().compile_authority(request)

    def authority_review(
        self,
        *,
        project_id: int,
        include_spec: str = "auto",
        output_format: str = "json",
    ) -> dict[str, Any]:
        """Return a pending authority review packet."""
        return self._get_authority_review().review(
            project_id=project_id,
            include_spec=include_spec,
            output_format=output_format,
        )

    def authority_accept(self, request: AuthorityAcceptRequest) -> dict[str, Any]:
        """Accept pending authority through the decision runner."""
        return self._get_authority_decision_runner().accept(request)

    def authority_reject(self, request: AuthorityRejectRequest) -> dict[str, Any]:
        """Reject pending authority through the decision runner."""
        return self._get_authority_decision_runner().reject(request)

    def authority_regenerate(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        spec_version_id: int,
        compiler_model: str | None = None,
        idempotency_key: str | None = None,
        changed_by: str = "cli-agent",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Regenerate compiled authority through the workbench facade."""
        from services.agent_workbench.authority_regenerate import (  # noqa: PLC0415
            AuthorityRegenerateRequest,
        )

        return self._get_authority_regenerate_runner().regenerate(
            AuthorityRegenerateRequest(
                project_id=project_id,
                spec_version_id=spec_version_id,
                compiler_model=compiler_model,
                idempotency_key=idempotency_key,
                changed_by=changed_by,
                dry_run=dry_run,
            )
        )

    def authority_feedback_record(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        pending_authority_id: int,
        expected_authority_fingerprint: str,
        feedback_file: str,
        idempotency_key: str,
        changed_by: str = "cli-agent",
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Record structured feedback for pending authority."""
        from services.agent_workbench.authority_curation import (  # noqa: PLC0415
            AuthorityFeedbackRecordRequest,
        )

        return self._get_authority_curation_runner().feedback_record(
            AuthorityFeedbackRecordRequest(
                project_id=project_id,
                pending_authority_id=pending_authority_id,
                expected_authority_fingerprint=expected_authority_fingerprint,
                feedback_file=feedback_file,
                idempotency_key=idempotency_key,
                changed_by=changed_by,
                correlation_id=correlation_id,
            )
        )

    def authority_curate(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        idempotency_key: str,
        spec_version_id: int | None = None,
        source_authority_id: int | None = None,
        expected_source_authority_fingerprint: str | None = None,
        feedback_attempt_id: str | None = None,
        recovery_mutation_event_id: int | None = None,
        expected_candidate_authority_id: int | None = None,
        expected_candidate_authority_fingerprint: str | None = None,
        max_iterations: int = 2,
        compiler_model: str | None = None,
        changed_by: str = "cli-agent",
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Run bounded authority curation."""
        from services.agent_workbench.authority_curation import (  # noqa: PLC0415
            AuthorityCurationRecoveryRequest,
            AuthorityCurationRequest,
        )

        if recovery_mutation_event_id is None and (
            expected_candidate_authority_id is not None
            or expected_candidate_authority_fingerprint is not None
        ):
            return _authority_curate_invalid_request(
                message=(
                    "authority curate recovery candidate inputs require "
                    "recovery mutation event id"
                ),
                correlation_id=correlation_id,
            )
        if recovery_mutation_event_id is not None:
            if (
                expected_candidate_authority_id is None
                or expected_candidate_authority_fingerprint is None
            ):
                return _authority_curate_invalid_request(
                    message=(
                        "authority curate recovery requires candidate "
                        "authority id and fingerprint"
                    ),
                    correlation_id=correlation_id,
                )
            if any(
                item is not None
                for item in (
                    spec_version_id,
                    source_authority_id,
                    expected_source_authority_fingerprint,
                    feedback_attempt_id,
                    compiler_model,
                )
            ):
                return _authority_curate_invalid_request(
                    message=(
                        "authority curate recovery cannot include normal "
                        "curation inputs"
                    ),
                    correlation_id=correlation_id,
                )
            return self._get_authority_curation_runner().recover(
                AuthorityCurationRecoveryRequest(
                    project_id=project_id,
                    recovery_mutation_event_id=recovery_mutation_event_id,
                    expected_candidate_authority_id=(
                        expected_candidate_authority_id
                    ),
                    expected_candidate_authority_fingerprint=(
                        expected_candidate_authority_fingerprint
                    ),
                    idempotency_key=idempotency_key,
                    changed_by=changed_by,
                    correlation_id=correlation_id,
                )
            )
        if (
            spec_version_id is None
            or source_authority_id is None
            or expected_source_authority_fingerprint is None
            or feedback_attempt_id is None
        ):
            return _authority_curate_invalid_request(
                message=(
                    "normal authority curate requires spec version, source "
                    "authority, source fingerprint, and feedback attempt"
                ),
                correlation_id=correlation_id,
            )
        return self._get_authority_curation_runner().curate(
            AuthorityCurationRequest(
                project_id=project_id,
                spec_version_id=spec_version_id,
                source_authority_id=source_authority_id,
                expected_source_authority_fingerprint=(
                    expected_source_authority_fingerprint
                ),
                feedback_attempt_id=feedback_attempt_id,
                max_iterations=max_iterations,
                compiler_model=compiler_model,
                idempotency_key=idempotency_key,
                changed_by=changed_by,
                correlation_id=correlation_id,
            )
        )

    def scope_extension_validate(
        self,
        *,
        project_id: int,
        spec_file: str,
        base_spec_version_id: int | None = None,
    ) -> dict[str, Any]:
        """Validate an amended spec through the scope-extension runner."""
        request = ScopeExtensionValidateRequest(
            project_id=project_id,
            spec_file=spec_file,
            base_spec_version_id=base_spec_version_id,
        )
        return self._get_scope_extension_runner().validate(request)

    def discovery_challenge_record(
        self,
        *,
        project_id: int,
        artifact_file: str,
        idempotency_key: str,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Record a Scope Discovery Challenge Artifact through the runner."""
        request = ChallengeArtifactRecordRequest(
            project_id=project_id,
            artifact_file=artifact_file,
            idempotency_key=idempotency_key,
            changed_by=changed_by,
        )
        return self._get_scope_discovery_runner().record_challenge_artifact(request)

    def discovery_prd_draft_record(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        challenge_artifact_id: int,
        prd_file: str,
        idempotency_key: str,
        supersedes_prd_id: int | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Record a Scope Discovery PRD draft through the runner."""
        request = PrdDraftRecordRequest(
            project_id=project_id,
            challenge_artifact_id=challenge_artifact_id,
            prd_file=prd_file,
            idempotency_key=idempotency_key,
            supersedes_prd_id=supersedes_prd_id,
            changed_by=changed_by,
        )
        return self._get_scope_discovery_runner().record_prd_draft(request)

    def discovery_prd_accept(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        prd_id: int,
        reviewer: str,
        acceptance_notes: str,
        idempotency_key: str,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Accept a Scope Discovery PRD through the runner."""
        request = PrdReviewRequest(
            project_id=project_id,
            prd_id=prd_id,
            reviewer=reviewer,
            notes=acceptance_notes,
            idempotency_key=idempotency_key,
            changed_by=changed_by,
        )
        return self._get_scope_discovery_runner().accept_prd(request)

    def discovery_prd_reject(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        prd_id: int,
        reviewer: str,
        rejection_notes: str,
        idempotency_key: str,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Reject a Scope Discovery PRD through the runner."""
        request = PrdReviewRequest(
            project_id=project_id,
            prd_id=prd_id,
            reviewer=reviewer,
            notes=rejection_notes,
            idempotency_key=idempotency_key,
            changed_by=changed_by,
        )
        return self._get_scope_discovery_runner().reject_prd(request)

    def discovery_spec_amendment_draft_record(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        prd_id: int,
        amendment_file: str,
        idempotency_key: str,
        base_spec_version_id: int | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Record a Scope Discovery Spec Amendment Draft through the runner."""
        request = SpecAmendmentDraftRecordRequest(
            project_id=project_id,
            prd_id=prd_id,
            amendment_file=amendment_file,
            idempotency_key=idempotency_key,
            base_spec_version_id=base_spec_version_id,
            changed_by=changed_by,
        )
        return self._get_scope_discovery_runner().record_spec_amendment_draft(request)

    def discovery_spec_amendment_accept(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        spec_amendment_draft_id: int,
        reviewer: str,
        acceptance_notes: str,
        idempotency_key: str,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Accept a Scope Discovery Spec Amendment Draft through the runner."""
        request = SpecAmendmentReviewRequest(
            project_id=project_id,
            spec_amendment_draft_id=spec_amendment_draft_id,
            reviewer=reviewer,
            notes=acceptance_notes,
            idempotency_key=idempotency_key,
            changed_by=changed_by,
        )
        return self._get_scope_discovery_runner().accept_spec_amendment(request)

    def discovery_spec_amendment_reject(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        spec_amendment_draft_id: int,
        reviewer: str,
        rejection_notes: str,
        idempotency_key: str,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Reject a Scope Discovery Spec Amendment Draft through the runner."""
        request = SpecAmendmentReviewRequest(
            project_id=project_id,
            spec_amendment_draft_id=spec_amendment_draft_id,
            reviewer=reviewer,
            notes=rejection_notes,
            idempotency_key=idempotency_key,
            changed_by=changed_by,
        )
        return self._get_scope_discovery_runner().reject_spec_amendment(request)

    def scope_extension_start(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        spec_file: str | None = None,
        base_spec_version_id: int | None = None,
        spec_amendment_draft_id: int | None = None,
        expected_state: str,
        idempotency_key: str,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Start guarded scope extension through the runner."""
        request = ScopeExtensionStartRequest(
            project_id=project_id,
            spec_file=spec_file,
            base_spec_version_id=base_spec_version_id,
            spec_amendment_draft_id=spec_amendment_draft_id,
            expected_state=expected_state,
            idempotency_key=idempotency_key,
            changed_by=changed_by,
        )
        return self._get_scope_extension_runner().start(request)

    def authority_status(self, *, project_id: int) -> dict[str, Any]:
        """Return authority status projection."""
        return self._get_authority_projection().status(project_id=project_id)

    def authority_invariants(
        self,
        *,
        project_id: int,
        spec_version_id: int | None = None,
    ) -> dict[str, Any]:
        """Return authority invariants projection."""
        return self._get_authority_projection().invariants(
            project_id=project_id,
            spec_version_id=spec_version_id,
        )

    def vision_generate(
        self,
        *,
        project_id: int,
        user_input: str | None = None,
    ) -> dict[str, Any]:
        """Generate or refine a Vision draft."""
        return self._get_vision_runner().generate(
            project_id=project_id,
            user_input=user_input,
        )

    def vision_history(self, *, project_id: int) -> dict[str, Any]:
        """Return Vision attempt history."""
        return self._get_vision_runner().history(project_id=project_id)

    def vision_save(self, *, project_id: int) -> dict[str, Any]:
        """Persist the current complete Vision draft."""
        return self._get_vision_runner().save(project_id=project_id)

    def backlog_generate(
        self,
        *,
        project_id: int,
        user_input: str | None = None,
    ) -> dict[str, Any]:
        """Generate or refine a Backlog draft."""
        return self._get_backlog_runner().generate(
            project_id=project_id,
            user_input=user_input,
        )

    def backlog_preview(
        self,
        *,
        project_id: int,
        user_input: str | None = None,
    ) -> dict[str, Any]:
        """Generate a non-persisted Backlog preview."""
        return self._get_backlog_runner().preview(
            project_id=project_id,
            user_input=user_input,
        )

    def backlog_refine_preview(
        self,
        *,
        project_id: int,
        source_attempt_id: str | None = None,
        operations_file: str | None = None,
        source_artifact: str | None = None,
        user_input: str | None = None,
    ) -> dict[str, Any]:
        """Preview canonical Backlog refinement operations."""
        return self._get_backlog_runner().refine_preview(
            project_id=project_id,
            source_attempt_id=source_attempt_id,
            operations_file=operations_file,
            source_artifact=source_artifact,
            user_input=user_input,
        )

    def backlog_refine_record(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        source_attempt_id: str,
        operations_file: str,
        expected_source_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        """Record canonical Backlog refinement operations."""
        return self._get_backlog_runner().refine_record(
            project_id=project_id,
            source_attempt_id=source_attempt_id,
            operations_file=operations_file,
            expected_source_fingerprint=expected_source_fingerprint,
            expected_state=expected_state,
            idempotency_key=idempotency_key,
            approval_id=approval_id,
        )

    def backlog_approve(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        approved_artifact_fingerprint: str,
        idempotency_key: str,
        source_attempt_id: str | None = None,
        attempt_id: str | None = None,
        operation_set_fingerprint: str | None = None,
        approved_operation_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Record host-mediated Backlog refinement approval."""
        return self._get_backlog_runner().approve(
            project_id=project_id,
            source_attempt_id=source_attempt_id,
            attempt_id=attempt_id,
            operation_set_fingerprint=operation_set_fingerprint,
            approved_artifact_fingerprint=approved_artifact_fingerprint,
            approved_operation_ids=approved_operation_ids,
            idempotency_key=idempotency_key,
        )

    def backlog_refine_import(
        self,
        *,
        project_id: int,
        source_artifact: str,
        edited_file: str,
        expected_source_fingerprint: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Fail closed until deterministic Backlog refinement import exists."""
        return self._get_backlog_runner().refine_import(
            project_id=project_id,
            source_artifact=source_artifact,
            edited_file=edited_file,
            expected_source_fingerprint=expected_source_fingerprint,
            idempotency_key=idempotency_key,
        )

    def backlog_history(self, *, project_id: int) -> dict[str, Any]:
        """Return Backlog attempt history."""
        return self._get_backlog_runner().history(project_id=project_id)

    def backlog_save(
        self,
        *,
        project_id: int,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Persist the current complete Backlog draft."""
        return self._get_backlog_runner().save(
            project_id=project_id,
            attempt_id=attempt_id,
            expected_artifact_fingerprint=expected_artifact_fingerprint,
            expected_state=expected_state,
            idempotency_key=idempotency_key,
        )

    def backlog_reset_active(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        reset_reason: str,
        archive_all_active_stories: bool,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Install an approved refined attempt as the active backlog baseline."""
        return self._get_backlog_runner().reset_active(
            project_id=project_id,
            attempt_id=attempt_id,
            expected_artifact_fingerprint=expected_artifact_fingerprint,
            expected_state=expected_state,
            reset_reason=reset_reason,
            archive_all_active_stories=archive_all_active_stories,
            idempotency_key=idempotency_key,
        )

    def backlog_reconcile(
        self,
        *,
        project_id: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Repair legacy duplicate active Backlog seed rows."""
        return self._get_backlog_runner().reconcile(
            project_id=project_id,
            idempotency_key=idempotency_key,
        )

    def evidence_collect(
        self,
        *,
        project_id: int,
        repo_path: str | None,
        from_file: str | None,
        idempotency_key: str,
        include_generated_artifacts: bool = False,
    ) -> dict[str, Any]:
        """Collect or import implementation evidence for backlog generation."""
        return self._get_evidence_runner().collect(
            project_id=project_id,
            repo_path=repo_path,
            from_file=from_file,
            idempotency_key=idempotency_key,
            include_generated_artifacts=include_generated_artifacts,
        )

    def as_built_assess(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        repo_path: str,
        spec_file: str | None,
        spec_mode: str,
        user_input: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Assess implementation state and cache it for backlog generation."""
        return self._get_as_built_runner().assess(
            project_id=project_id,
            repo_path=repo_path,
            spec_file=spec_file,
            spec_mode=spec_mode,
            user_input=user_input,
            idempotency_key=idempotency_key,
        )

    def brownfield_source_import(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        source_file: str,
        source_kind: str = "source_file",
        idempotency_key: str,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Record a raw brownfield source artifact."""
        return self._get_brownfield_runner().source_import(
            project_id=project_id,
            source_file=source_file,
            source_kind=source_kind,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            changed_by=changed_by,
        )

    def brownfield_scan(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        repo_path: str,
        source_attempt_id: str | None = None,
        idempotency_key: str,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Record a brownfield repository scan attempt."""
        return self._get_brownfield_runner().scan(
            project_id=project_id,
            repo_path=repo_path,
            source_attempt_id=source_attempt_id,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            changed_by=changed_by,
        )

    def brownfield_spec_draft(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        scan_attempt_id: str,
        user_input: str | None = None,
        idempotency_key: str,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Create a generated curated spec draft attempt."""
        return self._get_brownfield_runner().spec_draft(
            project_id=project_id,
            scan_attempt_id=scan_attempt_id,
            user_input=user_input,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            changed_by=changed_by,
        )

    def brownfield_spec_import(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        curated_spec_file: str,
        expected_scan_fingerprint: str,
        parent_draft_attempt_id: str | None = None,
        idempotency_key: str,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Record a human-imported curated spec attempt."""
        return self._get_brownfield_runner().spec_import(
            project_id=project_id,
            curated_spec_file=curated_spec_file,
            expected_scan_fingerprint=expected_scan_fingerprint,
            parent_draft_attempt_id=parent_draft_attempt_id,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            changed_by=changed_by,
        )

    def brownfield_spec_approve(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        expected_setup_status: str,
        idempotency_key: str,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Approve a curated spec attempt."""
        return self._get_brownfield_runner().spec_approve(
            project_id=project_id,
            attempt_id=attempt_id,
            expected_artifact_fingerprint=expected_artifact_fingerprint,
            expected_state=expected_state,
            expected_setup_status=expected_setup_status,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            changed_by=changed_by,
        )

    def roadmap_generate(
        self,
        *,
        project_id: int,
        user_input: str | None = None,
    ) -> dict[str, Any]:
        """Generate or refine a Roadmap draft."""
        return self._get_roadmap_runner().generate(
            project_id=project_id,
            user_input=user_input,
        )

    def roadmap_history(self, *, project_id: int) -> dict[str, Any]:
        """Return Roadmap attempt history."""
        return self._get_roadmap_runner().history(project_id=project_id)

    def roadmap_save(
        self,
        *,
        project_id: int,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Persist the current complete Roadmap draft."""
        return self._get_roadmap_runner().save(
            project_id=project_id,
            attempt_id=attempt_id,
            expected_artifact_fingerprint=expected_artifact_fingerprint,
            expected_state=expected_state,
            idempotency_key=idempotency_key,
        )

    def story_pending(self, *, project_id: int) -> dict[str, Any]:
        """Return Story pending roadmap requirements."""
        return self._get_story_runner().pending(project_id=project_id)

    def story_generate(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        parent_requirement: str,
        user_input: str | None = None,
        force_feedback: bool = False,
        target_story_id: int | None = None,
        target_refinement_slot: int | None = None,
    ) -> dict[str, Any]:
        """Generate or refine a Story draft."""
        return self._get_story_runner().generate(
            project_id=project_id,
            parent_requirement=parent_requirement,
            user_input=user_input,
            force_feedback=force_feedback,
            target_story_id=target_story_id,
            target_refinement_slot=target_refinement_slot,
        )

    def story_retry(
        self,
        *,
        project_id: int,
        parent_requirement: str,
    ) -> dict[str, Any]:
        """Retry the latest retryable Story request."""
        return self._get_story_runner().retry(
            project_id=project_id,
            parent_requirement=parent_requirement,
        )

    def story_history(
        self,
        *,
        project_id: int,
        parent_requirement: str,
    ) -> dict[str, Any]:
        """Return Story attempt history."""
        return self._get_story_runner().history(
            project_id=project_id,
            parent_requirement=parent_requirement,
        )

    def story_save(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        parent_requirement: str,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Persist the current complete Story draft."""
        return self._get_story_runner().save(
            project_id=project_id,
            parent_requirement=parent_requirement,
            attempt_id=attempt_id,
            expected_artifact_fingerprint=expected_artifact_fingerprint,
            expected_state=expected_state,
            idempotency_key=idempotency_key,
        )

    def story_save_patch(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        parent_requirement: str,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
        target_story_id: int | None = None,
        target_refinement_slot: int | None = None,
    ) -> dict[str, Any]:
        """Persist one targeted Story draft item."""
        return self._get_story_runner().save_patch(
            project_id=project_id,
            parent_requirement=parent_requirement,
            attempt_id=attempt_id,
            expected_artifact_fingerprint=expected_artifact_fingerprint,
            expected_state=expected_state,
            idempotency_key=idempotency_key,
            target_story_id=target_story_id,
            target_refinement_slot=target_refinement_slot,
        )

    def story_complete(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        expected_state: str,
        idempotency_key: str,
        scope: str | None = None,
        scope_id: str | None = None,
        parent_requirements: list[str] | None = None,
    ) -> dict[str, Any]:
        """Complete the Story phase."""
        return self._get_story_runner().complete(
            project_id=project_id,
            expected_state=expected_state,
            idempotency_key=idempotency_key,
            scope=scope,
            scope_id=scope_id,
            parent_requirements=parent_requirements,
        )

    def story_reopen(
        self,
        *,
        project_id: int,
        parent_requirement: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Reopen one saved Story requirement before Sprint work exists."""
        return self._get_story_runner().reopen(
            project_id=project_id,
            parent_requirement=parent_requirement,
            expected_state=expected_state,
            idempotency_key=idempotency_key,
        )

    def story_reconcile(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        story_id: int,
        action: str,
        reason: str,
        idempotency_key: str,
        changed_by: str = "cli-agent",
        evidence_links: list[str] | None = None,
        superseded_by_story_id: int | None = None,
    ) -> dict[str, Any]:
        """Record a Story reconciliation decision."""
        return self._get_story_runner().reconcile(
            project_id=project_id,
            story_id=story_id,
            action=action,
            reason=reason,
            idempotency_key=idempotency_key,
            changed_by=changed_by,
            evidence_links=evidence_links,
            superseded_by_story_id=superseded_by_story_id,
        )

    def requirement_reconcile(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        requirement: str,
        action: str,
        reason: str,
        idempotency_key: str,
        changed_by: str = "cli-agent",
        evidence_links: list[str] | None = None,
    ) -> dict[str, Any]:
        """Record a requirement reconciliation decision."""
        return self._get_story_runner().requirement_reconcile(
            project_id=project_id,
            requirement=requirement,
            action=action,
            reason=reason,
            idempotency_key=idempotency_key,
            changed_by=changed_by,
            evidence_links=evidence_links,
        )

    def story_repair_readiness(
        self,
        *,
        project_id: int,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Backfill Story planning metadata before Sprint work starts."""
        return self._get_story_runner().repair_readiness(
            project_id=project_id,
            expected_state=expected_state,
            idempotency_key=idempotency_key,
        )

    def story_repair_completion_scope(
        self,
        *,
        project_id: int,
        expected_state: str,
        expected_scope_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Clear stale Story completion scope before Sprint planning."""
        return self._get_story_runner().repair_completion_scope(
            project_id=project_id,
            expected_state=expected_state,
            expected_scope_id=expected_scope_id,
            idempotency_key=idempotency_key,
        )

    def story_dependencies_inspect(self, *, project_id: int) -> dict[str, Any]:
        """Inspect Story dependency graph."""
        return self._get_story_runner().dependency_inspect(project_id=project_id)

    def story_dependencies_propose(
        self,
        *,
        project_id: int,
        expected_state: str,
        idempotency_key: str,
        manual_edges: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a Story dependency proposal artifact."""
        return self._get_story_runner().dependency_propose(
            project_id=project_id,
            expected_state=expected_state,
            idempotency_key=idempotency_key,
            manual_edges=manual_edges,
        )

    def story_dependencies_apply(
        self,
        *,
        project_id: int,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Apply a reviewed Story dependency proposal artifact."""
        return self._get_story_runner().dependency_apply(
            project_id=project_id,
            attempt_id=attempt_id,
            expected_artifact_fingerprint=expected_artifact_fingerprint,
            expected_state=expected_state,
            idempotency_key=idempotency_key,
        )

    def sprint_generate(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        user_input: str | None = None,
        selected_story_ids: list[int] | None = None,
        excluded_story_ids: list[int] | None = None,
        max_story_points: int | None = None,
        include_task_decomposition: bool = True,
    ) -> dict[str, Any]:
        """Generate or refine a Sprint draft."""
        return self._get_sprint_runner().generate(
            project_id=project_id,
            user_input=user_input,
            selected_story_ids=selected_story_ids,
            excluded_story_ids=excluded_story_ids,
            max_story_points=max_story_points,
            include_task_decomposition=include_task_decomposition,
        )

    def sprint_history(self, *, project_id: int) -> dict[str, Any]:
        """Return Sprint planner attempts and execution history."""
        return self._get_sprint_runner().history(project_id=project_id)

    def sprint_metrics(self, *, project_id: int) -> dict[str, Any]:
        """Return read-only Sprint performance metrics."""
        return self._get_sprint_runner().metrics(project_id=project_id)

    def sprint_save(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        team_name: str,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Persist the current complete Sprint draft."""
        return self._get_sprint_runner().save(
            project_id=project_id,
            team_name=team_name,
            attempt_id=attempt_id,
            expected_artifact_fingerprint=expected_artifact_fingerprint,
            expected_state=expected_state,
            idempotency_key=idempotency_key,
        )

    def sprint_start(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Start a saved Sprint."""
        return self._get_sprint_runner().start(
            project_id=project_id,
            sprint_id=sprint_id,
            expected_state=expected_state,
            idempotency_key=idempotency_key,
        )

    def sprint_status(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Return Sprint execution status."""
        return self._get_sprint_runner().status(
            project_id=project_id,
            sprint_id=sprint_id,
        )

    def sprint_tasks(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Return Sprint execution tasks."""
        return self._get_sprint_runner().tasks(
            project_id=project_id,
            sprint_id=sprint_id,
        )

    def sprint_task_next(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Return the next Sprint task ticket."""
        return self._get_sprint_runner().task_next(
            project_id=project_id,
            sprint_id=sprint_id,
        )

    def sprint_task_show(
        self,
        *,
        project_id: int,
        task_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Return one Sprint task ticket."""
        return self._get_sprint_runner().task_show(
            project_id=project_id,
            task_id=task_id,
            sprint_id=sprint_id,
        )

    def sprint_task_history(
        self,
        *,
        project_id: int,
        task_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Return one Sprint task execution history."""
        return self._get_sprint_runner().task_history(
            project_id=project_id,
            task_id=task_id,
            sprint_id=sprint_id,
        )

    def sprint_task_update(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        task_id: int,
        status: str,
        expected_status: str,
        expected_task_fingerprint: str,
        idempotency_key: str,
        sprint_id: int | None = None,
        outcome_summary: str | None = None,
        artifact_refs: list[str] | None = None,
        checklist_result: str | None = None,
        validation_summary: str | None = None,
        notes: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Log Sprint task execution progress."""
        return self._get_sprint_runner().task_update(
            project_id=project_id,
            task_id=task_id,
            status=status,
            expected_status=expected_status,
            expected_task_fingerprint=expected_task_fingerprint,
            idempotency_key=idempotency_key,
            sprint_id=sprint_id,
            outcome_summary=outcome_summary,
            artifact_refs=artifact_refs,
            checklist_result=checklist_result,
            validation_summary=validation_summary,
            notes=notes,
            changed_by=changed_by,
        )

    def sprint_story_readiness(
        self,
        *,
        project_id: int,
        story_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Return close readiness for one Sprint story."""
        return self._get_sprint_runner().story_readiness(
            project_id=project_id,
            story_id=story_id,
            sprint_id=sprint_id,
        )

    def sprint_story_close(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        story_id: int,
        expected_status: str,
        expected_story_fingerprint: str,
        idempotency_key: str,
        resolution: str,
        completion_notes: str,
        evidence_links: list[str] | None = None,
        sprint_id: int | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Close one Sprint story."""
        return self._get_sprint_runner().story_close(
            project_id=project_id,
            story_id=story_id,
            expected_status=expected_status,
            expected_story_fingerprint=expected_story_fingerprint,
            idempotency_key=idempotency_key,
            resolution=resolution,
            completion_notes=completion_notes,
            evidence_links=evidence_links,
            sprint_id=sprint_id,
            changed_by=changed_by,
        )

    def sprint_close_readiness(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Return close readiness for the active Sprint."""
        return self._get_sprint_runner().close_readiness(
            project_id=project_id,
            sprint_id=sprint_id,
        )

    def sprint_close(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        expected_state: str,
        expected_status: str,
        expected_sprint_fingerprint: str,
        idempotency_key: str,
        completion_notes: str,
        follow_up_notes: str | None = None,
        sprint_id: int | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Close the active Sprint."""
        return self._get_sprint_runner().close(
            project_id=project_id,
            expected_state=expected_state,
            expected_status=expected_status,
            expected_sprint_fingerprint=expected_sprint_fingerprint,
            idempotency_key=idempotency_key,
            completion_notes=completion_notes,
            follow_up_notes=follow_up_notes,
            sprint_id=sprint_id,
            changed_by=changed_by,
        )

    def sprint_review(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> dict[str, Any]:
        """Return post-sprint review context."""
        return self._get_sprint_runner().review(
            project_id=project_id,
            sprint_id=sprint_id,
        )

    def sprint_triage(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        expected_state: str,
        impact: str,
        learning_summary: str,
        decision_reason: str,
        idempotency_key: str,
        affected_requirements: list[str] | None = None,
        affected_task_ids: list[int] | None = None,
        affected_story_ids: list[int] | None = None,
        affected_backlog_item_ids: list[str] | None = None,
        affected_roadmap_item_ids: list[str] | None = None,
        affected_layers: list[str] | None = None,
        sprint_id: int | None = None,
        replace_existing: bool = False,
        expected_triage_fingerprint: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Record post-sprint triage metadata."""
        return self._get_sprint_runner().triage(
            project_id=project_id,
            expected_state=expected_state,
            impact=impact,
            learning_summary=learning_summary,
            decision_reason=decision_reason,
            idempotency_key=idempotency_key,
            affected_requirements=affected_requirements,
            affected_task_ids=affected_task_ids,
            affected_story_ids=affected_story_ids,
            affected_backlog_item_ids=affected_backlog_item_ids,
            affected_roadmap_item_ids=affected_roadmap_item_ids,
            affected_layers=affected_layers,
            sprint_id=sprint_id,
            replace_existing=replace_existing,
            expected_triage_fingerprint=expected_triage_fingerprint,
            changed_by=changed_by,
        )

    def _get_read_projection(self) -> _ReadProjection:
        """Return the read projection, constructing the default lazily."""
        if self._read_projection is None:
            from services.agent_workbench.read_projection import (  # noqa: PLC0415
                ReadProjectionService,
            )

            self._read_projection = ReadProjectionService()
        return self._read_projection

    def _get_authority_projection(self) -> _AuthorityProjection:
        """Return the authority projection, constructing the default lazily."""
        if self._authority_projection is None:
            from services.agent_workbench.authority_projection import (  # noqa: PLC0415
                AuthorityProjectionService,
            )

            self._authority_projection = AuthorityProjectionService()
        return self._authority_projection

    def _get_context_pack(self) -> ContextPackService:
        """Return the context pack service after projections are needed."""
        if self._context_pack is None:
            from services.agent_workbench.context_pack import (  # noqa: PLC0415
                ContextPackService,
            )

            self._context_pack = ContextPackService(
                read_projection=self._get_read_projection(),
                authority_projection=self._get_authority_projection(),
            )
        return self._context_pack

    def _get_project_setup_runner(self) -> _ProjectSetupRunner:
        """Return the project setup runner, constructing the default lazily."""
        if self._project_setup_runner is None:
            self._project_setup_runner = ProjectSetupMutationRunner(engine=get_engine())
        return self._project_setup_runner

    def _get_authority_review(self) -> _AuthorityReview:
        """Return the authority review service, constructing the default lazily."""
        if self._authority_review is None:
            from services.agent_workbench.authority_review import (  # noqa: PLC0415
                AuthorityReviewService,
            )

            self._authority_review = AuthorityReviewService()
        return self._authority_review

    def _get_authority_decision_runner(self) -> _AuthorityDecisionRunner:
        """Return the authority decision runner, constructing the default lazily."""
        if self._authority_decision_runner is None:
            from services.agent_workbench.authority_decision import (  # noqa: PLC0415
                AuthorityDecisionRunner,
            )

            self._authority_decision_runner = AuthorityDecisionRunner()
        return self._authority_decision_runner

    def _get_authority_regenerate_runner(self) -> _AuthorityRegenerateRunner:
        """Return the authority regenerate runner, constructing it lazily."""
        if self._authority_regenerate_runner is None:
            from services.agent_workbench.authority_regenerate import (  # noqa: PLC0415
                default_authority_regenerate_runner,
            )

            self._authority_regenerate_runner = default_authority_regenerate_runner()
        return self._authority_regenerate_runner

    def _get_authority_curation_runner(self) -> _AuthorityCurationRunner:
        """Return the authority curation runner, constructing it lazily."""
        if self._authority_curation_runner is None:
            from services.agent_workbench.authority_curation import (  # noqa: PLC0415
                AuthorityCurationRunner,
            )

            self._authority_curation_runner = AuthorityCurationRunner(
                engine=get_engine()
            )
        return self._authority_curation_runner

    def _get_scope_extension_runner(self) -> _ScopeExtensionRunner:
        """Return the scope-extension runner, constructing the default lazily."""
        if self._scope_extension_runner is None:
            self._scope_extension_runner = _DefaultScopeExtensionRunner(
                sprint_candidate_count_resolver=(
                    self._scope_extension_sprint_candidate_count
                )
            )
        return self._scope_extension_runner

    def _get_scope_discovery_runner(self) -> _ScopeDiscoveryRunner:
        """Return the scope discovery runner, constructing the default lazily."""
        if self._scope_discovery_runner is None:
            self._scope_discovery_runner = _DefaultScopeDiscoveryRunner()
        return self._scope_discovery_runner

    def _scope_extension_sprint_candidate_count(self, project_id: int) -> int:
        """Resolve candidate count from the same read projection as workflow-next."""
        return _sprint_candidate_count(
            self.sprint_candidates(project_id=project_id)
        ) or 0

    def _get_vision_runner(self) -> _VisionPhaseRunner:
        """Return the Vision runner, constructing the default lazily."""
        if self._vision_runner is None:
            from services.agent_workbench.vision_phase import (  # noqa: PLC0415
                VisionPhaseRunner,
            )

            self._vision_runner = VisionPhaseRunner()
        return self._vision_runner

    def _get_backlog_runner(self) -> _BacklogPhaseRunner:
        """Return the Backlog runner, constructing the default lazily."""
        if self._backlog_runner is None:
            from services.agent_workbench.backlog_phase import (  # noqa: PLC0415
                BacklogPhaseRunner,
            )

            self._backlog_runner = BacklogPhaseRunner()
        return self._backlog_runner

    def _get_roadmap_runner(self) -> _RoadmapPhaseRunner:
        """Return the Roadmap runner, constructing the default lazily."""
        if self._roadmap_runner is None:
            from services.agent_workbench.roadmap_phase import (  # noqa: PLC0415
                RoadmapPhaseRunner,
            )

            self._roadmap_runner = RoadmapPhaseRunner()
        return self._roadmap_runner

    def _get_story_runner(self) -> _StoryPhaseRunner:
        """Return the Story runner, constructing the default lazily."""
        if self._story_runner is None:
            from services.agent_workbench.story_phase import (  # noqa: PLC0415
                StoryPhaseRunner,
            )

            self._story_runner = StoryPhaseRunner()
        return self._story_runner

    def _get_sprint_runner(self) -> _SprintPhaseRunner:
        """Return the Sprint runner, constructing the default lazily."""
        if self._sprint_runner is None:
            from services.agent_workbench.sprint_phase import (  # noqa: PLC0415
                SprintPhaseRunner,
            )

            self._sprint_runner = SprintPhaseRunner()
        return self._sprint_runner

    def _get_evidence_runner(self) -> _EvidenceCollectionRunner:
        """Return the evidence runner, constructing the default lazily."""
        if self._evidence_runner is None:
            from services.agent_workbench.evidence_collect import (  # noqa: PLC0415
                EvidenceCollectionRunner,
            )

            self._evidence_runner = EvidenceCollectionRunner()
        return self._evidence_runner

    def _get_as_built_runner(self) -> _AsBuiltAssessmentRunner:
        """Return the As-Built Assessment runner, constructing it lazily."""
        if self._as_built_runner is None:
            from services.agent_workbench.as_built_assessment import (  # noqa: PLC0415
                AsBuiltAssessmentRunner,
            )

            self._as_built_runner = AsBuiltAssessmentRunner()
        return self._as_built_runner

    def _get_brownfield_runner(self) -> _BrownfieldCurationRunner:
        """Return the brownfield curation runner, constructing it lazily."""
        if self._brownfield_runner is None:
            from services.agent_workbench.brownfield_curation import (  # noqa: PLC0415
                BrownfieldCurationRunner,
            )

            self._brownfield_runner = BrownfieldCurationRunner()
        return self._brownfield_runner


def _envelope_data(envelope: dict[str, Any]) -> dict[str, Any]:
    """Return dictionary data from a successful child projection."""
    data = envelope.get("data")
    return data if isinstance(data, dict) else {}


def _fsm_state_from_envelope(envelope: dict[str, Any]) -> str | None:
    """Return normalized FSM state from a workflow envelope."""
    data = _envelope_data(envelope)
    state = data.get("state")
    state_data = state if isinstance(state, dict) else {}
    fsm_state = state_data.get("fsm_state")
    return str(fsm_state).strip().upper() if fsm_state is not None else None


def _setup_status(envelope: dict[str, Any]) -> str | None:
    """Return normalized setup status from a workflow envelope."""
    data = _envelope_data(envelope)
    state = data.get("state")
    state_data = state if isinstance(state, dict) else {}
    setup_status = state_data.get("setup_status")
    return str(setup_status).strip().lower() if setup_status is not None else None


def _authority_has_pending_review(authority: dict[str, Any] | None) -> bool:
    """Return whether authority status exposes a pending candidate."""
    if authority is None or authority.get("ok") is not True:
        return False
    data = authority.get("data")
    if not isinstance(data, dict):
        return False
    return data.get("pending_authority_id") is not None


def _non_empty_string(value: object) -> str | None:
    """Return stripped string values only when non-empty."""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _positive_int_or_none(value: object) -> int | None:
    """Return positive integer values while rejecting bools."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def _sprint_candidate_count(candidates: dict[str, Any] | None) -> int | None:
    """Return non-negative Sprint candidate counts from a read projection."""
    if candidates is None or candidates.get("ok") is not True:
        return None
    count = _envelope_data(candidates).get("count")
    if isinstance(count, bool):
        return None
    if isinstance(count, int) and count >= 0:
        return count
    return None


def _sprint_candidate_excluded_counts(
    candidates: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return Sprint candidate exclusion counts from a read projection."""
    if candidates is None or candidates.get("ok") is not True:
        return {}
    excluded = _envelope_data(candidates).get("excluded_counts")
    return dict(excluded) if isinstance(excluded, dict) else {}


def _sprint_setup_stale_story_scope_blocker(
    candidates: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return blocker when a stale Story scope excludes all Sprint candidates."""
    if _sprint_candidate_count(candidates) != 0:
        return None
    excluded_counts = _sprint_candidate_excluded_counts(candidates)
    scoped_exclusion_count = _positive_int_or_none(
        excluded_counts.get("story_completion_scope")
    )
    if scoped_exclusion_count is None:
        return None
    scope = _envelope_data(candidates or {}).get("story_completion_scope")
    if not isinstance(scope, dict):
        return None
    return {
        "command": "agileforge sprint generate",
        "reason": "STALE_STORY_COMPLETION_SCOPE",
        "message": (
            "Sprint generation is blocked because the active Story completion "
            "scope excludes all current Sprint candidates. Use workflow next "
            "recovery commands to refresh or reconcile Story planning metadata."
        ),
        "candidate_count": 0,
        "excluded_counts": excluded_counts,
        "story_completion_scope": scope,
    }


def _sprint_setup_story_refinement_blocker(
    candidates: dict[str, Any] | None,
    *,
    workflow: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return blocker when Sprint setup has no refined candidate Stories."""
    if _sprint_candidate_count(candidates) != 0:
        return None
    state = _envelope_data(workflow or {}).get("state")
    state_data = state if isinstance(state, dict) else {}
    if (
        workflow is not None
        and _roadmap_requirements_from_state(state_data)
        and not _uncovered_story_requirements(workflow)
    ):
        return None
    excluded_counts = _sprint_candidate_excluded_counts(candidates)
    non_refined_count = _positive_int_or_none(excluded_counts.get("non_refined"))
    if non_refined_count is None:
        return None
    blocker: dict[str, Any] = {
        "command": "agileforge sprint generate",
        "reason": "SPRINT_CANDIDATES_REQUIRE_STORY_REFINEMENT",
        "message": (
            "Sprint generation is blocked because there are no refined Sprint "
            "candidates. Generate or save Stories for pending requirements "
            "before planning the Sprint."
        ),
        "candidate_count": 0,
        "excluded_counts": excluded_counts,
    }
    scope = _envelope_data(candidates or {}).get("story_completion_scope")
    if isinstance(scope, dict):
        blocker["story_completion_scope"] = scope
    return blocker


def _sprint_setup_candidate_readiness_blocker(
    candidates: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return blocker when nonzero Sprint candidates are not planning-ready."""
    candidate_count = _sprint_candidate_count(candidates)
    if candidate_count in (None, 0):
        return None
    readiness = _envelope_data(candidates or {}).get("readiness")
    if not isinstance(readiness, dict) or readiness.get("status") != "blocked":
        return None
    return {
        "command": "agileforge sprint generate",
        "reason": "SPRINT_CANDIDATES_NOT_PLANNING_READY",
        "message": (
            "Sprint generation is blocked because Sprint candidates are not "
            "planning-ready."
        ),
        "candidate_count": candidate_count,
        "readiness": dict(readiness),
    }


def _story_readiness_repair_blocker(project_id: int) -> dict[str, str] | None:
    """Return blocker when Story readiness repair would be unsafe."""
    from models.core import Sprint, SprintStory, UserStory  # noqa: PLC0415

    with Session(get_engine()) as session:
        active_story_ids = [
            story_id
            for story_id in session.exec(
                select(UserStory.story_id).where(
                    UserStory.product_id == project_id,
                    UserStory.is_refined == True,  # noqa: E712
                    UserStory.is_superseded == False,  # noqa: E712
                )
            ).all()
            if story_id is not None
        ]
        if not active_story_ids:
            return None

        sprint_link = session.exec(
            select(SprintStory.story_id)
            .join(Sprint, cast("Any", Sprint.sprint_id) == SprintStory.sprint_id)
            .where(
                Sprint.product_id == project_id,
                cast("Any", SprintStory.story_id).in_(active_story_ids),
            )
        ).first()
    if sprint_link is None:
        return None
    return {
        "reason": "STORY_READINESS_REPAIR_UNSAFE_AFTER_SPRINT_WORK",
        "message": "Story readiness repair is unsafe after Sprint work exists.",
    }


def _active_backlog_reset_stale_marker(envelope: dict[str, Any]) -> bool:
    """Return whether workflow state has the exact active-reset stale marker."""
    data = _envelope_data(envelope)
    state = data.get("state")
    if not isinstance(state, dict):
        return False

    stale_attempt_id = _non_empty_string(state.get("stale_since_backlog_attempt_id"))
    reset_attempt_id = _non_empty_string(state.get("active_backlog_reset_attempt_id"))
    return (
        state.get("downstream_backlog_stale") is True
        and state.get("stale_backlog_reason") == "active_backlog_reset"
        and stale_attempt_id is not None
        and stale_attempt_id == reset_attempt_id
    )


def _stale_backlog_reason(envelope: dict[str, Any]) -> str | None:
    """Return the active downstream stale reason from workflow state, if present."""
    data = _envelope_data(envelope)
    state = data.get("state")
    if not isinstance(state, dict) or state.get("downstream_backlog_stale") is not True:
        return None
    return _non_empty_string(state.get("stale_backlog_reason"))


def _active_backlog_reset_blocked_commands(
    *,
    include_story_pending: bool = False,
) -> list[dict[str, str]]:
    """Return downstream commands intentionally blocked after active reset."""
    commands: list[dict[str, str]] = []
    if include_story_pending:
        commands.append(
            {
                "command": "agileforge story pending",
                "reason": "DOWNSTREAM_BACKLOG_STALE_AFTER_ACTIVE_RESET",
                "message": (
                    "Story generation remains blocked until downstream reset-stale "
                    "clearing exists."
                ),
            }
        )
    commands.extend(
        [
            {
                "command": "agileforge story generate",
                "reason": "DOWNSTREAM_BACKLOG_STALE_AFTER_ACTIVE_RESET",
                "message": (
                    "Story generation remains blocked until downstream reset-stale "
                    "clearing exists."
                ),
            },
            {
                "command": "agileforge sprint save",
                "reason": "DOWNSTREAM_BACKLOG_STALE_AFTER_ACTIVE_RESET",
                "message": (
                    "Sprint generation remains blocked until downstream reset-stale "
                    "clearing exists."
                ),
            },
        ]
    )
    return commands


def _setup_workflow_next(
    *,
    project_id: int,
    setup_status: str,
    workflow: dict[str, Any],
    authority: dict[str, Any] | None,
    review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return setup substate routing without loading sprint-planning context."""
    if authority is not None and not authority.get("ok"):
        return authority

    data: dict[str, Any] = {
        "project_id": project_id,
        "status": setup_status,
        "next_valid_commands": [],
        "blocked_commands": [],
        "blocked_future_commands": [],
    }
    if setup_status == "authority_pending_review":
        _apply_authority_pending_review_routing(
            data=data,
            project_id=project_id,
            review=review,
        )
    elif setup_status == "authority_rejected":
        _apply_authority_rejected_routing(
            data=data,
            project_id=project_id,
            authority=authority,
            workflow=workflow,
        )
    elif setup_status == "authority_curating":
        _apply_authority_curating_routing(
            data=data,
            project_id=project_id,
            authority=authority,
            workflow=workflow,
        )
    elif setup_status == "failed":
        _apply_failed_setup_routing(
            data=data,
            project_id=project_id,
            workflow=workflow,
        )
    elif setup_status in {"authority_compile_required", "authority_compile_failed"}:
        _apply_authority_compile_routing(
            data=data,
            project_id=project_id,
            workflow=workflow,
            setup_status=setup_status,
        )
    elif setup_status == "authority_compiling":
        _apply_authority_compiling_routing(
            data=data,
            project_id=project_id,
            workflow=workflow,
        )
    elif setup_status == "brownfield_curation_required":
        _apply_brownfield_curation_routing(
            data=data,
            project_id=project_id,
            workflow=workflow,
        )

    data["source_fingerprint"] = canonical_hash(
        {
            "command": WORKFLOW_NEXT_COMMAND,
            "project_id": project_id,
            "status": data["status"],
            "workflow": _fingerprint_input(_envelope_data(workflow)),
            "authority": (
                _fingerprint_input(_envelope_data(authority))
                if authority is not None
                else {}
            ),
            "installed_command_names": sorted(installed_command_names()),
            "next_valid_commands": data["next_valid_commands"],
            "blocked_commands": data["blocked_commands"],
            "blocked_future_commands": data["blocked_future_commands"],
            "decision_actions_after_review": data.get(
                "decision_actions_after_review",
                [],
            ),
            "authority_review_summary": data.get("authority_review_summary", {}),
            "manual_remediation": data.get("manual_remediation", []),
            "next_actions": data.get("next_actions", []),
        }
    )
    return {
        "ok": True,
        "data": data,
        "warnings": [
            *_section_warnings(
                section="workflow",
                source="workflow_state",
                envelope=workflow,
            ),
            *(
                _section_warnings(
                    section="authority",
                    source="authority_status",
                    envelope=authority,
                )
                if authority is not None
                else []
            ),
            *(
                _section_warnings(
                    section="authority_review",
                    source="authority_review",
                    envelope=review,
                )
                if review is not None
                else []
            ),
        ],
        "errors": [],
    }


def _apply_brownfield_curation_routing(
    *,
    data: dict[str, Any],
    project_id: int,
    workflow: dict[str, Any],
) -> None:
    """Publish brownfield curation setup actions without sprint planning."""
    state = _envelope_data(workflow).get("state")
    workflow_state = state if isinstance(state, dict) else {}
    actions: list[dict[str, object]] = []
    for action in _as_list(workflow_state.get("setup_next_actions")):
        if not isinstance(action, Mapping):
            continue
        normalized_action = {str(key): value for key, value in action.items()}
        if isinstance(normalized_action.get("command"), str):
            actions.append(normalized_action)
    if not actions:
        actions = [
            {
                "command": "agileforge brownfield source import",
                "args": {"project_id": project_id},
                "reason": (
                    "Import brownfield source evidence before product-spec curation."
                ),
            },
            {
                "command": "agileforge brownfield scan",
                "args": {"project_id": project_id},
                "reason": (
                    "Record repository facts before product-spec curation."
                ),
            },
            {
                "command": "agileforge brownfield spec draft",
                "args": {"project_id": project_id},
                "reason": (
                    "Create a reviewed product-spec candidate from current "
                    "brownfield artifacts."
                ),
            },
            {
                "command": "agileforge brownfield spec import",
                "args": {"project_id": project_id},
                "reason": (
                    "Import a human-edited agileforge.spec.v1 candidate for approval."
                ),
            },
        ]

    data["next_actions"] = actions
    data["next_valid_commands"] = [str(action["command"]) for action in actions]


def _apply_authority_pending_review_routing(
    *,
    data: dict[str, Any],
    project_id: int,
    review: dict[str, Any] | None,
) -> None:
    """Publish review and decision commands for pending setup authority."""
    data["next_actions"] = [
        {
            "command": f"agileforge authority review --project-id {project_id} --open",
            "installed": True,
            "requires_cli_installation": False,
            "reason": "Review pending authority before accepting or rejecting it.",
        }
    ]
    review_summary = _authority_review_summary(review)
    accept_reason = "Record accepted authority only after review passes."
    accept_action: dict[str, Any] = {
        "command": f"agileforge authority accept --project-id {project_id}",
        "installed": True,
        "requires_cli_installation": False,
        "after_review": True,
        "requires": [],
    }
    if review_summary is not None:
        data["authority_review_summary"] = review_summary
        if review_summary.get("acceptance_status") == "blocked":
            codes = [
                str(code)
                for code in _as_list(review_summary.get("blocking_finding_codes"))
                if str(code)
            ]
            accept_action["blocked"] = True
            accept_action["review_summary"] = review_summary
            accept_action["requires"] = ["fatal_review_resolution"]
            accept_reason = (
                "Authority review has fatal blocking findings; resolve them "
                "and run authority review again before accepting. "
                f"Blocking codes: {', '.join(codes)}."
            )
    accept_action["reason"] = accept_reason
    data["decision_actions_after_review"] = [
        accept_action,
        {
            "command": (
                f"agileforge authority reject --project-id {project_id} "
                "--review-token <review_token> "
                "--reason <reason> --idempotency-key <idempotency_key>"
            ),
            "installed": True,
            "requires_cli_installation": False,
            "after_review": True,
            "requires": ["review_token", "reason", "idempotency_key"],
        },
    ]


def _apply_authority_rejected_routing(
    *,
    data: dict[str, Any],
    project_id: int,
    authority: dict[str, Any] | None,
    workflow: dict[str, Any],
) -> None:
    """Publish regeneration commands for rejected setup authority."""
    authority_data = _envelope_data(authority) if authority is not None else {}
    if authority_data.get("curation_in_progress") is True:
        _apply_authority_curation_in_progress_routing(
            data=data,
            project_id=project_id,
        )
        return
    if authority_data.get("curation_available") is True:
        curation_action = _authority_curate_next_action_from_state(
            project_id=project_id,
            workflow=workflow,
            authority_data=authority_data,
        )
        if curation_action is not None:
            if curation_action["installed"] is True:
                data["next_valid_commands"] = [curation_action["command"]]
            else:
                data["next_valid_commands"] = []
                data["blocked_future_commands"] = [
                    {
                        "command": curation_action["command"],
                        "installed": False,
                        "reason": (
                            "Authority curation is the preferred repair path, but "
                            "the CLI command is not installed yet."
                        ),
                    }
                ]
            data["next_actions"] = [curation_action]
            data["manual_remediation"] = []
            return
        data["next_actions"] = []
        data["blocked_commands"] = [
            {
                "command": AUTHORITY_CURATE_COMMAND,
                "installed": command_is_available(AUTHORITY_CURATE_COMMAND),
                "reason": (
                    "Authority curation is available, but required curation "
                    "guard fields are missing from authority status."
                ),
            }
        ]
        data["manual_remediation"] = ["Inspect authority status curation fields."]
        return

    spec_version_id = _rejected_spec_version_id(authority)
    if spec_version_id is not None:
        command = _authority_regenerate_command(
            project_id=project_id,
            spec_version_id=spec_version_id,
        )
        data["next_valid_commands"] = [command]
        data["next_actions"] = [_authority_regenerate_next_action(command)]
        data["manual_remediation"] = []
        return

    data["next_actions"] = []
    data["blocked_commands"] = [
        {
            "command": AUTHORITY_REGENERATE_COMMAND,
            "installed": True,
            "reason": (
                "Authority rejection requires regeneration, but the "
                "rejected spec version could not be determined."
            ),
        }
    ]
    data["manual_remediation"] = [
        "Inspect authority status for rejected_spec_version_id.",
    ]


def _apply_authority_curation_in_progress_routing(
    *,
    data: dict[str, Any],
    project_id: int,
) -> None:
    """Publish mutation/status inspection commands for active authority curation."""
    commands = [
        f"agileforge mutation list --project-id {project_id} --status pending",
        f"agileforge authority status --project-id {project_id}",
    ]
    data["next_valid_commands"] = commands
    data["next_actions"] = [
        {
            "command": command,
            "installed": True,
            "requires_cli_installation": False,
            "reason": "Inspect the active authority curation attempt.",
        }
        for command in commands
    ]
    data["manual_remediation"] = []


def _apply_authority_curating_routing(
    *,
    data: dict[str, Any],
    project_id: int,
    authority: dict[str, Any] | None,
    workflow: dict[str, Any],
) -> None:
    """Publish active curation inspection or explicit recovery action."""
    authority_data = _envelope_data(authority) if authority is not None else {}
    recovery_action = _authority_curate_recovery_next_action_from_state(
        project_id=project_id,
        workflow=workflow,
        authority_data=authority_data,
    )
    if recovery_action is None:
        _apply_authority_curation_in_progress_routing(
            data=data,
            project_id=project_id,
        )
        return
    if recovery_action["installed"] is True:
        data["next_valid_commands"] = [recovery_action["command"]]
    else:
        data["next_valid_commands"] = []
        data["blocked_future_commands"] = [
            {
                "command": recovery_action["command"],
                "installed": False,
                "reason": (
                    "Authority curation recovery is required, but the CLI "
                    "command is not installed yet."
                ),
            }
        ]
    data["next_actions"] = [recovery_action]
    data["manual_remediation"] = []


def _apply_failed_setup_routing(
    *,
    data: dict[str, Any],
    project_id: int,
    workflow: dict[str, Any],
) -> None:
    """Publish retry commands for failed setup."""
    retry_command = _failed_setup_retry_command(
        project_id=project_id,
        workflow=workflow,
    )
    if retry_command.get("runnable"):
        data["next_valid_commands"] = [str(retry_command["command"])]
        data["next_actions"] = [
            {
                "command": retry_command["command"],
                "installed": True,
                "requires_cli_installation": False,
                "reason": "Retry project setup after fixing the setup failure.",
            }
        ]
        return

    data["blocked_commands"] = [
        {
            "command": retry_command["command"],
            "installed": True,
            "reason": retry_command["reason"],
        }
    ]


def _apply_authority_compile_routing(
    *,
    data: dict[str, Any],
    project_id: int,
    workflow: dict[str, Any],
    setup_status: str,
) -> None:
    """Publish guarded authority compile commands for setup states."""
    compile_command = _authority_compile_command_from_workflow(
        project_id=project_id,
        workflow=workflow,
        expected_setup_status=setup_status,
    )
    if compile_command.get("runnable"):
        command = str(compile_command["command"])
        status_command = f"agileforge authority status --project-id {project_id}"
        data["next_valid_commands"] = [command]
        data["next_actions"] = [
            {
                "command": command,
                "installed": True,
                "requires_cli_installation": False,
                "reason": "Compile project authority with guarded setup inputs.",
            },
            {
                "command": status_command,
                "installed": True,
                "requires_cli_installation": False,
                "reason": "Inspect setup authority status after compile completes.",
            },
        ]
        return

    data["blocked_commands"] = [
        {
            "command": compile_command["command"],
            "installed": True,
            "reason": compile_command["reason"],
        }
    ]


def _apply_authority_compiling_routing(
    *,
    data: dict[str, Any],
    project_id: int,
    workflow: dict[str, Any],
) -> None:
    """Publish mutation inspection commands for an active authority compile."""
    state = _envelope_data(workflow).get("state")
    workflow_state = state if isinstance(state, dict) else {}
    mutation_event_id = workflow_state.get("setup_compile_mutation_event_id")
    is_mutation_event_id = isinstance(mutation_event_id, int) and not isinstance(
        mutation_event_id,
        bool,
    )
    if is_mutation_event_id:
        commands = [
            f"agileforge mutation show --mutation-event-id {mutation_event_id}",
            f"agileforge mutation list --project-id {project_id} --status pending",
            f"agileforge authority status --project-id {project_id}",
        ]
        data["next_valid_commands"] = commands
        data["next_actions"] = [
            {
                "command": command,
                "installed": True,
                "requires_cli_installation": False,
                "reason": "Inspect the active authority compile mutation.",
            }
            for command in commands
        ]
        return

    data["blocked_commands"] = [
        {
            "command": (
                "agileforge mutation show --mutation-event-id <mutation_event_id>"
            ),
            "installed": True,
            "reason": (
                "Active authority compile inspection requires "
                "setup_compile_mutation_event_id in workflow state."
            ),
        }
    ]


def _authority_compile_command_from_workflow(
    *,
    project_id: int,
    workflow: dict[str, Any],
    expected_setup_status: str,
) -> dict[str, Any]:
    """Build a guarded authority compile command from workflow setup fields."""
    state = _envelope_data(workflow).get("state")
    workflow_state = state if isinstance(state, dict) else {}
    spec_file_path = workflow_state.get("setup_spec_file_path")
    spec_hash = workflow_state.get("setup_spec_hash")
    spec_version_id = workflow_state.get("setup_spec_version_id")
    if not isinstance(spec_file_path, str) or not spec_file_path.strip():
        return {
            "command": "agileforge authority compile",
            "runnable": False,
            "reason": (
                "Authority compile requires setup_spec_file_path in workflow state."
            ),
        }
    if not isinstance(spec_hash, str) or not spec_hash.strip():
        return {
            "command": "agileforge authority compile",
            "runnable": False,
            "reason": "Authority compile requires setup_spec_hash in workflow state.",
        }
    if not isinstance(spec_version_id, int) or isinstance(spec_version_id, bool):
        return {
            "command": "agileforge authority compile",
            "runnable": False,
            "reason": (
                "Authority compile requires setup_spec_version_id in workflow state."
            ),
        }
    return {
        "command": (
            "agileforge authority compile "
            f"--project-id {project_id} "
            f"--spec-version-id {spec_version_id} "
            f"--expected-spec-hash {quote(spec_hash)} "
            "--expected-state SETUP_REQUIRED "
            f"--expected-setup-status {expected_setup_status}"
        ),
        "runnable": True,
    }


def _failed_setup_retry_command(
    *,
    project_id: int,
    workflow: dict[str, Any],
) -> dict[str, Any]:
    """Build a failed-setup retry command only when guard inputs are available."""
    state = _envelope_data(workflow).get("state")
    workflow_state = state if isinstance(state, dict) else {}
    placeholder_command = (
        f"agileforge project setup retry --project-id {project_id} "
        "--spec-file <spec-file> --expected-state SETUP_REQUIRED "
        "--expected-context-fingerprint <expected_context_fingerprint>"
    )
    spec_file_path = workflow_state.get("setup_spec_file_path")
    if not isinstance(spec_file_path, str) or not spec_file_path.strip():
        return {
            "command": placeholder_command,
            "runnable": False,
            "reason": (
                "Setup retry requires setup_spec_file_path in workflow state "
                "before a runnable guard can be computed."
            ),
        }
    resolved_spec_path = Path(spec_file_path).expanduser().resolve()
    try:
        context_fingerprint = setup_retry_context_fingerprint(
            project_id=project_id,
            resolved_spec_path=resolved_spec_path,
            workflow_state=workflow_state,
        )
    except (OSError, SpecContentNormalizationError, UnicodeError) as exc:
        return {
            "command": placeholder_command,
            "runnable": False,
            "reason": f"Setup retry guard could not be computed: {exc}",
        }
    return {
        "command": (
            f"agileforge project setup retry --project-id {project_id} "
            f"--spec-file {quote(str(resolved_spec_path))} "
            "--expected-state SETUP_REQUIRED "
            f"--expected-context-fingerprint {context_fingerprint}"
        ),
        "runnable": True,
    }


def _vision_workflow_next(
    *,
    project_id: int,
    workflow: dict[str, Any],
) -> dict[str, Any] | None:
    """Return Vision phase commands for Vision workflow states."""
    fsm_state = _fsm_state_from_envelope(workflow)
    if fsm_state not in {"VISION_INTERVIEW", "VISION_REVIEW", "VISION_PERSISTENCE"}:
        return None

    next_valid_commands: list[str] = []
    blocked_future_commands: list[Any] = []
    status: str | None = None
    if fsm_state == "VISION_PERSISTENCE":
        backlog_command = f"agileforge backlog generate --project-id {project_id}"
        if command_is_available("agileforge backlog generate"):
            next_valid_commands.append(backlog_command)
            status = "next_phase_available"
        else:
            blocked_future_commands.append(
                {
                    "command": backlog_command,
                    "installed": False,
                    "reason": "Backlog CLI is not installed yet.",
                }
            )
            status = "blocked_by_uninstalled_next_phase"

    command_candidates = _vision_command_candidates(
        project_id=project_id,
        fsm_state=fsm_state,
    )
    for command_name, command_text in command_candidates:
        if command_is_available(command_name):
            next_valid_commands.append(command_text)
        else:
            blocked_future_commands.append(command_text)

    data: dict[str, Any] = {
        "project_id": project_id,
        "next_valid_commands": next_valid_commands,
        "blocked_commands": [],
        "blocked_future_commands": blocked_future_commands,
    }
    if status is not None:
        data["status"] = status
    data["source_fingerprint"] = canonical_hash(
        {
            "command": WORKFLOW_NEXT_COMMAND,
            "project_id": project_id,
            "workflow": _fingerprint_input(_envelope_data(workflow)),
            "installed_command_names": sorted(installed_command_names()),
            "next_valid_commands": data["next_valid_commands"],
            "blocked_commands": data["blocked_commands"],
            "blocked_future_commands": data["blocked_future_commands"],
            "status": data.get("status"),
        }
    )
    return {
        "ok": True,
        "data": data,
        "warnings": _section_warnings(
            section="workflow",
            source="workflow_state",
            envelope=workflow,
        ),
        "errors": [],
    }


def _vision_command_candidates(
    *,
    project_id: int,
    fsm_state: str,
) -> list[tuple[str, str]]:
    """Return Vision command candidates for the current Vision state."""
    if fsm_state == "VISION_INTERVIEW":
        return [
            (
                "agileforge vision generate",
                f"agileforge vision generate --project-id {project_id}",
            )
        ]
    if fsm_state == "VISION_PERSISTENCE":
        return []
    return [
        (
            "agileforge vision save",
            f"agileforge vision save --project-id {project_id}",
        ),
        (
            "agileforge vision generate",
            f"agileforge vision generate --project-id {project_id} --input <feedback>",
        ),
    ]


def _backlog_workflow_next(
    *,
    project_id: int,
    workflow: dict[str, Any],
) -> dict[str, Any] | None:
    """Return Backlog phase commands for Backlog workflow states."""
    fsm_state = _fsm_state_from_envelope(workflow)
    if fsm_state not in {
        "BACKLOG_INTERVIEW",
        "BACKLOG_REVIEW",
        "BACKLOG_PERSISTENCE",
    }:
        return None

    next_valid_commands: list[str] = []
    blocked_commands: list[Any] = []
    blocked_future_commands: list[Any] = []
    status: str | None = None
    reset_stale_marker = _active_backlog_reset_stale_marker(workflow)
    if fsm_state == "BACKLOG_PERSISTENCE":
        roadmap_command = f"agileforge roadmap generate --project-id {project_id}"
        if command_is_available("agileforge roadmap generate"):
            next_valid_commands.append(roadmap_command)
            status = (
                "active_backlog_reset_requires_roadmap_regeneration"
                if reset_stale_marker
                else "next_phase_available"
            )
        else:
            blocked_future_commands.append(
                {
                    "command": roadmap_command,
                    "installed": False,
                    "reason": "Roadmap CLI is not installed yet.",
                }
            )
            status = "blocked_by_uninstalled_next_phase"
        if reset_stale_marker:
            blocked_commands.extend(_active_backlog_reset_blocked_commands())

    command_candidates = _backlog_command_candidates(
        project_id=project_id,
        fsm_state=fsm_state,
    )
    for command_name, command_text in command_candidates:
        if command_is_available(command_name):
            next_valid_commands.append(command_text)
        else:
            blocked_future_commands.append(command_text)

    if status is None:
        status = "next_phase_available" if next_valid_commands else None
    data: dict[str, Any] = {
        "project_id": project_id,
        "next_valid_commands": next_valid_commands,
        "blocked_commands": blocked_commands,
        "blocked_future_commands": blocked_future_commands,
    }
    if status is not None:
        data["status"] = status
    data["source_fingerprint"] = canonical_hash(
        {
            "command": WORKFLOW_NEXT_COMMAND,
            "project_id": project_id,
            "workflow": _fingerprint_input(_envelope_data(workflow)),
            "installed_command_names": sorted(installed_command_names()),
            "next_valid_commands": data["next_valid_commands"],
            "blocked_commands": data["blocked_commands"],
            "blocked_future_commands": data["blocked_future_commands"],
            "status": data.get("status"),
        }
    )
    return {
        "ok": True,
        "data": data,
        "warnings": _section_warnings(
            section="workflow",
            source="workflow_state",
            envelope=workflow,
        ),
        "errors": [],
    }


def _backlog_command_candidates(
    *,
    project_id: int,
    fsm_state: str,
) -> list[tuple[str, str]]:
    """Return Backlog command candidates for the current Backlog state."""
    if fsm_state == "BACKLOG_INTERVIEW":
        return [
            (
                "agileforge backlog generate",
                f"agileforge backlog generate --project-id {project_id}",
            )
        ]
    if fsm_state == "BACKLOG_PERSISTENCE":
        return []
    return [
        (
            "agileforge backlog save",
            (
                f"agileforge backlog save --project-id {project_id} "
                "--attempt-id <attempt_id> "
                "--expected-artifact-fingerprint <artifact_fingerprint> "
                "--expected-state BACKLOG_REVIEW "
                "--idempotency-key <idempotency_key>"
            ),
        ),
        (
            "agileforge backlog generate",
            (
                f"agileforge backlog generate --project-id {project_id} "
                "--input <feedback>"
            ),
        ),
    ]


def _roadmap_workflow_next(
    *,
    project_id: int,
    workflow: dict[str, Any],
) -> dict[str, Any] | None:
    """Return Roadmap phase commands for Roadmap workflow states."""
    fsm_state = _fsm_state_from_envelope(workflow)
    if fsm_state not in {
        "ROADMAP_INTERVIEW",
        "ROADMAP_REVIEW",
        "ROADMAP_PERSISTENCE",
    }:
        return None

    next_valid_commands: list[str] = []
    blocked_commands: list[Any] = []
    blocked_future_commands: list[Any] = []
    status: str | None = None
    if fsm_state == "ROADMAP_PERSISTENCE":
        (
            next_valid_commands,
            blocked_commands,
            blocked_future_commands,
            status,
        ) = _roadmap_persistence_story_routing(
            project_id=project_id,
            workflow=workflow,
        )

    command_candidates = _roadmap_command_candidates(
        project_id=project_id,
        fsm_state=fsm_state,
    )
    for command_name, command_text in command_candidates:
        if command_is_available(command_name):
            next_valid_commands.append(command_text)
        else:
            blocked_future_commands.append(command_text)

    if status is None:
        status = "next_phase_available" if next_valid_commands else None
    data: dict[str, Any] = {
        "project_id": project_id,
        "next_valid_commands": next_valid_commands,
        "blocked_commands": blocked_commands,
        "blocked_future_commands": blocked_future_commands,
    }
    if status is not None:
        data["status"] = status
    data["source_fingerprint"] = canonical_hash(
        {
            "command": WORKFLOW_NEXT_COMMAND,
            "project_id": project_id,
            "workflow": _fingerprint_input(_envelope_data(workflow)),
            "installed_command_names": sorted(installed_command_names()),
            "next_valid_commands": data["next_valid_commands"],
            "blocked_commands": data["blocked_commands"],
            "blocked_future_commands": data["blocked_future_commands"],
            "status": data.get("status"),
        }
    )
    return {
        "ok": True,
        "data": data,
        "warnings": _section_warnings(
            section="workflow",
            source="workflow_state",
            envelope=workflow,
        ),
        "errors": [],
    }


def _roadmap_persistence_story_routing(
    *,
    project_id: int,
    workflow: dict[str, Any],
) -> tuple[list[str], list[Any], list[Any], str]:
    """Return Story handoff routing from Roadmap persistence."""
    next_valid_commands: list[str] = []
    blocked_commands: list[Any] = []
    blocked_future_commands: list[Any] = []

    if _active_backlog_reset_stale_marker(workflow):
        blocked_commands.extend(
            _active_backlog_reset_blocked_commands(include_story_pending=True)
        )
        return (
            next_valid_commands,
            blocked_commands,
            blocked_future_commands,
            "blocked_by_stale_active_backlog_reset",
        )

    story_commands = [
        (
            "agileforge story pending",
            f"agileforge story pending --project-id {project_id}",
        ),
        (
            "agileforge story generate",
            (
                f"agileforge story generate --project-id {project_id} "
                "--parent-requirement <parent_requirement>"
            ),
        ),
    ]
    for command_name, command_text in story_commands:
        if command_is_available(command_name):
            next_valid_commands.append(command_text)

    if next_valid_commands:
        return (
            next_valid_commands,
            blocked_commands,
            blocked_future_commands,
            "next_phase_available",
        )

    blocked_future_commands.append(
        {
            "command": f"agileforge story pending --project-id {project_id}",
            "installed": False,
            "reason": "Story phase CLI is not installed yet.",
        }
    )
    return (
        next_valid_commands,
        blocked_commands,
        blocked_future_commands,
        "blocked_by_uninstalled_next_phase",
    )


def _roadmap_command_candidates(
    *,
    project_id: int,
    fsm_state: str,
) -> list[tuple[str, str]]:
    """Return Roadmap command candidates for the current Roadmap state."""
    if fsm_state == "ROADMAP_INTERVIEW":
        return [
            (
                "agileforge roadmap generate",
                f"agileforge roadmap generate --project-id {project_id}",
            )
        ]
    if fsm_state == "ROADMAP_PERSISTENCE":
        return []
    return [
        (
            "agileforge roadmap save",
            (
                f"agileforge roadmap save --project-id {project_id} "
                "--attempt-id <attempt_id> "
                "--expected-artifact-fingerprint <artifact_fingerprint> "
                "--expected-state ROADMAP_REVIEW "
                "--idempotency-key <idempotency_key>"
            ),
        ),
        (
            "agileforge roadmap generate",
            (
                f"agileforge roadmap generate --project-id {project_id} "
                "--input-file <feedback_file>"
            ),
        ),
    ]


def _story_workflow_next(
    *,
    project_id: int,
    workflow: dict[str, Any],
) -> dict[str, Any] | None:
    """Return Story phase commands for Story workflow states."""
    fsm_state = _fsm_state_from_envelope(workflow)
    if fsm_state not in {
        "STORY_INTERVIEW",
        "STORY_REVIEW",
        "STORY_PERSISTENCE",
    }:
        return None

    next_valid_commands: list[str] = []
    blocked_future_commands: list[Any] = []
    for command_name, command_text in _story_command_candidates(
        project_id=project_id,
        fsm_state=fsm_state,
        workflow=workflow,
    ):
        if command_is_available(command_name):
            next_valid_commands.append(command_text)
        else:
            blocked_future_commands.append(command_text)

    data: dict[str, Any] = {
        "project_id": project_id,
        "next_valid_commands": next_valid_commands,
        "blocked_commands": [],
        "blocked_future_commands": blocked_future_commands,
        "status": "next_phase_available" if next_valid_commands else None,
    }
    data["source_fingerprint"] = canonical_hash(
        {
            "command": WORKFLOW_NEXT_COMMAND,
            "project_id": project_id,
            "workflow": _fingerprint_input(_envelope_data(workflow)),
            "installed_command_names": sorted(installed_command_names()),
            "next_valid_commands": data["next_valid_commands"],
            "blocked_commands": data["blocked_commands"],
            "blocked_future_commands": data["blocked_future_commands"],
            "status": data["status"],
        }
    )
    return {
        "ok": True,
        "data": data,
        "warnings": _section_warnings(
            section="workflow",
            source="workflow_state",
            envelope=workflow,
        ),
        "errors": [],
    }


def _story_command_candidates(
    *,
    project_id: int,
    fsm_state: str,
    workflow: dict[str, Any],
) -> list[tuple[str, str]]:
    """Return Story command candidates for the current Story state."""
    pending_command = (
        "agileforge story pending",
        f"agileforge story pending --project-id {project_id}",
    )
    generate_command = (
        "agileforge story generate",
        (
            f"agileforge story generate --project-id {project_id} "
            "--parent-requirement <parent_requirement>"
        ),
    )
    if fsm_state == "STORY_INTERVIEW":
        return _story_interview_command_candidates(
            project_id=project_id,
            workflow=workflow,
            pending_command=pending_command,
            generate_command=generate_command,
        )
    if fsm_state == "STORY_PERSISTENCE":
        if not _story_coverage_is_complete(workflow):
            scoped_complete_commands = _covered_story_milestone_complete_commands(
                project_id=project_id,
                workflow=workflow,
            )
            selection_complete_commands = _covered_story_selection_complete_command(
                project_id=project_id,
                workflow=workflow,
            )
            if scoped_complete_commands or selection_complete_commands:
                return [
                    pending_command,
                    generate_command,
                    *_story_dependency_command_candidates(
                        project_id=project_id,
                        expected_state="STORY_PERSISTENCE",
                    ),
                    *scoped_complete_commands,
                    *selection_complete_commands,
                ]
            return [pending_command, generate_command]
        selection_complete_commands = _covered_story_selection_complete_command(
            project_id=project_id,
            workflow=workflow,
        )
        return [
            pending_command,
            *_story_dependency_command_candidates(
                project_id=project_id,
                expected_state="STORY_PERSISTENCE",
            ),
            (
                "agileforge story complete",
                (
                    f"agileforge story complete --project-id {project_id} "
                    "--expected-state STORY_PERSISTENCE "
                    "--idempotency-key <idempotency_key>"
                ),
            ),
            *selection_complete_commands,
        ]
    return [
        (
            "agileforge story history",
            (
                f"agileforge story history --project-id {project_id} "
                "--parent-requirement <parent_requirement>"
            ),
        ),
        (
            "agileforge story save",
            (
                f"agileforge story save --project-id {project_id} "
                "--parent-requirement <parent_requirement> "
                "--attempt-id <attempt_id> "
                "--expected-artifact-fingerprint <artifact_fingerprint> "
                "--expected-state STORY_REVIEW "
                "--idempotency-key <idempotency_key>"
            ),
        ),
        (
            "agileforge story generate",
            (
                f"agileforge story generate --project-id {project_id} "
                "--parent-requirement <parent_requirement> "
                "--input <feedback>"
            ),
        ),
    ]


def _story_interview_command_candidates(
    *,
    project_id: int,
    workflow: dict[str, Any],
    pending_command: tuple[str, str],
    generate_command: tuple[str, str],
) -> list[tuple[str, str]]:
    """Return Story commands for interview state, including recovery bridges."""
    refinement_requirement = _non_saveable_story_refinement_requirement(workflow)
    if refinement_requirement is not None:
        return _story_refinement_commands_for_requirement(
            project_id=project_id,
            parent_requirement=refinement_requirement,
        )
    scoped_complete_commands = _existing_story_scope_complete_commands(
        project_id=project_id,
        workflow=workflow,
        expected_state="STORY_INTERVIEW",
    )
    if scoped_complete_commands:
        return [pending_command, *scoped_complete_commands]
    if _story_coverage_is_complete(workflow):
        return [
            pending_command,
            (
                "agileforge story complete",
                (
                    f"agileforge story complete --project-id {project_id} "
                    "--expected-state STORY_INTERVIEW "
                    "--idempotency-key <idempotency_key>"
                ),
            ),
        ]
    review_candidate = _saveable_story_review_candidate(workflow)
    if review_candidate is None:
        return [pending_command, generate_command]
    return _story_review_commands_for_candidate(
        project_id=project_id,
        review_candidate=review_candidate,
    )


def _story_review_commands_for_candidate(
    *,
    project_id: int,
    review_candidate: dict[str, str],
) -> list[tuple[str, str]]:
    """Return guarded Story review commands for one saveable draft."""
    parent_flag = _story_parent_requirement_flag(review_candidate["parent_requirement"])
    return [
        (
            "agileforge story history",
            f"agileforge story history --project-id {project_id} {parent_flag}",
        ),
        (
            "agileforge story save",
            (
                f"agileforge story save --project-id {project_id} "
                f"{parent_flag} "
                f"--attempt-id {review_candidate['attempt_id']} "
                "--expected-artifact-fingerprint "
                f"{review_candidate['artifact_fingerprint']} "
                "--expected-state STORY_REVIEW "
                "--idempotency-key <idempotency_key>"
            ),
        ),
        (
            "agileforge story generate",
            (
                f"agileforge story generate --project-id {project_id} "
                f"{parent_flag} "
                "--input <feedback>"
            ),
        ),
    ]


def _story_refinement_commands_for_requirement(
    *,
    project_id: int,
    parent_requirement: str,
) -> list[tuple[str, str]]:
    """Return Story commands for refining an existing non-saveable draft."""
    parent_flag = _story_parent_requirement_flag(parent_requirement)
    return [
        (
            "agileforge story history",
            f"agileforge story history --project-id {project_id} {parent_flag}",
        ),
        (
            "agileforge story generate",
            (
                f"agileforge story generate --project-id {project_id} "
                f"{parent_flag} "
                "--input <feedback>"
            ),
        ),
    ]


def _non_saveable_story_refinement_requirement(workflow: dict[str, Any]) -> str | None:
    """Return a requirement whose latest Story attempt is not saveable."""
    state = _envelope_data(workflow).get("state")
    state_data = state if isinstance(state, dict) else {}
    interview_runtime = state_data.get("interview_runtime")
    if not isinstance(interview_runtime, dict):
        return None
    story_runtime = interview_runtime.get("story")
    if not isinstance(story_runtime, dict):
        return None

    from services.phases.story_service import story_interview_summary  # noqa: PLC0415

    for parent_requirement, runtime in story_runtime.items():
        if not isinstance(parent_requirement, str) or not parent_requirement:
            continue
        if not isinstance(runtime, dict):
            continue
        summary = story_interview_summary(runtime)
        quality = summary.get("quality")
        if isinstance(quality, dict) and quality.get("saveable") is False:
            return parent_requirement
    return None


def _saveable_story_review_candidate(
    workflow: dict[str, Any],
) -> dict[str, str] | None:
    """Return a saveable Story draft from runtime state, if one exists."""
    state = _envelope_data(workflow).get("state")
    state_data = state if isinstance(state, dict) else {}
    interview_runtime = state_data.get("interview_runtime")
    if not isinstance(interview_runtime, dict):
        return None
    story_runtime = interview_runtime.get("story")
    if not isinstance(story_runtime, dict):
        return None

    for parent_requirement, runtime in story_runtime.items():
        if not isinstance(parent_requirement, str) or not parent_requirement:
            continue
        if not isinstance(runtime, dict):
            continue
        if _story_requirement_is_satisfied(
            state_data,
            parent_requirement=parent_requirement,
        ):
            continue
        from services.phases.story_service import story_save_payload  # noqa: PLC0415

        if story_save_payload(runtime) is None:
            continue
        draft_projection = runtime.get("draft_projection")
        if not isinstance(draft_projection, dict):
            continue
        attempt_id = _non_empty_string(
            draft_projection.get("latest_reusable_attempt_id")
        )
        artifact_fingerprint = _non_empty_string(
            draft_projection.get("artifact_fingerprint")
        )
        if attempt_id is None or artifact_fingerprint is None:
            continue
        return {
            "parent_requirement": parent_requirement,
            "attempt_id": attempt_id,
            "artifact_fingerprint": artifact_fingerprint,
        }
    return None


def _existing_story_scope_complete_commands(
    *,
    project_id: int,
    workflow: dict[str, Any],
    expected_state: str,
) -> list[tuple[str, str]]:
    """Return completion commands for an already-recorded Story scope."""
    command = _existing_story_scope_complete_command(
        project_id=project_id,
        workflow=workflow,
        expected_state=expected_state,
    )
    return [] if command is None else [command]


def _existing_story_scope_complete_command(
    *,
    project_id: int,
    workflow: dict[str, Any],
    expected_state: str,
) -> tuple[str, str] | None:
    """Return a completion command for an already-recorded Story scope."""
    scope_data = _covered_existing_story_scope(workflow)
    if scope_data is None:
        return None

    scope_name, scope_id, requirements = scope_data
    if scope_name == "milestone" and scope_id is not None:
        return (
            "agileforge story complete",
            (
                f"agileforge story complete --project-id {project_id} "
                f"--expected-state {expected_state} "
                f"--scope milestone --scope-id {scope_id} "
                "--idempotency-key <idempotency_key>"
            ),
        )
    if scope_name == "selection":
        return _story_selection_complete_command(
            project_id=project_id,
            requirements=requirements,
            expected_state=expected_state,
        )
    return None


def _covered_existing_story_scope(
    workflow: dict[str, Any],
) -> tuple[str, str | None, list[str]] | None:
    """Return stored Story scope data when every scoped requirement is covered."""
    state = _envelope_data(workflow).get("state")
    state_data = state if isinstance(state, dict) else {}
    scope = state_data.get("story_completion_scope")
    if not isinstance(scope, dict):
        return None
    requirements = [
        requirement
        for requirement in scope.get("requirements", [])
        if isinstance(requirement, str) and requirement
    ]
    if not requirements:
        return None
    if not all(
        _story_requirement_is_satisfied(
            state_data,
            parent_requirement=requirement,
        )
        for requirement in requirements
    ):
        return None

    scope_name = _non_empty_string(scope.get("scope"))
    if scope_name not in {"milestone", "selection"}:
        return None
    return (scope_name, _non_empty_string(scope.get("scope_id")), requirements)


def _story_selection_complete_command(
    *,
    project_id: int,
    requirements: list[str],
    expected_state: str,
) -> tuple[str, str]:
    """Return a Story selection completion command."""
    parent_requirement_flags = " ".join(
        _story_parent_requirement_flag(requirement) for requirement in requirements
    )
    return (
        "agileforge story complete",
        (
            f"agileforge story complete --project-id {project_id} "
            f"--expected-state {expected_state} "
            "--scope selection "
            f"{parent_requirement_flags} "
            "--idempotency-key <idempotency_key>"
        ),
    )


def _story_requirement_is_covered(
    state_data: dict[str, Any],
    *,
    parent_requirement: str,
) -> bool:
    """Return whether a Story requirement is saved or merged."""
    saved = state_data.get("story_saved")
    saved_map = saved if isinstance(saved, dict) else {}
    return bool(saved_map.get(parent_requirement)) or (
        _story_requirement_has_merge_resolution(
            state_data,
            parent_requirement=parent_requirement,
        )
    )


def _story_requirement_is_satisfied(
    state_data: dict[str, Any],
    *,
    parent_requirement: str,
) -> bool:
    """Return whether a requirement needs no more Story work now."""
    from services.phases.story_service import (  # noqa: PLC0415
        requirement_reconciliation_satisfies_story_requirement,
    )

    return _story_requirement_is_covered(
        state_data,
        parent_requirement=parent_requirement,
    ) or requirement_reconciliation_satisfies_story_requirement(
        state_data,
        parent_requirement=parent_requirement,
    )


def _covered_story_milestone_complete_commands(
    *,
    project_id: int,
    workflow: dict[str, Any],
) -> list[tuple[str, str]]:
    """Return scoped Story complete commands for covered roadmap milestones."""
    state = _envelope_data(workflow).get("state")
    state_data = state if isinstance(state, dict) else {}
    releases = state_data.get("roadmap_releases")
    if not isinstance(releases, list):
        return []

    commands: list[tuple[str, str]] = []
    for release_index, release in enumerate(releases):
        if not isinstance(release, dict):
            continue
        release_data = cast("dict[str, Any]", release)
        items = release_data.get("items")
        if not isinstance(items, list):
            continue
        requirements = [item for item in items if isinstance(item, str) and item]
        if not requirements:
            continue
        if not all(
            _story_requirement_is_satisfied(
                state_data,
                parent_requirement=requirement,
            )
            for requirement in requirements
        ):
            continue
        scope_id = f"milestone_{release_index}"
        commands.append(
            (
                "agileforge story complete",
                (
                    f"agileforge story complete --project-id {project_id} "
                    "--expected-state STORY_PERSISTENCE "
                    f"--scope milestone --scope-id {scope_id} "
                    "--idempotency-key <idempotency_key>"
                ),
            )
        )
    return commands


def _covered_story_selection_complete_command(
    *,
    project_id: int,
    workflow: dict[str, Any],
) -> list[tuple[str, str]]:
    """Return one scoped Story complete command for covered roadmap requirements."""
    state = _envelope_data(workflow).get("state")
    state_data = state if isinstance(state, dict) else {}
    requirements = _roadmap_requirements_from_state(state_data)
    covered_requirements = [
        requirement
        for requirement in requirements
        if _story_requirement_is_satisfied(
            state_data,
            parent_requirement=requirement,
        )
    ]
    if not covered_requirements:
        return []

    parent_requirement_flags = " ".join(
        _story_parent_requirement_flag(requirement)
        for requirement in covered_requirements
    )
    return [
        (
            "agileforge story complete",
            (
                f"agileforge story complete --project-id {project_id} "
                "--expected-state STORY_PERSISTENCE "
                "--scope selection "
                f"{parent_requirement_flags} "
                "--idempotency-key <idempotency_key>"
            ),
        )
    ]


def _story_parent_requirement_flag(parent_requirement: str) -> str:
    """Return a quoted parent requirement flag for workflow command help."""
    quoted = (
        parent_requirement.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )
    return f'--parent-requirement "{quoted}"'


def _story_coverage_is_complete(workflow: dict[str, Any]) -> bool:
    """Return whether all Roadmap requirements have Story coverage."""
    state = _envelope_data(workflow).get("state")
    state_data = state if isinstance(state, dict) else {}
    requirements = _roadmap_requirements_from_state(state_data)
    if not requirements:
        return False

    return all(
        _story_requirement_is_satisfied(
            state_data,
            parent_requirement=requirement,
        )
        for requirement in requirements
    )


def _uncovered_story_requirements(workflow: dict[str, Any]) -> list[str]:
    """Return Roadmap requirements that still need saved or merged Stories."""
    state = _envelope_data(workflow).get("state")
    state_data = state if isinstance(state, dict) else {}
    return [
        requirement
        for requirement in _roadmap_requirements_from_state(state_data)
        if not _story_requirement_is_satisfied(
            state_data,
            parent_requirement=requirement,
        )
    ]


def _roadmap_requirements_from_state(state: dict[str, Any]) -> list[str]:
    """Return Roadmap requirement titles from workflow state."""
    releases = state.get("roadmap_releases")
    if not isinstance(releases, list):
        return []
    requirements: list[str] = []
    for release in releases:
        if not isinstance(release, dict):
            continue
        items = release.get("items")
        if not isinstance(items, list):
            continue
        requirements.extend(item for item in items if isinstance(item, str) and item)
    return requirements


def _story_requirement_has_merge_resolution(
    state: dict[str, Any],
    *,
    parent_requirement: str,
) -> bool:
    """Return whether a Story requirement has an accepted merge resolution."""
    interview_runtime = state.get("interview_runtime")
    if not isinstance(interview_runtime, dict):
        return False
    story_runtime = interview_runtime.get("story")
    if not isinstance(story_runtime, dict):
        return False
    runtime = story_runtime.get(parent_requirement)
    if not isinstance(runtime, dict):
        return False
    resolution = runtime.get("resolution_projection")
    if not isinstance(resolution, dict):
        return False
    return resolution.get("status") == "merged"


def _route_story_readiness_repair(
    *,
    project_id: int,
    next_valid_commands: list[str],
    blocked_commands: list[Any],
    blocked_future_commands: list[Any],
) -> dict[str, str] | None:
    """Route repair-readiness according to the same safety guard as the command."""
    repair_command = (
        f"agileforge story repair-readiness --project-id {project_id} "
        "--expected-state SPRINT_SETUP "
        "--idempotency-key <idempotency_key>"
    )
    if not command_is_available("agileforge story repair-readiness"):
        blocked_future_commands.append(repair_command)
        return None

    repair_blocker = _story_readiness_repair_blocker(project_id)
    if repair_blocker is None:
        next_valid_commands.append(repair_command)
        return None

    blocked_commands.append({"command": repair_command, **repair_blocker})
    return repair_blocker


def _story_completion_scope_repair_command(
    *,
    project_id: int,
    stale_scope_blocker: dict[str, Any],
) -> str | None:
    """Return the non-destructive Story completion-scope repair command."""
    scope = stale_scope_blocker.get("story_completion_scope")
    if not isinstance(scope, dict):
        return None
    scope_id = _non_empty_string(scope.get("scope_id"))
    if scope_id is None:
        return None
    return (
        f"agileforge story repair-completion-scope --project-id {project_id} "
        "--expected-state SPRINT_SETUP "
        f"--expected-scope-id {quote(scope_id)} "
        "--idempotency-key <idempotency_key>"
    )


def _route_story_completion_scope_repair(
    *,
    project_id: int,
    stale_scope_blocker: dict[str, Any],
    next_valid_commands: list[str],
    blocked_future_commands: list[Any],
) -> str | None:
    """Route non-destructive repair for stale Story completion scope."""
    repair_command = _story_completion_scope_repair_command(
        project_id=project_id,
        stale_scope_blocker=stale_scope_blocker,
    )
    if repair_command is None:
        return None
    if command_is_available("agileforge story repair-completion-scope"):
        next_valid_commands.append(repair_command)
        return repair_command
    blocked_future_commands.append(repair_command)
    return None


def _story_scope_reconcile_manual_review_blocker() -> dict[str, str]:
    """Return the blocked manual-review marker for stale Story scope."""
    return {
        "command": "agileforge story reconcile",
        "reason": "STALE_STORY_SCOPE_RECONCILE_REQUIRES_MANUAL_REVIEW",
        "message": (
            "Story reconciliation can terminally archive active Stories. "
            "workflow next does not recommend terminal reconciliation for stale "
            "Story completion scope automatically."
        ),
    }


def _route_story_scope_reconcile_for_blocker(
    *,
    stale_scope_blocker: dict[str, Any],
    story_readiness_repair_blocker: dict[str, str] | None,
    blocked_commands: list[Any],
) -> None:
    """Route Story reconciliation when readiness repair is unsafe."""
    if story_readiness_repair_blocker is None:
        return
    scope = stale_scope_blocker.get("story_completion_scope")
    if not isinstance(scope, dict):
        return
    blocked_commands.append(_story_scope_reconcile_manual_review_blocker())


def _first_pending_story_requirement(
    story_pending: dict[str, Any] | None,
) -> str | None:
    """Return the first pending Story requirement from the pending projection."""
    if story_pending is None or story_pending.get("ok") is not True:
        return None
    grouped_items = _envelope_data(story_pending).get("grouped_items")
    if not isinstance(grouped_items, list):
        return None
    for group in grouped_items:
        if not isinstance(group, dict):
            continue
        requirements = group.get("requirements")
        if not isinstance(requirements, list):
            continue
        for item in requirements:
            if not isinstance(item, dict):
                continue
            if str(item.get("status") or "").strip().lower() != "pending":
                continue
            requirement = str(item.get("requirement") or "").strip()
            if requirement:
                return requirement
    return None


def _route_story_generation_for_sprint_setup(
    *,
    project_id: int,
    story_pending: dict[str, Any] | None,
    next_valid_commands: list[str],
    blocked_future_commands: list[Any],
) -> str | None:
    """Route Sprint setup back to the next pending Story requirement."""
    pending_command = f"agileforge story pending --project-id {project_id}"
    if command_is_available("agileforge story pending"):
        next_valid_commands.append(pending_command)
    else:
        blocked_future_commands.append(pending_command)

    requirement = _first_pending_story_requirement(story_pending)
    if requirement is None:
        return None
    generate_command = (
        f"agileforge story generate --project-id {project_id} "
        f"--parent-requirement {quote(requirement)}"
    )
    if command_is_available("agileforge story generate"):
        next_valid_commands.append(generate_command)
        return generate_command
    blocked_future_commands.append(generate_command)
    return None


def _sprint_workflow_status(
    *,
    blocker_kind: str | None,
    readiness_repair_blocked: bool,
    has_completion_scope_repair_command: bool,
    has_story_generation_command: bool,
    has_next_valid_commands: bool,
) -> str | None:
    """Return workflow-next status for Sprint routing."""
    if blocker_kind is None:
        return "next_phase_available" if has_next_valid_commands else None
    if blocker_kind == "story_refinement":
        if has_story_generation_command:
            return "sprint_setup_story_generation_required"
        return "sprint_setup_story_generation_blocked"
    if not readiness_repair_blocked or has_completion_scope_repair_command:
        return "sprint_setup_story_scope_repair_required"
    return "sprint_setup_story_scope_repair_blocked"


def _sprint_dependency_mutation_suppressed(
    *,
    command_name: str,
    has_setup_blocker: bool,
) -> bool:
    """Return whether dependency mutation cannot resolve the current blocker."""
    return has_setup_blocker and command_name in {
        "agileforge story dependencies propose",
        "agileforge story dependencies apply",
    }


def _sprint_generate_blocker(
    *,
    command_name: str,
    stale_scope_blocker: dict[str, Any] | None,
    story_refinement_blocker: dict[str, Any] | None,
    candidate_readiness_blocker: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the active blocker for Sprint generation, if any."""
    if command_name != "agileforge sprint generate":
        return None
    return (
        stale_scope_blocker
        or story_refinement_blocker
        or candidate_readiness_blocker
    )


def _sprint_setup_blockers(
    *,
    fsm_state: str | None,
    sprint_candidates: dict[str, Any] | None,
    workflow: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Return active Sprint setup blockers in priority order."""
    if fsm_state != "SPRINT_SETUP":
        return None, None, None
    stale_scope_blocker = _sprint_setup_stale_story_scope_blocker(sprint_candidates)
    story_refinement_blocker = (
        _sprint_setup_story_refinement_blocker(
            sprint_candidates,
            workflow=workflow,
        )
        if stale_scope_blocker is None
        else None
    )
    candidate_readiness_blocker = (
        _sprint_setup_candidate_readiness_blocker(sprint_candidates)
        if stale_scope_blocker is None and story_refinement_blocker is None
        else None
    )
    return stale_scope_blocker, story_refinement_blocker, candidate_readiness_blocker


def _route_sprint_command_candidate(  # noqa: PLR0913
    *,
    command_name: str,
    command_text: str,
    has_setup_blocker: bool,
    save_blocker: dict[str, Any] | None,
    stale_scope_blocker: dict[str, Any] | None,
    story_refinement_blocker: dict[str, Any] | None,
    candidate_readiness_blocker: dict[str, Any] | None,
    next_valid_commands: list[str],
    blocked_commands: list[Any],
    blocked_future_commands: list[Any],
) -> None:
    """Route one Sprint command candidate into next-valid or blocked output."""
    if _sprint_dependency_mutation_suppressed(
        command_name=command_name,
        has_setup_blocker=has_setup_blocker,
    ):
        return
    if command_name == "agileforge sprint save" and save_blocker is not None:
        blocked_commands.append(
            {
                "command": command_name,
                **save_blocker,
            }
        )
        return
    generate_blocker = _sprint_generate_blocker(
        command_name=command_name,
        stale_scope_blocker=stale_scope_blocker,
        story_refinement_blocker=story_refinement_blocker,
        candidate_readiness_blocker=candidate_readiness_blocker,
    )
    if generate_blocker is not None:
        blocked_commands.append(generate_blocker)
        return
    if command_is_available(command_name):
        next_valid_commands.append(command_text)
    else:
        blocked_future_commands.append(command_text)


def _sprint_workflow_next(
    *,
    project_id: int,
    workflow: dict[str, Any],
    sprint_candidates: dict[str, Any] | None = None,
    story_pending: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return Sprint phase commands for Sprint workflow states."""
    fsm_state = _fsm_state_from_envelope(workflow)
    if fsm_state not in {
        "SPRINT_SETUP",
        "SPRINT_DRAFT",
        "SPRINT_PERSISTENCE",
        "SPRINT_VIEW",
        "SPRINT_COMPLETE",
    }:
        return None

    if fsm_state == "SPRINT_COMPLETE":
        return _sprint_complete_workflow_next(
            project_id=project_id,
            workflow=workflow,
        )
    next_valid_commands: list[str] = []
    blocked_commands: list[Any] = []
    blocked_future_commands: list[Any] = []
    save_blocker = (
        _sprint_save_blocker(workflow) if fsm_state == "SPRINT_DRAFT" else None
    )
    stale_scope_blocker, story_refinement_blocker, candidate_readiness_blocker = (
        _sprint_setup_blockers(
            fsm_state=fsm_state,
            sprint_candidates=sprint_candidates,
            workflow=workflow,
        )
    )
    has_setup_blocker = (
        stale_scope_blocker is not None or story_refinement_blocker is not None
    )
    story_readiness_repair_blocker = None
    story_completion_scope_repair_command = None
    story_generation_command = None
    for command_name, command_text in _sprint_command_candidates(
        project_id=project_id,
        fsm_state=fsm_state,
    ):
        _route_sprint_command_candidate(
            command_name=command_name,
            command_text=command_text,
            has_setup_blocker=has_setup_blocker,
            save_blocker=save_blocker,
            stale_scope_blocker=stale_scope_blocker,
            story_refinement_blocker=story_refinement_blocker,
            candidate_readiness_blocker=candidate_readiness_blocker,
            next_valid_commands=next_valid_commands,
            blocked_commands=blocked_commands,
            blocked_future_commands=blocked_future_commands,
        )

    if stale_scope_blocker is not None:
        story_readiness_repair_blocker = _route_story_readiness_repair(
            project_id=project_id,
            next_valid_commands=next_valid_commands,
            blocked_commands=blocked_commands,
            blocked_future_commands=blocked_future_commands,
        )
        if story_readiness_repair_blocker is not None:
            story_completion_scope_repair_command = (
                _route_story_completion_scope_repair(
                    project_id=project_id,
                    stale_scope_blocker=stale_scope_blocker,
                    next_valid_commands=next_valid_commands,
                    blocked_future_commands=blocked_future_commands,
                )
            )
        _route_story_scope_reconcile_for_blocker(
            stale_scope_blocker=stale_scope_blocker,
            story_readiness_repair_blocker=story_readiness_repair_blocker,
            blocked_commands=blocked_commands,
        )
    if story_refinement_blocker is not None:
        story_generation_command = _route_story_generation_for_sprint_setup(
            project_id=project_id,
            story_pending=story_pending,
            next_valid_commands=next_valid_commands,
            blocked_future_commands=blocked_future_commands,
        )

    data: dict[str, Any] = {
        "project_id": project_id,
        "next_valid_commands": next_valid_commands,
        "blocked_commands": blocked_commands,
        "blocked_future_commands": blocked_future_commands,
        "status": _sprint_workflow_status(
            blocker_kind=(
                "stale_scope"
                if stale_scope_blocker is not None
                else "story_refinement"
                if story_refinement_blocker is not None
                else None
            ),
            readiness_repair_blocked=story_readiness_repair_blocker is not None,
            has_completion_scope_repair_command=(
                story_completion_scope_repair_command is not None
            ),
            has_story_generation_command=story_generation_command is not None,
            has_next_valid_commands=bool(next_valid_commands),
        ),
    }
    data["source_fingerprint"] = canonical_hash(
        {
            "command": WORKFLOW_NEXT_COMMAND,
            "project_id": project_id,
            "workflow": _fingerprint_input(_envelope_data(workflow)),
            "sprint_candidates": (
                _fingerprint_input(_envelope_data(sprint_candidates))
                if sprint_candidates is not None
                else None
            ),
            "story_pending": (
                _fingerprint_input(_envelope_data(story_pending))
                if story_pending is not None
                else None
            ),
            "installed_command_names": sorted(installed_command_names()),
            "next_valid_commands": data["next_valid_commands"],
            "blocked_commands": data["blocked_commands"],
            "blocked_future_commands": data["blocked_future_commands"],
            "status": data["status"],
        }
    )
    return {
        "ok": True,
        "data": data,
        "warnings": _section_warnings(
            section="workflow",
            source="workflow_state",
            envelope=workflow,
        ),
        "errors": [],
    }


def _sprint_complete_workflow_next(
    *,
    project_id: int,
    workflow: dict[str, Any],
    sprint_candidates: dict[str, Any] | None = None,
    scope_extension_preconditions: ScopeExtensionPreconditions | None = None,
) -> dict[str, Any]:
    """Return workflow-next routing for SPRINT_COMPLETE."""
    next_valid_commands: list[str] = []
    blocked_commands: list[Any] = []
    blocked_future_commands: list[Any] = []
    state = _envelope_data(workflow).get("state")
    state_data = state if isinstance(state, dict) else {}
    current_triage = current_triage_for_latest_sprint(state_data)
    stale_backlog_reason = _stale_backlog_reason(workflow)
    if stale_backlog_reason == "active_backlog_reset":
        return _sprint_complete_next_response(
            project_id=project_id,
            workflow=workflow,
            next_valid_commands=next_valid_commands,
            blocked_commands=_active_backlog_reset_blocked_commands(),
            blocked_future_commands=blocked_future_commands,
            status="post_sprint_blocked_by_stale_backlog",
        )
    if stale_backlog_reason == "refined_backlog_recorded":
        return _post_sprint_refined_backlog_recorded_next(
            project_id=project_id,
            workflow=workflow,
        )
    if current_triage is None and post_sprint_triage_required(state_data):
        next_actions: list[dict[str, Any]] = []
        for command_name, command_text in _post_sprint_triage_required_commands(
            project_id=project_id,
        ):
            installed = command_is_available(command_name)
            if command_name == "agileforge sprint triage":
                next_actions = [
                    _post_sprint_triage_required_next_action(
                        command=command_text,
                        installed=installed,
                    )
                ]
            if installed:
                next_valid_commands.append(command_text)
            else:
                blocked_future_commands.append(command_text)
        return _sprint_complete_next_response(
            project_id=project_id,
            workflow=workflow,
            next_valid_commands=next_valid_commands,
            blocked_commands=_post_sprint_triage_required_blocked_commands(
                project_id=project_id,
                workflow=workflow,
            ),
            blocked_future_commands=blocked_future_commands,
            status="post_sprint_triage_required",
            next_actions=next_actions,
        )

    impact_next = _post_sprint_triage_impact_next(
        project_id=project_id,
        workflow=workflow,
        triage=current_triage,
        sprint_candidates=sprint_candidates,
        scope_extension_preconditions=scope_extension_preconditions,
    )
    if impact_next is not None:
        return impact_next

    for command_name, command_text in _sprint_complete_backlog_refinement_commands(
        project_id=project_id,
        workflow=workflow,
    ):
        if command_is_available(command_name):
            next_valid_commands.append(command_text)
        else:
            blocked_future_commands.append(command_text)
    status = (
        "sprint_complete_backlog_refinement_available"
        if next_valid_commands
        else "sprint_complete"
    )
    return _sprint_complete_next_response(
        project_id=project_id,
        workflow=workflow,
        next_valid_commands=next_valid_commands,
        blocked_commands=blocked_commands,
        blocked_future_commands=blocked_future_commands,
        status=status,
    )


def _post_sprint_triage_impact_next(
    *,
    project_id: int,
    workflow: dict[str, Any],
    triage: dict[str, Any] | None,
    sprint_candidates: dict[str, Any] | None = None,
    scope_extension_preconditions: ScopeExtensionPreconditions | None = None,
) -> dict[str, Any] | None:
    """Return routed workflow-next data for recorded triage impacts."""
    if triage is None:
        return None
    triage_impact = triage.get("impact")
    if triage_impact == "none":
        impact_next = _post_sprint_none_next(
            project_id=project_id,
            workflow=workflow,
            sprint_candidates=sprint_candidates,
            scope_extension_preconditions=scope_extension_preconditions,
        )
    elif triage_impact == "story":
        impact_next = _post_sprint_story_next(
            project_id=project_id,
            workflow=workflow,
            triage=triage,
        )
    elif triage_impact == "task":
        impact_next = _post_sprint_task_next(
            project_id=project_id,
            workflow=workflow,
            triage=triage,
        )
    elif triage_impact == "backlog":
        impact_next = _post_sprint_backlog_next(
            project_id=project_id,
            workflow=workflow,
        )
    elif triage_impact == "roadmap":
        impact_next = _post_sprint_roadmap_next(
            project_id=project_id,
            workflow=workflow,
        )
    elif triage_impact == "multiple":
        impact_next = _post_sprint_multiple_next(
            project_id=project_id,
            workflow=workflow,
            triage=triage,
        )
    else:
        impact_next = None
    return impact_next


def _post_sprint_none_next(
    *,
    project_id: int,
    workflow: dict[str, Any],
    sprint_candidates: dict[str, Any] | None = None,
    scope_extension_preconditions: ScopeExtensionPreconditions | None = None,
) -> dict[str, Any]:
    """Return next-cycle routing when triage records no follow-up impact."""
    planned_sprint_id = _planned_sprint_id(workflow)
    blocked_commands: list[dict[str, Any]] = []
    commands = [
        (
            "agileforge story pending",
            f"agileforge story pending --project-id {project_id}",
        ),
        (
            "agileforge sprint candidates",
            f"agileforge sprint candidates --project-id {project_id}",
        ),
    ]
    pending_story_requirements = _uncovered_story_requirements(workflow)
    candidate_count = _sprint_candidate_count(sprint_candidates)
    if (
        planned_sprint_id is None
        and candidate_count == 0
        and pending_story_requirements
    ):
        story_command = (
            f"agileforge story generate --project-id {project_id} "
            f"{_story_parent_requirement_flag(pending_story_requirements[0])}"
        )
        commands.insert(
            1,
            (
                "agileforge story generate",
                story_command,
            ),
        )
        next_valid_commands, blocked_future_commands = _installed_command_texts(
            commands
        )
        story_command_installed = command_is_available("agileforge story generate")
        status = "post_sprint_story_generation_required"
        return _sprint_complete_next_response(
            project_id=project_id,
            workflow=workflow,
            next_valid_commands=next_valid_commands,
            blocked_commands=[
                {
                    "command": "agileforge sprint generate",
                    "reason": "NO_SAVED_SPRINT_CANDIDATES",
                    "message": (
                        "Sprint generation is blocked until at least one saved Story "
                        "candidate is available."
                    ),
                    "candidate_count": candidate_count,
                    "pending_story_requirements": len(pending_story_requirements),
                }
            ],
            blocked_future_commands=blocked_future_commands,
            status=status,
            next_actions=[
                {
                    "command": story_command,
                    "status": status,
                    "reason": (
                        "Post-sprint triage recorded no follow-up impact, but no "
                        "Sprint candidates are available and Roadmap requirements "
                        "still need saved Stories."
                    ),
                    "runnable": story_command_installed,
                    "installed": story_command_installed,
                    "requires_cli_installation": not story_command_installed,
                }
            ],
        )
    if (
        planned_sprint_id is None
        and candidate_count == 0
        and scope_extension_preconditions is not None
    ):
        return _post_sprint_scope_extension_next(
            project_id=project_id,
            workflow=workflow,
            scope_extension_preconditions=scope_extension_preconditions,
            commands=commands,
        )
    if planned_sprint_id is None and candidate_count == 0:
        next_valid_commands, blocked_future_commands = _installed_command_texts(
            commands
        )
        candidates_command_installed = command_is_available(
            "agileforge sprint candidates"
        )
        status = "post_sprint_sprint_candidates_unavailable"
        return _sprint_complete_next_response(
            project_id=project_id,
            workflow=workflow,
            next_valid_commands=next_valid_commands,
            blocked_commands=[
                {
                    "command": "agileforge sprint generate",
                    "reason": "NO_REFINED_SPRINT_CANDIDATES",
                    "message": (
                        "Sprint generation is blocked because no refined Story "
                        "candidates are available."
                    ),
                    "candidate_count": candidate_count,
                    "excluded_counts": _sprint_candidate_excluded_counts(
                        sprint_candidates
                    ),
                }
            ],
            blocked_future_commands=blocked_future_commands,
            status=status,
            next_actions=[
                {
                    "command": (
                        f"agileforge sprint candidates --project-id {project_id}"
                    ),
                    "status": status,
                    "reason": (
                        "Post-sprint triage recorded no follow-up impact, but Sprint "
                        "generation has no refined candidates to plan."
                    ),
                    "runnable": candidates_command_installed,
                    "installed": candidates_command_installed,
                    "requires_cli_installation": not candidates_command_installed,
                }
            ],
        )
    if planned_sprint_id is None:
        commands.append(
            (
                "agileforge sprint generate",
                f"agileforge sprint generate --project-id {project_id}",
            )
        )
        primary_command_name = "agileforge sprint generate"
        primary_command = f"agileforge sprint generate --project-id {project_id}"
        status = "post_sprint_story_continuation_available"
    else:
        primary_command = _post_sprint_planned_sprint_start_command(
            project_id=project_id,
            planned_sprint_id=planned_sprint_id,
            expected_state="SPRINT_COMPLETE",
        )
        primary_command_name = "agileforge sprint start"
        status = "post_sprint_planned_sprint_start_blocked"
        blocked_commands.append(
            _post_sprint_planned_sprint_start_blocker(command=primary_command)
        )
    next_valid_commands, blocked_future_commands = _installed_command_texts(commands)
    primary_command_installed = command_is_available(primary_command_name)
    primary_command_runnable = (
        primary_command_installed
        and status != "post_sprint_planned_sprint_start_blocked"
    )
    next_actions = [
        {
            "command": primary_command,
            "status": status,
            "reason": (
                "POST_SPRINT_PLANNED_SPRINT_START_NOT_IMPLEMENTED"
                if planned_sprint_id is not None
                else "Post-sprint triage recorded no follow-up impact."
            ),
            "runnable": primary_command_runnable,
            "installed": primary_command_installed,
            "requires_cli_installation": not primary_command_installed,
        }
    ]
    return _sprint_complete_next_response(
        project_id=project_id,
        workflow=workflow,
        next_valid_commands=next_valid_commands,
        blocked_commands=blocked_commands,
        blocked_future_commands=blocked_future_commands,
        status=status,
        next_actions=next_actions,
    )


def _post_sprint_scope_extension_next(
    *,
    project_id: int,
    workflow: dict[str, Any],
    scope_extension_preconditions: ScopeExtensionPreconditions,
    commands: list[tuple[str, str]],
) -> dict[str, Any]:
    """Return exhausted-project routing through scope extension."""
    next_valid_commands, blocked_future_commands = _installed_command_texts(commands)
    validate_command = (
        f"agileforge scope extension validate --project-id {project_id} "
        "--spec-file <amended_spec_file>"
    )
    validate_command_installed = command_is_available(
        "agileforge scope extension validate"
    )
    if validate_command_installed:
        next_valid_commands.insert(0, validate_command)
    else:
        blocked_future_commands.insert(0, validate_command)
    if scope_extension_preconditions.available:
        status = scope_extension_preconditions.status
        return _sprint_complete_next_response(
            project_id=project_id,
            workflow=workflow,
            next_valid_commands=next_valid_commands,
            blocked_commands=[],
            blocked_future_commands=blocked_future_commands,
            status=status,
            next_actions=[
                {
                    "command": validate_command,
                    "status": status,
                    "reason": (
                        "The current execution scope is exhausted; validate an "
                        "amended spec before generating new work."
                    ),
                    "runnable": validate_command_installed,
                    "installed": validate_command_installed,
                    "requires_cli_installation": not validate_command_installed,
                }
            ],
        )

    reason = (
        scope_extension_preconditions.blocking_reason
        or "SCOPE_EXTENSION_NOT_AVAILABLE"
    )
    status = scope_extension_preconditions.status
    return _sprint_complete_next_response(
        project_id=project_id,
        workflow=workflow,
        next_valid_commands=next_valid_commands,
        blocked_commands=[
            {
                "command": "agileforge scope extension validate",
                "reason": reason,
                "message": (
                    "Scope extension is blocked until current project work is "
                    "exhausted."
                ),
            }
        ],
        blocked_future_commands=blocked_future_commands,
        status=status,
        next_actions=[
            {
                "command": validate_command,
                "status": status,
                "reason": reason,
                "runnable": False,
                "installed": validate_command_installed,
                "requires_cli_installation": not validate_command_installed,
            }
        ],
    )


def _planned_sprint_id(workflow: dict[str, Any]) -> int | None:
    """Return the planned Sprint id from workflow state, if present."""
    state = _envelope_data(workflow).get("state")
    state_data = state if isinstance(state, dict) else {}
    return _positive_int_or_none(state_data.get("planned_sprint_id"))


def _post_sprint_planned_sprint_start_command(
    *,
    project_id: int,
    planned_sprint_id: int,
    expected_state: str,
) -> str:
    """Return the guarded post-sprint planned Sprint start command."""
    return (
        f"agileforge sprint start --project-id {project_id} "
        f"--sprint-id {planned_sprint_id} "
        f"--expected-state {expected_state} "
        "--idempotency-key <idempotency_key>"
    )


def _post_sprint_planned_sprint_start_blocker(
    *,
    command: str,
) -> dict[str, str]:
    """Return the blocker for the not-yet-executable planned Sprint bridge."""
    return {
        "command": command,
        "reason": "POST_SPRINT_PLANNED_SPRINT_START_NOT_IMPLEMENTED",
        "message": (
            "Starting a planned Sprint from SPRINT_COMPLETE is not executable "
            "until a workflow bridge is implemented."
        ),
    }


def _post_sprint_triage_required_blocked_commands(
    *,
    project_id: int,
    workflow: dict[str, Any],
) -> list[dict[str, str]]:
    """Return Sprint bridge commands blocked until triage is recorded."""
    planned_sprint_id = _planned_sprint_id(workflow)
    if planned_sprint_id is None:
        return []
    return [
        {
            "command": _post_sprint_planned_sprint_start_command(
                project_id=project_id,
                planned_sprint_id=planned_sprint_id,
                expected_state="SPRINT_COMPLETE",
            ),
            "reason": "POST_SPRINT_TRIAGE_REQUIRED",
            "message": (
                "Record post-sprint triage before starting the planned Sprint."
            ),
        }
    ]


def _post_sprint_backlog_next(
    *,
    project_id: int,
    workflow: dict[str, Any],
) -> dict[str, Any]:
    """Return Backlog refinement bridge routing for backlog impact."""
    commands = _sprint_complete_backlog_refinement_commands(
        project_id=project_id,
        workflow=workflow,
    )
    next_valid_commands, blocked_future_commands = _installed_command_texts(commands)
    blocked_commands: list[dict[str, str]] = []
    status = "post_sprint_backlog_refinement_available"
    if not commands:
        blocked_commands = _backlog_source_unavailable_blocked_commands()
        status = "post_sprint_backlog_source_unavailable"
    return _sprint_complete_next_response(
        project_id=project_id,
        workflow=workflow,
        next_valid_commands=next_valid_commands,
        blocked_commands=blocked_commands,
        blocked_future_commands=blocked_future_commands,
        status=status,
    )


def _backlog_source_unavailable_blocked_commands() -> list[dict[str, str]]:
    """Return Backlog bridge blockers when no deterministic source exists."""
    reason = ErrorCode.BACKLOG_SOURCE_UNAVAILABLE.value
    message = (
        "Backlog impact was recorded, but no source attempt and fingerprint are "
        "available for a runnable refinement bridge."
    )
    return [
        {
            "command": "agileforge backlog refine-preview",
            "reason": reason,
            "message": message,
        },
        {
            "command": "agileforge backlog refine-record",
            "reason": reason,
            "message": message,
        },
        {
            "command": "agileforge backlog refine-import",
            "reason": reason,
            "message": message,
        },
    ]


def _post_sprint_refined_backlog_recorded_next(
    *,
    project_id: int,
    workflow: dict[str, Any],
) -> dict[str, Any]:
    """Return stale routing after a Backlog refinement was recorded."""
    attempt_id, artifact_fingerprint = _latest_backlog_attempt_guards(workflow)
    commands = [
        (
            "agileforge backlog history",
            f"agileforge backlog history --project-id {project_id}",
        )
    ]
    blocked_commands: list[dict[str, str]] = []
    if attempt_id is not None and artifact_fingerprint is not None:
        commands.extend(
            [
                (
                    "agileforge backlog save",
                    (
                        f"agileforge backlog save --project-id {project_id} "
                        f"--attempt-id {attempt_id} "
                        f"--expected-artifact-fingerprint {artifact_fingerprint} "
                        "--expected-state BACKLOG_REVIEW "
                        "--idempotency-key <idempotency_key>"
                    ),
                ),
                (
                    "agileforge backlog reset-active",
                    (
                        f"agileforge backlog reset-active --project-id {project_id} "
                        f"--attempt-id {attempt_id} "
                        f"--expected-artifact-fingerprint {artifact_fingerprint} "
                        "--expected-state BACKLOG_REVIEW "
                        "--reset-reason <reset_reason> "
                        "--archive-all-active-stories "
                        "--idempotency-key <idempotency_key>"
                    ),
                ),
            ]
        )
    else:
        blocked_commands.extend(_refined_backlog_source_unavailable_blocked_commands())
    next_valid_commands, blocked_future_commands = _installed_command_texts(commands)
    blocked_commands.extend(
        _post_sprint_stale_continuation_blockers(
            reason="DOWNSTREAM_BACKLOG_STALE_AFTER_REFINED_BACKLOG_RECORDED",
        )
    )
    return _sprint_complete_next_response(
        project_id=project_id,
        workflow=workflow,
        next_valid_commands=next_valid_commands,
        blocked_commands=blocked_commands,
        blocked_future_commands=blocked_future_commands,
        status="post_sprint_blocked_by_stale_backlog",
    )


def _refined_backlog_source_unavailable_blocked_commands() -> list[dict[str, str]]:
    """Return stale refined Backlog blockers when guard values are missing."""
    reason = ErrorCode.BACKLOG_SOURCE_UNAVAILABLE.value
    message = (
        "Refined Backlog stale routing cannot advertise guarded Backlog commands "
        "until the latest Backlog attempt id and fingerprint are available."
    )
    return [
        {
            "command": "agileforge backlog save",
            "reason": reason,
            "message": message,
        },
        {
            "command": "agileforge backlog reset-active",
            "reason": reason,
            "message": message,
        },
    ]


def _latest_backlog_attempt_guards(
    workflow: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Return latest Backlog attempt id and fingerprint guard values."""
    source_attempt = _latest_backlog_attempt(workflow)
    if source_attempt is None:
        return None, None
    return (
        _non_empty_string(source_attempt.get("attempt_id")),
        _non_empty_string(source_attempt.get("artifact_fingerprint")),
    )


def _post_sprint_stale_continuation_blockers(
    *,
    reason: str,
) -> list[dict[str, str]]:
    """Return Story and Sprint continuation blockers for stale Backlog guards."""
    message = (
        "Downstream Story and Sprint work remains blocked until stale Backlog "
        "reconciliation is resolved."
    )
    return [
        {
            "command": "agileforge story generate",
            "reason": reason,
            "message": message,
        },
        {
            "command": "agileforge sprint candidates",
            "reason": reason,
            "message": message,
        },
        {
            "command": "agileforge sprint generate",
            "reason": reason,
            "message": message,
        },
        {
            "command": "agileforge sprint start",
            "reason": reason,
            "message": message,
        },
    ]


def _post_sprint_story_next(
    *,
    project_id: int,
    workflow: dict[str, Any],
    triage: dict[str, Any],
) -> dict[str, Any]:
    """Return Story reconciliation routing for story-level impacts."""
    commands: list[tuple[str, str]] = [
        (
            "agileforge story pending",
            f"agileforge story pending --project-id {project_id}",
        )
    ]
    affected_requirements = triage.get("affected_requirements")
    if isinstance(affected_requirements, list) and affected_requirements:
        for requirement in affected_requirements:
            if not isinstance(requirement, str) or not requirement:
                continue
            commands.append(
                (
                    "agileforge story generate",
                    (
                        f"agileforge story generate --project-id {project_id} "
                        f"{_story_parent_requirement_flag(requirement)}"
                    ),
                )
            )
    else:
        commands.append(
            (
                "agileforge story generate",
                (
                    f"agileforge story generate --project-id {project_id} "
                    "--parent-requirement <parent_requirement>"
                ),
            )
        )
    next_valid_commands, blocked_future_commands = _installed_command_texts(commands)
    blocked_commands = [
        {
            "command": "agileforge sprint generate",
            "reason": "POST_SPRINT_STORY_IMPACT_NEEDS_RECONCILIATION",
            "message": (
                "Story-level post-sprint impact must be reconciled before "
                "generating another Sprint."
            ),
        }
    ]
    return _sprint_complete_next_response(
        project_id=project_id,
        workflow=workflow,
        next_valid_commands=next_valid_commands,
        blocked_commands=blocked_commands,
        blocked_future_commands=blocked_future_commands,
        status="post_sprint_story_impact_needs_reconciliation",
    )


def _post_sprint_roadmap_next(
    *,
    project_id: int,
    workflow: dict[str, Any],
) -> dict[str, Any]:
    """Return Roadmap reconciliation routing for roadmap-level impacts."""
    commands = [
        (
            "agileforge roadmap history",
            f"agileforge roadmap history --project-id {project_id}",
        ),
        (
            "agileforge roadmap generate",
            (
                f"agileforge roadmap generate --project-id {project_id} "
                "--input-file <feedback_file>"
            ),
        ),
    ]
    next_valid_commands, blocked_future_commands = _installed_command_texts(commands)
    return _sprint_complete_next_response(
        project_id=project_id,
        workflow=workflow,
        next_valid_commands=next_valid_commands,
        blocked_commands=_post_sprint_roadmap_blocked_commands(),
        blocked_future_commands=blocked_future_commands,
        status="post_sprint_roadmap_reconciliation_available",
    )


def _post_sprint_roadmap_blocked_commands() -> list[dict[str, str]]:
    """Return downstream continuation blockers for roadmap-impact triage."""
    reason = "POST_SPRINT_ROADMAP_IMPACT_NEEDS_RECONCILIATION"
    message = (
        "Roadmap-level post-sprint impact must be reconciled before continuing "
        "Story or Sprint planning."
    )
    return [
        {
            "command": "agileforge story generate",
            "reason": reason,
            "message": message,
        },
        {
            "command": "agileforge sprint candidates",
            "reason": reason,
            "message": message,
        },
        {
            "command": "agileforge sprint generate",
            "reason": reason,
            "message": message,
        },
    ]


def _post_sprint_task_next(
    *,
    project_id: int,
    workflow: dict[str, Any],
    triage: dict[str, Any],
) -> dict[str, Any]:
    """Return task-impact routing for completed Sprint review context."""
    state = _envelope_data(workflow).get("state")
    state_data = state if isinstance(state, dict) else {}
    latest_completed_sprint_id = state_data.get("latest_completed_sprint_id")
    sprint_status_command = f"agileforge sprint status --project-id {project_id}"
    if isinstance(latest_completed_sprint_id, int) and latest_completed_sprint_id > 0:
        sprint_status_command = (
            f"{sprint_status_command} --sprint-id {latest_completed_sprint_id}"
        )
    commands = [
        (
            "agileforge sprint review",
            f"agileforge sprint review --project-id {project_id}",
        ),
        (
            "agileforge sprint status",
            sprint_status_command,
        ),
        (
            "agileforge sprint history",
            f"agileforge sprint history --project-id {project_id}",
        ),
    ]
    next_valid_commands, blocked_future_commands = _installed_command_texts(commands)
    blocked_commands = [
        {
            "command": "agileforge sprint task carryover",
            "reason": "TASK_CARRYOVER_NOT_IMPLEMENTED",
            "message": (
                "Task carryover is not implemented yet; review the completed "
                "Sprint before planning follow-up work."
            ),
            "affected_task_ids": triage.get("affected_task_ids", []),
        }
    ]
    return _sprint_complete_next_response(
        project_id=project_id,
        workflow=workflow,
        next_valid_commands=next_valid_commands,
        blocked_commands=blocked_commands,
        blocked_future_commands=blocked_future_commands,
        status="post_sprint_task_impact_needs_carryover",
    )


def _post_sprint_multiple_next(
    *,
    project_id: int,
    workflow: dict[str, Any],
    triage: dict[str, Any],
) -> dict[str, Any]:
    """Return guarded correction routing for multi-layer impacts."""
    triage_fingerprint = str(
        triage.get("triage_fingerprint") or "<triage_fingerprint>"
    ).strip()
    commands = [
        (
            "agileforge sprint review",
            f"agileforge sprint review --project-id {project_id}",
        ),
        (
            "agileforge sprint triage",
            (
                f"agileforge sprint triage --project-id {project_id} "
                "--expected-state SPRINT_COMPLETE --replace-existing "
                f"--expected-triage-fingerprint {triage_fingerprint}"
            ),
        ),
    ]
    next_valid_commands, blocked_future_commands = _installed_command_texts(commands)
    return _sprint_complete_next_response(
        project_id=project_id,
        workflow=workflow,
        next_valid_commands=next_valid_commands,
        blocked_commands=_post_sprint_multiple_blocked_commands(triage=triage),
        blocked_future_commands=blocked_future_commands,
        status="post_sprint_multiple_impacts_need_decision",
    )


def _installed_command_texts(
    commands: list[tuple[str, str]],
) -> tuple[list[str], list[str]]:
    """Split command texts by installed command availability."""
    next_valid_commands: list[str] = []
    blocked_future_commands: list[str] = []
    for command_name, command_text in commands:
        if command_is_available(command_name):
            next_valid_commands.append(command_text)
        else:
            blocked_future_commands.append(command_text)
    return next_valid_commands, blocked_future_commands


def _post_sprint_multiple_blocked_commands(
    *,
    triage: dict[str, Any],
) -> list[dict[str, str]]:
    """Return blocked layer bridge commands for multi-impact triage."""
    layer_commands = {
        "task": "agileforge sprint task carryover",
        "story": "agileforge story generate",
        "roadmap": "agileforge roadmap generate",
        "backlog": "agileforge backlog refine",
    }
    layers = triage.get("affected_layers")
    if isinstance(layers, list):
        affected_layers = {layer for layer in layers if isinstance(layer, str)}
    else:
        affected_layers = set()
    blocked_commands: list[dict[str, str]] = []
    for layer in ("story", "task", "roadmap", "backlog"):
        if layer not in affected_layers:
            continue
        command = layer_commands.get(layer)
        if command is None:
            continue
        blocked_commands.append(
            {
                "command": command,
                "reason": "POST_SPRINT_MULTIPLE_IMPACTS_NEED_DECISION",
                "message": (
                    "Resolve the post-sprint triage decision before routing "
                    f"{layer} follow-up."
                ),
            }
        )
    return blocked_commands


def _sprint_complete_next_response(  # noqa: PLR0913
    *,
    project_id: int,
    workflow: dict[str, Any],
    next_valid_commands: list[str],
    blocked_commands: list[Any],
    blocked_future_commands: list[Any],
    status: str,
    next_actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a workflow-next response for completed-Sprint routing."""
    data: dict[str, Any] = {
        "project_id": project_id,
        "next_valid_commands": next_valid_commands,
        "blocked_commands": blocked_commands,
        "blocked_future_commands": blocked_future_commands,
        "status": status,
    }
    if next_actions is not None:
        data["next_actions"] = next_actions
    fingerprint_input: dict[str, Any] = {
        "command": WORKFLOW_NEXT_COMMAND,
        "project_id": project_id,
        "workflow": _fingerprint_input(_envelope_data(workflow)),
        "installed_command_names": sorted(installed_command_names()),
        "next_valid_commands": data["next_valid_commands"],
        "blocked_commands": data["blocked_commands"],
        "blocked_future_commands": data["blocked_future_commands"],
        "status": data["status"],
    }
    if next_actions is not None:
        fingerprint_input["next_actions"] = data["next_actions"]
    data["source_fingerprint"] = canonical_hash(fingerprint_input)
    return {
        "ok": True,
        "data": data,
        "warnings": _section_warnings(
            section="workflow",
            source="workflow_state",
            envelope=workflow,
        ),
        "errors": [],
    }


def _post_sprint_triage_required_commands(
    *,
    project_id: int,
) -> list[tuple[str, str]]:
    """Return Sprint commands when a completed Sprint needs triage."""
    return [
        (
            "agileforge sprint review",
            f"agileforge sprint review --project-id {project_id}",
        ),
        (
            "agileforge sprint triage",
            _post_sprint_triage_required_triage_command(project_id=project_id),
        ),
        (
            "agileforge sprint history",
            f"agileforge sprint history --project-id {project_id}",
        ),
    ]


def _post_sprint_triage_required_triage_command(*, project_id: int) -> str:
    """Return the guarded Sprint triage command template."""
    return (
        f"agileforge sprint triage --project-id {project_id} "
        "--expected-state SPRINT_COMPLETE --impact <impact> "
        "--learning-summary <summary> --decision-reason <reason> "
        "--idempotency-key <idempotency_key>"
    )


def _post_sprint_triage_required_next_action(
    *,
    command: str,
    installed: bool,
) -> dict[str, Any]:
    """Return the structured action for required post-sprint triage."""
    return {
        "command": command,
        "status": "post_sprint_triage_required",
        "reason": "A completed Sprint needs learning triage before next-cycle routing.",
        "runnable": installed,
        "installed": installed,
        "requires_cli_installation": not installed,
        "requires": [
            "expected_state",
            "impact",
            "learning_summary",
            "decision_reason",
            "idempotency_key",
        ],
    }


def _sprint_complete_backlog_refinement_commands(
    *,
    project_id: int,
    workflow: dict[str, Any],
) -> list[tuple[str, str]]:
    """Return Backlog refinement command templates after Sprint close."""
    source_attempt = _latest_backlog_attempt(workflow)
    if source_attempt is None:
        return []

    source_attempt_id = str(source_attempt.get("attempt_id") or "").strip()
    if not source_attempt_id:
        return []
    source_fingerprint = str(source_attempt.get("artifact_fingerprint") or "").strip()
    if not source_fingerprint:
        return []
    return [
        (
            "agileforge backlog refine-preview",
            (
                f"agileforge backlog refine-preview --project-id {project_id} "
                f"--source-attempt-id {source_attempt_id} "
                "--operations-file <operations_file>"
            ),
        ),
        (
            "agileforge backlog refine-record",
            (
                f"agileforge backlog refine-record --project-id {project_id} "
                f"--source-attempt-id {source_attempt_id} "
                "--operations-file <operations_file> "
                f"--expected-source-fingerprint {source_fingerprint} "
                "--expected-state SPRINT_COMPLETE "
                "--idempotency-key <idempotency_key>"
            ),
        ),
        (
            "agileforge backlog refine-import",
            (
                f"agileforge backlog refine-import --project-id {project_id} "
                "--source-artifact <source_artifact> "
                "--edited-file <edited_file> "
                f"--expected-source-fingerprint {source_fingerprint} "
                "--idempotency-key <idempotency_key>"
            ),
        ),
    ]


def _latest_backlog_attempt(workflow: dict[str, Any]) -> dict[str, Any] | None:
    """Return the latest Backlog attempt from workflow state, if present."""
    state = _envelope_data(workflow).get("state")
    state_data = state if isinstance(state, dict) else {}
    attempts = state_data.get("backlog_attempts")
    if not isinstance(attempts, list):
        return None
    for attempt in reversed(attempts):
        if isinstance(attempt, dict):
            return cast("dict[str, Any]", attempt)
    return None


def _sprint_save_blocker(workflow: dict[str, Any]) -> dict[str, Any] | None:
    """Return a blocker when the current Sprint draft is not save-ready."""
    state = _envelope_data(workflow).get("state")
    state_data = state if isinstance(state, dict) else {}
    assessment = state_data.get("sprint_plan_assessment")
    attempts = state_data.get("sprint_attempts")
    if not isinstance(assessment, dict) or assessment.get("is_complete") is not True:
        return {"reason_code": "SPRINT_DRAFT_INCOMPLETE", "details": {}}
    if not isinstance(attempts, list) or not attempts:
        return {"reason_code": "SPRINT_DRAFT_NO_ATTEMPT", "details": {}}

    latest = attempts[-1]
    if not isinstance(latest, dict):
        return {
            "reason_code": "SPRINT_DRAFT_NOT_LATEST_COMPLETE",
            "details": {
                "latest_attempt_id": None,
                "draft_attempt_id": assessment.get("attempt_id"),
            },
        }
    if (
        latest.get("is_complete") is not True
        or latest.get("attempt_id") != assessment.get("attempt_id")
        or latest.get("artifact_fingerprint") != assessment.get("artifact_fingerprint")
    ):
        return {
            "reason_code": "SPRINT_DRAFT_NOT_LATEST_COMPLETE",
            "details": {
                "latest_attempt_id": latest.get("attempt_id"),
                "draft_attempt_id": assessment.get("attempt_id"),
            },
        }
    return None


def _sprint_command_candidates(
    *,
    project_id: int,
    fsm_state: str,
) -> list[tuple[str, str]]:
    """Return Sprint command candidates for the current Sprint state."""
    if fsm_state == "SPRINT_SETUP":
        return [
            *_story_dependency_command_candidates(
                project_id=project_id,
                expected_state="SPRINT_SETUP",
            ),
            (
                "agileforge sprint candidates",
                f"agileforge sprint candidates --project-id {project_id}",
            ),
            (
                "agileforge sprint generate",
                f"agileforge sprint generate --project-id {project_id}",
            ),
        ]
    if fsm_state == "SPRINT_DRAFT":
        return [
            (
                "agileforge sprint history",
                f"agileforge sprint history --project-id {project_id}",
            ),
            *_story_dependency_command_candidates(
                project_id=project_id,
                expected_state="SPRINT_DRAFT",
            ),
            (
                "agileforge sprint save",
                (
                    f"agileforge sprint save --project-id {project_id} "
                    "--team-name <team_name> "
                    "--attempt-id <attempt_id> "
                    "--expected-artifact-fingerprint <artifact_fingerprint> "
                    "--expected-state SPRINT_DRAFT "
                    "--idempotency-key <idempotency_key>"
                ),
            ),
            (
                "agileforge sprint generate",
                (
                    f"agileforge sprint generate --project-id {project_id} "
                    "--input <feedback>"
                ),
            ),
        ]
    if fsm_state == "SPRINT_PERSISTENCE":
        return [
            (
                "agileforge sprint start",
                (
                    f"agileforge sprint start --project-id {project_id} "
                    "--expected-state SPRINT_PERSISTENCE "
                    "--idempotency-key <idempotency_key>"
                ),
            ),
            (
                "agileforge sprint history",
                f"agileforge sprint history --project-id {project_id}",
            ),
        ]
    if fsm_state == "SPRINT_VIEW":
        from services.agent_workbench.sprint_phase import (  # noqa: PLC0415
            sprint_task_update_command_text,
        )

        return [
            (
                "agileforge sprint task next",
                f"agileforge sprint task next --project-id {project_id}",
            ),
            (
                "agileforge sprint status",
                f"agileforge sprint status --project-id {project_id}",
            ),
            (
                "agileforge sprint tasks",
                f"agileforge sprint tasks --project-id {project_id}",
            ),
            (
                "agileforge sprint task show",
                (
                    f"agileforge sprint task show --project-id {project_id} "
                    "--task-id <task_id>"
                ),
            ),
            (
                "agileforge sprint task update",
                sprint_task_update_command_text(
                    project_id=project_id,
                    task_id="<task_id>",
                    status="Done",
                    expected_status="<expected_status>",
                    expected_task_fingerprint="<task_fingerprint>",
                    idempotency_key="<idempotency_key>",
                    include_done_evidence=True,
                    artifact_targets=None,
                ),
            ),
            (
                "agileforge sprint story readiness",
                (
                    f"agileforge sprint story readiness --project-id {project_id} "
                    "--story-id <story_id>"
                ),
            ),
            (
                "agileforge sprint story close",
                (
                    f"agileforge sprint story close --project-id {project_id} "
                    "--story-id <story_id> --expected-status <expected_status> "
                    "--expected-story-fingerprint <story_fingerprint> "
                    "--idempotency-key <idempotency_key> "
                    "--resolution Completed --completion-notes <notes>"
                ),
            ),
            (
                "agileforge sprint close-readiness",
                f"agileforge sprint close-readiness --project-id {project_id}",
            ),
            (
                "agileforge sprint close",
                (
                    f"agileforge sprint close --project-id {project_id} "
                    "--expected-state SPRINT_VIEW "
                    "--expected-status Active "
                    "--expected-sprint-fingerprint <sprint_fingerprint> "
                    "--idempotency-key <idempotency_key> "
                    "--completion-notes <notes>"
                ),
            ),
            *_story_dependency_command_candidates(
                project_id=project_id,
                expected_state="SPRINT_VIEW",
            ),
            (
                "agileforge sprint history",
                f"agileforge sprint history --project-id {project_id}",
            ),
        ]
    return [
        (
            "agileforge sprint history",
            f"agileforge sprint history --project-id {project_id}",
        )
    ]


def _story_dependency_command_candidates(
    *,
    project_id: int,
    expected_state: str,
) -> list[tuple[str, str]]:
    """Return Story dependency review command candidates for current state."""
    return [
        (
            "agileforge story dependencies inspect",
            f"agileforge story dependencies inspect --project-id {project_id}",
        ),
        (
            "agileforge story dependencies propose",
            (
                f"agileforge story dependencies propose --project-id {project_id} "
                f"--expected-state {expected_state} "
                "--idempotency-key <idempotency_key>"
            ),
        ),
        (
            "agileforge story dependencies apply",
            (
                f"agileforge story dependencies apply --project-id {project_id} "
                "--attempt-id <attempt_id> "
                "--expected-artifact-fingerprint <artifact_fingerprint> "
                f"--expected-state {expected_state} "
                "--idempotency-key <idempotency_key>"
            ),
        ),
    ]


def _uninstalled_phase_workflow_next(
    *,
    project_id: int,
    workflow: dict[str, Any],
) -> dict[str, Any] | None:
    """Return explicit blocked commands for known phases without CLI runners."""
    fsm_state = _fsm_state_from_envelope(workflow)
    command_candidates = _uninstalled_phase_command_candidates(
        project_id=project_id,
        fsm_state=fsm_state,
    )
    if command_candidates is None:
        return None

    next_valid_commands: list[str] = []
    blocked_future_commands: list[dict[str, Any]] = []
    for command_name, command_text, reason in command_candidates:
        if command_is_available(command_name):
            next_valid_commands.append(command_text)
        else:
            blocked_future_commands.append(
                {
                    "command": command_text,
                    "installed": False,
                    "reason": reason,
                }
            )

    status = (
        "next_phase_available"
        if next_valid_commands
        else "blocked_by_uninstalled_next_phase"
    )
    data: dict[str, Any] = {
        "project_id": project_id,
        "next_valid_commands": next_valid_commands,
        "blocked_commands": [],
        "blocked_future_commands": blocked_future_commands,
        "status": status,
    }
    data["source_fingerprint"] = canonical_hash(
        {
            "command": WORKFLOW_NEXT_COMMAND,
            "project_id": project_id,
            "workflow": _fingerprint_input(_envelope_data(workflow)),
            "installed_command_names": sorted(installed_command_names()),
            "next_valid_commands": data["next_valid_commands"],
            "blocked_commands": data["blocked_commands"],
            "blocked_future_commands": data["blocked_future_commands"],
            "status": data["status"],
        }
    )
    return {
        "ok": True,
        "data": data,
        "warnings": _section_warnings(
            section="workflow",
            source="workflow_state",
            envelope=workflow,
        ),
        "errors": [],
    }


def _uninstalled_phase_command_candidates(
    *,
    project_id: int,
    fsm_state: str | None,
) -> list[tuple[str, str, str]] | None:
    """Return command templates for workflow phases not yet exposed in the CLI."""
    phase_commands: dict[str, list[tuple[str, str, str]]] = {}
    candidates = phase_commands.get(str(fsm_state or ""))
    if candidates is None:
        return None
    return [
        (command_name, command_text.format(project_id=project_id), reason)
        for command_name, command_text, reason in candidates
    ]


def _authority_review_summary(review: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a compact authority review summary from a review envelope."""
    if review is None or review.get("ok") is not True:
        return None
    data = _envelope_data(review)
    summary = data.get("review_summary")
    return dict(summary) if isinstance(summary, dict) else None


def _as_list(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(cast("list[object]", value))
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _rejected_spec_version_id(authority: dict[str, Any] | None) -> int | None:
    """Return the spec version to regenerate after authority rejection."""
    if authority is None:
        return None
    authority_data = _envelope_data(authority)
    for key in (
        "rejected_spec_version_id",
        "pending_compiled_spec_version_id",
        "latest_spec_version_id",
    ):
        value = authority_data.get(key)
        if isinstance(value, int):
            return value
    return None


def _authority_curate_next_action_from_state(
    *,
    project_id: int,
    workflow: dict[str, Any],
    authority_data: dict[str, Any],
) -> dict[str, Any] | None:
    """Return next action for structured authority curation when guards exist."""
    spec_version_id = _workflow_setup_spec_version_id(workflow)
    if spec_version_id is None:
        spec_version_id = _spec_version_id_from_authority_data(authority_data)
    source_authority_id = _source_authority_id(authority_data)
    source_authority_fingerprint = _source_authority_fingerprint(authority_data)
    feedback_attempt_id = authority_data.get("latest_feedback_attempt_id")
    if (
        spec_version_id is None
        or source_authority_id is None
        or source_authority_fingerprint is None
        or not isinstance(feedback_attempt_id, str)
    ):
        return None
    installed = command_is_available(AUTHORITY_CURATE_COMMAND)
    command = (
        f"{AUTHORITY_CURATE_COMMAND} --project-id {project_id} "
        f"--spec-version-id {spec_version_id} "
        f"--source-authority-id {source_authority_id} "
        "--expected-source-authority-fingerprint "
        f"{source_authority_fingerprint} "
        f"--feedback-attempt-id {feedback_attempt_id} "
        "--idempotency-key <idempotency_key>"
    )
    return {
        "command": command,
        "installed": installed,
        "requires_cli_installation": not installed,
        "reason": "Structured authority feedback exists for the rejected authority.",
        "requires": ["idempotency_key"],
    }


def _authority_curate_recovery_next_action_from_state(
    *,
    project_id: int,
    workflow: dict[str, Any],
    authority_data: dict[str, Any],
) -> dict[str, Any] | None:
    """Return authority curation recovery action when candidate guards exist."""
    recovery_mutation_event_id = authority_data.get(
        "latest_curation_mutation_event_id"
    )
    if not isinstance(recovery_mutation_event_id, int):
        workflow_state = _envelope_data(workflow).get("state")
        state_data = workflow_state if isinstance(workflow_state, dict) else {}
        recovery_mutation_event_id = state_data.get(
            "setup_curation_mutation_event_id"
        )
    recovery_candidate_authority_id = authority_data.get(
        "latest_curation_candidate_authority_id"
    )
    recovery_candidate_fingerprint = authority_data.get(
        "latest_curation_candidate_authority_fingerprint"
    )
    if (
        isinstance(recovery_mutation_event_id, int)
        and isinstance(recovery_candidate_authority_id, int)
        and isinstance(recovery_candidate_fingerprint, str)
        and recovery_candidate_fingerprint
    ):
        installed = command_is_available(AUTHORITY_CURATE_COMMAND)
        command = (
            f"{AUTHORITY_CURATE_COMMAND} --project-id {project_id} "
            f"--recovery-mutation-event-id {recovery_mutation_event_id} "
            f"--expected-candidate-authority-id {recovery_candidate_authority_id} "
            "--expected-candidate-authority-fingerprint "
            f"{recovery_candidate_fingerprint} "
            "--idempotency-key <idempotency_key>"
        )
        return {
            "command": command,
            "installed": installed,
            "requires_cli_installation": not installed,
            "reason": (
                "Authority curation published a candidate and needs explicit "
                "recovery to restore pending review."
            ),
            "requires": ["idempotency_key"],
        }
    return None


def _workflow_setup_spec_version_id(workflow: dict[str, Any]) -> int | None:
    """Return setup_spec_version_id from workflow state when concrete."""
    state = _envelope_data(workflow).get("state")
    workflow_state = state if isinstance(state, dict) else {}
    value = workflow_state.get("setup_spec_version_id")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _spec_version_id_from_authority_data(authority_data: dict[str, Any]) -> int | None:
    """Return a concrete rejected or pending authority spec version."""
    for key in (
        "rejected_spec_version_id",
        "pending_compiled_spec_version_id",
        "latest_spec_version_id",
    ):
        value = authority_data.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _source_authority_id(authority_data: dict[str, Any]) -> int | None:
    """Return the source authority id for curation."""
    for key in ("rejected_pending_authority_id", "pending_authority_id"):
        value = authority_data.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _source_authority_fingerprint(authority_data: dict[str, Any]) -> str | None:
    """Return the source authority fingerprint for curation."""
    for key in (
        "pending_authority_fingerprint",
        "latest_feedback_source_authority_fingerprint",
        "source_authority_fingerprint",
    ):
        value = authority_data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _authority_regenerate_command(
    *,
    project_id: int,
    spec_version_id: int,
) -> str:
    """Return the installed authority regeneration command for a rejected spec."""
    return (
        f"{AUTHORITY_REGENERATE_COMMAND} --project-id {project_id} "
        f"--spec-version-id {spec_version_id} "
        "--idempotency-key <idempotency_key>"
    )


def _authority_regenerate_next_action(command: str) -> dict[str, Any]:
    """Return the workflow-next action for authority regeneration."""
    return {
        "command": command,
        "installed": True,
        "requires_cli_installation": False,
        "reason": (
            "Regenerate compiled authority after rejection, then review the "
            "regenerated pending authority before acceptance."
        ),
        "requires": ["idempotency_key"],
    }


def _data_envelope(data: dict[str, Any]) -> dict[str, Any]:
    """Wrap payload data in the application envelope shape."""
    return {
        "ok": True,
        "data": data,
        "warnings": [],
        "errors": [],
    }


def _mutation_ledger_repository() -> (
    tuple[MutationLedgerRepository, None]
    | tuple[
        None,
        dict[str, Any],
    ]
):
    """Return a mutation ledger repo or a schema-not-ready envelope."""
    engine = get_engine()
    readiness = check_schema_readiness(engine, MUTATION_LEDGER_REQUIREMENTS)
    if readiness.ok:
        return MutationLedgerRepository(engine=engine), None

    error = workbench_error(
        ErrorCode.SCHEMA_NOT_READY,
        details={"missing": readiness.missing},
        remediation=["agileforge schema check"],
    )
    return None, {
        "ok": False,
        "data": None,
        "warnings": [],
        "errors": [error.to_dict()],
    }


def _fingerprint_input(data: dict[str, Any]) -> object:
    """Return the stable child fingerprint when available, else child data."""
    return data.get("source_fingerprint") or data.get("authority_fingerprint") or data


def _section_warnings(
    *,
    section: str,
    source: str,
    envelope: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return child warnings with facade section labels."""
    warnings = envelope.get("warnings", [])
    if not isinstance(warnings, list):
        return []

    labeled: list[dict[str, Any]] = []
    for warning in warnings:
        if not isinstance(warning, dict):
            continue
        labeled.append({"section": section, "source": source, **warning})
    return labeled
