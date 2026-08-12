"""Host preparation for direct Specification authoring."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlmodel import Session

from models.core import Project
from services.contracts.specification_authoring import SpecificationAuthoringInput
from services.specification_authoring_input import SpecificationAuthoringInputService
from tests.workflow.lifecycle_fixtures import _seed_accepted_vision_and_goal
from workflow.contracts import (
    FactReference,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
)
from workflow.fingerprints import canonical_hash

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def _decision(
    vision_id: int,
    vision_fp: str,
    goal_id: int,
    goal_fp: str,
) -> NodeDecision:
    return NodeDecision(
        node_id="specification.author",
        child_graph_id="product_discovery",
        request_kind="author_specification",
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="SPECIFICATION_INITIAL_REQUIRED",
        fact_references=(
            FactReference(
                fact_type="vision",
                fact_id=str(vision_id),
                fingerprint=vision_fp,
            ),
            FactReference(
                fact_type="product_goal",
                fact_id=str(goal_id),
                fingerprint=goal_fp,
            ),
        ),
        decision_fingerprint=canonical_hash({"decision": "author"}),
    )


def _seed(engine: Engine) -> tuple[int, str, int, str]:
    with Session(engine) as session:
        project = Project(name="Specification authoring input")
        session.add(project)
        session.flush()
        assert project.project_id is not None
        vision, goal = _seed_accepted_vision_and_goal(
            session,
            project_id=project.project_id,
            recorded_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
        )
        session.commit()
        assert vision.vision_artifact_id is not None
        assert goal.product_goal_artifact_id is not None
        return (
            vision.vision_artifact_id,
            vision.content_fingerprint,
            goal.product_goal_artifact_id,
            goal.content_fingerprint,
        )


def test_builds_initial_input_from_exact_accepted_lineage(engine: Engine) -> None:
    """The model receives complete durable source context, not caller JSON."""
    vision_id, vision_fp, goal_id, goal_fp = _seed(engine)

    raw = SpecificationAuthoringInputService(engine=engine).build(
        project_id=1,
        decision=_decision(vision_id, vision_fp, goal_id, goal_fp),
    )
    result = SpecificationAuthoringInput.model_validate(raw)

    assert result.operation == "initial"
    assert result.accepted_vision.artifact_id == vision_id
    assert result.accepted_product_goal.artifact_id == goal_id
    assert result.base_specification is None
    assert result.prior_candidate is None
    assert {(item.kind.value, item.fingerprint) for item in result.source_manifest} >= {
        ("vision", vision_fp),
        ("product_goal", goal_fp),
    }
    assert all(item.source_id.startswith("SRC.") for item in result.source_manifest)
    assert {item.source_id for item in result.source_context} == {
        item.source_id for item in result.source_manifest
    }


def test_rejects_decision_with_stale_goal_fingerprint(engine: Engine) -> None:
    """A graph reference mismatch fails before a durable provider attempt starts."""
    vision_id, vision_fp, goal_id, goal_fp = _seed(engine)
    stale = _decision(vision_id, vision_fp, goal_id, goal_fp).model_copy(
        update={
            "fact_references": (
                FactReference(
                    fact_type="vision",
                    fact_id=str(vision_id),
                    fingerprint=vision_fp,
                ),
                FactReference(
                    fact_type="product_goal",
                    fact_id=str(goal_id),
                    fingerprint="sha256:" + ("f" * 64),
                ),
            )
        }
    )

    with pytest.raises(ValueError, match="accepted Vision and Product Goal"):
        SpecificationAuthoringInputService(engine=engine).build(
            project_id=1,
            decision=stale,
        )
