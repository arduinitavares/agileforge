"""Product repository deletion tests."""

from dataclasses import dataclass

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine
from sqlmodel import Session, col, select

from models.core import Product, UserStory, UserStoryDependency
from models.specs import (
    CompiledSpecAuthority,
    SpecAuthorityAcceptance,
    SpecRegistry,
)
from repositories.product import ProductRepository


@dataclass(frozen=True)
class _SeededAuthorityProject:
    product_id: int
    spec_version_id: int
    authority_ids: frozenset[int]
    acceptance_id: int
    story_ids: tuple[int, int]
    dependency_id: int


def _seed_authority_project(
    session: Session,
    *,
    name: str,
) -> _SeededAuthorityProject:
    product = Product(name=name)
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
    current_story = UserStory(
        title="Current pinned story",
        product_id=product_id,
        accepted_spec_version_id=spec_version_id,
    )
    superseded_story = UserStory(
        title="Superseded pinned story",
        product_id=product_id,
        accepted_spec_version_id=spec_version_id,
    )
    session.add(acceptance)
    session.add(current_story)
    session.add(superseded_story)
    session.flush()
    assert acceptance.id is not None
    assert current_story.story_id is not None
    assert superseded_story.story_id is not None

    superseded_story.superseded_by_story_id = current_story.story_id
    dependency = UserStoryDependency(
        product_id=product_id,
        dependent_story_id=current_story.story_id,
        prerequisite_story_id=superseded_story.story_id,
        status="active",
        source="manual_review",
        confidence="reviewed",
    )
    session.add(superseded_story)
    session.add(dependency)
    session.commit()
    assert dependency.dependency_id is not None

    return _SeededAuthorityProject(
        product_id=product_id,
        spec_version_id=spec_version_id,
        authority_ids=frozenset({retained_v2.authority_id, current_v3.authority_id}),
        acceptance_id=acceptance.id,
        story_ids=(current_story.story_id, superseded_story.story_id),
        dependency_id=dependency.dependency_id,
    )


def test_delete_project_removes_authority_history_and_pinned_story(
    engine: Engine,
) -> None:
    """Delete all retained authority rows after their dependent records."""
    with Session(engine) as session:
        assert (
            session.connection().exec_driver_sql("PRAGMA foreign_keys").scalar_one()
            == 1
        )

        seeded = _seed_authority_project(session, name="Authority history")

        session.expire_all()
        stored_spec = session.get(SpecRegistry, seeded.spec_version_id)
        assert stored_spec is not None
        assert len(stored_spec.compiled_authority) == len(seeded.authority_ids)

        assert ProductRepository(session).delete_project(seeded.product_id) is True

        assert session.get(Product, seeded.product_id) is None
        for story_id in seeded.story_ids:
            assert session.get(UserStory, story_id) is None
        assert session.get(UserStoryDependency, seeded.dependency_id) is None
        assert session.get(SpecAuthorityAcceptance, seeded.acceptance_id) is None
        assert session.get(SpecRegistry, seeded.spec_version_id) is None
        remaining_authority_ids = set(
            session.exec(
                select(CompiledSpecAuthority.authority_id).where(
                    col(CompiledSpecAuthority.authority_id).in_(seeded.authority_ids)
                )
            ).all()
        )
        assert remaining_authority_ids == set()


def test_delete_project_neutralizes_external_story_self_reference(
    engine: Engine,
) -> None:
    """Preserve an outside story after deleting the story it referenced."""
    with Session(engine) as session:
        deleted_product = Product(name="Deleted project")
        surviving_product = Product(name="Surviving project")
        session.add(deleted_product)
        session.add(surviving_product)
        session.flush()
        assert deleted_product.product_id is not None
        assert surviving_product.product_id is not None

        deleted_story = UserStory(
            title="Deleted story",
            product_id=deleted_product.product_id,
        )
        surviving_story = UserStory(
            title="Surviving story",
            product_id=surviving_product.product_id,
        )
        session.add(deleted_story)
        session.add(surviving_story)
        session.flush()
        assert deleted_story.story_id is not None
        assert surviving_story.story_id is not None
        surviving_story.superseded_by_story_id = deleted_story.story_id
        cross_project_dependency = UserStoryDependency(
            product_id=surviving_product.product_id,
            dependent_story_id=surviving_story.story_id,
            prerequisite_story_id=deleted_story.story_id,
            status="active",
            source="manual_review",
            confidence="reviewed",
        )
        session.add(surviving_story)
        session.add(cross_project_dependency)
        session.commit()
        assert cross_project_dependency.dependency_id is not None

        assert (
            ProductRepository(session).delete_project(deleted_product.product_id)
            is True
        )

        stored_survivor = session.get(UserStory, surviving_story.story_id)
        assert stored_survivor is not None
        assert stored_survivor.superseded_by_story_id is None
        assert (
            session.get(
                UserStoryDependency,
                cross_project_dependency.dependency_id,
            )
            is None
        )


def test_delete_project_rolls_back_when_commit_fails(engine: Engine) -> None:
    """Leave persisted project data intact when the transaction cannot commit."""
    with Session(engine) as session:
        seeded = _seed_authority_project(session, name="Rollback project")
        dml_statements: list[str] = []

        def fail_commit(_session: Session) -> None:
            msg = "injected commit failure"
            raise RuntimeError(msg)

        def capture_dml(
            _connection: Connection,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            operation = statement.lstrip().partition(" ")[0].upper()
            if operation in {"DELETE", "UPDATE"}:
                dml_statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture_dml)
        event.listen(session, "before_commit", fail_commit)
        try:
            with pytest.raises(RuntimeError, match="injected commit failure"):
                ProductRepository(session).delete_project(seeded.product_id)
        finally:
            event.remove(session, "before_commit", fail_commit)
            event.remove(engine, "before_cursor_execute", capture_dml)

        assert dml_statements
        assert session.in_transaction() is False
        assert session.get(Product, seeded.product_id) is not None
        stored_spec = session.get(SpecRegistry, seeded.spec_version_id)
        assert stored_spec is not None
        assert len(stored_spec.compiled_authority) == len(seeded.authority_ids)
        assert session.get(SpecAuthorityAcceptance, seeded.acceptance_id) is not None
        for story_id in seeded.story_ids:
            assert session.get(UserStory, story_id) is not None
        stored_superseded_story = session.get(UserStory, seeded.story_ids[1])
        assert stored_superseded_story is not None
        assert stored_superseded_story.superseded_by_story_id == seeded.story_ids[0]
        assert session.get(UserStoryDependency, seeded.dependency_id) is not None
