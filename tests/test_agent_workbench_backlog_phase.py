"""Tests for agent workbench Backlog phase runner."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from sqlmodel import select

from models.core import Product, UserStory
from models.enums import StoryStatus, WorkflowEventType
from models.events import WorkflowEvent
from services.agent_workbench.backlog_phase import BacklogPhaseRunner
from services.backlog_runtime import build_backlog_input_context

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
    assert result["data"]["input_context"]["implementation_evidence"] == (
        '{"schema_version":"agileforge.reconciliation_report.v1","findings":[]}'
    )
    assert result["data"]["input_context"]["implementation_evidence"].startswith(
        '{"schema_version"'
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
