"""
TDD: save_vision_tool must link the spec file and trigger authority.

compilation when pending_spec_path is in state, so that VISION_PERSISTENCE
does not need to do any spec work.

Run with: pytest tests/unit/test_save_vision_links_spec.py -v
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from google.adk.tools import ToolContext
from sqlalchemy.engine import Engine
from sqlmodel import Session

from agile_sqlmodel import Product
from orchestrator_agent.agent_tools.product_vision_tool.tools import (
    save_vision_tool,
)
from services.contracts.vision import SaveVisionInput

# ============================================================================
# Helpers
# ============================================================================


def _make_context(state: dict) -> MagicMock:
    ctx = MagicMock(spec=ToolContext)
    ctx.state = state
    return ctx


def _stub_compile_success(params: object, tool_context: object = None) -> object:
    """Stub returning a successful compilation."""
    del params, tool_context
    return {
        "success": True,
        "spec_version_id": 42,
        "authority_id": 99,
    }


def _stub_compile_failure(params: object, tool_context: object = None) -> object:
    """Stub returning a failed compilation."""
    del params, tool_context
    return {"success": False, "error": "LLM unavailable"}


# ============================================================================
# Tests
# ============================================================================


class TestSaveVisionLinksSpec:
    """save_vision_tool must link spec + compile authority when.

    pending_spec_path is present in state.
    """

    def test_links_spec_when_pending_spec_path_in_state(self, engine: Engine) -> None:
        """
        GIVEN: pending_spec_path is in tool_context.state.

        WHEN: save_vision_tool creates a new product
        THEN:
            - product.spec_file_path is set
            - product.spec_loaded_at is populated
            - state["spec_persisted"] is True.
        """
        spec_path = "test_specs/test_quadra.md"
        p = Path(spec_path)
        p.parent.mkdir(exist_ok=True)
        if not p.exists():
            p.write_text("# Test Spec\n## Features\n- F1", encoding="utf-8")

        ctx = _make_context(
            {
                "pending_spec_path": spec_path,
                "pending_spec_content": "# Test Spec",
            }
        )

        vision_input = SaveVisionInput(
            project_name="Spec Link Project",
            product_vision_statement="A great vision.",
        )

        with (
            patch(
                "orchestrator_agent.agent_tools.product_vision_tool.tools.get_engine",
                return_value=engine,
            ),
            patch(
                "orchestrator_agent.agent_tools.product_vision_tool.tools.update_spec_and_compile_authority",
                side_effect=_stub_compile_success,
            ) as compile_mock,
        ):
            result = save_vision_tool(vision_input, ctx)

        assert result["success"] is True
        product_id = result["product_id"]

        # DB assertions
        with Session(engine) as session:
            product = session.get(Product, product_id)
            assert product is not None
            assert product.spec_file_path == spec_path
            assert product.spec_loaded_at is not None

        # State assertions
        assert ctx.state.get("spec_persisted") is True

        # Compilation was triggered
        compile_mock.assert_called_once()
        call_args = compile_mock.call_args
        assert call_args[0][0].product_id == product_id
        assert call_args[0][0].content_ref == spec_path

    def test_no_spec_link_when_no_pending_path(self, engine: Engine) -> None:
        """
        GIVEN: no pending_spec_path in state.

        WHEN: save_vision_tool creates a product
        THEN:
            - product.spec_file_path is NOT set
            - No compilation is triggered
            - spec_persisted is NOT set.
        """
        ctx = _make_context({})

        vision_input = SaveVisionInput(
            project_name="No Spec Project",
            product_vision_statement="Vision without spec.",
        )

        with (
            patch(
                "orchestrator_agent.agent_tools.product_vision_tool.tools.get_engine",
                return_value=engine,
            ),
            patch(
                "orchestrator_agent.agent_tools.product_vision_tool.tools.update_spec_and_compile_authority",
                side_effect=_stub_compile_success,
            ) as compile_mock,
        ):
            result = save_vision_tool(vision_input, ctx)

        assert result["success"] is True

        with Session(engine) as session:
            product = session.get(Product, result["product_id"])
            assert product is not None
            assert product.spec_file_path is None
            assert product.spec_loaded_at is None

        assert "spec_persisted" not in ctx.state
        compile_mock.assert_not_called()

    def test_compile_failure_still_links_spec(self, engine: Engine) -> None:
        """
        GIVEN: pending_spec_path is in state, but authority compile fails.

        WHEN: save_vision_tool creates a product
        THEN:
            - product.spec_file_path IS set (link succeeded)
            - spec_persisted is True (file link done)
            - Result still reports overall success (vision saved)
            - Result includes compile_error info.
        """
        spec_path = "test_specs/test_quadra.md"
        p = Path(spec_path)
        p.parent.mkdir(exist_ok=True)
        if not p.exists():
            p.write_text("# Test Spec\n", encoding="utf-8")

        ctx = _make_context(
            {
                "pending_spec_path": spec_path,
            }
        )

        vision_input = SaveVisionInput(
            project_name="Compile Fail Project",
            product_vision_statement="Vision here.",
        )

        with (
            patch(
                "orchestrator_agent.agent_tools.product_vision_tool.tools.get_engine",
                return_value=engine,
            ),
            patch(
                "orchestrator_agent.agent_tools.product_vision_tool.tools.update_spec_and_compile_authority",
                side_effect=_stub_compile_failure,
            ),
        ):
            result = save_vision_tool(vision_input, ctx)

        # Vision save itself succeeded
        assert result["success"] is True

        # Spec file was linked even though compile failed
        with Session(engine) as session:
            product = session.get(Product, result["product_id"])
            assert product is not None
            assert product.spec_file_path == spec_path

        assert ctx.state.get("spec_persisted") is True

    def test_update_mode_also_links_spec(self, engine: Engine) -> None:
        """
        GIVEN: An existing product (update mode), pending_spec_path in state.

        WHEN: save_vision_tool updates the product
        THEN: spec is linked and authority compiled.
        """
        # Pre-create product
        with Session(engine) as session:
            prod = Product(name="Existing For Spec", vision="Old vision")
            session.add(prod)
            session.commit()
            p_id = prod.product_id

        spec_path = "test_specs/test_quadra.md"
        p = Path(spec_path)
        p.parent.mkdir(exist_ok=True)
        if not p.exists():
            p.write_text("# Test\n", encoding="utf-8")

        ctx = _make_context(
            {
                "pending_spec_path": spec_path,
            }
        )

        vision_input = SaveVisionInput(
            product_id=p_id,
            project_name="Existing For Spec",
            product_vision_statement="Updated vision.",
        )

        with (
            patch(
                "orchestrator_agent.agent_tools.product_vision_tool.tools.get_engine",
                return_value=engine,
            ),
            patch(
                "orchestrator_agent.agent_tools.product_vision_tool.tools.update_spec_and_compile_authority",
                side_effect=_stub_compile_success,
            ) as compile_mock,
        ):
            result = save_vision_tool(vision_input, ctx)

        assert result["success"] is True

        with Session(engine) as session:
            product = session.get(Product, p_id)
            assert product is not None
            assert product.spec_file_path == spec_path
            assert product.spec_loaded_at is not None
            assert product.vision == "Updated vision."

        compile_mock.assert_called_once()
        assert ctx.state.get("spec_persisted") is True
