# tools/story_query_tools.py
"""
Tools for querying features and story-related data.

Extracted from legacy product_user_story_tool/tools.py to preserve
query_features_for_stories functionality after legacy agent removal.
"""

import logging
import re
from dataclasses import dataclass
from typing import Annotated, Any, cast

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from models.core import Epic, Feature, Project, Theme, UserStory
from models.db import get_engine

logger: logging.Logger = logging.getLogger(name=__name__)

_EMPTY_TEXT_ERROR = "Field cannot be empty or whitespace-only"

# --- Schemas for querying features ---


class FeatureForStory(BaseModel):
    """
    Schema for a single feature ready for story generation.

    CRITICAL: theme and epic are REQUIRED fields. They must NEVER be None or "Unknown".
    This enforces the contract that all features must have proper roadmap metadata.

    IMMUTABILITY: This model is frozen to prevent accidental mutation during
    pipeline processing.
    All fields are set at construction time and cannot be changed.
    """

    model_config = {"frozen": True}  # Immutable after construction

    feature_id: Annotated[int, Field(description="Feature ID")]
    feature_title: Annotated[str, Field(description="Feature title")]
    # --- Stable ID-based references (preferred for validation) ---
    theme_id: Annotated[
        int,
        Field(
            description=(
                "Theme database ID (stable reference that eliminates duplicate "
                "name ambiguity)"
            ),
        ),
    ]
    epic_id: Annotated[
        int,
        Field(
            description=(
                "Epic database ID (stable reference that eliminates duplicate "
                "name ambiguity)"
            ),
        ),
    ]
    # --- Title-based references (human-readable, validated against IDs) ---
    theme: Annotated[
        str,
        Field(
            description="Theme name (REQUIRED - must never be None or 'Unknown')",
            min_length=1,  # Enforce non-empty after stripping
        ),
    ]
    epic: Annotated[
        str,
        Field(
            description="Epic name (REQUIRED - must never be None or 'Unknown')",
            min_length=1,  # Enforce non-empty after stripping
        ),
    ]
    existing_stories_count: Annotated[
        int | None,
        Field(description="Number of existing stories. Defaults to 0 if not provided."),
    ] = None
    time_frame: Annotated[
        str | None,
        Field(description="Roadmap time frame. Defaults to None if not provided."),
    ] = None
    theme_justification: Annotated[
        str | None,
        Field(
            description=(
                "Strategic theme justification. Defaults to None if not provided."
            )
        ),
    ] = None
    sibling_features: Annotated[
        list[str],
        Field(default_factory=list, description="Other features in same theme"),
    ]

    @field_validator("theme", "epic")
    @classmethod
    def validate_non_empty_after_strip(cls, v: str) -> str:
        """Strip whitespace and validate min_length."""
        if not v or not v.strip():
            raise ValueError(_EMPTY_TEXT_ERROR)
        return v.strip()


class QueryFeaturesOutput(BaseModel):
    """Output schema for query_features_for_stories with validated structure."""

    success: Annotated[bool, Field(description="Whether query succeeded")]
    project_id: Annotated[int, Field(description="Project ID")]
    product_name: Annotated[str, Field(description="Project name")]
    features_flat: Annotated[
        list[FeatureForStory],
        Field(description="Flat list of features with REQUIRED theme/epic metadata"),
    ]
    structure: Annotated[
        list[dict[str, Any]],
        Field(description="Hierarchical theme/epic/feature structure"),
    ]
    total_features: Annotated[int, Field(description="Total feature count")]
    message: Annotated[str, Field(description="Human-readable message")]


class QueryFeaturesInput(BaseModel):
    """Input schema for querying features."""

    project_id: Annotated[int, Field(description="The project ID to query.")]


@dataclass(frozen=True)
class _FeatureEntryContext:
    theme: Theme
    theme_id: int
    epic: Epic
    epic_id: int
    time_frame_value: str | None


@dataclass(frozen=True)
class _EpicPayloadContext:
    feature_entry: _FeatureEntryContext
    theme_all_features: list[str]
    story_counts: dict[int, int]


@dataclass(frozen=True)
class _QueryGraph:
    epics_by_theme: dict[int, list[Epic]]
    features_by_epic: dict[int, list[Feature]]
    features_by_theme: dict[int, list[str]]
    story_counts: dict[int, int]


def _derive_time_frame_from_title(title: str) -> str | None:
    if not title:
        return None
    match = re.search(r"\b(now|next|later)\b", title, re.IGNORECASE)
    if not match:
        return None
    value = match.group(1).lower()
    return value.capitalize()


def _load_themes(session: Session, project_id: int) -> list[Theme]:
    return list(session.exec(select(Theme).where(Theme.project_id == project_id)).all())


def _load_epics(session: Session, theme_ids: list[int]) -> list[Epic]:
    if not theme_ids:
        return []
    return list(
        session.exec(
            select(Epic).where(cast("Any", Epic.theme_id).in_(theme_ids))
        ).all()
    )


def _index_epics(
    theme_ids: list[int], epics: list[Epic]
) -> tuple[dict[int, list[Epic]], dict[int, int], list[int]]:
    epic_ids = [epic.epic_id for epic in epics if epic.epic_id is not None]
    epics_by_theme: dict[int, list[Epic]] = {theme_id: [] for theme_id in theme_ids}
    epic_to_theme: dict[int, int] = {}
    for epic in epics:
        theme_id = epic.theme_id
        epic_id = epic.epic_id
        if theme_id is None or epic_id is None:
            continue
        epics_by_theme[theme_id].append(epic)
        epic_to_theme[epic_id] = theme_id
    return epics_by_theme, epic_to_theme, epic_ids


def _load_features(session: Session, epic_ids: list[int]) -> list[Feature]:
    if not epic_ids:
        return []
    return list(
        session.exec(
            select(Feature).where(cast("Any", Feature.epic_id).in_(epic_ids))
        ).all()
    )


def _index_features(
    *,
    theme_ids: list[int],
    epic_ids: list[int],
    features: list[Feature],
    epic_to_theme: dict[int, int],
) -> tuple[dict[int, list[Feature]], dict[int, list[str]], list[int]]:
    feature_ids = [
        feature.feature_id for feature in features if feature.feature_id is not None
    ]
    features_by_epic: dict[int, list[Feature]] = {epic_id: [] for epic_id in epic_ids}
    features_by_theme: dict[int, list[str]] = {theme_id: [] for theme_id in theme_ids}
    for feature in features:
        epic_id = feature.epic_id
        feature_id = feature.feature_id
        if epic_id is None or feature_id is None:
            continue
        features_by_epic[epic_id].append(feature)
        theme_id = epic_to_theme.get(epic_id)
        if theme_id is not None:
            features_by_theme[theme_id].append(feature.title)
    return features_by_epic, features_by_theme, feature_ids


def _load_story_counts(session: Session, feature_ids: list[int]) -> dict[int, int]:
    if not feature_ids:
        return {}
    story_counts_query = (
        select(UserStory.feature_id, func.count(UserStory.story_id))
        .where(cast("Any", UserStory.feature_id).in_(feature_ids))
        .group_by(UserStory.feature_id)
    )
    results = session.exec(story_counts_query).all()
    return {
        feature_id: count for feature_id, count in results if feature_id is not None
    }


def _build_feature_entry(
    feature: Feature,
    context: _FeatureEntryContext,
    count: int,
    sibling_features: list[str],
) -> tuple[FeatureForStory, dict[str, Any]]:
    feature_obj = FeatureForStory(
        feature_id=cast("int", feature.feature_id),
        feature_title=feature.title,
        theme_id=context.theme_id,
        epic_id=context.epic_id,
        theme=context.theme.title,
        epic=context.epic.title,
        existing_stories_count=count,
        time_frame=context.time_frame_value,
        theme_justification=context.theme.description,
        sibling_features=sibling_features,
    )
    feature_info: dict[str, Any] = {
        "feature_id": feature.feature_id,
        "feature_title": feature.title,
        "existing_stories_count": count,
        "time_frame": context.time_frame_value,
        "theme_justification": context.theme.description,
        "sibling_features": sibling_features,
    }
    return feature_obj, feature_info


def _build_epic_payload(
    epic: Epic,
    features_by_epic: dict[int, list[Feature]],
    context: _EpicPayloadContext,
) -> tuple[list[FeatureForStory], dict[str, Any]]:
    epic_id = epic.epic_id
    epic_data: dict[str, Any] = {
        "epic_id": epic_id,
        "epic_title": epic.title,
        "features": [],
    }
    if epic_id is None:
        return [], epic_data

    feature_items: list[FeatureForStory] = []
    for feature in features_by_epic.get(epic_id, []):
        feature_id = feature.feature_id
        if feature_id is None:
            continue
        sibling_features = [
            item for item in context.theme_all_features if item != feature.title
        ]
        feature_obj, feature_info = _build_feature_entry(
            feature,
            context.feature_entry,
            context.story_counts.get(feature_id, 0),
            sibling_features=sibling_features,
        )
        feature_items.append(feature_obj)
        epic_data["features"].append(feature_info)
    return feature_items, epic_data


def _build_theme_payload(
    theme: Theme,
    graph: _QueryGraph,
) -> tuple[list[FeatureForStory], dict[str, Any]] | None:
    theme_id = theme.theme_id
    if theme_id is None:
        return None

    time_frame_value = theme.time_frame.value if theme.time_frame else None
    if time_frame_value is None:
        time_frame_value = _derive_time_frame_from_title(theme.title)
    theme_data: dict[str, Any] = {
        "theme_id": theme_id,
        "theme_title": theme.title,
        "time_frame": time_frame_value,
        "justification": theme.description,
        "epics": [],
    }

    feature_items: list[FeatureForStory] = []
    theme_all_features = graph.features_by_theme.get(theme_id, [])
    for epic in graph.epics_by_theme.get(theme_id, []):
        epic_context = _EpicPayloadContext(
            feature_entry=_FeatureEntryContext(
                theme=theme,
                theme_id=theme_id,
                epic=epic,
                epic_id=cast("int", epic.epic_id),
                time_frame_value=time_frame_value,
            ),
            theme_all_features=theme_all_features,
            story_counts=graph.story_counts,
        )
        epic_features, epic_data = _build_epic_payload(
            epic,
            graph.features_by_epic,
            epic_context,
        )
        feature_items.extend(epic_features)
        theme_data["epics"].append(epic_data)
    return feature_items, theme_data


def _build_query_output(
    query_input: QueryFeaturesInput,
    project: Project,
    themes: list[Theme],
    graph: _QueryGraph,
) -> QueryFeaturesOutput:
    features_list: list[FeatureForStory] = []
    structure: list[dict[str, Any]] = []
    for theme in themes:
        theme_payload = _build_theme_payload(theme, graph)
        if theme_payload is None:
            continue
        feature_items, theme_data = theme_payload
        features_list.extend(feature_items)
        structure.append(theme_data)

    return QueryFeaturesOutput(
        success=True,
        project_id=query_input.project_id,
        product_name=project.name,
        features_flat=features_list,
        structure=structure,
        total_features=len(features_list),
        message=f"Found {len(features_list)} features for '{project.name}'",
    )


def query_features_for_stories(
    query_input: QueryFeaturesInput,
) -> dict[str, Any]:
    """
    Query all features for a project, organized by theme/epic.

    Used by the workflow adapter to provide context to the user story agent.

    Returns a JSON-serializable `QueryFeaturesOutput` payload whose
    `features_flat` entries always include validated theme and epic metadata.
    """
    logger.debug(
        "Querying features for story generation with project_id=%s.",
        query_input.project_id,
    )

    try:
        with Session(get_engine()) as session:
            project = session.get(Project, query_input.project_id)
            if not project:
                return {
                    "success": False,
                    "error": f"Project {query_input.project_id} not found",
                }

            themes = _load_themes(session, query_input.project_id)
            theme_ids = [t.theme_id for t in themes if t.theme_id is not None]
            epics = _load_epics(session, theme_ids)
            epics_by_theme, epic_to_theme, epic_ids = _index_epics(theme_ids, epics)
            features = _load_features(session, epic_ids)
            features_by_epic, features_by_theme, feature_ids = _index_features(
                theme_ids=theme_ids,
                epic_ids=epic_ids,
                features=features,
                epic_to_theme=epic_to_theme,
            )
            story_counts = _load_story_counts(session, feature_ids)
            graph = _QueryGraph(
                epics_by_theme=epics_by_theme,
                features_by_epic=features_by_epic,
                features_by_theme=features_by_theme,
                story_counts=story_counts,
            )
            validated_output = _build_query_output(
                query_input,
                project,
                themes,
                graph,
            )
            return validated_output.model_dump()

    except SQLAlchemyError:
        logger.exception(
            "Failed querying features for story generation with project_id=%s.",
            query_input.project_id,
        )
        raise
