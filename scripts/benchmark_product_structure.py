"""Script for benchmark product structure."""

import sys
import time
from pathlib import Path

from sqlalchemy import Engine, event
from sqlmodel import Session, SQLModel, create_engine

from utils.cli_output import emit

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agile_sqlmodel import Project, StoryStatus, UserStory
from models.core import Epic, Feature, Theme
from tools.db_tools import query_product_structure

# Setup in-memory DB for benchmarking
engine = create_engine("sqlite:///:memory:")
SQLModel.metadata.create_all(engine)

# Patch the engine in db_tools
import tools.db_tools  # noqa: E402


def _benchmark_engine() -> Engine:
    return engine


tools.db_tools.__dict__["get_engine"] = _benchmark_engine


def _require_id(value: int | None, name: str) -> int:
    if value is None:
        msg = f"{name} was not generated"
        raise RuntimeError(msg)
    return value


def seed_database(
    product_count: int = 1,
    themes_per_product: int = 5,
    epics_per_theme: int = 5,
    features_per_epic: int = 5,
    stories_per_feature: int = 5,
) -> None:
    """Return seed database."""
    with Session(engine) as session:
        for p in range(product_count):
            product = Project(
                name=f"Project {p}", vision="Vision", description="Description"
            )
            session.add(product)
            session.commit()
            session.refresh(product)
            project_id = _require_id(product.project_id, "Project ID")

            for t in range(themes_per_product):
                theme = Theme(
                    title=f"Theme {t}",
                    description="Desc",
                    project_id=project_id,
                )
                session.add(theme)
                session.commit()
                session.refresh(theme)
                theme_id = _require_id(theme.theme_id, "Theme ID")

                for e in range(epics_per_theme):
                    epic = Epic(title=f"Epic {e}", summary="Sum", theme_id=theme_id)
                    session.add(epic)
                    session.commit()
                    session.refresh(epic)
                    epic_id = _require_id(epic.epic_id, "Epic ID")

                    for f in range(features_per_epic):
                        feature = Feature(
                            title=f"Feature {f}",
                            description="Desc",
                            epic_id=epic_id,
                        )
                        session.add(feature)
                        session.commit()
                        session.refresh(feature)
                        feature_id = _require_id(feature.feature_id, "Feature ID")

                        for s in range(stories_per_feature):
                            story = UserStory(
                                title=f"Story {s}",
                                story_description="Desc",
                                status=StoryStatus.TO_DO,
                                project_id=project_id,
                                feature_id=feature_id,
                            )
                            session.add(story)
        session.commit()
    emit(
        f"Seeded DB with {product_count} projects, {themes_per_product} themes/prod, {epics_per_theme} epics/theme, {features_per_epic} features/epic, {stories_per_feature} stories/feature."  # noqa: E501
    )


def benchmark() -> None:
    # Reset query count
    """Return benchmark."""
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

    # Measure
    start_time = time.time()
    result = query_product_structure(1)
    end_time = time.time()

    duration = end_time - start_time

    emit(f"Execution Time: {duration:.4f} seconds")
    emit(f"Query Count: {query_count}")

    if not result["success"]:
        emit("Error in query_product_structure")
        sys.exit(1)


if __name__ == "__main__":
    emit("Seeding database...")
    seed_database()
    emit("Running benchmark...")
    benchmark()
