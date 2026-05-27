"""Tests for evidence-aware reconciliation collection models."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from services.agent_workbench import evidence_collect as evidence_collect_module
from services.agent_workbench.evidence_collect import (
    CollectorMetadata,
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
