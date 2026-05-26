"""Agent workbench application facade."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, Protocol, cast

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

from models.db import get_engine
from services.agent_workbench.authority_decision import (
    AuthorityAcceptRequest,
    AuthorityDecisionRunner,
    AuthorityRejectRequest,
)
from services.agent_workbench.authority_projection import AuthorityProjectionService
from services.agent_workbench.authority_review import AuthorityReviewService
from services.agent_workbench.command_registry import (
    command_is_available,
    installed_command_names,
)
from services.agent_workbench.command_schema import (
    capabilities_payload,
    command_schema_payload,
)
from services.agent_workbench.context_pack import ContextPackService
from services.agent_workbench.diagnostics import doctor_payload, schema_check_payload
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.agent_workbench.fingerprints import canonical_hash
from services.agent_workbench.mutation_ledger import MutationLedgerRepository
from services.agent_workbench.project_setup import (
    ProjectCreateRequest,
    ProjectSetupMutationRunner,
    ProjectSetupRetryRequest,
)
from services.agent_workbench.read_projection import ReadProjectionService
from services.agent_workbench.schema_readiness import (
    MUTATION_LEDGER_REQUIREMENTS,
    check_schema_readiness,
)

STATUS_COMMAND: Final[str] = "agileforge status"
WORKFLOW_NEXT_COMMAND: Final[str] = "agileforge workflow next"


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

    def complete(
        self,
        *,
        project_id: int,
        expected_state: str,
        idempotency_key: str,
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
        vision_runner: _VisionPhaseRunner | None = None,
        backlog_runner: _BacklogPhaseRunner | None = None,
        roadmap_runner: _RoadmapPhaseRunner | None = None,
        story_runner: _StoryPhaseRunner | None = None,
        sprint_runner: _SprintPhaseRunner | None = None,
    ) -> None:
        """Initialize the facade with explicit projection dependencies."""
        self._read_projection = read_projection
        self._authority_projection = authority_projection
        self._project_setup_runner = project_setup_runner
        self._authority_review = authority_review
        self._authority_decision_runner = authority_decision_runner
        self._vision_runner = vision_runner
        self._backlog_runner = backlog_runner
        self._roadmap_runner = roadmap_runner
        self._story_runner = story_runner
        self._sprint_runner = sprint_runner
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
            review = (
                self.authority_review(
                    project_id=project_id,
                    include_spec="summary",
                    output_format="json",
                )
                if setup_status == "authority_pending_review"
                and (authority is None or authority.get("ok") is True)
                else None
            )
            return _setup_workflow_next(
                project_id=project_id,
                setup_status=setup_status,
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
    ) -> dict[str, Any]:
        """Generate or refine a Story draft."""
        return self._get_story_runner().generate(
            project_id=project_id,
            parent_requirement=parent_requirement,
            user_input=user_input,
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

    def story_complete(
        self,
        *,
        project_id: int,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Complete the Story phase."""
        return self._get_story_runner().complete(
            project_id=project_id,
            expected_state=expected_state,
            idempotency_key=idempotency_key,
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
    ) -> dict[str, Any]:
        """Create a Story dependency proposal artifact."""
        return self._get_story_runner().dependency_propose(
            project_id=project_id,
            expected_state=expected_state,
            idempotency_key=idempotency_key,
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

    def _get_read_projection(self) -> _ReadProjection:
        """Return the read projection, constructing the default lazily."""
        if self._read_projection is None:
            self._read_projection = ReadProjectionService()
        return self._read_projection

    def _get_authority_projection(self) -> _AuthorityProjection:
        """Return the authority projection, constructing the default lazily."""
        if self._authority_projection is None:
            self._authority_projection = AuthorityProjectionService()
        return self._authority_projection

    def _get_context_pack(self) -> ContextPackService:
        """Return the context pack service after projections are needed."""
        if self._context_pack is None:
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
            self._authority_review = AuthorityReviewService()
        return self._authority_review

    def _get_authority_decision_runner(self) -> _AuthorityDecisionRunner:
        """Return the authority decision runner, constructing the default lazily."""
        if self._authority_decision_runner is None:
            self._authority_decision_runner = AuthorityDecisionRunner()
        return self._authority_decision_runner

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
        data["next_actions"] = []
        data["blocked_future_commands"] = [
            {
                "command": (
                    "agileforge project spec update "
                    f"--project-id {project_id} --spec-file "
                    f"{_authority_spec_file_template(authority)}"
                ),
                "installed": False,
                "reason": (
                    "Spec update/recompile is required after authority rejection, "
                    "but this command is not installed yet."
                ),
            }
        ]
        data["manual_remediation"] = [
            "No installed CLI command can recompile a rejected authority yet.",
            (
                "Revise the spec or compiler, then run the future project spec "
                "update command when installed."
            ),
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
    blocked_future_commands: list[Any] = []
    status: str | None = None
    if fsm_state == "BACKLOG_PERSISTENCE":
        roadmap_command = f"agileforge roadmap generate --project-id {project_id}"
        if command_is_available("agileforge roadmap generate"):
            next_valid_commands.append(roadmap_command)
            status = "next_phase_available"
        else:
            blocked_future_commands.append(
                {
                    "command": roadmap_command,
                    "installed": False,
                    "reason": "Roadmap CLI is not installed yet.",
                }
            )
            status = "blocked_by_uninstalled_next_phase"

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
    blocked_future_commands: list[Any] = []
    status: str | None = None
    if fsm_state == "ROADMAP_PERSISTENCE":
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
            status = "next_phase_available"
        else:
            blocked_future_commands.append(
                {
                    "command": (f"agileforge story pending --project-id {project_id}"),
                    "installed": False,
                    "reason": "Story phase CLI is not installed yet.",
                }
            )
            status = "blocked_by_uninstalled_next_phase"

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
            return [pending_command, generate_command]
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


def _story_coverage_is_complete(workflow: dict[str, Any]) -> bool:
    """Return whether all Roadmap requirements have Story coverage."""
    state = _envelope_data(workflow).get("state")
    state_data = state if isinstance(state, dict) else {}
    requirements = _roadmap_requirements_from_state(state_data)
    if not requirements:
        return False

    saved = state_data.get("story_saved")
    saved_map = saved if isinstance(saved, dict) else {}
    return all(
        bool(saved_map.get(requirement))
        or _story_requirement_has_merge_resolution(
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
    }:
        return None

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
                "agileforge sprint status",
                f"agileforge sprint status --project-id {project_id}",
            ),
            (
                "agileforge sprint tasks",
                f"agileforge sprint tasks --project-id {project_id}",
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


def _authority_spec_file_template(authority: dict[str, Any] | None) -> str:
    """Return a concrete disk spec path when available, else a template token."""
    if authority is None:
        return "<spec-file>"
    authority_data = _envelope_data(authority)
    disk_spec = authority_data.get("disk_spec")
    if isinstance(disk_spec, dict):
        resolved_path = disk_spec.get("resolved_path")
        if isinstance(resolved_path, str) and resolved_path:
            return resolved_path
    return "<updated-spec-file>"


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
