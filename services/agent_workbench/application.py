"""Agent workbench application facade."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, Protocol, cast

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

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
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.agent_workbench.fingerprints import canonical_hash
from services.agent_workbench.mutation_ledger import MutationLedgerRepository
from services.agent_workbench.post_sprint_triage import (
    current_triage_for_latest_sprint,
    post_sprint_triage_required,
)
from services.agent_workbench.project_setup import (
    ProjectCreateRequest,
    ProjectSetupMutationRunner,
    ProjectSetupRetryRequest,
)
from services.agent_workbench.schema_readiness import (
    MUTATION_LEDGER_REQUIREMENTS,
    check_schema_readiness,
)

STATUS_COMMAND: Final[str] = "agileforge status"
WORKFLOW_NEXT_COMMAND: Final[str] = "agileforge workflow next"
AUTHORITY_REGENERATE_COMMAND: Final[str] = "agileforge authority regenerate"


def get_engine() -> Engine:
    """Return the default engine without importing the DB layer at import time."""
    from models.db import get_engine as _get_engine  # noqa: PLC0415

    return _get_engine()


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

    def generate(
        self,
        *,
        project_id: int,
        parent_requirement: str,
        user_input: str | None = None,
        force_feedback: bool = False,
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

    def repair_readiness(
        self,
        *,
        project_id: int,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Backfill Story planning metadata before Sprint work starts."""
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
        team_velocity_assumption: str = "Medium",
        sprint_duration_days: int = 14,
        max_story_points: int | None = None,
        include_task_decomposition: bool = True,
    ) -> dict[str, Any]:
        """Generate or refine a Sprint draft."""
        ...

    def history(self, *, project_id: int) -> dict[str, Any]:
        """Return Sprint attempt history."""
        ...

    def save(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        team_name: str,
        sprint_start_date: str,
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
        vision_runner: _VisionPhaseRunner | None = None,
        backlog_runner: _BacklogPhaseRunner | None = None,
        roadmap_runner: _RoadmapPhaseRunner | None = None,
        story_runner: _StoryPhaseRunner | None = None,
        sprint_runner: _SprintPhaseRunner | None = None,
        evidence_runner: _EvidenceCollectionRunner | None = None,
        as_built_runner: _AsBuiltAssessmentRunner | None = None,
    ) -> None:
        """Initialize the facade with explicit projection dependencies."""
        self._read_projection = read_projection
        self._authority_projection = authority_projection
        self._project_setup_runner = project_setup_runner
        self._authority_review = authority_review
        self._authority_decision_runner = authority_decision_runner
        self._authority_regenerate_runner = authority_regenerate_runner
        self._vision_runner = vision_runner
        self._backlog_runner = backlog_runner
        self._roadmap_runner = roadmap_runner
        self._story_runner = story_runner
        self._sprint_runner = sprint_runner
        self._evidence_runner = evidence_runner
        self._as_built_runner = as_built_runner
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
            "authority_pending_review",
            "authority_rejected",
            "failed",
        }:
            authority = (
                self.authority_status(project_id=project_id)
                if setup_status != "failed"
                else None
            )
            effective_setup_status = (
                "authority_pending_review"
                if _authority_has_pending_review(authority)
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

        phase_next_handlers = (
            _vision_workflow_next,
            _backlog_workflow_next,
            _roadmap_workflow_next,
            _story_workflow_next,
            _sprint_workflow_next,
            _uninstalled_phase_workflow_next,
        )
        for phase_next_handler in phase_next_handlers:
            phase_next = phase_next_handler(project_id=project_id, workflow=workflow)
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
        spec_file: str,
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
        spec_file: str,
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

    def authority_regenerate(
        self,
        *,
        project_id: int,
        spec_version_id: int,
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
                idempotency_key=idempotency_key,
                changed_by=changed_by,
                dry_run=dry_run,
            )
        )

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
    ) -> dict[str, Any]:
        """Collect or import implementation evidence for backlog generation."""
        return self._get_evidence_runner().collect(
            project_id=project_id,
            repo_path=repo_path,
            from_file=from_file,
            idempotency_key=idempotency_key,
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

    def story_generate(
        self,
        *,
        project_id: int,
        parent_requirement: str,
        user_input: str | None = None,
        force_feedback: bool = False,
    ) -> dict[str, Any]:
        """Generate or refine a Story draft."""
        return self._get_story_runner().generate(
            project_id=project_id,
            parent_requirement=parent_requirement,
            user_input=user_input,
            force_feedback=force_feedback,
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
        team_velocity_assumption: str = "Medium",
        sprint_duration_days: int = 14,
        max_story_points: int | None = None,
        include_task_decomposition: bool = True,
    ) -> dict[str, Any]:
        """Generate or refine a Sprint draft."""
        return self._get_sprint_runner().generate(
            project_id=project_id,
            user_input=user_input,
            selected_story_ids=selected_story_ids,
            team_velocity_assumption=team_velocity_assumption,
            sprint_duration_days=sprint_duration_days,
            max_story_points=max_story_points,
            include_task_decomposition=include_task_decomposition,
        )

    def sprint_history(self, *, project_id: int) -> dict[str, Any]:
        """Return Sprint attempt history."""
        return self._get_sprint_runner().history(project_id=project_id)

    def sprint_save(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        team_name: str,
        sprint_start_date: str,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Persist the current complete Sprint draft."""
        return self._get_sprint_runner().save(
            project_id=project_id,
            team_name=team_name,
            sprint_start_date=sprint_start_date,
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
        "next_valid_commands": [],
        "blocked_commands": [],
        "blocked_future_commands": [],
    }
    review_summary = _authority_review_summary(review)
    if setup_status == "authority_pending_review":
        data["next_actions"] = [
            {
                "command": (
                    f"agileforge authority review --project-id {project_id} --open"
                ),
                "installed": True,
                "requires_cli_installation": False,
                "reason": "Review pending authority before accepting or rejecting it.",
            }
        ]
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
    elif setup_status == "authority_rejected":
        spec_version_id = _rejected_spec_version_id(authority)
        if spec_version_id is not None:
            command = _authority_regenerate_command(
                project_id=project_id,
                spec_version_id=spec_version_id,
            )
            data["next_valid_commands"] = [command]
            data["next_actions"] = [_authority_regenerate_next_action(command)]
            data["manual_remediation"] = []
        else:
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
    elif setup_status == "failed":
        data["next_valid_commands"] = [
            (
                f"agileforge project setup retry --project-id {project_id} "
                "--spec-file <spec-file> --expected-state SETUP_REQUIRED "
                "--expected-context-fingerprint <expected_context_fingerprint>"
            )
        ]

    data["source_fingerprint"] = canonical_hash(
        {
            "command": WORKFLOW_NEXT_COMMAND,
            "project_id": project_id,
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
                "--input <feedback>"
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
        return [pending_command, generate_command]
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
            _story_requirement_is_covered(
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
        if _story_requirement_is_covered(
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
        _story_requirement_is_covered(
            state_data,
            parent_requirement=requirement,
        )
        for requirement in requirements
    )


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


def _sprint_workflow_next(
    *,
    project_id: int,
    workflow: dict[str, Any],
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
    for command_name, command_text in _sprint_command_candidates(
        project_id=project_id,
        fsm_state=fsm_state,
    ):
        if command_name == "agileforge sprint save" and save_blocker is not None:
            blocked_commands.append(
                {
                    "command": command_name,
                    **save_blocker,
                }
            )
            continue
        if command_is_available(command_name):
            next_valid_commands.append(command_text)
        else:
            blocked_future_commands.append(command_text)

    data: dict[str, Any] = {
        "project_id": project_id,
        "next_valid_commands": next_valid_commands,
        "blocked_commands": blocked_commands,
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


def _sprint_complete_workflow_next(
    *,
    project_id: int,
    workflow: dict[str, Any],
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
) -> dict[str, Any] | None:
    """Return routed workflow-next data for recorded triage impacts."""
    if triage is None:
        return None
    triage_impact = triage.get("impact")
    if triage_impact == "none":
        impact_next = _post_sprint_none_next(
            project_id=project_id,
            workflow=workflow,
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
) -> dict[str, Any]:
    """Return next-cycle routing when triage records no follow-up impact."""
    planned_sprint_id = _planned_sprint_id(workflow)
    blocked_commands: list[dict[str, str]] = []
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
            f"agileforge roadmap generate --project-id {project_id} --input <feedback>",
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
                    "--sprint-start-date <YYYY-MM-DD> "
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
                (
                    f"agileforge sprint task update --project-id {project_id} "
                    "--task-id <task_id> --status <status> "
                    "--expected-status <expected_status> "
                    "--expected-task-fingerprint <task_fingerprint> "
                    "--idempotency-key <idempotency_key>"
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
