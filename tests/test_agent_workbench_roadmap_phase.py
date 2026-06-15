"""Tests for agent workbench Roadmap phase runner."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

from sqlmodel import Session, SQLModel, create_engine

from models.core import Product, UserStory
from models.enums import StoryStatus
from models.specs import SpecRegistry
from services.agent_workbench.roadmap_phase import RoadmapPhaseRunner
from services.roadmap_runtime import build_roadmap_input_context

BASE_SPEC_VERSION_ID = 11
AMENDED_SPEC_VERSION_ID = 12


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
            "fsm_state": "ROADMAP_INTERVIEW",
            "setup_status": "passed",
            "product_vision_assessment": {
                "product_vision_statement": "A clear saved vision.",
                "is_complete": True,
            },
            "backlog_items": [
                {
                    "priority": 1,
                    "requirement": "Choose weekly squad",
                    "value_driver": "Strategic",
                    "justification": "Core value",
                    "estimated_effort": "M",
                }
            ],
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


def test_roadmap_generate_hydrates_vision_spec_authority_and_backlog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Roadmap generate must pass Vision, spec, authority, and Backlog to agent."""
    captured: dict[str, Any] = {}

    def fake_select_project(
        product_id: int, tool_context: SimpleNamespace
    ) -> dict[str, Any]:
        state = tool_context.state
        state["pending_spec_content"] = "SPEC CONTENT"
        state["compiled_authority_cached"] = "AUTHORITY JSON"
        return {"success": True, "project_id": product_id}

    async def fake_run_roadmap_agent_from_state(
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
                "product_vision": state["product_vision_assessment"][
                    "product_vision_statement"
                ],
                "technical_spec": state.get("pending_spec_content"),
                "compiled_authority": state.get("compiled_authority_cached"),
                "backlog_items": state.get("backlog_items"),
                "prior_roadmap_state": "NO_HISTORY",
                "user_input": user_input or "",
            },
            "output_artifact": {
                "roadmap_releases": [
                    {
                        "release_name": "Milestone 1",
                        "theme": "Foundation",
                        "focus_area": "Technical Foundation",
                        "items": ["Choose weekly squad"],
                        "reasoning": "Start here",
                    }
                ],
                "roadmap_summary": "Draft roadmap",
                "is_complete": False,
                "clarifying_questions": ["Which milestone first?"],
            },
            "is_complete": False,
            "error": None,
        }

    monkeypatch.setattr(
        "services.agent_workbench.roadmap_phase.select_project",
        fake_select_project,
    )
    monkeypatch.setattr(
        "services.agent_workbench.roadmap_phase.run_roadmap_agent_from_state",
        fake_run_roadmap_agent_from_state,
    )
    runner = RoadmapPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=_FakeWorkflowService(),
    )

    result = runner.generate(project_id=2, user_input="draft roadmap")

    assert result["ok"] is True
    assert captured["state"]["pending_spec_content"] == "SPEC CONTENT"
    assert captured["state"]["compiled_authority_cached"] == "AUTHORITY JSON"
    assert captured["state"]["backlog_items"][0]["requirement"] == (
        "Choose weekly squad"
    )
    assert result["data"]["input_context"]["technical_spec"] == "SPEC CONTENT"
    assert result["data"]["input_context"]["compiled_authority"] == "AUTHORITY JSON"
    assert result["data"]["input_context"]["backlog_items"][0]["requirement"] == (
        "Choose weekly squad"
    )


def test_roadmap_generate_returns_failure_envelope_for_runtime_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Roadmap runtime failures must be loud to agent-facing CLI callers."""

    def fake_select_project(
        product_id: int, tool_context: SimpleNamespace
    ) -> dict[str, Any]:
        state = tool_context.state
        state["pending_spec_content"] = "SPEC CONTENT"
        state["compiled_authority_cached"] = "AUTHORITY JSON"
        return {"success": True, "project_id": product_id}

    async def fake_run_roadmap_agent_from_state(
        state: dict[str, Any],
        *,
        project_id: int,
        user_input: str | None,
    ) -> dict[str, Any]:
        del state, project_id, user_input
        return {
            "success": False,
            "error": "ROADMAP_GENERATION_FAILED",
            "failure_stage": "invocation_exception",
            "failure_summary": "provider rejected model",
            "failure_artifact_id": "roadmap-failure-1",
            "has_full_artifact": True,
            "input_context": {
                "product_vision": "A clear saved vision.",
                "technical_spec": "SPEC CONTENT",
                "compiled_authority": "AUTHORITY JSON",
                "backlog_items": [],
                "prior_roadmap_state": "NO_HISTORY",
                "user_input": "",
            },
            "output_artifact": {
                "is_complete": False,
                "error": "ROADMAP_GENERATION_FAILED",
                "failure_summary": "provider rejected model",
            },
            "is_complete": False,
        }

    monkeypatch.setattr(
        "services.agent_workbench.roadmap_phase.select_project",
        fake_select_project,
    )
    monkeypatch.setattr(
        "services.agent_workbench.roadmap_phase.run_roadmap_agent_from_state",
        fake_run_roadmap_agent_from_state,
    )
    runner = RoadmapPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=_FakeWorkflowService(),
    )

    result = runner.generate(project_id=2)

    assert result["ok"] is False
    assert result["data"] is None
    assert result["errors"][0]["code"] == "MUTATION_FAILED"
    assert result["errors"][0]["details"]["roadmap_run_success"] is False
    assert result["errors"][0]["details"]["failure_stage"] == "invocation_exception"


def test_roadmap_generate_reloads_active_seed_backlog_after_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Active reset must route Roadmap through persisted seed rows, not stale state."""
    engine = create_engine("sqlite://", echo=False)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Product(product_id=2, name="Cartola"))
        session.add(
            UserStory(
                product_id=2,
                title="Validate Captain-Aware Optimization Contract",
                status=StoryStatus.TO_DO,
                rank="1",
                story_points=3,
                story_description="Verify existing captain multiplier behavior.",
                story_origin="backlog_seed",
                is_superseded=False,
            )
        )
        session.commit()

    workflow = _FakeWorkflowService()
    workflow.state.update(
        {
            "fsm_state": "BACKLOG_PERSISTENCE",
            "downstream_backlog_stale": True,
            "stale_backlog_reason": "active_backlog_reset",
            "stale_since_backlog_attempt_id": "backlog-attempt-12",
            "active_backlog_reset_attempt_id": "backlog-attempt-12",
            "backlog_items": [
                {
                    "priority": 1,
                    "requirement": "Stale refined item",
                    "value_driver": "Strategic",
                    "justification": "Old refinement artifact item.",
                    "estimated_effort": "M",
                    "item_id": "item-001",
                    "item_fingerprint": "sha256:item",
                    "classification": "verification",
                    "refinement_provenance": {"operation_id": "op-1"},
                    "source_attempt_id": "backlog-attempt-12",
                    "source_artifact_fingerprint": "sha256:source",
                }
            ],
        }
    )
    captured: dict[str, Any] = {}

    def fake_select_project(
        product_id: int, tool_context: SimpleNamespace
    ) -> dict[str, Any]:
        state = tool_context.state
        state["pending_spec_content"] = "SPEC CONTENT"
        state["compiled_authority_cached"] = "AUTHORITY JSON"
        return {"success": True, "project_id": product_id}

    async def fake_run_roadmap_agent_from_state(
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
                "product_vision": state["product_vision_assessment"][
                    "product_vision_statement"
                ],
                "technical_spec": state.get("pending_spec_content"),
                "compiled_authority": state.get("compiled_authority_cached"),
                "backlog_items": state.get("backlog_items"),
                "prior_roadmap_state": "NO_HISTORY",
                "user_input": user_input or "",
            },
            "output_artifact": {
                "roadmap_releases": [
                    {
                        "release_name": "Milestone 1",
                        "theme": "Foundation",
                        "focus_area": "Technical Foundation",
                        "items": ["Validate Captain-Aware Optimization Contract"],
                        "reasoning": "Start from active reset baseline.",
                    }
                ],
                "roadmap_summary": "Draft roadmap",
                "is_complete": False,
                "clarifying_questions": [],
            },
            "is_complete": False,
            "error": None,
        }

    monkeypatch.setattr(
        "services.agent_workbench.roadmap_phase.get_engine",
        lambda: engine,
    )
    monkeypatch.setattr(
        "services.agent_workbench.roadmap_phase.select_project",
        fake_select_project,
    )
    monkeypatch.setattr(
        "services.agent_workbench.roadmap_phase.run_roadmap_agent_from_state",
        fake_run_roadmap_agent_from_state,
    )
    runner = RoadmapPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=workflow,
    )

    result = runner.generate(project_id=2, user_input="Regenerate after reset")

    assert result["ok"] is True
    assert captured["state"]["backlog_items"] == [
        {
            "priority": 1,
            "requirement": "Validate Captain-Aware Optimization Contract",
            "value_driver": "Strategic",
            "justification": "Verify existing captain multiplier behavior.",
            "estimated_effort": "M",
        }
    ]


def test_roadmap_generate_uses_scope_extension_backlog_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scope extension Roadmap generation must hydrate appended extension backlog."""
    engine = create_engine("sqlite://", echo=False)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Product(product_id=2, name="Cartola"))
        session.add(
            SpecRegistry(
                spec_version_id=12,
                product_id=2,
                spec_hash="sha256:amended",
                content="AMENDED SPEC",
                status="approved",
            )
        )
        session.commit()
        session.add(
            UserStory(
                product_id=2,
                title="Add analyst export",
                status=StoryStatus.TO_DO,
                rank="2",
                story_points=1,
                story_description="New scope from amended spec.",
                story_origin="scope_extension",
                accepted_spec_version_id=12,
                is_superseded=False,
            )
        )
        session.commit()

    workflow = _FakeWorkflowService()
    existing_roadmap = [
        {
            "release_name": "Milestone 1",
            "theme": "Foundation",
            "focus_area": "Technical Foundation",
            "items": ["Choose weekly squad"],
            "reasoning": "Already approved.",
        }
    ]
    workflow.state.update(
        {
            "fsm_state": "BACKLOG_PERSISTENCE",
            "scope_extension_context": {
                "schema": "agileforge.scope_extension.v1",
                "base_spec_version_id": BASE_SPEC_VERSION_ID,
                "base_spec_hash": "sha256:base",
                "amended_spec_version_id": AMENDED_SPEC_VERSION_ID,
                "amended_spec_hash": "sha256:amended",
                "added_source_item_ids": ["REQ.new-analytics"],
                "backlog_extension_saved_at": "2026-06-14T10:00:00Z",
                "backlog_extension_attempt_id": "backlog-attempt-4",
                "backlog_extension_artifact_fingerprint": "sha256:backlog",
            },
            "roadmap_releases": existing_roadmap,
            "backlog_items": [
                {
                    "priority": 1,
                    "requirement": "Choose weekly squad",
                    "value_driver": "Strategic",
                    "justification": "Old backlog item.",
                    "estimated_effort": "M",
                }
            ],
        }
    )
    captured: dict[str, Any] = {}

    def fake_select_project(
        product_id: int, tool_context: SimpleNamespace
    ) -> dict[str, Any]:
        state = tool_context.state
        state["pending_spec_content"] = "AMENDED SPEC"
        state["compiled_authority_cached"] = "AUTHORITY JSON"
        return {"success": True, "project_id": product_id}

    async def fake_run_roadmap_agent_from_state(
        state: dict[str, Any],
        *,
        project_id: int,
        user_input: str | None,
    ) -> dict[str, Any]:
        del project_id
        input_context = build_roadmap_input_context(state, user_input=user_input)
        captured["state"] = dict(state)
        captured["input_context"] = input_context
        return {
            "success": True,
            "input_context": input_context,
            "output_artifact": {
                "roadmap_releases": [
                    {
                        "release_name": "Milestone 2",
                        "theme": "Analytics",
                        "focus_area": "User Value",
                        "items": ["Add analyst export"],
                        "reasoning": "Append the accepted extension scope.",
                    }
                ],
                "roadmap_summary": "Extension roadmap",
                "is_complete": True,
                "clarifying_questions": [],
            },
            "is_complete": True,
            "error": None,
        }

    monkeypatch.setattr(
        "services.agent_workbench.roadmap_phase.get_engine",
        lambda: engine,
    )
    monkeypatch.setattr(
        "services.agent_workbench.roadmap_phase.select_project",
        fake_select_project,
    )
    monkeypatch.setattr(
        "services.agent_workbench.roadmap_phase.run_roadmap_agent_from_state",
        fake_run_roadmap_agent_from_state,
    )
    runner = RoadmapPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=workflow,
    )

    result = runner.generate(project_id=2)

    assert result["ok"] is True
    assert captured["state"]["backlog_items"] == [
        {
            "priority": 2,
            "requirement": "Add analyst export",
            "value_driver": "Strategic",
            "justification": "New scope from amended spec.",
            "estimated_effort": "S",
            "story_origin": "scope_extension",
            "accepted_spec_version_id": AMENDED_SPEC_VERSION_ID,
        }
    ]
    assert captured["input_context"]["generation_mode"] == "scope_extension"
    assert captured["input_context"]["existing_roadmap_context"] == existing_roadmap
    assert captured["input_context"]["extension_backlog_items"] == [
        {
            "requirement": "Add analyst export",
            "accepted_spec_version_id": AMENDED_SPEC_VERSION_ID,
            "source_item_ids": ["REQ.new-analytics"],
        }
    ]


def test_roadmap_generate_after_extension_save_hydrates_normal_backlog_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completed extension Roadmap saves must not force extension-only hydration."""
    engine = create_engine("sqlite://", echo=False)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Product(product_id=2, name="Cartola"))
        session.add(
            SpecRegistry(
                spec_version_id=12,
                product_id=2,
                spec_hash="sha256:amended",
                content="AMENDED SPEC",
                status="approved",
            )
        )
        session.commit()
        session.add(
            UserStory(
                product_id=2,
                title="Choose weekly squad",
                status=StoryStatus.TO_DO,
                rank="1",
                story_points=3,
                story_description="Core normal backlog item.",
                story_origin="backlog_seed",
                is_superseded=False,
            )
        )
        session.add(
            UserStory(
                product_id=2,
                title="Add analyst export",
                status=StoryStatus.TO_DO,
                rank="2",
                story_points=1,
                story_description="New scope from amended spec.",
                story_origin="scope_extension",
                accepted_spec_version_id=12,
                is_superseded=False,
            )
        )
        session.commit()

    workflow = _FakeWorkflowService()
    workflow.state.update(
        {
            "fsm_state": "STORY_INTERVIEW",
            "scope_extension_context": {
                "schema": "agileforge.scope_extension.v1",
                "base_spec_version_id": BASE_SPEC_VERSION_ID,
                "base_spec_hash": "sha256:base",
                "amended_spec_version_id": AMENDED_SPEC_VERSION_ID,
                "amended_spec_hash": "sha256:amended",
                "added_source_item_ids": ["REQ.new-analytics"],
                "backlog_extension_saved_at": "2026-06-14T10:00:00Z",
                "backlog_extension_attempt_id": "backlog-attempt-4",
                "backlog_extension_artifact_fingerprint": "sha256:backlog",
                "roadmap_extension_saved_at": "2026-06-14T12:00:00Z",
                "roadmap_extension_attempt_id": "roadmap-attempt-2",
                "roadmap_extension_artifact_fingerprint": "sha256:roadmap",
            },
            "roadmap_releases": [
                {
                    "release_name": "Milestone 1",
                    "theme": "Foundation",
                    "focus_area": "Technical Foundation",
                    "items": ["Choose weekly squad"],
                    "reasoning": "Already approved.",
                },
                {
                    "release_name": "Milestone 2",
                    "theme": "Analytics",
                    "focus_area": "User Value",
                    "items": ["Add analyst export"],
                    "reasoning": "Saved extension scope.",
                },
            ],
            "backlog_items": [],
        }
    )
    captured: dict[str, Any] = {}

    def fake_select_project(
        product_id: int, tool_context: SimpleNamespace
    ) -> dict[str, Any]:
        state = tool_context.state
        state["pending_spec_content"] = "AMENDED SPEC"
        state["compiled_authority_cached"] = "AUTHORITY JSON"
        return {"success": True, "project_id": product_id}

    async def fake_run_roadmap_agent_from_state(
        state: dict[str, Any],
        *,
        project_id: int,
        user_input: str | None,
    ) -> dict[str, Any]:
        del project_id
        input_context = build_roadmap_input_context(state, user_input=user_input)
        captured["state"] = dict(state)
        captured["input_context"] = input_context
        return {
            "success": True,
            "input_context": input_context,
            "output_artifact": {
                "roadmap_releases": [
                    {
                        "release_name": "Milestone 1",
                        "theme": "Foundation",
                        "focus_area": "Technical Foundation",
                        "items": ["Choose weekly squad"],
                        "reasoning": "Normal roadmap refinement.",
                    }
                ],
                "roadmap_summary": "Normal roadmap",
                "is_complete": False,
                "clarifying_questions": [],
            },
            "is_complete": False,
            "error": None,
        }

    monkeypatch.setattr(
        "services.agent_workbench.roadmap_phase.get_engine",
        lambda: engine,
    )
    monkeypatch.setattr(
        "services.agent_workbench.roadmap_phase.select_project",
        fake_select_project,
    )
    monkeypatch.setattr(
        "services.agent_workbench.roadmap_phase.run_roadmap_agent_from_state",
        fake_run_roadmap_agent_from_state,
    )
    runner = RoadmapPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=workflow,
    )

    result = runner.generate(project_id=2, user_input="refine normal roadmap")

    assert result["ok"] is True
    assert captured["state"]["backlog_items"] == [
        {
            "priority": 1,
            "requirement": "Choose weekly squad",
            "value_driver": "Strategic",
            "justification": "Core normal backlog item.",
            "estimated_effort": "M",
        }
    ]
    assert "generation_mode" not in captured["input_context"]
    assert "existing_roadmap_context" not in captured["input_context"]
    assert captured["input_context"]["backlog_items"] == [
        {
            "priority": 1,
            "requirement": "Choose weekly squad",
            "value_driver": "Strategic",
            "justification": "Core normal backlog item.",
            "estimated_effort": "M",
        }
    ]
