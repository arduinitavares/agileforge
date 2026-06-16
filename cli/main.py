"""Agent workbench CLI transport."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import io
import json
import sys
from collections.abc import Callable, Mapping
from contextlib import redirect_stdout
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, Protocol, TypedDict, cast
from uuid import uuid4

from pydantic import ValidationError

from models.enums import StoryResolution
from services.agent_workbench.envelope import (
    WorkbenchError,
    WorkbenchWarning,
    error_envelope,
    success_envelope,
)
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from utils.agileforge_spec_profile import (
    TechnicalSpecArtifact,
    canonical_spec_hash,
    export_agileforge_spec_schema,
    render_markdown,
    rendered_markdown_hash,
)
from utils.logging_config import configure_logging

if TYPE_CHECKING:
    from services.agent_workbench.authority_decision import (
        AuthorityAcceptRequest,
        AuthorityRejectRequest,
        IncompleteReviewOverride,
    )

DEFAULT_CONTEXT_PHASE: str = "overview"
INVALID_COMMAND_EXIT_CODE: int = 2
COMMAND_EXCEPTION_EXIT_CODE: int = 1
HELP_DESCRIPTION: str = (
    "AgileForge agent-facing CLI for workflow inspection and guarded mutations."
)
HELP_EPILOG: str = (
    """\
Examples:
  agileforge project list
  agileforge status --project-id 1
  agileforge workflow state --project-id 1
  agileforge authority status --project-id 1
  agileforge authority review --project-id 1
  agileforge authority accept --project-id 1
  agileforge authority reject --project-id 1 --review-token <review_token> """
    """--reason "..." --idempotency-key reject-001
  agileforge vision generate --project-id 1 --input "optional guidance"
  agileforge vision save --project-id 1
  agileforge backlog generate --project-id 1 --input "optional guidance"
  agileforge backlog preview --project-id 1
  agileforge backlog save --project-id 1 --attempt-id <attempt_id> """
    """--expected-artifact-fingerprint <fingerprint> --expected-state BACKLOG_REVIEW """
    """--idempotency-key save-backlog-001
  agileforge backlog reconcile --project-id 1 --idempotency-key reconcile-backlog-001
  agileforge backlog refine-preview --project-id 1 --source-attempt-id <attempt_id> """
    """--operations-file refinement_ops.json
  agileforge backlog refine-record --project-id 1 --source-attempt-id <attempt_id> """
    """--operations-file refinement_ops.json --expected-source-fingerprint """
    """<fingerprint> --expected-state SPRINT_COMPLETE --idempotency-key """
    """refine-backlog-001
  agileforge backlog approve --project-id 1 --attempt-id <attempt_id> """
    """--approved-artifact-fingerprint <fingerprint> --idempotency-key """
    """approve-refinement-001
  agileforge backlog refine-import --project-id 1 --source-artifact source.json """
    """--edited-file edited.json --expected-source-fingerprint <fingerprint> """
    """--idempotency-key refine-import-001
  agileforge backlog reset-active --project-id 1 --attempt-id <attempt_id> """
    """--expected-artifact-fingerprint <fingerprint> --expected-state """
    """BACKLOG_REVIEW --reset-reason "active_backlog_reset" """
    """--archive-all-active-stories --idempotency-key reset-active-001
  agileforge evidence collect --project-id 1 --repo-path /path/to/repo """
    """--idempotency-key evidence-001
  agileforge evidence collect --project-id 1 --from-file evidence_report.json """
    """--idempotency-key evidence-import-001
  agileforge as-built assess --project-id 1 --repo-path /path/to/repo """
    """--spec-mode unknown --idempotency-key as-built-001
  agileforge roadmap generate --project-id 1 --input "optional guidance"
  agileforge roadmap save --project-id 1 --attempt-id <attempt_id> """
    """--expected-artifact-fingerprint <fingerprint> --expected-state ROADMAP_REVIEW """
    """--idempotency-key save-roadmap-001
  agileforge story pending --project-id 1
  agileforge story generate --project-id 1 --parent-requirement 'Roadmap requirement'
  agileforge story save --project-id 1 --parent-requirement 'Roadmap requirement' """
    """--attempt-id <attempt_id> --expected-artifact-fingerprint <fingerprint> """
    """--expected-state STORY_REVIEW --idempotency-key save-story-001
  agileforge sprint candidates --project-id 1
  agileforge sprint generate --project-id 1
  agileforge sprint save --project-id 1 --team-name Delivery --attempt-id """
    """<attempt_id> --expected-artifact-fingerprint <fingerprint> """
    """--expected-state SPRINT_DRAFT --idempotency-key save-sprint-001
  agileforge sprint start --project-id 1 --expected-state SPRINT_PERSISTENCE """
    """--idempotency-key start-sprint-001
  agileforge sprint status --project-id 1
  agileforge sprint status --project-id 1 --sprint-id <completed_sprint_id>
  agileforge sprint tasks --project-id 1
  agileforge sprint task next --project-id 1
  agileforge sprint task update --project-id 1 --task-id <task_id> """
    """--status Done --expected-status "In Progress" --expected-task-fingerprint """
    """<task_fingerprint> --idempotency-key update-task-001 """
    """--outcome-summary "..." --checklist-result fully_met """
    """--artifact-ref path/to/file --validation-summary "pytest ..."
  agileforge scope extension validate --project-id 1 --spec-file amended-spec.json
  agileforge scope extension start --project-id 1 --spec-file amended-spec.json """
    """--base-spec-version-id 3 --expected-state SPRINT_COMPLETE """
    """--idempotency-key scope-extension-001
  agileforge context pack --project-id 1 --phase sprint-planning
"""
)
type JsonObject = dict[str, object]
type JsonList = list[object]
CommandResult = tuple[str, JsonObject]
CommandHandler = Callable[[argparse.Namespace, "_Application"], CommandResult]
INCOMPLETE_REVIEW_OVERRIDE_PARTS = 3


class _AuthorityRequestKwargs(TypedDict):
    """Typed common kwargs for authority decision request models."""

    project_id: int
    review_token: str | None
    pending_authority_id: int | None
    expected_authority_fingerprint: str | None
    expected_source_spec_hash: str | None
    expected_disk_spec_hash: str | None
    expected_resolved_spec_path: str | None
    expected_state: str | None
    expected_setup_status: str | None
    expected_content_included: bool | None
    expected_omission_assessment: str | None
    expected_coverage_summary_fingerprint: str | None
    idempotency_key: str | None
    changed_by: str | None
    actor_mode: str


AUTHORITY_EXPLICIT_GUARD_FIELDS: tuple[str, ...] = (
    "pending_authority_id",
    "expected_authority_fingerprint",
    "expected_source_spec_hash",
    "expected_disk_spec_hash",
    "expected_resolved_spec_path",
    "expected_state",
    "expected_setup_status",
)
AUTHORITY_COMPLETENESS_GUARD_FIELDS: tuple[str, ...] = (
    "expected_content_included",
    "expected_omission_assessment",
    "expected_coverage_summary_fingerprint",
)
AUTHORITY_ALL_GUARD_FIELDS: tuple[str, ...] = (
    *AUTHORITY_EXPLICIT_GUARD_FIELDS,
    *AUTHORITY_COMPLETENESS_GUARD_FIELDS,
)


class _CliParseError(Exception):
    """Raised when argparse rejects normal command input."""


class _WorkbenchArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that lets main emit JSON for parse errors."""

    def error(self, message: str) -> NoReturn:
        """Raise instead of writing argparse error text to stderr."""
        raise _CliParseError(message)


class _Application(Protocol):
    """Application methods exposed to the CLI transport."""

    def project_list(self) -> JsonObject:
        """Return project list projection."""
        ...

    def project_show(self, *, project_id: int) -> JsonObject:
        """Return project detail projection."""
        ...

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
    ) -> JsonObject:
        """Create a project through the guarded mutation facade."""
        ...

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
    ) -> JsonObject:
        """Retry interrupted project setup through the guarded mutation facade."""
        ...

    def scope_extension_validate(
        self,
        *,
        project_id: int,
        spec_file: str,
        base_spec_version_id: int | None = None,
    ) -> JsonObject:
        """Validate an amended spec through the scope-extension runner."""
        ...

    def scope_extension_start(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        spec_file: str,
        base_spec_version_id: int,
        expected_state: str,
        idempotency_key: str,
        changed_by: str = "cli-agent",
    ) -> JsonObject:
        """Start guarded scope extension through the runner."""
        ...

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
    ) -> JsonObject:
        """Compile pending authority through the guarded mutation facade."""
        ...

    def workflow_state(self, *, project_id: int) -> JsonObject:
        """Return workflow state projection."""
        ...

    def workflow_next(self, *, project_id: int) -> JsonObject:
        """Return next workflow commands projection."""
        ...

    def authority_status(self, *, project_id: int) -> JsonObject:
        """Return authority status projection."""
        ...

    def authority_invariants(
        self,
        *,
        project_id: int,
        spec_version_id: int | None = None,
    ) -> JsonObject:
        """Return authority invariants projection."""
        ...

    def authority_review(
        self,
        *,
        project_id: int,
        include_spec: str = "auto",
        output_format: str = "json",
    ) -> JsonObject:
        """Return a pending authority review packet."""
        ...

    def authority_accept(self, request: AuthorityAcceptRequest) -> JsonObject:
        """Accept pending authority from a guarded request."""
        ...

    def authority_reject(self, request: AuthorityRejectRequest) -> JsonObject:
        """Reject pending authority from a guarded request."""
        ...

    def authority_regenerate(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        spec_version_id: int,
        compiler_model: str | None = None,
        idempotency_key: str | None = None,
        changed_by: str = "cli-agent",
        dry_run: bool = False,
    ) -> JsonObject:
        """Regenerate compiled authority for an approved spec version."""
        ...

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
    ) -> JsonObject:
        """Record structured feedback for pending authority."""
        ...

    def authority_curate(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        spec_version_id: int,
        source_authority_id: int,
        expected_source_authority_fingerprint: str,
        feedback_attempt_id: str,
        idempotency_key: str,
        max_iterations: int = 2,
        compiler_model: str | None = None,
        changed_by: str = "cli-agent",
        correlation_id: str | None = None,
    ) -> JsonObject:
        """Run bounded authority curation."""
        ...

    def vision_generate(
        self,
        *,
        project_id: int,
        user_input: str | None = None,
    ) -> JsonObject:
        """Generate or refine a Vision draft."""
        ...

    def vision_history(self, *, project_id: int) -> JsonObject:
        """Return Vision attempt history."""
        ...

    def vision_save(self, *, project_id: int) -> JsonObject:
        """Persist the current Vision draft."""
        ...

    def backlog_generate(
        self,
        *,
        project_id: int,
        user_input: str | None = None,
    ) -> JsonObject:
        """Generate or refine a Backlog draft."""
        ...

    def backlog_preview(
        self,
        *,
        project_id: int,
        user_input: str | None = None,
    ) -> JsonObject:
        """Generate a non-persisted Backlog preview."""
        ...

    def backlog_refine_preview(
        self,
        *,
        project_id: int,
        source_attempt_id: str | None = None,
        operations_file: str | None = None,
        source_artifact: str | None = None,
        user_input: str | None = None,
    ) -> JsonObject:
        """Preview canonical Backlog refinement operations."""
        ...

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
    ) -> JsonObject:
        """Record canonical Backlog refinement operations."""
        ...

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
    ) -> JsonObject:
        """Record host-mediated Backlog refinement approval."""
        ...

    def backlog_refine_import(
        self,
        *,
        project_id: int,
        source_artifact: str,
        edited_file: str,
        expected_source_fingerprint: str,
        idempotency_key: str,
    ) -> JsonObject:
        """Fail closed until deterministic Backlog refinement import exists."""
        ...

    def backlog_history(self, *, project_id: int) -> JsonObject:
        """Return Backlog attempt history."""
        ...

    def backlog_save(
        self,
        *,
        project_id: int,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> JsonObject:
        """Persist the current Backlog draft."""
        ...

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
    ) -> JsonObject:
        """Install an approved refined attempt as the active backlog baseline."""
        ...

    def backlog_reconcile(
        self,
        *,
        project_id: int,
        idempotency_key: str,
    ) -> JsonObject:
        """Repair legacy duplicate active Backlog seed rows."""
        ...

    def evidence_collect(
        self,
        *,
        project_id: int,
        repo_path: str | None,
        from_file: str | None,
        idempotency_key: str,
        include_generated_artifacts: bool = False,
    ) -> JsonObject:
        """Collect or import evidence and cache it in workflow state."""
        ...

    def as_built_assess(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        repo_path: str,
        spec_file: str | None,
        spec_mode: str,
        user_input: str | None,
        idempotency_key: str,
    ) -> JsonObject:
        """Assess implementation state and cache it in workflow state."""
        ...

    def brownfield_source_import(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        source_file: str,
        source_kind: str = "source_file",
        idempotency_key: str,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> JsonObject:
        """Record a raw brownfield source artifact."""
        ...

    def brownfield_scan(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        repo_path: str,
        source_attempt_id: str | None = None,
        idempotency_key: str,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> JsonObject:
        """Record a brownfield repository scan attempt."""
        ...

    def brownfield_spec_draft(
        self,
        **kwargs: object,
    ) -> JsonObject:
        """Create a generated curated spec draft attempt."""
        ...

    def brownfield_spec_import(
        self,
        **kwargs: object,
    ) -> JsonObject:
        """Record a human-imported curated spec attempt."""
        ...

    def brownfield_spec_approve(
        self,
        **kwargs: object,
    ) -> JsonObject:
        """Approve a curated spec attempt."""
        ...

    def roadmap_generate(
        self,
        *,
        project_id: int,
        user_input: str | None = None,
    ) -> JsonObject:
        """Generate or refine a Roadmap draft."""
        ...

    def roadmap_history(self, *, project_id: int) -> JsonObject:
        """Return Roadmap attempt history."""
        ...

    def roadmap_save(
        self,
        *,
        project_id: int,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> JsonObject:
        """Persist the current Roadmap draft."""
        ...

    def story_show(self, *, story_id: int) -> JsonObject:
        """Return story detail projection."""
        ...

    def story_pending(self, *, project_id: int) -> JsonObject:
        """Return Story pending roadmap requirements."""
        ...

    def story_generate(
        self,
        *,
        project_id: int,
        parent_requirement: str,
        user_input: str | None = None,
        force_feedback: bool = False,
    ) -> JsonObject:
        """Generate or refine a Story draft."""
        ...

    def story_retry(self, *, project_id: int, parent_requirement: str) -> JsonObject:
        """Retry the latest retryable Story request."""
        ...

    def story_history(
        self,
        *,
        project_id: int,
        parent_requirement: str,
    ) -> JsonObject:
        """Return Story attempt history."""
        ...

    def story_save(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        parent_requirement: str,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> JsonObject:
        """Persist the current Story draft."""
        ...

    def story_complete(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        expected_state: str,
        idempotency_key: str,
        scope: str | None = None,
        scope_id: str | None = None,
        parent_requirements: list[str] | None = None,
    ) -> JsonObject:
        """Complete the Story phase."""
        ...

    def story_reopen(
        self,
        *,
        project_id: int,
        parent_requirement: str,
        expected_state: str,
        idempotency_key: str,
    ) -> JsonObject:
        """Reopen one saved Story requirement before Sprint work exists."""
        ...

    def story_repair_readiness(
        self,
        *,
        project_id: int,
        expected_state: str,
        idempotency_key: str,
    ) -> JsonObject:
        """Backfill Story planning metadata before Sprint work starts."""
        ...

    def story_dependencies_inspect(self, *, project_id: int) -> JsonObject:
        """Inspect Story dependency graph."""
        ...

    def story_dependencies_propose(
        self,
        *,
        project_id: int,
        expected_state: str,
        idempotency_key: str,
        manual_edges: list[str] | None = None,
    ) -> JsonObject:
        """Create a Story dependency proposal artifact."""
        ...

    def story_dependencies_apply(
        self,
        *,
        project_id: int,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> JsonObject:
        """Apply a reviewed Story dependency proposal artifact."""
        ...

    def sprint_candidates(self, *, project_id: int) -> JsonObject:
        """Return sprint candidate projection."""
        ...

    def sprint_generate(
        self,
        *,
        project_id: int,
        user_input: str | None = None,
        selected_story_ids: list[int] | None = None,
        max_story_points: int | None = None,
        include_task_decomposition: bool = True,
    ) -> JsonObject:
        """Generate or refine a Sprint draft."""
        ...

    def sprint_history(self, *, project_id: int) -> JsonObject:
        """Return Sprint planner attempts and execution history."""
        ...

    def sprint_metrics(self, *, project_id: int) -> JsonObject:
        """Return Sprint metrics and planning recommendation."""
        ...

    def sprint_save(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        team_name: str,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> JsonObject:
        """Persist the current Sprint draft."""
        ...

    def sprint_start(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
        expected_state: str,
        idempotency_key: str,
    ) -> JsonObject:
        """Start a saved Sprint."""
        ...

    def sprint_status(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        """Return Sprint execution status."""
        ...

    def sprint_tasks(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        """Return Sprint execution tasks."""
        ...

    def sprint_task_next(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        """Return the next Sprint task ticket."""
        ...

    def sprint_task_show(
        self,
        *,
        project_id: int,
        task_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        """Return one Sprint task ticket."""
        ...

    def sprint_task_history(
        self,
        *,
        project_id: int,
        task_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        """Return one Sprint task execution history."""
        ...

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
    ) -> JsonObject:
        """Log Sprint task execution progress."""
        ...

    def sprint_story_readiness(
        self,
        *,
        project_id: int,
        story_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        """Return close readiness for one Sprint story."""
        ...

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
    ) -> JsonObject:
        """Close one Sprint story."""
        ...

    def sprint_close_readiness(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        """Return close readiness for the active Sprint."""
        ...

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
    ) -> JsonObject:
        """Close the active Sprint."""
        ...

    def sprint_review(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        """Return post-sprint review context."""
        ...

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
    ) -> JsonObject:
        """Record post-sprint triage metadata."""
        ...

    def context_pack(
        self,
        *,
        project_id: int,
        phase: str = DEFAULT_CONTEXT_PHASE,
    ) -> JsonObject:
        """Return a context pack projection."""
        ...

    def status(self, *, project_id: int) -> JsonObject:
        """Return project status projection."""
        ...

    def doctor(self) -> JsonObject:
        """Return local diagnostics."""
        ...

    def schema_check(self) -> JsonObject:
        """Return schema readiness diagnostics."""
        ...

    def capabilities(self) -> JsonObject:
        """Return installed command capabilities."""
        ...

    def command_schema(self, *, command_name: str) -> JsonObject:
        """Return one command schema."""
        ...

    def mutation_show(self, *, mutation_event_id: int) -> JsonObject:
        """Return one mutation ledger event."""
        ...

    def mutation_list(
        self,
        *,
        project_id: int | None = None,
        status: str | None = None,
    ) -> JsonObject:
        """Return mutation ledger events."""
        ...

    def mutation_resume(
        self,
        *,
        mutation_event_id: int,
        correlation_id: str | None = None,
    ) -> JsonObject:
        """Acquire a recovery lease for a mutation event."""
        ...


def _print_json(payload: JsonObject) -> None:
    """Write one JSON envelope to stdout."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    sys.stdout.write("\n")


def _coerce_exit_code(value: object, *, default: int = 1) -> int:
    """Return a process exit code from a structured error value."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.removeprefix("-").isdigit():
            return int(stripped)
    return default


def _as_mapping(value: object) -> Mapping[object, object] | None:
    """Return a typed mapping view for JSON-like dictionaries."""
    if not isinstance(value, dict):
        return None
    return cast("Mapping[object, object]", value)


def _exit_code(result: JsonObject) -> int:
    """Return the process exit code for an envelope."""
    if result.get("ok") is True:
        return 0

    errors = result.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        first_mapping = _as_mapping(first)
        if first_mapping is not None:
            return _coerce_exit_code(first_mapping.get("exit_code"))

    return 1


def _string_list(value: object) -> list[str]:
    """Return a list of strings from a structured envelope field."""
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _details_dict(value: object) -> dict[str, object]:
    """Return a details mapping from a structured envelope field."""
    mapping = _as_mapping(value)
    if mapping is None:
        return {}
    return {str(key): detail for key, detail in mapping.items()}


def _warning_from_value(value: object) -> WorkbenchWarning:
    """Return a WorkbenchWarning from a raw service warning."""
    if isinstance(value, WorkbenchWarning):
        return value
    mapping = _as_mapping(value)
    if mapping is not None:
        return WorkbenchWarning(
            code=str(mapping.get("code", "COMMAND_WARNING")),
            message=str(mapping.get("message", "Command warning.")),
            details=_details_dict(mapping.get("details")),
            remediation=_string_list(mapping.get("remediation")),
        )
    return WorkbenchWarning(
        code="COMMAND_WARNING",
        message=str(value),
    )


def _warnings_from_result(result: JsonObject) -> list[WorkbenchWarning]:
    """Return structured warnings from a service result."""
    warnings = result.get("warnings")
    if not isinstance(warnings, list):
        return []
    return [_warning_from_value(warning) for warning in warnings]


def _error_from_value(value: object) -> WorkbenchError:
    """Return a WorkbenchError from a raw service error."""
    if isinstance(value, WorkbenchError):
        return value
    mapping = _as_mapping(value)
    if mapping is not None:
        return WorkbenchError(
            code=str(mapping.get("code", "COMMAND_FAILED")),
            message=str(mapping.get("message", "Command failed.")),
            details=_details_dict(mapping.get("details")),
            remediation=_string_list(mapping.get("remediation")),
            exit_code=_coerce_exit_code(mapping.get("exit_code")),
            retryable=mapping.get("retryable") is True,
        )
    return WorkbenchError(
        code="COMMAND_FAILED",
        message=str(value),
        exit_code=1,
        retryable=False,
    )


def _errors_from_result(result: JsonObject) -> list[WorkbenchError]:
    """Return structured errors from a service result."""
    errors = result.get("errors")
    if not isinstance(errors, list):
        return []
    return [_error_from_value(error) for error in errors]


def _success_data(result: JsonObject) -> JsonObject | JsonList:
    """Return success data in an envelope-compatible shape."""
    data = result.get("data")
    if isinstance(data, dict):
        return {str(key): value for key, value in data.items()}
    if isinstance(data, list):
        return cast("JsonList", data)
    return {}


def _source_fingerprint(result: JsonObject) -> str | None:
    """Return source fingerprint metadata from successful result data."""
    data = result.get("data")
    data_mapping = _as_mapping(data)
    if data_mapping is None:
        return None
    source_fingerprint = data_mapping.get("source_fingerprint")
    if isinstance(source_fingerprint, str):
        return source_fingerprint
    return None


def _wrap(
    command: str,
    result: JsonObject,
    *,
    compact_warnings: bool = False,
) -> JsonObject:
    """Wrap a service result in a stable CLI envelope when needed."""
    if "meta" in result:
        if compact_warnings:
            envelope = dict(result)
            envelope["warnings"] = [
                warning.to_dict()
                for warning in _compact_warning_list(_warnings_from_result(result))
            ]
            return envelope
        return result

    warnings = _warnings_from_result(result)
    if compact_warnings:
        warnings = _compact_warning_list(warnings)
    if result.get("ok") is True:
        return success_envelope(
            command=command,
            data=_success_data(result),
            warnings=warnings,
            source_fingerprint=_source_fingerprint(result),
        )

    errors = _errors_from_result(result)
    if errors:
        envelope = error_envelope(
            command=command,
            error=errors[0],
            warnings=warnings,
        )
        data = result.get("data")
        if isinstance(data, dict):
            envelope["data"] = {str(key): value for key, value in data.items()}
        if len(errors) > 1:
            envelope["errors"] = [error.to_dict() for error in errors]
        return envelope

    return error_envelope(
        command=command,
        error=WorkbenchError(
            code="COMMAND_FAILED",
            message="Command failed without structured error details.",
            exit_code=1,
            retryable=False,
        ),
        warnings=warnings,
    )


def _compact_warning_list(warnings: list[WorkbenchWarning]) -> list[WorkbenchWarning]:
    """Return a compact single-warning summary for noisy warning lists."""
    if not warnings:
        return []
    warning_counts: dict[str, int] = {}
    for warning in warnings:
        warning_counts[warning.code] = warning_counts.get(warning.code, 0) + 1
    return [
        WorkbenchWarning(
            code="EVIDENCE_WARNINGS_COMPACTED",
            message=f"Evidence collection produced {len(warnings)} warnings.",
            details={
                "warning_count": len(warnings),
                "warning_counts": dict(sorted(warning_counts.items())),
                "sample_warnings": [warnings[0].to_dict()],
                "verbose_flag": "--verbose",
            },
            remediation=[
                "Rerun with --verbose to include full warning details.",
            ],
        )
    ]


def _compact_warnings_requested(args: argparse.Namespace) -> bool:
    """Return whether this command should compact warning payloads."""
    return (
        getattr(args, "group", None) == "evidence"
        and getattr(args, "action", None) == "collect"
        and not bool(getattr(args, "verbose", False))
    )


def _plain_text_output(args: argparse.Namespace, result: JsonObject) -> str | None:
    """Return plain text for commands that explicitly requested text output."""
    if (
        getattr(args, "group", None) != "authority"
        or getattr(args, "action", None) != "review"
        or getattr(args, "format", None) != "text"
        or result.get("ok") is not True
    ):
        return None
    data = _as_mapping(result.get("data"))
    if data is None:
        return None
    text = data.get("text")
    return text if isinstance(text, str) else None


def _parse_error_envelope(message: str, argv: list[str] | None) -> JsonObject:
    """Return a structured envelope for invalid command input."""
    parsed_argv = list(argv) if argv is not None else sys.argv[1:]
    return error_envelope(
        command="agileforge",
        error=WorkbenchError(
            code="INVALID_COMMAND",
            message=message,
            details={"argv": parsed_argv},
            remediation=["Run agileforge --help."],
            exit_code=INVALID_COMMAND_EXIT_CODE,
            retryable=False,
        ),
    )


def _parse_bool_token(value: str) -> bool:
    """Parse an explicit true/false CLI token."""
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    msg = "expected one of: true, false"
    raise argparse.ArgumentTypeError(msg)


def _add_authority_guard_args(command: argparse.ArgumentParser) -> None:
    """Add explicit authority review guard arguments to a parser."""
    command.add_argument("--pending-authority-id", type=int)
    command.add_argument("--expected-authority-fingerprint")
    command.add_argument("--expected-source-spec-hash")
    command.add_argument("--expected-disk-spec-hash")
    command.add_argument("--expected-resolved-spec-path")
    command.add_argument("--expected-state")
    command.add_argument("--expected-setup-status")
    command.add_argument("--expected-content-included", type=_parse_bool_token)
    command.add_argument(
        "--expected-omission-assessment",
        choices=("complete", "incomplete"),
    )
    command.add_argument("--expected-coverage-summary-fingerprint")


def _exception_envelope(exc: Exception) -> JsonObject:
    """Return a structured envelope for unexpected command exceptions."""
    return error_envelope(
        command="agileforge",
        error=WorkbenchError(
            code="COMMAND_EXCEPTION",
            message=str(exc) or "Command failed with an unexpected exception.",
            details={"exception_type": type(exc).__name__},
            remediation=[],
            exit_code=COMMAND_EXCEPTION_EXIT_CODE,
            retryable=False,
        ),
    )


def build_parser() -> argparse.ArgumentParser:  # noqa: PLR0915
    """Build the top-level CLI parser."""
    parser = _WorkbenchArgumentParser(
        prog="agileforge",
        description=HELP_DESCRIPTION,
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(
        dest="group",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )

    project = subparsers.add_parser(
        "project",
        help="List and inspect AgileForge projects.",
    )
    project_sub = project.add_subparsers(
        dest="action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    project_list = project_sub.add_parser("list", help="List projects.")
    project_list.set_defaults(command_handler=_project_list)
    project_show = project_sub.add_parser("show", help="Show one project.")
    project_show.add_argument("--project-id", type=int, required=True)
    project_show.set_defaults(command_handler=_project_show)
    project_create = project_sub.add_parser("create", help="Create a project.")
    project_create.add_argument("--name", required=True)
    project_create.add_argument("--spec-file")
    project_create.add_argument(
        "--setup-mode",
        choices=("greenfield", "brownfield"),
        default="greenfield",
    )
    project_create.add_argument("--idempotency-key")
    project_create.add_argument("--dry-run", action="store_true")
    project_create.add_argument("--dry-run-id")
    project_create.add_argument("--correlation-id")
    project_create.add_argument("--changed-by", default="cli-agent")
    project_create.set_defaults(command_handler=_project_create)
    project_setup = project_sub.add_parser("setup", help="Retry project setup.")
    project_setup_sub = project_setup.add_subparsers(
        dest="setup_action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    project_setup_retry = project_setup_sub.add_parser("retry", help="Retry setup.")
    project_setup_retry.add_argument("--project-id", type=int, required=True)
    project_setup_retry.add_argument("--spec-file")
    project_setup_retry.add_argument(
        "--setup-mode",
        choices=("greenfield", "brownfield"),
        default="greenfield",
    )
    project_setup_retry.add_argument("--expected-state", required=True)
    project_setup_retry.add_argument("--expected-context-fingerprint", required=True)
    project_setup_retry.add_argument("--recovery-mutation-event-id", type=int)
    project_setup_retry.add_argument("--idempotency-key")
    project_setup_retry.add_argument("--dry-run", action="store_true")
    project_setup_retry.add_argument("--dry-run-id")
    project_setup_retry.add_argument("--correlation-id")
    project_setup_retry.add_argument("--changed-by", default="cli-agent")
    project_setup_retry.set_defaults(command_handler=_project_setup_retry)

    workflow = subparsers.add_parser(
        "workflow",
        help="Inspect workflow state and next installed commands.",
    )
    workflow_sub = workflow.add_subparsers(
        dest="action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    workflow_state = workflow_sub.add_parser("state", help="Show workflow state.")
    workflow_state.add_argument("--project-id", type=int, required=True)
    workflow_state.set_defaults(command_handler=_workflow_state)
    workflow_next = workflow_sub.add_parser("next", help="Show next commands.")
    workflow_next.add_argument("--project-id", type=int, required=True)
    workflow_next.set_defaults(command_handler=_workflow_next)

    scope = subparsers.add_parser(
        "scope",
        help="Validate and start project scope extensions.",
    )
    scope_sub = scope.add_subparsers(
        dest="action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    scope_extension = scope_sub.add_parser(
        "extension",
        help="Run the project scope extension ritual.",
    )
    scope_extension_sub = scope_extension.add_subparsers(
        dest="extension_action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    scope_extension_validate = scope_extension_sub.add_parser(
        "validate",
        help="Validate an amended additive project spec.",
    )
    scope_extension_validate.add_argument("--project-id", type=int, required=True)
    scope_extension_validate.add_argument("--spec-file", required=True)
    scope_extension_validate.add_argument("--base-spec-version-id", type=int)
    scope_extension_validate.set_defaults(command_handler=_scope_extension_validate)
    scope_extension_start = scope_extension_sub.add_parser(
        "start",
        help="Start a guarded additive project scope extension.",
    )
    scope_extension_start.add_argument("--project-id", type=int, required=True)
    scope_extension_start.add_argument("--spec-file", required=True)
    scope_extension_start.add_argument(
        "--base-spec-version-id",
        type=int,
        required=True,
    )
    scope_extension_start.add_argument("--expected-state", required=True)
    scope_extension_start.add_argument("--idempotency-key", required=True)
    scope_extension_start.add_argument("--changed-by", default="cli-agent")
    scope_extension_start.set_defaults(command_handler=_scope_extension_start)

    authority = subparsers.add_parser(
        "authority",
        help="Inspect and manage Spec Authority.",
    )
    authority_sub = authority.add_subparsers(
        dest="action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    authority_compile = authority_sub.add_parser(
        "compile",
        help="Compile pending Spec Authority for a created project.",
    )
    authority_compile.add_argument("--project-id", type=int, required=True)
    authority_compile.add_argument("--spec-version-id", type=int, required=True)
    authority_compile.add_argument("--expected-spec-hash", required=True)
    authority_compile.add_argument("--expected-state", required=True)
    authority_compile.add_argument("--expected-setup-status", required=True)
    authority_compile.add_argument("--compiler-model")
    authority_compile.add_argument("--idempotency-key")
    authority_compile.add_argument("--dry-run", action="store_true")
    authority_compile.add_argument("--dry-run-id")
    authority_compile.add_argument("--correlation-id")
    authority_compile.add_argument("--changed-by", default="cli-agent")
    authority_compile.set_defaults(command_handler=_authority_compile)
    authority_status = authority_sub.add_parser("status", help="Show authority status.")
    authority_status.add_argument("--project-id", type=int, required=True)
    authority_status.set_defaults(command_handler=_authority_status)
    authority_invariants = authority_sub.add_parser(
        "invariants",
        help="List authority invariants.",
    )
    authority_invariants.add_argument("--project-id", type=int, required=True)
    authority_invariants.add_argument("--spec-version-id", type=int)
    authority_invariants.set_defaults(command_handler=_authority_invariants)
    authority_review = authority_sub.add_parser(
        "review",
        help="Build a pending authority review packet.",
    )
    authority_review.add_argument("--project-id", type=int, required=True)
    authority_review.add_argument(
        "--include-spec",
        choices=("auto", "full", "summary"),
        default="auto",
    )
    authority_review.add_argument(
        "--open",
        action="store_true",
        help="Acknowledge that the review packet should be opened for human review.",
    )
    authority_review.add_argument("--format", choices=("json", "text"), default="json")
    authority_review.set_defaults(command_handler=_authority_review)
    authority_accept = authority_sub.add_parser(
        "accept",
        help="Accept reviewed pending authority.",
    )
    authority_accept.add_argument("--project-id", type=int, required=True)
    authority_accept.add_argument("--review-token")
    _add_authority_guard_args(authority_accept)
    authority_accept.add_argument("--idempotency-key")
    authority_accept.add_argument("--allow-incomplete-review", action="store_true")
    authority_accept.add_argument("--incomplete-review-rationale")
    authority_accept.add_argument(
        "--incomplete-review-override",
        action="append",
        default=[],
        metavar="CANDIDATE_ID:FINDING_CODE:RATIONALE",
    )
    authority_accept.add_argument("--changed-by")
    authority_accept.set_defaults(command_handler=_authority_accept)
    authority_reject = authority_sub.add_parser(
        "reject",
        help="Reject reviewed pending authority.",
    )
    authority_reject.add_argument("--project-id", type=int, required=True)
    authority_reject.add_argument("--review-token")
    _add_authority_guard_args(authority_reject)
    authority_reject.add_argument("--reason")
    authority_reject.add_argument("--idempotency-key")
    authority_reject.add_argument("--changed-by")
    authority_reject.set_defaults(command_handler=_authority_reject)
    authority_regenerate = authority_sub.add_parser(
        "regenerate",
        help="Regenerate v2 compiled authority for an approved spec.",
    )
    authority_regenerate.add_argument("--project-id", type=int, required=True)
    authority_regenerate.add_argument("--spec-version-id", type=int, required=True)
    authority_regenerate.add_argument("--compiler-model")
    authority_regenerate.add_argument("--idempotency-key")
    authority_regenerate.add_argument("--changed-by", default="cli-agent")
    authority_regenerate.add_argument("--dry-run", action="store_true")
    authority_regenerate.set_defaults(command_handler=_authority_regenerate)
    authority_feedback = authority_sub.add_parser(
        "feedback",
        help="Record structured feedback for pending authority.",
    )
    authority_feedback_sub = authority_feedback.add_subparsers(
        dest="feedback_command",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    authority_feedback_record = authority_feedback_sub.add_parser(
        "record",
        help="Record structured feedback for pending authority.",
    )
    authority_feedback_record.add_argument("--project-id", type=int, required=True)
    authority_feedback_record.add_argument(
        "--pending-authority-id",
        type=int,
        required=True,
    )
    authority_feedback_record.add_argument(
        "--expected-authority-fingerprint",
        required=True,
    )
    authority_feedback_record.add_argument("--feedback-file", required=True)
    authority_feedback_record.add_argument("--idempotency-key", required=True)
    authority_feedback_record.add_argument("--changed-by", default="cli-agent")
    authority_feedback_record.add_argument("--correlation-id")
    authority_feedback_record.set_defaults(
        command_handler=_authority_feedback_record
    )
    authority_curate = authority_sub.add_parser(
        "curate",
        help="Run bounded authority curation.",
    )
    authority_curate.add_argument("--project-id", type=int, required=True)
    authority_curate.add_argument("--spec-version-id", type=int, required=True)
    authority_curate.add_argument("--source-authority-id", type=int, required=True)
    authority_curate.add_argument(
        "--expected-source-authority-fingerprint",
        required=True,
    )
    authority_curate.add_argument("--feedback-attempt-id", required=True)
    authority_curate.add_argument("--max-iterations", type=int, default=2)
    authority_curate.add_argument("--compiler-model")
    authority_curate.add_argument("--idempotency-key", required=True)
    authority_curate.add_argument("--changed-by", default="cli-agent")
    authority_curate.add_argument("--correlation-id")
    authority_curate.set_defaults(command_handler=_authority_curate)

    vision = subparsers.add_parser("vision", help="Run Vision phase commands.")
    vision_sub = vision.add_subparsers(
        dest="action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    vision_generate = vision_sub.add_parser(
        "generate",
        help="Generate or refine a Vision draft.",
    )
    vision_generate.add_argument("--project-id", type=int, required=True)
    vision_generate.add_argument("--input", dest="user_input")
    vision_generate.set_defaults(command_handler=_vision_generate)
    vision_history = vision_sub.add_parser(
        "history",
        help="Show Vision attempt history.",
    )
    vision_history.add_argument("--project-id", type=int, required=True)
    vision_history.set_defaults(command_handler=_vision_history)
    vision_save = vision_sub.add_parser(
        "save",
        help="Persist the current complete Vision draft.",
    )
    vision_save.add_argument("--project-id", type=int, required=True)
    vision_save.set_defaults(command_handler=_vision_save)

    backlog = subparsers.add_parser("backlog", help="Run Backlog phase commands.")
    backlog_sub = backlog.add_subparsers(
        dest="action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    backlog_generate = backlog_sub.add_parser(
        "generate",
        help="Generate or refine a Backlog draft.",
    )
    backlog_generate.add_argument("--project-id", type=int, required=True)
    backlog_generate.add_argument("--input", dest="user_input")
    backlog_generate.set_defaults(command_handler=_backlog_generate)
    backlog_preview = backlog_sub.add_parser(
        "preview",
        help="Generate a non-persisted Backlog preview.",
    )
    backlog_preview.add_argument("--project-id", type=int, required=True)
    backlog_preview.add_argument("--input", dest="user_input")
    backlog_preview.set_defaults(command_handler=_backlog_preview)
    backlog_refine_preview = backlog_sub.add_parser(
        "refine-preview",
        help="Preview canonical Backlog refinement operations.",
    )
    backlog_refine_preview.add_argument("--project-id", type=int, required=True)
    backlog_refine_preview.add_argument("--source-attempt-id")
    backlog_refine_preview.add_argument("--operations-file")
    backlog_refine_preview.add_argument("--source-artifact")
    backlog_refine_preview.add_argument("--input", dest="user_input")
    backlog_refine_preview.set_defaults(command_handler=_backlog_refine_preview)
    backlog_refine_record = backlog_sub.add_parser(
        "refine-record",
        help="Record canonical Backlog refinement operations.",
    )
    backlog_refine_record.add_argument("--project-id", type=int, required=True)
    backlog_refine_record.add_argument("--source-attempt-id", required=True)
    backlog_refine_record.add_argument("--operations-file", required=True)
    backlog_refine_record.add_argument("--expected-source-fingerprint", required=True)
    backlog_refine_record.add_argument("--expected-state", required=True)
    backlog_refine_record.add_argument("--idempotency-key", required=True)
    backlog_refine_record.add_argument("--approval-id")
    backlog_refine_record.set_defaults(command_handler=_backlog_refine_record)
    backlog_approve = backlog_sub.add_parser(
        "approve",
        help="Record host-mediated Backlog refinement approval.",
    )
    backlog_approve.add_argument("--project-id", type=int, required=True)
    backlog_approve.add_argument("--source-attempt-id")
    backlog_approve.add_argument("--attempt-id")
    backlog_approve.add_argument("--operation-set-fingerprint")
    backlog_approve.add_argument("--approved-artifact-fingerprint", required=True)
    backlog_approve.add_argument(
        "--approved-operation-id",
        action="append",
        dest="approved_operation_ids",
    )
    backlog_approve.add_argument("--idempotency-key", required=True)
    backlog_approve.set_defaults(command_handler=_backlog_approve)
    backlog_refine_import = backlog_sub.add_parser(
        "refine-import",
        help="Import deterministic Backlog refinement edits.",
    )
    backlog_refine_import.add_argument("--project-id", type=int, required=True)
    backlog_refine_import.add_argument("--source-artifact", required=True)
    backlog_refine_import.add_argument("--edited-file", required=True)
    backlog_refine_import.add_argument("--expected-source-fingerprint", required=True)
    backlog_refine_import.add_argument("--idempotency-key", required=True)
    backlog_refine_import.set_defaults(command_handler=_backlog_refine_import)
    backlog_history = backlog_sub.add_parser(
        "history",
        help="Show Backlog attempt history.",
    )
    backlog_history.add_argument("--project-id", type=int, required=True)
    backlog_history.set_defaults(command_handler=_backlog_history)
    backlog_save = backlog_sub.add_parser(
        "save",
        help="Persist the current complete Backlog draft.",
    )
    backlog_save.add_argument("--project-id", type=int, required=True)
    backlog_save.add_argument("--attempt-id", required=True)
    backlog_save.add_argument("--expected-artifact-fingerprint", required=True)
    backlog_save.add_argument("--expected-state", required=True)
    backlog_save.add_argument("--idempotency-key", required=True)
    backlog_save.set_defaults(command_handler=_backlog_save)
    backlog_reset_active = backlog_sub.add_parser(
        "reset-active",
        help="Soft-archive active backlog rows and install an approved refinement.",
    )
    backlog_reset_active.add_argument("--project-id", type=int, required=True)
    backlog_reset_active.add_argument("--attempt-id", required=True)
    backlog_reset_active.add_argument(
        "--expected-artifact-fingerprint",
        required=True,
    )
    backlog_reset_active.add_argument("--expected-state", required=True)
    backlog_reset_active.add_argument("--reset-reason", required=True)
    backlog_reset_active.add_argument(
        "--archive-all-active-stories",
        action="store_true",
    )
    backlog_reset_active.add_argument("--idempotency-key", required=True)
    backlog_reset_active.set_defaults(command_handler=_backlog_reset_active)
    backlog_reconcile = backlog_sub.add_parser(
        "reconcile",
        help="Repair legacy duplicate active Backlog seed rows.",
    )
    backlog_reconcile.add_argument("--project-id", type=int, required=True)
    backlog_reconcile.add_argument("--idempotency-key", required=True)
    backlog_reconcile.set_defaults(command_handler=_backlog_reconcile)

    evidence = subparsers.add_parser(
        "evidence",
        help="Collect implementation evidence for backlog generation.",
    )
    evidence_sub = evidence.add_subparsers(
        dest="action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    evidence_collect = evidence_sub.add_parser(
        "collect",
        help="Collect exact-tag evidence or import a reconciliation report.",
    )
    evidence_collect.add_argument("--project-id", type=int, required=True)
    evidence_collect.add_argument("--repo-path")
    evidence_collect.add_argument("--from-file")
    evidence_collect.add_argument("--idempotency-key", required=True)
    evidence_collect.add_argument(
        "--include-generated-artifacts",
        action="store_true",
        help="Scan generated AgileForge artifacts and binary outputs.",
    )
    evidence_collect.add_argument(
        "--verbose",
        action="store_true",
        help="Include full evidence warning details in JSON output.",
    )
    evidence_collect.set_defaults(command_handler=_evidence_collect)

    as_built = subparsers.add_parser(
        "as-built",
        help="Assess repository implementation state before backlog generation.",
    )
    as_built_sub = as_built.add_subparsers(
        dest="action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    as_built_assess = as_built_sub.add_parser(
        "assess",
        help="Assess current implementation state against accepted authority.",
    )
    as_built_assess.add_argument("--project-id", type=int, required=True)
    as_built_assess.add_argument("--repo-path", required=True)
    as_built_assess.add_argument("--spec-file")
    as_built_assess.add_argument(
        "--spec-mode",
        choices=("current_state", "desired_state", "proposed_change", "unknown"),
        default="unknown",
    )
    as_built_assess.add_argument("--user-input")
    as_built_assess.add_argument("--idempotency-key", required=True)
    as_built_assess.set_defaults(command_handler=_as_built_assess)

    brownfield = subparsers.add_parser(
        "brownfield",
        help="Record brownfield curation sources and repository scans.",
    )
    brownfield_sub = brownfield.add_subparsers(
        dest="action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    brownfield_source = brownfield_sub.add_parser(
        "source",
        help="Record raw brownfield source artifacts.",
    )
    brownfield_source_sub = brownfield_source.add_subparsers(
        dest="source_action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    brownfield_source_import = brownfield_source_sub.add_parser(
        "import",
        help="Import a raw brownfield source file.",
    )
    brownfield_source_import.add_argument("--project-id", type=int, required=True)
    brownfield_source_import.add_argument("--source-file", required=True)
    brownfield_source_import.add_argument("--source-kind", default="source_file")
    brownfield_source_import.add_argument("--idempotency-key", required=True)
    brownfield_source_import.add_argument("--correlation-id")
    brownfield_source_import.add_argument("--changed-by", default="cli-agent")
    brownfield_source_import.set_defaults(
        command_handler=_brownfield_source_import
    )
    brownfield_scan = brownfield_sub.add_parser(
        "scan",
        help="Record a bounded brownfield repository scan.",
    )
    brownfield_scan.add_argument("--project-id", type=int, required=True)
    brownfield_scan.add_argument("--repo-path", required=True)
    brownfield_scan.add_argument("--source-attempt-id")
    brownfield_scan.add_argument("--idempotency-key", required=True)
    brownfield_scan.add_argument("--correlation-id")
    brownfield_scan.add_argument("--changed-by", default="cli-agent")
    brownfield_scan.set_defaults(command_handler=_brownfield_scan)
    brownfield_spec = brownfield_sub.add_parser(
        "spec",
        help="Draft, import, or approve curated brownfield specs.",
    )
    brownfield_spec_sub = brownfield_spec.add_subparsers(
        dest="spec_action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    brownfield_spec_draft = brownfield_spec_sub.add_parser(
        "draft",
        help="Create a generated curated spec draft.",
    )
    brownfield_spec_draft.add_argument("--project-id", type=int, required=True)
    brownfield_spec_draft.add_argument("--scan-attempt-id", required=True)
    brownfield_spec_draft.add_argument("--user-input")
    brownfield_spec_draft.add_argument("--idempotency-key", required=True)
    brownfield_spec_draft.add_argument("--correlation-id")
    brownfield_spec_draft.add_argument("--changed-by", default="cli-agent")
    brownfield_spec_draft.set_defaults(command_handler=_brownfield_spec_draft)
    brownfield_spec_import = brownfield_spec_sub.add_parser(
        "import",
        help="Import a human-curated brownfield spec.",
    )
    brownfield_spec_import.add_argument("--project-id", type=int, required=True)
    brownfield_spec_import.add_argument("--curated-spec-file", required=True)
    brownfield_spec_import.add_argument("--expected-scan-fingerprint", required=True)
    brownfield_spec_import.add_argument("--parent-draft-attempt-id")
    brownfield_spec_import.add_argument("--idempotency-key", required=True)
    brownfield_spec_import.add_argument("--correlation-id")
    brownfield_spec_import.add_argument("--changed-by", default="cli-agent")
    brownfield_spec_import.set_defaults(command_handler=_brownfield_spec_import)
    brownfield_spec_approve = brownfield_spec_sub.add_parser(
        "approve",
        help="Approve a curated brownfield spec for authority compilation.",
    )
    brownfield_spec_approve.add_argument("--project-id", type=int, required=True)
    brownfield_spec_approve.add_argument("--attempt-id", required=True)
    brownfield_spec_approve.add_argument(
        "--expected-artifact-fingerprint",
        required=True,
    )
    brownfield_spec_approve.add_argument("--expected-state", required=True)
    brownfield_spec_approve.add_argument("--expected-setup-status", required=True)
    brownfield_spec_approve.add_argument("--idempotency-key", required=True)
    brownfield_spec_approve.add_argument("--correlation-id")
    brownfield_spec_approve.add_argument("--changed-by", default="cli-agent")
    brownfield_spec_approve.set_defaults(command_handler=_brownfield_spec_approve)

    roadmap = subparsers.add_parser("roadmap", help="Run Roadmap phase commands.")
    roadmap_sub = roadmap.add_subparsers(
        dest="action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    roadmap_generate = roadmap_sub.add_parser(
        "generate",
        help="Generate or refine a Roadmap draft.",
    )
    roadmap_generate.add_argument("--project-id", type=int, required=True)
    roadmap_generate.add_argument("--input", dest="user_input")
    roadmap_generate.set_defaults(command_handler=_roadmap_generate)
    roadmap_history = roadmap_sub.add_parser(
        "history",
        help="Show Roadmap attempt history.",
    )
    roadmap_history.add_argument("--project-id", type=int, required=True)
    roadmap_history.set_defaults(command_handler=_roadmap_history)
    roadmap_save = roadmap_sub.add_parser(
        "save",
        help="Persist the current complete Roadmap draft.",
    )
    roadmap_save.add_argument("--project-id", type=int, required=True)
    roadmap_save.add_argument("--attempt-id", required=True)
    roadmap_save.add_argument("--expected-artifact-fingerprint", required=True)
    roadmap_save.add_argument("--expected-state", required=True)
    roadmap_save.add_argument("--idempotency-key", required=True)
    roadmap_save.set_defaults(command_handler=_roadmap_save)

    story = subparsers.add_parser("story", help="Inspect user stories.")
    story_sub = story.add_subparsers(
        dest="action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    story_show = story_sub.add_parser("show", help="Show one story.")
    story_show.add_argument("--story-id", type=int, required=True)
    story_show.set_defaults(command_handler=_story_show)
    story_pending = story_sub.add_parser(
        "pending",
        help="List roadmap requirements pending Story coverage.",
        description=(
            "List Roadmap requirements grouped by Story coverage state. Saved or "
            "merged Story drafts can be completed as a selected scope; pending "
            "requirements remain excluded from scoped Sprint planning until they "
            "are refined later."
        ),
        epilog=(
            "Examples:\n"
            "  agileforge story pending --project-id 1\n"
            "  agileforge story complete --project-id 1 --scope selection "
            "--parent-requirement <requirement> --expected-state STORY_PERSISTENCE "
            "--idempotency-key complete-story-selection-001"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    story_pending.add_argument("--project-id", type=int, required=True)
    story_pending.set_defaults(command_handler=_story_pending)
    story_generate = story_sub.add_parser(
        "generate",
        help="Generate or refine Story drafts.",
    )
    story_generate.add_argument("--project-id", type=int, required=True)
    story_generate.add_argument("--parent-requirement", required=True)
    story_generate.add_argument("--input", dest="user_input")
    story_generate.add_argument(
        "--force-feedback",
        action="store_true",
        help="Run Story generation even when feedback quality needs revision.",
    )
    story_generate.set_defaults(command_handler=_story_generate)
    story_retry = story_sub.add_parser(
        "retry",
        help="Retry the latest retryable Story request.",
    )
    story_retry.add_argument("--project-id", type=int, required=True)
    story_retry.add_argument("--parent-requirement", required=True)
    story_retry.set_defaults(command_handler=_story_retry)
    story_history = story_sub.add_parser(
        "history",
        help="Show Story attempt history.",
    )
    story_history.add_argument("--project-id", type=int, required=True)
    story_history.add_argument("--parent-requirement", required=True)
    story_history.set_defaults(command_handler=_story_history)
    story_save = story_sub.add_parser("save", help="Persist a reviewed Story draft.")
    story_save.add_argument("--project-id", type=int, required=True)
    story_save.add_argument("--parent-requirement", required=True)
    story_save.add_argument("--attempt-id", required=True)
    story_save.add_argument("--expected-artifact-fingerprint", required=True)
    story_save.add_argument("--expected-state", required=True)
    story_save.add_argument("--idempotency-key", required=True)
    story_save.set_defaults(command_handler=_story_save)
    story_complete = story_sub.add_parser(
        "complete",
        help="Complete the Story phase.",
        description=(
            "Complete Story work and move to Sprint setup. Without --scope, every "
            "Roadmap requirement must be saved or merged. With --scope milestone "
            "or --scope selection, only requirements in that planning scope are "
            "gated."
        ),
        epilog=(
            "Scoped completion example: --scope milestone --scope-id milestone_0. "
            "Scope ids come from agileforge story pending. Unknown scope ids fail "
            "without advancing state. Reusing the same idempotency key replays the "
            "prior result; a later different scope is not accepted after the "
            "workflow has already moved past STORY_PERSISTENCE."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    story_complete.add_argument("--project-id", type=int, required=True)
    story_complete.add_argument("--expected-state", required=True)
    story_complete.add_argument("--idempotency-key", required=True)
    story_complete.add_argument(
        "--scope",
        choices=["milestone", "selection"],
        help=(
            "Optionally complete only a planning scope. Supported values: "
            "milestone and selection."
        ),
    )
    story_complete.add_argument(
        "--scope-id",
        help=(
            "Scope identifier from story pending, for example milestone_0. "
            "Use with --scope milestone."
        ),
    )
    story_complete.add_argument(
        "--parent-requirement",
        action="append",
        help=(
            "Parent requirement to include when --scope selection is used. "
            "Repeat this flag for multiple saved requirements."
        ),
    )
    story_complete.set_defaults(command_handler=_story_complete)
    story_reopen = story_sub.add_parser(
        "reopen",
        help=(
            "Reopen a saved Story requirement for correction before Sprint work exists."
        ),
    )
    story_reopen.add_argument("--project-id", type=int, required=True)
    story_reopen.add_argument("--parent-requirement", required=True)
    story_reopen.add_argument("--expected-state", required=True)
    story_reopen.add_argument("--idempotency-key", required=True)
    story_reopen.set_defaults(command_handler=_story_reopen)
    story_repair = story_sub.add_parser(
        "repair-readiness",
        help="Backfill Story planning metadata before Sprint work starts.",
    )
    story_repair.add_argument("--project-id", type=int, required=True)
    story_repair.add_argument("--expected-state", required=True)
    story_repair.add_argument("--idempotency-key", required=True)
    story_repair.set_defaults(command_handler=_story_repair_readiness)
    story_dependencies = story_sub.add_parser(
        "dependencies",
        help="Inspect and review Story dependency edges.",
    )
    story_dependencies_sub = story_dependencies.add_subparsers(
        dest="dependency_action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    story_dependencies_inspect = story_dependencies_sub.add_parser(
        "inspect",
        help="Inspect active and proposed Story dependency edges.",
    )
    story_dependencies_inspect.add_argument("--project-id", type=int, required=True)
    story_dependencies_inspect.set_defaults(command_handler=_story_dependencies_inspect)
    story_dependencies_propose = story_dependencies_sub.add_parser(
        "propose",
        help="Create a reviewed Story dependency proposal artifact.",
    )
    story_dependencies_propose.add_argument("--project-id", type=int, required=True)
    story_dependencies_propose.add_argument("--expected-state", required=True)
    story_dependencies_propose.add_argument("--idempotency-key", required=True)
    story_dependencies_propose.add_argument(
        "--manual-edge",
        action="append",
        default=[],
        dest="manual_edges",
        help=(
            "Add reviewed edge in dependent_story_id:prerequisite_story_id form. "
            "Repeat for multiple edges."
        ),
    )
    story_dependencies_propose.set_defaults(command_handler=_story_dependencies_propose)
    story_dependencies_apply = story_dependencies_sub.add_parser(
        "apply",
        help="Apply a reviewed Story dependency proposal artifact.",
    )
    story_dependencies_apply.add_argument("--project-id", type=int, required=True)
    story_dependencies_apply.add_argument("--attempt-id", required=True)
    story_dependencies_apply.add_argument(
        "--expected-artifact-fingerprint",
        required=True,
    )
    story_dependencies_apply.add_argument("--expected-state", required=True)
    story_dependencies_apply.add_argument("--idempotency-key", required=True)
    story_dependencies_apply.set_defaults(command_handler=_story_dependencies_apply)

    sprint = subparsers.add_parser("sprint", help="Run Sprint phase commands.")
    sprint_sub = sprint.add_subparsers(
        dest="action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    sprint_candidates = sprint_sub.add_parser(
        "candidates",
        help="List sprint candidate stories.",
        description=(
            "List sprint candidate stories. If Story completed a selected scope, "
            "candidates are filtered to that saved Story scope and non-refined "
            "requirements are counted as excluded."
        ),
        epilog=(
            "Examples:\n"
            "  agileforge sprint candidates --project-id 1\n\n"
            "Readiness shows whether the selected scope can seed a Sprint. "
            "Excluded counts explain requirements left outside the current "
            "candidate pool."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sprint_candidates.add_argument("--project-id", type=int, required=True)
    sprint_candidates.set_defaults(command_handler=_sprint_candidates)
    sprint_generate = sprint_sub.add_parser(
        "generate",
        help="Generate or refine a Sprint draft.",
    )
    sprint_generate.add_argument("--project-id", type=int, required=True)
    sprint_generate.add_argument("--input", dest="user_input")
    sprint_generate.add_argument(
        "--selected-story-ids",
        type=_parse_selected_story_ids,
    )
    sprint_generate.add_argument(
        "--max-story-points",
        type=_parse_positive_story_points,
    )
    sprint_generate.add_argument(
        "--no-task-decomposition",
        action="store_false",
        dest="include_task_decomposition",
    )
    sprint_generate.set_defaults(
        command_handler=_sprint_generate,
        include_task_decomposition=True,
    )
    sprint_history = sprint_sub.add_parser(
        "history",
        help="Show Sprint planner attempts and execution history.",
    )
    sprint_history.add_argument("--project-id", type=int, required=True)
    sprint_history.set_defaults(command_handler=_sprint_history)
    sprint_metrics = sprint_sub.add_parser(
        "metrics",
        help="Show Sprint metrics and planning recommendation.",
    )
    sprint_metrics.add_argument("--project-id", type=int, required=True)
    sprint_metrics.set_defaults(command_handler=_sprint_metrics)
    sprint_save = sprint_sub.add_parser(
        "save",
        help="Persist a reviewed Sprint draft.",
    )
    sprint_save.add_argument("--project-id", type=int, required=True)
    sprint_save.add_argument("--team-name", required=True)
    sprint_save.add_argument("--attempt-id", required=True)
    sprint_save.add_argument("--expected-artifact-fingerprint", required=True)
    sprint_save.add_argument("--expected-state", required=True)
    sprint_save.add_argument("--idempotency-key", required=True)
    sprint_save.set_defaults(command_handler=_sprint_save)
    sprint_start = sprint_sub.add_parser(
        "start",
        help="Start a saved Sprint for execution.",
    )
    sprint_start.add_argument("--project-id", type=int, required=True)
    sprint_start.add_argument("--sprint-id", type=int)
    sprint_start.add_argument("--expected-state", required=True)
    sprint_start.add_argument("--idempotency-key", required=True)
    sprint_start.set_defaults(command_handler=_sprint_start)
    sprint_status = sprint_sub.add_parser(
        "status",
        help="Show Sprint execution status.",
        description=(
            "Show Sprint execution status. By default this shows the active or "
            "planned Sprint. Completed Sprints require --sprint-id because they "
            "are read-only history, not the current execution Sprint."
        ),
        epilog=(
            "Examples:\n"
            "  agileforge sprint status --project-id 1\n"
            "  agileforge sprint status --project-id 1 "
            "--sprint-id <completed_sprint_id>"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sprint_status.add_argument("--project-id", type=int, required=True)
    sprint_status.add_argument("--sprint-id", type=int)
    sprint_status.set_defaults(command_handler=_sprint_status)
    sprint_tasks = sprint_sub.add_parser(
        "tasks",
        help="List Sprint execution tasks.",
    )
    sprint_tasks.add_argument("--project-id", type=int, required=True)
    sprint_tasks.add_argument("--sprint-id", type=int)
    sprint_tasks.set_defaults(command_handler=_sprint_tasks)
    sprint_task = sprint_sub.add_parser(
        "task",
        help="Work active Sprint task tickets.",
    )
    sprint_task_sub = sprint_task.add_subparsers(
        dest="task_action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    sprint_task_next = sprint_task_sub.add_parser(
        "next",
        help="Return the next unblocked Sprint task ticket.",
    )
    sprint_task_next.add_argument("--project-id", type=int, required=True)
    sprint_task_next.add_argument("--sprint-id", type=int)
    sprint_task_next.set_defaults(command_handler=_sprint_task_next)
    sprint_task_show = sprint_task_sub.add_parser(
        "show",
        help="Show one Sprint task ticket.",
    )
    sprint_task_show.add_argument("--project-id", type=int, required=True)
    sprint_task_show.add_argument("--task-id", type=int, required=True)
    sprint_task_show.add_argument("--sprint-id", type=int)
    sprint_task_show.set_defaults(command_handler=_sprint_task_show)
    sprint_task_history = sprint_task_sub.add_parser(
        "history",
        help="Show one Sprint task execution history.",
    )
    sprint_task_history.add_argument("--project-id", type=int, required=True)
    sprint_task_history.add_argument("--task-id", type=int, required=True)
    sprint_task_history.add_argument("--sprint-id", type=int)
    sprint_task_history.set_defaults(command_handler=_sprint_task_history)
    sprint_task_update = sprint_task_sub.add_parser(
        "update",
        help="Log Sprint task execution progress.",
    )
    sprint_task_update.add_argument("--project-id", type=int, required=True)
    sprint_task_update.add_argument("--task-id", type=int, required=True)
    sprint_task_update.add_argument("--status", required=True)
    sprint_task_update.add_argument("--expected-status", required=True)
    sprint_task_update.add_argument("--expected-task-fingerprint", required=True)
    sprint_task_update.add_argument("--idempotency-key", required=True)
    sprint_task_update.add_argument("--sprint-id", type=int)
    sprint_task_update.add_argument("--outcome-summary")
    sprint_task_update.add_argument(
        "--artifact-ref",
        action="append",
        default=[],
        dest="artifact_refs",
    )
    sprint_task_update.add_argument(
        "--checklist-result",
        choices=("fully_met", "partially_met", "not_checked"),
    )
    sprint_task_update.add_argument("--validation-summary")
    sprint_task_update.add_argument("--notes")
    sprint_task_update.add_argument("--changed-by", default="cli-agent")
    sprint_task_update.set_defaults(command_handler=_sprint_task_update)
    sprint_close_readiness = sprint_sub.add_parser(
        "close-readiness",
        help="Return close readiness for the active Sprint.",
    )
    sprint_close_readiness.add_argument("--project-id", type=int, required=True)
    sprint_close_readiness.add_argument("--sprint-id", type=int)
    sprint_close_readiness.set_defaults(command_handler=_sprint_close_readiness)
    sprint_close = sprint_sub.add_parser(
        "close",
        help="Close an active Sprint after every story is done.",
    )
    sprint_close.add_argument("--project-id", type=int, required=True)
    sprint_close.add_argument("--expected-state", required=True)
    sprint_close.add_argument("--expected-status", required=True)
    sprint_close.add_argument("--expected-sprint-fingerprint", required=True)
    sprint_close.add_argument("--idempotency-key", required=True)
    sprint_close.add_argument("--completion-notes", required=True)
    sprint_close.add_argument("--follow-up-notes")
    sprint_close.add_argument("--sprint-id", type=int)
    sprint_close.add_argument("--changed-by", default="cli-agent")
    sprint_close.set_defaults(command_handler=_sprint_close)
    sprint_review = sprint_sub.add_parser(
        "review",
        help="Review completed Sprint learning before routing the next cycle.",
    )
    sprint_review.add_argument("--project-id", type=int, required=True)
    sprint_review.add_argument("--sprint-id", type=int)
    sprint_review.set_defaults(command_handler=_sprint_review)
    sprint_triage = sprint_sub.add_parser(
        "triage",
        help="Record post-sprint learning impact routing.",
    )
    sprint_triage.add_argument("--project-id", type=int, required=True)
    sprint_triage.add_argument("--sprint-id", type=int)
    sprint_triage.add_argument("--expected-state", required=True)
    sprint_triage.add_argument(
        "--impact",
        choices=("none", "task", "story", "roadmap", "backlog", "multiple"),
        required=True,
    )
    sprint_triage.add_argument("--affected-requirement", action="append", default=[])
    sprint_triage.add_argument(
        "--affected-task-id",
        action="append",
        type=int,
        default=[],
    )
    sprint_triage.add_argument(
        "--affected-story-id",
        action="append",
        type=int,
        default=[],
    )
    sprint_triage.add_argument(
        "--affected-backlog-item-id",
        action="append",
        default=[],
    )
    sprint_triage.add_argument(
        "--affected-roadmap-item-id",
        action="append",
        default=[],
    )
    sprint_triage.add_argument(
        "--affected-layer",
        action="append",
        choices=("task", "story", "roadmap", "backlog"),
        default=[],
    )
    sprint_triage.add_argument("--learning-summary", required=True)
    sprint_triage.add_argument("--decision-reason", required=True)
    sprint_triage.add_argument("--idempotency-key", required=True)
    sprint_triage.add_argument("--replace-existing", action="store_true")
    sprint_triage.add_argument("--expected-triage-fingerprint")
    sprint_triage.add_argument("--changed-by", default="cli-agent")
    sprint_triage.set_defaults(command_handler=_sprint_triage)
    sprint_story = sprint_sub.add_parser(
        "story",
        help="Inspect and close active Sprint stories.",
    )
    sprint_story_sub = sprint_story.add_subparsers(
        dest="story_action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    sprint_story_readiness = sprint_story_sub.add_parser(
        "readiness",
        help="Return close readiness for one Sprint story.",
    )
    sprint_story_readiness.add_argument("--project-id", type=int, required=True)
    sprint_story_readiness.add_argument("--story-id", type=int, required=True)
    sprint_story_readiness.add_argument("--sprint-id", type=int)
    sprint_story_readiness.set_defaults(command_handler=_sprint_story_readiness)
    sprint_story_close = sprint_story_sub.add_parser(
        "close",
        help="Close one Sprint story after its tasks are complete.",
    )
    sprint_story_close.add_argument("--project-id", type=int, required=True)
    sprint_story_close.add_argument("--story-id", type=int, required=True)
    sprint_story_close.add_argument("--expected-status", required=True)
    sprint_story_close.add_argument("--expected-story-fingerprint", required=True)
    sprint_story_close.add_argument("--idempotency-key", required=True)
    sprint_story_close.add_argument(
        "--resolution",
        choices=tuple(resolution.value for resolution in StoryResolution),
        required=True,
    )
    sprint_story_close.add_argument("--completion-notes", required=True)
    sprint_story_close.add_argument(
        "--evidence-link",
        action="append",
        default=[],
        dest="evidence_links",
    )
    sprint_story_close.add_argument("--sprint-id", type=int)
    sprint_story_close.add_argument("--changed-by", default="cli-agent")
    sprint_story_close.set_defaults(command_handler=_sprint_story_close)

    context = subparsers.add_parser("context", help="Build bounded agent context.")
    context_sub = context.add_subparsers(
        dest="action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    context_pack = context_sub.add_parser("pack", help="Build a context pack.")
    context_pack.add_argument("--project-id", type=int, required=True)
    context_pack.add_argument("--phase", default=DEFAULT_CONTEXT_PHASE)
    context_pack.set_defaults(command_handler=_context_pack)

    status = subparsers.add_parser("status", help="Show project orientation status.")
    status.add_argument("--project-id", type=int, required=True)
    status.set_defaults(command_handler=_status)

    doctor = subparsers.add_parser("doctor", help="Run CLI diagnostics.")
    doctor.set_defaults(command_handler=_doctor)

    capabilities = subparsers.add_parser(
        "capabilities",
        help="Show installed command capabilities.",
    )
    capabilities.set_defaults(command_handler=_capabilities)

    schema = subparsers.add_parser("schema", help="Inspect CLI schemas.")
    schema_sub = schema.add_subparsers(
        dest="action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    schema_check = schema_sub.add_parser("check", help="Check storage schema.")
    schema_check.set_defaults(command_handler=_schema_check)

    spec = subparsers.add_parser("spec", help="Inspect AgileForge spec artifacts.")
    spec_sub = spec.add_subparsers(
        dest="action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    spec_profile = spec_sub.add_parser("profile", help="Inspect spec profile data.")
    spec_profile_sub = spec_profile.add_subparsers(
        dest="profile_action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    spec_profile_schema = spec_profile_sub.add_parser(
        "schema",
        help="Export the AgileForge spec profile JSON Schema.",
    )
    spec_profile_schema.set_defaults(command_handler=_spec_profile_schema)
    spec_profile_validate = spec_profile_sub.add_parser(
        "validate",
        help="Validate an AgileForge spec profile JSON file.",
    )
    spec_profile_validate.add_argument("--spec-file", required=True)
    spec_profile_validate.add_argument("--render-md")
    spec_profile_validate.set_defaults(command_handler=_spec_profile_validate)

    command = subparsers.add_parser("command", help="Inspect command contracts.")
    command_sub = command.add_subparsers(
        dest="action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    command_schema = command_sub.add_parser("schema", help="Show command schema.")
    command_schema.add_argument("command_name")
    command_schema.set_defaults(command_handler=_command_schema)

    mutation = subparsers.add_parser("mutation", help="Inspect mutation ledger.")
    mutation_sub = mutation.add_subparsers(
        dest="action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    mutation_show = mutation_sub.add_parser("show", help="Show one mutation event.")
    mutation_show.add_argument("--mutation-event-id", type=int, required=True)
    mutation_show.set_defaults(command_handler=_mutation_show)
    mutation_list = mutation_sub.add_parser("list", help="List mutation events.")
    mutation_list.add_argument("--project-id", type=int)
    mutation_list.add_argument("--status")
    mutation_list.set_defaults(command_handler=_mutation_list)
    mutation_resume = mutation_sub.add_parser(
        "resume",
        help="Resume a recovery-required mutation event.",
    )
    mutation_resume.add_argument("--mutation-event-id", type=int, required=True)
    mutation_resume.add_argument("--correlation-id")
    mutation_resume.set_defaults(command_handler=_mutation_resume)
    return parser


def _project_list(
    _args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route project list to the application facade."""
    return "agileforge project list", application.project_list()


def _project_show(args: argparse.Namespace, application: _Application) -> CommandResult:
    """Route project show to the application facade."""
    return "agileforge project show", application.project_show(
        project_id=args.project_id
    )


def _mutation_arg_error(command: str, error: WorkbenchError) -> CommandResult:
    """Return a command result for mutation argument validation failures."""
    return command, {
        "ok": False,
        "data": None,
        "warnings": [],
        "errors": [error.to_dict()],
    }


def _validate_mutation_idempotency_args(
    args: argparse.Namespace,
) -> WorkbenchError | None:
    """Validate dry-run/idempotency flag combinations for mutations."""
    if args.dry_run and args.idempotency_key:
        return WorkbenchError(
            code="INVALID_COMMAND",
            message="--idempotency-key is not allowed with --dry-run.",
            details={"idempotency_key": args.idempotency_key},
            remediation=["Use --dry-run-id for dry-run tracing."],
            exit_code=INVALID_COMMAND_EXIT_CODE,
            retryable=False,
        )
    if not args.dry_run and not args.idempotency_key:
        args.idempotency_key = f"auto-{uuid4()}"
    return None


def _project_create(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route project create to the application facade."""
    command = "agileforge project create"
    validation_error = _validate_mutation_idempotency_args(args)
    if validation_error is not None:
        return _mutation_arg_error(command, validation_error)
    return command, application.project_create(
        name=args.name,
        spec_file=args.spec_file,
        setup_mode=args.setup_mode,
        idempotency_key=args.idempotency_key,
        dry_run=args.dry_run,
        dry_run_id=args.dry_run_id,
        correlation_id=args.correlation_id,
        changed_by=args.changed_by,
    )


def _project_setup_retry(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route project setup retry to the application facade."""
    command = "agileforge project setup retry"
    validation_error = _validate_mutation_idempotency_args(args)
    if validation_error is not None:
        return _mutation_arg_error(command, validation_error)
    return command, application.project_setup_retry(
        project_id=args.project_id,
        spec_file=args.spec_file,
        setup_mode=args.setup_mode,
        expected_state=args.expected_state,
        expected_context_fingerprint=args.expected_context_fingerprint,
        recovery_mutation_event_id=args.recovery_mutation_event_id,
        idempotency_key=args.idempotency_key,
        dry_run=args.dry_run,
        dry_run_id=args.dry_run_id,
        correlation_id=args.correlation_id,
        changed_by=args.changed_by,
    )


def _scope_extension_validate(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route scope extension validation to the application facade."""
    return "agileforge scope extension validate", application.scope_extension_validate(
        project_id=args.project_id,
        spec_file=args.spec_file,
        base_spec_version_id=args.base_spec_version_id,
    )


def _scope_extension_start(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route guarded scope extension start to the application facade."""
    command = "agileforge scope extension start"
    if not str(args.idempotency_key).strip():
        return _invalid_command(
            command,
            "Scope extension start requires a non-blank idempotency key.",
            details={"blank": ["idempotency_key"]},
            remediation=["Pass a non-blank --idempotency-key value."],
        )
    return command, application.scope_extension_start(
        project_id=args.project_id,
        spec_file=args.spec_file,
        base_spec_version_id=args.base_spec_version_id,
        expected_state=args.expected_state,
        idempotency_key=args.idempotency_key.strip(),
        changed_by=args.changed_by,
    )


def _authority_compile(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route authority compile to the application facade."""
    command = "agileforge authority compile"
    validation_error = _validate_mutation_idempotency_args(args)
    if validation_error is not None:
        return _mutation_arg_error(command, validation_error)
    return command, application.authority_compile(
        project_id=args.project_id,
        spec_version_id=args.spec_version_id,
        expected_spec_hash=args.expected_spec_hash,
        expected_state=args.expected_state,
        expected_setup_status=args.expected_setup_status,
        compiler_model=args.compiler_model,
        idempotency_key=args.idempotency_key,
        dry_run=args.dry_run,
        dry_run_id=args.dry_run_id,
        correlation_id=args.correlation_id,
        changed_by=args.changed_by,
    )


def _workflow_state(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route workflow state to the application facade."""
    return "agileforge workflow state", application.workflow_state(
        project_id=args.project_id
    )


def _workflow_next(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route workflow next to the application facade."""
    return "agileforge workflow next", application.workflow_next(
        project_id=args.project_id
    )


def _authority_status(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route authority status to the application facade."""
    return "agileforge authority status", application.authority_status(
        project_id=args.project_id
    )


def _authority_invariants(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route authority invariants to the application facade."""
    return "agileforge authority invariants", application.authority_invariants(
        project_id=args.project_id,
        spec_version_id=args.spec_version_id,
    )


def _authority_review(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route authority review to the application facade."""
    return "agileforge authority review", application.authority_review(
        project_id=args.project_id,
        include_spec=args.include_spec,
        output_format=args.format,
    )


def _authority_regenerate(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route authority regenerate to the application facade."""
    return "agileforge authority regenerate", application.authority_regenerate(
        project_id=args.project_id,
        spec_version_id=args.spec_version_id,
        compiler_model=args.compiler_model,
        idempotency_key=args.idempotency_key,
        changed_by=args.changed_by,
        dry_run=args.dry_run,
    )


def _authority_feedback_record(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route authority feedback recording to the application facade."""
    return (
        "agileforge authority feedback record",
        application.authority_feedback_record(
            project_id=args.project_id,
            pending_authority_id=args.pending_authority_id,
            expected_authority_fingerprint=args.expected_authority_fingerprint,
            feedback_file=args.feedback_file,
            idempotency_key=args.idempotency_key,
            changed_by=args.changed_by,
            correlation_id=args.correlation_id,
        ),
    )


def _authority_curate(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route authority curation to the application facade."""
    return (
        "agileforge authority curate",
        application.authority_curate(
            project_id=args.project_id,
            spec_version_id=args.spec_version_id,
            source_authority_id=args.source_authority_id,
            expected_source_authority_fingerprint=(
                args.expected_source_authority_fingerprint
            ),
            feedback_attempt_id=args.feedback_attempt_id,
            max_iterations=args.max_iterations,
            compiler_model=args.compiler_model,
            idempotency_key=args.idempotency_key,
            changed_by=args.changed_by,
            correlation_id=args.correlation_id,
        ),
    )


def _invalid_command(
    command: str,
    message: str,
    *,
    details: dict[str, object] | None = None,
    remediation: list[str] | None = None,
) -> CommandResult:
    """Return a structured invalid-command result."""
    return _mutation_arg_error(
        command,
        WorkbenchError(
            code=ErrorCode.INVALID_COMMAND.value,
            message=message,
            details=details or {},
            remediation=remediation or ["Run agileforge authority --help."],
            exit_code=INVALID_COMMAND_EXIT_CODE,
            retryable=False,
        ),
    )


def _authority_review_required(command: str) -> CommandResult:
    """Return a missing-review-token result for non-interactive decisions."""
    return _mutation_arg_error(
        command,
        workbench_error(
            ErrorCode.AUTHORITY_REVIEW_REQUIRED,
            message="Run authority review first and pass --review-token.",
            remediation=[
                "Run agileforge authority review --project-id <id>.",
                "Pass --review-token, or run from a TTY for interactive review.",
            ],
        ),
    )


def _has_explicit_authority_args(args: argparse.Namespace) -> bool:
    """Return whether explicit decision mode appears to be requested."""
    return bool(args.idempotency_key) or any(
        getattr(args, field_name) is not None
        for field_name in AUTHORITY_ALL_GUARD_FIELDS
    )


def _has_explicit_authority_guard_args(args: argparse.Namespace) -> bool:
    """Return whether explicit authority guard fields were passed."""
    return any(
        getattr(args, field_name) is not None
        for field_name in AUTHORITY_ALL_GUARD_FIELDS
    )


def _missing_authority_guards(
    args: argparse.Namespace,
    *,
    require_completeness: bool,
) -> list[str]:
    """Return required explicit guard fields missing from parsed args."""
    fields = list(AUTHORITY_EXPLICIT_GUARD_FIELDS)
    if require_completeness:
        fields.extend(AUTHORITY_COMPLETENESS_GUARD_FIELDS)
    return [field_name for field_name in fields if getattr(args, field_name) is None]


def _authority_actor_mode(changed_by: str | None, *, token_mode: bool) -> str:
    """Return the actor mode implied by CLI decision input."""
    if not token_mode:
        return "cli-agent"
    if changed_by is None:
        return "cli-human"
    normalized = changed_by.lower()
    if "agent" in normalized or "bot" in normalized or "automation" in normalized:
        return "cli-agent"
    return "cli-human"


def _decision_idempotency_key(args: argparse.Namespace) -> str | None:
    """Return an explicit or generated idempotency key for token mode."""
    if args.idempotency_key:
        return cast("str", args.idempotency_key)
    if args.review_token:
        return f"human-token:{uuid4()}"
    return None


def _auto_authority_idempotency_key(
    *,
    action: str,
    project_id: int,
    review_token: str,
) -> str:
    """Return a deterministic idempotency key for simple authority decisions."""
    digest = hashlib.sha256(review_token.encode("utf-8")).hexdigest()[:16]
    return f"authority-{action}-{project_id}-{digest}"


def _authority_request_kwargs(args: argparse.Namespace) -> _AuthorityRequestKwargs:
    """Return request keyword args common to accept/reject decisions."""
    return {
        "project_id": cast("int", args.project_id),
        "review_token": cast("str | None", args.review_token),
        "pending_authority_id": cast("int | None", args.pending_authority_id),
        "expected_authority_fingerprint": cast(
            "str | None",
            args.expected_authority_fingerprint,
        ),
        "expected_source_spec_hash": cast(
            "str | None",
            args.expected_source_spec_hash,
        ),
        "expected_disk_spec_hash": cast(
            "str | None",
            args.expected_disk_spec_hash,
        ),
        "expected_resolved_spec_path": cast(
            "str | None",
            args.expected_resolved_spec_path,
        ),
        "expected_state": cast("str | None", args.expected_state),
        "expected_setup_status": cast("str | None", args.expected_setup_status),
        "expected_content_included": cast(
            "bool | None",
            args.expected_content_included,
        ),
        "expected_omission_assessment": cast(
            "str | None",
            args.expected_omission_assessment,
        ),
        "expected_coverage_summary_fingerprint": cast(
            "str | None",
            args.expected_coverage_summary_fingerprint,
        ),
        "idempotency_key": _decision_idempotency_key(args),
        "changed_by": cast("str | None", args.changed_by),
        "actor_mode": _authority_actor_mode(
            cast("str | None", args.changed_by),
            token_mode=bool(cast("str | None", args.review_token)),
        ),
    }


def _authority_validation_failure(
    command: str,
    exc: ValidationError | ValueError,
) -> CommandResult:
    """Return a structured invalid-command result for request model errors."""
    return _invalid_command(
        command,
        "Invalid authority decision arguments.",
        details={"validation_error": str(exc)},
    )


def _validate_incomplete_override(args: argparse.Namespace) -> CommandResult | None:
    """Validate incomplete review override arguments."""
    overrides = cast("list[str]", args.incomplete_review_override or [])
    if not overrides:
        return None
    try:
        _parse_incomplete_review_overrides(overrides)
    except ValueError as exc:
        return _invalid_command(
            "agileforge authority accept",
            str(exc),
            details={"field": "incomplete_review_override"},
        )
    return None


def _parse_incomplete_review_overrides(
    raw_overrides: list[str],
) -> list[IncompleteReviewOverride]:
    """Parse repeated candidate-scoped incomplete review override flags."""
    from services.agent_workbench.authority_decision import (  # noqa: PLC0415
        IncompleteReviewOverride,
    )

    parsed: list[IncompleteReviewOverride] = []
    for raw in raw_overrides:
        parts = raw.split(":", 2)
        if len(parts) != INCOMPLETE_REVIEW_OVERRIDE_PARTS or not all(
            part.strip() for part in parts
        ):
            msg = (
                "--incomplete-review-override must be "
                "<candidate_id>:<finding_code>:<rationale>."
            )
            raise ValueError(msg)
        candidate_id, finding_code, rationale = (part.strip() for part in parts)
        parsed.append(
            IncompleteReviewOverride(
                candidate_id=candidate_id,
                finding_code=finding_code,
                rationale=rationale,
            )
        )
    return parsed


def _validate_authority_explicit_args(
    args: argparse.Namespace,
    *,
    command: str,
    require_completeness: bool,
) -> CommandResult | None:
    """Validate explicit authority decision mode arguments."""
    missing = _missing_authority_guards(
        args,
        require_completeness=require_completeness,
    )
    if missing:
        return _invalid_command(
            command,
            "Explicit authority decision mode requires guard fields.",
            details={"missing": missing},
            remediation=["Pass --review-token or every required explicit guard."],
        )
    if not args.idempotency_key:
        return _invalid_command(
            command,
            "Explicit authority decision mode requires --idempotency-key.",
            details={"missing": ["idempotency_key"]},
        )
    return None


def _review_token_from_latest_review(result: JsonObject) -> str | None:
    """Return the latest review token from current or legacy review packets."""
    data = _review_data(result)
    if data is None:
        return None
    guards = _as_mapping(data.get("guard_tokens"))
    review_token = guards.get("review_token") if guards is not None else None
    if isinstance(review_token, str) and review_token.strip():
        return review_token
    legacy_token = data.get("review_token")
    if isinstance(legacy_token, str) and legacy_token.strip():
        return legacy_token
    return None


def _latest_authority_review_token(
    *,
    command: str,
    project_id: int,
    application: _Application,
) -> str | CommandResult:
    """Fetch the latest accept-ready authority review token for simple accept."""
    review = application.authority_review(
        project_id=project_id,
        include_spec="auto",
        output_format="json",
    )
    if review.get("ok") is not True:
        return command, review
    review_token = _review_token_from_latest_review(review)
    if review_token is None:
        return _authority_review_required(command)
    data = _review_data(review) or {}
    review_summary = _as_mapping(data.get("review_summary"))
    if (
        review_summary is not None
        and review_summary.get("acceptance_status") == "blocked"
    ):
        return _mutation_arg_error(
            command,
            workbench_error(
                ErrorCode.AUTHORITY_REVIEW_INCOMPLETE,
                message="Latest authority review has blocking findings.",
                details={
                    "review_summary": {
                        str(key): value for key, value in review_summary.items()
                    }
                },
                remediation=[
                    "Resolve fatal authority review findings and run authority "
                    "review again."
                ],
            ),
        )
    return review_token


def _args_with_latest_review_token(
    args: argparse.Namespace,
    *,
    review_token: str,
) -> argparse.Namespace:
    """Return accept args populated with a fetched review token."""
    values = vars(args).copy()
    values["review_token"] = review_token
    values["idempotency_key"] = args.idempotency_key or _auto_authority_idempotency_key(
        action="accept",
        project_id=cast("int", args.project_id),
        review_token=review_token,
    )
    return argparse.Namespace(**values)


def _authority_accept(  # noqa: PLR0911
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route authority accept to the application facade."""
    from services.agent_workbench.authority_decision import (  # noqa: PLC0415
        AuthorityAcceptRequest,
    )

    command = "agileforge authority accept"
    validation_error = _validate_incomplete_override(args)
    if validation_error is not None:
        return validation_error

    if args.review_token:
        try:
            request = AuthorityAcceptRequest(
                **_authority_request_kwargs(args),
                allow_incomplete_review=args.allow_incomplete_review,
                incomplete_review_rationale=args.incomplete_review_rationale,
                incomplete_review_overrides=_parse_incomplete_review_overrides(
                    cast("list[str]", args.incomplete_review_override or [])
                ),
            )
        except (ValidationError, ValueError) as exc:
            return _authority_validation_failure(command, exc)
        return command, application.authority_accept(request)

    if _has_explicit_authority_guard_args(args):
        validation_error = _validate_authority_explicit_args(
            args,
            command=command,
            require_completeness=True,
        )
        if validation_error is not None:
            return validation_error
        try:
            request = AuthorityAcceptRequest(
                **_authority_request_kwargs(args),
                allow_incomplete_review=args.allow_incomplete_review,
                incomplete_review_rationale=args.incomplete_review_rationale,
                incomplete_review_overrides=_parse_incomplete_review_overrides(
                    cast("list[str]", args.incomplete_review_override or [])
                ),
            )
        except (ValidationError, ValueError) as exc:
            return _authority_validation_failure(command, exc)
        return command, application.authority_accept(request)

    latest_token = _latest_authority_review_token(
        command=command,
        project_id=cast("int", args.project_id),
        application=application,
    )
    if not isinstance(latest_token, str):
        return latest_token
    token_args = _args_with_latest_review_token(args, review_token=latest_token)
    try:
        request = AuthorityAcceptRequest(
            **_authority_request_kwargs(token_args),
            allow_incomplete_review=token_args.allow_incomplete_review,
            incomplete_review_rationale=token_args.incomplete_review_rationale,
            incomplete_review_overrides=_parse_incomplete_review_overrides(
                cast("list[str]", token_args.incomplete_review_override or [])
            ),
        )
    except (ValidationError, ValueError) as exc:
        return _authority_validation_failure(command, exc)
    return command, application.authority_accept(request)


def _authority_reject(  # noqa: PLR0911
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route authority reject to the application facade."""
    from services.agent_workbench.authority_decision import (  # noqa: PLC0415
        AuthorityRejectRequest,
    )

    command = "agileforge authority reject"
    if args.review_token:
        if not _non_empty(args.reason):
            return _invalid_command(
                command,
                "--reason is required for authority reject.",
                details={"missing": ["reason"]},
            )
        if not args.idempotency_key:
            return _invalid_command(
                command,
                "Authority reject requires --idempotency-key.",
                details={"missing": ["idempotency_key"]},
            )
        try:
            request = AuthorityRejectRequest(
                **_authority_request_kwargs(args),
                reason=args.reason,
            )
        except (ValidationError, ValueError) as exc:
            return _authority_validation_failure(command, exc)
        return command, application.authority_reject(request)

    if _has_explicit_authority_args(args):
        if not _non_empty(args.reason):
            return _invalid_command(
                command,
                "--reason is required for authority reject.",
                details={"missing": ["reason"]},
            )
        validation_error = _validate_authority_explicit_args(
            args,
            command=command,
            require_completeness=False,
        )
        if validation_error is not None:
            return validation_error
        try:
            request = AuthorityRejectRequest(
                **_authority_request_kwargs(args),
                reason=args.reason,
            )
        except (ValidationError, ValueError) as exc:
            return _authority_validation_failure(command, exc)
        return command, application.authority_reject(request)

    if not sys.stdin.isatty():
        return _authority_review_required(command)

    return _interactive_authority_reject(args, application)


def _non_empty(value: object) -> bool:
    """Return whether a CLI string value is non-empty after trimming."""
    return isinstance(value, str) and bool(value.strip())


def _parse_selected_story_ids(value: str) -> list[int]:
    """Parse comma-separated story IDs for Sprint generation."""
    ids: list[int] = []
    seen: set[int] = set()
    for raw_part in value.split(","):
        part = raw_part.strip()
        if not part:
            continue
        try:
            story_id = int(part)
        except ValueError as exc:
            message = "--selected-story-ids must be a comma-separated list of integers."
            raise argparse.ArgumentTypeError(message) from exc
        if story_id <= 0:
            message = "--selected-story-ids values must be positive integers."
            raise argparse.ArgumentTypeError(message)
        if story_id not in seen:
            seen.add(story_id)
            ids.append(story_id)
    if not ids:
        message = "--selected-story-ids must include at least one story ID."
        raise argparse.ArgumentTypeError(message)
    return ids


def _parse_positive_story_points(value: str) -> int:
    """Parse a positive story-point capacity value."""
    try:
        points = int(value)
    except ValueError as exc:
        message = "--max-story-points must be a positive integer."
        raise argparse.ArgumentTypeError(message) from exc
    if points <= 0:
        message = "--max-story-points must be a positive integer."
        raise argparse.ArgumentTypeError(message)
    return points


def _review_data(result: JsonObject) -> Mapping[object, object] | None:
    """Return the data mapping from a review result."""
    return _as_mapping(result.get("data"))


def _guard_tokens_from_review(result: JsonObject) -> Mapping[object, object] | None:
    """Return guard tokens from a review result."""
    data = _review_data(result)
    if data is None:
        return None
    return _as_mapping(data.get("guard_tokens"))


def _print_authority_review_summary(result: JsonObject) -> None:
    """Print a compact review summary for interactive decisions."""
    data = _review_data(result) or {}
    project = _as_mapping(data.get("project")) or {}
    spec = _as_mapping(data.get("spec")) or {}
    pending = _as_mapping(data.get("pending_authority")) or {}
    guards = _as_mapping(data.get("guard_tokens")) or {}
    sys.stderr.write(
        "\n".join(
            [
                "Authority review",
                f"  project_id: {project.get('project_id', '')}",
                f"  authority_id: {pending.get('authority_id', '')}",
                f"  spec_path: {spec.get('resolved_path', '')}",
                (
                    "  omission_assessment: "
                    f"{guards.get('expected_omission_assessment', '')}"
                ),
            ]
        )
        + "\n"
    )


def _args_from_review_token(
    args: argparse.Namespace,
    *,
    review_token: str,
    incomplete_review_rationale: str | None = None,
) -> argparse.Namespace:
    """Return a decision namespace populated for token-mode submission."""
    values = vars(args).copy()
    values["review_token"] = review_token
    values["idempotency_key"] = f"human-token:{uuid4()}"
    values["changed_by"] = args.changed_by
    if incomplete_review_rationale is not None:
        values["allow_incomplete_review"] = True
        values["incomplete_review_rationale"] = incomplete_review_rationale
    return argparse.Namespace(**values)


def _interactive_authority_accept(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Review and confirm authority acceptance in a TTY session."""
    from services.agent_workbench.authority_decision import (  # noqa: PLC0415
        AuthorityAcceptRequest,
    )

    command = "agileforge authority accept"
    review = application.authority_review(
        project_id=args.project_id,
        include_spec="auto",
        output_format="json",
    )
    if review.get("ok") is not True:
        return command, review
    guards = _guard_tokens_from_review(review)
    review_token = guards.get("review_token") if guards is not None else None
    if not isinstance(review_token, str):
        return _authority_review_required(command)
    guards = cast("Mapping[object, object]", guards)
    _print_authority_review_summary(review)
    omission = guards.get("expected_omission_assessment")
    phrase = (
        "ACCEPT AUTHORITY" if omission == "complete" else "ACCEPT INCOMPLETE AUTHORITY"
    )
    typed = _input_from_stderr(f'Type "{phrase}" to continue: ')
    if typed != phrase:
        return _invalid_command(
            command,
            "Authority acceptance confirmation did not match.",
            details={"required_phrase": phrase},
        )
    if omission != "complete":
        validation_error = _validate_incomplete_override(args)
        if validation_error is not None:
            return validation_error
    token_args = _args_from_review_token(
        args,
        review_token=review_token,
    )
    try:
        request = AuthorityAcceptRequest(
            **_authority_request_kwargs(token_args),
            allow_incomplete_review=token_args.allow_incomplete_review,
            incomplete_review_rationale=token_args.incomplete_review_rationale,
            incomplete_review_overrides=_parse_incomplete_review_overrides(
                cast("list[str]", token_args.incomplete_review_override or [])
            ),
        )
    except (ValidationError, ValueError) as exc:
        return _authority_validation_failure(command, exc)
    return command, application.authority_accept(request)


def _interactive_authority_reject(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Review and confirm authority rejection in a TTY session."""
    from services.agent_workbench.authority_decision import (  # noqa: PLC0415
        AuthorityRejectRequest,
    )

    command = "agileforge authority reject"
    review = application.authority_review(
        project_id=args.project_id,
        include_spec="auto",
        output_format="json",
    )
    if review.get("ok") is not True:
        return command, review
    guards = _guard_tokens_from_review(review)
    review_token = guards.get("review_token") if guards is not None else None
    if not isinstance(review_token, str):
        return _authority_review_required(command)
    _print_authority_review_summary(review)
    reason = _input_from_stderr("Rejection reason: ").strip()
    if not reason:
        return _invalid_command(
            command,
            "Authority rejection requires a reason.",
            details={"missing": ["reason"]},
        )
    token_args = _args_from_review_token(args, review_token=review_token)
    try:
        request = AuthorityRejectRequest(
            **_authority_request_kwargs(token_args),
            reason=reason,
        )
    except (ValidationError, ValueError) as exc:
        return _authority_validation_failure(command, exc)
    return command, application.authority_reject(request)


def _input_from_stderr(prompt: str) -> str:
    """Prompt interactive CLI users on stderr so stdout remains machine-owned."""
    sys.stderr.write(prompt)
    sys.stderr.flush()
    return input()


def _vision_generate(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Vision generate to the application facade."""
    return "agileforge vision generate", application.vision_generate(
        project_id=args.project_id,
        user_input=args.user_input,
    )


def _vision_history(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Vision history to the application facade."""
    return "agileforge vision history", application.vision_history(
        project_id=args.project_id,
    )


def _vision_save(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Vision save to the application facade."""
    return "agileforge vision save", application.vision_save(
        project_id=args.project_id,
    )


def _backlog_generate(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Backlog generate to the application facade."""
    return "agileforge backlog generate", application.backlog_generate(
        project_id=args.project_id,
        user_input=args.user_input,
    )


def _backlog_preview(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Backlog preview to the application facade."""
    return "agileforge backlog preview", application.backlog_preview(
        project_id=args.project_id,
        user_input=args.user_input,
    )


def _backlog_refine_preview(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Backlog refinement preview to the application facade."""
    return "agileforge backlog refine-preview", application.backlog_refine_preview(
        project_id=args.project_id,
        source_attempt_id=args.source_attempt_id,
        operations_file=args.operations_file,
        source_artifact=args.source_artifact,
        user_input=args.user_input,
    )


def _backlog_refine_record(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Backlog refinement record to the application facade."""
    return "agileforge backlog refine-record", application.backlog_refine_record(
        project_id=args.project_id,
        source_attempt_id=args.source_attempt_id,
        operations_file=args.operations_file,
        expected_source_fingerprint=args.expected_source_fingerprint,
        expected_state=args.expected_state,
        idempotency_key=args.idempotency_key,
        approval_id=args.approval_id,
    )


def _backlog_approve(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Backlog refinement approval to the application facade."""
    return "agileforge backlog approve", application.backlog_approve(
        project_id=args.project_id,
        source_attempt_id=args.source_attempt_id,
        attempt_id=args.attempt_id,
        operation_set_fingerprint=args.operation_set_fingerprint,
        approved_artifact_fingerprint=args.approved_artifact_fingerprint,
        approved_operation_ids=args.approved_operation_ids,
        idempotency_key=args.idempotency_key,
    )


def _backlog_refine_import(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Backlog refinement import to the application facade."""
    return "agileforge backlog refine-import", application.backlog_refine_import(
        project_id=args.project_id,
        source_artifact=args.source_artifact,
        edited_file=args.edited_file,
        expected_source_fingerprint=args.expected_source_fingerprint,
        idempotency_key=args.idempotency_key,
    )


def _backlog_history(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Backlog history to the application facade."""
    return "agileforge backlog history", application.backlog_history(
        project_id=args.project_id,
    )


def _backlog_save(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Backlog save to the application facade."""
    return "agileforge backlog save", application.backlog_save(
        project_id=args.project_id,
        attempt_id=args.attempt_id,
        expected_artifact_fingerprint=args.expected_artifact_fingerprint,
        expected_state=args.expected_state,
        idempotency_key=args.idempotency_key,
    )


def _backlog_reset_active(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Backlog reset-active to the application facade."""
    return "agileforge backlog reset-active", application.backlog_reset_active(
        project_id=args.project_id,
        attempt_id=args.attempt_id,
        expected_artifact_fingerprint=args.expected_artifact_fingerprint,
        expected_state=args.expected_state,
        reset_reason=args.reset_reason,
        archive_all_active_stories=args.archive_all_active_stories,
        idempotency_key=args.idempotency_key,
    )


def _backlog_reconcile(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Backlog reconcile to the application facade."""
    return "agileforge backlog reconcile", application.backlog_reconcile(
        project_id=args.project_id,
        idempotency_key=args.idempotency_key,
    )


def _evidence_collect(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route evidence collection to the application facade."""
    return "agileforge evidence collect", application.evidence_collect(
        project_id=args.project_id,
        repo_path=args.repo_path,
        from_file=args.from_file,
        idempotency_key=args.idempotency_key,
        include_generated_artifacts=args.include_generated_artifacts,
    )


def _as_built_assess(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route As-Built Assessment to the application facade."""
    return "agileforge as-built assess", application.as_built_assess(
        project_id=args.project_id,
        repo_path=args.repo_path,
        spec_file=args.spec_file,
        spec_mode=args.spec_mode,
        user_input=args.user_input,
        idempotency_key=args.idempotency_key,
    )


def _brownfield_source_import(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route brownfield source import to the application facade."""
    return "agileforge brownfield source import", application.brownfield_source_import(
        project_id=args.project_id,
        source_file=args.source_file,
        source_kind=args.source_kind,
        idempotency_key=args.idempotency_key,
        correlation_id=args.correlation_id,
        changed_by=args.changed_by,
    )


def _brownfield_scan(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route brownfield scan to the application facade."""
    return "agileforge brownfield scan", application.brownfield_scan(
        project_id=args.project_id,
        repo_path=args.repo_path,
        source_attempt_id=args.source_attempt_id,
        idempotency_key=args.idempotency_key,
        correlation_id=args.correlation_id,
        changed_by=args.changed_by,
    )


def _brownfield_spec_draft(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route brownfield spec draft to the application facade."""
    return "agileforge brownfield spec draft", application.brownfield_spec_draft(
        project_id=args.project_id,
        scan_attempt_id=args.scan_attempt_id,
        user_input=args.user_input,
        idempotency_key=args.idempotency_key,
        correlation_id=args.correlation_id,
        changed_by=args.changed_by,
    )


def _brownfield_spec_import(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route brownfield spec import to the application facade."""
    return "agileforge brownfield spec import", application.brownfield_spec_import(
        project_id=args.project_id,
        curated_spec_file=args.curated_spec_file,
        expected_scan_fingerprint=args.expected_scan_fingerprint,
        parent_draft_attempt_id=args.parent_draft_attempt_id,
        idempotency_key=args.idempotency_key,
        correlation_id=args.correlation_id,
        changed_by=args.changed_by,
    )


def _brownfield_spec_approve(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route brownfield spec approve to the application facade."""
    return "agileforge brownfield spec approve", application.brownfield_spec_approve(
        project_id=args.project_id,
        attempt_id=args.attempt_id,
        expected_artifact_fingerprint=args.expected_artifact_fingerprint,
        expected_state=args.expected_state,
        expected_setup_status=args.expected_setup_status,
        idempotency_key=args.idempotency_key,
        correlation_id=args.correlation_id,
        changed_by=args.changed_by,
    )


def _roadmap_generate(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Roadmap generate to the application facade."""
    return "agileforge roadmap generate", application.roadmap_generate(
        project_id=args.project_id,
        user_input=args.user_input,
    )


def _roadmap_history(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Roadmap history to the application facade."""
    return "agileforge roadmap history", application.roadmap_history(
        project_id=args.project_id,
    )


def _roadmap_save(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Roadmap save to the application facade."""
    return "agileforge roadmap save", application.roadmap_save(
        project_id=args.project_id,
        attempt_id=args.attempt_id,
        expected_artifact_fingerprint=args.expected_artifact_fingerprint,
        expected_state=args.expected_state,
        idempotency_key=args.idempotency_key,
    )


def _story_show(args: argparse.Namespace, application: _Application) -> CommandResult:
    """Route story show to the application facade."""
    return "agileforge story show", application.story_show(story_id=args.story_id)


def _story_pending(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Story pending to the application facade."""
    return "agileforge story pending", application.story_pending(
        project_id=args.project_id,
    )


def _story_generate(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Story generation to the application facade."""
    return "agileforge story generate", application.story_generate(
        project_id=args.project_id,
        parent_requirement=args.parent_requirement,
        user_input=args.user_input,
        force_feedback=bool(args.force_feedback),
    )


def _story_retry(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Story retry to the application facade."""
    return "agileforge story retry", application.story_retry(
        project_id=args.project_id,
        parent_requirement=args.parent_requirement,
    )


def _story_history(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Story history to the application facade."""
    return "agileforge story history", application.story_history(
        project_id=args.project_id,
        parent_requirement=args.parent_requirement,
    )


def _story_save(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Story save to the application facade."""
    return "agileforge story save", application.story_save(
        project_id=args.project_id,
        parent_requirement=args.parent_requirement,
        attempt_id=args.attempt_id,
        expected_artifact_fingerprint=args.expected_artifact_fingerprint,
        expected_state=args.expected_state,
        idempotency_key=args.idempotency_key,
    )


def _story_complete(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Story complete to the application facade."""
    return "agileforge story complete", application.story_complete(
        project_id=args.project_id,
        expected_state=args.expected_state,
        idempotency_key=args.idempotency_key,
        scope=args.scope,
        scope_id=args.scope_id,
        parent_requirements=args.parent_requirement,
    )


def _story_reopen(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Story reopen to the application facade."""
    return "agileforge story reopen", application.story_reopen(
        project_id=args.project_id,
        parent_requirement=args.parent_requirement,
        expected_state=args.expected_state,
        idempotency_key=args.idempotency_key,
    )


def _story_repair_readiness(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Story readiness repair to the application facade."""
    return "agileforge story repair-readiness", application.story_repair_readiness(
        project_id=args.project_id,
        expected_state=args.expected_state,
        idempotency_key=args.idempotency_key,
    )


def _story_dependencies_inspect(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Story dependency inspect to the application facade."""
    return (
        "agileforge story dependencies inspect",
        application.story_dependencies_inspect(project_id=args.project_id),
    )


def _story_dependencies_propose(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Story dependency propose to the application facade."""
    return (
        "agileforge story dependencies propose",
        application.story_dependencies_propose(
            project_id=args.project_id,
            expected_state=args.expected_state,
            idempotency_key=args.idempotency_key,
            manual_edges=args.manual_edges,
        ),
    )


def _story_dependencies_apply(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Story dependency apply to the application facade."""
    return (
        "agileforge story dependencies apply",
        application.story_dependencies_apply(
            project_id=args.project_id,
            attempt_id=args.attempt_id,
            expected_artifact_fingerprint=args.expected_artifact_fingerprint,
            expected_state=args.expected_state,
            idempotency_key=args.idempotency_key,
        ),
    )


def _sprint_candidates(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route sprint candidates to the application facade."""
    return "agileforge sprint candidates", application.sprint_candidates(
        project_id=args.project_id
    )


def _sprint_generate(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Sprint generation to the application facade."""
    return "agileforge sprint generate", application.sprint_generate(
        project_id=args.project_id,
        user_input=args.user_input,
        selected_story_ids=args.selected_story_ids,
        max_story_points=args.max_story_points,
        include_task_decomposition=args.include_task_decomposition,
    )


def _sprint_history(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Sprint history to the application facade."""
    return "agileforge sprint history", application.sprint_history(
        project_id=args.project_id,
    )


def _print_sprint_metrics_summary(result: JsonObject) -> None:
    """Print a compact Sprint metrics summary for humans."""
    if result.get("ok") is not True:
        return
    data = _as_mapping(result.get("data")) or {}
    summary = _as_mapping(data.get("summary")) or {}
    recommendation = _as_mapping(data.get("recommendation")) or {}
    sys.stderr.write(
        "\n".join(
            [
                "Sprint metrics",
                f"  project_id: {data.get('project_id', '')}",
                f"  status: {data.get('status', '')}",
                (
                    "  completed_sprint_count: "
                    f"{summary.get('completed_sprint_count', '')}"
                ),
                (
                    "  recommended_next_sprint_points: "
                    f"{recommendation.get('recommended_next_sprint_points', '')}"
                ),
            ]
        )
        + "\n"
    )


def _sprint_metrics(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Sprint metrics to the application facade."""
    result = application.sprint_metrics(project_id=args.project_id)
    _print_sprint_metrics_summary(result)
    return "agileforge sprint metrics", result


def _sprint_save(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Sprint save to the application facade."""
    required_text_fields = (
        "team_name",
        "attempt_id",
        "expected_artifact_fingerprint",
        "expected_state",
        "idempotency_key",
    )
    blank_fields = [
        field_name
        for field_name in required_text_fields
        if not str(getattr(args, field_name, "")).strip()
    ]
    if blank_fields:
        return _invalid_command(
            "agileforge sprint save",
            "Sprint save requires non-blank guard fields.",
            details={"blank": blank_fields},
            remediation=[
                "Pass non-blank --team-name, --attempt-id, "
                "--expected-artifact-fingerprint, --expected-state, and "
                "--idempotency-key values."
            ],
        )

    return "agileforge sprint save", application.sprint_save(
        project_id=args.project_id,
        team_name=args.team_name.strip(),
        attempt_id=args.attempt_id.strip(),
        expected_artifact_fingerprint=args.expected_artifact_fingerprint.strip(),
        expected_state=args.expected_state.strip(),
        idempotency_key=args.idempotency_key.strip(),
    )


def _sprint_start(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Sprint start to the application facade."""
    return "agileforge sprint start", application.sprint_start(
        project_id=args.project_id,
        sprint_id=args.sprint_id,
        expected_state=args.expected_state,
        idempotency_key=args.idempotency_key,
    )


def _sprint_status(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Sprint status to the application facade."""
    return "agileforge sprint status", application.sprint_status(
        project_id=args.project_id,
        sprint_id=args.sprint_id,
    )


def _sprint_tasks(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Sprint tasks to the application facade."""
    return "agileforge sprint tasks", application.sprint_tasks(
        project_id=args.project_id,
        sprint_id=args.sprint_id,
    )


def _sprint_task_next(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Sprint task next to the application facade."""
    return "agileforge sprint task next", application.sprint_task_next(
        project_id=args.project_id,
        sprint_id=args.sprint_id,
    )


def _sprint_task_show(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Sprint task show to the application facade."""
    return "agileforge sprint task show", application.sprint_task_show(
        project_id=args.project_id,
        task_id=args.task_id,
        sprint_id=args.sprint_id,
    )


def _sprint_task_history(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Sprint task history to the application facade."""
    return "agileforge sprint task history", application.sprint_task_history(
        project_id=args.project_id,
        task_id=args.task_id,
        sprint_id=args.sprint_id,
    )


def _sprint_task_update(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Sprint task update to the application facade."""
    return "agileforge sprint task update", application.sprint_task_update(
        project_id=args.project_id,
        task_id=args.task_id,
        status=args.status,
        expected_status=args.expected_status,
        expected_task_fingerprint=args.expected_task_fingerprint,
        idempotency_key=args.idempotency_key,
        sprint_id=args.sprint_id,
        outcome_summary=args.outcome_summary,
        artifact_refs=args.artifact_refs,
        checklist_result=args.checklist_result,
        validation_summary=args.validation_summary,
        notes=args.notes,
        changed_by=args.changed_by,
    )


def _sprint_story_readiness(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Sprint story readiness to the application facade."""
    return "agileforge sprint story readiness", application.sprint_story_readiness(
        project_id=args.project_id,
        story_id=args.story_id,
        sprint_id=args.sprint_id,
    )


def _sprint_story_close(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Sprint story close to the application facade."""
    return "agileforge sprint story close", application.sprint_story_close(
        project_id=args.project_id,
        story_id=args.story_id,
        expected_status=args.expected_status,
        expected_story_fingerprint=args.expected_story_fingerprint,
        idempotency_key=args.idempotency_key,
        resolution=args.resolution,
        completion_notes=args.completion_notes,
        evidence_links=args.evidence_links,
        sprint_id=args.sprint_id,
        changed_by=args.changed_by,
    )


def _sprint_close_readiness(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Sprint close readiness to the application facade."""
    return "agileforge sprint close-readiness", application.sprint_close_readiness(
        project_id=args.project_id,
        sprint_id=args.sprint_id,
    )


def _sprint_close(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Sprint close to the application facade."""
    return "agileforge sprint close", application.sprint_close(
        project_id=args.project_id,
        expected_state=args.expected_state,
        expected_status=args.expected_status,
        expected_sprint_fingerprint=args.expected_sprint_fingerprint,
        idempotency_key=args.idempotency_key,
        completion_notes=args.completion_notes,
        follow_up_notes=args.follow_up_notes,
        sprint_id=args.sprint_id,
        changed_by=args.changed_by,
    )


def _sprint_review(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route post-sprint review to the application facade."""
    return "agileforge sprint review", application.sprint_review(
        project_id=args.project_id,
        sprint_id=args.sprint_id,
    )


def _sprint_triage(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route post-sprint triage to the application facade."""
    return "agileforge sprint triage", application.sprint_triage(
        project_id=args.project_id,
        expected_state=args.expected_state,
        impact=args.impact,
        learning_summary=args.learning_summary,
        decision_reason=args.decision_reason,
        idempotency_key=args.idempotency_key,
        affected_requirements=args.affected_requirement,
        affected_task_ids=args.affected_task_id,
        affected_story_ids=args.affected_story_id,
        affected_backlog_item_ids=args.affected_backlog_item_id,
        affected_roadmap_item_ids=args.affected_roadmap_item_id,
        affected_layers=args.affected_layer,
        sprint_id=args.sprint_id,
        replace_existing=args.replace_existing,
        expected_triage_fingerprint=args.expected_triage_fingerprint,
        changed_by=args.changed_by,
    )


def _context_pack(args: argparse.Namespace, application: _Application) -> CommandResult:
    """Route context pack to the application facade."""
    return "agileforge context pack", application.context_pack(
        project_id=args.project_id,
        phase=args.phase,
    )


def _status(args: argparse.Namespace, application: _Application) -> CommandResult:
    """Route root status to the application facade."""
    return "agileforge status", application.status(project_id=args.project_id)


def _doctor(
    _args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route doctor diagnostics to the application facade."""
    return "agileforge doctor", application.doctor()


def _schema_check(
    _args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route schema check diagnostics to the application facade."""
    return "agileforge schema check", application.schema_check()


def _spec_profile_schema(
    _args: argparse.Namespace,
    _application: _Application,
) -> CommandResult:
    """Return the AgileForge spec profile JSON Schema."""
    return (
        "agileforge spec profile schema",
        {
            "ok": True,
            "data": {"schema": export_agileforge_spec_schema()},
            "warnings": [],
            "errors": [],
        },
    )


def _spec_profile_validate(
    args: argparse.Namespace,
    _application: _Application,
) -> CommandResult:
    """Validate a spec profile JSON file and optionally render Markdown."""
    command = "agileforge spec profile validate"
    spec_path = Path(str(args.spec_file)).expanduser().resolve()
    try:
        raw_spec = spec_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        return _spec_profile_error(
            command,
            ErrorCode.SPEC_FILE_NOT_FOUND,
            str(exc),
            details={
                "exception_type": type(exc).__name__,
                "spec_file": str(spec_path),
            },
            remediation=["Pass an existing AgileForge spec profile JSON file."],
        )
    except (OSError, UnicodeDecodeError) as exc:
        return _spec_profile_error(
            command,
            ErrorCode.SPEC_FILE_INVALID,
            str(exc),
            details={
                "exception_type": type(exc).__name__,
                "spec_file": str(spec_path),
            },
            remediation=["Pass a readable UTF-8 AgileForge spec profile JSON file."],
        )

    try:
        artifact = TechnicalSpecArtifact.model_validate_json(raw_spec)
    except ValidationError as exc:
        return _spec_profile_error(
            command,
            ErrorCode.SPEC_FILE_INVALID,
            str(exc),
            details={
                "exception_type": type(exc).__name__,
                "spec_file": str(spec_path),
            },
            remediation=["Pass a valid AgileForge spec profile JSON file."],
        )

    markdown = render_markdown(artifact)
    render_md = getattr(args, "render_md", None)
    if render_md:
        render_path = Path(str(render_md)).expanduser().resolve()
        try:
            render_path.write_text(markdown, encoding="utf-8")
        except OSError as exc:
            return _spec_profile_error(
                command,
                ErrorCode.INVALID_COMMAND,
                str(exc),
                details={
                    "exception_type": type(exc).__name__,
                    "render_md": str(render_path),
                },
                remediation=["Choose a writable Markdown output path for --render-md."],
            )

    return (
        command,
        {
            "ok": True,
            "data": {
                "format": "agileforge.spec.v1",
                "spec_sha256": canonical_spec_hash(artifact),
                "rendered_markdown_sha256": rendered_markdown_hash(markdown),
            },
            "warnings": [],
            "errors": [],
        },
    )


def _spec_profile_error(
    command: str,
    code: ErrorCode,
    message: str,
    *,
    details: dict[str, object],
    remediation: list[str],
) -> CommandResult:
    """Return a structured spec profile command failure."""
    return (
        command,
        {
            "ok": False,
            "data": None,
            "warnings": [],
            "errors": [
                workbench_error(
                    code,
                    message=message,
                    details=details,
                    remediation=remediation,
                ).to_dict()
            ],
        },
    )


def _capabilities(
    _args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route capabilities to the application facade."""
    return "agileforge capabilities", application.capabilities()


def _command_schema(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route command schema lookup to the application facade."""
    return "agileforge command schema", application.command_schema(
        command_name=args.command_name,
    )


def _mutation_show(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route mutation show to the application facade."""
    return "agileforge mutation show", application.mutation_show(
        mutation_event_id=args.mutation_event_id,
    )


def _mutation_list(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route mutation list to the application facade."""
    return "agileforge mutation list", application.mutation_list(
        project_id=args.project_id,
        status=args.status,
    )


def _mutation_resume(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route mutation resume lease acquisition to the application facade."""
    return "agileforge mutation resume", application.mutation_resume(
        mutation_event_id=args.mutation_event_id,
        correlation_id=args.correlation_id,
    )


def _dispatch(args: argparse.Namespace, application: _Application) -> CommandResult:
    """Route parsed arguments to the application facade."""
    handler = getattr(args, "command_handler", None)
    if callable(handler):
        return cast("CommandHandler", handler)(args, application)

    group = args.group
    action = getattr(args, "action", None)
    return "agileforge", {
        "ok": False,
        "warnings": [],
        "errors": [
            {
                "code": "COMMAND_NOT_IMPLEMENTED",
                "message": "Command is not implemented.",
                "details": {"group": group, "action": action},
                "remediation": ["Run agileforge --help."],
                "exit_code": 2,
                "retryable": False,
            }
        ],
    }


def main(argv: list[str] | None = None, *, application: object | None = None) -> int:
    """Run the CLI and return a process exit code."""
    configure_logging(console=False)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except _CliParseError as exc:
        envelope = _parse_error_envelope(str(exc), argv)
        _print_json(envelope)
        return INVALID_COMMAND_EXIT_CODE

    try:
        app = (
            cast("_Application", application)
            if application is not None
            else _default_application()
        )
        with redirect_stdout(io.StringIO()):
            command, result = _dispatch(args, app)
        plain_text = _plain_text_output(args, result)
        if plain_text is not None:
            sys.stdout.write(f"{plain_text}\n")
            return 0
        envelope = _wrap(
            command,
            result,
            compact_warnings=_compact_warnings_requested(args),
        )
    except Exception as exc:  # noqa: BLE001
        envelope = _exception_envelope(exc)
        _print_json(envelope)
        return COMMAND_EXCEPTION_EXIT_CODE

    _print_json(envelope)
    return _exit_code(envelope)


def _default_application() -> _Application:
    """Create the default application facade."""
    application_module = importlib.import_module("services.agent_workbench.application")
    application_factory = cast(
        "Callable[[], _Application]",
        application_module.AgentWorkbenchApplication,
    )
    return application_factory()


if __name__ == "__main__":
    raise SystemExit(main())
