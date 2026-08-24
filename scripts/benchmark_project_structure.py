"""Benchmark Project hierarchy queries against an in-memory database."""

import time

from sqlalchemy import Engine, event
from sqlmodel import Session, SQLModel, create_engine

import tools.db_tools
from agile_sqlmodel import Project
from models.core import Epic, Feature, Theme
from tools.db_tools import query_project_structure
from utils.cli_output import emit

# Setup in-memory DB for benchmarking
engine = create_engine("sqlite:///:memory:")
SQLModel.metadata.create_all(engine)


# Patch the engine in db_tools
def _benchmark_engine() -> Engine:
    return engine


tools.db_tools.__dict__["get_engine"] = _benchmark_engine


def _require_id(value: int | None, name: str) -> int:
    if value is None:
        msg = f"{name} was not generated"
        raise RuntimeError(msg)
    return value


def seed_database(
    project_count: int = 1,
    themes_per_project: int = 5,
    epics_per_theme: int = 5,
    features_per_epic: int = 5,
) -> None:
    """Return seed database."""
    with Session(engine) as session:
        for p in range(project_count):
            project = Project(
                name=f"Project {p}", vision="Vision", description="Description"
            )
            session.add(project)
            session.commit()
            session.refresh(project)
            project_id = _require_id(project.project_id, "Project ID")

            for t in range(themes_per_project):
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
        session.commit()
    emit(
        f"Seeded DB with {project_count} projects, "
        f"{themes_per_project} themes/project, {epics_per_theme} epics/theme, "
        f"{features_per_epic} features/epic."
    )


def benchmark() -> None:
    # Reset query count
    """Return benchmark."""
    query_count = 0

    @event.listens_for(Engine, "before_cursor_execute")
    def before_cursor_execute(*_event_args: object) -> None:
        nonlocal query_count
        query_count += 1

    # Measure
    start_time = time.time()
    result = query_project_structure(1)
    end_time = time.time()

    duration = end_time - start_time

    emit(f"Execution Time: {duration:.4f} seconds")
    emit(f"Query Count: {query_count}")

    if not result["success"]:
        emit("Error in query_project_structure")
        raise SystemExit(1)


if __name__ == "__main__":
    emit("Seeding database...")
    seed_database()
    emit("Running benchmark...")
    benchmark()
