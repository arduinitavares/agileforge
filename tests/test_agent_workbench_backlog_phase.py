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
from utils.brownfield_annotations import (
    BrownfieldAnnotation,
    BrownfieldDisagreement,
    BrownfieldModelAssertion,
    BrownfieldSelectedCapability,
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


def test_backlog_preview_returns_brownfield_warnings_without_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workbench preview should expose brownfield warnings as review data."""

    async def fake_run_backlog_agent_from_state(
        state: dict[str, Any],
        *,
        project_id: int,
        user_input: str | None,
    ) -> dict[str, Any]:
        del state, project_id, user_input
        return {
            "success": True,
            "error": None,
            "failure_stage": None,
            "failure_summary": None,
            "failure_artifact_id": None,
            "has_full_artifact": False,
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
                "backlog_items": [{"requirement": "Verify current behavior"}],
                "is_complete": True,
                "clarifying_questions": [],
                "brownfield_warnings": [
                    {
                        "code": "possible_mapping",
                        "item_index": 0,
                        "severity": "review",
                        "match_tier": "fuzzy",
                        "authority_ref": None,
                        "invariant_refs": [],
                        "message": "Possible As-Built mapping.",
                        "details": {},
                    }
                ],
            },
            "is_complete": True,
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

    assert result["ok"] is True
    assert result["data"]["persisted"] is False
    assert (
        result["data"]["output_artifact"]["brownfield_warnings"][0]["code"]
        == "possible_mapping"
    )


def test_backlog_preview_omits_brownfield_repair_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Brownfield issues are warnings now, so preview has no repair diagnostics."""

    async def fake_run_backlog_agent_from_state(
        state: dict[str, Any],
        *,
        project_id: int,
        user_input: str | None,
    ) -> dict[str, Any]:
        del state, project_id, user_input
        return {
            "success": True,
            "error": None,
            "failure_stage": None,
            "failure_summary": None,
            "failure_artifact_id": None,
            "has_full_artifact": False,
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
                "backlog_items": [{"requirement": "Verify current behavior"}],
                "is_complete": True,
                "clarifying_questions": [],
                "brownfield_warnings": [],
            },
            "is_complete": True,
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

    assert result["ok"] is True
    assert "brownfield_retry_attempted" not in result["data"]
    assert "brownfield_retry_marker" not in result["data"]


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


def test_brownfield_annotation_schema_represents_dual_provenance() -> None:
    """Annotation schema must preserve host and model values side by side."""
    annotation = BrownfieldAnnotation.model_validate(
        {
            "schema_version": "agileforge.brownfield_annotation.v1",
            "source": "host_derived",
            "match_tier": "exact",
            "match_basis": ["authority_ref"],
            "conflict": False,
            "selected": {
                "authority_ref": "QUALITY.security-secrets",
                "capability_title": "Security Secrets",
                "invariant_refs": ["INV-506454637a21ed73"],
                "as_built_status": "not_observed",
                "recommended_backlog_treatment": "create_discovery_item",
                "confidence": "medium",
            },
            "candidates": [],
            "model_assertion": {
                "source": "model_asserted",
                "authority_ref": "QUALITY.security-secrets",
                "capability_hint": "secrets protection",
                "as_built_status": "observed",
                "recommended_backlog_treatment": "skip_new_implementation",
            },
            "disagreements": [
                {
                    "field": "as_built_status",
                    "model_value": "observed",
                    "host_value": "not_observed",
                    "code": "status_disagreement",
                }
            ],
            "warning_codes": ["status_disagreement"],
        }
    )

    assert isinstance(annotation.selected, BrownfieldSelectedCapability)
    assert isinstance(annotation.model_assertion, BrownfieldModelAssertion)
    assert isinstance(annotation.disagreements[0], BrownfieldDisagreement)
    assert annotation.warning_codes == ["status_disagreement"]


def test_backlog_runtime_fills_annotation_from_exact_authority_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exact model authority refs should produce host-derived annotations."""
    result = _run_brownfield_backlog_runtime(
        monkeypatch,
        {
            "requirement": "Verify Live Squad Recommendation",
            "authority_ref": "REQ.live-squad-recommendation",
            "value_driver": "Strategic",
            "justification": "Validate existing behavior.",
            "estimated_effort": "S",
        },
    )

    assert result["success"] is True
    item = result["output_artifact"]["backlog_items"][0]
    annotation = item["as_built_annotation"]
    assert annotation["match_tier"] == "exact"
    assert annotation["selected"]["authority_ref"] == "REQ.live-squad-recommendation"
    assert annotation["selected"]["as_built_status"] == "observed"
    assert annotation["selected"]["recommended_backlog_treatment"] == (
        "skip_new_implementation"
    )
    assert "metadata_filled_by_host" in annotation["warning_codes"]
    assert result["output_artifact"]["brownfield_warnings"][0]["code"] == (
        "metadata_filled_by_host"
    )


def test_backlog_runtime_warns_on_fuzzy_mapping_without_authoritative_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fuzzy capability-title matches should warn without selecting a contract."""
    result = _run_brownfield_backlog_runtime(
        monkeypatch,
        {
            "requirement": "Live Squad Recommendation Evidence",
            "value_driver": "Strategic",
            "justification": "Looks related but has no exact authority ref.",
            "estimated_effort": "S",
        },
    )

    assert result["success"] is True
    item = result["output_artifact"]["backlog_items"][0]
    annotation = item["as_built_annotation"]
    assert annotation["match_tier"] == "fuzzy"
    assert annotation["selected"] is None
    assert annotation["candidates"][0]["authority_ref"] == (
        "REQ.live-squad-recommendation"
    )
    assert "possible_mapping" in annotation["warning_codes"]


def test_annotation_preserves_status_and_treatment_from_as_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host annotation must carry As-Built status/treatment, not model guesses."""
    assessment = _as_built_assessment_payload()
    assessment["capability_assessments"] = [
        {
            "authority_ref": "QUALITY.security-secrets",
            "invariant_refs": ["INV-506454637a21ed73"],
            "capability_title": "Security Secrets",
            "status": "not_observed",
            "confidence": "medium",
            "evidence": [],
            "limitations": ["No direct proof."],
            "recommended_backlog_treatment": "create_discovery_item",
            "reasoning": "Indirect hygiene only.",
        }
    ]

    result = _run_brownfield_backlog_runtime(
        monkeypatch,
        {
            "requirement": "Verify Secrets Protection",
            "authority_ref": "QUALITY.security-secrets",
            "capability_hint": "secrets protection",
            "value_driver": "Strategic",
            "justification": "Review secret handling evidence.",
            "estimated_effort": "S",
        },
        assessment=assessment,
    )

    assert result["success"] is True
    item = result["output_artifact"]["backlog_items"][0]
    selected = item["as_built_annotation"]["selected"]
    assert selected["as_built_status"] == "not_observed"
    assert selected["recommended_backlog_treatment"] == "create_discovery_item"


def test_annotation_preserves_legacy_model_disagreement_as_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy model-owned fields are stripped but retained as disagreement data."""
    assessment = _as_built_assessment_payload()
    assessment["capability_assessments"] = [
        {
            "authority_ref": "QUALITY.security-secrets",
            "invariant_refs": ["INV-506454637a21ed73"],
            "capability_title": "Security Secrets",
            "status": "not_observed",
            "confidence": "medium",
            "evidence": [],
            "limitations": ["No direct proof."],
            "recommended_backlog_treatment": "create_discovery_item",
            "reasoning": "Indirect hygiene only.",
        }
    ]

    result = _run_brownfield_backlog_runtime(
        monkeypatch,
        {
            "requirement": "Verify Secrets Protection",
            "capability_name": "Secrets Protection",
            "authority_ref": "QUALITY.security-secrets",
            "as_built_status": "observed",
            "recommended_backlog_treatment": "skip_new_implementation",
            "value_driver": "Strategic",
            "justification": "Review secret handling evidence.",
            "estimated_effort": "S",
        },
        assessment=assessment,
    )

    assert result["success"] is True
    item = result["output_artifact"]["backlog_items"][0]
    assert "capability_name" not in item
    assert "as_built_status" not in item
    assert "recommended_backlog_treatment" not in item
    annotation = item["as_built_annotation"]
    assert annotation["model_assertion"]["as_built_status"] == "observed"
    assert annotation["model_assertion"]["recommended_backlog_treatment"] == (
        "skip_new_implementation"
    )
    assert "status_disagreement" in annotation["warning_codes"]
    assert "treatment_disagreement" in annotation["warning_codes"]
    disagreement_codes = {
        disagreement["code"] for disagreement in annotation["disagreements"]
    }
    assert {"status_disagreement", "treatment_disagreement"} <= disagreement_codes


def test_annotation_warns_when_exact_ref_conflicts_with_item_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid exact ref with unrelated item text should be warning-only."""
    assessment = _as_built_assessment_payload()
    assessment["capability_assessments"] = [
        {
            "authority_ref": "REQ.real-submit-disabled",
            "invariant_refs": ["INV-real-submit-disabled"],
            "capability_title": "Real Submit Disabled",
            "status": "observed",
            "confidence": "medium",
            "evidence": [],
            "limitations": [],
            "recommended_backlog_treatment": "skip_new_implementation",
            "reasoning": "Real-submit safety is already represented.",
        }
    ]

    result = _run_brownfield_backlog_runtime(
        monkeypatch,
        {
            "requirement": "Verify Secrets Protection",
            "authority_ref": "REQ.real-submit-disabled",
            "capability_hint": "security secrets",
            "value_driver": "Strategic",
            "justification": "The ref is valid but points elsewhere.",
            "estimated_effort": "S",
        },
        assessment=assessment,
    )

    assert result["success"] is True
    annotation = result["output_artifact"]["backlog_items"][0][
        "as_built_annotation"
    ]
    assert annotation["match_tier"] == "exact"
    assert annotation["selected"]["authority_ref"] == "REQ.real-submit-disabled"
    assert "capability_disagreement" in annotation["warning_codes"]
    assert annotation["disagreements"][0]["code"] == "capability_disagreement"


def test_annotation_warns_on_unmatched_asserted_authority_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unbacked authority refs are preview warnings and save blockers."""
    result = _run_brownfield_backlog_runtime(
        monkeypatch,
        {
            "requirement": "Verify imaginary billing adapter",
            "authority_ref": "REQ.imaginary-billing-adapter",
            "capability_hint": "Imaginary billing adapter",
            "value_driver": "Strategic",
            "justification": "Model asserted a missing authority ref.",
            "estimated_effort": "S",
        },
    )

    assert result["success"] is True
    annotation = result["output_artifact"]["backlog_items"][0][
        "as_built_annotation"
    ]
    assert annotation["match_tier"] == "none"
    assert annotation["selected"] is None
    assert "asserted_authority_ref_unmatched" in annotation["warning_codes"]
    warning = result["output_artifact"]["brownfield_warnings"][0]
    assert warning["code"] == "asserted_authority_ref_unmatched"
    assert warning["severity"] == "block_on_save"


def test_annotation_warns_on_conflicting_invariant_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed invariant-level rows should stay warning-only for preview."""
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
            "value_driver": "Strategic",
            "justification": "Review mixed invariant evidence.",
            "estimated_effort": "S",
        },
        assessment=assessment,
    )

    assert result["success"] is True
    annotation = result["output_artifact"]["backlog_items"][0][
        "as_built_annotation"
    ]
    assert annotation["match_tier"] == "exact"
    assert annotation["conflict"] is True
    assert annotation["selected"] is None
    assert "conflicting_invariants" in annotation["warning_codes"]
    assert len(annotation["candidates"]) == 2  # noqa: PLR2004


def test_runtime_strips_model_supplied_annotation_without_as_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Greenfield output should not receive host-owned annotations."""

    async def fake_invoke_backlog_agent(payload: object) -> str:
        del payload
        return _backlog_output_json(
            {
                "requirement": "Build New Feature",
                "authority_ref": None,
                "capability_hint": None,
                "value_driver": "Strategic",
                "justification": "No As-Built cache exists.",
                "estimated_effort": "S",
            }
        )

    monkeypatch.setattr(
        "services.backlog_runtime._invoke_backlog_agent",
        fake_invoke_backlog_agent,
    )

    async def call_runtime() -> dict[str, Any]:
        return await run_backlog_agent_from_state(
            {
                "product_vision_assessment": {
                    "product_vision_statement": "A clear saved vision.",
                    "is_complete": True,
                },
                "pending_spec_content": "SPEC CONTENT",
                "compiled_authority_cached": "AUTHORITY JSON",
            },
            project_id=2,
            user_input="draft backlog",
        )

    result = anyio.run(call_runtime)

    assert result["success"] is True
    item = result["output_artifact"]["backlog_items"][0]
    assert "as_built_annotation" not in item
    assert result["output_artifact"]["brownfield_warnings"] == []


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
