import logging

from sqlalchemy import delete, or_, update
from sqlmodel import Session, col, select

from models.agent_workbench import (
    DiscoveryChallengeArtifact,
    DiscoveryPrd,
    DiscoverySpecAmendmentDraft,
    GreenfieldDiscoveryChallengeArtifact,
    GreenfieldDiscoveryContext,
    GreenfieldDiscoveryPrd,
    GreenfieldDiscoverySpecAmendmentDraft,
)
from models.authority_curation import (
    AuthorityCurationAttempt,
    AuthorityFeedbackAttempt,
)
from models.brownfield import (
    BrownfieldScanAttempt,
    BrownfieldSourceArtifact,
    BrownfieldSpecApproval,
    BrownfieldSpecDraftAttempt,
)
from models.core import (
    Epic,
    Feature,
    Product,
    ProductPersona,
    ProductTeam,
    Sprint,
    SprintStory,
    Task,
    Theme,
    UserStory,
    UserStoryDependency,
)
from models.db import get_engine
from models.events import StoryCompletionLog, TaskExecutionLog, WorkflowEvent
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry

logger = logging.getLogger(__name__)


def _delete_project_curation_rows(session: Session, product_id: int) -> None:
    """Delete authority curation rows that directly reference a project."""
    session.exec(
        delete(AuthorityCurationAttempt).where(
            col(AuthorityCurationAttempt.project_id) == product_id
        )
    )
    session.exec(
        delete(AuthorityFeedbackAttempt).where(
            col(AuthorityFeedbackAttempt.project_id) == product_id
        )
    )


def _delete_project_discovery_rows(session: Session, product_id: int) -> None:
    """Delete discovery artifact chains that reference a project."""
    prd_ids = list(
        session.exec(
            select(DiscoveryPrd.prd_id).where(DiscoveryPrd.project_id == product_id)
        ).all()
    )
    session.exec(
        delete(DiscoverySpecAmendmentDraft).where(
            col(DiscoverySpecAmendmentDraft.project_id) == product_id
        )
    )
    if prd_ids:
        session.exec(
            update(DiscoveryPrd)
            .where(col(DiscoveryPrd.supersedes_prd_id).in_(prd_ids))
            .values(supersedes_prd_id=None)
        )
    session.exec(delete(DiscoveryPrd).where(col(DiscoveryPrd.project_id) == product_id))
    session.exec(
        delete(DiscoveryChallengeArtifact).where(
            col(DiscoveryChallengeArtifact.project_id) == product_id
        )
    )

    greenfield_context_ids = list(
        session.exec(
            select(GreenfieldDiscoveryContext.greenfield_context_id).where(
                GreenfieldDiscoveryContext.project_id == product_id
            )
        ).all()
    )
    if not greenfield_context_ids:
        return

    session.exec(
        delete(GreenfieldDiscoverySpecAmendmentDraft).where(
            col(GreenfieldDiscoverySpecAmendmentDraft.greenfield_context_id).in_(
                greenfield_context_ids
            )
        )
    )
    session.exec(
        delete(GreenfieldDiscoveryPrd).where(
            col(GreenfieldDiscoveryPrd.greenfield_context_id).in_(
                greenfield_context_ids
            )
        )
    )
    session.exec(
        delete(GreenfieldDiscoveryChallengeArtifact).where(
            col(GreenfieldDiscoveryChallengeArtifact.greenfield_context_id).in_(
                greenfield_context_ids
            )
        )
    )
    session.exec(
        delete(GreenfieldDiscoveryContext).where(
            col(GreenfieldDiscoveryContext.greenfield_context_id).in_(
                greenfield_context_ids
            )
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
    product_id: int,
    sprint_ids: list[int],
) -> None:
    """Delete events linked directly to the project or through its sprints."""
    statement = delete(WorkflowEvent)
    if sprint_ids:
        statement = statement.where(
            or_(
                col(WorkflowEvent.product_id) == product_id,
                col(WorkflowEvent.sprint_id).in_(sprint_ids),
            )
        )
    else:
        statement = statement.where(col(WorkflowEvent.product_id) == product_id)
    session.exec(statement)


class ProductRepository:
    """Repository handling database operations for the Product entity."""

    def __init__(self, session: Session | None = None):
        # Allow passing an explicit session (for transactions),
        # otherwise create one and close it immediately per call.
        self._session = session

    def _get_session(self) -> Session:
        return self._session if self._session else Session(get_engine())

    def get_all(self) -> list[Product]:
        """Fetch all products."""
        with self._get_session() as session:
            statement = select(Product)
            return list(session.exec(statement).all())

    def get_by_id(self, product_id: int) -> Product | None:
        """Fetch a specific product by its ID."""
        with self._get_session() as session:
            return session.get(Product, product_id)

    def create(self, name: str, description: str | None = None) -> Product:
        """Create a new product."""
        product = Product(name=name, description=description)
        # We must manage the transaction locally if we spawned the session
        session = self._get_session()
        try:
            session.add(product)
            session.commit()
            session.refresh(product)
            return product
        finally:
            if not self._session:
                session.close()

    def update_vision(self, product_id: int, vision: str) -> Product | None:
        """Update the vision text for a product."""
        session = self._get_session()
        try:
            product = session.get(Product, product_id)
            if product:
                product.vision = vision
                session.add(product)
                session.commit()
                session.refresh(product)
            return product
        finally:
            if not self._session:
                session.close()

    def update_technical_spec(
        self, product_id: int, technical_spec: str
    ) -> Product | None:
        """Update the raw technical spec for a product."""
        session = self._get_session()
        try:
            product = session.get(Product, product_id)
            if product:
                product.technical_spec = technical_spec
                session.add(product)
                session.commit()
                session.refresh(product)
            return product
        finally:
            if not self._session:
                session.close()

    def update_compiled_authority(
        self, product_id: int, compiled_json: str
    ) -> Product | None:
        """Update the compiled authority JSON for a product."""
        session = self._get_session()
        try:
            product = session.get(Product, product_id)
            if product:
                product.compiled_authority_json = compiled_json
                session.add(product)
                session.commit()
                session.refresh(product)
        finally:
            if not self._session:
                session.close()

    def delete_project(self, product_id: int) -> bool:
        """Fully delete a product and all of its associated agile entities."""
        session = self._get_session()
        try:
            product = session.get(Product, product_id)
            if not product:
                return False

            sprints = session.exec(
                select(Sprint).where(Sprint.product_id == product_id)
            ).all()
            sprint_ids = [
                sprint.sprint_id for sprint in sprints if sprint.sprint_id is not None
            ]
            _delete_project_workflow_events(
                session,
                product_id=product_id,
                sprint_ids=sprint_ids,
            )

            # Delete SpecAuthorityAcceptance records
            for sa in session.exec(
                select(SpecAuthorityAcceptance).where(
                    SpecAuthorityAcceptance.product_id == product_id
                )
            ).all():
                session.delete(sa)
            session.flush()

            # Delete Brownfield curation artifacts
            for approval in session.exec(
                select(BrownfieldSpecApproval).where(
                    BrownfieldSpecApproval.project_id == product_id
                )
            ).all():
                session.delete(approval)
            for draft_attempt in session.exec(
                select(BrownfieldSpecDraftAttempt).where(
                    BrownfieldSpecDraftAttempt.project_id == product_id
                )
            ).all():
                session.delete(draft_attempt)
            for scan_attempt in session.exec(
                select(BrownfieldScanAttempt).where(
                    BrownfieldScanAttempt.project_id == product_id
                )
            ).all():
                session.delete(scan_attempt)
            for source_artifact in session.exec(
                select(BrownfieldSourceArtifact).where(
                    BrownfieldSourceArtifact.project_id == product_id
                )
            ).all():
                session.delete(source_artifact)

            _delete_project_curation_rows(session, product_id)
            _delete_project_discovery_rows(session, product_id)

            # Delete ProductPersonas
            for persona in session.exec(
                select(ProductPersona).where(ProductPersona.product_id == product_id)
            ).all():
                session.delete(persona)

            stories = session.exec(
                select(UserStory).where(UserStory.product_id == product_id)
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

            # Handle Sprints (and mappings)
            for sprint in sprints:
                session.exec(
                    delete(SprintStory).where(
                        col(SprintStory.sprint_id) == sprint.sprint_id
                    )
                )
                session.delete(sprint)

            # Handle UserStories (and dependencies / tasks / logs)
            if story_ids:
                for dependency in session.exec(
                    select(UserStoryDependency).where(
                        or_(
                            col(UserStoryDependency.dependent_story_id).in_(story_ids),
                            col(UserStoryDependency.prerequisite_story_id).in_(
                                story_ids
                            ),
                        )
                    )
                ).all():
                    session.delete(dependency)
                for referring_story in session.exec(
                    select(UserStory).where(
                        col(UserStory.superseded_by_story_id).in_(story_ids)
                    )
                ).all():
                    referring_story.superseded_by_story_id = None
                    session.add(referring_story)
                session.flush()

            for story in stories:
                for t in session.exec(
                    select(Task).where(Task.story_id == story.story_id)
                ).all():
                    session.delete(t)
                for log in session.exec(
                    select(StoryCompletionLog).where(
                        StoryCompletionLog.story_id == story.story_id
                    )
                ).all():
                    session.delete(log)
                session.delete(story)

            # Stories may be pinned to spec versions.
            session.flush()

            # Delete every compiled authority row before its spec version.
            for spec_ver in session.exec(
                select(SpecRegistry).where(SpecRegistry.product_id == product_id)
            ).all():
                for compiled in session.exec(
                    select(CompiledSpecAuthority).where(
                        CompiledSpecAuthority.spec_version_id
                        == spec_ver.spec_version_id
                    )
                ).all():
                    session.delete(compiled)
                session.delete(spec_ver)
            session.flush()

            # Handle Themes -> Epics -> Features
            for theme in session.exec(
                select(Theme).where(Theme.product_id == product_id)
            ).all():
                for epic in session.exec(
                    select(Epic).where(Epic.theme_id == theme.theme_id)
                ).all():
                    for feature in session.exec(
                        select(Feature).where(Feature.epic_id == epic.epic_id)
                    ).all():
                        session.delete(feature)
                    session.delete(epic)
                session.delete(theme)

            # Handle Teams Mappings
            for pt in session.exec(
                select(ProductTeam).where(ProductTeam.product_id == product_id)
            ).all():
                session.delete(pt)

            # Finally delete the product
            session.delete(product)

            session.commit()
        except Exception:
            session.rollback()
            raise
        else:
            return True
        finally:
            if not self._session:
                session.close()
