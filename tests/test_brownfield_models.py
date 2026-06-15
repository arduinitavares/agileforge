"""Tests for brownfield SQLModel persistence models."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from agile_sqlmodel import BrownfieldSourceArtifact, Product

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def test_source_attempt_is_unique_per_project(engine: Engine) -> None:
    """Verify source artifact attempt IDs are unique per project."""
    with Session(engine) as session:
        product = Product(name="Brownfield Product")
        session.add(product)
        session.commit()
        session.refresh(product)

        first_artifact = BrownfieldSourceArtifact(
            project_id=product.product_id,
            attempt_id="source-attempt-1",
            artifact_fingerprint="artifact-fingerprint-1",
            request_hash="request-hash-1",
        )
        session.add(first_artifact)
        session.commit()

        duplicate_artifact = BrownfieldSourceArtifact(
            project_id=product.product_id,
            attempt_id="source-attempt-1",
            artifact_fingerprint="artifact-fingerprint-2",
            request_hash="request-hash-2",
        )
        session.add(duplicate_artifact)

        with pytest.raises(IntegrityError):
            session.commit()
