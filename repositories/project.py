"""Persistence operations for the canonical Project aggregate."""

import logging

from sqlalchemy import delete, or_
from sqlmodel import Session, col, select

from models.core import (
    Epic,
    Feature,
    Project,
    ProjectPersona,
    ProjectTeam,
    Sprint,
    SprintStory,
    Task,
    Theme,
    UserStory,
    UserStoryDependency,
)
from models.db import get_engine
from models.events import StoryCompletionLog, TaskExecutionLog, WorkflowEvent
from models.product_definition import (
    ProductGoalArtifact,
    ProductGoalArtifactDecision,
    ProductGoalInterviewTurn,
    ProductGoalOutcome,
    SpecificationCandidate,
    SpecificationDecision,
    SpecificationSource,
    VisionArtifact,
    VisionArtifactDecision,
    VisionEvidenceSnapshot,
    VisionInterviewTurn,
    VisionRevisionIntent,
)
from models.repository import RepositoryBinding
from models.specs import SpecRegistry
from models.workflow import (
    BacklogArtifact,
    BacklogArtifactDecision,
    PostSprintTriage,
    RoadmapArtifact,
    RoadmapArtifactDecision,
    SprintClosure,
    SprintPlanArtifact,
    SprintPlanArtifactDecision,
    SprintReview,
    SprintStart,
    StoryArtifact,
    StoryArtifactDecision,
    StoryClosure,
    StoryDependencyReview,
    TaskCompletionEvidence,
    WorkflowNodeAttempt,
    WorkflowNodeAttemptOutcome,
)

logger: logging.Logger = logging.getLogger(name=__name__)

PROJECT_DELETION_CONFLICT_MESSAGE = "Project deletion blocked by retained references."


class ProjectDeletionConflictError(RuntimeError):
    """Raised when deleting a project would orphan another project's data."""

    def __init__(self, *, project_id: int, references: tuple[str, ...]) -> None:
        """Record the project and inbound foreign-key relationships."""
        super().__init__(PROJECT_DELETION_CONFLICT_MESSAGE)
        self.project_id = project_id
        self.references = references


def _delete_project_spec_rows(session: Session, project_id: int) -> None:
    """Delete the cyclic candidate/registry pair in foreign-key-safe order."""
    spec_versions = session.exec(
        select(SpecRegistry)
        .where(SpecRegistry.project_id == project_id)
        .order_by(col(SpecRegistry.spec_version_id).desc())
    ).all()
    for spec_version in spec_versions:
        session.delete(spec_version)
    session.flush()
    session.exec(
        delete(SpecificationDecision).where(
            col(SpecificationDecision.project_id) == project_id
        )
    )
    session.flush()
    session.exec(
        delete(SpecificationCandidate).where(
            col(SpecificationCandidate.project_id) == project_id
        )
    )
    session.flush()
    session.exec(
        delete(SpecificationSource).where(
            col(SpecificationSource.project_id) == project_id
        )
    )
    session.flush()


def _delete_project_story_dependencies(
    session: Session,
    *,
    project_id: int,
) -> None:
    """Delete only dependency associations owned by the deleted Project."""
    session.exec(
        delete(UserStoryDependency).where(
            col(UserStoryDependency.project_id) == project_id
        )
    )


def _ensure_no_cross_project_story_dependencies(
    session: Session,
    *,
    project_id: int,
    story_ids: list[int],
) -> None:
    """Fail before mutation when another Project owns an inbound Story edge."""
    if not story_ids:
        return
    inbound = session.exec(
        select(UserStoryDependency.dependency_id).where(
            col(UserStoryDependency.project_id) != project_id,
            or_(
                col(UserStoryDependency.dependent_story_id).in_(story_ids),
                col(UserStoryDependency.prerequisite_story_id).in_(story_ids),
            ),
        )
    ).first()
    if inbound is not None:
        raise ProjectDeletionConflictError(
            project_id=project_id,
            references=("user_story_dependencies.project_id",),
        )


def _delete_task_execution_logs(
    session: Session,
    *,
    task_ids: list[int],
    sprint_ids: list[int],
) -> None:
    """Delete execution logs before either referenced parent is deleted."""
    if not task_ids and not sprint_ids:
        return

    statement = delete(TaskExecutionLog)
    if task_ids and sprint_ids:
        statement = statement.where(
            or_(
                col(TaskExecutionLog.task_id).in_(task_ids),
                col(TaskExecutionLog.sprint_id).in_(sprint_ids),
            )
        )
    elif task_ids:
        statement = statement.where(col(TaskExecutionLog.task_id).in_(task_ids))
    else:
        statement = statement.where(col(TaskExecutionLog.sprint_id).in_(sprint_ids))

    session.exec(statement)


def _delete_project_workflow_events(
    session: Session,
    *,
    project_id: int,
    sprint_ids: list[int],
) -> None:
    """Delete events linked directly to the project or through its sprints."""
    statement = delete(WorkflowEvent)
    if sprint_ids:
        statement = statement.where(
            or_(
                col(WorkflowEvent.project_id) == project_id,
                col(WorkflowEvent.sprint_id).in_(sprint_ids),
            )
        )
    else:
        statement = statement.where(col(WorkflowEvent.project_id) == project_id)
    session.exec(statement)


def _delete_project_personas(session: Session, project_id: int) -> None:
    """Delete personas owned by the project."""
    rows = session.exec(
        select(ProjectPersona).where(ProjectPersona.project_id == project_id)
    ).all()
    for row in rows:
        session.delete(row)


def _delete_project_sprints(session: Session, sprints: list[Sprint]) -> None:
    """Delete already-unlinked Sprints."""
    for sprint in sprints:
        session.delete(sprint)


def _delete_project_sprint_plan_rows(session: Session, project_id: int) -> None:
    """Delete Sprint-plan decisions and immutable rows from newest to oldest."""
    session.exec(
        delete(SprintPlanArtifactDecision).where(
            col(SprintPlanArtifactDecision.project_id) == project_id
        )
    )
    rows = session.exec(
        select(SprintPlanArtifact)
        .where(col(SprintPlanArtifact.project_id) == project_id)
        .order_by(col(SprintPlanArtifact.version_number).desc())
    ).all()
    for row in rows:
        session.delete(row)
    session.flush()


def _delete_project_execution_rows(
    session: Session,
    *,
    project_id: int,
    sprint_ids: list[int],
    story_ids: list[int],
    task_ids: list[int],
) -> None:
    """Delete Sprint/Story/Task dependents before operational parent rows."""
    for model in (
        TaskCompletionEvidence,
        StoryClosure,
        SprintClosure,
        SprintReview,
        PostSprintTriage,
    ):
        session.exec(delete(model).where(col(model.project_id) == project_id))
    _delete_task_execution_logs(
        session,
        task_ids=task_ids,
        sprint_ids=sprint_ids,
    )
    if story_ids:
        session.exec(
            delete(StoryCompletionLog).where(
                col(StoryCompletionLog.story_id).in_(story_ids)
            )
        )
    link_statement = delete(SprintStory)
    if sprint_ids and story_ids:
        link_statement = link_statement.where(
            or_(
                col(SprintStory.sprint_id).in_(sprint_ids),
                col(SprintStory.story_id).in_(story_ids),
            )
        )
    elif sprint_ids:
        link_statement = link_statement.where(
            col(SprintStory.sprint_id).in_(sprint_ids)
        )
    elif story_ids:
        link_statement = link_statement.where(col(SprintStory.story_id).in_(story_ids))
    else:
        return
    session.exec(link_statement)


def _delete_project_planning_artifacts(session: Session, project_id: int) -> None:
    """Delete reviewed Story, Roadmap, and Backlog chains child-first."""
    for decision_model in (
        StoryArtifactDecision,
        RoadmapArtifactDecision,
        BacklogArtifactDecision,
    ):
        session.exec(
            delete(decision_model).where(col(decision_model.project_id) == project_id)
        )
    for artifact_model in (StoryArtifact, RoadmapArtifact, BacklogArtifact):
        rows = session.exec(
            select(artifact_model)
            .where(col(artifact_model.project_id) == project_id)
            .order_by(col(artifact_model.version_number).desc())
        ).all()
        for row in rows:
            session.delete(row)
        session.flush()


def _delete_project_stories(
    session: Session,
    *,
    project_id: int,
    stories: list[UserStory],
) -> None:
    """Delete project Tasks and Stories after all dependent rows are gone."""
    _delete_project_story_dependencies(
        session,
        project_id=project_id,
    )
    for story in stories:
        tasks = session.exec(select(Task).where(Task.story_id == story.story_id)).all()
        for task in tasks:
            session.delete(task)
        session.delete(story)


def _delete_project_roadmap(session: Session, project_id: int) -> None:
    """Delete the project theme, epic, and feature hierarchy."""
    themes = session.exec(select(Theme).where(Theme.project_id == project_id)).all()
    for theme in themes:
        epics = session.exec(select(Epic).where(Epic.theme_id == theme.theme_id)).all()
        for epic in epics:
            features = session.exec(
                select(Feature).where(Feature.epic_id == epic.epic_id)
            ).all()
            for feature in features:
                session.delete(feature)
            session.delete(epic)
        session.delete(theme)


def _delete_project_lifecycle_rows(session: Session, project: Project) -> None:
    """Delete current product-definition and repository rows in FK-safe order."""
    project_id = project.project_id
    if project_id is None:
        message = "Project deletion requires a durable Project identity."
        raise RuntimeError(message)
    for model in (
        ProductGoalOutcome,
        ProductGoalArtifactDecision,
        ProductGoalArtifact,
        ProductGoalInterviewTurn,
        VisionArtifactDecision,
    ):
        session.exec(delete(model).where(col(model.project_id) == project_id))
    _delete_project_vision_rows(session, project_id)
    session.exec(
        delete(VisionEvidenceSnapshot).where(
            col(VisionEvidenceSnapshot.project_id) == project_id
        )
    )
    session.exec(
        delete(WorkflowNodeAttemptOutcome).where(
            col(WorkflowNodeAttemptOutcome.project_id) == project_id
        )
    )
    session.exec(
        delete(WorkflowNodeAttempt).where(
            col(WorkflowNodeAttempt.project_id) == project_id
        )
    )
    project.active_repository_binding_id = None
    session.add(project)
    session.flush()
    session.exec(
        delete(RepositoryBinding).where(col(RepositoryBinding.project_id) == project_id)
    )


def _delete_project_vision_rows(session: Session, project_id: int) -> None:
    """Delete revision chains before their source Vision artifacts and turns."""
    intents = session.exec(
        select(VisionRevisionIntent)
        .where(col(VisionRevisionIntent.project_id) == project_id)
        .order_by(col(VisionRevisionIntent.vision_revision_intent_id).desc())
    ).all()
    for intent in intents:
        turns = session.exec(
            select(VisionInterviewTurn)
            .where(
                col(VisionInterviewTurn.project_id) == project_id,
                col(VisionInterviewTurn.revision_intent_id)
                == intent.vision_revision_intent_id,
            )
            .order_by(col(VisionInterviewTurn.vision_interview_turn_id).desc())
        ).all()
        turn_ids = [
            turn.vision_interview_turn_id
            for turn in turns
            if turn.vision_interview_turn_id is not None
        ]
        if turn_ids:
            artifacts = session.exec(
                select(VisionArtifact)
                .where(
                    col(VisionArtifact.project_id) == project_id,
                    col(VisionArtifact.source_interview_turn_id).in_(turn_ids),
                )
                .order_by(col(VisionArtifact.version_number).desc())
            ).all()
            for artifact in artifacts:
                session.delete(artifact)
            session.flush()
        for turn in turns:
            session.delete(turn)
        session.flush()
        session.delete(intent)
        session.flush()

    remaining_artifacts = session.exec(
        select(VisionArtifact)
        .where(col(VisionArtifact.project_id) == project_id)
        .order_by(col(VisionArtifact.version_number).desc())
    ).all()
    for artifact in remaining_artifacts:
        session.delete(artifact)
    session.flush()

    remaining_turns = session.exec(
        select(VisionInterviewTurn)
        .where(col(VisionInterviewTurn.project_id) == project_id)
        .order_by(col(VisionInterviewTurn.vision_interview_turn_id).desc())
    ).all()
    for turn in remaining_turns:
        session.delete(turn)
    session.flush()


class ProjectRepository:
    """Repository handling database operations for the Project entity."""

    def __init__(self, session: Session | None = None) -> None:
        """Use a caller-owned transaction or open sessions per operation."""
        # Allow passing an explicit session (for transactions),
        # otherwise create one and close it immediately per call.
        self._session = session

    def _get_session(self) -> Session:
        if self._session is not None:
            return self._session
        return Session(get_engine())

    def get_all(self) -> list[Project]:
        """Fetch all projects."""
        with self._get_session() as session:
            statement = select(Project)
            return list(session.exec(statement).all())

    def get_by_id(self, project_id: int) -> Project | None:
        """Fetch a specific project by its ID."""
        with self._get_session() as session:
            return session.get(Project, project_id)

    def create(self, name: str, description: str | None = None) -> Project:
        """Create a new project."""
        project = Project(name=name, description=description)
        # We must manage the transaction locally if we spawned the session
        session = self._get_session()
        try:
            session.add(project)
            session.commit()
            session.refresh(project)
            return project
        finally:
            if not self._session:
                session.close()

    def delete_project(self, project_id: int) -> bool:
        """Fully delete a project and all associated agile entities."""
        session = self._get_session()
        try:
            project = session.get(Project, project_id)
            if not project:
                return False

            sprints = session.exec(
                select(Sprint).where(Sprint.project_id == project_id)
            ).all()
            sprint_ids = [
                sprint.sprint_id for sprint in sprints if sprint.sprint_id is not None
            ]
            stories = session.exec(
                select(UserStory).where(UserStory.project_id == project_id)
            ).all()
            story_ids = [
                story.story_id for story in stories if story.story_id is not None
            ]
            task_ids = (
                list(
                    session.exec(
                        select(Task.task_id).where(col(Task.story_id).in_(story_ids))
                    ).all()
                )
                if story_ids
                else []
            )
            _ensure_no_cross_project_story_dependencies(
                session,
                project_id=project_id,
                story_ids=story_ids,
            )

            session.exec(
                delete(SprintStart).where(col(SprintStart.project_id) == project_id)
            )
            session.exec(
                delete(StoryDependencyReview).where(
                    col(StoryDependencyReview.project_id) == project_id
                )
            )
            _delete_project_workflow_events(
                session,
                project_id=project_id,
                sprint_ids=sprint_ids,
            )
            _delete_project_execution_rows(
                session,
                project_id=project_id,
                sprint_ids=sprint_ids,
                story_ids=story_ids,
                task_ids=task_ids,
            )

            _delete_project_personas(session, project_id)

            _delete_project_sprint_plan_rows(session, project_id)
            _delete_project_sprints(session, list(sprints))
            _delete_project_stories(
                session,
                project_id=project_id,
                stories=list(stories),
            )
            session.flush()

            _delete_project_planning_artifacts(session, project_id)
            _delete_project_spec_rows(session, project_id)
            _delete_project_lifecycle_rows(session, project)
            _delete_project_roadmap(session, project_id)
            session.exec(
                delete(ProjectTeam).where(col(ProjectTeam.project_id) == project_id)
            )
            session.delete(project)

            session.commit()
        except Exception:
            session.rollback()
            raise
        else:
            return True
        finally:
            if not self._session:
                session.close()
