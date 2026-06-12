"""Tests for the agileforge CLI transport."""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from cli.main import main
from services.agent_workbench.error_codes import ErrorCode

type JsonObject = dict[str, Any]
PROJECT_ID = 7
SPEC_VERSION_ID = 3
STORY_ID = 42
RECOMMENDED_SPRINT_POINTS = 5
ERROR_EXIT_CODE = 5
INVALID_COMMAND_EXIT_CODE = 2
COMMAND_EXCEPTION_EXIT_CODE = 1


class _FakeApplication:
    """Fake application facade used to verify CLI routing."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.results: dict[str, JsonObject] = {}

    def __bool__(self) -> bool:
        """Return false to catch truthiness-based dependency selection."""
        return False

    def project_list(self) -> JsonObject:
        """Return a project list payload."""
        self.calls.append(("project_list", {}))
        return {"ok": True, "data": {"items": []}, "warnings": [], "errors": []}

    def project_show(self, *, project_id: int) -> JsonObject:
        """Return a project detail payload."""
        self.calls.append(("project_show", {"project_id": project_id}))
        return {
            "ok": True,
            "data": {"project_id": project_id},
            "warnings": [],
            "errors": [],
        }

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
    ) -> JsonObject:
        """Return a project create payload."""
        self.calls.append(
            (
                "project_create",
                {
                    "name": name,
                    "spec_file": spec_file,
                    "idempotency_key": idempotency_key,
                    "dry_run": dry_run,
                    "dry_run_id": dry_run_id,
                    "correlation_id": correlation_id,
                    "changed_by": changed_by,
                },
            )
        )
        return {"ok": True, "data": {"project_id": 1}, "warnings": [], "errors": []}

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
    ) -> JsonObject:
        """Return a project setup retry payload."""
        self.calls.append(
            (
                "project_setup_retry",
                {
                    "project_id": project_id,
                    "spec_file": spec_file,
                    "expected_state": expected_state,
                    "expected_context_fingerprint": expected_context_fingerprint,
                    "recovery_mutation_event_id": recovery_mutation_event_id,
                    "idempotency_key": idempotency_key,
                    "dry_run": dry_run,
                    "dry_run_id": dry_run_id,
                    "correlation_id": correlation_id,
                    "changed_by": changed_by,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id},
            "warnings": [],
            "errors": [],
        }

    def workflow_state(self, *, project_id: int) -> JsonObject:
        """Return a workflow state payload."""
        self.calls.append(("workflow_state", {"project_id": project_id}))
        return {
            "ok": True,
            "data": {"project_id": project_id, "state": {}},
            "warnings": [],
            "errors": [],
        }

    def workflow_next(self, *, project_id: int) -> JsonObject:
        """Return a workflow next payload."""
        self.calls.append(("workflow_next", {"project_id": project_id}))
        return {
            "ok": True,
            "data": {"project_id": project_id, "next_valid_commands": []},
            "warnings": [],
            "errors": [],
        }

    def authority_status(self, *, project_id: int) -> JsonObject:
        """Return an authority status payload."""
        self.calls.append(("authority_status", {"project_id": project_id}))
        return {
            "ok": True,
            "data": {"project_id": project_id, "status": "missing"},
            "warnings": [],
            "errors": [],
        }

    def authority_invariants(
        self,
        *,
        project_id: int,
        spec_version_id: int | None = None,
    ) -> JsonObject:
        """Return an authority invariants payload."""
        self.calls.append(
            (
                "authority_invariants",
                {
                    "project_id": project_id,
                    "spec_version_id": spec_version_id,
                },
            )
        )
        return {
            "ok": True,
            "data": {
                "project_id": project_id,
                "spec_version_id": spec_version_id,
            },
            "warnings": [],
            "errors": [],
        }

    def vision_generate(
        self,
        *,
        project_id: int,
        user_input: str | None = None,
    ) -> JsonObject:
        """Return a vision generate payload."""
        self.calls.append(
            (
                "vision_generate",
                {"project_id": project_id, "user_input": user_input},
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "is_complete": False},
            "warnings": [],
            "errors": [],
        }

    def vision_history(self, *, project_id: int) -> JsonObject:
        """Return a vision history payload."""
        self.calls.append(("vision_history", {"project_id": project_id}))
        return {
            "ok": True,
            "data": {"project_id": project_id, "items": []},
            "warnings": [],
            "errors": [],
        }

    def vision_save(self, *, project_id: int) -> JsonObject:
        """Return a vision save payload."""
        self.calls.append(("vision_save", {"project_id": project_id}))
        return {
            "ok": True,
            "data": {"project_id": project_id, "fsm_state": "VISION_PERSISTENCE"},
            "warnings": [],
            "errors": [],
        }

    def backlog_generate(
        self,
        *,
        project_id: int,
        user_input: str | None = None,
    ) -> JsonObject:
        """Return a backlog generate payload."""
        self.calls.append(
            (
                "backlog_generate",
                {"project_id": project_id, "user_input": user_input},
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "is_complete": False},
            "warnings": [],
            "errors": [],
        }

    def backlog_preview(
        self,
        *,
        project_id: int,
        user_input: str | None = None,
    ) -> JsonObject:
        """Return a backlog preview payload."""
        self.calls.append(
            (
                "backlog_preview",
                {"project_id": project_id, "user_input": user_input},
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "persisted": False},
            "warnings": [],
            "errors": [],
        }

    def backlog_refine_preview(
        self,
        *,
        project_id: int,
        source_attempt_id: str | None = None,
        operations_file: str | None = None,
        source_artifact: str | None = None,
        user_input: str | None = None,
    ) -> JsonObject:
        """Return a backlog refinement preview payload."""
        self.calls.append(
            (
                "backlog_refine_preview",
                {
                    "project_id": project_id,
                    "source_attempt_id": source_attempt_id,
                    "operations_file": operations_file,
                    "source_artifact": source_artifact,
                    "user_input": user_input,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "persisted": False},
            "warnings": [],
            "errors": [],
        }

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
        """Return a recorded backlog refinement payload."""
        self.calls.append(
            (
                "backlog_refine_record",
                {
                    "project_id": project_id,
                    "source_attempt_id": source_attempt_id,
                    "operations_file": operations_file,
                    "expected_source_fingerprint": expected_source_fingerprint,
                    "expected_state": expected_state,
                    "idempotency_key": idempotency_key,
                    "approval_id": approval_id,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "fsm_state": "BACKLOG_REVIEW"},
            "warnings": [],
            "errors": [],
        }

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
        """Return a backlog refinement approval payload."""
        self.calls.append(
            (
                "backlog_approve",
                {
                    "project_id": project_id,
                    "source_attempt_id": source_attempt_id,
                    "attempt_id": attempt_id,
                    "operation_set_fingerprint": operation_set_fingerprint,
                    "approved_artifact_fingerprint": approved_artifact_fingerprint,
                    "approved_operation_ids": approved_operation_ids,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "approval_id": "approval:1234"},
            "warnings": [],
            "errors": [],
        }

    def backlog_refine_import(
        self,
        *,
        project_id: int,
        source_artifact: str,
        edited_file: str,
        expected_source_fingerprint: str,
        idempotency_key: str,
    ) -> JsonObject:
        """Return a backlog refinement import payload."""
        self.calls.append(
            (
                "backlog_refine_import",
                {
                    "project_id": project_id,
                    "source_artifact": source_artifact,
                    "edited_file": edited_file,
                    "expected_source_fingerprint": expected_source_fingerprint,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "placeholder": True},
            "warnings": [],
            "errors": [],
        }

    def backlog_history(self, *, project_id: int) -> JsonObject:
        """Return a backlog history payload."""
        self.calls.append(("backlog_history", {"project_id": project_id}))
        return {
            "ok": True,
            "data": {"project_id": project_id, "items": []},
            "warnings": [],
            "errors": [],
        }

    def backlog_save(
        self,
        *,
        project_id: int,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> JsonObject:
        """Return a backlog save payload."""
        self.calls.append(
            (
                "backlog_save",
                {
                    "project_id": project_id,
                    "attempt_id": attempt_id,
                    "expected_artifact_fingerprint": expected_artifact_fingerprint,
                    "expected_state": expected_state,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "fsm_state": "BACKLOG_PERSISTENCE"},
            "warnings": [],
            "errors": [],
        }

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
        """Return a backlog reset-active payload."""
        self.calls.append(
            (
                "backlog_reset_active",
                {
                    "project_id": project_id,
                    "attempt_id": attempt_id,
                    "expected_artifact_fingerprint": expected_artifact_fingerprint,
                    "expected_state": expected_state,
                    "reset_reason": reset_reason,
                    "archive_all_active_stories": archive_all_active_stories,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "fsm_state": "BACKLOG_PERSISTENCE"},
            "warnings": [],
            "errors": [],
        }

    def backlog_reconcile(
        self,
        *,
        project_id: int,
        idempotency_key: str,
    ) -> JsonObject:
        """Return a backlog reconcile payload."""
        self.calls.append(
            (
                "backlog_reconcile",
                {
                    "project_id": project_id,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "active_after": 2},
            "warnings": [],
            "errors": [],
        }

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
        """Return an as-built assessment payload."""
        self.calls.append(
            (
                "as_built_assess",
                {
                    "project_id": project_id,
                    "repo_path": repo_path,
                    "spec_file": spec_file,
                    "spec_mode": spec_mode,
                    "user_input": user_input,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "is_complete": True},
            "warnings": [],
            "errors": [],
        }

    def roadmap_generate(
        self,
        *,
        project_id: int,
        user_input: str | None = None,
    ) -> JsonObject:
        """Return a roadmap generate payload."""
        self.calls.append(
            (
                "roadmap_generate",
                {"project_id": project_id, "user_input": user_input},
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "is_complete": False},
            "warnings": [],
            "errors": [],
        }

    def roadmap_history(self, *, project_id: int) -> JsonObject:
        """Return a roadmap history payload."""
        self.calls.append(("roadmap_history", {"project_id": project_id}))
        return {
            "ok": True,
            "data": {"project_id": project_id, "items": []},
            "warnings": [],
            "errors": [],
        }

    def roadmap_save(
        self,
        *,
        project_id: int,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> JsonObject:
        """Return a roadmap save payload."""
        self.calls.append(
            (
                "roadmap_save",
                {
                    "project_id": project_id,
                    "attempt_id": attempt_id,
                    "expected_artifact_fingerprint": expected_artifact_fingerprint,
                    "expected_state": expected_state,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "fsm_state": "ROADMAP_PERSISTENCE"},
            "warnings": [],
            "errors": [],
        }

    def story_show(self, *, story_id: int) -> JsonObject:
        """Return a story detail payload."""
        self.calls.append(("story_show", {"story_id": story_id}))
        return {
            "ok": True,
            "data": {"story_id": story_id},
            "warnings": [],
            "errors": [],
        }

    def story_pending(self, *, project_id: int) -> JsonObject:
        """Return a story pending payload."""
        self.calls.append(("story_pending", {"project_id": project_id}))
        return {
            "ok": True,
            "data": {"project_id": project_id, "pending": []},
            "warnings": [],
            "errors": [],
        }

    def story_generate(
        self,
        *,
        project_id: int,
        parent_requirement: str,
        user_input: str | None = None,
        force_feedback: bool = False,
    ) -> JsonObject:
        """Return a story generate payload."""
        self.calls.append(
            (
                "story_generate",
                {
                    "project_id": project_id,
                    "parent_requirement": parent_requirement,
                    "user_input": user_input,
                    "force_feedback": force_feedback,
                },
            )
        )
        return self.results.get("story_generate") or {
            "ok": True,
            "data": {"project_id": project_id, "is_complete": False},
            "warnings": [],
            "errors": [],
        }

    def story_retry(
        self,
        *,
        project_id: int,
        parent_requirement: str,
    ) -> JsonObject:
        """Return a story retry payload."""
        self.calls.append(
            (
                "story_retry",
                {
                    "project_id": project_id,
                    "parent_requirement": parent_requirement,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "retry_started": True},
            "warnings": [],
            "errors": [],
        }

    def story_history(
        self,
        *,
        project_id: int,
        parent_requirement: str,
    ) -> JsonObject:
        """Return a story history payload."""
        self.calls.append(
            (
                "story_history",
                {
                    "project_id": project_id,
                    "parent_requirement": parent_requirement,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "items": []},
            "warnings": [],
            "errors": [],
        }

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
        """Return a story save payload."""
        self.calls.append(
            (
                "story_save",
                {
                    "project_id": project_id,
                    "parent_requirement": parent_requirement,
                    "attempt_id": attempt_id,
                    "expected_artifact_fingerprint": expected_artifact_fingerprint,
                    "expected_state": expected_state,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return self.results.get("story_save") or {
            "ok": True,
            "data": {"project_id": project_id, "fsm_state": "STORY_PERSISTENCE"},
            "warnings": [],
            "errors": [],
        }

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
        """Return a story complete payload."""
        call_args: JsonObject = {
            "project_id": project_id,
            "expected_state": expected_state,
            "idempotency_key": idempotency_key,
        }
        if scope is not None:
            call_args["scope"] = scope
            call_args["scope_id"] = scope_id
        elif scope_id is not None:
            call_args["scope_id"] = scope_id
        if parent_requirements is not None:
            call_args["parent_requirements"] = parent_requirements
        self.calls.append(
            (
                "story_complete",
                call_args,
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "fsm_state": "SPRINT_SETUP"},
            "warnings": [],
            "errors": [],
        }

    def story_reopen(
        self,
        *,
        project_id: int,
        parent_requirement: str,
        expected_state: str,
        idempotency_key: str,
    ) -> JsonObject:
        """Return a story reopen payload."""
        self.calls.append(
            (
                "story_reopen",
                {
                    "project_id": project_id,
                    "parent_requirement": parent_requirement,
                    "expected_state": expected_state,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "fsm_state": "STORY_INTERVIEW"},
            "warnings": [],
            "errors": [],
        }

    def story_repair_readiness(
        self,
        *,
        project_id: int,
        expected_state: str,
        idempotency_key: str,
    ) -> JsonObject:
        """Return a story readiness repair payload."""
        self.calls.append(
            (
                "story_repair_readiness",
                {
                    "project_id": project_id,
                    "expected_state": expected_state,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {
            "ok": True,
            "data": {
                "project_id": project_id,
                "fsm_state": "SPRINT_SETUP",
                "repair_result": {"repaired_count": 1, "story_ids": [66]},
            },
            "warnings": [],
            "errors": [],
        }

    def story_dependencies_inspect(self, *, project_id: int) -> JsonObject:
        """Return a story dependency inspect payload."""
        self.calls.append(("story_dependencies_inspect", {"project_id": project_id}))
        return {
            "ok": True,
            "data": {"project_id": project_id, "active_edge_count": 0},
            "warnings": [],
            "errors": [],
        }

    def story_dependencies_propose(
        self,
        *,
        project_id: int,
        expected_state: str,
        idempotency_key: str,
        manual_edges: list[str] | None = None,
    ) -> JsonObject:
        """Return a story dependency propose payload."""
        self.calls.append(
            (
                "story_dependencies_propose",
                {
                    "project_id": project_id,
                    "expected_state": expected_state,
                    "idempotency_key": idempotency_key,
                    "manual_edges": manual_edges,
                },
            )
        )
        return {
            "ok": True,
            "data": {
                "project_id": project_id,
                "attempt_id": "story-dependencies-test",
                "artifact_fingerprint": "sha256:" + "a" * 64,
            },
            "warnings": [],
            "errors": [],
        }

    def story_dependencies_apply(
        self,
        *,
        project_id: int,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> JsonObject:
        """Return a story dependency apply payload."""
        self.calls.append(
            (
                "story_dependencies_apply",
                {
                    "project_id": project_id,
                    "attempt_id": attempt_id,
                    "expected_artifact_fingerprint": expected_artifact_fingerprint,
                    "expected_state": expected_state,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "activated_edge_count": 1},
            "warnings": [],
            "errors": [],
        }

    def sprint_candidates(self, *, project_id: int) -> JsonObject:
        """Return a sprint candidates payload."""
        self.calls.append(("sprint_candidates", {"project_id": project_id}))
        return {
            "ok": True,
            "data": {"project_id": project_id, "items": []},
            "warnings": [],
            "errors": [],
        }

    def sprint_generate(
        self,
        *,
        project_id: int,
        user_input: str | None = None,
        selected_story_ids: list[int] | None = None,
        max_story_points: int | None = None,
        include_task_decomposition: bool = True,
    ) -> JsonObject:
        """Return a sprint generate payload."""
        self.calls.append(
            (
                "sprint_generate",
                {
                    "project_id": project_id,
                    "user_input": user_input,
                    "selected_story_ids": selected_story_ids,
                    "max_story_points": max_story_points,
                    "include_task_decomposition": include_task_decomposition,
                },
            )
        )
        return self.results.get("sprint_generate") or {
            "ok": True,
            "data": {"project_id": project_id, "fsm_state": "SPRINT_DRAFT"},
            "warnings": [],
            "errors": [],
        }

    def sprint_history(self, *, project_id: int) -> JsonObject:
        """Return a sprint history payload."""
        self.calls.append(("sprint_history", {"project_id": project_id}))
        return {
            "ok": True,
            "data": {"project_id": project_id, "items": []},
            "warnings": [],
            "errors": [],
        }

    def sprint_metrics(self, *, project_id: int) -> JsonObject:
        """Return a sprint metrics payload."""
        self.calls.append(("sprint_metrics", {"project_id": project_id}))
        return self.results.get("sprint_metrics") or {
            "ok": True,
            "data": {
                "project_id": project_id,
                "status": "ready",
                "summary": {
                    "completed_sprint_count": 4,
                    "completed_story_points": 18,
                    "average_points_per_sprint": 4.5,
                    "median_points_per_sprint": 5,
                    "points_per_hour": 1.8,
                },
                "recommendation": {
                    "recommended_next_sprint_points": RECOMMENDED_SPRINT_POINTS,
                },
                "completed_sprints": [],
                "token_metrics": {"status": "unavailable"},
                "data_quality_warnings": [],
            },
            "warnings": [],
            "errors": [],
        }

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
        """Return a sprint save payload."""
        self.calls.append(
            (
                "sprint_save",
                {
                    "project_id": project_id,
                    "team_name": team_name,
                    "attempt_id": attempt_id,
                    "expected_artifact_fingerprint": expected_artifact_fingerprint,
                    "expected_state": expected_state,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return self.results.get("sprint_save") or {
            "ok": True,
            "data": {"project_id": project_id, "fsm_state": "SPRINT_PERSISTENCE"},
            "warnings": [],
            "errors": [],
        }

    def sprint_start(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
        expected_state: str,
        idempotency_key: str,
    ) -> JsonObject:
        """Return a sprint start payload."""
        self.calls.append(
            (
                "sprint_start",
                {
                    "project_id": project_id,
                    "sprint_id": sprint_id,
                    "expected_state": expected_state,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {
            "ok": True,
            "data": {
                "project_id": project_id,
                "sprint_id": sprint_id or 11,
                "fsm_state": "SPRINT_VIEW",
            },
            "warnings": [],
            "errors": [],
        }

    def sprint_status(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        """Return a sprint status payload."""
        self.calls.append(
            ("sprint_status", {"project_id": project_id, "sprint_id": sprint_id})
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "sprint_id": sprint_id or 11},
            "warnings": [],
            "errors": [],
        }

    def sprint_tasks(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        """Return a sprint tasks payload."""
        self.calls.append(
            ("sprint_tasks", {"project_id": project_id, "sprint_id": sprint_id})
        )
        return {
            "ok": True,
            "data": {
                "project_id": project_id,
                "sprint_id": sprint_id or 11,
                "tasks": [],
            },
            "warnings": [],
            "errors": [],
        }

    def sprint_task_next(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        """Return a sprint task ticket payload."""
        self.calls.append(
            ("sprint_task_next", {"project_id": project_id, "sprint_id": sprint_id})
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "task_ticket": None},
            "warnings": [],
            "errors": [],
        }

    def sprint_task_show(
        self,
        *,
        project_id: int,
        task_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        """Return a sprint task ticket payload."""
        self.calls.append(
            (
                "sprint_task_show",
                {"project_id": project_id, "task_id": task_id, "sprint_id": sprint_id},
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "task_ticket": {"task_id": task_id}},
            "warnings": [],
            "errors": [],
        }

    def sprint_task_history(
        self,
        *,
        project_id: int,
        task_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        """Return a sprint task history payload."""
        self.calls.append(
            (
                "sprint_task_history",
                {"project_id": project_id, "task_id": task_id, "sprint_id": sprint_id},
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "execution": {"history": []}},
            "warnings": [],
            "errors": [],
        }

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
        """Return a sprint task update payload."""
        self.calls.append(
            (
                "sprint_task_update",
                {
                    "project_id": project_id,
                    "task_id": task_id,
                    "status": status,
                    "expected_status": expected_status,
                    "expected_task_fingerprint": expected_task_fingerprint,
                    "idempotency_key": idempotency_key,
                    "sprint_id": sprint_id,
                    "outcome_summary": outcome_summary,
                    "artifact_refs": artifact_refs,
                    "checklist_result": checklist_result,
                    "validation_summary": validation_summary,
                    "notes": notes,
                    "changed_by": changed_by,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "task_id": task_id},
            "warnings": [],
            "errors": [],
        }

    def sprint_story_readiness(
        self,
        *,
        project_id: int,
        story_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        """Return a sprint story readiness payload."""
        self.calls.append(
            (
                "sprint_story_readiness",
                {
                    "project_id": project_id,
                    "story_id": story_id,
                    "sprint_id": sprint_id,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "story_id": story_id},
            "warnings": [],
            "errors": [],
        }

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
        """Return a sprint story close payload."""
        self.calls.append(
            (
                "sprint_story_close",
                {
                    "project_id": project_id,
                    "story_id": story_id,
                    "expected_status": expected_status,
                    "expected_story_fingerprint": expected_story_fingerprint,
                    "idempotency_key": idempotency_key,
                    "resolution": resolution,
                    "completion_notes": completion_notes,
                    "evidence_links": evidence_links,
                    "sprint_id": sprint_id,
                    "changed_by": changed_by,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "story_id": story_id},
            "warnings": [],
            "errors": [],
        }

    def sprint_close_readiness(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        """Return a sprint close readiness payload."""
        self.calls.append(
            (
                "sprint_close_readiness",
                {"project_id": project_id, "sprint_id": sprint_id},
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "sprint_id": sprint_id or 11},
            "warnings": [],
            "errors": [],
        }

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
        """Return a sprint close payload."""
        self.calls.append(
            (
                "sprint_close",
                {
                    "project_id": project_id,
                    "expected_state": expected_state,
                    "expected_status": expected_status,
                    "expected_sprint_fingerprint": expected_sprint_fingerprint,
                    "idempotency_key": idempotency_key,
                    "completion_notes": completion_notes,
                    "follow_up_notes": follow_up_notes,
                    "sprint_id": sprint_id,
                    "changed_by": changed_by,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "sprint_id": sprint_id or 11},
            "warnings": [],
            "errors": [],
        }

    def sprint_review(
        self,
        *,
        project_id: int,
        sprint_id: int | None = None,
    ) -> JsonObject:
        """Return a post-sprint review payload."""
        self.calls.append(
            ("sprint_review", {"project_id": project_id, "sprint_id": sprint_id})
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "sprint_id": sprint_id or 11},
            "warnings": [],
            "errors": [],
        }

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
        """Return a post-sprint triage payload."""
        self.calls.append(
            (
                "sprint_triage",
                {
                    "project_id": project_id,
                    "expected_state": expected_state,
                    "impact": impact,
                    "learning_summary": learning_summary,
                    "decision_reason": decision_reason,
                    "idempotency_key": idempotency_key,
                    "affected_requirements": affected_requirements,
                    "affected_task_ids": affected_task_ids,
                    "affected_story_ids": affected_story_ids,
                    "affected_backlog_item_ids": affected_backlog_item_ids,
                    "affected_roadmap_item_ids": affected_roadmap_item_ids,
                    "affected_layers": affected_layers,
                    "sprint_id": sprint_id,
                    "replace_existing": replace_existing,
                    "expected_triage_fingerprint": expected_triage_fingerprint,
                    "changed_by": changed_by,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "impact": impact},
            "warnings": [],
            "errors": [],
        }

    def context_pack(
        self,
        *,
        project_id: int,
        phase: str = "overview",
    ) -> JsonObject:
        """Return a context pack payload."""
        self.calls.append(("context_pack", {"project_id": project_id, "phase": phase}))
        return {
            "ok": True,
            "data": {"project_id": project_id, "phase": phase},
            "warnings": [],
            "errors": [],
        }

    def status(self, *, project_id: int) -> JsonObject:
        """Return a root status payload."""
        self.calls.append(("status", {"project_id": project_id}))
        return {
            "ok": True,
            "data": {"project_id": project_id, "status": "ok"},
            "warnings": [],
            "errors": [],
        }

    def doctor(self) -> JsonObject:
        """Return a doctor diagnostics payload."""
        self.calls.append(("doctor", {}))
        return {"ok": True, "data": {"checks": []}, "warnings": [], "errors": []}

    def schema_check(self) -> JsonObject:
        """Return a schema check diagnostics payload."""
        self.calls.append(("schema_check", {}))
        return {"ok": True, "data": {"stores": []}, "warnings": [], "errors": []}

    def capabilities(self) -> JsonObject:
        """Return a capabilities payload."""
        self.calls.append(("capabilities", {}))
        return {"ok": True, "data": {"commands": []}, "warnings": [], "errors": []}

    def command_schema(self, *, command_name: str) -> JsonObject:
        """Return a command schema payload."""
        self.calls.append(("command_schema", {"command_name": command_name}))
        return {
            "ok": True,
            "data": {"name": command_name},
            "warnings": [],
            "errors": [],
        }

    def mutation_show(self, *, mutation_event_id: int) -> JsonObject:
        """Return a mutation ledger row payload."""
        self.calls.append(("mutation_show", {"mutation_event_id": mutation_event_id}))
        return {
            "ok": True,
            "data": {"mutation_event_id": mutation_event_id},
            "warnings": [],
            "errors": [],
        }

    def mutation_list(
        self,
        *,
        project_id: int | None = None,
        status: str | None = None,
    ) -> JsonObject:
        """Return mutation ledger rows."""
        self.calls.append(
            ("mutation_list", {"project_id": project_id, "status": status})
        )
        return {"ok": True, "data": {"items": []}, "warnings": [], "errors": []}

    def mutation_resume(
        self,
        *,
        mutation_event_id: int,
        correlation_id: str | None = None,
    ) -> JsonObject:
        """Return a mutation resume payload."""
        self.calls.append(
            (
                "mutation_resume",
                {
                    "mutation_event_id": mutation_event_id,
                    "correlation_id": correlation_id,
                },
            )
        )
        return {
            "ok": True,
            "data": {"mutation_event_id": mutation_event_id},
            "warnings": [],
            "errors": [],
        }


class _FailingApplication(_FakeApplication):
    """Fake application that returns a structured command failure."""

    def project_show(self, *, project_id: int) -> JsonObject:
        """Return a structured project show failure."""
        self.calls.append(("project_show", {"project_id": project_id}))
        return {
            "ok": False,
            "data": None,
            "warnings": [
                {
                    "code": "CACHE_STALE",
                    "message": "Cached projection is stale.",
                    "details": {"project_id": project_id},
                    "remediation": ["Retry after refresh."],
                }
            ],
            "errors": [
                {
                    "code": "PROJECT_NOT_FOUND",
                    "message": "Project does not exist.",
                    "details": {"project_id": project_id},
                    "remediation": ["agileforge project list"],
                    "exit_code": ERROR_EXIT_CODE,
                    "retryable": False,
                }
            ],
        }


class _ExplodingApplication(_FakeApplication):
    """Fake application that raises an unexpected exception."""

    def project_list(self) -> JsonObject:
        """Raise an unexpected runtime error."""
        self.calls.append(("project_list", {}))
        msg = "projection exploded"
        raise RuntimeError(msg)


def _stdout_payload(capsys: pytest.CaptureFixture[str]) -> JsonObject:
    """Return captured stdout as a JSON object."""
    captured = capsys.readouterr()
    assert captured.err == ""
    return cast("JsonObject", json.loads(captured.out))


def _mapping(value: object) -> JsonObject:
    """Return a JSON object field from a payload."""
    assert isinstance(value, dict)
    return cast("JsonObject", value)


def _sequence(value: object) -> list[object]:
    """Return a JSON list field from a payload."""
    assert isinstance(value, list)
    return cast("list[object]", value)


def _first_mapping(value: object) -> JsonObject:
    """Return the first JSON object from a list field."""
    items = _sequence(value)
    assert items
    first = items[0]
    assert isinstance(first, dict)
    return cast("JsonObject", first)


def _agileforge_spec_profile_payload() -> dict[str, object]:
    """Return a minimal valid AgileForge spec profile payload."""
    return {
        "schema_version": "agileforge.spec.v1",
        "artifact_id": "SPEC.test",
        "title": "Test Spec",
        "status": "draft",
        "version": "0.1",
        "created_at": "2026-05-18",
        "updated_at": "2026-05-18",
        "summary": "Exercise the spec profile CLI.",
        "problem_statement": "Agents need a structured spec profile smoke test.",
        "items": [
            {
                "id": "GOAL.test.profile-cli",
                "type": "GOAL",
                "status": "proposed",
                "title": "Profile CLI",
                "statement": "Expose spec profile utilities through the CLI.",
            },
            {
                "id": "REQ.test.render-markdown",
                "type": "REQ",
                "status": "proposed",
                "title": "Render markdown",
                "statement": "The CLI MUST render deterministic Markdown.",
                "level": "MUST",
                "verification": "unit-test",
                "acceptance": [
                    "Given a valid spec profile, when validation renders Markdown, "
                    "then the target file starts with the spec title."
                ],
            },
        ],
        "relations": [
            {
                "from": "REQ.test.render-markdown",
                "type": "satisfies",
                "to": "GOAL.test.profile-cli",
            }
        ],
        "controlled_terms": [],
        "external_references": [],
        "rendering": {
            "markdown_profile": "agileforge.spec_markdown.v1",
            "rendered_markdown_sha256": None,
        },
    }


def test_cli_writes_success_json_to_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify CLI emits a success envelope to stdout only."""
    app = _FakeApplication()

    rc = main(["project", "list"], application=app)

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert payload["ok"] is True
    assert payload["data"] == {"items": []}
    assert payload["warnings"] == []
    assert payload["errors"] == []
    assert _mapping(payload["meta"])["command"] == "agileforge project list"
    assert app.calls == [("project_list", {})]


def test_spec_profile_schema_command_outputs_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Expose the AgileForge spec profile JSON Schema through the CLI."""
    rc = main(["spec", "profile", "schema"], application=_FakeApplication())

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert payload["ok"] is True
    schema_id = _mapping(_mapping(payload["data"])["schema"])["$id"]
    assert isinstance(schema_id, str)
    assert schema_id.endswith("agileforge.spec.v1.json")
    assert _mapping(payload["meta"])["command"] == "agileforge spec profile schema"


def test_spec_profile_validate_can_render_markdown(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Validate a spec profile JSON file and write its Markdown rendering."""
    spec_path = tmp_path / "spec.json"
    render_path = tmp_path / "spec.md"
    spec_path.write_text(
        json.dumps(_agileforge_spec_profile_payload()),
        encoding="utf-8",
    )

    rc = main(
        [
            "spec",
            "profile",
            "validate",
            "--spec-file",
            str(spec_path),
            "--render-md",
            str(render_path),
        ],
        application=_FakeApplication(),
    )

    payload = _stdout_payload(capsys)
    data = _mapping(payload["data"])
    assert rc == 0
    assert payload["ok"] is True
    assert data["format"] == "agileforge.spec.v1"
    assert str(data["spec_sha256"]).startswith("sha256:")
    assert str(data["rendered_markdown_sha256"]).startswith("sha256:")
    assert render_path.read_text(encoding="utf-8").startswith("# Test Spec")
    assert _mapping(payload["meta"])["command"] == "agileforge spec profile validate"


def test_spec_profile_validate_missing_spec_file_returns_registered_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Return a registered error code when the profile input file is missing."""
    missing_spec_path = tmp_path / "missing-spec.json"

    rc = main(
        [
            "spec",
            "profile",
            "validate",
            "--spec-file",
            str(missing_spec_path),
        ],
        application=_FakeApplication(),
    )

    payload = _stdout_payload(capsys)
    error = _first_mapping(payload["errors"])
    assert rc == INVALID_COMMAND_EXIT_CODE
    assert payload["ok"] is False
    assert error["code"] == ErrorCode.SPEC_FILE_NOT_FOUND.value
    assert _mapping(error["details"])["spec_file"] == str(missing_spec_path.resolve())


def test_spec_profile_validate_invalid_spec_file_returns_registered_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Return a registered error code when profile JSON validation fails."""
    spec_path = tmp_path / "invalid-spec.json"
    spec_path.write_text('{"schema_version": "agileforge.spec.v1"}', encoding="utf-8")

    rc = main(
        [
            "spec",
            "profile",
            "validate",
            "--spec-file",
            str(spec_path),
        ],
        application=_FakeApplication(),
    )

    payload = _stdout_payload(capsys)
    error = _first_mapping(payload["errors"])
    assert rc == INVALID_COMMAND_EXIT_CODE
    assert payload["ok"] is False
    assert error["code"] == ErrorCode.SPEC_FILE_INVALID.value
    assert _mapping(error["details"])["spec_file"] == str(spec_path.resolve())


def test_spec_profile_validate_render_write_failure_returns_invalid_command(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Report Markdown render write failures separately from input validation."""
    spec_path = tmp_path / "spec.json"
    render_path = tmp_path / "render-target"
    render_path.mkdir()
    spec_path.write_text(
        json.dumps(_agileforge_spec_profile_payload()),
        encoding="utf-8",
    )

    rc = main(
        [
            "spec",
            "profile",
            "validate",
            "--spec-file",
            str(spec_path),
            "--render-md",
            str(render_path),
        ],
        application=_FakeApplication(),
    )

    payload = _stdout_payload(capsys)
    error = _first_mapping(payload["errors"])
    assert rc == INVALID_COMMAND_EXIT_CODE
    assert payload["ok"] is False
    assert error["code"] == ErrorCode.INVALID_COMMAND.value
    assert _mapping(error["details"])["render_md"] == str(render_path.resolve())


def test_cli_redirects_application_stdout_away_from_json_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify lower-layer stdout noise is suppressed, not moved to stderr."""
    app = _FakeApplication()

    def noisy_project_list() -> JsonObject:
        sys.stdout.write("LiteLLM completion() model=openai/example\n")
        app.calls.append(("project_list", {}))
        return {"ok": True, "data": {"items": []}, "warnings": [], "errors": []}

    cast("Any", app).project_list = noisy_project_list

    rc = main(["project", "list"], application=app)

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.startswith("{")
    assert "LiteLLM" not in captured.out
    assert captured.err == ""
    payload = cast("JsonObject", json.loads(captured.out))
    assert payload["ok"] is True
    assert app.calls == [("project_list", {})]


def test_cli_wraps_success_source_fingerprint_in_meta(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Expose successful result source fingerprints in envelope metadata."""
    source_fingerprint = "sha256:" + "b" * 64
    app = _FakeApplication()

    def project_list_with_source() -> JsonObject:
        app.calls.append(("project_list", {}))
        return {
            "ok": True,
            "data": {"items": [], "source_fingerprint": source_fingerprint},
            "warnings": [],
            "errors": [],
        }

    cast("Any", app).project_list = project_list_with_source

    rc = main(["project", "list"], application=app)

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert _mapping(payload["data"])["source_fingerprint"] == source_fingerprint
    assert _mapping(payload["meta"])["source_fingerprint"] == source_fingerprint


def test_cli_routes_project_create_to_application(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify project create routes mutation args to the application facade."""
    app = _FakeApplication()

    rc = main(
        [
            "project",
            "create",
            "--name",
            "CLI Project",
            "--spec-file",
            "specs/app.md",
            "--idempotency-key",
            "create-cli-project-001",
            "--changed-by",
            "test-agent",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert _mapping(payload["meta"])["command"] == "agileforge project create"
    assert app.calls == [
        (
            "project_create",
            {
                "name": "CLI Project",
                "spec_file": "specs/app.md",
                "idempotency_key": "create-cli-project-001",
                "dry_run": False,
                "dry_run_id": None,
                "correlation_id": None,
                "changed_by": "test-agent",
            },
        )
    ]


def test_cli_routes_project_create_dry_run_without_idempotency_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify project create dry-run routes without consuming idempotency."""
    app = _FakeApplication()

    rc = main(
        [
            "project",
            "create",
            "--name",
            "CLI Project",
            "--spec-file",
            "specs/app.md",
            "--dry-run",
            "--dry-run-id",
            "preview-001",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert _mapping(payload["meta"])["command"] == "agileforge project create"
    assert app.calls == [
        (
            "project_create",
            {
                "name": "CLI Project",
                "spec_file": "specs/app.md",
                "idempotency_key": None,
                "dry_run": True,
                "dry_run_id": "preview-001",
                "correlation_id": None,
                "changed_by": "cli-agent",
            },
        )
    ]


@pytest.mark.parametrize(
    "argv",
    [
        [
            "project",
            "create",
            "--name",
            "CLI Project",
            "--spec-file",
            "specs/app.md",
            "--dry-run",
            "--dry-run-id",
            "preview-001",
            "--idempotency-key",
            "create-001",
        ],
    ],
)
def test_cli_rejects_invalid_project_create_idempotency_args(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify project create enforces dry-run/idempotency CLI contract."""
    app = _FakeApplication()

    rc = main(argv, application=app)

    payload = _stdout_payload(capsys)
    assert rc == INVALID_COMMAND_EXIT_CODE
    assert _mapping(payload["meta"])["command"] == "agileforge project create"
    assert _first_mapping(payload["errors"])["code"] == "INVALID_COMMAND"
    assert app.calls == []


def test_cli_generates_auto_idempotency_key_when_omitted() -> None:
    """Verify project create automatically generates an idempotency key if omitted."""
    app = _FakeApplication()

    rc = main(
        [
            "project",
            "create",
            "--name",
            "CLI Project",
            "--spec-file",
            "specs/app.md",
        ],
        application=app,
    )

    assert rc == 0
    assert len(app.calls) == 1
    call_args = app.calls[0][1]
    key = call_args["idempotency_key"]
    assert isinstance(key, str)
    assert key.startswith("auto-")


def test_cli_routes_project_setup_retry_to_application(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify project setup retry routes stale guards and recovery id."""
    app = _FakeApplication()

    rc = main(
        [
            "project",
            "setup",
            "retry",
            "--project-id",
            str(PROJECT_ID),
            "--spec-file",
            "specs/app.md",
            "--expected-state",
            "SETUP_REQUIRED",
            "--expected-context-fingerprint",
            "ctx123",
            "--recovery-mutation-event-id",
            "42",
            "--idempotency-key",
            "retry-cli-project-001",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert _mapping(payload["meta"])["command"] == "agileforge project setup retry"
    assert app.calls == [
        (
            "project_setup_retry",
            {
                "project_id": PROJECT_ID,
                "spec_file": "specs/app.md",
                "expected_state": "SETUP_REQUIRED",
                "expected_context_fingerprint": "ctx123",
                "recovery_mutation_event_id": 42,
                "idempotency_key": "retry-cli-project-001",
                "dry_run": False,
                "dry_run_id": None,
                "correlation_id": None,
                "changed_by": "cli-agent",
            },
        )
    ]


def test_cli_routes_project_setup_retry_dry_run_without_idempotency_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify setup retry dry-run routes without consuming idempotency."""
    app = _FakeApplication()

    rc = main(
        [
            "project",
            "setup",
            "retry",
            "--project-id",
            str(PROJECT_ID),
            "--spec-file",
            "specs/app.md",
            "--expected-state",
            "SETUP_REQUIRED",
            "--expected-context-fingerprint",
            "ctx123",
            "--recovery-mutation-event-id",
            "42",
            "--dry-run",
            "--dry-run-id",
            "retry-preview-001",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert _mapping(payload["meta"])["command"] == "agileforge project setup retry"
    assert app.calls == [
        (
            "project_setup_retry",
            {
                "project_id": PROJECT_ID,
                "spec_file": "specs/app.md",
                "expected_state": "SETUP_REQUIRED",
                "expected_context_fingerprint": "ctx123",
                "recovery_mutation_event_id": 42,
                "idempotency_key": None,
                "dry_run": True,
                "dry_run_id": "retry-preview-001",
                "correlation_id": None,
                "changed_by": "cli-agent",
            },
        )
    ]


def test_cli_rejects_project_setup_retry_dry_run_with_idempotency_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify setup retry dry-run rejects idempotency keys."""
    app = _FakeApplication()

    rc = main(
        [
            "project",
            "setup",
            "retry",
            "--project-id",
            str(PROJECT_ID),
            "--spec-file",
            "specs/app.md",
            "--expected-state",
            "SETUP_REQUIRED",
            "--expected-context-fingerprint",
            "ctx123",
            "--dry-run",
            "--dry-run-id",
            "retry-preview-001",
            "--idempotency-key",
            "retry-001",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == INVALID_COMMAND_EXIT_CODE
    assert _mapping(payload["meta"])["command"] == "agileforge project setup retry"
    assert _first_mapping(payload["errors"])["code"] == "INVALID_COMMAND"
    assert app.calls == []


def test_cli_routes_authority_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify authority status command routes project id."""
    app = _FakeApplication()

    rc = main(
        ["authority", "status", "--project-id", str(PROJECT_ID)],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == 0
    data = _mapping(payload["data"])
    assert data["project_id"] == PROJECT_ID
    assert data["status"] == "missing"
    assert _mapping(payload["meta"])["command"] == "agileforge authority status"
    assert app.calls == [("authority_status", {"project_id": PROJECT_ID})]


@pytest.mark.parametrize(
    ("argv", "expected_call", "expected_command"),
    [
        (
            [
                "vision",
                "generate",
                "--project-id",
                str(PROJECT_ID),
                "--input",
                "clarify target users",
            ],
            (
                "vision_generate",
                {"project_id": PROJECT_ID, "user_input": "clarify target users"},
            ),
            "agileforge vision generate",
        ),
        (
            ["vision", "history", "--project-id", str(PROJECT_ID)],
            ("vision_history", {"project_id": PROJECT_ID}),
            "agileforge vision history",
        ),
        (
            ["vision", "save", "--project-id", str(PROJECT_ID)],
            ("vision_save", {"project_id": PROJECT_ID}),
            "agileforge vision save",
        ),
    ],
)
def test_cli_routes_vision_commands(
    argv: list[str],
    expected_call: tuple[str, dict[str, object]],
    expected_command: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify Vision phase commands route through the agent CLI."""
    app = _FakeApplication()

    rc = main(argv, application=app)

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert _mapping(payload["meta"])["command"] == expected_command
    assert app.calls == [expected_call]


@pytest.mark.parametrize(
    ("argv", "expected_call", "expected_command"),
    [
        (
            [
                "backlog",
                "generate",
                "--project-id",
                str(PROJECT_ID),
                "--input",
                "split MVP and future bets",
            ],
            (
                "backlog_generate",
                {"project_id": PROJECT_ID, "user_input": "split MVP and future bets"},
            ),
            "agileforge backlog generate",
        ),
        (
            [
                "backlog",
                "preview",
                "--project-id",
                str(PROJECT_ID),
                "--input",
                "brownfield preview",
            ],
            (
                "backlog_preview",
                {"project_id": PROJECT_ID, "user_input": "brownfield preview"},
            ),
            "agileforge backlog preview",
        ),
        (
            [
                "backlog",
                "refine-preview",
                "--project-id",
                str(PROJECT_ID),
                "--source-attempt-id",
                "backlog-attempt-1",
                "--operations-file",
                "fixtures/operations.json",
            ],
            (
                "backlog_refine_preview",
                {
                    "project_id": PROJECT_ID,
                    "source_attempt_id": "backlog-attempt-1",
                    "operations_file": "fixtures/operations.json",
                    "source_artifact": None,
                    "user_input": None,
                },
            ),
            "agileforge backlog refine-preview",
        ),
        (
            [
                "backlog",
                "refine-record",
                "--project-id",
                str(PROJECT_ID),
                "--source-attempt-id",
                "backlog-attempt-1",
                "--operations-file",
                "fixtures/operations.json",
                "--expected-source-fingerprint",
                "sha256:" + "b" * 64,
                "--expected-state",
                "SPRINT_COMPLETE",
                "--idempotency-key",
                "refine-record-1",
            ],
            (
                "backlog_refine_record",
                {
                    "project_id": PROJECT_ID,
                    "source_attempt_id": "backlog-attempt-1",
                    "operations_file": "fixtures/operations.json",
                    "expected_source_fingerprint": "sha256:" + "b" * 64,
                    "expected_state": "SPRINT_COMPLETE",
                    "idempotency_key": "refine-record-1",
                    "approval_id": None,
                },
            ),
            "agileforge backlog refine-record",
        ),
        (
            [
                "backlog",
                "approve",
                "--project-id",
                str(PROJECT_ID),
                "--source-attempt-id",
                "backlog-attempt-1",
                "--operation-set-fingerprint",
                "sha256:" + "c" * 64,
                "--approved-artifact-fingerprint",
                "sha256:" + "d" * 64,
                "--approved-operation-id",
                "op-2",
                "--approved-operation-id",
                "op-1",
                "--idempotency-key",
                "approve-refinement-1",
            ],
            (
                "backlog_approve",
                {
                    "project_id": PROJECT_ID,
                    "source_attempt_id": "backlog-attempt-1",
                    "attempt_id": None,
                    "operation_set_fingerprint": "sha256:" + "c" * 64,
                    "approved_artifact_fingerprint": "sha256:" + "d" * 64,
                    "approved_operation_ids": ["op-2", "op-1"],
                    "idempotency_key": "approve-refinement-1",
                },
            ),
            "agileforge backlog approve",
        ),
        (
            [
                "backlog",
                "refine-import",
                "--project-id",
                str(PROJECT_ID),
                "--source-artifact",
                "fixtures/source.json",
                "--edited-file",
                "fixtures/edited.json",
                "--expected-source-fingerprint",
                "sha256:" + "e" * 64,
                "--idempotency-key",
                "refine-import-1",
            ],
            (
                "backlog_refine_import",
                {
                    "project_id": PROJECT_ID,
                    "source_artifact": "fixtures/source.json",
                    "edited_file": "fixtures/edited.json",
                    "expected_source_fingerprint": "sha256:" + "e" * 64,
                    "idempotency_key": "refine-import-1",
                },
            ),
            "agileforge backlog refine-import",
        ),
        (
            ["backlog", "history", "--project-id", str(PROJECT_ID)],
            ("backlog_history", {"project_id": PROJECT_ID}),
            "agileforge backlog history",
        ),
        (
            [
                "backlog",
                "save",
                "--project-id",
                str(PROJECT_ID),
                "--attempt-id",
                "backlog-attempt-1",
                "--expected-artifact-fingerprint",
                "sha256:" + "a" * 64,
                "--expected-state",
                "BACKLOG_REVIEW",
                "--idempotency-key",
                "save-backlog-1",
            ],
            (
                "backlog_save",
                {
                    "project_id": PROJECT_ID,
                    "attempt_id": "backlog-attempt-1",
                    "expected_artifact_fingerprint": "sha256:" + "a" * 64,
                    "expected_state": "BACKLOG_REVIEW",
                    "idempotency_key": "save-backlog-1",
                },
            ),
            "agileforge backlog save",
        ),
        (
            [
                "backlog",
                "reset-active",
                "--project-id",
                str(PROJECT_ID),
                "--attempt-id",
                "backlog-attempt-1",
                "--expected-artifact-fingerprint",
                "sha256:" + "f" * 64,
                "--expected-state",
                "BACKLOG_REVIEW",
                "--reset-reason",
                "pre-brownfield reset",
                "--archive-all-active-stories",
                "--idempotency-key",
                "reset-active-1",
            ],
            (
                "backlog_reset_active",
                {
                    "project_id": PROJECT_ID,
                    "attempt_id": "backlog-attempt-1",
                    "expected_artifact_fingerprint": "sha256:" + "f" * 64,
                    "expected_state": "BACKLOG_REVIEW",
                    "reset_reason": "pre-brownfield reset",
                    "archive_all_active_stories": True,
                    "idempotency_key": "reset-active-1",
                },
            ),
            "agileforge backlog reset-active",
        ),
        (
            [
                "backlog",
                "reconcile",
                "--project-id",
                str(PROJECT_ID),
                "--idempotency-key",
                "reconcile-backlog-1",
            ],
            (
                "backlog_reconcile",
                {
                    "project_id": PROJECT_ID,
                    "idempotency_key": "reconcile-backlog-1",
                },
            ),
            "agileforge backlog reconcile",
        ),
    ],
)
def test_cli_routes_backlog_commands(
    argv: list[str],
    expected_call: tuple[str, dict[str, object]],
    expected_command: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify Backlog phase commands route through the agent CLI."""
    app = _FakeApplication()

    rc = main(argv, application=app)

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert _mapping(payload["meta"])["command"] == expected_command
    assert app.calls == [expected_call]


def test_backlog_reset_active_routes_to_application(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify reset-active routes guarded args to the application facade."""
    app = _FakeApplication()

    rc = main(
        [
            "backlog",
            "reset-active",
            "--project-id",
            str(PROJECT_ID),
            "--attempt-id",
            "backlog-attempt-1",
            "--expected-artifact-fingerprint",
            "sha256:" + "f" * 64,
            "--expected-state",
            "BACKLOG_REVIEW",
            "--reset-reason",
            "pre-brownfield reset",
            "--archive-all-active-stories",
            "--idempotency-key",
            "reset-active-1",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert _mapping(payload["meta"])["command"] == "agileforge backlog reset-active"
    assert app.calls == [
        (
            "backlog_reset_active",
            {
                "project_id": PROJECT_ID,
                "attempt_id": "backlog-attempt-1",
                "expected_artifact_fingerprint": "sha256:" + "f" * 64,
                "expected_state": "BACKLOG_REVIEW",
                "reset_reason": "pre-brownfield reset",
                "archive_all_active_stories": True,
                "idempotency_key": "reset-active-1",
            },
        )
    ]


def test_backlog_reset_active_cli_routes_expected_arguments(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI forwards reset-active guarded arguments to application."""
    app = _FakeApplication()

    rc = main(
        [
            "backlog",
            "reset-active",
            "--project-id",
            str(PROJECT_ID),
            "--attempt-id",
            "backlog-attempt-12",
            "--expected-artifact-fingerprint",
            "sha256:artifact",
            "--expected-state",
            "BACKLOG_REVIEW",
            "--reset-reason",
            "pre-brownfield backlog reset",
            "--archive-all-active-stories",
            "--idempotency-key",
            "reset-active-cli-1",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert _mapping(payload["meta"])["command"] == "agileforge backlog reset-active"
    assert app.calls == [
        (
            "backlog_reset_active",
            {
                "project_id": PROJECT_ID,
                "attempt_id": "backlog-attempt-12",
                "expected_artifact_fingerprint": "sha256:artifact",
                "expected_state": "BACKLOG_REVIEW",
                "reset_reason": "pre-brownfield backlog reset",
                "archive_all_active_stories": True,
                "idempotency_key": "reset-active-cli-1",
            },
        )
    ]


def test_cli_routes_as_built_assess_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify As-Built Assessment routes through the agent CLI."""
    app = _FakeApplication()

    rc = main(
        [
            "as-built",
            "assess",
            "--project-id",
            str(PROJECT_ID),
            "--repo-path",
            "/repo",
            "--spec-file",
            "/repo/spec.md",
            "--spec-mode",
            "unknown",
            "--user-input",
            "brownfield check",
            "--idempotency-key",
            "as-built-1",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert _mapping(payload["meta"])["command"] == "agileforge as-built assess"
    assert app.calls == [
        (
            "as_built_assess",
            {
                "project_id": PROJECT_ID,
                "repo_path": "/repo",
                "spec_file": "/repo/spec.md",
                "spec_mode": "unknown",
                "user_input": "brownfield check",
                "idempotency_key": "as-built-1",
            },
        )
    ]


@pytest.mark.parametrize(
    ("argv", "expected_call", "expected_command"),
    [
        (
            [
                "roadmap",
                "generate",
                "--project-id",
                str(PROJECT_ID),
                "--input",
                "split foundation and decision UX",
            ],
            (
                "roadmap_generate",
                {
                    "project_id": PROJECT_ID,
                    "user_input": "split foundation and decision UX",
                },
            ),
            "agileforge roadmap generate",
        ),
        (
            ["roadmap", "history", "--project-id", str(PROJECT_ID)],
            ("roadmap_history", {"project_id": PROJECT_ID}),
            "agileforge roadmap history",
        ),
        (
            [
                "roadmap",
                "save",
                "--project-id",
                str(PROJECT_ID),
                "--attempt-id",
                "roadmap-attempt-1",
                "--expected-artifact-fingerprint",
                "sha256:" + "a" * 64,
                "--expected-state",
                "ROADMAP_REVIEW",
                "--idempotency-key",
                "save-roadmap-1",
            ],
            (
                "roadmap_save",
                {
                    "project_id": PROJECT_ID,
                    "attempt_id": "roadmap-attempt-1",
                    "expected_artifact_fingerprint": "sha256:" + "a" * 64,
                    "expected_state": "ROADMAP_REVIEW",
                    "idempotency_key": "save-roadmap-1",
                },
            ),
            "agileforge roadmap save",
        ),
    ],
)
def test_cli_routes_roadmap_commands(
    argv: list[str],
    expected_call: tuple[str, dict[str, object]],
    expected_command: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify Roadmap phase commands route through the agent CLI."""
    app = _FakeApplication()

    rc = main(argv, application=app)

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert _mapping(payload["meta"])["command"] == expected_command
    assert app.calls == [expected_call]


@pytest.mark.parametrize(
    ("argv", "expected_call", "expected_command"),
    [
        (
            ["story", "pending", "--project-id", str(PROJECT_ID)],
            ("story_pending", {"project_id": PROJECT_ID}),
            "agileforge story pending",
        ),
        (
            [
                "story",
                "generate",
                "--project-id",
                str(PROJECT_ID),
                "--parent-requirement",
                "REQ.checkout",
                "--input",
                "focus payment errors",
                "--force-feedback",
            ],
            (
                "story_generate",
                {
                    "project_id": PROJECT_ID,
                    "parent_requirement": "REQ.checkout",
                    "user_input": "focus payment errors",
                    "force_feedback": True,
                },
            ),
            "agileforge story generate",
        ),
        (
            [
                "story",
                "retry",
                "--project-id",
                str(PROJECT_ID),
                "--parent-requirement",
                "REQ.checkout",
            ],
            (
                "story_retry",
                {
                    "project_id": PROJECT_ID,
                    "parent_requirement": "REQ.checkout",
                },
            ),
            "agileforge story retry",
        ),
        (
            [
                "story",
                "history",
                "--project-id",
                str(PROJECT_ID),
                "--parent-requirement",
                "REQ.checkout",
            ],
            (
                "story_history",
                {
                    "project_id": PROJECT_ID,
                    "parent_requirement": "REQ.checkout",
                },
            ),
            "agileforge story history",
        ),
        (
            [
                "story",
                "save",
                "--project-id",
                str(PROJECT_ID),
                "--parent-requirement",
                "REQ.checkout",
                "--attempt-id",
                "story-attempt-1",
                "--expected-artifact-fingerprint",
                "sha256:" + "a" * 64,
                "--expected-state",
                "STORY_REVIEW",
                "--idempotency-key",
                "save-story-1",
            ],
            (
                "story_save",
                {
                    "project_id": PROJECT_ID,
                    "parent_requirement": "REQ.checkout",
                    "attempt_id": "story-attempt-1",
                    "expected_artifact_fingerprint": "sha256:" + "a" * 64,
                    "expected_state": "STORY_REVIEW",
                    "idempotency_key": "save-story-1",
                },
            ),
            "agileforge story save",
        ),
        (
            [
                "story",
                "complete",
                "--project-id",
                str(PROJECT_ID),
                "--expected-state",
                "STORY_PERSISTENCE",
                "--idempotency-key",
                "complete-story-1",
            ],
            (
                "story_complete",
                {
                    "project_id": PROJECT_ID,
                    "expected_state": "STORY_PERSISTENCE",
                    "idempotency_key": "complete-story-1",
                },
            ),
            "agileforge story complete",
        ),
        (
            [
                "story",
                "complete",
                "--project-id",
                str(PROJECT_ID),
                "--expected-state",
                "STORY_PERSISTENCE",
                "--idempotency-key",
                "complete-story-milestone-0",
                "--scope",
                "milestone",
                "--scope-id",
                "milestone_0",
            ],
            (
                "story_complete",
                {
                    "project_id": PROJECT_ID,
                    "expected_state": "STORY_PERSISTENCE",
                    "idempotency_key": "complete-story-milestone-0",
                    "scope": "milestone",
                    "scope_id": "milestone_0",
                },
            ),
            "agileforge story complete",
        ),
        (
            [
                "story",
                "complete",
                "--project-id",
                str(PROJECT_ID),
                "--expected-state",
                "STORY_PERSISTENCE",
                "--idempotency-key",
                "complete-story-selection",
                "--scope",
                "selection",
                "--parent-requirement",
                "Technology and Model Research Spike",
                "--parent-requirement",
                "Python Project Scaffold and uv Management Setup",
            ],
            (
                "story_complete",
                {
                    "project_id": PROJECT_ID,
                    "expected_state": "STORY_PERSISTENCE",
                    "idempotency_key": "complete-story-selection",
                    "scope": "selection",
                    "scope_id": None,
                    "parent_requirements": [
                        "Technology and Model Research Spike",
                        "Python Project Scaffold and uv Management Setup",
                    ],
                },
            ),
            "agileforge story complete",
        ),
        (
            [
                "story",
                "reopen",
                "--project-id",
                str(PROJECT_ID),
                "--parent-requirement",
                "REQ.checkout",
                "--expected-state",
                "SPRINT_SETUP",
                "--idempotency-key",
                "reopen-story-1",
            ],
            (
                "story_reopen",
                {
                    "project_id": PROJECT_ID,
                    "parent_requirement": "REQ.checkout",
                    "expected_state": "SPRINT_SETUP",
                    "idempotency_key": "reopen-story-1",
                },
            ),
            "agileforge story reopen",
        ),
        (
            [
                "story",
                "repair-readiness",
                "--project-id",
                str(PROJECT_ID),
                "--expected-state",
                "SPRINT_SETUP",
                "--idempotency-key",
                "repair-story-readiness-2",
            ],
            (
                "story_repair_readiness",
                {
                    "project_id": PROJECT_ID,
                    "expected_state": "SPRINT_SETUP",
                    "idempotency_key": "repair-story-readiness-2",
                },
            ),
            "agileforge story repair-readiness",
        ),
        (
            [
                "story",
                "dependencies",
                "inspect",
                "--project-id",
                str(PROJECT_ID),
            ],
            ("story_dependencies_inspect", {"project_id": PROJECT_ID}),
            "agileforge story dependencies inspect",
        ),
        (
            [
                "story",
                "dependencies",
                "propose",
                "--project-id",
                str(PROJECT_ID),
                "--expected-state",
                "SPRINT_SETUP",
                "--idempotency-key",
                "dep-propose-1",
                "--manual-edge",
                "85:67",
                "--manual-edge",
                "85:68",
            ],
            (
                "story_dependencies_propose",
                {
                    "project_id": PROJECT_ID,
                    "expected_state": "SPRINT_SETUP",
                    "idempotency_key": "dep-propose-1",
                    "manual_edges": ["85:67", "85:68"],
                },
            ),
            "agileforge story dependencies propose",
        ),
        (
            [
                "story",
                "dependencies",
                "apply",
                "--project-id",
                str(PROJECT_ID),
                "--attempt-id",
                "story-dependencies-test",
                "--expected-artifact-fingerprint",
                "sha256:" + "a" * 64,
                "--expected-state",
                "SPRINT_SETUP",
                "--idempotency-key",
                "dep-apply-1",
            ],
            (
                "story_dependencies_apply",
                {
                    "project_id": PROJECT_ID,
                    "attempt_id": "story-dependencies-test",
                    "expected_artifact_fingerprint": "sha256:" + "a" * 64,
                    "expected_state": "SPRINT_SETUP",
                    "idempotency_key": "dep-apply-1",
                },
            ),
            "agileforge story dependencies apply",
        ),
    ],
)
def test_cli_routes_story_phase_commands(
    argv: list[str],
    expected_call: tuple[str, dict[str, object]],
    expected_command: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify Story phase commands route through the agent CLI."""
    app = _FakeApplication()

    rc = main(argv, application=app)

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert _mapping(payload["meta"])["command"] == expected_command
    assert app.calls == [expected_call]


def test_cli_routes_story_complete_selection_scope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Story complete routes selected parent requirements through the CLI."""
    app = _FakeApplication()

    rc = main(
        [
            "story",
            "complete",
            "--project-id",
            str(PROJECT_ID),
            "--expected-state",
            "STORY_PERSISTENCE",
            "--idempotency-key",
            "complete-story-selection",
            "--scope",
            "selection",
            "--parent-requirement",
            "Technology and Model Research Spike",
            "--parent-requirement",
            "Python Project Scaffold and uv Management Setup",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert _mapping(payload["meta"])["command"] == "agileforge story complete"
    assert app.calls == [
        (
            "story_complete",
            {
                "project_id": PROJECT_ID,
                "expected_state": "STORY_PERSISTENCE",
                "idempotency_key": "complete-story-selection",
                "scope": "selection",
                "scope_id": None,
                "parent_requirements": [
                    "Technology and Model Research Spike",
                    "Python Project Scaffold and uv Management Setup",
                ],
            },
        )
    ]


def test_story_reopen_cli_routes_guard_fields() -> None:
    """Story reopen routes guarded fields to the application facade."""
    app = _FakeApplication()

    exit_code = main(
        [
            "story",
            "reopen",
            "--project-id",
            "7",
            "--parent-requirement",
            "Requirement A",
            "--expected-state",
            "SPRINT_SETUP",
            "--idempotency-key",
            "reopen-story-7-a",
        ],
        application=app,
    )

    assert exit_code == 0
    assert app.calls[-1] == (
        "story_reopen",
        {
            "project_id": 7,
            "parent_requirement": "Requirement A",
            "expected_state": "SPRINT_SETUP",
            "idempotency_key": "reopen-story-7-a",
        },
    )


def test_story_repair_readiness_cli_routes_guard_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Story readiness repair routes guarded fields to the application facade."""
    app = _FakeApplication()

    exit_code = main(
        [
            "story",
            "repair-readiness",
            "--project-id",
            "7",
            "--expected-state",
            "SPRINT_SETUP",
            "--idempotency-key",
            "repair-story-readiness-7",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert exit_code == 0
    assert payload["data"]["repair_result"] == {
        "repaired_count": 1,
        "story_ids": [66],
    }
    assert app.calls[-1] == (
        "story_repair_readiness",
        {
            "project_id": 7,
            "expected_state": "SPRINT_SETUP",
            "idempotency_key": "repair-story-readiness-7",
        },
    )


def test_story_generate_cli_flattens_phase_data(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Story generate CLI prints flattened phase service data."""
    app = _FakeApplication()
    app.results["story_generate"] = {
        "ok": True,
        "data": {
            "fsm_state": "STORY_REVIEW",
            "parent_requirement": "Requirement A",
            "output_artifact": {"parent_requirement": "Requirement A"},
            "current_draft": {"attempt_id": "attempt-1"},
            "save": {"available": True},
            "retry": {"available": False},
            "resolution": {"available": False},
        },
        "warnings": [],
        "errors": [],
    }

    exit_code = main(
        [
            "story",
            "generate",
            "--project-id",
            "7",
            "--parent-requirement",
            "Requirement A",
        ],
        application=app,
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["output_artifact"]["parent_requirement"] == "Requirement A"
    assert "data" not in payload["data"]


def test_sprint_generate_cli_routes_generation_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Sprint generate CLI routes selected stories and capacity options."""
    app = _FakeApplication()

    exit_code = main(
        [
            "sprint",
            "generate",
            "--project-id",
            "7",
            "--selected-story-ids",
            "66,85",
            "--max-story-points",
            "8",
            "--input",
            "Focus on live command hardening.",
            "--no-task-decomposition",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert exit_code == 0
    assert payload["data"]["fsm_state"] == "SPRINT_DRAFT"
    assert app.calls[-1] == (
        "sprint_generate",
        {
            "project_id": 7,
            "user_input": "Focus on live command hardening.",
            "selected_story_ids": [66, 85],
            "max_story_points": 8,
            "include_task_decomposition": False,
        },
    )


@pytest.mark.parametrize(
    "removed_arg",
    ["--team-velocity-assumption", "--sprint-duration-days"],
)
def test_sprint_generate_cli_rejects_removed_capacity_args(
    capsys: pytest.CaptureFixture[str],
    removed_arg: str,
) -> None:
    """Sprint generate no longer accepts velocity or calendar capacity flags."""
    app = _FakeApplication()

    rc = main(
        [
            "sprint",
            "generate",
            "--project-id",
            "7",
            removed_arg,
            "10" if removed_arg == "--sprint-duration-days" else "High",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == INVALID_COMMAND_EXIT_CODE
    assert payload["ok"] is False
    assert _mapping(payload["errors"][0])["code"] == ErrorCode.INVALID_COMMAND.value
    assert app.calls == []


@pytest.mark.parametrize("max_story_points", ["0", "-1"])
def test_sprint_generate_cli_rejects_non_positive_max_story_points(
    capsys: pytest.CaptureFixture[str],
    max_story_points: str,
) -> None:
    """Sprint generate requires positive story point capacity overrides."""
    app = _FakeApplication()

    rc = main(
        [
            "sprint",
            "generate",
            "--project-id",
            "7",
            "--max-story-points",
            max_story_points,
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == INVALID_COMMAND_EXIT_CODE
    assert payload["ok"] is False
    assert _mapping(payload["errors"][0])["code"] == ErrorCode.INVALID_COMMAND.value
    assert "--max-story-points" in _mapping(payload["errors"][0])["message"]
    assert app.calls == []


def test_sprint_history_cli_routes_to_application(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Sprint history CLI routes to the application facade."""
    app = _FakeApplication()

    exit_code = main(["sprint", "history", "--project-id", "7"], application=app)

    payload = _stdout_payload(capsys)
    assert exit_code == 0
    assert payload["data"]["items"] == []
    assert app.calls[-1] == ("sprint_history", {"project_id": 7})


def test_sprint_metrics_cli_routes_and_prints_summary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Sprint metrics CLI routes to the application facade and summarizes."""
    app = _FakeApplication()

    exit_code = main(["sprint", "metrics", "--project-id", "7"], application=app)

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["data"]["status"] == "ready"
    assert (
        payload["data"]["recommendation"]["recommended_next_sprint_points"]
        == RECOMMENDED_SPRINT_POINTS
    )
    assert app.calls[-1] == ("sprint_metrics", {"project_id": 7})
    assert "Sprint metrics" in captured.err
    assert "project_id: 7" in captured.err
    assert "completed_sprint_count: 4" in captured.err
    assert "recommended_next_sprint_points: 5" in captured.err


def test_sprint_save_cli_requires_review_guards(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Sprint save CLI routes guard fields to the application facade."""
    app = _FakeApplication()

    exit_code = main(
        [
            "sprint",
            "save",
            "--project-id",
            "7",
            "--team-name",
            "Delivery",
            "--attempt-id",
            "sprint-attempt-1",
            "--expected-artifact-fingerprint",
            "sha256:abc",
            "--expected-state",
            "SPRINT_DRAFT",
            "--idempotency-key",
            "save-sprint-7-001",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert exit_code == 0
    assert payload["data"]["fsm_state"] == "SPRINT_PERSISTENCE"
    assert app.calls[-1] == (
        "sprint_save",
        {
            "project_id": 7,
            "team_name": "Delivery",
            "attempt_id": "sprint-attempt-1",
            "expected_artifact_fingerprint": "sha256:abc",
            "expected_state": "SPRINT_DRAFT",
            "idempotency_key": "save-sprint-7-001",
        },
    )


def test_sprint_save_cli_rejects_removed_start_date_arg(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Sprint save no longer accepts caller-supplied calendar dates."""
    app = _FakeApplication()

    rc = main(
        [
            "sprint",
            "save",
            "--project-id",
            "7",
            "--team-name",
            "Delivery",
            "--sprint-start-date",
            "2026-05-25",
            "--attempt-id",
            "sprint-attempt-1",
            "--expected-artifact-fingerprint",
            "sha256:abc",
            "--expected-state",
            "SPRINT_DRAFT",
            "--idempotency-key",
            "save-sprint-7-001",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == INVALID_COMMAND_EXIT_CODE
    assert payload["ok"] is False
    assert _mapping(payload["errors"][0])["code"] == ErrorCode.INVALID_COMMAND.value
    assert app.calls == []


@pytest.mark.parametrize("idempotency_key", ["", "   "])
def test_sprint_save_cli_rejects_blank_idempotency_key(
    capsys: pytest.CaptureFixture[str],
    idempotency_key: str,
) -> None:
    """Sprint save CLI requires a non-blank idempotency key."""
    app = _FakeApplication()

    rc = main(
        [
            "sprint",
            "save",
            "--project-id",
            "7",
            "--team-name",
            "Delivery",
            "--attempt-id",
            "sprint-attempt-1",
            "--expected-artifact-fingerprint",
            "sha256:abc",
            "--expected-state",
            "SPRINT_DRAFT",
            "--idempotency-key",
            idempotency_key,
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == INVALID_COMMAND_EXIT_CODE
    assert payload["ok"] is False
    assert _mapping(payload["errors"][0])["code"] == ErrorCode.INVALID_COMMAND.value
    assert app.calls == []


def test_sprint_start_cli_requires_expected_state_and_idempotency(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Sprint start CLI routes guarded activation fields."""
    app = _FakeApplication()

    exit_code = main(
        [
            "sprint",
            "start",
            "--project-id",
            "7",
            "--expected-state",
            "SPRINT_PERSISTENCE",
            "--idempotency-key",
            "start-sprint-7-001",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert exit_code == 0
    assert payload["data"]["fsm_state"] == "SPRINT_VIEW"
    assert app.calls[-1] == (
        "sprint_start",
        {
            "project_id": 7,
            "sprint_id": None,
            "expected_state": "SPRINT_PERSISTENCE",
            "idempotency_key": "start-sprint-7-001",
        },
    )


def test_sprint_status_and_tasks_cli_route_optional_sprint_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Sprint status/tasks CLI allow the runner to resolve the active sprint."""
    resolved_sprint_id = 11
    app = _FakeApplication()

    status_exit = main(["sprint", "status", "--project-id", "7"], application=app)
    tasks_exit = main(["sprint", "tasks", "--project-id", "7"], application=app)

    assert status_exit == 0
    assert tasks_exit == 0
    outputs = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    assert outputs[0]["data"]["sprint_id"] == resolved_sprint_id
    assert outputs[1]["data"]["tasks"] == []
    assert app.calls[-2:] == [
        ("sprint_status", {"project_id": 7, "sprint_id": None}),
        ("sprint_tasks", {"project_id": 7, "sprint_id": None}),
    ]


def test_sprint_task_cli_routes_ticket_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Sprint task CLI exposes agent-native ticket reads and guarded updates."""
    app = _FakeApplication()

    next_exit = main(["sprint", "task", "next", "--project-id", "7"], application=app)
    show_exit = main(
        ["sprint", "task", "show", "--project-id", "7", "--task-id", "123"],
        application=app,
    )
    history_exit = main(
        ["sprint", "task", "history", "--project-id", "7", "--task-id", "123"],
        application=app,
    )
    update_exit = main(
        [
            "sprint",
            "task",
            "update",
            "--project-id",
            "7",
            "--task-id",
            "123",
            "--status",
            "Done",
            "--expected-status",
            "In Progress",
            "--expected-task-fingerprint",
            "sha256:abc",
            "--idempotency-key",
            "task-update-123-001",
            "--outcome-summary",
            "Implemented the task.",
            "--artifact-ref",
            "scripts/run_live_round.py",
            "--artifact-ref",
            "tests/test_live_budget.py",
            "--checklist-result",
            "fully_met",
            "--validation-summary",
            "uv run pytest tests/test_live_budget.py -q",
            "--notes",
            "No known gaps.",
        ],
        application=app,
    )

    outputs = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    assert [next_exit, show_exit, history_exit, update_exit] == [0, 0, 0, 0]
    assert [payload["ok"] for payload in outputs] == [True, True, True, True]
    assert app.calls[-4:] == [
        ("sprint_task_next", {"project_id": 7, "sprint_id": None}),
        (
            "sprint_task_show",
            {"project_id": 7, "task_id": 123, "sprint_id": None},
        ),
        (
            "sprint_task_history",
            {"project_id": 7, "task_id": 123, "sprint_id": None},
        ),
        (
            "sprint_task_update",
            {
                "project_id": 7,
                "task_id": 123,
                "status": "Done",
                "expected_status": "In Progress",
                "expected_task_fingerprint": "sha256:abc",
                "idempotency_key": "task-update-123-001",
                "sprint_id": None,
                "outcome_summary": "Implemented the task.",
                "artifact_refs": [
                    "scripts/run_live_round.py",
                    "tests/test_live_budget.py",
                ],
                "checklist_result": "fully_met",
                "validation_summary": "uv run pytest tests/test_live_budget.py -q",
                "notes": "No known gaps.",
                "changed_by": "cli-agent",
            },
        ),
    ]


def test_sprint_story_cli_routes_readiness_and_close(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Sprint story CLI exposes readiness reads and guarded close mutation."""
    app = _FakeApplication()

    readiness_exit = main(
        ["sprint", "story", "readiness", "--project-id", "7", "--story-id", "66"],
        application=app,
    )
    close_exit = main(
        [
            "sprint",
            "story",
            "close",
            "--project-id",
            "7",
            "--story-id",
            "66",
            "--expected-status",
            "To Do",
            "--expected-story-fingerprint",
            "sha256:story",
            "--idempotency-key",
            "close-story-66-001",
            "--resolution",
            "Completed",
            "--completion-notes",
            "All tasks completed.",
            "--evidence-link",
            "scripts/run_live_round.py",
            "--evidence-link",
            "tests/test_live_budget.py",
        ],
        application=app,
    )

    outputs = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    assert [readiness_exit, close_exit] == [0, 0]
    assert [payload["ok"] for payload in outputs] == [True, True]
    assert app.calls[-2:] == [
        (
            "sprint_story_readiness",
            {"project_id": 7, "story_id": 66, "sprint_id": None},
        ),
        (
            "sprint_story_close",
            {
                "project_id": 7,
                "story_id": 66,
                "expected_status": "To Do",
                "expected_story_fingerprint": "sha256:story",
                "idempotency_key": "close-story-66-001",
                "resolution": "Completed",
                "completion_notes": "All tasks completed.",
                "evidence_links": [
                    "scripts/run_live_round.py",
                    "tests/test_live_budget.py",
                ],
                "sprint_id": None,
                "changed_by": "cli-agent",
            },
        ),
    ]


def test_sprint_close_cli_routes_readiness_and_guarded_close(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Sprint close CLI exposes readiness reads and guarded close mutation."""
    app = _FakeApplication()

    readiness_exit = main(
        ["sprint", "close-readiness", "--project-id", "7"],
        application=app,
    )
    close_exit = main(
        [
            "sprint",
            "close",
            "--project-id",
            "7",
            "--expected-state",
            "SPRINT_VIEW",
            "--expected-status",
            "Active",
            "--expected-sprint-fingerprint",
            "sha256:sprint",
            "--idempotency-key",
            "close-sprint-001",
            "--completion-notes",
            "All committed stories completed.",
            "--follow-up-notes",
            "Prepare next sprint.",
        ],
        application=app,
    )

    outputs = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    assert [readiness_exit, close_exit] == [0, 0]
    assert [payload["ok"] for payload in outputs] == [True, True]
    assert app.calls[-2:] == [
        (
            "sprint_close_readiness",
            {"project_id": 7, "sprint_id": None},
        ),
        (
            "sprint_close",
            {
                "project_id": 7,
                "expected_state": "SPRINT_VIEW",
                "expected_status": "Active",
                "expected_sprint_fingerprint": "sha256:sprint",
                "idempotency_key": "close-sprint-001",
                "completion_notes": "All committed stories completed.",
                "follow_up_notes": "Prepare next sprint.",
                "sprint_id": None,
                "changed_by": "cli-agent",
            },
        ),
    ]


def test_sprint_review_cli_routes_to_application(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Sprint review CLI routes to the application facade."""
    sprint_id = 11
    app = _FakeApplication()

    exit_code = main(
        ["sprint", "review", "--project-id", "7", "--sprint-id", str(sprint_id)],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert exit_code == 0
    assert payload["data"]["sprint_id"] == sprint_id
    assert app.calls[-1] == ("sprint_review", {"project_id": 7, "sprint_id": sprint_id})


def test_sprint_triage_cli_routes_learning_impact(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Sprint triage CLI routes learning impact fields."""
    app = _FakeApplication()

    exit_code = main(
        [
            "sprint",
            "triage",
            "--project-id",
            "7",
            "--expected-state",
            "SPRINT_COMPLETE",
            "--impact",
            "multiple",
            "--affected-layer",
            "story",
            "--affected-layer",
            "backlog",
            "--learning-summary",
            "Learned",
            "--decision-reason",
            "Routing",
            "--idempotency-key",
            "triage-001",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert exit_code == 0
    assert payload["data"]["impact"] == "multiple"
    assert app.calls[-1] == (
        "sprint_triage",
        {
            "project_id": 7,
            "expected_state": "SPRINT_COMPLETE",
            "impact": "multiple",
            "learning_summary": "Learned",
            "decision_reason": "Routing",
            "idempotency_key": "triage-001",
            "affected_requirements": [],
            "affected_task_ids": [],
            "affected_story_ids": [],
            "affected_backlog_item_ids": [],
            "affected_roadmap_item_ids": [],
            "affected_layers": ["story", "backlog"],
            "sprint_id": None,
            "replace_existing": False,
            "expected_triage_fingerprint": None,
            "changed_by": "cli-agent",
        },
    )


def test_story_save_cli_flattens_save_result(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Story save CLI prints flattened save result."""
    app = _FakeApplication()
    app.results["story_save"] = {
        "ok": True,
        "data": {
            "parent_requirement": "Requirement A",
            "attempt_id": "attempt-1",
            "artifact_fingerprint": "sha256:abc",
            "fsm_state": "STORY_PERSISTENCE",
            "save_result": {"success": True, "saved_count": 1},
        },
        "warnings": [],
        "errors": [],
    }

    exit_code = main(
        [
            "story",
            "save",
            "--project-id",
            "7",
            "--parent-requirement",
            "Requirement A",
            "--attempt-id",
            "attempt-1",
            "--expected-artifact-fingerprint",
            "sha256:abc",
            "--expected-state",
            "STORY_REVIEW",
            "--idempotency-key",
            "story-save-7-a",
        ],
        application=app,
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["save_result"]["saved_count"] == 1
    assert "data" not in payload["data"]


def test_cli_requires_backlog_save_guards(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Backlog save must be explicitly tied to a reviewed draft attempt."""
    app = _FakeApplication()

    rc = main(
        ["backlog", "save", "--project-id", str(PROJECT_ID)],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == INVALID_COMMAND_EXIT_CODE
    assert payload["ok"] is False
    assert _mapping(payload["errors"][0])["code"] == ErrorCode.INVALID_COMMAND.value
    assert app.calls == []


def test_cli_requires_roadmap_save_guards(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Roadmap save must be explicitly tied to a reviewed draft attempt."""
    app = _FakeApplication()

    rc = main(
        ["roadmap", "save", "--project-id", str(PROJECT_ID)],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == INVALID_COMMAND_EXIT_CODE
    assert payload["ok"] is False
    assert _mapping(payload["errors"][0])["code"] == ErrorCode.INVALID_COMMAND.value
    assert app.calls == []


@pytest.mark.parametrize(
    ("argv", "expected_call", "expected_command"),
    [
        (["doctor"], ("doctor", {}), "agileforge doctor"),
        (["schema", "check"], ("schema_check", {}), "agileforge schema check"),
        (["capabilities"], ("capabilities", {}), "agileforge capabilities"),
        (
            ["command", "schema", "agileforge status"],
            ("command_schema", {"command_name": "agileforge status"}),
            "agileforge command schema",
        ),
        (
            ["mutation", "show", "--mutation-event-id", "101"],
            ("mutation_show", {"mutation_event_id": 101}),
            "agileforge mutation show",
        ),
        (
            [
                "mutation",
                "list",
                "--project-id",
                "7",
                "--status",
                "recovery_required",
            ],
            ("mutation_list", {"project_id": 7, "status": "recovery_required"}),
            "agileforge mutation list",
        ),
        (
            [
                "mutation",
                "resume",
                "--mutation-event-id",
                "101",
                "--correlation-id",
                "corr-1",
            ],
            (
                "mutation_resume",
                {"mutation_event_id": 101, "correlation_id": "corr-1"},
            ),
            "agileforge mutation resume",
        ),
    ],
)
def test_cli_routes_phase_2a_operational_commands(
    argv: list[str],
    expected_call: tuple[str, dict[str, object]],
    expected_command: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify Phase 2A operational commands route to the application facade."""
    app = _FakeApplication()

    rc = main(argv, application=app)

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert _mapping(payload["meta"])["command"] == expected_command
    assert app.calls == [expected_call]


def test_cli_uses_error_exit_code_and_preserves_warnings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify structured errors are enveloped with their first exit code."""
    app = _FailingApplication()

    rc = main(
        ["project", "show", "--project-id", str(PROJECT_ID)],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == ERROR_EXIT_CODE
    assert payload["ok"] is False
    assert payload["data"] is None
    assert _mapping(payload["meta"])["command"] == "agileforge project show"
    assert payload["warnings"] == [
        {
            "code": "CACHE_STALE",
            "message": "Cached projection is stale.",
            "details": {"project_id": PROJECT_ID},
            "remediation": ["Retry after refresh."],
        }
    ]
    assert payload["errors"] == [
        {
            "code": "PROJECT_NOT_FOUND",
            "message": "Project does not exist.",
            "details": {"project_id": PROJECT_ID},
            "remediation": ["agileforge project list"],
            "exit_code": ERROR_EXIT_CODE,
            "retryable": False,
        }
    ]


def test_cli_preserves_error_data_from_service_result(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Keep structured failure data when wrapping service errors."""
    app = _FakeApplication()

    def mutation_resume_conflict(
        *,
        mutation_event_id: int,
        correlation_id: str | None = None,
    ) -> JsonObject:
        app.calls.append(
            (
                "mutation_resume",
                {
                    "mutation_event_id": mutation_event_id,
                    "correlation_id": correlation_id,
                },
            )
        )
        return {
            "ok": False,
            "data": {"mutation_event_id": mutation_event_id, "status": "pending"},
            "warnings": [],
            "errors": [
                {
                    "code": "MUTATION_RESUME_CONFLICT",
                    "message": "Another worker acquired recovery.",
                    "details": {"mutation_event_id": mutation_event_id},
                    "remediation": [],
                    "exit_code": 1,
                    "retryable": True,
                }
            ],
        }

    cast("Any", app).mutation_resume = mutation_resume_conflict

    rc = main(
        ["mutation", "resume", "--mutation-event-id", "101"],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == 1
    assert payload["ok"] is False
    assert payload["data"] == {"mutation_event_id": 101, "status": "pending"}
    assert app.calls == [
        (
            "mutation_resume",
            {"mutation_event_id": 101, "correlation_id": None},
        )
    ]


def test_cli_unexpected_exceptions_return_json_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify unexpected application errors stay inside the CLI envelope."""
    app = _ExplodingApplication()

    rc = main(["project", "list"], application=app)

    payload = _stdout_payload(capsys)
    assert rc == COMMAND_EXCEPTION_EXIT_CODE
    assert payload["ok"] is False
    assert payload["data"] is None
    assert payload["warnings"] == []
    assert _mapping(payload["meta"])["command"] == "agileforge"
    error = _first_mapping(payload["errors"])
    assert error["code"] == "COMMAND_EXCEPTION"
    assert error["message"] == "projection exploded"
    assert error["exit_code"] == COMMAND_EXCEPTION_EXIT_CODE
    assert error["retryable"] is False
    assert error["details"] == {"exception_type": "RuntimeError"}


def test_cli_parse_errors_return_json_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify direct main parse errors return structured JSON."""
    rc = main(["project", "show"])

    payload = _stdout_payload(capsys)
    assert rc == INVALID_COMMAND_EXIT_CODE
    assert payload["ok"] is False
    assert payload["data"] is None
    assert payload["warnings"] == []
    assert _mapping(payload["meta"])["command"] == "agileforge"
    error = _first_mapping(payload["errors"])
    assert error["code"] == "INVALID_COMMAND"
    assert error["exit_code"] == INVALID_COMMAND_EXIT_CODE
    assert "--project-id" in str(error["message"])


def test_module_parse_errors_return_json_envelope() -> None:
    """Verify python -m parse errors return structured JSON."""
    result = subprocess.run(  # nosec B603
        [sys.executable, "-m", "cli.main", "project", "show"],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        check=False,
    )

    payload = cast("JsonObject", json.loads(result.stdout))
    assert result.returncode == INVALID_COMMAND_EXIT_CODE
    assert result.stderr == ""
    assert payload["ok"] is False
    assert _mapping(payload["meta"])["command"] == "agileforge"
    error = _first_mapping(payload["errors"])
    assert error["code"] == "INVALID_COMMAND"
    assert error["exit_code"] == INVALID_COMMAND_EXIT_CODE


def test_top_level_help_describes_agent_workbench_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify help output is useful for agents and developers."""
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 0
    assert captured.err == ""
    assert "AgileForge" in captured.out
    assert "agent-facing CLI" in captured.out
    assert "read-only" not in captured.out
    assert "agileforge project list" in captured.out
    assert "agileforge authority review --project-id 1" in captured.out
    assert "agileforge authority accept --project-id 1" in captured.out
    assert (
        "agileforge authority reject --project-id 1 --review-token <review_token> "
        '--reason "..."'
    ) in captured.out
    assert (
        "agileforge backlog refine-preview --project-id 1 "
        "--source-attempt-id <attempt_id> --operations-file refinement_ops.json"
    ) in captured.out
    assert (
        "agileforge backlog refine-record --project-id 1 "
        "--source-attempt-id <attempt_id> --operations-file refinement_ops.json "
        "--expected-source-fingerprint <fingerprint> "
        "--expected-state SPRINT_COMPLETE --idempotency-key refine-backlog-001"
    ) in captured.out
    assert (
        "agileforge backlog approve --project-id 1 --attempt-id <attempt_id> "
        "--approved-artifact-fingerprint <fingerprint> "
        "--idempotency-key approve-refinement-001"
    ) in captured.out
    assert (
        "agileforge backlog refine-import --project-id 1 "
        "--source-artifact source.json --edited-file edited.json "
        "--expected-source-fingerprint <fingerprint> "
        "--idempotency-key refine-import-001"
    ) in captured.out
    assert (
        "agileforge context pack --project-id 1 --phase sprint-planning" in captured.out
    )
    assert (
        "agileforge sprint status --project-id 1 --sprint-id <completed_sprint_id>"
        in captured.out
    )


@pytest.mark.parametrize(
    ("argv", "expected_fragments"),
    [
        (
            ["story", "pending", "--help"],
            [
                "Saved or merged Story drafts can be completed as a selected scope",
                "agileforge story complete --project-id 1 --scope selection",
                "pending requirements remain excluded from scoped Sprint planning",
            ],
        ),
        (
            ["sprint", "candidates", "--help"],
            [
                "If Story completed a selected scope, candidates are filtered",
                "non-refined requirements are counted as excluded",
                "agileforge sprint candidates --project-id 1",
            ],
        ),
        (
            ["sprint", "status", "--help"],
            [
                "By default this shows the active or planned Sprint",
                "Completed Sprints require --sprint-id",
                (
                    "agileforge sprint status --project-id 1 "
                    "--sprint-id <completed_sprint_id>"
                ),
            ],
        ),
    ],
)
def test_scoped_story_sprint_help_explains_selection_behavior(
    argv: list[str],
    expected_fragments: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Scoped Story/Sprint help should explain selection and completed history."""
    with pytest.raises(SystemExit) as exc_info:
        main(argv)

    captured = capsys.readouterr()
    assert exc_info.value.code == 0
    assert captured.err == ""
    for fragment in expected_fragments:
        assert fragment in captured.out


def test_cli_configures_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify CLI startup configures file logging without console logging."""
    calls: list[dict[str, object]] = []

    def fake_configure_logging(**kwargs: object) -> None:
        calls.append(dict(kwargs))

    monkeypatch.setattr("cli.main.configure_logging", fake_configure_logging)

    exit_code = main(["project", "list"], application=_FakeApplication())

    assert exit_code == 0
    assert calls == [{"console": False}]


def test_packaged_project_exposes_api_module_from_other_cwd(
    tmp_path: Path,
) -> None:
    """Verify package metadata keeps top-level api importable outside repo cwd."""
    uv_path = shutil.which("uv")
    assert uv_path is not None

    result = subprocess.run(  # noqa: S603  # nosec B603
        [
            uv_path,
            "run",
            "--project",
            str(Path.cwd()),
            "--frozen",
            "python",
            "-c",
            (
                "import importlib.util; "
                "spec = importlib.util.find_spec('api'); "
                "print(spec.origin if spec else 'MISSING')"
            ),
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.rstrip().endswith("api.py")


@pytest.mark.parametrize(
    ("argv", "expected_command", "expected_call"),
    [
        (
            ["project", "show", "--project-id", str(PROJECT_ID)],
            "agileforge project show",
            ("project_show", {"project_id": PROJECT_ID}),
        ),
        (
            ["workflow", "state", "--project-id", str(PROJECT_ID)],
            "agileforge workflow state",
            ("workflow_state", {"project_id": PROJECT_ID}),
        ),
        (
            ["workflow", "next", "--project-id", str(PROJECT_ID)],
            "agileforge workflow next",
            ("workflow_next", {"project_id": PROJECT_ID}),
        ),
        (
            [
                "authority",
                "invariants",
                "--project-id",
                str(PROJECT_ID),
                "--spec-version-id",
                str(SPEC_VERSION_ID),
            ],
            "agileforge authority invariants",
            (
                "authority_invariants",
                {"project_id": PROJECT_ID, "spec_version_id": SPEC_VERSION_ID},
            ),
        ),
        (
            ["story", "show", "--story-id", str(STORY_ID)],
            "agileforge story show",
            ("story_show", {"story_id": STORY_ID}),
        ),
        (
            ["sprint", "candidates", "--project-id", str(PROJECT_ID)],
            "agileforge sprint candidates",
            ("sprint_candidates", {"project_id": PROJECT_ID}),
        ),
        (
            [
                "context",
                "pack",
                "--project-id",
                str(PROJECT_ID),
                "--phase",
                "sprint-planning",
            ],
            "agileforge context pack",
            (
                "context_pack",
                {"project_id": PROJECT_ID, "phase": "sprint-planning"},
            ),
        ),
        (
            ["status", "--project-id", str(PROJECT_ID)],
            "agileforge status",
            ("status", {"project_id": PROJECT_ID}),
        ),
    ],
)
def test_cli_routes_phase_1_command_surface(
    argv: list[str],
    expected_command: str,
    expected_call: tuple[str, dict[str, object]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify every Phase 1 command is routed through the CLI transport."""
    app = _FakeApplication()

    rc = main(argv, application=app)

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert payload["ok"] is True
    assert _mapping(payload["meta"])["command"] == expected_command
    assert app.calls == [expected_call]
