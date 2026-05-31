"""Tests for agent workbench Backlog phase runner."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import anyio
from sqlmodel import select

from models.core import Product, UserStory
from models.enums import StoryStatus, WorkflowEventType
from models.events import WorkflowEvent
from services.agent_workbench.backlog_phase import BacklogPhaseRunner
from services.agent_workbench.fingerprints import canonical_hash, canonical_json
from services.backlog_runtime import (
    build_backlog_input_context,
    run_backlog_agent_from_state,
)

if TYPE_CHECKING:
    import pytest
    from sqlmodel import Session


class _FakeProductRepo:
    """Fake product repo with setup-passed project data."""

    def get_by_id(self, product_id: int) -> SimpleNamespace:
        """Return a product-like object."""
        return SimpleNamespace(
            product_id=product_id,
            name="Cartola",
            spec_file_path="specs/spec.json",
            compiled_authority_json='{"authority": true}',
            vision="A clear saved vision.",
        )


class _FakeWorkflowService:
    """Fake workflow service with persisted session state."""

    def __init__(self) -> None:
        self.state: dict[str, Any] = {
            "fsm_state": "BACKLOG_INTERVIEW",
            "setup_status": "passed",
            "product_vision_assessment": {
                "product_vision_statement": "A clear saved vision.",
                "is_complete": True,
            },
        }

    def get_session_status(self, session_id: str) -> dict[str, Any]:
        """Return current state."""
        del session_id
        return dict(self.state)

    async def initialize_session(self, session_id: str) -> str:
        """No-op session initialization."""
        return session_id

    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, Any],
    ) -> None:
        """Persist state updates."""
        del session_id
        self.state.update(partial_update)


def _as_built_assessment_payload(
    *,
    evidence_pack_fingerprint: str = "sha256:pack",
    builder_version: str = "agileforge.as_built_pack_builder.v1",
) -> dict[str, Any]:
    """Return a schema-valid as-built assessment payload."""
    return {
        "schema_version": "agileforge.as_built_assessment.v1",
        "project_id": 2,
        "assessment_id": "as-built-2-pack",
        "agent_version": "agileforge.as_built_assessor.v1",
        "evidence_pack_builder_version": builder_version,
        "authority_fingerprint": "sha256:authority",
        "evidence_pack_fingerprint": evidence_pack_fingerprint,
        "generated_at": "2026-05-28T12:00:00Z",
        "assessment_summary": "Observed current behavior.",
        "repo_snapshot": {
            "path": "/repo",
            "git_commit": "abc123",
            "dirty": False,
        },
        "capability_assessments": [
            {
                "authority_ref": "REQ.live-squad-recommendation",
                "invariant_refs": ["INV-a4b296c058e88663"],
                "capability_title": "Live squad recommendation",
                "status": "observed",
                "confidence": "medium",
                "evidence": [],
                "limitations": ["Tests were not executed."],
                "recommended_backlog_treatment": "skip_new_implementation",
                "reasoning": "Repo evidence supports the capability.",
            }
        ],
        "cross_cutting_findings": [],
        "open_questions": [],
        "is_complete": True,
        "clarifying_questions": [],
    }


def _as_built_state(
    assessment: dict[str, Any],
    *,
    meta_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return workflow state containing cached assessment and metadata."""
    canonical = canonical_json(assessment)
    meta = {
        "schema_version": "agileforge.as_built_assessment.v1",
        "agent_version": assessment["agent_version"],
        "evidence_pack_builder_version": assessment[
            "evidence_pack_builder_version"
        ],
        "authority_fingerprint": assessment["authority_fingerprint"],
        "repo_git_commit": assessment["repo_snapshot"]["git_commit"],
        "repo_dirty": assessment["repo_snapshot"]["dirty"],
        "evidence_pack_fingerprint": assessment["evidence_pack_fingerprint"],
        "assessment_fingerprint": canonical_hash(assessment),
        "generated_at": assessment["generated_at"],
    }
    if meta_overrides:
        meta.update(meta_overrides)
    return {
        "as_built_assessment_cached": canonical,
        "as_built_assessment_cache_meta": meta,
    }


def _backlog_output_json(
    item: dict[str, Any],
    *,
    is_complete: bool = True,
    clarifying_questions: list[str] | None = None,
) -> str:
    """Return a schema-shaped Backlog Primer response with one item."""
    backlog_item = {
        "priority": 1,
        "requirement": "Verify Live Squad Recommendation",
        "value_driver": "Strategic",
        "justification": "Aligns the backlog with observed repository behavior.",
        "estimated_effort": "S",
    }
    backlog_item.update(item)
    return json.dumps(
        {
            "backlog_items": [backlog_item],
            "is_complete": is_complete,
            "clarifying_questions": clarifying_questions or [],
        }
    )


def _brownfield_backlog_state(
    assessment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return workflow state with a fresh As-Built assessment cache."""
    assessment_payload = assessment or _as_built_assessment_payload()
    return {
        "product_vision_assessment": {
            "product_vision_statement": "A clear saved vision.",
            "is_complete": True,
        },
        "pending_spec_content": "SPEC CONTENT",
        "compiled_authority_cached": "AUTHORITY JSON",
        **_as_built_state(assessment_payload),
    }


def _run_brownfield_backlog_runtime(
    monkeypatch: pytest.MonkeyPatch,
    item: dict[str, Any],
    *,
    assessment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the backlog runtime against a single mocked backlog item."""

    async def fake_invoke_backlog_agent(payload: object) -> str:
        del payload
        return _backlog_output_json(item)

    def fake_write_failure_artifact(**kwargs: object) -> dict[str, Any]:
        raw_output = kwargs.get("raw_output") or ""
        raw_output_preview = raw_output[:500] if isinstance(raw_output, str) else ""
        return {
            "metadata": {
                "failure_artifact_id": "backlog-failure-test",
                "failure_stage": kwargs["failure_stage"],
                "failure_summary": kwargs["failure_summary"],
                "raw_output_preview": raw_output_preview,
                "has_full_artifact": False,
            }
        }

    async def call_runtime() -> dict[str, Any]:
        return await run_backlog_agent_from_state(
            _brownfield_backlog_state(assessment),
            project_id=2,
            user_input="draft backlog",
        )

    monkeypatch.setattr(
        "services.backlog_runtime._invoke_backlog_agent",
        fake_invoke_backlog_agent,
    )
    monkeypatch.setattr(
        "services.backlog_runtime.write_failure_artifact",
        fake_write_failure_artifact,
    )

    return anyio.run(call_runtime)


def test_backlog_generate_hydrates_vision_spec_and_authority_before_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backlog generate must pass Vision, spec, and accepted authority to the agent."""
    captured: dict[str, Any] = {}

    def fake_select_project(
        product_id: int, tool_context: SimpleNamespace
    ) -> dict[str, Any]:
        state = tool_context.state
        state["pending_spec_content"] = "SPEC CONTENT"
        state["compiled_authority_cached"] = "AUTHORITY JSON"
        state["implementation_evidence_cached"] = (
            '{"schema_version":"agileforge.reconciliation_report.v1","findings":[]}'
        )
        state["product_vision_assessment"] = {
            "product_vision_statement": "A clear saved vision.",
            "is_complete": True,
        }
        return {"success": True, "project_id": product_id}

    async def fake_run_backlog_agent_from_state(
        state: dict[str, Any],
        *,
        project_id: int,
        user_input: str | None,
    ) -> dict[str, Any]:
        captured["state"] = dict(state)
        captured["project_id"] = project_id
        captured["user_input"] = user_input
        return {
            "success": True,
            "input_context": {
                "product_vision_statement": state["product_vision_assessment"][
                    "product_vision_statement"
                ],
                "technical_spec": state.get("pending_spec_content"),
                "compiled_authority": state.get("compiled_authority_cached"),
                "prior_backlog_state": "NO_HISTORY",
                "as_built_assessment": "NO_AS_BUILT_ASSESSMENT",
                "implementation_evidence": state.get("implementation_evidence_cached"),
                "user_input": user_input or "",
            },
            "output_artifact": {
                "backlog_items": [{"title": "Choose weekly squad"}],
                "is_complete": False,
                "clarifying_questions": ["Which MVP slice first?"],
            },
            "is_complete": False,
            "error": None,
        }

    monkeypatch.setattr(
        "services.agent_workbench.backlog_phase.select_project",
        fake_select_project,
    )
    monkeypatch.setattr(
        "services.agent_workbench.backlog_phase.run_backlog_agent_from_state",
        fake_run_backlog_agent_from_state,
    )
    runner = BacklogPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=_FakeWorkflowService(),
    )

    result = runner.generate(project_id=2, user_input="draft backlog")

    assert result["ok"] is True
    assert captured["state"]["pending_spec_content"] == "SPEC CONTENT"
    assert captured["state"]["compiled_authority_cached"] == "AUTHORITY JSON"
    assert (
        captured["state"]["product_vision_assessment"]["product_vision_statement"]
        == "A clear saved vision."
    )
    assert result["data"]["input_context"]["technical_spec"] == "SPEC CONTENT"
    assert result["data"]["input_context"]["compiled_authority"] == "AUTHORITY JSON"
    assert result["data"]["input_context"]["as_built_assessment"] == (
        "NO_AS_BUILT_ASSESSMENT"
    )
    assert result["data"]["input_context"]["implementation_evidence"] == (
        '{"schema_version":"agileforge.reconciliation_report.v1","findings":[]}'
    )
    assert result["data"]["input_context"]["implementation_evidence"].startswith(
        '{"schema_version"'
    )


def test_backlog_preview_runs_from_sprint_complete_without_persisting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backlog preview should be safe for brownfield quality checks post-sprint."""
    captured: dict[str, Any] = {}

    def fake_select_project(
        product_id: int, tool_context: SimpleNamespace
    ) -> dict[str, Any]:
        state = tool_context.state
        state["pending_spec_content"] = "SPEC CONTENT"
        state["compiled_authority_cached"] = "AUTHORITY JSON"
        state["product_vision_assessment"] = {
            "product_vision_statement": "A clear saved vision.",
            "is_complete": True,
        }
        state.update(_as_built_state(_as_built_assessment_payload()))
        return {"success": True, "project_id": product_id}

    async def fake_run_backlog_agent_from_state(
        state: dict[str, Any],
        *,
        project_id: int,
        user_input: str | None,
    ) -> dict[str, Any]:
        captured["state"] = dict(state)
        captured["project_id"] = project_id
        captured["user_input"] = user_input
        input_context = build_backlog_input_context(state, user_input=user_input)
        return {
            "success": True,
            "input_context": input_context,
            "output_artifact": {
                "backlog_items": [{"title": "Verify live squad evidence"}],
                "is_complete": True,
                "clarifying_questions": [],
            },
            "is_complete": True,
            "error": None,
        }

    monkeypatch.setattr(
        "services.agent_workbench.backlog_phase.select_project",
        fake_select_project,
    )
    monkeypatch.setattr(
        "services.agent_workbench.backlog_phase.run_backlog_agent_from_state",
        fake_run_backlog_agent_from_state,
    )
    workflow = _FakeWorkflowService()
    workflow.state.update(
        {
            "fsm_state": "SPRINT_COMPLETE",
            "backlog_attempts": [{"attempt_id": "old-attempt"}],
        }
    )
    runner = BacklogPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=workflow,
    )

    result = runner.preview(project_id=2)

    assert result["ok"] is True
    assert result["data"]["persisted"] is False
    assert result["data"]["attempt_id"] is None
    assert result["data"]["fsm_state"] == "SPRINT_COMPLETE"
    assert result["data"]["input_context"]["as_built_assessment"].startswith("{")
    assert "product_backlog_assessment" not in workflow.state
    assert workflow.state["backlog_attempts"] == [{"attempt_id": "old-attempt"}]


def test_backlog_preview_surfaces_brownfield_contract_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workbench preview envelope should expose brownfield validation failures."""

    async def fake_run_backlog_agent_from_state(
        state: dict[str, Any],
        *,
        project_id: int,
        user_input: str | None,
    ) -> dict[str, Any]:
        del state, project_id, user_input
        return {
            "success": False,
            "error": "Backlog brownfield contract validation failed",
            "failure_stage": "brownfield_contract_validation",
            "failure_summary": "Backlog brownfield contract validation failed",
            "failure_artifact_id": "backlog-failure-brownfield-1",
            "has_full_artifact": True,
            "input_context": {
                "product_vision_statement": "A clear saved vision.",
                "technical_spec": "SPEC CONTENT",
                "compiled_authority": "AUTHORITY JSON",
                "prior_backlog_state": "NO_HISTORY",
                "as_built_assessment": "{}",
                "implementation_evidence": "NO_EVIDENCE",
                "user_input": "",
            },
            "output_artifact": {
                "is_complete": False,
                "error": "BACKLOG_GENERATION_FAILED",
                "failure_summary": "Backlog brownfield contract validation failed",
            },
            "is_complete": False,
        }

    def fake_select_project(
        product_id: int, tool_context: SimpleNamespace
    ) -> dict[str, Any]:
        state = tool_context.state
        state["pending_spec_content"] = "SPEC CONTENT"
        state["compiled_authority_cached"] = "AUTHORITY JSON"
        state["product_vision_assessment"] = {
            "product_vision_statement": "A clear saved vision.",
            "is_complete": True,
        }
        return {"success": True, "project_id": product_id}

    monkeypatch.setattr(
        "services.agent_workbench.backlog_phase.select_project",
        fake_select_project,
    )
    monkeypatch.setattr(
        "services.agent_workbench.backlog_phase.run_backlog_agent_from_state",
        fake_run_backlog_agent_from_state,
    )
    runner = BacklogPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=_FakeWorkflowService(),
    )

    result = runner.preview(project_id=2)

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "MUTATION_FAILED"
    assert (
        result["errors"][0]["details"]["failure_stage"]
        == "brownfield_contract_validation"
    )


def test_backlog_preview_surfaces_brownfield_retry_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed previews should expose bounded retry diagnostics in the envelope."""

    async def fake_run_backlog_agent_from_state(
        state: dict[str, Any],
        *,
        project_id: int,
        user_input: str | None,
    ) -> dict[str, Any]:
        del state, project_id, user_input
        return {
            "success": False,
            "error": "Backlog brownfield contract validation failed",
            "failure_stage": "brownfield_contract_validation",
            "failure_summary": "Backlog brownfield contract validation failed",
            "failure_artifact_id": "backlog-failure-brownfield-retry",
            "has_full_artifact": True,
            "brownfield_retry_attempted": True,
            "brownfield_retry_count": 1,
            "brownfield_retry_marker": "BROWNFIELD CONTRACT RETRY",
            "brownfield_retry_failed_stage": "brownfield_contract_validation",
            "input_context": {
                "product_vision_statement": "A clear saved vision.",
                "technical_spec": "SPEC CONTENT",
                "compiled_authority": "AUTHORITY JSON",
                "prior_backlog_state": "NO_HISTORY",
                "as_built_assessment": "{}",
                "implementation_evidence": "NO_EVIDENCE",
                "user_input": (
                    "BROWNFIELD CONTRACT RETRY: fix exact contract errors"
                ),
            },
            "output_artifact": {
                "is_complete": False,
                "error": "BACKLOG_GENERATION_FAILED",
                "failure_summary": "Backlog brownfield contract validation failed",
            },
            "is_complete": False,
        }

    def fake_select_project(
        product_id: int, tool_context: SimpleNamespace
    ) -> dict[str, Any]:
        state = tool_context.state
        state["pending_spec_content"] = "SPEC CONTENT"
        state["compiled_authority_cached"] = "AUTHORITY JSON"
        state["product_vision_assessment"] = {
            "product_vision_statement": "A clear saved vision.",
            "is_complete": True,
        }
        return {"success": True, "project_id": product_id}

    monkeypatch.setattr(
        "services.agent_workbench.backlog_phase.select_project",
        fake_select_project,
    )
    monkeypatch.setattr(
        "services.agent_workbench.backlog_phase.run_backlog_agent_from_state",
        fake_run_backlog_agent_from_state,
    )
    runner = BacklogPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=_FakeWorkflowService(),
    )

    result = runner.preview(project_id=2)

    details = result["errors"][0]["details"]
    assert details["brownfield_retry_attempted"] is True
    assert details["brownfield_retry_count"] == 1
    assert details["brownfield_retry_marker"] == "BROWNFIELD CONTRACT RETRY"
    assert details["brownfield_retry_failed_stage"] == (
        "brownfield_contract_validation"
    )


def test_build_backlog_input_context_uses_no_evidence_when_cache_missing() -> None:
    """Backlog input context should use NO_EVIDENCE without cached evidence."""
    context = build_backlog_input_context(
        {
            "product_vision_assessment": {
                "product_vision_statement": "A clear saved vision.",
                "is_complete": True,
            },
            "pending_spec_content": "SPEC CONTENT",
            "compiled_authority_cached": "AUTHORITY JSON",
        },
        user_input=None,
    )

    assert context["implementation_evidence"] == "NO_EVIDENCE"
    assert context["as_built_assessment"] == "NO_AS_BUILT_ASSESSMENT"


def test_build_backlog_input_context_serializes_cached_evidence() -> None:
    """Backlog input context should pass cached evidence through as JSON text."""
    context = build_backlog_input_context(
        {
            "product_vision_assessment": {
                "product_vision_statement": "A clear saved vision.",
                "is_complete": True,
            },
            "pending_spec_content": "SPEC CONTENT",
            "compiled_authority_cached": "AUTHORITY JSON",
            "implementation_evidence_cached": {
                "schema_version": "agileforge.reconciliation_report.v1",
                "findings": [],
            },
        },
        user_input=None,
    )

    assert context["implementation_evidence"] == (
        '{"schema_version": "agileforge.reconciliation_report.v1", "findings": []}'
    )
    assert context["as_built_assessment"] == "NO_AS_BUILT_ASSESSMENT"


def test_build_backlog_input_context_serializes_cached_as_built_assessment() -> None:
    """Backlog input context should pass fresh as-built assessment through."""
    assessment = _as_built_assessment_payload()
    context = build_backlog_input_context(
        {
            "product_vision_assessment": {
                "product_vision_statement": "A clear saved vision.",
                "is_complete": True,
            },
            "pending_spec_content": "SPEC CONTENT",
            "compiled_authority_cached": "AUTHORITY JSON",
            **_as_built_state(assessment),
        },
        user_input=None,
    )

    assert context["as_built_assessment"] == canonical_json(assessment)


def test_build_backlog_input_context_rejects_stale_as_built_fingerprint() -> None:
    """Evidence-pack fingerprint mismatch suppresses stale assessment cache."""
    assessment = _as_built_assessment_payload()
    context = build_backlog_input_context(
        {
            "product_vision_assessment": {
                "product_vision_statement": "A clear saved vision.",
                "is_complete": True,
            },
            "pending_spec_content": "SPEC CONTENT",
            "compiled_authority_cached": "AUTHORITY JSON",
            **_as_built_state(
                assessment,
                meta_overrides={"evidence_pack_fingerprint": "sha256:changed"},
            ),
        },
        user_input=None,
    )

    assert context["as_built_assessment"] == "NO_AS_BUILT_ASSESSMENT"


def test_build_backlog_input_context_rejects_stale_builder_version() -> None:
    """Builder version mismatch suppresses stale assessment cache."""
    assessment = _as_built_assessment_payload(
        builder_version="agileforge.as_built_pack_builder.v0"
    )
    context = build_backlog_input_context(
        {
            "product_vision_assessment": {
                "product_vision_statement": "A clear saved vision.",
                "is_complete": True,
            },
            "pending_spec_content": "SPEC CONTENT",
            "compiled_authority_cached": "AUTHORITY JSON",
            **_as_built_state(assessment),
        },
        user_input=None,
    )

    assert context["as_built_assessment"] == "NO_AS_BUILT_ASSESSMENT"


def test_backlog_runtime_accepts_valid_brownfield_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime validation should accept exact As-Built metadata for mapped items."""
    result = _run_brownfield_backlog_runtime(
        monkeypatch,
        {
            "requirement": "Verify Live Squad Recommendation",
            "capability_name": "Live squad recommendation",
            "authority_ref": "REQ.live-squad-recommendation",
            "as_built_status": "observed",
            "recommended_backlog_treatment": "skip_new_implementation",
        },
    )

    assert result["success"] is True
    output_item = result["output_artifact"]["backlog_items"][0]
    assert output_item["capability_name"] == "Live squad recommendation"


def test_backlog_runtime_rejects_capability_title_without_brownfield_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A noun-only capability title must not pass without brownfield metadata."""
    result = _run_brownfield_backlog_runtime(
        monkeypatch,
        {"requirement": "Live squad recommendation"},
    )

    assert result["success"] is False
    assert result["failure_stage"] == "brownfield_contract_validation"
    summary = result["failure_summary"]
    assert "missing capability_name" in summary
    assert "missing as_built_status" in summary
    assert "missing recommended_backlog_treatment" in summary


def test_backlog_runtime_rejects_expanded_capability_title_without_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding extra title words must not bypass brownfield metadata rules."""
    result = _run_brownfield_backlog_runtime(
        monkeypatch,
        {"requirement": "Live Squad Recommendation With Risk Guard"},
    )

    assert result["success"] is False
    assert result["failure_stage"] == "brownfield_contract_validation"
    summary = result["failure_summary"]
    assert "appears to map to As-Built capability" in summary
    assert "REQ.live-squad-recommendation" in summary


def test_backlog_runtime_rejects_mismatched_brownfield_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mapped items must copy status and treatment from the As-Built assessment."""
    result = _run_brownfield_backlog_runtime(
        monkeypatch,
        {
            "requirement": "Verify Live Squad Recommendation",
            "capability_name": "Live squad recommendation",
            "authority_ref": "REQ.live-squad-recommendation",
            "as_built_status": "not_observed",
            "recommended_backlog_treatment": "create_product_item",
        },
    )

    assert result["success"] is False
    assert result["failure_stage"] == "brownfield_contract_validation"
    summary = result["failure_summary"]
    assert "as_built_status must equal 'observed'" in summary
    assert (
        "recommended_backlog_treatment must equal 'skip_new_implementation'"
        in summary
    )
    assert "missing as_built_status" not in summary
    assert "missing recommended_backlog_treatment" not in summary


def test_backlog_runtime_rejects_unbacked_brownfield_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Brownfield metadata must be backed by an As-Built capability."""
    result = _run_brownfield_backlog_runtime(
        monkeypatch,
        {
            "requirement": "Verify imaginary billing adapter",
            "capability_name": "Imaginary billing adapter",
            "authority_ref": "REQ.imaginary-billing-adapter",
            "as_built_status": "observed",
            "recommended_backlog_treatment": "skip_new_implementation",
        },
    )

    assert result["success"] is False
    assert result["failure_stage"] == "brownfield_contract_validation"
    assert "brownfield metadata does not match As-Built capability" in (
        result["failure_summary"]
    )


def test_backlog_runtime_allows_homogeneous_duplicate_authority_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One requirement may have several invariant-level As-Built entries."""
    assessment = _as_built_assessment_payload()
    assessment["capability_assessments"].append(
        {
            "authority_ref": "REQ.live-squad-recommendation",
            "invariant_refs": ["INV-second"],
            "capability_title": "Live squad recommendation",
            "status": "observed",
            "confidence": "medium",
            "evidence": [],
            "limitations": ["Second invariant fixture."],
            "recommended_backlog_treatment": "skip_new_implementation",
            "reasoning": "Same backlog-level contract, different invariant.",
        }
    )

    result = _run_brownfield_backlog_runtime(
        monkeypatch,
        {
            "requirement": "Verify Live Squad Recommendation",
            "capability_name": "Live squad recommendation",
            "authority_ref": "REQ.live-squad-recommendation",
            "as_built_status": "observed",
            "recommended_backlog_treatment": "skip_new_implementation",
        },
        assessment=assessment,
    )

    assert result["success"] is True


def test_backlog_runtime_allows_status_to_select_duplicate_authority_ref_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Status and treatment metadata can select one mixed authority-ref subgroup."""
    assessment = _as_built_assessment_payload()
    assessment["capability_assessments"].append(
        {
            "authority_ref": "REQ.live-squad-recommendation",
            "invariant_refs": ["INV-missing"],
            "capability_title": "Live squad recommendation",
            "status": "not_observed",
            "confidence": "low",
            "evidence": [],
            "limitations": ["Heterogeneous fixture."],
            "recommended_backlog_treatment": "create_discovery_item",
            "reasoning": "Same ref, different assessment status.",
        }
    )

    result = _run_brownfield_backlog_runtime(
        monkeypatch,
        {
            "requirement": "Verify Live Squad Recommendation",
            "capability_name": "Live squad recommendation",
            "authority_ref": "REQ.live-squad-recommendation",
            "as_built_status": "observed",
            "recommended_backlog_treatment": "skip_new_implementation",
        },
        assessment=assessment,
    )

    assert result["success"] is True


def test_backlog_runtime_reports_treatment_mismatch_inside_duplicate_ref_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duplicate authority refs should still report the exact wrong treatment."""
    assessment = _as_built_assessment_payload()
    assessment["capability_assessments"].append(
        {
            "authority_ref": "REQ.live-squad-recommendation",
            "invariant_refs": ["INV-second"],
            "capability_title": "Live squad recommendation",
            "status": "observed",
            "confidence": "medium",
            "evidence": [],
            "limitations": ["Second invariant fixture."],
            "recommended_backlog_treatment": "skip_new_implementation",
            "reasoning": "Same backlog-level contract, different invariant.",
        }
    )

    result = _run_brownfield_backlog_runtime(
        monkeypatch,
        {
            "requirement": "Harden Live Squad Recommendation",
            "capability_name": "Live squad recommendation",
            "authority_ref": "REQ.live-squad-recommendation",
            "as_built_status": "observed",
            "recommended_backlog_treatment": "create_hardening_item",
        },
        assessment=assessment,
    )

    assert result["success"] is False
    assert result["failure_stage"] == "brownfield_contract_validation"
    assert "recommended_backlog_treatment must equal 'skip_new_implementation'" in (
        result["failure_summary"]
    )
    assert "authority_ref metadata does not match" not in result["failure_summary"]


def test_backlog_runtime_rejects_ambiguous_authority_ref_without_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed authority-ref groups need enough emitted metadata to select a contract."""
    assessment = _as_built_assessment_payload()
    assessment["capability_assessments"].append(
        {
            "authority_ref": "REQ.live-squad-recommendation",
            "invariant_refs": ["INV-missing"],
            "capability_title": "Live squad recommendation",
            "status": "not_observed",
            "confidence": "low",
            "evidence": [],
            "limitations": ["Heterogeneous fixture."],
            "recommended_backlog_treatment": "create_discovery_item",
            "reasoning": "Same ref, different assessment status.",
        }
    )

    result = _run_brownfield_backlog_runtime(
        monkeypatch,
        {
            "requirement": "Verify Live Squad Recommendation",
            "authority_ref": "REQ.live-squad-recommendation",
        },
        assessment=assessment,
    )

    assert result["success"] is False
    assert result["failure_stage"] == "brownfield_contract_validation"
    assert "ambiguous As-Built authority_ref" in result["failure_summary"]


def test_backlog_runtime_rejects_duplicate_as_built_capability_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ambiguous normalized As-Built keys must fail brownfield validation."""
    assessment = _as_built_assessment_payload()
    assessment["capability_assessments"].append(
        {
            "authority_ref": "Live squad recommendation",
            "invariant_refs": ["INV-duplicate"],
            "capability_title": "Alternative squad planning",
            "status": "not_observed",
            "confidence": "medium",
            "evidence": [],
            "limitations": ["Duplicate key fixture."],
            "recommended_backlog_treatment": "create_product_item",
            "reasoning": "Fixture creates an authority/title normalized collision.",
        }
    )

    result = _run_brownfield_backlog_runtime(
        monkeypatch,
        {
            "requirement": "Verify Live Squad Recommendation",
            "capability_name": "Live squad recommendation",
            "as_built_status": "observed",
            "recommended_backlog_treatment": "skip_new_implementation",
        },
        assessment=assessment,
    )

    assert result["success"] is False
    assert result["failure_stage"] == "brownfield_contract_validation"
    assert "duplicate ambiguous As-Built capability key" in result["failure_summary"]


def test_backlog_runtime_allows_exact_authority_ref_for_duplicate_titles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exact authority_ref should disambiguate duplicate capability titles."""
    assessment = _as_built_assessment_payload()
    assessment["capability_assessments"].append(
        {
            "authority_ref": "REQ.live-squad-docs",
            "invariant_refs": ["INV-docs"],
            "capability_title": "Live squad recommendation",
            "status": "observed",
            "confidence": "medium",
            "evidence": [],
            "limitations": ["Duplicate title fixture."],
            "recommended_backlog_treatment": "skip_new_implementation",
            "reasoning": "Fixture creates a title collision.",
        }
    )

    result = _run_brownfield_backlog_runtime(
        monkeypatch,
        {
            "requirement": "Verify Live Squad Recommendation",
            "capability_name": "Live squad recommendation",
            "authority_ref": "REQ.live-squad-recommendation",
            "as_built_status": "observed",
            "recommended_backlog_treatment": "skip_new_implementation",
        },
        assessment=assessment,
    )

    assert result["success"] is True


def test_backlog_runtime_normalizes_observed_item_greenfield_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mapped observed capabilities should get brownfield-safe title prefixes."""
    result = _run_brownfield_backlog_runtime(
        monkeypatch,
        {
            "requirement": "Build Live Squad Recommendation",
            "capability_name": "Live squad recommendation",
            "authority_ref": "REQ.live-squad-recommendation",
            "as_built_status": "observed",
            "recommended_backlog_treatment": "skip_new_implementation",
        },
    )

    assert result["success"] is True
    output_item = result["output_artifact"]["backlog_items"][0]
    assert output_item["requirement"] == "Verify Live Squad Recommendation"


def test_backlog_runtime_allows_discovery_treatment_formalize_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery recommendations should allow formalization/discovery titles."""
    assessment = _as_built_assessment_payload()
    assessment["capability_assessments"] = [
        {
            "authority_ref": "DATA.promotion-decision",
            "invariant_refs": ["INV-promotion"],
            "capability_title": "Promotion Decision",
            "status": "not_observed",
            "confidence": "low",
            "evidence": [],
            "limitations": ["Schema not observed."],
            "recommended_backlog_treatment": "create_discovery_item",
            "reasoning": "Needs discovery/formalization, not direct product build.",
        }
    ]

    result = _run_brownfield_backlog_runtime(
        monkeypatch,
        {
            "requirement": "Formalize Promotion Decision Artifact Schema",
            "capability_name": "Promotion Decision",
            "authority_ref": "DATA.promotion-decision",
            "as_built_status": "not_observed",
            "recommended_backlog_treatment": "create_discovery_item",
        },
        assessment=assessment,
    )

    assert result["success"] is True


def test_backlog_runtime_normalizes_product_treatment_verify_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mapped product-work recommendations should get product title prefixes."""
    assessment = _as_built_assessment_payload()
    assessment["capability_assessments"] = [
        {
            "authority_ref": "REQ.post-round-review",
            "invariant_refs": ["INV-post-round"],
            "capability_title": "Post Round Review",
            "status": "not_observed",
            "confidence": "low",
            "evidence": [],
            "limitations": ["Capability not observed."],
            "recommended_backlog_treatment": "create_product_item",
            "reasoning": "Accepted authority requires product work.",
        }
    ]

    result = _run_brownfield_backlog_runtime(
        monkeypatch,
        {
            "requirement": "Verify Post-Round Review Artifact",
            "capability_name": "Post Round Review",
            "authority_ref": "REQ.post-round-review",
            "as_built_status": "not_observed",
            "recommended_backlog_treatment": "create_product_item",
        },
        assessment=assessment,
    )

    assert result["success"] is True
    output_item = result["output_artifact"]["backlog_items"][0]
    assert output_item["requirement"] == "Build Post-Round Review Artifact"


def test_backlog_runtime_retries_brownfield_contract_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime should give the agent one feedback pass for contract repairs."""
    calls: list[str] = []

    async def fake_invoke_backlog_agent(payload: object) -> str:
        payload_model = cast("Any", payload)
        user_input = payload_model.user_input
        assert isinstance(user_input, str)
        calls.append(user_input)
        if len(calls) == 1:
            return _backlog_output_json(
                {
                    "requirement": "Verify Live Squad Recommendation",
                    "capability_name": "Wrong live squad name",
                    "authority_ref": "REQ.live-squad-recommendation",
                    "as_built_status": "observed",
                    "recommended_backlog_treatment": "skip_new_implementation",
                }
            )
        return _backlog_output_json(
            {
                "requirement": "Verify Live Squad Recommendation",
                "capability_name": "Live squad recommendation",
                "authority_ref": "REQ.live-squad-recommendation",
                "as_built_status": "observed",
                "recommended_backlog_treatment": "skip_new_implementation",
            }
        )

    monkeypatch.setattr(
        "services.backlog_runtime._invoke_backlog_agent",
        fake_invoke_backlog_agent,
    )

    async def call_runtime() -> dict[str, Any]:
        return await run_backlog_agent_from_state(
            _brownfield_backlog_state(),
            project_id=2,
            user_input="draft backlog",
        )

    result = anyio.run(call_runtime)

    assert result["success"] is True
    expected_call_count = 2
    assert len(calls) == expected_call_count
    assert calls[0] == "draft backlog"
    assert "BROWNFIELD CONTRACT RETRY" in calls[1]
    assert "capability_name must match" in calls[1]


def test_backlog_runtime_retry_feedback_includes_title_prefix_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry feedback should prevent newly mapped brownfield title failures."""
    calls: list[str] = []

    async def fake_invoke_backlog_agent(payload: object) -> str:
        payload_model = cast("Any", payload)
        user_input = payload_model.user_input
        assert isinstance(user_input, str)
        calls.append(user_input)
        if len(calls) == 1:
            return _backlog_output_json(
                {
                    "requirement": "Verify Live Squad Recommendation",
                    "capability_name": "Wrong live squad name",
                    "authority_ref": "REQ.live-squad-recommendation",
                    "as_built_status": "observed",
                    "recommended_backlog_treatment": "skip_new_implementation",
                }
            )
        return _backlog_output_json(
            {
                "requirement": "Verify Live Squad Recommendation",
                "capability_name": "Live squad recommendation",
                "authority_ref": "REQ.live-squad-recommendation",
                "as_built_status": "observed",
                "recommended_backlog_treatment": "skip_new_implementation",
            }
        )

    monkeypatch.setattr(
        "services.backlog_runtime._invoke_backlog_agent",
        fake_invoke_backlog_agent,
    )

    async def call_runtime() -> dict[str, Any]:
        return await run_backlog_agent_from_state(
            _brownfield_backlog_state(),
            project_id=2,
            user_input="draft backlog",
        )

    result = anyio.run(call_runtime)

    assert result["success"] is True
    expected_call_count = 2
    assert len(calls) == expected_call_count
    assert "skip_new_implementation -> Verify, Document, Monitor, Preserve" in (
        calls[1]
    )
    assert "create_verification_item -> Verify, Validate, Harden" in calls[1]
    assert "create_discovery_item -> Discover, Investigate, Clarify" in calls[1]
    assert "create_product_item -> Build, Add, Implement, Create" in calls[1]
    assert "uses As-Built capability terms" in calls[1]
    assert "split it into mapped single-capability items" in calls[1]
    assert "recommended_backlog_treatment unchanged" in calls[1]


def test_backlog_runtime_failed_brownfield_retry_exposes_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime failures after retry should expose bounded retry diagnostics."""
    calls: list[str] = []

    async def fake_invoke_backlog_agent(payload: object) -> str:
        payload_model = cast("Any", payload)
        user_input = payload_model.user_input
        assert isinstance(user_input, str)
        calls.append(user_input)
        return _backlog_output_json(
            {
                "requirement": "Verify Live Squad Recommendation",
                "capability_name": "Wrong live squad name",
                "authority_ref": "REQ.live-squad-recommendation",
                "as_built_status": "observed",
                "recommended_backlog_treatment": "skip_new_implementation",
            }
        )

    monkeypatch.setattr(
        "services.backlog_runtime._invoke_backlog_agent",
        fake_invoke_backlog_agent,
    )

    async def call_runtime() -> dict[str, Any]:
        return await run_backlog_agent_from_state(
            _brownfield_backlog_state(),
            project_id=2,
            user_input="draft backlog",
        )

    result = anyio.run(call_runtime)

    assert result["success"] is False
    assert result["failure_stage"] == "brownfield_contract_validation"
    assert result["brownfield_retry_attempted"] is True
    assert result["brownfield_retry_count"] == 1
    assert result["brownfield_retry_marker"] == "BROWNFIELD CONTRACT RETRY"
    assert result["brownfield_retry_failed_stage"] == (
        "brownfield_contract_validation"
    )
    expected_call_count = 2
    assert len(calls) == expected_call_count
    assert result["input_context"]["user_input"] == calls[1]
    assert "BROWNFIELD CONTRACT RETRY" in result["input_context"]["user_input"]


def test_backlog_generate_returns_failure_envelope_for_runtime_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backlog runtime failures must be loud to agent-facing CLI callers."""

    def fake_select_project(
        product_id: int, tool_context: SimpleNamespace
    ) -> dict[str, Any]:
        state = tool_context.state
        state["pending_spec_content"] = "SPEC CONTENT"
        state["compiled_authority_cached"] = "AUTHORITY JSON"
        return {"success": True, "project_id": product_id}

    async def fake_run_backlog_agent_from_state(
        state: dict[str, Any],
        *,
        project_id: int,
        user_input: str | None,
    ) -> dict[str, Any]:
        del state, project_id, user_input
        return {
            "success": False,
            "error": "BACKLOG_GENERATION_FAILED",
            "failure_stage": "invocation_exception",
            "failure_summary": "provider rejected model",
            "failure_artifact_id": "backlog-failure-1",
            "has_full_artifact": True,
            "input_context": {
                "product_vision_statement": "A clear saved vision.",
                "technical_spec": "SPEC CONTENT",
                "compiled_authority": "AUTHORITY JSON",
                "prior_backlog_state": "NO_HISTORY",
                "as_built_assessment": "NO_AS_BUILT_ASSESSMENT",
                "user_input": "",
            },
            "output_artifact": {
                "is_complete": False,
                "error": "BACKLOG_GENERATION_FAILED",
                "failure_summary": "provider rejected model",
            },
            "is_complete": False,
        }

    monkeypatch.setattr(
        "services.agent_workbench.backlog_phase.select_project",
        fake_select_project,
    )
    monkeypatch.setattr(
        "services.agent_workbench.backlog_phase.run_backlog_agent_from_state",
        fake_run_backlog_agent_from_state,
    )
    runner = BacklogPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=_FakeWorkflowService(),
    )

    result = runner.generate(project_id=2)

    assert result["ok"] is False
    assert result["data"] is None
    assert result["errors"][0]["code"] == "MUTATION_FAILED"
    assert result["errors"][0]["details"]["backlog_run_success"] is False
    assert result["errors"][0]["details"]["failure_stage"] == "invocation_exception"


def test_backlog_reconcile_supersedes_legacy_duplicate_active_seed_rows(
    session: Session,
) -> None:
    """Legacy duplicate Backlog saves should collapse to one active seed cohort."""
    product = Product(name="Cartola")
    session.add(product)
    session.commit()
    session.refresh(product)
    assert product.product_id is not None
    product_id = product.product_id
    base = datetime(2026, 5, 22, 12, tzinfo=UTC)
    for offset, title, rank in [
        (0, "Old lineup import", "1"),
        (1, "Old projection view", "2"),
        (10, "Refined lineup import", "1"),
        (11, "Refined projection view", "2"),
    ]:
        session.add(
            UserStory(
                product_id=product_id,
                title=title,
                status=StoryStatus.TO_DO,
                rank=rank,
                story_origin="backlog_seed",
                is_refined=False,
                is_superseded=False,
                created_at=base + timedelta(minutes=offset),
                updated_at=base + timedelta(minutes=offset),
            )
        )
    session.add(
        WorkflowEvent(
            event_type=WorkflowEventType.BACKLOG_SAVED,
            product_id=product_id,
            timestamp=base + timedelta(minutes=2),
            event_metadata=json.dumps({"processed_count": 2, "created_count": 2}),
        )
    )
    session.add(
        WorkflowEvent(
            event_type=WorkflowEventType.BACKLOG_SAVED,
            product_id=product_id,
            timestamp=base + timedelta(minutes=12),
            event_metadata=json.dumps({"processed_count": 2, "created_count": 2}),
        )
    )
    session.commit()

    runner = BacklogPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=_FakeWorkflowService(),
    )

    result = runner.reconcile(
        project_id=product_id,
        idempotency_key="reconcile-backlog-legacy-1",
    )

    assert result["ok"] is True
    assert result["data"]["active_before"] == 4  # noqa: PLR2004
    assert result["data"]["active_after"] == 2  # noqa: PLR2004
    assert result["data"]["superseded_count"] == 2  # noqa: PLR2004
    rows = session.exec(
        select(UserStory)
        .where(UserStory.product_id == product_id)
        .order_by(cast("Any", UserStory.story_id))
    ).all()
    assert [row.title for row in rows if not row.is_superseded] == [
        "Refined lineup import",
        "Refined projection view",
    ]
    assert [row.title for row in rows if row.is_superseded] == [
        "Old lineup import",
        "Old projection view",
    ]


def test_backlog_reconcile_blocks_when_existing_backlog_progressed(
    session: Session,
) -> None:
    """Canonical backlog repair must fail closed once any active row progressed."""
    product = Product(name="Cartola")
    session.add(product)
    session.commit()
    session.refresh(product)
    assert product.product_id is not None
    product_id = product.product_id
    session.add_all(
        [
            UserStory(
                product_id=product_id,
                title="Old lineup import",
                status=StoryStatus.TO_DO,
                rank="1",
                story_origin="backlog_seed",
                is_refined=False,
                is_superseded=False,
            ),
            UserStory(
                product_id=product_id,
                title="Refined projection view",
                status=StoryStatus.IN_PROGRESS,
                rank="1",
                story_origin="backlog_seed",
                is_refined=False,
                is_superseded=False,
            ),
        ]
    )
    session.commit()
    runner = BacklogPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=_FakeWorkflowService(),
    )

    result = runner.reconcile(
        project_id=product_id,
        idempotency_key="reconcile-backlog-blocked-1",
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "MUTATION_FAILED"
    assert result["errors"][0]["details"]["blocked_count"] == 1
    assert (
        session.exec(
            select(UserStory).where(
                UserStory.product_id == product_id,
                UserStory.is_superseded == True,  # noqa: E712
            )
        ).all()
        == []
    )
