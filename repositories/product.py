import logging

from sqlmodel import Session, select

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
)
from models.db import get_engine
from models.events import StoryCompletionLog, WorkflowEvent
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry

logger = logging.getLogger(__name__)


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

            # Delete WorkflowEvent records
            for event in session.exec(
                select(WorkflowEvent).where(WorkflowEvent.product_id == product_id)
            ).all():
                session.delete(event)

            # Delete SpecAuthorityAcceptance records
            session.exec(
                select(SpecAuthorityAcceptance).where(
                    SpecAuthorityAcceptance.product_id == product_id
                )
            ).all()
            for sa in session.exec(
                select(SpecAuthorityAcceptance).where(
                    SpecAuthorityAcceptance.product_id == product_id
                )
            ).all():
                session.delete(sa)

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

            # Delete SpecRegistry (+ CompiledSpecAuthority is 1:1, but child records might need manual drop depending on FKs)
            for spec_ver in session.exec(
                select(SpecRegistry).where(SpecRegistry.product_id == product_id)
            ).all():
                comp = session.exec(
                    select(CompiledSpecAuthority).where(
                        CompiledSpecAuthority.spec_version_id
                        == spec_ver.spec_version_id
                    )
                ).first()
                if comp:
                    session.delete(comp)
                session.delete(spec_ver)

            # Delete ProductPersonas
            for persona in session.exec(
                select(ProductPersona).where(ProductPersona.product_id == product_id)
            ).all():
                session.delete(persona)

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

            # Handle Sprints (and mappings)
            for sprint in session.exec(
                select(Sprint).where(Sprint.product_id == product_id)
            ).all():
                for sm in session.exec(
                    select(SprintStory).where(SprintStory.sprint_id == sprint.sprint_id)
                ).all():
                    session.delete(sm)
                session.delete(sprint)

            # Handle UserStories (and tasks / logs)
            for story in session.exec(
                select(UserStory).where(UserStory.product_id == product_id)
            ).all():
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

            # Handle Teams Mappings
            for pt in session.exec(
                select(ProductTeam).where(ProductTeam.product_id == product_id)
            ).all():
                session.delete(pt)

            # Finally delete the product
            session.delete(product)

            session.commit()
            return True
        finally:
            if not self._session:
                session.close()
