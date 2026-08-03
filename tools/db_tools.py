# pylint: disable=no-member
# tools/db_tools.py

"""
Database persistence tools for agents to call.

These functions are designed to be invoked by Claude as tool calls.
"""

from typing import Any, TypedDict, cast

from pydantic import BaseModel
from sqlmodel import Session, select

from models.core import Epic, Feature, Project, ProjectPersona, Task, Theme, UserStory
from models.db import get_engine


class SeedProjectPersonasInput(BaseModel):
    """Input schema for seed_project_personas tool."""

    project_id: int


class _DefaultPersona(TypedDict):
    name: str
    is_default: bool
    category: str
    description: str


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


def seed_project_personas(params: SeedProjectPersonasInput) -> dict[str, Any]:
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
            f"Seeded {created_count} default personas for project "
            f"'{project.name}'"
        )
        return {
            "success": True,
            "project_id": params.project_id,
            "message": message,
            "count": created_count,
        }


class CreateOrGetProductInput(BaseModel):
    """Input schema for create_or_get_product tool."""

    product_name: str
    vision: str | None
    description: str | None


def create_or_get_product(params: CreateOrGetProductInput) -> dict[str, Any]:
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
            select(Project).where(Project.name == params.product_name)
        ).first()

        if not project:
            project = Project(
                name=params.product_name,
                vision=params.vision,
                description=params.description,
            )
            session.add(project)
            session.commit()
            session.refresh(project)
            return {
                "success": True,
                "project_id": project.project_id,
                "action": "created",
                "message": (
                    f"Created project '{params.product_name}' "
                    f"with ID {project.project_id}"
                ),
            }

        if params.vision is not None:
            project.vision = params.vision
        if params.description is not None:
            project.description = params.description
        session.add(project)
        session.commit()
        session.refresh(project)
        return {
            "success": True,
            "project_id": project.project_id,
            "action": "updated",
            "message": (
                f"Updated project '{params.product_name}' (ID {project.project_id})"
            ),
        }


def persist_roadmap(
    project_id: int, roadmap_items: list[dict[str, Any]]
) -> dict[str, Any]:
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

        created: dict[str, list[dict[str, Any]]] = {
            "themes": [],
            "epics": [],
            "features": [],
        }

        for item in roadmap_items:
            # Create Theme
            theme = Theme(
                title=(
                    f"{item.get('quarter', '')} - {item.get('theme_title', 'Unnamed')}"
                ),
                description=item.get("theme_description", ""),
                project_id=project_id,
            )
            session.add(theme)
            session.flush()

            if theme.theme_id is None:
                raise _PersistRoadmapError.missing_theme_id(theme.title)
            created["themes"].append({"id": theme.theme_id, "title": theme.title})

            # Create Epics under this Theme
            for epic_data in item.get("epics", []):
                epic = Epic(
                    title=epic_data.get("epic_title", "Unnamed Epic"),
                    summary=epic_data.get("epic_summary", ""),
                    theme_id=theme.theme_id,
                )
                session.add(epic)
                session.flush()

                if epic.epic_id is None:
                    raise _PersistRoadmapError.missing_epic_id(epic.title)
                created["epics"].append({"id": epic.epic_id, "title": epic.title})

                # Create Features under this Epic
                for feature_data in epic_data.get("features", []):
                    feature = Feature(
                        title=feature_data.get("title", "Unnamed Feature"),
                        description=feature_data.get("description", ""),
                        epic_id=epic.epic_id,
                    )
                    session.add(feature)
                    session.flush()

                    if feature.feature_id is None:
                        raise _PersistRoadmapError.missing_feature_id()
                    # Fix for Pylance (reportUnknownVariableType)
                    feature_dict: dict[str, Any] = {
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


class CreateUserStoryInput(BaseModel):
    """Input schema for create_user_story tool."""

    project_id: int
    feature_id: int
    title: str
    description: str
    acceptance_criteria: str | None
    story_points: int | None


def create_user_story(params: CreateUserStoryInput) -> dict[str, Any]:
    """
    Agent tool: Create a user story under a feature.

    Args:
        params: Input data for creating a user story.

    Returns:
        Dict with story_id and status
    """
    with Session(get_engine()) as session:
        feature = session.get(Feature, params.feature_id)
        if not feature:
            return {
                "success": False,
                "error": f"Feature {params.feature_id} not found",
            }

        story = UserStory(
            title=params.title,
            story_description=params.description,
            acceptance_criteria=params.acceptance_criteria,
            story_points=params.story_points,
            feature_id=params.feature_id,
            project_id=params.project_id,
        )
        session.add(story)
        session.commit()
        session.refresh(story)

        return {
            "success": True,
            "story_id": story.story_id,
            "feature_id": params.feature_id,
            "project_id": params.project_id,
            "message": (
                f"Created user story '{params.title}' with ID {story.story_id}"
            ),
        }


class CreateTaskInput(BaseModel):
    """Input schema for create_task tool."""

    story_id: int
    title: str
    description: str | None


def create_task(params: CreateTaskInput) -> dict[str, Any]:
    """
    Agent tool: Create a task under a user story.

    Args:
        params: Input data for creating a task.

    Returns:
        Dict with task_id and status
    """
    with Session(get_engine()) as session:
        story = session.get(UserStory, params.story_id)
        if not story:
            return {
                "success": False,
                "error": f"User story {params.story_id} not found",
            }

        # Fix for Pylance (reportCallIssue):
        # The 'Task' model only has a required 'description' field.
        # We combine the 'title' and optional 'description' from this
        # function to satisfy the model's requirement.
        task_description = params.title
        if params.description is not None:
            task_description = f"{params.title}\n\n{params.description}"

        task = Task(description=task_description, story_id=params.story_id)
        session.add(task)
        session.commit()
        session.refresh(task)

        return {
            "success": True,
            "task_id": task.task_id,
            "story_id": params.story_id,
            "message": f"Created task '{params.title}' with ID {task.task_id}",
        }


def query_product_structure(project_id: int) -> dict[str, Any]:
    """
    Agent tool: Query the full hierarchy of a project (for verification).

    Returns the entire Theme -> Epic -> Feature -> Story structure.
    """
    with Session(get_engine()) as session:
        project = session.get(Project, project_id)
        if not project:
            return {
                "success": False,
                "error": f"Project {project_id} not found",
            }
        themes = _load_product_themes(session, project_id)
        theme_ids = [theme.theme_id for theme in themes if theme.theme_id is not None]
        epics = _load_epics_for_theme_ids(session, theme_ids)
        epic_ids = [epic.epic_id for epic in epics if epic.epic_id is not None]
        features = _load_features_for_epic_ids(session, epic_ids)
        feature_ids = [
            feature.feature_id for feature in features if feature.feature_id is not None
        ]
        stories = _load_stories_for_feature_ids(session, feature_ids)
        structure = _build_product_structure(
            project=project,
            themes=themes,
            epics=epics,
            features=features,
            stories=stories,
        )

        return {"success": True, "structure": structure}


def _load_product_themes(session: Session, project_id: int) -> list[Theme]:
    return list(session.exec(select(Theme).where(Theme.project_id == project_id)).all())


def _load_epics_for_theme_ids(session: Session, theme_ids: list[int]) -> list[Epic]:
    if not theme_ids:
        return []
    return list(
        session.exec(
            select(Epic).where(cast("Any", Epic.theme_id).in_(theme_ids))
        ).all()
    )


def _load_features_for_epic_ids(session: Session, epic_ids: list[int]) -> list[Feature]:
    if not epic_ids:
        return []
    return list(
        session.exec(
            select(Feature).where(cast("Any", Feature.epic_id).in_(epic_ids))
        ).all()
    )


def _load_stories_for_feature_ids(
    session: Session,
    feature_ids: list[int],
) -> list[UserStory]:
    if not feature_ids:
        return []
    return list(
        session.exec(
            select(UserStory).where(cast("Any", UserStory.feature_id).in_(feature_ids))
        ).all()
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


def _group_stories_by_feature(
    stories: list[UserStory],
    feature_ids: list[int],
) -> dict[int, list[UserStory]]:
    grouped: dict[int, list[UserStory]] = {feature_id: [] for feature_id in feature_ids}
    for story in stories:
        feature_id = story.feature_id
        if feature_id is not None and feature_id in grouped:
            grouped[feature_id].append(story)
    return grouped


def _build_product_structure(
    *,
    project: Project,
    themes: list[Theme],
    epics: list[Epic],
    features: list[Feature],
    stories: list[UserStory],
) -> dict[str, Any]:
    theme_ids = [theme.theme_id for theme in themes if theme.theme_id is not None]
    epic_ids = [epic.epic_id for epic in epics if epic.epic_id is not None]
    feature_ids = [
        feature.feature_id for feature in features if feature.feature_id is not None
    ]

    epics_by_theme = _group_epics_by_theme(epics, theme_ids)
    features_by_epic = _group_features_by_epic(features, epic_ids)
    stories_by_feature = _group_stories_by_feature(stories, feature_ids)

    structure: dict[str, Any] = {
        "project": {
            "id": project.project_id,
            "name": project.name,
            "vision": project.vision,
        },
        "themes": [],
    }

    for theme in themes:
        theme_id = theme.theme_id
        if theme_id is None:
            continue
        theme_data = _build_theme_data(
            theme=theme,
            epics=epics_by_theme.get(theme_id, []),
            features_by_epic=features_by_epic,
            stories_by_feature=stories_by_feature,
        )
        structure["themes"].append(theme_data)

    return structure


def _build_theme_data(
    *,
    theme: Theme,
    epics: list[Epic],
    features_by_epic: dict[int, list[Feature]],
    stories_by_feature: dict[int, list[UserStory]],
) -> dict[str, Any]:
    theme_id = cast("int", theme.theme_id)
    theme_data: dict[str, Any] = {
        "id": theme_id,
        "title": theme.title,
        "epics": [],
    }
    for epic in epics:
        epic_data = _build_epic_data(
            epic=epic,
            features=features_by_epic.get(cast("int", epic.epic_id), []),
            stories_by_feature=stories_by_feature,
        )
        theme_data["epics"].append(epic_data)
    return theme_data


def _build_epic_data(
    *,
    epic: Epic,
    features: list[Feature],
    stories_by_feature: dict[int, list[UserStory]],
) -> dict[str, Any]:
    epic_id = cast("int", epic.epic_id)
    epic_data: dict[str, Any] = {
        "id": epic_id,
        "title": epic.title,
        "features": [],
    }
    for feature in features:
        feature_id = cast("int", feature.feature_id)
        feature_data: dict[str, Any] = {
            "id": feature_id,
            "title": feature.title,
            "stories": _build_story_entries(stories_by_feature.get(feature_id, [])),
        }
        epic_data["features"].append(feature_data)
    return epic_data


def _build_story_entries(stories: list[UserStory]) -> list[dict[str, Any]]:
    return [
        {
            "id": story.story_id,
            "title": story.title,
            "description": story.story_description,
            "points": story.story_points,
        }
        for story in stories
    ]


def get_story_details(story_id: int) -> dict[str, Any]:
    """
    Agent tool: Fetch details for a specific story by its ID.

    Args:
        story_id: The ID of the story to fetch

    Returns:
        Dict with story details or error message
    """
    with Session(get_engine()) as session:
        story = session.get(UserStory, story_id)

        if not story:
            return {
                "success": False,
                "story_id": story_id,
                "message": f"Story with ID {story_id} not found.",
            }

        return {
            "success": True,
            "story_id": story.story_id,
            "title": story.title,
            "description": story.story_description,
            "acceptance_criteria": story.acceptance_criteria,
            "status": story.status.value
            if hasattr(story.status, "value")
            else story.status,
            "story_points": story.story_points,
            "rank": story.rank,
            "feature_id": story.feature_id,
            "project_id": story.project_id,
            "created_at": str(story.created_at),
            "updated_at": str(story.updated_at),
        }
