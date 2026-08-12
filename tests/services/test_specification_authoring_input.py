"""Host preparation for direct Specification authoring."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from git import Repo
from sqlmodel import Session

from adapters.git.repository_probe import GitPythonRepositoryProbe
from models.core import Project
from models.repository import RepositoryBinding
from services.contracts.specification_authoring import (
    SPECIFICATION_PRODUCT_GOAL_SOURCE_ID,
    SPECIFICATION_VISION_SOURCE_ID,
    SpecificationAuthoringInput,
)
from services.specification_authoring_input import SpecificationAuthoringInputService
from tests.workflow.lifecycle_fixtures import _seed_accepted_vision_and_goal
from workflow.contracts import (
    FactReference,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
    WorkflowErrorCode,
)
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine


def _decision(
    vision_id: int,
    vision_fp: str,
    goal_id: int,
    goal_fp: str,
) -> NodeDecision:
    return NodeDecision(
        node_id="specification.author",
        child_graph_id="specification",
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
    assert {
        item.source_id
        for item in result.source_manifest
        if item.kind.value in {"vision", "product_goal"}
    } == {
        SPECIFICATION_VISION_SOURCE_ID,
        SPECIFICATION_PRODUCT_GOAL_SOURCE_ID,
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


def test_collects_bounded_repository_context_created_after_goal(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Post-Goal research can reach to-spec through the approved source surface."""
    vision_id, vision_fp, goal_id, goal_fp = _seed(engine)
    repository = tmp_path / "post-goal-source"
    repository.mkdir()
    with Repo.init(repository) as repo:
        with repo.config_writer() as config:
            config.set_value("user", "name", "Specification Source Test")
            config.set_value("user", "email", "source@example.com")
        (repository / "CONTEXT.md").write_text(
            "Post-Goal grill result: retries must remain idempotent.\n",
            encoding="utf-8",
        )
        repo.index.add(["CONTEXT.md"])
        repo.index.commit("record post-goal source")
    (repository / "CONTEXT.md").write_text(
        "Post-Goal grill result: retries must remain idempotent and auditable.\n",
        encoding="utf-8",
    )
    probe = GitPythonRepositoryProbe()
    observed = probe.inspect(repository)
    with Session(engine) as session:
        binding = RepositoryBinding(
            project_id=1,
            worktree_path=observed.worktree_path,
            common_git_dir=observed.common_git_dir,
            head_sha=observed.head_sha,
            branch_name=observed.branch_name,
            detached_head=observed.detached_head,
            dirty=observed.dirty,
            status_fingerprint=observed.status_fingerprint,
            status_entries_json=canonical_json(
                [item.model_dump(mode="json") for item in observed.status_entries]
            ),
            remotes_json=canonical_json(list(observed.remotes)),
            warnings_json=canonical_json(
                [item.model_dump(mode="json") for item in observed.warnings]
            ),
            probe_version=observed.probe_version,
            inspected_at=observed.inspected_at,
            recorded_by="source-test",
        )
        session.add(binding)
        session.flush()
        project = session.get(Project, 1)
        assert project is not None
        project.active_repository_binding_id = binding.repository_binding_id
        session.add(project)
        session.commit()

    service = SpecificationAuthoringInputService(
        engine=engine,
        repository_probe=probe,
    )
    raw = service.build(
        project_id=1,
        decision=_decision(vision_id, vision_fp, goal_id, goal_fp),
    )
    result = SpecificationAuthoringInput.model_validate(raw)
    current = next(
        item
        for item in result.source_context
        if item.source_id == "SRC.repository-context.active"
    )

    assert "Post-Goal grill result" in json.dumps(current.content)
    manifest = next(
        item
        for item in result.source_manifest
        if item.source_id == current.source_id
    )
    assert manifest.fingerprint == current.fingerprint

    (repository / "CONTEXT.md").write_text(
        "Post-Goal grill result: retries may now duplicate writes.\n",
        encoding="utf-8",
    )
    assert probe.inspect(repository).status_fingerprint == observed.status_fingerprint

    stale = service.revalidate_sources(1, raw)

    assert stale is not None
    assert stale.code is WorkflowErrorCode.STALE_SPECIFICATION_INPUT
