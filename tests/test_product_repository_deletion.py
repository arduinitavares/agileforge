"""Product repository deletion tests."""

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from models.core import Product, UserStory
from models.specs import (
    CompiledSpecAuthority,
    SpecAuthorityAcceptance,
    SpecRegistry,
)
from repositories.product import ProductRepository


def test_delete_project_removes_authority_history_and_pinned_story(
    engine: Engine,
) -> None:
    """Delete all retained authority rows after their dependent records."""
    with Session(engine) as session:
        assert (
            session.connection().exec_driver_sql("PRAGMA foreign_keys").scalar_one()
            == 1
        )

        product = Product(name="Authority history")
        session.add(product)
        session.flush()
        assert product.product_id is not None
        product_id = product.product_id

        spec = SpecRegistry(
            product_id=product_id,
            spec_hash="spec-hash",
            content="# Approved spec",
            status="approved",
        )
        session.add(spec)
        session.flush()
        assert spec.spec_version_id is not None
        spec_version_id = spec.spec_version_id

        retained_v2 = CompiledSpecAuthority(
            spec_version_id=spec_version_id,
            compiler_version="2.0.0",
            prompt_hash="v2-prompt",
            scope_themes="[]",
            invariants="[]",
            eligible_feature_ids="[]",
        )
        current_v3 = CompiledSpecAuthority(
            spec_version_id=spec_version_id,
            compiler_version="3.0.0",
            prompt_hash="v3-prompt",
            scope_themes="[]",
            invariants="[]",
            eligible_feature_ids="[]",
        )
        session.add(retained_v2)
        session.add(current_v3)
        session.flush()
        assert retained_v2.authority_id is not None
        assert current_v3.authority_id is not None
        authority_ids = {retained_v2.authority_id, current_v3.authority_id}

        acceptance = SpecAuthorityAcceptance(
            product_id=product_id,
            spec_version_id=spec_version_id,
            status="accepted",
            policy="test",
            decided_by="test",
            compiler_version=current_v3.compiler_version,
            prompt_hash=current_v3.prompt_hash,
            spec_hash=spec.spec_hash,
            pending_authority_id=current_v3.authority_id,
        )
        story = UserStory(
            title="Pinned story",
            product_id=product_id,
            accepted_spec_version_id=spec_version_id,
        )
        session.add(acceptance)
        session.add(story)
        session.commit()
        assert acceptance.id is not None
        acceptance_id = acceptance.id
        assert story.story_id is not None
        story_id = story.story_id

        session.expire_all()
        stored_spec = session.get(SpecRegistry, spec_version_id)
        assert stored_spec is not None
        assert len(stored_spec.compiled_authority) == len(authority_ids)

        assert ProductRepository(session).delete_project(product_id) is True

        assert session.get(Product, product_id) is None
        assert session.get(UserStory, story_id) is None
        assert session.get(SpecAuthorityAcceptance, acceptance_id) is None
        assert session.get(SpecRegistry, spec_version_id) is None
        remaining_authority_ids = set(
            session.exec(
                select(CompiledSpecAuthority.authority_id).where(
                    col(CompiledSpecAuthority.authority_id).in_(authority_ids)
                )
            ).all()
        )
        assert remaining_authority_ids == set()


def test_delete_project_rolls_back_when_commit_fails(engine: Engine) -> None:
    """Leave persisted project data intact when the transaction cannot commit."""
    with Session(engine) as session:
        product = Product(name="Rollback project")
        session.add(product)
        session.commit()
        assert product.product_id is not None
        product_id = product.product_id

        def fail_commit(_session: Session) -> None:
            msg = "injected commit failure"
            raise RuntimeError(msg)

        event.listen(session, "before_commit", fail_commit)
        try:
            with pytest.raises(RuntimeError, match="injected commit failure"):
                ProductRepository(session).delete_project(product_id)
        finally:
            event.remove(session, "before_commit", fail_commit)

        assert session.in_transaction() is False
        assert session.get(Product, product_id) is not None
