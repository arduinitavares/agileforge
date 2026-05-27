"""Tests for evidence-aware reconciliation collection models."""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine, select

from models.core import Product
from models.enums import WorkflowEventType
from models.events import WorkflowEvent
from models.specs import (
    CompiledSpecAuthority,
    SpecAuthorityAcceptance,
    SpecRegistry,
)
from services.agent_workbench import evidence_collect as evidence_collect_module
from services.agent_workbench.authority_projection import pending_authority_fingerprint
from services.agent_workbench.evidence_collect import (
    EVIDENCE_COLLECT_COMMAND,
    IMPLEMENTATION_EVIDENCE_STATE_KEY,
    CollectorMetadata,
    EvidenceCollectionRunner,
    EvidencePath,
    ReconciliationFinding,
    ReconciliationReport,
    RepoMetadata,
    build_summary,
    classify_finding,
)


def _evidence(kind: str, path: str = "services/example.py") -> EvidencePath:
    """Build a minimal evidence path for classification tests."""
    return EvidencePath(
        kind=kind,
        path=path,
        match_count=1,
        matched_terms=["example"],
    )


def test_scanner_exports_expected_contract() -> None:
    """Verify Task 2 scanner entry points are exposed by the module."""
    expected_names = [
        "SpecEvidenceTarget",
        "collect_repo_evidence",
        "file_kind_for_path",
    ]

    missing_names = [
        name for name in expected_names if not hasattr(evidence_collect_module, name)
    ]

    assert missing_names == []


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_budget.py", "test"),
        ("src/test_helpers.py", "source"),
        ("src/config/test_db.js", "source"),
        ("src/budget.test.js", "test"),
        ("tests/README.md", "doc"),
        ("tests/fixtures/example.json", "source"),
        ("docs/budget.md", "doc"),
        ("pyproject.toml", "config"),
    ],
)
def test_file_kind_for_path_uses_exact_test_doc_and_config_rules(
    path: str,
    expected: str,
) -> None:
    """Verify scanner path classification uses exact test path rules."""
    assert evidence_collect_module.file_kind_for_path(Path(path)) == expected


def test_collect_repo_evidence_treats_invariant_terms_as_equivalent_matches(
    tmp_path: Path,
) -> None:
    """Verify target terms can be evidenced by either requirement or invariant tags."""
    repo_path = tmp_path
    (repo_path / "src").mkdir()
    (repo_path / "tests").mkdir()
    (repo_path / "src" / "budget.py").write_text(
        "def validate_budget():\n    return 'INV-abc123'\n",
        encoding="utf-8",
    )
    (repo_path / "tests" / "test_budget.py").write_text(
        "def test_budget_validation():\n    assert 'REQ.budget-validation'\n",
        encoding="utf-8",
    )
    target = evidence_collect_module.SpecEvidenceTarget(
        spec_item_id="REQ.budget-validation",
        item_type="REQ",
        verification_method="unit-test",
        matched_terms=["REQ.budget-validation", "INV-abc123"],
    )

    findings, warnings = evidence_collect_module.collect_repo_evidence(
        repo_path,
        [target],
    )

    assert warnings == []
    assert len(findings) == 1
    assert findings[0].status == "evidenced"
    assert findings[0].confidence == "medium"
    assert {evidence.kind for evidence in findings[0].evidence_paths} == {
        "source",
        "test",
    }


def test_collect_repo_evidence_skips_database_lock_binary_and_oversized_files(
    tmp_path: Path,
) -> None:
    """Verify scanner skips files that should not be read as source evidence."""
    repo_path = tmp_path
    (repo_path / "src").mkdir()
    (repo_path / "src" / "budget.py").write_text(
        "REQ.budget-validation\n",
        encoding="utf-8",
    )
    (repo_path / "agileforge.db").write_bytes(b"REQ.budget-validation")
    (repo_path / "uv.lock").write_text(
        "REQ.budget-validation\n",
        encoding="utf-8",
    )
    (repo_path / "chart.png").write_bytes(b"REQ.budget-validation")
    (repo_path / "large.txt").write_text(
        f"{'x' * (500 * 1024)}REQ.budget-validation\n",
        encoding="utf-8",
    )
    target = evidence_collect_module.SpecEvidenceTarget(
        spec_item_id="REQ.budget-validation",
        item_type="REQ",
        verification_method="unit-test",
        matched_terms=["REQ.budget-validation"],
    )

    findings, warnings = evidence_collect_module.collect_repo_evidence(
        repo_path,
        [target],
    )

    assert len(findings) == 1
    assert findings[0].status == "evidence_missing"
    assert [evidence.path for evidence in findings[0].evidence_paths] == [
        "src/budget.py"
    ]
    assert {evidence.kind for evidence in findings[0].evidence_paths} == {"source"}
    assert any(warning.code == "EVIDENCE_FILE_SKIPPED" for warning in warnings)


def test_collect_repo_evidence_uses_tag_boundaries_not_substrings(
    tmp_path: Path,
) -> None:
    """Verify similarly prefixed IDs do not create false positive evidence."""
    repo_path = tmp_path
    (repo_path / "src").mkdir()
    (repo_path / "src" / "budget.py").write_text(
        "# REQ-10\n",
        encoding="utf-8",
    )
    target = evidence_collect_module.SpecEvidenceTarget(
        spec_item_id="REQ-1",
        item_type="REQ",
        verification_method="unit-test",
        matched_terms=["REQ-1"],
    )

    findings, warnings = evidence_collect_module.collect_repo_evidence(
        repo_path,
        [target],
    )

    assert warnings == []
    assert findings[0].status == "missing"
    assert findings[0].evidence_paths == []


def test_collect_repo_evidence_does_not_treat_markdown_under_tests_as_test_evidence(
    tmp_path: Path,
) -> None:
    """Verify documentation in tests/ does not satisfy required test evidence."""
    repo_path = tmp_path
    (repo_path / "src").mkdir()
    (repo_path / "tests").mkdir()
    (repo_path / "src" / "budget.py").write_text(
        "# REQ.budget-validation\n",
        encoding="utf-8",
    )
    (repo_path / "tests" / "README.md").write_text(
        "REQ.budget-validation\n",
        encoding="utf-8",
    )
    target = evidence_collect_module.SpecEvidenceTarget(
        spec_item_id="REQ.budget-validation",
        item_type="REQ",
        verification_method="unit-test",
        matched_terms=["REQ.budget-validation"],
    )

    findings, warnings = evidence_collect_module.collect_repo_evidence(
        repo_path,
        [target],
    )

    assert warnings == []
    assert findings[0].status == "evidence_missing"
    assert {evidence.kind for evidence in findings[0].evidence_paths} == {
        "doc",
        "source",
    }


def test_collect_repo_evidence_warns_when_directory_cannot_be_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify unreadable repository subtrees produce a warning."""

    def fake_walk(
        repo_root: Path,
        onerror: object | None = None,
    ) -> object:
        error = OSError("blocked")
        error.filename = str(repo_root / "secret")
        if callable(onerror):
            onerror(error)
        return iter(())

    monkeypatch.setattr(evidence_collect_module.os, "walk", fake_walk)
    target = evidence_collect_module.SpecEvidenceTarget(
        spec_item_id="REQ.budget-validation",
        item_type="REQ",
        verification_method="unit-test",
        matched_terms=["REQ.budget-validation"],
    )

    findings, warnings = evidence_collect_module.collect_repo_evidence(
        tmp_path,
        [target],
    )

    assert findings[0].status == "missing"
    assert [warning.code for warning in warnings] == ["EVIDENCE_FILE_UNREADABLE"]


@pytest.mark.parametrize(
    ("evidence_paths", "verification_method", "expected"),
    [
        (
            [
                _evidence("source"),
                _evidence("test", "tests/test_example.py"),
            ],
            "unit-test",
            ("evidenced", "medium"),
        ),
        (
            [_evidence("source")],
            "unit-test",
            ("evidence_missing", "medium"),
        ),
        (
            [_evidence("test", "tests/test_example.py")],
            "unit-test",
            ("evidence_missing", "medium"),
        ),
        (
            [],
            "unit-test",
            ("missing", "low"),
        ),
        (
            [_evidence("source")],
            "unsupported-method",
            ("unknown", "low"),
        ),
    ],
)
def test_classify_finding_reports_evidence_status_and_confidence(
    evidence_paths: list[EvidencePath],
    verification_method: str,
    expected: tuple[str, str],
) -> None:
    """Verify finding classification follows verification evidence rules."""
    assert classify_finding(
        evidence_paths,
        verification_method=verification_method,
    ) == expected


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {"kind": "source", "path": "   ", "matched_terms": ["example"]},
            "path must be non-empty",
        ),
        (
            {"kind": "source", "path": "services/example.py", "matched_terms": []},
            "matched_terms must be non-empty",
        ),
        (
            {"kind": "source", "path": "services/example.py", "match_count": 1},
            "Field required",
        ),
    ],
)
def test_evidence_path_rejects_empty_required_evidence_fields(
    payload: dict[str, object],
    message: str,
) -> None:
    """Verify evidence paths require useful location and matching context."""
    with pytest.raises(ValidationError, match=message):
        EvidencePath(**payload)


def test_report_schema_matches_phase_1a_contract() -> None:
    """Verify report models use the stored workflow-state contract."""
    finding = ReconciliationFinding(
        spec_item_id="REQ.budget-validation",
        item_type="REQ",
        verification_method="unit-test",
        status="evidence_missing",
        confidence="medium",
        evidence_paths=[_evidence("source", "src/budget.py")],
        notes=["Exact behavior reference found. Required test reference not found."],
    )
    report = ReconciliationReport(
        project_id=2,
        spec_version_id=7,
        compiled_authority_fingerprint="sha256:authority",
        repo=RepoMetadata(
            path="/repo",
            git_commit="abc123",
            dirty=False,
        ),
        generated_at="2026-05-27T12:00:00Z",
        collector=CollectorMetadata(),
        summary=build_summary([finding]),
        findings=[finding],
    )

    assert report.schema_version == "agileforge.reconciliation_report.v1"
    assert report.repo is not None
    assert report.repo.path == "/repo"
    assert report.repo.git_commit == "abc123"
    assert report.collector.strategy == "exact_tag_match"
    assert report.collector.version == "agileforge.evidence_collect.v1"
    assert report.findings[0].spec_item_id == "REQ.budget-validation"
    assert report.findings[0].notes == [
        "Exact behavior reference found. Required test reference not found."
    ]
    assert report.summary == {
        "finding_count": 1,
        "evidenced": 0,
        "evidence_missing": 1,
        "missing": 0,
        "unknown": 0,
    }


def _report_payload(
    *,
    project_id: int = 2,
    fingerprint: str = "sha256:current",
) -> dict[str, object]:
    """Build a minimal importable reconciliation report payload."""
    return {
        "schema_version": "agileforge.reconciliation_report.v1",
        "project_id": project_id,
        "spec_version_id": 7,
        "compiled_authority_fingerprint": fingerprint,
        "repo": None,
        "generated_at": "2026-05-27T12:00:00Z",
        "collector": {"strategy": "manual", "version": "external.v1"},
        "summary": {
            "finding_count": 0,
            "evidenced": 0,
            "evidence_missing": 0,
            "missing": 0,
            "unknown": 0,
        },
        "findings": [],
    }


def test_targets_from_compiled_authority_uses_item_and_invariant_ids() -> None:
    """Verify normative items include related invariant IDs as match terms."""
    compiled = {
        "spec_version_id": 7,
        "items": [
            {
                "id": "REQ.budget-validation",
                "type": "REQ",
                "verification": "unit-test",
                "relations": [
                    {"type": "verifies", "target": "INV-budget-positive"},
                    {"type": "implements", "to": "INV-budget-cli"},
                    {"type": "satisfies", "target": "GOAL-budget"},
                    {
                        "type": "relates",
                        "target": "REQ.other",
                        "to": "INV-budget-shadow",
                    },
                ],
            },
            {
                "id": "GOAL.budget-control",
                "type": "GOAL",
                "verification": "inspection",
                "relations": [{"type": "verifies", "target": "INV-ignored"}],
            },
        ],
        "invariants": [{"id": "INV-budget-positive"}],
    }

    targets, warnings = evidence_collect_module.targets_from_compiled_authority(
        compiled
    )

    assert warnings == []
    assert targets == [
        evidence_collect_module.SpecEvidenceTarget(
            spec_item_id="REQ.budget-validation",
            item_type="REQ",
            verification_method="unit-test",
            matched_terms=[
                "INV-budget-cli",
                "INV-budget-positive",
                "INV-budget-shadow",
                "REQ.budget-validation",
            ],
        )
    ]


def test_targets_from_compiled_authority_keeps_only_normative_item_types() -> None:
    """Verify targets are created only for supported normative item types."""
    compiled = {
        "items": [
            {"id": "REQ.example", "type": "REQ"},
            {"id": "QUALITY.example", "type": "QUALITY"},
            {"id": "CONSTRAINT.example", "type": "CONSTRAINT"},
            {"id": "INTERFACE.example", "type": "INTERFACE"},
            {"id": "DATA.example", "type": "DATA"},
            {"id": "GOAL.example", "type": "GOAL"},
            {"id": "TERM.example", "type": "TERM"},
        ]
    }

    targets, warnings = evidence_collect_module.targets_from_compiled_authority(
        compiled
    )

    assert warnings == []
    assert [target.item_type for target in targets] == [
        "REQ",
        "QUALITY",
        "CONSTRAINT",
        "INTERFACE",
        "DATA",
    ]
    assert [target.verification_method for target in targets] == [
        "not-yet-defined",
        "not-yet-defined",
        "not-yet-defined",
        "not-yet-defined",
        "not-yet-defined",
    ]


def test_import_report_json_rejects_authority_fingerprint_mismatch() -> None:
    """Verify stale reports cannot be imported against current authority."""
    report = _report_payload(fingerprint="sha256:old")

    with pytest.raises(ValueError, match="authority fingerprint mismatch"):
        evidence_collect_module.import_report_json(
            json.dumps(report),
            project_id=2,
            current_authority_fingerprint="sha256:current",
        )


def test_import_report_json_rejects_project_id_mismatch() -> None:
    """Verify reports from another project are rejected."""
    report = _report_payload(project_id=3)

    with pytest.raises(ValueError, match="project_id mismatch"):
        evidence_collect_module.import_report_json(
            json.dumps(report),
            project_id=2,
            current_authority_fingerprint="sha256:current",
        )


def test_import_report_json_preserves_null_repo_and_external_collector() -> None:
    """Verify external reports keep metadata and warn when repo data is absent."""
    report = _report_payload()

    imported, warnings = evidence_collect_module.import_report_json(
        json.dumps(report),
        project_id=2,
        current_authority_fingerprint="sha256:current",
    )

    assert isinstance(imported, ReconciliationReport)
    assert imported.repo is None
    assert imported.collector.strategy == "manual"
    assert imported.collector.version == "external.v1"
    assert [warning.code for warning in warnings] == [
        "EVIDENCE_REPO_METADATA_MISSING"
    ]


class _WorkflowStub:
    """Workflow state stub used by evidence collection runner tests."""

    def __init__(self) -> None:
        self.state: dict[str, object] = {"fsm_state": "BACKLOG_INTERVIEW"}

    def get_session_status(self, session_id: str) -> dict[str, object]:
        """Return the current test workflow state."""
        _ = session_id
        return dict(self.state)

    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, object],
    ) -> None:
        """Merge a partial workflow state update."""
        _ = session_id
        self.state.update(partial_update)


class _ProductRepoStub:
    """Product repository stub used by evidence collection runner tests."""

    def get_by_id(self, product_id: int) -> object | None:
        """Return a product placeholder for positive project lookups."""
        return SimpleNamespace(product_id=product_id, name="Evidence Project")


def _seed_authority(engine: Engine) -> str:
    """Seed one accepted authority row for runner tests."""
    with Session(engine) as session:
        product = Product(name="Evidence Project")
        session.add(product)
        session.commit()
        spec = SpecRegistry(
            product_id=1,
            spec_hash="spec-hash",
            content="{}",
            status="approved",
            approved_at=datetime(2026, 5, 27, tzinfo=UTC),
        )
        session.add(spec)
        session.commit()
        authority = CompiledSpecAuthority(
            spec_version_id=1,
            compiler_version="1",
            prompt_hash="prompt",
            compiled_at=datetime(2026, 5, 27, tzinfo=UTC),
            compiled_artifact_json=json.dumps(
                {
                    "spec_version_id": 1,
                    "items": [
                        {
                            "id": "REQ.budget-validation",
                            "type": "REQ",
                            "verification": "unit-test",
                        }
                    ],
                }
            ),
            scope_themes="[]",
            invariants="[]",
            eligible_feature_ids="[]",
        )
        session.add(authority)
        session.commit()
        authority_fingerprint = pending_authority_fingerprint(authority)
        assert authority_fingerprint is not None
        session.add(
            SpecAuthorityAcceptance(
                product_id=1,
                spec_version_id=1,
                status="accepted",
                policy="test",
                decided_by="test",
                decided_at=datetime(2026, 5, 27, tzinfo=UTC),
                compiler_version="1",
                prompt_hash="prompt",
                spec_hash="spec-hash",
                pending_authority_id=1,
                authority_fingerprint=authority_fingerprint,
            )
        )
        session.commit()
        return authority_fingerprint


def test_runner_stores_report_and_event(tmp_path: Path) -> None:
    """Verify collection stores workflow cache and audit event."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    _seed_authority(engine)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "budget.py").write_text("# REQ.budget-validation\n", encoding="utf-8")
    workflow = _WorkflowStub()
    runner = EvidenceCollectionRunner(
        engine=engine,
        product_repo=_ProductRepoStub(),
        workflow_service=workflow,
    )

    result = runner.collect(
        project_id=1,
        repo_path=str(repo),
        from_file=None,
        idempotency_key="evidence-1",
    )

    assert result["ok"] is True
    assert IMPLEMENTATION_EVIDENCE_STATE_KEY in workflow.state
    assert _mapping(result["meta"])["command"] == EVIDENCE_COLLECT_COMMAND
    data = _mapping(result["data"])
    report = _mapping(data["report"])
    assert report["schema_version"] == "agileforge.reconciliation_report.v1"
    with Session(engine) as session:
        event = session.exec(select(WorkflowEvent)).one()
        assert event.event_type == WorkflowEventType.EVIDENCE_COLLECTED


def test_runner_replays_same_idempotency_key_for_same_import(
    tmp_path: Path,
) -> None:
    """Verify identical idempotent imports replay successfully."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    authority_fingerprint = _seed_authority(engine)
    report_file = tmp_path / "report.json"
    report_file.write_text(
        json.dumps(_report_payload(project_id=1, fingerprint=authority_fingerprint)),
        encoding="utf-8",
    )
    workflow = _WorkflowStub()
    runner = EvidenceCollectionRunner(
        engine=engine,
        product_repo=_ProductRepoStub(),
        workflow_service=workflow,
    )

    first = runner.collect(
        project_id=1,
        repo_path=None,
        from_file=str(report_file),
        idempotency_key="same-key",
    )
    second = runner.collect(
        project_id=1,
        repo_path=None,
        from_file=str(report_file),
        idempotency_key="same-key",
    )

    assert first["ok"] is True
    assert second["ok"] is True
    data = _mapping(second["data"])
    assert data["idempotent_replay"] is True


def test_runner_replays_original_report_after_later_collection(
    tmp_path: Path,
) -> None:
    """Verify replay uses immutable event data, not overwritten workflow cache."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    authority_fingerprint = _seed_authority(engine)
    first_file = tmp_path / "first.json"
    second_file = tmp_path / "second.json"
    first_payload = _report_payload(project_id=1, fingerprint=authority_fingerprint)
    first_payload["generated_at"] = "2026-05-27T12:00:00Z"
    second_payload = _report_payload(project_id=1, fingerprint=authority_fingerprint)
    second_payload["generated_at"] = "2026-05-27T12:02:00Z"
    first_file.write_text(json.dumps(first_payload), encoding="utf-8")
    second_file.write_text(json.dumps(second_payload), encoding="utf-8")
    workflow = _WorkflowStub()
    runner = EvidenceCollectionRunner(
        engine=engine,
        product_repo=_ProductRepoStub(),
        workflow_service=workflow,
    )

    assert runner.collect(
        project_id=1,
        repo_path=None,
        from_file=str(first_file),
        idempotency_key="first-key",
    )["ok"] is True
    assert runner.collect(
        project_id=1,
        repo_path=None,
        from_file=str(second_file),
        idempotency_key="second-key",
    )["ok"] is True
    replay = runner.collect(
        project_id=1,
        repo_path=None,
        from_file=str(first_file),
        idempotency_key="first-key",
    )

    assert replay["ok"] is True
    report = _mapping(_mapping(replay["data"])["report"])
    assert report["generated_at"] == "2026-05-27T12:00:00Z"


def test_runner_rejects_authority_row_mismatched_to_acceptance(
    tmp_path: Path,
) -> None:
    """Verify stale accepted decisions do not scan a changed authority row."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    _seed_authority(engine)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "budget.py").write_text("# REQ.budget-validation\n", encoding="utf-8")
    with Session(engine) as session:
        authority = session.get(CompiledSpecAuthority, 1)
        assert authority is not None
        authority.prompt_hash = "changed"
        session.add(authority)
        session.commit()
    runner = EvidenceCollectionRunner(
        engine=engine,
        product_repo=_ProductRepoStub(),
        workflow_service=_WorkflowStub(),
    )

    result = runner.collect(
        project_id=1,
        repo_path=str(repo),
        from_file=None,
        idempotency_key="mismatch",
    )

    assert result["ok"] is False
    assert _mapping(result["errors"][0])["code"] == "AUTHORITY_ACCEPTANCE_MISMATCH"


def test_runner_rejects_authority_artifact_mismatched_to_acceptance(
    tmp_path: Path,
) -> None:
    """Verify artifact changes cannot keep the old accepted fingerprint."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    _seed_authority(engine)
    repo = tmp_path / "repo"
    repo.mkdir()
    with Session(engine) as session:
        authority = session.get(CompiledSpecAuthority, 1)
        assert authority is not None
        authority.compiled_artifact_json = json.dumps(
            {
                "spec_version_id": 1,
                "items": [
                    {
                        "id": "REQ.changed",
                        "type": "REQ",
                        "verification": "unit-test",
                    }
                ],
            }
        )
        session.add(authority)
        session.commit()
    runner = EvidenceCollectionRunner(
        engine=engine,
        product_repo=_ProductRepoStub(),
        workflow_service=_WorkflowStub(),
    )

    result = runner.collect(
        project_id=1,
        repo_path=str(repo),
        from_file=None,
        idempotency_key="artifact-mismatch",
    )

    assert result["ok"] is False
    assert _mapping(result["errors"][0])["code"] == "AUTHORITY_ACCEPTANCE_MISMATCH"


def test_runner_returns_error_envelope_for_invalid_compiled_authority_json(
    tmp_path: Path,
) -> None:
    """Verify malformed authority JSON does not escape as an exception."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    _seed_authority(engine)
    repo = tmp_path / "repo"
    repo.mkdir()
    with Session(engine) as session:
        authority = session.get(CompiledSpecAuthority, 1)
        assert authority is not None
        authority.compiled_artifact_json = "{"
        session.add(authority)
        session.flush()
        acceptance = session.get(SpecAuthorityAcceptance, 1)
        assert acceptance is not None
        acceptance.authority_fingerprint = pending_authority_fingerprint(authority)
        session.add(acceptance)
        session.commit()
    runner = EvidenceCollectionRunner(
        engine=engine,
        product_repo=_ProductRepoStub(),
        workflow_service=_WorkflowStub(),
    )

    result = runner.collect(
        project_id=1,
        repo_path=str(repo),
        from_file=None,
        idempotency_key="invalid-json",
    )

    assert result["ok"] is False
    assert _mapping(result["errors"][0])["code"] == "AUTHORITY_NOT_COMPILED"


def test_runner_returns_error_envelope_for_non_object_compiled_authority_json(
    tmp_path: Path,
) -> None:
    """Verify non-object authority JSON fails closed."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    _seed_authority(engine)
    repo = tmp_path / "repo"
    repo.mkdir()
    with Session(engine) as session:
        authority = session.get(CompiledSpecAuthority, 1)
        assert authority is not None
        authority.compiled_artifact_json = "[]"
        session.add(authority)
        session.flush()
        acceptance = session.get(SpecAuthorityAcceptance, 1)
        assert acceptance is not None
        acceptance.authority_fingerprint = pending_authority_fingerprint(authority)
        session.add(acceptance)
        session.commit()
    runner = EvidenceCollectionRunner(
        engine=engine,
        product_repo=_ProductRepoStub(),
        workflow_service=_WorkflowStub(),
    )

    result = runner.collect(
        project_id=1,
        repo_path=str(repo),
        from_file=None,
        idempotency_key="non-object-json",
    )

    assert result["ok"] is False
    assert _mapping(result["errors"][0])["code"] == "AUTHORITY_NOT_COMPILED"


def test_runner_rejects_idempotency_key_reuse_with_changed_file(
    tmp_path: Path,
) -> None:
    """Verify reused keys with different imported content fail closed."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    authority_fingerprint = _seed_authority(engine)
    report_file = tmp_path / "report.json"
    base = _report_payload(project_id=1, fingerprint=authority_fingerprint)
    report_file.write_text(json.dumps(base), encoding="utf-8")
    workflow = _WorkflowStub()
    runner = EvidenceCollectionRunner(
        engine=engine,
        product_repo=_ProductRepoStub(),
        workflow_service=workflow,
    )

    assert runner.collect(
        project_id=1,
        repo_path=None,
        from_file=str(report_file),
        idempotency_key="same-key",
    )["ok"] is True
    changed = dict(base)
    changed["generated_at"] = "2026-05-27T12:01:00Z"
    report_file.write_text(json.dumps(changed), encoding="utf-8")
    result = runner.collect(
        project_id=1,
        repo_path=None,
        from_file=str(report_file),
        idempotency_key="same-key",
    )

    assert result["ok"] is False
    assert _mapping(result["errors"][0])["code"] == "IDEMPOTENCY_KEY_REUSED"


def _mapping(value: object) -> dict[str, object]:
    """Return a JSON object from an envelope field."""
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)
