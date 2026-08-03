"""Unit tests for the product_vision_tool module."""

from unittest.mock import MagicMock, patch

from google.adk.tools import ToolContext
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from agile_sqlmodel import Product
from orchestrator_agent.agent_tools.product_vision_tool.tools import (
    save_vision_tool,
)
from services.contracts.vision import SaveVisionInput


def test_save_vision_tool_creates_project_and_returns_dict(engine: Engine) -> None:
    """Test that save_vision_tool creates a project and returns the correct dictionary structure."""  # noqa: E501
    # Mock ToolContext
    tool_context = MagicMock(spec=ToolContext)
    tool_context.state = {}

    # Input data
    vision_input = SaveVisionInput(
        project_name="New Test Project",
        product_vision_statement="A vision for the future.",
    )

    # Patch get_engine to use our test fixture engine
    with patch(
        "orchestrator_agent.agent_tools.product_vision_tool.tools.get_engine",
        return_value=engine,
    ):
        result = save_vision_tool(vision_input, tool_context)

    # Verify result structure
    assert isinstance(result, dict)
    assert result["success"] is True
    assert result["project_name"] == "New Test Project"
    assert "product_id" in result
    assert isinstance(result["product_id"], int)
    assert "SUCCESS: Saved project" in result["message"]

    # Verify DB state
    with Session(engine) as session:
        product = session.exec(
            select(Product).where(Product.name == "New Test Project")
        ).first()
        assert product is not None
        assert product.product_id == result["product_id"]
        assert product.vision == "A vision for the future."

    # Verify context update
    assert tool_context.state["current_project_name"] == "New Test Project"
    assert tool_context.state["active_project"]["product_id"] == result["product_id"]


def test_save_vision_tool_updates_existing_project(engine: Engine) -> None:
    """Test that save_vision_tool updates an existing project and returns the ID."""
    # Pre-populate DB
    with Session(engine) as session:
        existing_project = Product(name="Existing Project", vision="Old vision")
        session.add(existing_project)
        session.commit()
        original_id = existing_project.product_id

    # Mock ToolContext
    tool_context = MagicMock(spec=ToolContext)
    tool_context.state = {}

    # Input data
    vision_input = SaveVisionInput(
        product_id=original_id,
        project_name="Existing Project",
        product_vision_statement="New Updated Vision",
    )

    # Patch get_engine
    with patch(
        "orchestrator_agent.agent_tools.product_vision_tool.tools.get_engine",
        return_value=engine,
    ):
        result = save_vision_tool(vision_input, tool_context)

    # Verify result
    assert result["success"] is True
    assert result["product_id"] == original_id
    assert "SUCCESS: Saved project" in result["message"]

    # Verify DB update
    with Session(engine) as session:
        product = session.exec(
            select(Product).where(Product.name == "Existing Project")
        ).first()
        assert product is not None
        assert product.vision == "New Updated Vision"


def test_save_vision_tool_handles_missing_active_project(engine: Engine) -> None:
    """Test that save_vision_tool handles missing active_project state safely."""
    with Session(engine) as session:
        existing_project = Product(name="Hashbrown", vision="Old")
        session.add(existing_project)
        session.commit()
        product_id = existing_project.product_id

    tool_context = MagicMock(spec=ToolContext)
    tool_context.state = {"active_project": None}

    vision_input = SaveVisionInput(
        product_id=product_id,
        project_name="Hashbrown",
        product_vision_statement="New Vision",
    )

    with patch(
        "orchestrator_agent.agent_tools.product_vision_tool.tools.get_engine",
        return_value=engine,
    ):
        result = save_vision_tool(vision_input, tool_context)

    assert result["success"] is True
    active_project = tool_context.state["active_project"]
    assert isinstance(active_project, dict)
    assert active_project["name"] == "Hashbrown"
    assert active_project["structure"] == {
        "themes": 0,
        "epics": 0,
        "features": 0,
        "user_stories": 0,
    }


def test_save_vision_tool_updates_existing_project_by_name_when_id_is_none(
    engine: Engine,
) -> None:
    """Test that save_vision_tool falls back to update if name exists but ID is None."""
    # 1. Pre-seed the DB with a project
    with Session(engine) as session:
        existing = Product(name="Duplicate Name Project", vision="Old Vision")
        session.add(existing)
        session.commit()
        session.refresh(existing)
        existing_id = existing.product_id

    # Mock ToolContext
    tool_context = MagicMock(spec=ToolContext)
    tool_context.state = {}

    # Input: Same name, None ID, New Vision
    vision_input = SaveVisionInput(
        project_name="Duplicate Name Project",
        product_id=None,
        product_vision_statement="New Updated Vision",
    )

    with patch(
        "orchestrator_agent.agent_tools.product_vision_tool.tools.get_engine",
        return_value=engine,
    ):
        result = save_vision_tool(vision_input, tool_context)

    # Verify Result
    assert result["success"] is True
    assert result["product_id"] == existing_id
    # message might not contain "Updating" if I didn't verify that part, but let's check basic success.  # noqa: E501

    # Verify DB Update
    with Session(engine) as session:
        updated = session.get(Product, existing_id)
        assert updated is not None
        assert updated.vision == "New Updated Vision"
        # Ensure no duplicate was created
        all_prods = session.exec(
            select(Product).where(Product.name == "Duplicate Name Project")
        ).all()
        assert len(all_prods) == 1
