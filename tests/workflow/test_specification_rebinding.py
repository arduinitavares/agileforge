"""Accepted requirements survive real Git checkout cycles without source writes."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from git import Repo
from sqlmodel import Session, select

from models.core import Project
from models.product_definition import SpecificationSource
from models.repository import RepositoryBinding, repository_binding_fingerprint
from repositories.workflow import WorkflowFactRepository
from services.specification_source_registration import (
    SpecificationSourceRegistrationError,
    SpecificationSourceRegistrationErrorCode,
    SpecificationSourceRegistrationService,
)
from tests.workflow.test_product_discovery_transitions import (
    NOW,
    _accept_request,
    _domain,
    _payload,
    _ready_project,
    _register_source,
    _repository,
    _structure,
)
from workflow.contracts import RecommendationKind
from workflow.definitions.product_discovery import (
    accepted_current_spec,
    current_specification_source,
)
from workflow.requests.project import RecordRepositoryBinding, RepositoryBindingInput

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from services.repository_probe import RepositoryProbe
    from workflow.domain import WorkflowDomain


def _attach(
    engine: Engine,
    domain: WorkflowDomain,
    project_id: int,
    path: Path,
    probe: RepositoryProbe,
) -> None:
    """Exercise the real append-only repository transition and its exact guards."""
    with Session(engine) as session:
        project = session.get(Project, project_id)
        assert project is not None
        prior = session.get(RepositoryBinding, project.active_repository_binding_id)
        assert prior is not None
        fingerprint = repository_binding_fingerprint(prior)
    position = domain.position(project_id)
    result = domain.transition(
        RecordRepositoryBinding(
            project_id=project_id,
            operation="attach",
            requested_repository_path=str(path),
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            expected_active_binding_fingerprint=fingerprint,
            binding=RepositoryBindingInput.from_probe(
                probe.inspect(path).model_copy(
                    update={"inspected_at": position.evaluated_at}
                ),
                recorded_by="test",
            ),
            idempotency_key=f"attach-{fingerprint}-{path.name}",
            actor="test",
        )
    )
    assert result.ok, result.error


def test_accepted_specification_survives_worktree_merge_and_next_checkout(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """Branch and commit changes leave the exact accepted rows and bytes intact."""
    project_id, _, _, _, _, repository, probe = _ready_project(
        engine, tmp_path, name="issue269"
    )
    domain = _domain(engine)
    structured = _structure(
        engine, domain, project_id=project_id, payload=_payload(), key="initial"
    )
    assert structured.ok
    domain = _domain(engine, at=NOW + timedelta(seconds=1))
    accepted = domain.transition(
        _accept_request(domain, project_id=project_id, key="accept")
    )
    assert accepted.ok
    with Session(engine) as session:
        initial = WorkflowFactRepository(session).load(project_id)
        spec = accepted_current_spec(initial)
        sources = [
            item.model_dump()
            for item in session.exec(select(SpecificationSource)).all()
        ]
    assert spec is not None

    worktree = tmp_path / "sprint-one"
    next_worktree = tmp_path / "sprint-two"
    with Repo(repository) as repo:
        main_branch = repo.active_branch.name
        repo.git.worktree("add", "-b", "sprint-one", str(worktree))
        _attach(engine, domain, project_id, worktree, probe)
        _assert_accepted_rebinding(
            engine, domain, project_id, spec.spec_version_id, sources
        )
        _commit_implementation(worktree)
        repo.git.merge("--ff-only", "sprint-one")
        assert repo.active_branch.name == main_branch
        _attach(engine, domain, project_id, repository, probe)
        _assert_accepted_rebinding(
            engine, domain, project_id, spec.spec_version_id, sources
        )
        repo.git.worktree("add", "-b", "sprint-two", str(next_worktree))
        _attach(engine, domain, project_id, next_worktree, probe)
        _assert_accepted_rebinding(
            engine, domain, project_id, spec.spec_version_id, sources
        )
        assert (repository / "SPECIFICATION.md").read_bytes() == (
            next_worktree / "SPECIFICATION.md"
        ).read_bytes()

    # A different execution repository preserves accepted requirements.
    other = _repository(tmp_path, name="different-repository")
    _attach(engine, domain, project_id, other, probe)
    _assert_accepted_rebinding(
        engine, domain, project_id, spec.spec_version_id, sources
    )
    service = SpecificationSourceRegistrationService(
        engine=engine, repository_probe=probe
    )
    assert service.capability(project_id).available
    (other / "SPECIFICATION.md").write_text("Changed requirements\n", encoding="utf-8")
    with pytest.raises(SpecificationSourceRegistrationError) as stale:
        service.capability(project_id)
    assert (
        stale.value.code
        is SpecificationSourceRegistrationErrorCode.REPOSITORY_PROVENANCE_STALE
    )
    _assert_accepted_rebinding(
        engine, domain, project_id, spec.spec_version_id, sources
    )

    # Only explicit source capture after a fresh binding enables authoring a revision.
    _attach(engine, domain, project_id, other, probe)
    registered = _register_source(
        engine, domain, project_id=project_id, repository_probe=probe, key="revision"
    )
    assert registered.ok
    position = domain.position(project_id)
    assert any(
        item.request_kind == "structure_specification" for item in position.decisions
    )
    with Session(engine) as session:
        updated = WorkflowFactRepository(session).load(project_id)
    assert accepted_current_spec(updated) == spec
    assert current_specification_source(updated) is not None
    assert updated.specification_sources[0] == initial.specification_sources[0]


def _commit_implementation(worktree: Path) -> None:
    """Advance HEAD with implementation work while preserving Specification bytes."""
    (worktree / "implementation.txt").write_text(
        "Sprint implementation\n", encoding="utf-8"
    )
    with Repo(worktree) as implementation:
        implementation.index.add(["implementation.txt"])
        implementation.index.commit("deliver Sprint")


def _assert_accepted_rebinding(
    engine: Engine,
    domain: WorkflowDomain,
    project_id: int,
    spec_id: int,
    original_sources: list[dict[str, object]],
) -> None:
    """Check public action classification and immutable stored acceptance evidence."""
    position = domain.position(project_id)
    registration = next(
        item
        for item in position.decisions
        if item.request_kind == "register_specification_source"
    )
    assert registration.recommendation_kind is RecommendationKind.OPTIONAL_REENTRY
    assert registration.reason_code == "SPECIFICATION_SOURCE_REPLACEMENT_AVAILABLE"
    assert not any(
        item.request_kind == "structure_specification" for item in position.decisions
    )
    with Session(engine) as session:
        snapshot = WorkflowFactRepository(session).load(project_id)
        accepted = accepted_current_spec(snapshot)
        assert accepted is not None
        assert accepted.spec_version_id == spec_id
        assert current_specification_source(snapshot) is None
        assert [
            item.model_dump()
            for item in session.exec(select(SpecificationSource)).all()
        ] == original_sources
