"""Persistence operations for the canonical Project aggregate."""

import logging

from sqlalchemy import delete, or_, update
from sqlmodel import Session, col, select

from models.authority_curation import (
    AuthorityCurationAttempt,
    AuthorityFeedbackAttempt,
)
from models.core import (
    Epic,
    Feature,
    Project,
    ProjectPersona,
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
    VisionArtifact,
    VisionArtifactDecision,
    VisionEvidenceSnapshot,
    VisionInterviewTurn,
    VisionRevisionIntent,
)
from models.repository import RepositoryBinding
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry
from models.workflow import WorkflowNodeAttempt, WorkflowNodeAttemptOutcome

logger: logging.Logger = logging.getLogger(name=__name__)

PROJECT_DELETION_CONFLICT_MESSAGE = "Project deletion blocked by retained references."


class ProjectDeletionConflictError(RuntimeError):
    """Raised when deleting a project would orphan another project's data."""

    def __init__(self, *, project_id: int, references: tuple[str, ...]) -> None:
        """Record the project and inbound foreign-key relationships."""
        super().__init__(PROJECT_DELETION_CONFLICT_MESSAGE)
        self.project_id = project_id
        self.references = references


def _ensure_project_authority_deletable(session: Session, project_id: int) -> None:
    """Reject deletion before removing any historically accepted authority."""
    accepted_authority_id = session.exec(
        select(SpecAuthorityAcceptance.id)
        .join(
            SpecRegistry,
            col(SpecRegistry.spec_version_id)
            == col(SpecAuthorityAcceptance.spec_version_id),
        )
        .where(
            or_(
                col(SpecAuthorityAcceptance.project_id) == project_id,
                col(SpecRegistry.project_id) == project_id,
            ),
            col(SpecAuthorityAcceptance.status) == "accepted",
        )
    ).first()
    if accepted_authority_id is not None:
        raise ProjectDeletionConflictError(
            project_id=project_id,
            references=("spec_authority_acceptance.status",),
        )


def _neutralize_surviving_spec_pins(session: Session, project_id: int) -> None:
    """Clear nullable story pins to spec versions that will be deleted."""
    spec_version_ids = list(
        session.exec(
            select(SpecRegistry.spec_version_id).where(
                SpecRegistry.project_id == project_id
            )
        ).all()
    )
    if not spec_version_ids:
        return
    session.exec(
        update(UserStory)
        .where(
            col(UserStory.project_id) != project_id,
            col(UserStory.accepted_spec_version_id).in_(spec_version_ids),
        )
        .values(accepted_spec_version_id=None)
    )


def _delete_project_spec_rows(session: Session, project_id: int) -> None:
    """Delete the cyclic candidate/registry pair in foreign-key-safe order."""
    _neutralize_surviving_spec_pins(session, project_id)
    spec_versions = session.exec(
        select(SpecRegistry).where(SpecRegistry.project_id == project_id)
    ).all()
    spec_version_ids = [
        spec_version.spec_version_id
        for spec_version in spec_versions
        if spec_version.spec_version_id is not None
    ]
    if spec_version_ids:
        session.exec(
            delete(SpecAuthorityAcceptance).where(
                col(SpecAuthorityAcceptance.spec_version_id).in_(spec_version_ids)
            )
        )
    session.exec(
        delete(SpecificationDecision).where(
            col(SpecificationDecision.project_id) == project_id
        )
    )
    for spec_version in spec_versions:
        for compiled in session.exec(
            select(CompiledSpecAuthority).where(
                CompiledSpecAuthority.spec_version_id == spec_version.spec_version_id
            )
        ).all():
            session.delete(compiled)
    session.flush()
    session.exec(delete(SpecRegistry).where(col(SpecRegistry.project_id) == project_id))
    session.flush()
    session.exec(
        delete(SpecificationCandidate).where(
            col(SpecificationCandidate.project_id) == project_id
        )
    )
    session.flush()


def _delete_project_story_dependencies(
    session: Session,
    *,
    project_id: int,
    story_ids: list[int],
) -> None:
    """Delete dependency associations owned by or linked to the project."""
    statement = delete(UserStoryDependency)
    if story_ids:
        statement = statement.where(
            or_(
                col(UserStoryDependency.project_id) == project_id,
                col(UserStoryDependency.dependent_story_id).in_(story_ids),
                col(UserStoryDependency.prerequisite_story_id).in_(story_ids),
            )
        )
    else:
        statement = statement.where(col(UserStoryDependency.project_id) == project_id)
    session.exec(statement)


def _delete_project_curation_rows(session: Session, project_id: int) -> None:
    """Delete authority curation rows that directly reference a project."""
    session.exec(
        delete(AuthorityCurationAttempt).where(
            col(AuthorityCurationAttempt.project_id) == project_id
        )
    )
    session.exec(
        delete(AuthorityFeedbackAttempt).where(
            col(AuthorityFeedbackAttempt.project_id) == project_id
        )
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


def _delete_project_acceptances(session: Session, project_id: int) -> None:
    """Delete non-accepted authority rows owned by the project."""
    rows = session.exec(
        select(SpecAuthorityAcceptance).where(
            SpecAuthorityAcceptance.project_id == project_id
        )
    ).all()
    for row in rows:
        session.delete(row)
    session.flush()


def _delete_project_personas(session: Session, project_id: int) -> None:
    """Delete personas owned by the project."""
    rows = session.exec(
        select(ProjectPersona).where(ProjectPersona.project_id == project_id)
    ).all()
    for row in rows:
        session.delete(row)


def _delete_project_sprints(session: Session, sprints: list[Sprint]) -> None:
    """Delete sprint-story links before their sprints."""
    for sprint in sprints:
        session.exec(
            delete(SprintStory).where(col(SprintStory.sprint_id) == sprint.sprint_id)
        )
        session.delete(sprint)


def _delete_project_stories(
    session: Session,
    *,
    project_id: int,
    stories: list[UserStory],
) -> None:
    """Delete project stories and dependent task/completion rows."""
    story_ids = [story.story_id for story in stories if story.story_id is not None]
    _delete_project_story_dependencies(
        session,
        project_id=project_id,
        story_ids=story_ids,
    )
    if story_ids:
        referring_stories = session.exec(
            select(UserStory).where(
                col(UserStory.superseded_by_story_id).in_(story_ids)
            )
        ).all()
        for story in referring_stories:
            story.superseded_by_story_id = None
            session.add(story)
        session.flush()

    for story in stories:
        tasks = session.exec(select(Task).where(Task.story_id == story.story_id)).all()
        for task in tasks:
            session.delete(task)
        logs = session.exec(
            select(StoryCompletionLog).where(
                StoryCompletionLog.story_id == story.story_id
            )
        ).all()
        for log in logs:
            session.delete(log)
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

            _ensure_project_authority_deletable(session, project_id)
            _delete_project_spec_rows(session, project_id)
            _delete_project_lifecycle_rows(session, project)

            sprints = session.exec(
                select(Sprint).where(Sprint.project_id == project_id)
            ).all()
            sprint_ids = [
                sprint.sprint_id for sprint in sprints if sprint.sprint_id is not None
            ]
            _delete_project_workflow_events(
                session,
                project_id=project_id,
                sprint_ids=sprint_ids,
            )

            _delete_project_acceptances(session, project_id)

            _delete_project_curation_rows(session, project_id)

            _delete_project_personas(session, project_id)

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

            _delete_task_execution_logs(
                session,
                task_ids=task_ids,
                sprint_ids=sprint_ids,
            )

            _delete_project_sprints(session, list(sprints))
            _delete_project_stories(
                session,
                project_id=project_id,
                stories=list(stories),
            )
            session.flush()

            _delete_project_roadmap(session, project_id)
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
