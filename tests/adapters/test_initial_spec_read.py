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
from tests.workflow.lifecycle_fixtures import seed_accepted_specification
from workflow import WorkflowDomain
from workflow.clock import FixedClock
from workflow.definitions.root import ROOT_GRAPH
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from sqlmodel import Session

    from workflow.contracts import JsonObject

EVALUATED_AT = datetime(2026, 8, 3, 16, tzinfo=UTC)
SPEC_CONTENT: JsonObject = {
    "schema_version": "agileforge.spec.v1",
    "title": "Reviewed initial specification",
    "summary": "Canonical content visible before the human decision.",
}
@pytest.fixture
def domain(engine: Engine) -> WorkflowDomain:
    """Build a deterministic workflow domain over the test database."""
    return WorkflowDomain(
        engine=engine,
        graph=ROOT_GRAPH,
        clock=FixedClock(now_value=EVALUATED_AT),
    )


def _required_int(value: object) -> int:
    assert isinstance(value, int)
    return value


def _create_project(session: Session, *, name: str = "Initial spec read") -> int:
    project = Project(name=name)
    session.add(project)
    session.commit()
    return _required_int(project.project_id)


def _seed_active_draft(
    session: Session,
) -> tuple[int, int, str]:
    project_id = _create_project(session)
    seed_accepted_specification(
        session,
        project_id=project_id,
        content=canonical_json(SPEC_CONTENT),
        recorded_at=EVALUATED_AT,
    )
    run = DiscoveryRun(project_id=project_id, purpose="initial", ordinal=1)
    session.add(run)
    session.flush()
    run_id = _required_int(run.discovery_run_id)
    fingerprint = canonical_hash(SPEC_CONTENT)
    draft = SpecDraft(
        project_id=project_id,
        discovery_run_id=run_id,
        kind="initial",
        version_number=1,
        canonical_content_json=canonical_json(SPEC_CONTENT),
        content_fingerprint=fingerprint,
        provenance_path="v2-specification:accepted",
        created_at=EVALUATED_AT,
    )
    session.add(draft)
    session.commit()
    draft_id = _required_int(draft.spec_draft_id)
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


def test_initial_spec_read_uses_v2_lineage_without_setup_decision(
    domain: WorkflowDomain,
    engine: Engine,
    session: Session,
) -> None:
    """Return canonical draft identity without restoring retired setup routing."""
    project_id, draft_id, fingerprint = _seed_active_draft(session)
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
        "provenance_path": "v2-specification:accepted",
        "created_at": EVALUATED_AT.isoformat(),
        "updated_at": EVALUATED_AT.isoformat(),
    }
    position = domain.position(project_id)
    assert all("brownfield" not in item.node_id for item in position.decisions)
    assert all("setup" not in item.node_id for item in position.decisions)
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
    engine: Engine,
    session: Session,
) -> None:
    """Fail closed for missing ownership, no draft, or an ambiguous chain."""
    project_id = 999
    if scenario == "missing_draft":
        project_id = _create_project(session, name="Missing draft")
    elif scenario == "ambiguous_draft":
        project_id = _create_project(session, name="Ambiguous")
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
    engine: Engine,
    session: Session,
) -> None:
    """Return the typed invalid-draft failure for both persisted corruption modes."""
    project_id, draft_id, _fingerprint = _seed_active_draft(session)
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
    session: Session,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Route the parser command to the non-routing production projection."""
    project_id, draft_id, fingerprint = _seed_active_draft(session)
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
