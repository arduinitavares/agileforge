"""Tests for evidence-aware reconciliation collection models."""

import pytest
from pydantic import ValidationError

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
