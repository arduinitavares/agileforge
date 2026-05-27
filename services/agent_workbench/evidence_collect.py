"""Evidence collection contracts for backlog reconciliation reports."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.agent_workbench.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from collections.abc import Sequence

REPORT_SCHEMA_VERSION: str = "agileforge.reconciliation_report.v1"
COLLECTOR_STRATEGY: str = "exact_tag_match"
COLLECTOR_VERSION: str = "agileforge.evidence_collect.v1"
IMPLEMENTATION_EVIDENCE_STATE_KEY: str = "implementation_evidence_cached"
EVIDENCE_COLLECT_COMMAND: str = "agileforge evidence collect"

FindingStatus = Literal["evidenced", "evidence_missing", "missing", "unknown"]
EvidenceConfidence = Literal["medium", "low"]
ValidationState = Literal["not_run"]
EvidenceKind = Literal["source", "test", "doc", "config"]

TEST_REQUIRED_VERIFICATION_METHODS: frozenset[str] = frozenset(
    {
        "unit-test",
        "integration-test",
        "system-test",
        "acceptance-test",
    }
)
NON_TEST_VERIFICATION_METHODS: frozenset[str] = frozenset(
    {
        "inspection",
        "analysis",
        "manual-review",
        "monitoring",
    }
)
SUPPORTED_VERIFICATION_METHODS: frozenset[str] = (
    TEST_REQUIRED_VERIFICATION_METHODS | NON_TEST_VERIFICATION_METHODS
)
BEHAVIOR_EVIDENCE_KINDS: frozenset[EvidenceKind] = frozenset(
    {"source", "doc", "config"}
)


class EvidencePath(BaseModel):
    """A repository path that matched evidence terms for a finding."""

    model_config = ConfigDict(extra="forbid")

    path: str
    kind: EvidenceKind
    match_count: int = Field(ge=1)
    matched_terms: list[str]

    @field_validator("path")
    @classmethod
    def _path_must_be_non_empty(cls, value: str) -> str:
        """Require evidence to point at a concrete repository path."""
        normalized = value.strip()
        if not normalized:
            msg = "path must be non-empty"
            raise ValueError(msg)
        return normalized

    @field_validator("matched_terms")
    @classmethod
    def _matched_terms_must_be_non_empty(cls, value: list[str]) -> list[str]:
        """Require at least one matched evidence term."""
        normalized = [term.strip() for term in value if term.strip()]
        if not normalized:
            msg = "matched_terms must be non-empty"
            raise ValueError(msg)
        return normalized


class RepoMetadata(BaseModel):
    """Repository identity captured with a reconciliation report."""

    model_config = ConfigDict(extra="forbid")

    path: str
    git_commit: str | None = None
    dirty: bool = False

    @field_validator("path")
    @classmethod
    def _path_must_be_non_empty(cls, value: str) -> str:
        """Require a concrete repository root path."""
        normalized = value.strip()
        if not normalized:
            msg = "path must be non-empty"
            raise ValueError(msg)
        return normalized


class CollectorMetadata(BaseModel):
    """Collector identity and execution metadata for a report."""

    model_config = ConfigDict(extra="forbid")

    strategy: str = COLLECTOR_STRATEGY
    version: str = COLLECTOR_VERSION


class ReconciliationFinding(BaseModel):
    """Evidence status for one backlog reconciliation finding."""

    model_config = ConfigDict(extra="forbid")

    spec_item_id: str
    item_type: str
    verification_method: str
    status: FindingStatus
    confidence: EvidenceConfidence
    validation_state: ValidationState = "not_run"
    evidence_paths: list[EvidencePath] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @field_validator("spec_item_id", "item_type")
    @classmethod
    def _required_string_must_be_non_empty(cls, value: str) -> str:
        """Require stable finding identifiers."""
        normalized = value.strip()
        if not normalized:
            msg = "finding identifier fields must be non-empty"
            raise ValueError(msg)
        return normalized

    @field_validator("verification_method")
    @classmethod
    def _verification_method_must_be_non_empty(cls, value: str) -> str:
        """Require a verification method value, even when unsupported."""
        normalized = value.strip()
        if not normalized:
            msg = "verification_method must be non-empty"
            raise ValueError(msg)
        return normalized


class ReconciliationReport(BaseModel):
    """Versioned evidence reconciliation report."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agileforge.reconciliation_report.v1"] = (
        REPORT_SCHEMA_VERSION
    )
    project_id: int
    spec_version_id: int
    compiled_authority_fingerprint: str
    repo: RepoMetadata | None = None
    generated_at: str
    collector: CollectorMetadata = Field(default_factory=CollectorMetadata)
    summary: dict[str, int]
    findings: list[ReconciliationFinding]


def utc_now_iso() -> str:
    """Return the current UTC timestamp as a stable ISO-8601 string."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def canonical_report_json(report: ReconciliationReport) -> str:
    """Serialize a reconciliation report in canonical JSON form."""
    return canonical_json(report.model_dump(mode="json"))


def report_fingerprint(report: ReconciliationReport) -> str:
    """Return the canonical report fingerprint."""
    return canonical_hash(report.model_dump(mode="json"))


def classify_finding(
    evidence_paths: Sequence[EvidencePath],
    *,
    verification_method: str,
) -> tuple[FindingStatus, EvidenceConfidence]:
    """Classify evidence completeness for one verification method."""
    if verification_method not in SUPPORTED_VERIFICATION_METHODS:
        return ("unknown", "low")

    has_behavior_ref = any(
        evidence_path.kind in BEHAVIOR_EVIDENCE_KINDS
        for evidence_path in evidence_paths
    )
    has_test_ref = any(
        evidence_path.kind == "test" for evidence_path in evidence_paths
    )

    if not has_behavior_ref and not has_test_ref:
        return ("missing", "low")
    if (
        has_behavior_ref
        and verification_method in TEST_REQUIRED_VERIFICATION_METHODS
        and not has_test_ref
    ):
        return ("evidence_missing", "medium")
    if has_test_ref and not has_behavior_ref:
        return ("evidence_missing", "medium")
    if has_behavior_ref and (
        has_test_ref or verification_method in NON_TEST_VERIFICATION_METHODS
    ):
        return ("evidenced", "medium")
    return ("unknown", "low")


def build_summary(findings: Sequence[ReconciliationFinding]) -> dict[str, int]:
    """Build stable status and confidence counts for report consumers."""
    summary: dict[str, int] = {
        "finding_count": len(findings),
        "evidenced": 0,
        "evidence_missing": 0,
        "missing": 0,
        "unknown": 0,
    }
    for finding in findings:
        summary[finding.status] += 1
    return summary
