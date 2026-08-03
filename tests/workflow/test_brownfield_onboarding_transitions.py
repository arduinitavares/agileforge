"""Transactional brownfield onboarding transition tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError
from sqlmodel import Session, select

from models.core import Project
from models.specs import SpecRegistry
from models.workflow import RepositoryBaseline, RepositoryInventory, SpecDraft
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
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
from workflow.fingerprints import canonical_hash, canonical_json
from workflow.repository_inventory import (
    canonical_inventory_payload,
    encode_repository_path,
    encode_repository_paths,
    inventory_binding_fingerprint,
)
from workflow.requests.onboarding import RepositoryInventoryEntry

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
SELECTED_FOR_MODEL = ("README.md",)
SURROGATE_PATH = "bad-\udcff.py"


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
            name="Existing Project",
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
    payload = canonical_inventory_payload(
        git_available=True,
        commit=COMMIT,
        dirty=False,
        files=(
            (".env", 8, None, "secret"),
            ("README.md", 12, "sha256:readme", "hashable"),
        ),
        total_bytes=TOTAL_BYTES,
    )
    return inventory_binding_fingerprint(payload, SELECTED_FOR_MODEL)


def _record_baseline(
    domain: WorkflowDomain,
    project_id: int,
    *,
    key: str,
) -> int:
    result = domain.transition(
        RecordRepositoryBaseline.model_validate(
            {
                **_guards(
                    domain,
                    project_id,
                    RecordRepositoryBaseline.node_id,
                    key,
                ),
                "repository_path": REPOSITORY_PATH,
                "git_commit": COMMIT,
                "dirty": False,
                "baseline_fingerprint": _baseline_fingerprint(),
            }
        )
    )
    assert result.ok is True
    return _required_output_id(result, "repository_baseline_id")


def _record_inventory(
    domain: WorkflowDomain,
    project_id: int,
    baseline_id: int,
    *,
    key: str,
) -> int:
    result = domain.transition(
        RecordRepositoryInventory.model_validate(
            {
                **_guards(
                    domain,
                    project_id,
                    RecordRepositoryInventory.node_id,
                    key,
                ),
                "repository_baseline_id": baseline_id,
                "git_available": True,
                "files": FILES,
                "selected_for_model": SELECTED_FOR_MODEL,
                "total_bytes": TOTAL_BYTES,
                "inventory_fingerprint": _inventory_fingerprint(),
            }
        )
    )
    assert result.ok is True
    return _required_output_id(result, "repository_inventory_id")


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
                "git_available": True,
                "files": FILES,
                "selected_for_model": SELECTED_FOR_MODEL,
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
                "repository_inventory_fingerprint": _inventory_fingerprint(),
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
        assert len(session.exec(select(Project)).all()) == 1
        project = session.get(Project, project_id)
        assert project is not None
        assert project.name == "Existing Project"
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
                "git_available": True,
                "files": FILES,
                "selected_for_model": SELECTED_FOR_MODEL,
                "total_bytes": TOTAL_BYTES,
                "inventory_fingerprint": "sha256:wrong",
            }
        )
    )

    assert result.ok is False
    with Session(engine) as session:
        assert session.exec(select(RepositoryInventory)).all() == []


def test_repository_inventory_contract_serializes_surrogate_paths_reversibly() -> None:
    """Preserve raw path bytes through validation and request fingerprinting."""
    encoded_path = encode_repository_path(SURROGATE_PATH)
    entry = RepositoryInventoryEntry.model_validate(
        {
            "path": SURROGATE_PATH,
            "size_bytes": 4,
            "sha256": "sha256:surrogate",
            "content_status": "hashable",
        }
    )
    payload = canonical_inventory_payload(
        git_available=True,
        commit=COMMIT,
        dirty=False,
        files=((SURROGATE_PATH, 4, "sha256:surrogate", "hashable"),),
        total_bytes=4,
    )
    request = RecordRepositoryInventory.model_validate(
        {
            "project_id": 1,
            "graph_version": "agileforge.workflow.v1",
            "fact_fingerprint": "sha256:facts",
            "decision_fingerprint": "sha256:decision",
            "idempotency_key": "surrogate-contract",
            "actor": ACTOR,
            "repository_baseline_id": 2,
            "git_available": True,
            "files": (entry,),
            "selected_for_model": (SURROGATE_PATH,),
            "total_bytes": 4,
            "inventory_fingerprint": inventory_binding_fingerprint(
                payload,
                (SURROGATE_PATH,),
            ),
        }
    )

    dumped = request.model_dump(mode="json")
    dumped_files = dumped["files"]
    assert isinstance(dumped_files, list)
    assert dumped_files[0]["path"] == encoded_path
    assert dumped["selected_for_model"] == [encoded_path]
    assert canonical_hash(dumped).startswith("sha256:")
    assert encode_repository_path(encoded_path) != encoded_path


def test_surrogate_path_round_trips_through_handler_and_loader(
    domain: WorkflowDomain,
    engine: Engine,
) -> None:
    """Persist canonical ASCII path bytes and restore the original path string."""
    project_id = _open_brownfield(domain)
    baseline_id = _record_baseline(domain, project_id, key="surrogate-baseline")
    files = (
        {
            "path": SURROGATE_PATH,
            "size_bytes": 4,
            "sha256": "sha256:surrogate",
            "content_status": "hashable",
        },
    )
    selected = (SURROGATE_PATH,)
    payload = canonical_inventory_payload(
        git_available=True,
        commit=COMMIT,
        dirty=False,
        files=((SURROGATE_PATH, 4, "sha256:surrogate", "hashable"),),
        total_bytes=4,
    )
    fingerprint = inventory_binding_fingerprint(payload, selected)
    result = domain.transition(
        RecordRepositoryInventory.model_validate(
            {
                **_guards(
                    domain,
                    project_id,
                    RecordRepositoryInventory.node_id,
                    "surrogate-inventory",
                ),
                "repository_baseline_id": baseline_id,
                "git_available": True,
                "files": files,
                "selected_for_model": selected,
                "total_bytes": 4,
                "inventory_fingerprint": fingerprint,
            }
        )
    )

    assert result.ok is True
    with Session(engine) as session:
        row = session.exec(select(RepositoryInventory)).one()
        assert row.canonical_inventory_json == canonical_json(payload)
        assert row.selected_for_model_json == canonical_json(
            encode_repository_paths(selected)
        )
        assert row.content_fingerprint == fingerprint
        snapshot = WorkflowFactRepository(session).load(project_id)
    assert snapshot.repository_inventories[0].selected_for_model == selected


def test_load_rejects_selected_for_model_only_tampering(
    domain: WorkflowDomain,
    engine: Engine,
) -> None:
    """Bind the exact bounded selection to the persisted inventory fingerprint."""
    project_id = _open_brownfield(domain)
    baseline_id = _record_baseline(domain, project_id, key="selection-baseline")
    _record_inventory(
        domain,
        project_id,
        baseline_id,
        key="selection-inventory",
    )
    with Session(engine) as session:
        row = session.exec(select(RepositoryInventory)).one()
        row.selected_for_model_json = canonical_json(())
        session.add(row)
        session.commit()

    with (
        Session(engine) as session,
        pytest.raises(WorkflowFactLoadError, match="binding mismatch"),
    ):
        WorkflowFactRepository(session).load(project_id)


def test_load_rejects_inventory_metadata_only_tampering(
    domain: WorkflowDomain,
    engine: Engine,
) -> None:
    """Reject a changed file digest even when summary columns are untouched."""
    project_id = _open_brownfield(domain)
    baseline_id = _record_baseline(domain, project_id, key="metadata-baseline")
    _record_inventory(
        domain,
        project_id,
        baseline_id,
        key="metadata-inventory",
    )
    with Session(engine) as session:
        row = session.exec(select(RepositoryInventory)).one()
        payload = json.loads(row.canonical_inventory_json)
        assert isinstance(payload, dict)
        files = payload["files"]
        assert isinstance(files, list)
        readme = files[1]
        assert isinstance(readme, dict)
        readme["sha256"] = "sha256:tampered"
        row.canonical_inventory_json = canonical_json(payload)
        session.add(row)
        session.commit()

    with (
        Session(engine) as session,
        pytest.raises(WorkflowFactLoadError, match="binding mismatch"),
    ):
        WorkflowFactRepository(session).load(project_id)


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
                "repository_inventory_fingerprint": _inventory_fingerprint(),
                "canonical_content": _spec_content(),
                "provenance_path": None,
            }
        )


def test_brownfield_spec_rejects_mismatched_inventory_fingerprint(
    domain: WorkflowDomain,
    engine: Engine,
) -> None:
    """Persist no draft when its trusted inventory binding is not exact."""
    project_id = _open_brownfield(domain)
    baseline_id = _record_baseline(domain, project_id, key="mismatch-baseline")
    inventory_id = _record_inventory(
        domain,
        project_id,
        baseline_id,
        key="mismatch-inventory",
    )

    result = domain.transition(
        RecordBrownfieldSpecDraft.model_validate(
            {
                **_guards(
                    domain,
                    project_id,
                    RecordBrownfieldSpecDraft.node_id,
                    "mismatch-curation",
                ),
                "repository_inventory_id": inventory_id,
                "repository_inventory_fingerprint": f"sha256:{'f' * 64}",
                "canonical_content": _spec_content(),
                "provenance_path": f"repository-inventory:{inventory_id}",
            }
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert "exact" in result.error.message
    with Session(engine) as session:
        assert session.exec(select(SpecDraft)).all() == []
