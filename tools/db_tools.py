# pylint: disable=no-member
# tools/db_tools.py

"""
Database persistence tools for agents to call.

These functions are designed to be invoked by Claude as tool calls.
"""

from collections.abc import Mapping, Sequence
from typing import Literal, TypedDict, TypeGuard

from pydantic import BaseModel
from sqlmodel import Session, col, select

from models.core import Epic, Feature, Project, ProjectPersona, Theme
from models.db import get_engine
from services.vision_projection import (
    VisionLineageError,
    load_current_accepted_vision,
)


class SeedProjectPersonasInput(BaseModel):
    """Input schema for seed_project_personas tool."""

    project_id: int


class _DefaultPersona(TypedDict):
    name: str
    is_default: bool
    category: str
    description: str


class _ToolFailure(TypedDict):
    success: Literal[False]
    error: str


class _ExistingPersonasResult(TypedDict):
    success: Literal[True]
    message: str
    count: int


class _CreatedPersonasResult(_ExistingPersonasResult):
    project_id: int


type SeedProjectPersonasResult = (
    _ToolFailure | _ExistingPersonasResult | _CreatedPersonasResult
)


class _PersistRoadmapError(RuntimeError):
    @classmethod
    def missing_theme_id(cls, title: str) -> "_PersistRoadmapError":
        message = f"Failed to create Theme '{title}', ID is None after flush."
        return cls(message)

    @classmethod
    def missing_epic_id(cls, title: str) -> "_PersistRoadmapError":
        message = f"Failed to create Epic '{title}', ID is None after flush."
        return cls(message)

    @classmethod
    def missing_feature_id(cls) -> "_PersistRoadmapError":
        message = "Failed to create Feature, ID is None after flush."
        return cls(message)


def seed_project_personas(
    params: SeedProjectPersonasInput,
) -> SeedProjectPersonasResult:
    """
    Agent tool: Seed default personas for the Review-First project.

    Call this after project creation.
    """
    with Session(get_engine()) as session:
        project = session.get(Project, params.project_id)
        if not project:
            return {
                "success": False,
                "error": f"Project {params.project_id} not found",
            }

        # Check if personas already exist
        existing = session.exec(
            select(ProjectPersona).where(ProjectPersona.project_id == params.project_id)
        ).all()
        if existing:
            return {
                "success": True,
                "message": f"Personas already exist for project {params.project_id}",
                "count": len(existing),
            }

        default_personas: list[_DefaultPersona] = [
            {
                "name": "automation engineer",
                "is_default": True,
                "category": "primary_user",
                "description": (
                    "Automation and control engineers performing P&ID review "
                    "and extraction configuration"
                ),
            },
            {
                "name": "engineering qa reviewer",
                "is_default": False,
                "category": "primary_user",
                "description": (
                    "Engineering QA reviewers performing mandatory validation "
                    "and sign-off"
                ),
            },
            {
                "name": "it administrator",
                "is_default": False,
                "category": "admin",
                "description": (
                    "IT administrators managing deployment, security, and "
                    "user permissions"
                ),
            },
            {
                "name": "ml engineer",
                "is_default": False,
                "category": "platform",
                "description": "ML engineers training and tuning extraction models",
            },
        ]

        created_count = 0
        for p_data in default_personas:
            persona = ProjectPersona(
                project_id=params.project_id,
                persona_name=p_data["name"],
                is_default=p_data["is_default"],
                category=p_data["category"],
                description=p_data["description"],
            )
            session.add(persona)
            created_count += 1

        session.commit()
        message = (
            f"Seeded {created_count} default personas for project '{project.name}'"
        )
        return {
            "success": True,
            "project_id": params.project_id,
            "message": message,
            "count": created_count,
        }


class CreateOrGetProjectInput(BaseModel):
    """Input schema for create_or_get_project tool."""

    project_name: str
    vision: str | None
    description: str | None


class CreateOrGetProjectResult(TypedDict):
    """Stable result for creating or updating one Project."""

    success: Literal[True]
    project_id: int
    action: Literal["created", "updated"]
    message: str


def _require_project_id(project: Project) -> int:
    project_id = project.project_id
    if project_id is None:
        message = "Persisted Project did not receive an identity."
        raise RuntimeError(message)
    return project_id


def create_or_get_project(params: CreateOrGetProjectInput) -> CreateOrGetProjectResult:
    """
    Agent tool: Create a project or update its vision.

    Args:
        params: Input data for creating or getting a project.

    Returns:
        Dict with project_id and status
    """
    with Session(get_engine()) as session:
        # Try to find existing project
        project = session.exec(
            select(Project).where(Project.name == params.project_name)
        ).first()

        if not project:
            project = Project(
                name=params.project_name,
                vision=params.vision,
                description=params.description,
            )
            session.add(project)
            session.commit()
            session.refresh(project)
            project_id = _require_project_id(project)
            return {
                "success": True,
                "project_id": project_id,
                "action": "created",
                "message": (
                    f"Created project '{params.project_name}' with ID {project_id}"
                ),
            }

        if params.vision is not None:
            project.vision = params.vision
        if params.description is not None:
            project.description = params.description
        session.add(project)
        session.commit()
        session.refresh(project)
        project_id = _require_project_id(project)
        return {
            "success": True,
            "project_id": project_id,
            "action": "updated",
            "message": (f"Updated project '{params.project_name}' (ID {project_id})"),
        }


class _RoadmapEntry(TypedDict):
    id: int
    title: str


class _CreatedRoadmap(TypedDict):
    themes: list[_RoadmapEntry]
    epics: list[_RoadmapEntry]
    features: list[_RoadmapEntry]


class _PersistRoadmapSuccess(TypedDict):
    success: Literal[True]
    project_id: int
    created: _CreatedRoadmap
    message: str


type PersistRoadmapResult = _ToolFailure | _PersistRoadmapSuccess


def _mapping_text(
    values: Mapping[str, object],
    key: str,
    default: str,
) -> str:
    value = values.get(key)
    return value if isinstance(value, str) else default


def _mapping_list(
    values: Mapping[str, object],
    key: str,
) -> list[Mapping[str, object]]:
    value = values.get(key)
    if not isinstance(value, list):
        return []
    return [entry for entry in value if _is_string_mapping(entry)]


def _is_string_mapping(value: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(value, Mapping) and all(isinstance(key, str) for key in value)


def persist_roadmap(
    project_id: int,
    roadmap_items: Sequence[Mapping[str, object]],
) -> PersistRoadmapResult:
    """
    Agent tool: Parse roadmap and create Theme/Epic/Feature hierarchy.

    Args:
        project_id: The project to attach roadmap to
        roadmap_items: List of dicts with structure. (See docstring in editor)

    Returns:
        Dict with created IDs and status
    """
    with Session(get_engine()) as session:
        project = session.get(Project, project_id)
        if not project:
            return {
                "success": False,
                "error": f"Project {project_id} not found",
            }

        created: _CreatedRoadmap = {
            "themes": [],
            "epics": [],
            "features": [],
        }

        for item in roadmap_items:
            # Create Theme
            theme = Theme(
                title=(
                    f"{_mapping_text(item, 'quarter', '')} - "
                    f"{_mapping_text(item, 'theme_title', 'Unnamed')}"
                ),
                description=_mapping_text(item, "theme_description", ""),
                project_id=project_id,
            )
            session.add(theme)
            session.flush()

            if theme.theme_id is None:
                raise _PersistRoadmapError.missing_theme_id(theme.title)
            created["themes"].append({"id": theme.theme_id, "title": theme.title})

            # Create Epics under this Theme
            for epic_data in _mapping_list(item, "epics"):
                epic = Epic(
                    title=_mapping_text(epic_data, "epic_title", "Unnamed Epic"),
                    summary=_mapping_text(epic_data, "epic_summary", ""),
                    theme_id=theme.theme_id,
                )
                session.add(epic)
                session.flush()

                if epic.epic_id is None:
                    raise _PersistRoadmapError.missing_epic_id(epic.title)
                created["epics"].append({"id": epic.epic_id, "title": epic.title})

                # Create Features under this Epic
                for feature_data in _mapping_list(epic_data, "features"):
                    feature = Feature(
                        title=_mapping_text(feature_data, "title", "Unnamed Feature"),
                        description=_mapping_text(feature_data, "description", ""),
                        epic_id=epic.epic_id,
                    )
                    session.add(feature)
                    session.flush()

                    if feature.feature_id is None:
                        raise _PersistRoadmapError.missing_feature_id()
                    feature_dict: _RoadmapEntry = {
                        "id": feature.feature_id,
                        "title": feature.title,
                    }
                    created["features"].append(feature_dict)

        session.commit()

        return {
            "success": True,
            "project_id": project_id,
            "created": created,
            "message": (
                f"Created {len(created['themes'])} themes, "
                f"{len(created['epics'])} epics, "
                f"{len(created['features'])} features"
            ),
        }


class _FeatureStructure(TypedDict):
    id: int
    title: str


class _EpicStructure(TypedDict):
    id: int
    title: str
    features: list[_FeatureStructure]


class _ThemeStructure(TypedDict):
    id: int
    title: str
    epics: list[_EpicStructure]


class _ProjectStructureSummary(TypedDict):
    id: int
    name: str
    vision: str | None


class ProjectStructure(TypedDict):
    """Nested Project roadmap returned by the query tool."""

    project: _ProjectStructureSummary
    themes: list[_ThemeStructure]


class QueryProjectStructureSuccess(TypedDict):
    """Successful Project structure query result."""

    success: Literal[True]
    structure: ProjectStructure


class QueryProjectStructureFailure(TypedDict):
    """Failed Project structure query result."""

    success: Literal[False]
    error: str


type QueryProjectStructureResult = (
    QueryProjectStructureSuccess | QueryProjectStructureFailure
)


def query_project_structure(project_id: int) -> QueryProjectStructureResult:
    """
    Agent tool: Query the full hierarchy of a project (for verification).

    Returns the Theme -> Epic -> Feature project structure.
    """
    with Session(get_engine()) as session:
        project = session.get(Project, project_id)
        if not project:
            return {
                "success": False,
                "error": f"Project {project_id} not found",
            }
        try:
            vision = load_current_accepted_vision(session, project_id=project_id)
        except VisionLineageError as error:
            return {
                "success": False,
                "error": f"Vision lineage is invalid: {error}",
            }
        themes = _load_project_themes(session, project_id)
        theme_ids = [theme.theme_id for theme in themes if theme.theme_id is not None]
        epics = _load_epics_for_theme_ids(session, theme_ids)
        epic_ids = [epic.epic_id for epic in epics if epic.epic_id is not None]
        features = _load_features_for_epic_ids(session, epic_ids)
        project_summary: _ProjectStructureSummary = {
            "id": _require_project_id(project),
            "name": project.name,
            "vision": None if vision is None else vision.statement,
        }
        structure = _build_project_structure(
            project=project_summary,
            themes=themes,
            epics=epics,
            features=features,
        )

        return {"success": True, "structure": structure}


def _load_project_themes(session: Session, project_id: int) -> list[Theme]:
    return list(session.exec(select(Theme).where(Theme.project_id == project_id)).all())


def _load_epics_for_theme_ids(session: Session, theme_ids: list[int]) -> list[Epic]:
    if not theme_ids:
        return []
    return list(
        session.exec(select(Epic).where(col(Epic.theme_id).in_(theme_ids))).all()
    )


def _load_features_for_epic_ids(session: Session, epic_ids: list[int]) -> list[Feature]:
    if not epic_ids:
        return []
    return list(
        session.exec(select(Feature).where(col(Feature.epic_id).in_(epic_ids))).all()
    )


def _group_epics_by_theme(
    epics: list[Epic],
    theme_ids: list[int],
) -> dict[int, list[Epic]]:
    grouped: dict[int, list[Epic]] = {theme_id: [] for theme_id in theme_ids}
    for epic in epics:
        theme_id = epic.theme_id
        if theme_id is not None and theme_id in grouped:
            grouped[theme_id].append(epic)
    return grouped


def _group_features_by_epic(
    features: list[Feature],
    epic_ids: list[int],
) -> dict[int, list[Feature]]:
    grouped: dict[int, list[Feature]] = {epic_id: [] for epic_id in epic_ids}
    for feature in features:
        epic_id = feature.epic_id
        if epic_id is not None and epic_id in grouped:
            grouped[epic_id].append(feature)
    return grouped


def _build_project_structure(
    *,
    project: _ProjectStructureSummary,
    themes: list[Theme],
    epics: list[Epic],
    features: list[Feature],
) -> ProjectStructure:
    theme_ids = [theme.theme_id for theme in themes if theme.theme_id is not None]
    epic_ids = [epic.epic_id for epic in epics if epic.epic_id is not None]
    epics_by_theme = _group_epics_by_theme(epics, theme_ids)
    features_by_epic = _group_features_by_epic(features, epic_ids)

    theme_entries: list[_ThemeStructure] = []
    structure: ProjectStructure = {
        "project": project,
        "themes": theme_entries,
    }

    for theme in themes:
        theme_id = theme.theme_id
        if theme_id is None:
            continue
        theme_data = _build_theme_data(
            theme=theme,
            epics=epics_by_theme.get(theme_id, []),
            features_by_epic=features_by_epic,
        )
        theme_entries.append(theme_data)

    return structure


def _build_theme_data(
    *,
    theme: Theme,
    epics: list[Epic],
    features_by_epic: dict[int, list[Feature]],
) -> _ThemeStructure:
    if theme.theme_id is None:
        message = "Theme structure requires a persisted identity."
        raise RuntimeError(message)
    theme_id = theme.theme_id
    epic_entries: list[_EpicStructure] = []
    theme_data: _ThemeStructure = {
        "id": theme_id,
        "title": theme.title,
        "epics": epic_entries,
    }
    for epic in epics:
        epic_id = epic.epic_id
        if epic_id is None:
            continue
        epic_data = _build_epic_data(
            epic=epic,
            features=features_by_epic.get(epic_id, []),
        )
        epic_entries.append(epic_data)
    return theme_data


def _build_epic_data(
    *,
    epic: Epic,
    features: list[Feature],
) -> _EpicStructure:
    if epic.epic_id is None:
        message = "Epic structure requires a persisted identity."
        raise RuntimeError(message)
    epic_id = epic.epic_id
    feature_entries: list[_FeatureStructure] = []
    epic_data: _EpicStructure = {
        "id": epic_id,
        "title": epic.title,
        "features": feature_entries,
    }
    for feature in features:
        if feature.feature_id is None:
            continue
        feature_data: _FeatureStructure = {
            "id": feature.feature_id,
            "title": feature.title,
        }
        feature_entries.append(feature_data)
    return epic_data
