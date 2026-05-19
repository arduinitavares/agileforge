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
from services.agent_workbench.command_registry import installed_command_names
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


class AgentWorkbenchApplication:
    """Thin facade shared by CLI transport and future API parity paths."""

    def __init__(
        self,
        *,
        read_projection: _ReadProjection | None = None,
        authority_projection: _AuthorityProjection | None = None,
        project_setup_runner: _ProjectSetupRunner | None = None,
        authority_review: _AuthorityReview | None = None,
        authority_decision_runner: _AuthorityDecisionRunner | None = None,
    ) -> None:
        """Initialize the facade with explicit projection dependencies."""
        self._read_projection = read_projection
        self._authority_projection = authority_projection
        self._project_setup_runner = project_setup_runner
        self._authority_review = authority_review
        self._authority_decision_runner = authority_decision_runner
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


def _mutation_ledger_repository() -> tuple[MutationLedgerRepository, None] | tuple[
    None,
    dict[str, Any],
]:
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
