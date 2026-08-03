"""Script for benchmark roadmap."""

import sys
import time
from pathlib import Path
from sqlite3 import Connection
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from utils.cli_output import emit

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agile_sqlmodel import Project
from tools import db_tools

# Setup in-memory DB
DB_URL = "sqlite:///:memory:"
engine = create_engine(DB_URL, echo=False)


@event.listens_for(Engine, "connect")
def set_sqlite_pragma(
    dbapi_connection: Connection, _connection_record: object
) -> None:
    """Return set sqlite pragma."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SQLModel.metadata.create_all(engine)

# Patch the engine in db_tools
def _benchmark_engine() -> Engine:
    return engine


db_tools.__dict__["get_engine"] = _benchmark_engine


def _require_id(value: int | None, name: str) -> int:
    if value is None:
        msg = f"{name} was not generated"
        raise RuntimeError(msg)
    return value


def generate_roadmap_data(
    num_themes: int = 5, epics_per_theme: int = 5, features_per_epic: int = 5
) -> list[dict[str, Any]]:
    """Return generate roadmap data."""
    roadmap: list[dict[str, Any]] = []
    for t in range(num_themes):
        theme = {
            "quarter": "Q1",
            "theme_title": f"Theme {t}",
            "theme_description": f"Description for Theme {t}",
            "epics": [],
        }
        for e in range(epics_per_theme):
            epic = {
                "epic_title": f"Epic {t}-{e}",
                "epic_summary": f"Summary for Epic {t}-{e}",
                "features": [],
            }
            for f in range(features_per_epic):
                feature = {
                    "title": f"Feature {t}-{e}-{f}",
                    "description": f"Desc for Feature {t}-{e}-{f}",
                }
                epic["features"].append(feature)
            theme["epics"].append(epic)
        roadmap.append(theme)
    return roadmap


def run_benchmark() -> float:
    # create product
    """Return run benchmark."""
    with Session(engine) as session:
        product = Project(name="Benchmark Project", vision="Speed")
        session.add(product)
        session.commit()
        session.refresh(product)
        project_id = _require_id(product.project_id, "Project ID")

    # generate data
    # Increase load to make the difference obvious
    # 10 * 10 * 10 = 1000 features + 100 epics + 10 themes = 1110 objects
    data = generate_roadmap_data(
        num_themes=10, epics_per_theme=10, features_per_epic=10
    )

    emit(
        f"Benchmarking persist_roadmap with {len(data)} themes, {len(data) * 10} epics, {len(data) * 10 * 10} features..."  # noqa: E501
    )

    start_time = time.time()
    result = db_tools.persist_roadmap(project_id, data)
    end_time = time.time()

    duration = end_time - start_time

    if result["success"]:
        emit(f"Success! Duration: {duration:.4f} seconds")
        emit(result["message"])
    else:
        emit(f"Failed: {result}")

    return duration


if __name__ == "__main__":
    run_benchmark()
