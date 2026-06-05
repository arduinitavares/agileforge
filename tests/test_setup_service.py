"""Tests for setup service."""

from types import SimpleNamespace
from typing import Any, Never

import pytest

JsonDict = dict[str, Any]


@pytest.mark.asyncio
async def test_run_project_setup_marks_compile_failure_and_skips_auto_vision() -> None:
    """Verify run project setup marks compile failure and skips auto vision."""
    from services.setup_service import run_project_setup  # noqa: PLC0415

    context = SimpleNamespace(state={"fsm_state": "SETUP_REQUIRED"}, session_id="1")
    saved: JsonDict = {}
    calls: JsonDict = {}

    async def hydrate_context(session_id: str, project_id: int) -> SimpleNamespace:
        calls["hydrate"] = (session_id, project_id)
        return context

    def build_tool_context(ctx: object) -> object:
        return ctx

    def link_spec_to_product(
        params: JsonDict,
        tool_context: object = None,
    ) -> JsonDict:
        calls["link"] = {
            "params": params,
            "tool_context": tool_context,
        }
        return {
            "success": True,
            "compile_success": False,
            "compile_error": "invalid spec path",
            "failure_artifact_id": "setup-artifact-1",
            "failure_stage": "output_validation",
            "failure_summary": "SPEC_COMPILATION_FAILED: invalid spec path",
            "raw_output_preview": '{"invalid": true}',
            "has_full_artifact": True,
        }

    def refresh_project_context(project_id: int, tool_context: object) -> JsonDict:
        calls["refresh"] = (project_id, tool_context)
        return {"success": True}

    def load_project(project_id: int) -> object:
        calls["load_project"] = project_id
        return SimpleNamespace(
            product_id=project_id,
            spec_file_path="invalid/path.md",
            compiled_authority_json=None,
        )

    def setup_blocker(_product: object) -> str | None:
        return None

    async def run_vision_agent(
        _state: object, *, project_id: int, user_input: str
    ) -> Never:
        del project_id, user_input
        msg = "vision agent should not be called on compile failure"
        raise AssertionError(msg)

    def save_session_state(session_id: str, state: JsonDict) -> None:
        saved["session_id"] = session_id
        saved["state"] = dict(state)

    result = await run_project_setup(
        session_id="1",
        project_id=1,
        spec_file_path="invalid/path.md",
        hydrate_context=hydrate_context,
        build_tool_context=build_tool_context,
        link_spec_to_product=link_spec_to_product,
        refresh_project_context=refresh_project_context,
        load_project=load_project,
        setup_blocker=setup_blocker,
        run_vision_agent=run_vision_agent,
        now_iso=lambda: "2026-04-05T00:00:00Z",
        save_session_state=save_session_state,
    )

    assert result["passed"] is False
    assert result["error"] == "invalid spec path"
    assert result["fsm_state"] == "SETUP_REQUIRED"
    assert result["vision_auto_run"]["attempted"] is False
    assert result["failure_artifact_id"] == "setup-artifact-1"
    assert saved["session_id"] == "1"
    assert saved["state"]["setup_status"] == "failed"
    assert saved["state"]["setup_failure_artifact_id"] == "setup-artifact-1"
    assert saved["state"]["setup_spec_file_path"] == "invalid/path.md"


@pytest.mark.asyncio
async def test_run_project_setup_runs_auto_vision_after_successful_setup() -> None:
    """Verify run project setup runs auto vision after successful setup."""
    from services.setup_service import run_project_setup  # noqa: PLC0415

    context = SimpleNamespace(
        state={
            "fsm_state": "SETUP_REQUIRED",
            "pending_spec_content": "SPEC",
            "compiled_authority_cached": (
                '{"schema_version":"agileforge.compiled_authority.v2"}'
            ),
        },
        session_id="7",
    )
    saved: JsonDict = {}
    calls: JsonDict = {}

    async def hydrate_context(session_id: str, project_id: int) -> SimpleNamespace:
        calls["hydrate"] = (session_id, project_id)
        return context

    def build_tool_context(ctx: object) -> object:
        return ctx

    def link_spec_to_product(
        params: JsonDict,
        tool_context: object = None,
    ) -> JsonDict:
        calls["link"] = {
            "params": params,
            "tool_context": tool_context,
        }
        return {
            "success": True,
            "compile_success": True,
            "spec_path": params["spec_path"],
        }

    def refresh_project_context(project_id: int, tool_context: object) -> JsonDict:
        calls["refresh"] = (project_id, tool_context)
        return {"success": True}

    def load_project(project_id: int) -> object:
        return SimpleNamespace(
            product_id=project_id,
            spec_file_path=__file__,
            compiled_authority_json=(
                '{"schema_version":"agileforge.compiled_authority.v2"}'
            ),
        )

    def setup_blocker(_product: object) -> str | None:
        return None

    async def run_vision_agent(
        state: JsonDict, *, project_id: int, user_input: str
    ) -> JsonDict:
        calls["vision"] = {
            "project_id": project_id,
            "user_input": user_input,
            "state": dict(state),
        }
        return {
            "success": True,
            "input_context": {
                "user_raw_text": user_input or "",
                "specification_content": state.get("pending_spec_content"),
                "compiled_authority": state.get("compiled_authority_cached"),
            },
            "output_artifact": {
                "updated_components": {"project_name": "Vision Project"},
                "product_vision_statement": "Draft vision",
                "is_complete": False,
                "clarifying_questions": ["Need more detail"],
            },
            "is_complete": False,
            "error": None,
            "failure_artifact_id": None,
            "failure_stage": None,
            "failure_summary": None,
            "raw_output_preview": None,
            "has_full_artifact": False,
        }

    def save_session_state(session_id: str, state: JsonDict) -> None:
        saved["session_id"] = session_id
        saved["state"] = dict(state)

    result = await run_project_setup(
        session_id="7",
        project_id=7,
        spec_file_path=__file__,
        hydrate_context=hydrate_context,
        build_tool_context=build_tool_context,
        link_spec_to_product=link_spec_to_product,
        refresh_project_context=refresh_project_context,
        load_project=load_project,
        setup_blocker=setup_blocker,
        run_vision_agent=run_vision_agent,
        now_iso=lambda: "2026-04-05T00:00:00Z",
        save_session_state=save_session_state,
    )

    assert result["passed"] is True
    assert result["error"] is None
    assert result["fsm_state"] == "VISION_INTERVIEW"
    assert result["vision_auto_run"]["attempted"] is True
    assert result["vision_auto_run"]["success"] is True
    assert saved["session_id"] == "7"
    assert saved["state"]["setup_status"] == "passed"
    assert saved["state"]["fsm_state"] == "VISION_INTERVIEW"
    assert saved["state"]["vision_attempts"][0]["trigger"] == "auto_setup_transition"
    assert saved["state"]["vision_components"] == {"project_name": "Vision Project"}


@pytest.mark.asyncio
async def test_run_project_setup_blocks_legacy_authority() -> None:
    """Unsupported authority artifacts should block setup before Vision starts."""
    from services.setup_service import run_project_setup  # noqa: PLC0415

    context = SimpleNamespace(
        state={
            "fsm_state": "SETUP_REQUIRED",
            "latest_spec_version_id": 42,
            "compiled_authority_cached": '{"invariants":[]}',
        },
        session_id="7",
    )
    saved: JsonDict = {}

    async def hydrate_context(session_id: str, project_id: int) -> SimpleNamespace:
        del session_id, project_id
        return context

    def build_tool_context(ctx: object) -> object:
        return ctx

    def link_spec_to_product(
        params: JsonDict,
        tool_context: object = None,
    ) -> JsonDict:
        del params, tool_context
        return {"success": True, "compile_success": True}

    def refresh_project_context(project_id: int, tool_context: object) -> JsonDict:
        del project_id, tool_context
        return {"success": True}

    def load_project(project_id: int) -> object:
        return SimpleNamespace(
            product_id=project_id,
            latest_spec_version_id=42,
            spec_file_path=__file__,
            compiled_artifact_json='{"invariants":[]}',
            compiled_authority_json='{"invariants":[]}',
        )

    def setup_blocker(_product: object) -> str | None:
        return None

    async def run_vision_agent(
        _state: object, *, project_id: int, user_input: str
    ) -> Never:
        del project_id, user_input
        msg = "vision agent should not start with unsupported authority"
        raise AssertionError(msg)

    def save_session_state(session_id: str, state: JsonDict) -> None:
        saved["session_id"] = session_id
        saved["state"] = dict(state)

    result = await run_project_setup(
        session_id="7",
        project_id=7,
        spec_file_path=__file__,
        hydrate_context=hydrate_context,
        build_tool_context=build_tool_context,
        link_spec_to_product=link_spec_to_product,
        refresh_project_context=refresh_project_context,
        load_project=load_project,
        setup_blocker=setup_blocker,
        run_vision_agent=run_vision_agent,
        now_iso=lambda: "2026-04-05T00:00:00Z",
        save_session_state=save_session_state,
    )

    next_action = {
        "command": "agileforge authority regenerate",
        "args": {
            "project_id": 7,
            "spec_version_id": 42,
            "idempotency_key": "<new-key>",
        },
        "reason": "regenerate unsupported compiled authority before continuing.",
    }
    assert result["passed"] is False
    assert result["error"] == "Compiled authority artifact schema is unsupported."
    assert result["next_actions"] == [next_action]
    assert saved["state"]["setup_status"] == "failed"
    assert saved["state"]["setup_next_actions"] == [next_action]
