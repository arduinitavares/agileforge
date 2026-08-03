"""Script for verify query features performance."""

import os
import sys
import time

from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine

from utils.cli_output import emit

# Ensure we can import from root
sys.path.append(os.getcwd())  # noqa: PTH109

from agile_sqlmodel import Project, UserStory
from models.core import Epic, Feature, Theme
from tools.story_query_tools import QueryFeaturesInput, query_features_for_stories

# Setup In-Memory DB for Benchmark
TEST_DB_URL = "sqlite:///:memory:"
test_engine = create_engine(TEST_DB_URL)

# Patch the engine used by the tool
import agile_sqlmodel  # noqa: E402
import tools.story_query_tools as target_module  # noqa: E402

agile_sqlmodel.engine = test_engine


def _test_engine() -> Engine:
    return test_engine


target_module.__dict__["get_engine"] = _test_engine


def _require_id(value: int | None, name: str) -> int:
    if value is None:
        msg = f"{name} was not generated"
        raise RuntimeError(msg)
    return value


def setup_data(
    num_themes: int = 5,
    epics_per_theme: int = 5,
    features_per_epic: int = 10,
    stories_per_feature: int = 5,
) -> int:
    """Return setup data."""
    emit(
        f"Setting up data: {num_themes} Themes, {epics_per_theme} Epics/Theme, {features_per_epic} Features/Epic..."  # noqa: E501
    )
    SQLModel.metadata.create_all(test_engine)

    with Session(test_engine) as session:
        product = Project(name="Benchmark Project")
        session.add(product)
        session.commit()
        session.refresh(product)
        project_id = _require_id(product.project_id, "Project ID")

        for t in range(num_themes):
            theme = Theme(title=f"Theme {t}", project_id=project_id)
            session.add(theme)
            session.commit()
            session.refresh(theme)
            theme_id = _require_id(theme.theme_id, "Theme ID")

            for e in range(epics_per_theme):
                epic = Epic(title=f"Epic {t}-{e}", theme_id=theme_id)
                session.add(epic)
                session.commit()
                session.refresh(epic)
                epic_id = _require_id(epic.epic_id, "Epic ID")

                features: list[Feature] = []
                for f in range(features_per_epic):
                    feature = Feature(title=f"Feature {t}-{e}-{f}", epic_id=epic_id)
                    features.append(feature)
                session.add_all(features)
                session.commit()

                for feature in features:
                    feature_id = _require_id(feature.feature_id, "Feature ID")
                    stories: list[UserStory] = []
                    for s in range(stories_per_feature):
                        story = UserStory(
                            title=f"Story {feature_id}-{s}",
                            project_id=project_id,
                            feature_id=feature_id,
                        )
                        stories.append(story)
                    session.add_all(stories)
                session.commit()

        return project_id


def run_benchmark() -> None:
    """Return run benchmark."""
    project_id = setup_data(
        num_themes=5, epics_per_theme=5, features_per_epic=20, stories_per_feature=2
    )
    # Total features: 5 * 5 * 20 = 500 features.

    emit("Starting Benchmark...")
    start_time = time.time()

    result = query_features_for_stories(QueryFeaturesInput(project_id=project_id))

    end_time = time.time()
    duration = end_time - start_time

    emit(f"\nBenchmark Completed in {duration:.4f} seconds")
    emit(f"Total Features Found: {result.get('total_features')}")

    # Validation of structure
    structure = result.get("structure", [])
    if structure:
        first_theme = structure[0]
        emit(f"Theme keys: {list(first_theme.keys())}")
        if first_theme.get("epics"):
            first_epic = first_theme["epics"][0]
            emit(f"Epic keys: {list(first_epic.keys())}")
            if first_epic.get("features"):
                first_feature = first_epic["features"][0]
                emit(f"Feature keys: {list(first_feature.keys())}")
                emit(
                    f"Sibling features count: {len(first_feature.get('sibling_features', []))}"  # noqa: E501
                )


if __name__ == "__main__":
    run_benchmark()
