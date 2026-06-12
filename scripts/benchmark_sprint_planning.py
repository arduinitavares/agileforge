"""Script for benchmark sprint planning."""

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from sqlalchemy import Engine, event
from sqlmodel import Session, SQLModel, create_engine

from utils.cli_output import emit

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agile_sqlmodel import Product, StoryStatus, UserStory
from models.core import Epic, Feature, ProductTeam, Team, Theme
from orchestrator_agent.agent_tools.sprint_planner_tool.tools import (
    SaveSprintPlanInput,
    save_sprint_plan_tool,
)

# Boundary contract: from models.core import ProductTeam

if TYPE_CHECKING:
    from google.adk.tools import ToolContext

# Setup in-memory DB for benchmarking
engine = create_engine("sqlite:///:memory:")
SQLModel.metadata.create_all(engine)

# Patch the engine in the tools module
import orchestrator_agent.agent_tools.sprint_planner_tool.tools as sprint_tools  # noqa: E402


def _benchmark_engine() -> Engine:
    return engine


sprint_tools.__dict__["get_engine"] = _benchmark_engine


def _require_id(value: int | None, name: str) -> int:
    if value is None:
        msg = f"{name} was not generated"
        raise RuntimeError(msg)
    return value


def seed_database() -> tuple[int, int, list[int]]:
    """Return seed database."""
    with Session(engine) as session:
        # Create Products
        p1 = Product(name="Product A", description="Main product")
        session.add(p1)
        p2 = Product(name="Product B", description="Other product")
        session.add(p2)
        session.commit()
        session.refresh(p1)
        session.refresh(p2)
        p1_id = _require_id(p1.product_id, "Product A ID")
        p2_id = _require_id(p2.product_id, "Product B ID")

        # Create Team
        team = Team(name="Team Alpha")
        session.add(team)
        session.commit()
        session.refresh(team)
        team_id = _require_id(team.team_id, "Team ID")

        # Link Team to Product A
        link = ProductTeam(product_id=p1_id, team_id=team_id)
        session.add(link)
        session.commit()

        # Create Feature for Product A (to test joinedload)
        f1 = Feature(  # noqa: F841
            title="Feature 1", description="Desc", epic_id=1
        )  # epic_id doesn't exist but FKs might fail if enforced.
        # Actually agile_sqlmodel enforces FKs via PRAGMA, so I should probably be careful or just create structure properly.  # noqa: E501
        # But wait, Feature requires Epic. Epic requires Theme.
        # Let's create full hierarchy to be safe and test join performance.
        theme = Theme(title="Theme 1", product_id=p1_id)
        session.add(theme)
        session.commit()
        session.refresh(theme)
        theme_id = _require_id(theme.theme_id, "Theme ID")

        epic = Epic(title="Epic 1", theme_id=theme_id)
        session.add(epic)
        session.commit()
        session.refresh(epic)
        epic_id = _require_id(epic.epic_id, "Epic ID")

        feature = Feature(title="Feature 1", epic_id=epic_id)
        session.add(feature)
        session.commit()
        session.refresh(feature)
        feature_id = _require_id(feature.feature_id, "Feature ID")

        valid_story_ids: list[int] = []

        # 30 Valid Stories (Product A, TO_DO)
        for i in range(30):
            s = UserStory(
                title=f"Valid Story {i}",
                status=StoryStatus.TO_DO,
                product_id=p1_id,
                feature_id=feature_id,  # Link to feature to trigger join
                story_points=3,
            )
            session.add(s)
            session.commit()
            session.refresh(s)
            valid_story_ids.append(_require_id(s.story_id, f"Valid story {i} ID"))

        # 10 Wrong Product Stories (Product B, TO_DO)
        for i in range(10):
            s = UserStory(
                title=f"Wrong Product Story {i}",
                status=StoryStatus.TO_DO,
                product_id=p2_id,
                story_points=5,
            )
            session.add(s)
            session.commit()
            session.refresh(s)

        # 10 Wrong Status Stories (Product A, IN_PROGRESS)
        for i in range(10):
            s = UserStory(
                title=f"Wrong Status Story {i}",
                status=StoryStatus.IN_PROGRESS,
                product_id=p1_id,
                story_points=2,
            )
            session.add(s)
            session.commit()
            session.refresh(s)

        return p1_id, team_id, valid_story_ids


def benchmark() -> None:
    """Return benchmark."""
    product_id, team_id, valid_story_ids = seed_database()

    emit(
        f"Benchmarking sprint plan save with {len(valid_story_ids)} selected stories."
    )

    # Reset query count
    query_count = 0

    @event.listens_for(Engine, "before_cursor_execute")
    def before_cursor_execute(  # noqa: PLR0913
        conn: object,
        cursor: object,
        statement: object,
        parameters: object,
        context: object,
        executemany: object,
    ) -> None:
        del conn, cursor, statement, parameters, context, executemany
        nonlocal query_count
        query_count += 1
        # Optional: Print statement to see what's happening (noisy)
        # print(f"QUERY: {statement}")  # noqa: ERA001

    sprint_plan = {
        "sprint_goal": "Benchmark Sprint",
        "sprint_number": 1,
        "selected_stories": [
            {
                "story_id": story_id,
                "story_title": f"Valid Story {index}",
                "tasks": [],
                "reason_for_selection": "Benchmark-selected backlog item",
            }
            for index, story_id in enumerate(valid_story_ids)
        ],
        "deselected_stories": [],
        "capacity_analysis": {
            "capacity_points": len(valid_story_ids) * 3,
            "capacity_source": "user_override",
            "capacity_basis": "Synthetic benchmark capacity.",
            "selected_count": len(valid_story_ids),
            "story_points_used": len(valid_story_ids) * 3,
            "remaining_capacity_points": 0,
            "commitment_note": "Benchmark commitment check.",
            "reasoning": "Synthetic benchmark plan for save-path query counting.",
        },
    }
    tool_context = cast(
        "ToolContext",
        SimpleNamespace(
            state={
                "sprint_plan": sprint_plan,
                "sprint_input": {"include_task_decomposition": False},
            },
            session_id="benchmark-sprint-planning",
        ),
    )

    plan_input = SaveSprintPlanInput(
        product_id=product_id,
        team_id=team_id,
    )

    # Measure
    start_time = time.time()
    result = save_sprint_plan_tool(plan_input, tool_context)
    end_time = time.time()

    duration = end_time - start_time

    emit(f"\nExecution Time: {duration:.4f} seconds")
    emit(f"Query Count: {query_count}")

    if not result["success"]:
        emit("Error in save_sprint_plan_tool")
        emit(result.get("error"))
        sys.exit(1)

    emit(f"Selected Stories Saved: {result['selected_story_count']}")

    # Validation
    # We expect 30 valid stories to be persisted.
    if result["selected_story_count"] != 30:  # noqa: PLR2004
        msg = f"Expected 30 selected stories, got {result['selected_story_count']}."
        raise AssertionError(msg)
    emit("Validation counts match expectations.")


if __name__ == "__main__":
    benchmark()
