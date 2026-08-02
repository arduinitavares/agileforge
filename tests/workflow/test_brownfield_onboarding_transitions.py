"""Transactional brownfield onboarding transition tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError
from sqlmodel import Session, select

from models.core import Product
from models.specs import SpecRegistry
from models.workflow import RepositoryBaseline, RepositoryInventory, SpecDraft
from workflow import (
    DecideBrownfieldInitialSpec,
    OpenProjectShell,
    RecordBrownfieldSpecDraft,
    RecordRepositoryBaseline,
    RecordRepositoryInventory,
    RegisterInitialScope,
    WorkflowDomain,
)
from workflow.clock import FixedClock
from workflow.contracts import NodeCategory, NodeDecision, TransitionResult
from workflow.definitions.root import ROOT_GRAPH
from workflow.fingerprints import canonical_hash

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

EVALUATED_AT = datetime(2026, 8, 2, 17, tzinfo=UTC)
ACTOR = "operator@example.com"
REPOSITORY_PATH = "/evidence/brownfield"
COMMIT = "b" * 40
FILES = (
    {
        "path": ".env",
        "size_bytes": 8,
        "sha256": None,
        "content_status": "secret",
    },
    {
        "path": "README.md",
        "size_bytes": 12,
        "sha256": "sha256:readme",
        "content_status": "hashable",
    },
)
TOTAL_BYTES = 20


@pytest.fixture
def domain(engine: Engine) -> WorkflowDomain:
    """Build a deterministic workflow domain."""
    return WorkflowDomain(
        engine=engine,
        graph=ROOT_GRAPH,
        clock=FixedClock(now_value=EVALUATED_AT),
    )


def _required_output_id(result: TransitionResult, key: str) -> int:
    value = result.output.get(key)
    assert isinstance(value, int)
    return value


def _available_decision(result: TransitionResult, node_id: str) -> NodeDecision:
    assert result.position is not None
    decision = next(
        item for item in result.position.decisions if item.node_id == node_id
    )
    assert decision.category is NodeCategory.AVAILABLE
    return decision


def _guards(
    domain: WorkflowDomain,
    project_id: int,
    node_id: str,
    key: str,
) -> dict[str, object]:
    position = domain.position(project_id)
    decision = next(item for item in position.decisions if item.node_id == node_id)
    assert decision.category is NodeCategory.AVAILABLE
    return {
        "project_id": project_id,
        "graph_version": position.graph_version,
        "fact_fingerprint": position.fact_fingerprint,
        "decision_fingerprint": decision.decision_fingerprint,
        "idempotency_key": key,
        "actor": ACTOR,
        "correlation_id": "task-8",
        "instance_key": decision.instance_key,
    }


def _open_brownfield(domain: WorkflowDomain) -> int:
    result = domain.transition(
        OpenProjectShell(
            name="Existing Product",
            origin="brownfield",
            idempotency_key="open-brownfield",
            actor=ACTOR,
        )
    )
    assert result.ok is True
    return _required_output_id(result, "project_id")


def _baseline_fingerprint() -> str:
    return canonical_hash(
        {
            "repository_path": REPOSITORY_PATH,
            "git_commit": COMMIT,
            "dirty": False,
        }
    )


def _inventory_fingerprint() -> str:
    return canonical_hash({"files": list(FILES), "total_bytes": TOTAL_BYTES})


def _spec_content() -> dict[str, object]:
    return {
        "schema_version": "agileforge.spec.v1",
        "artifact_id": "SPEC.brownfield",
        "title": "Brownfield Initial Scope",
        "status": "draft",
        "version": "0.1",
        "created_at": "2026-08-02",
        "updated_at": "2026-08-02",
        "summary": "Reviewed initial scope derived from repository evidence.",
        "problem_statement": "Existing behavior needs explicit accepted authority.",
        "items": [
            {
                "id": "REQ.brownfield.001",
                "type": "REQ",
                "status": "proposed",
                "level": "MUST",
                "title": "Preserve observed behavior",
                "statement": "The system MUST preserve reviewed observed behavior.",
                "verification": "system-test",
                "acceptance": ["The reviewed behavior remains available."],
            }
        ],
        "relations": [],
        "controlled_terms": [],
        "external_references": [],
        "rendering": {
            "markdown_profile": "agileforge.spec_markdown.v1",
            "rendered_markdown_sha256": None,
        },
    }


def test_brownfield_transitions_persist_evidence_then_share_registration(
    domain: WorkflowDomain,
    engine: Engine,
) -> None:
    """Persist all four brownfield facts before shared registration."""
    project_id = _open_brownfield(domain)
    baseline = domain.transition(
        RecordRepositoryBaseline.model_validate(
            {
                **_guards(
                    domain,
                    project_id,
                    RecordRepositoryBaseline.node_id,
                    "baseline-1",
                ),
                "repository_path": REPOSITORY_PATH,
                "git_commit": COMMIT,
                "dirty": False,
                "baseline_fingerprint": _baseline_fingerprint(),
            }
        )
    )
    assert baseline.ok is True
    baseline_id = _required_output_id(baseline, "repository_baseline_id")

    inventory = domain.transition(
        RecordRepositoryInventory.model_validate(
            {
                **_guards(
                    domain,
                    project_id,
                    RecordRepositoryInventory.node_id,
                    "inventory-1",
                ),
                "repository_baseline_id": baseline_id,
                "files": FILES,
                "selected_for_model": ("README.md",),
                "total_bytes": TOTAL_BYTES,
                "inventory_fingerprint": _inventory_fingerprint(),
            }
        )
    )
    assert inventory.ok is True
    inventory_id = _required_output_id(inventory, "repository_inventory_id")

    draft = domain.transition(
        RecordBrownfieldSpecDraft.model_validate(
            {
                **_guards(
                    domain,
                    project_id,
                    RecordBrownfieldSpecDraft.node_id,
                    "brownfield-spec-1",
                ),
                "repository_inventory_id": inventory_id,
                "canonical_content": _spec_content(),
                "provenance_path": "repository-inventory:41",
            }
        )
    )
    assert draft.ok is True
    draft_id = _required_output_id(draft, "spec_draft_id")
    draft_fingerprint = draft.output["content_fingerprint"]
    assert isinstance(draft_fingerprint, str)

    reviewed = domain.transition(
        DecideBrownfieldInitialSpec.model_validate(
            {
                **_guards(
                    domain,
                    project_id,
                    DecideBrownfieldInitialSpec.node_id,
                    "brownfield-review-1",
                ),
                "spec_draft_id": draft_id,
                "artifact_fingerprint": draft_fingerprint,
                "decision": "accepted",
                "notes": "Repository evidence reviewed by operator.",
            }
        )
    )
    assert reviewed.ok is True
    assert reviewed.position is not None
    registration_decision = _available_decision(
        reviewed,
        RegisterInitialScope.node_id,
    )

    registered = domain.transition(
        RegisterInitialScope(
            project_id=project_id,
            graph_version=reviewed.position.graph_version,
            fact_fingerprint=reviewed.position.fact_fingerprint,
            decision_fingerprint=registration_decision.decision_fingerprint,
            idempotency_key="register-brownfield-1",
            actor=ACTOR,
            correlation_id="task-8",
            spec_draft_id=draft_id,
        )
    )

    assert registered.ok is True
    with Session(engine) as session:
        assert len(session.exec(select(Product)).all()) == 1
        project = session.get(Product, project_id)
        assert project is not None
        assert project.name == "Existing Product"
        assert len(session.exec(select(RepositoryBaseline)).all()) == 1
        assert len(session.exec(select(RepositoryInventory)).all()) == 1
        assert len(session.exec(select(SpecDraft)).all()) == 1
        assert len(session.exec(select(SpecRegistry)).all()) == 1


def test_baseline_fingerprint_mismatch_does_not_persist(
    domain: WorkflowDomain,
    engine: Engine,
) -> None:
    """Bind baseline evidence to its exact canonical path and Git state."""
    project_id = _open_brownfield(domain)
    result = domain.transition(
        RecordRepositoryBaseline.model_validate(
            {
                **_guards(
                    domain,
                    project_id,
                    RecordRepositoryBaseline.node_id,
                    "baseline-tampered",
                ),
                "repository_path": REPOSITORY_PATH,
                "git_commit": COMMIT,
                "dirty": False,
                "baseline_fingerprint": "sha256:wrong",
            }
        )
    )

    assert result.ok is False
    with Session(engine) as session:
        assert session.exec(select(RepositoryBaseline)).all() == []


def test_inventory_fingerprint_mismatch_does_not_persist(
    domain: WorkflowDomain,
    engine: Engine,
) -> None:
    """Bind complete inventory independently from bounded model selection."""
    project_id = _open_brownfield(domain)
    baseline = domain.transition(
        RecordRepositoryBaseline.model_validate(
            {
                **_guards(
                    domain,
                    project_id,
                    RecordRepositoryBaseline.node_id,
                    "baseline-for-inventory-tamper",
                ),
                "repository_path": REPOSITORY_PATH,
                "git_commit": COMMIT,
                "dirty": False,
                "baseline_fingerprint": _baseline_fingerprint(),
            }
        )
    )
    baseline_id = _required_output_id(baseline, "repository_baseline_id")

    result = domain.transition(
        RecordRepositoryInventory.model_validate(
            {
                **_guards(
                    domain,
                    project_id,
                    RecordRepositoryInventory.node_id,
                    "inventory-tampered",
                ),
                "repository_baseline_id": baseline_id,
                "files": FILES,
                "selected_for_model": ("README.md",),
                "total_bytes": TOTAL_BYTES,
                "inventory_fingerprint": "sha256:wrong",
            }
        )
    )

    assert result.ok is False
    with Session(engine) as session:
        assert session.exec(select(RepositoryInventory)).all() == []


def test_brownfield_spec_attempt_binding_requires_complete_pair() -> None:
    """Retain base-class ADK attempt identity and fingerprint pairing."""
    with pytest.raises(ValidationError):
        RecordBrownfieldSpecDraft.model_validate(
            {
                "project_id": 1,
                "graph_version": "agileforge.workflow.v1",
                "fact_fingerprint": "sha256:facts",
                "decision_fingerprint": "sha256:decision",
                "idempotency_key": "attempt-pair",
                "actor": ACTOR,
                "attempt_id": 3,
                "repository_inventory_id": 4,
                "canonical_content": _spec_content(),
                "provenance_path": None,
            }
        )
