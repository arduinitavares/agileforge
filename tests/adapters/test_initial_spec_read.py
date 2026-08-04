"""Facts-only initial specification review projection and CLI tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cli.main import build_parser, main
from models.core import Project
from models.workflow import DiscoveryRun, SpecDraft
from services.application import AgileForgeApplication
from services.read_projections import DurableReadProjectionService
from workflow import (
    DecideBrownfieldInitialSpec,
    OpenProjectShell,
    RecordBrownfieldSpecDraft,
    RecordRepositoryBaseline,
    RecordRepositoryInventory,
    WorkflowDomain,
)
from workflow.clock import FixedClock
from workflow.contracts import JsonObject, NodeCategory, NodeDecision
from workflow.definitions.root import ROOT_GRAPH
from workflow.fingerprints import canonical_hash, canonical_json
from workflow.repository_inventory import (
    canonical_inventory_payload,
    inventory_binding_fingerprint,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from sqlmodel import Session

EVALUATED_AT = datetime(2026, 8, 3, 16, tzinfo=UTC)
ACTOR = "operator@example.com"
REPOSITORY_PATH = "/operator-selected/repository"
GIT_COMMIT = "c" * 40
SPEC_CONTENT: JsonObject = {
    "schema_version": "agileforge.spec.v1",
    "title": "Reviewed initial specification",
    "summary": "Canonical content visible before the human decision.",
}
INVENTORY_FILES = (
    {
        "path": "README.md",
        "size_bytes": 12,
        "sha256": "sha256:readme",
        "content_status": "hashable",
    },
)


@pytest.fixture
def domain(engine: Engine) -> WorkflowDomain:
    """Build a deterministic workflow domain over the test database."""
    return WorkflowDomain(
        engine=engine,
        graph=ROOT_GRAPH,
        clock=FixedClock(now_value=EVALUATED_AT),
    )


def _decision(domain: WorkflowDomain, project_id: int, node_id: str) -> NodeDecision:
    position = domain.position(project_id)
    decision = next(item for item in position.decisions if item.node_id == node_id)
    assert decision.category is NodeCategory.AVAILABLE
    return decision


def _guards(
    domain: WorkflowDomain,
    project_id: int,
    node_id: str,
    idempotency_key: str,
) -> dict[str, object]:
    position = domain.position(project_id)
    decision = _decision(domain, project_id, node_id)
    return {
        "project_id": project_id,
        "graph_version": position.graph_version,
        "fact_fingerprint": position.fact_fingerprint,
        "decision_fingerprint": decision.decision_fingerprint,
        "instance_key": decision.instance_key,
        "idempotency_key": idempotency_key,
        "actor": ACTOR,
    }


def _required_int(value: object) -> int:
    assert isinstance(value, int)
    return value


def _open_project(domain: WorkflowDomain, *, key: str = "open-project") -> int:
    result = domain.transition(
        OpenProjectShell(
            name="Initial spec read",
            origin="brownfield",
            idempotency_key=key,
            actor=ACTOR,
        )
    )
    assert result.ok is True
    return _required_int(result.output.get("project_id"))


def _inventory_fingerprint() -> str:
    payload = canonical_inventory_payload(
        git_available=True,
        commit=GIT_COMMIT,
        dirty=False,
        files=(("README.md", 12, "sha256:readme", "hashable"),),
        total_bytes=12,
    )
    return inventory_binding_fingerprint(payload, ("README.md",))


def _seed_active_draft(
    domain: WorkflowDomain,
) -> tuple[int, int, str]:
    project_id = _open_project(domain)
    baseline_fingerprint = canonical_hash(
        {
            "repository_path": REPOSITORY_PATH,
            "git_commit": GIT_COMMIT,
            "dirty": False,
        }
    )
    baseline = domain.transition(
        RecordRepositoryBaseline.model_validate(
            {
                **_guards(
                    domain,
                    project_id,
                    RecordRepositoryBaseline.node_id,
                    "record-baseline",
                ),
                "repository_path": REPOSITORY_PATH,
                "git_commit": GIT_COMMIT,
                "dirty": False,
                "baseline_fingerprint": baseline_fingerprint,
            }
        )
    )
    assert baseline.ok is True
    baseline_id = _required_int(baseline.output.get("repository_baseline_id"))
    inventory_fingerprint = _inventory_fingerprint()
    inventory = domain.transition(
        RecordRepositoryInventory.model_validate(
            {
                **_guards(
                    domain,
                    project_id,
                    RecordRepositoryInventory.node_id,
                    "record-inventory",
                ),
                "repository_baseline_id": baseline_id,
                "git_available": True,
                "files": INVENTORY_FILES,
                "selected_for_model": ["README.md"],
                "total_bytes": 12,
                "inventory_fingerprint": inventory_fingerprint,
            }
        )
    )
    assert inventory.ok is True
    inventory_id = _required_int(inventory.output.get("repository_inventory_id"))
    draft = domain.transition(
        RecordBrownfieldSpecDraft.model_validate(
            {
                **_guards(
                    domain,
                    project_id,
                    RecordBrownfieldSpecDraft.node_id,
                    "record-spec-draft",
                ),
                "repository_inventory_id": inventory_id,
                "repository_inventory_fingerprint": inventory_fingerprint,
                "canonical_content": SPEC_CONTENT,
                "provenance_path": "repository-inventory:reviewed",
            }
        )
    )
    assert draft.ok is True
    draft_id = _required_int(draft.output.get("spec_draft_id"))
    fingerprint = draft.output.get("content_fingerprint")
    assert isinstance(fingerprint, str)
    return project_id, draft_id, fingerprint


def _error_code(result: JsonObject) -> str:
    errors = result.get("errors")
    assert isinstance(errors, list)
    assert len(errors) == 1
    error = errors[0]
    assert isinstance(error, dict)
    code = error.get("code")
    assert isinstance(code, str)
    return code


def test_initial_spec_read_matches_available_human_decision(
    domain: WorkflowDomain,
    engine: Engine,
) -> None:
    """Return the canonical draft identity bound to the available decision."""
    project_id, draft_id, fingerprint = _seed_active_draft(domain)
    projection = DurableReadProjectionService(engine=engine)
    read = getattr(projection, "project_initial_spec", None)

    assert callable(read), "initial-spec projection is missing"
    result = read(project_id=project_id)

    assert result["ok"] is True
    data = result["data"]
    assert isinstance(data, dict)
    draft = data["active_draft"]
    assert isinstance(draft, dict)
    assert draft == {
        "spec_draft_id": draft_id,
        "discovery_run_id": 1,
        "kind": "initial",
        "version_number": 1,
        "canonical_content": SPEC_CONTENT,
        "content_fingerprint": fingerprint,
        "provenance_path": "repository-inventory:reviewed",
        "created_at": EVALUATED_AT.isoformat(),
        "updated_at": EVALUATED_AT.isoformat(),
    }
    decision = _decision(
        domain,
        project_id,
        DecideBrownfieldInitialSpec.node_id,
    )
    draft_reference = next(
        item for item in decision.fact_references if item.fact_type == "spec_draft"
    )
    assert draft_reference.fact_id == str(draft_id)
    assert draft_reference.fingerprint == fingerprint

    accepted = domain.transition(
        DecideBrownfieldInitialSpec.model_validate(
            {
                **_guards(
                    domain,
                    project_id,
                    DecideBrownfieldInitialSpec.node_id,
                    "accept-reviewed-draft",
                ),
                "spec_draft_id": draft["spec_draft_id"],
                "artifact_fingerprint": draft["content_fingerprint"],
                "decision": "accepted",
                "notes": "Reviewed through the supported facts-only read.",
            }
        )
    )
    assert accepted.ok is True
    serialized = json.dumps(result, sort_keys=True)
    for forbidden in ("command", "recommendation", "routing", "session"):
        assert forbidden not in serialized.lower()


@pytest.mark.parametrize(
    ("scenario", "expected_code"),
    [
        ("missing_project", "PROJECT_NOT_FOUND"),
        ("missing_draft", "INITIAL_SPEC_DRAFT_NOT_FOUND"),
        ("ambiguous_draft", "INITIAL_SPEC_DRAFT_AMBIGUOUS"),
    ],
)
def test_initial_spec_read_returns_typed_failures(
    scenario: str,
    expected_code: str,
    domain: WorkflowDomain,
    engine: Engine,
    session: Session,
) -> None:
    """Fail closed for missing ownership, no draft, or an ambiguous chain."""
    project_id = 999
    if scenario == "missing_draft":
        project_id = _open_project(domain, key="open-without-draft")
    elif scenario == "ambiguous_draft":
        project = Project(name="Ambiguous", origin="brownfield")
        session.add(project)
        session.flush()
        project_id = _required_int(project.project_id)
        run = DiscoveryRun(project_id=project_id, purpose="initial", ordinal=1)
        session.add(run)
        session.flush()
        run_id = _required_int(run.discovery_run_id)
        for version in (1, 2):
            content: JsonObject = {"version": version}
            session.add(
                SpecDraft(
                    project_id=project_id,
                    discovery_run_id=run_id,
                    kind="initial",
                    version_number=version,
                    canonical_content_json=canonical_json(content),
                    content_fingerprint=canonical_hash(content),
                    created_at=EVALUATED_AT,
                )
            )
        session.commit()
    projection = DurableReadProjectionService(engine=engine)
    read = getattr(projection, "project_initial_spec", None)

    assert callable(read), "initial-spec projection is missing"
    result = read(project_id=project_id)

    assert result["ok"] is False
    assert _error_code(result) == expected_code


@pytest.mark.parametrize("corruption", ["malformed_content", "fingerprint_mismatch"])
def test_initial_spec_read_returns_typed_failure_for_corrupt_active_draft(
    corruption: str,
    domain: WorkflowDomain,
    engine: Engine,
    session: Session,
) -> None:
    """Return the typed invalid-draft failure for both persisted corruption modes."""
    project_id, draft_id, _fingerprint = _seed_active_draft(domain)
    draft = session.get(SpecDraft, draft_id)
    assert draft is not None
    if corruption == "malformed_content":
        draft.canonical_content_json = "{not-canonical-json"
    else:
        draft.content_fingerprint = "sha256:does-not-match-content"
    session.add(draft)
    session.commit()

    result = DurableReadProjectionService(engine=engine).project_initial_spec(
        project_id=project_id
    )

    assert result["ok"] is False
    assert _error_code(result) == "INITIAL_SPEC_DRAFT_INVALID"
    assert result["data"] == {
        "project_id": project_id,
        "spec_draft_id": draft_id,
    }


def test_project_initial_spec_cli_is_a_supported_read(
    domain: WorkflowDomain,
    engine: Engine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Route the parser command to the non-routing production projection."""
    project_id, draft_id, fingerprint = _seed_active_draft(domain)
    argv = ["project", "initial-spec", "--project-id", str(project_id)]

    try:
        parsed = build_parser().parse_args(argv)
    except ValueError:
        parsed = None
    assert parsed is not None, "project initial-spec parser command is missing"
    exit_code = main(
        argv,
        application=AgileForgeApplication(
            workflow_domain=domain,
            read_projection=DurableReadProjectionService(engine=engine),
        ),
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["active_draft"]["spec_draft_id"] == draft_id
    assert payload["data"]["active_draft"]["content_fingerprint"] == fingerprint


def test_agent_cli_manual_names_initial_spec_read() -> None:
    """Keep the supported review command in the operational CLI contract."""
    manual = (Path(__file__).parents[2] / "docs" / "agent-cli-manual.md").read_text()

    assert (
        "./agileforge-dev cli --profile local -- project initial-spec --project-id 41"
    ) in manual
    assert "canonical content" in manual
    assert "content_fingerprint" in manual
