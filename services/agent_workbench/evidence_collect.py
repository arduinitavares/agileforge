"""Evidence collection contracts for backlog reconciliation reports."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.agent_workbench.envelope import WorkbenchWarning
from services.agent_workbench.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from collections.abc import Sequence

REPORT_SCHEMA_VERSION: str = "agileforge.reconciliation_report.v1"
COLLECTOR_STRATEGY: str = "exact_tag_match"
COLLECTOR_VERSION: str = "agileforge.evidence_collect.v1"
IMPLEMENTATION_EVIDENCE_STATE_KEY: str = "implementation_evidence_cached"
EVIDENCE_COLLECT_COMMAND: str = "agileforge evidence collect"
MAX_SCAN_BYTES: int = 500 * 1024
NORMATIVE_ITEM_TYPES: set[str] = {
    "REQ",
    "QUALITY",
    "CONSTRAINT",
    "INTERFACE",
    "DATA",
}

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
SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "venv",
    }
)
SKIP_FILE_NAMES: frozenset[str] = frozenset(
    {
        "uv.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
    }
)
SKIP_FILE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".db",
        ".sqlite",
        ".sqlite3",
        ".pyc",
        ".pyo",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".pdf",
        ".zip",
        ".gz",
    }
)
CONFIG_FILE_NAMES: frozenset[str] = frozenset(
    {
        ".env.example",
        "pyproject.toml",
        "ruff.toml",
        "mypy.ini",
        "pytest.ini",
        "package.json",
        "tsconfig.json",
    }
)
DOC_DIR_NAMES: frozenset[str] = frozenset({"doc", "docs", "documentation"})
DOC_SUFFIXES: frozenset[str] = frozenset({".md", ".mdx", ".rst", ".txt"})
CONFIG_SUFFIXES: frozenset[str] = frozenset({".toml", ".yaml", ".yml"})
TEST_SOURCE_SUFFIXES: frozenset[str] = frozenset(
    {".py", ".js", ".jsx", ".ts", ".tsx"}
)
PYTHON_TEST_HELPER_NAMES: frozenset[str] = frozenset(
    {"test_helper.py", "test_helpers.py"}
)
JAVASCRIPT_TEST_SUFFIXES: tuple[str, ...] = (
    ".test.js",
    ".spec.js",
    ".test.ts",
    ".spec.ts",
    ".test.tsx",
    ".spec.tsx",
)


@dataclass(frozen=True)
class SpecEvidenceTarget:
    """Exact evidence terms to scan for one spec item."""

    spec_item_id: str
    item_type: str
    verification_method: str
    matched_terms: list[str]


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


def targets_from_compiled_authority(
    compiled_authority: dict[str, Any],
) -> tuple[list[SpecEvidenceTarget], list[WorkbenchWarning]]:
    """Extract exact evidence targets from compiled authority JSON."""
    raw_items = compiled_authority.get("items")
    if not isinstance(raw_items, list):
        return (
            [],
            [
                WorkbenchWarning(
                    code="EVIDENCE_AUTHORITY_ITEMS_MISSING",
                    message="Compiled authority has no items list.",
                )
            ],
        )

    targets: list[SpecEvidenceTarget] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue

        item_id = str(raw_item.get("id") or "").strip()
        item_type = str(raw_item.get("type") or "").strip()
        if not item_id or item_type not in NORMATIVE_ITEM_TYPES:
            continue

        verification_method = (
            str(raw_item.get("verification") or "not-yet-defined").strip()
            or "not-yet-defined"
        )
        matched_terms = {item_id}
        raw_relations = raw_item.get("relations")
        if isinstance(raw_relations, list):
            matched_terms.update(_invariant_terms_from_relations(raw_relations))

        targets.append(
            SpecEvidenceTarget(
                spec_item_id=item_id,
                item_type=item_type,
                verification_method=verification_method,
                matched_terms=sorted(matched_terms),
            )
        )

    warnings: list[WorkbenchWarning] = []
    if not targets:
        warnings.append(
            WorkbenchWarning(
                code="EVIDENCE_TARGETS_EMPTY",
                message="No supported normative spec items were found.",
            )
        )
    return (targets, warnings)


def import_report_json(
    raw_json: str,
    *,
    project_id: int,
    current_authority_fingerprint: str,
) -> tuple[ReconciliationReport, list[WorkbenchWarning]]:
    """Validate and import a reconciliation report JSON string."""
    payload = json.loads(raw_json)
    report = ReconciliationReport.model_validate(payload)
    if report.project_id != project_id:
        msg = "project_id mismatch"
        raise ValueError(msg)
    if report.compiled_authority_fingerprint != current_authority_fingerprint:
        msg = "authority fingerprint mismatch"
        raise ValueError(msg)

    warnings: list[WorkbenchWarning] = []
    if report.repo is None:
        warnings.append(
            WorkbenchWarning(
                code="EVIDENCE_REPO_METADATA_MISSING",
                message="Imported report has no repo metadata.",
            )
        )
    return (report, warnings)


def _invariant_terms_from_relations(raw_relations: list[object]) -> set[str]:
    """Extract invariant reference terms from authority item relations."""
    invariant_terms: set[str] = set()
    for raw_relation in raw_relations:
        if not isinstance(raw_relation, dict):
            continue
        for relation_key in ("target", "to"):
            relation_target = str(raw_relation.get(relation_key) or "").strip()
            if relation_target.startswith("INV-"):
                invariant_terms.add(relation_target)
    return invariant_terms


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


def file_kind_for_path(path: Path) -> EvidenceKind:
    """Return the evidence kind for a repository-relative path."""
    path_parts = path.parts
    parent_parts = path_parts[:-1]
    name = path.name
    lower_name = name.lower()
    lower_suffix = path.suffix.lower()

    if lower_suffix in DOC_SUFFIXES or (
        path_parts and path_parts[0] in DOC_DIR_NAMES
    ):
        return "doc"
    if lower_name in CONFIG_FILE_NAMES or lower_suffix in CONFIG_SUFFIXES:
        return "config"
    if _is_test_file_name(lower_name):
        return "test"
    if lower_suffix in TEST_SOURCE_SUFFIXES and any(
        part in {"test", "tests"} for part in parent_parts
    ):
        return "test"
    return "source"


def collect_repo_evidence(
    repo_path: Path,
    targets: list[SpecEvidenceTarget],
) -> tuple[list[ReconciliationFinding], list[WorkbenchWarning]]:
    """Scan a repository for exact target evidence references."""
    evidence_by_target: list[list[EvidencePath]] = [[] for _ in targets]
    warnings: list[WorkbenchWarning] = []

    files, traversal_warnings = _iter_scannable_files(repo_path)
    warnings.extend(traversal_warnings)
    for file_path, relative_path in files:
        skip_reason = _skip_file_reason(file_path, relative_path)
        if skip_reason is not None:
            warnings.append(_skipped_file_warning(relative_path, skip_reason))
            continue

        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            warnings.append(_unreadable_file_warning(relative_path, str(exc)))
            continue
        except OSError as exc:
            warnings.append(_unreadable_file_warning(relative_path, str(exc)))
            continue

        for target_index, target in enumerate(targets):
            evidence_path = _evidence_path_for_content(relative_path, content, target)
            if evidence_path is not None:
                evidence_by_target[target_index].append(evidence_path)

    findings: list[ReconciliationFinding] = []
    for target, evidence_paths in zip(targets, evidence_by_target, strict=True):
        status, confidence = classify_finding(
            evidence_paths,
            verification_method=target.verification_method,
        )
        findings.append(
            ReconciliationFinding(
                spec_item_id=target.spec_item_id,
                item_type=target.item_type,
                verification_method=target.verification_method,
                status=status,
                confidence=confidence,
                validation_state="not_run",
                evidence_paths=evidence_paths,
                notes=_finding_notes(
                    status,
                    evidence_paths,
                    verification_method=target.verification_method,
                ),
            )
        )

    return (findings, warnings)


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


def _is_test_file_name(lower_name: str) -> bool:
    """Return whether a file name is an exact test file pattern."""
    if lower_name in PYTHON_TEST_HELPER_NAMES:
        return False
    if lower_name.startswith("test_") and lower_name.endswith(".py"):
        return True
    if lower_name.endswith("_test.py"):
        return True
    return lower_name.endswith(JAVASCRIPT_TEST_SUFFIXES)


def _iter_scannable_files(
    repo_path: Path,
) -> tuple[Sequence[tuple[Path, Path]], list[WorkbenchWarning]]:
    """Yield repository files in deterministic order while pruning skip dirs."""
    repo_root = repo_path.resolve()
    scannable_files: list[tuple[Path, Path]] = []
    warnings: list[WorkbenchWarning] = []

    def onerror(error: OSError) -> None:
        raw_path = Path(error.filename or repo_root)
        try:
            relative_path = raw_path.relative_to(repo_root)
        except ValueError:
            relative_path = raw_path
        warnings.append(_unreadable_file_warning(relative_path, str(error)))

    for root, dirnames, filenames in os.walk(repo_root, onerror=onerror):
        dirnames[:] = sorted(
            dirname for dirname in dirnames if dirname not in SKIP_DIR_NAMES
        )
        root_path = Path(root)
        for filename in sorted(filenames):
            file_path = root_path / filename
            relative_path = file_path.relative_to(repo_root)
            scannable_files.append((file_path, relative_path))
    return scannable_files, warnings


def _skip_file_reason(file_path: Path, relative_path: Path) -> str | None:
    """Return a skip reason for files that should not be scanned."""
    lower_name = relative_path.name.lower()
    lower_suffix = relative_path.suffix.lower()

    if lower_name in SKIP_FILE_NAMES:
        return "file_name"
    if lower_suffix in SKIP_FILE_SUFFIXES:
        return "file_suffix"

    try:
        size = file_path.stat().st_size
    except OSError:
        return None
    if size > MAX_SCAN_BYTES:
        return "file_too_large"
    return None


def _evidence_path_for_content(
    relative_path: Path,
    content: str,
    target: SpecEvidenceTarget,
) -> EvidencePath | None:
    """Return an evidence path when content matches any target term."""
    counts: dict[str, int] = {}
    for term in _target_terms(target):
        count = _count_exact_term(content, term)
        if count > 0:
            counts[term] = count
    if not counts:
        return None
    return EvidencePath(
        path=relative_path.as_posix(),
        kind=file_kind_for_path(relative_path),
        match_count=sum(counts.values()),
        matched_terms=sorted(counts),
    )


def _target_terms(target: SpecEvidenceTarget) -> list[str]:
    """Return unique, non-empty target terms in deterministic order."""
    return sorted({term.strip() for term in target.matched_terms if term.strip()})


def _count_exact_term(content: str, term: str) -> int:
    """Count exact metadata-tag occurrences without prefix substring matches."""
    escaped = re.escape(term)
    pattern = rf"(?<![A-Za-z0-9_.-]){escaped}(?![A-Za-z0-9_.-])"
    return len(re.findall(pattern, content))


def _finding_notes(
    status: FindingStatus,
    evidence_paths: Sequence[EvidencePath],
    *,
    verification_method: str,
) -> list[str]:
    """Return stable explanatory notes for one finding classification."""
    has_test_ref = any(
        evidence_path.kind == "test" for evidence_path in evidence_paths
    )
    if status == "evidenced":
        return ["Exact reference evidence found. Tests were not executed."]
    if status == "evidence_missing" and has_test_ref:
        return ["Exact test reference found. No behavior/source reference found."]
    if status == "evidence_missing":
        return ["Exact behavior reference found. Required test reference not found."]
    if status == "missing":
        return [
            "No exact references found. Absence of tags is low-confidence evidence."
        ]
    return [
        f"Evidence classification is unknown for verification method "
        f"{verification_method!r}."
    ]


def _skipped_file_warning(relative_path: Path, reason: str) -> WorkbenchWarning:
    """Return a warning for a skipped repository file."""
    details: dict[str, object] = {
        "path": relative_path.as_posix(),
        "reason": reason,
    }
    if reason == "file_too_large":
        details["max_scan_bytes"] = MAX_SCAN_BYTES
    return WorkbenchWarning(
        code="EVIDENCE_FILE_SKIPPED",
        message="Repository evidence file was skipped.",
        details=details,
        remediation=["Reference a smaller UTF-8 text file for exact evidence tags."],
    )


def _unreadable_file_warning(relative_path: Path, reason: str) -> WorkbenchWarning:
    """Return a warning for a repository file that could not be decoded."""
    return WorkbenchWarning(
        code="EVIDENCE_FILE_UNREADABLE",
        message="Repository evidence file could not be read as UTF-8 text.",
        details={
            "path": relative_path.as_posix(),
            "reason": reason,
        },
        remediation=["Ensure evidence files are readable UTF-8 text."],
    )
