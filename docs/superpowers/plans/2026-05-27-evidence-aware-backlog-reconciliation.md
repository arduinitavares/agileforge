# Evidence-Aware Backlog Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Phase 1a evidence collection that stores a transient `ReconciliationReport` in workflow state and feeds it to backlog generation.

**Architecture:** Add a focused `services.agent_workbench.evidence_collect` module for schema, classification, exact-match scanning, report import, idempotency replay, and workflow-state persistence. Expose it through `AgentWorkbenchApplication` and `agileforge evidence collect`. Backlog generation consumes `workflow_state["implementation_evidence_cached"]` as advisory context.

**Tech Stack:** Python 3.12, Pydantic, SQLModel, argparse CLI, existing AgileForge workflow state and `WorkflowEvent` audit patterns, `uv run pytest`.

---

## File Structure

- Create `services/agent_workbench/evidence_collect.py`
  - Owns `ReconciliationReport` Pydantic schemas.
  - Owns exact-match classification and repo scanning.
  - Owns `EvidenceCollectionRunner`.
  - Owns workflow-state storage and `EVIDENCE_COLLECTED` idempotent replay.
- Modify `models/enums.py`
  - Add `WorkflowEventType.EVIDENCE_COLLECTED`.
- Modify `services/agent_workbench/application.py`
  - Add `_EvidenceCollectionRunner` protocol, lazy runner, and `evidence_collect(...)` facade method.
- Modify `cli/main.py`
  - Add `evidence collect` parser, application protocol method, and command handler.
- Modify `services/agent_workbench/command_registry.py`
  - Register `agileforge evidence collect` as a mutating command requiring idempotency.
- Modify `orchestrator_agent/agent_tools/backlog_primer/schemes.py`
  - Add `implementation_evidence` to `InputSchema`.
- Modify `services/backlog_runtime.py`
  - Populate `implementation_evidence` from workflow state or `NO_EVIDENCE`.
- Modify `orchestrator_agent/agent_tools/backlog_primer/instructions.txt`
  - Add evidence-aware backlog guidance.
- Modify tests:
  - `tests/test_evidence_collect.py`
  - `tests/test_backlog_primer_agent.py`
  - `tests/test_agent_workbench_backlog_phase.py`
  - `tests/test_agent_workbench_phase1_integration.py`
  - `tests/test_model_package_boundary.py` if enum/import-boundary assertions require updates.

---

### Task 1: Report Schema And Classification Core

**Files:**
- Create: `services/agent_workbench/evidence_collect.py`
- Create: `tests/test_evidence_collect.py`

- [ ] **Step 1: Write failing schema/classification tests**

Add this file:

```python
# tests/test_evidence_collect.py

from __future__ import annotations

from services.agent_workbench.evidence_collect import (
    EvidencePath,
    classify_finding,
)


def test_classifies_source_and_test_reference_as_evidenced() -> None:
    status, confidence = classify_finding(
        verification_method="unit-test",
        evidence_paths=[
            EvidencePath(
                path="src/budget.py",
                kind="source",
                match_count=1,
                matched_terms=["REQ.budget-validation"],
            ),
            EvidencePath(
                path="tests/test_budget.py",
                kind="test",
                match_count=1,
                matched_terms=["REQ.budget-validation"],
            ),
        ],
    )

    assert status == "evidenced"
    assert confidence == "medium"


def test_classifies_source_without_required_test_as_evidence_missing() -> None:
    status, confidence = classify_finding(
        verification_method="unit-test",
        evidence_paths=[
            EvidencePath(
                path="src/budget.py",
                kind="source",
                match_count=1,
                matched_terms=["REQ.budget-validation"],
            )
        ],
    )

    assert status == "evidence_missing"
    assert confidence == "medium"


def test_classifies_test_without_source_as_evidence_missing() -> None:
    status, confidence = classify_finding(
        verification_method="unit-test",
        evidence_paths=[
            EvidencePath(
                path="tests/test_budget.py",
                kind="test",
                match_count=1,
                matched_terms=["REQ.budget-validation"],
            )
        ],
    )

    assert status == "evidence_missing"
    assert confidence == "medium"


def test_classifies_absent_references_as_missing_low_confidence() -> None:
    status, confidence = classify_finding(
        verification_method="unit-test",
        evidence_paths=[],
    )

    assert status == "missing"
    assert confidence == "low"


def test_classifies_unsupported_verification_as_unknown() -> None:
    status, confidence = classify_finding(
        verification_method="not-yet-defined",
        evidence_paths=[
            EvidencePath(
                path="src/budget.py",
                kind="source",
                match_count=1,
                matched_terms=["REQ.budget-validation"],
            )
        ],
    )

    assert status == "unknown"
    assert confidence == "low"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_evidence_collect.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'services.agent_workbench.evidence_collect'`.

- [ ] **Step 3: Implement report schema and classifier**

Create `services/agent_workbench/evidence_collect.py`:

```python
"""Evidence collection for Phase 1a backlog reconciliation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.agent_workbench.fingerprints import canonical_hash, canonical_json

REPORT_SCHEMA_VERSION = "agileforge.reconciliation_report.v1"
COLLECTOR_STRATEGY = "exact_tag_match"
COLLECTOR_VERSION = "agileforge.evidence_collect.v1"
IMPLEMENTATION_EVIDENCE_STATE_KEY = "implementation_evidence_cached"
EVIDENCE_COLLECT_COMMAND = "agileforge evidence collect"

FindingStatus = Literal["evidenced", "evidence_missing", "missing", "unknown"]
EvidenceConfidence = Literal["medium", "low"]
ValidationState = Literal["not_run"]
EvidenceKind = Literal["source", "test", "doc", "config"]

TEST_REQUIRED_VERIFICATION_METHODS = frozenset(
    {"unit-test", "integration-test", "system-test", "acceptance-test"}
)
NON_TEST_VERIFICATION_METHODS = frozenset(
    {"inspection", "analysis", "manual-review", "monitoring"}
)
SUPPORTED_VERIFICATION_METHODS = (
    TEST_REQUIRED_VERIFICATION_METHODS | NON_TEST_VERIFICATION_METHODS
)


class EvidencePath(BaseModel):
    """One exact-match evidence location."""

    model_config = ConfigDict(extra="forbid")

    path: str
    kind: EvidenceKind
    match_count: int = Field(ge=1)
    matched_terms: list[str] = Field(default_factory=list)

    @field_validator("path")
    @classmethod
    def _path_not_empty(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("path must not be empty")
        return trimmed

    @field_validator("matched_terms")
    @classmethod
    def _terms_not_empty(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        if not normalized:
            raise ValueError("matched_terms must not be empty")
        return normalized


class RepoMetadata(BaseModel):
    """Repository state captured when evidence was collected."""

    model_config = ConfigDict(extra="forbid")

    path: str
    git_commit: str | None = None
    dirty: bool = False


class CollectorMetadata(BaseModel):
    """Collector implementation identity."""

    model_config = ConfigDict(extra="forbid")

    strategy: str = COLLECTOR_STRATEGY
    version: str = COLLECTOR_VERSION


class ReconciliationFinding(BaseModel):
    """One spec item assessment based on exact reference evidence."""

    model_config = ConfigDict(extra="forbid")

    spec_item_id: str
    item_type: str
    verification_method: str
    status: FindingStatus
    confidence: EvidenceConfidence
    validation_state: ValidationState = "not_run"
    evidence_paths: list[EvidencePath] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ReconciliationReport(BaseModel):
    """Raw transient evidence report stored in workflow state."""

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
    """Return canonical UTC timestamp for reports."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_report_json(report: ReconciliationReport) -> str:
    """Return canonical report JSON for workflow-state storage."""
    return canonical_json(report.model_dump(mode="json"))


def report_fingerprint(report: ReconciliationReport) -> str:
    """Return stable fingerprint for a report."""
    return canonical_hash(report.model_dump(mode="json"))


def classify_finding(
    *,
    verification_method: str,
    evidence_paths: list[EvidencePath],
) -> tuple[FindingStatus, EvidenceConfidence]:
    """Classify exact-match evidence without claiming runtime correctness."""
    normalized_method = verification_method.strip().lower()
    has_behavior_ref = any(
        path.kind in {"source", "doc", "config"} for path in evidence_paths
    )
    has_test_ref = any(path.kind == "test" for path in evidence_paths)

    if normalized_method not in SUPPORTED_VERIFICATION_METHODS:
        return "unknown", "low"

    needs_test = normalized_method in TEST_REQUIRED_VERIFICATION_METHODS
    if not has_behavior_ref and not has_test_ref:
        return "missing", "low"
    if has_behavior_ref and needs_test and not has_test_ref:
        return "evidence_missing", "medium"
    if has_test_ref and not has_behavior_ref:
        return "evidence_missing", "medium"
    if has_behavior_ref and (has_test_ref or not needs_test):
        return "evidenced", "medium"
    return "unknown", "low"


def build_summary(findings: list[ReconciliationFinding]) -> dict[str, int]:
    """Return status counts for a report."""
    summary = {
        "finding_count": len(findings),
        "evidenced": 0,
        "evidence_missing": 0,
        "missing": 0,
        "unknown": 0,
    }
    for finding in findings:
        summary[finding.status] += 1
    return summary
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/test_evidence_collect.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/agent_workbench/evidence_collect.py tests/test_evidence_collect.py
git commit -m "feat: add reconciliation report schema"
```

---

### Task 2: Exact-Match Repo Scanner

**Files:**
- Modify: `services/agent_workbench/evidence_collect.py`
- Modify: `tests/test_evidence_collect.py`

- [ ] **Step 1: Add failing scanner tests**

Append to `tests/test_evidence_collect.py`:

```python
from pathlib import Path

from services.agent_workbench.evidence_collect import (
    SpecEvidenceTarget,
    collect_repo_evidence,
    file_kind_for_path,
)


def test_file_kind_requires_exact_test_directory_or_test_filename() -> None:
    assert file_kind_for_path(Path("tests/test_budget.py")) == "test"
    assert file_kind_for_path(Path("src/test_helpers.py")) == "source"
    assert file_kind_for_path(Path("src/config/test_db.js")) == "source"
    assert file_kind_for_path(Path("src/budget.test.js")) == "test"
    assert file_kind_for_path(Path("docs/budget.md")) == "doc"
    assert file_kind_for_path(Path("pyproject.toml")) == "config"


def test_collect_repo_evidence_uses_invariant_terms_as_equivalent_matches(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "budget.py").write_text("# INV-abc123\n", encoding="utf-8")
    (tmp_path / "tests" / "test_budget.py").write_text(
        "# REQ.budget-validation\n",
        encoding="utf-8",
    )

    findings, warnings = collect_repo_evidence(
        repo_path=tmp_path,
        targets=[
            SpecEvidenceTarget(
                spec_item_id="REQ.budget-validation",
                item_type="REQ",
                verification_method="unit-test",
                matched_terms=["REQ.budget-validation", "INV-abc123"],
            )
        ],
    )

    assert warnings == []
    assert findings[0].status == "evidenced"
    assert findings[0].confidence == "medium"
    assert {path.kind for path in findings[0].evidence_paths} == {"source", "test"}


def test_collect_repo_evidence_skips_database_lock_binary_and_large_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "budget.py").write_text(
        "# REQ.budget-validation\n",
        encoding="utf-8",
    )
    (tmp_path / "agileforge.db").write_bytes(b"REQ.budget-validation")
    (tmp_path / "uv.lock").write_text("REQ.budget-validation", encoding="utf-8")
    (tmp_path / "large.txt").write_text("x" * 600_000, encoding="utf-8")

    findings, warnings = collect_repo_evidence(
        repo_path=tmp_path,
        targets=[
            SpecEvidenceTarget(
                spec_item_id="REQ.budget-validation",
                item_type="REQ",
                verification_method="unit-test",
                matched_terms=["REQ.budget-validation"],
            )
        ],
    )

    assert findings[0].status == "evidence_missing"
    assert [path.path for path in findings[0].evidence_paths] == ["src/budget.py"]
    assert {warning.code for warning in warnings} == {"EVIDENCE_FILE_SKIPPED"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_evidence_collect.py -q
```

Expected: FAIL with missing `SpecEvidenceTarget`, `collect_repo_evidence`, or `file_kind_for_path`.

- [ ] **Step 3: Implement file classification and scanner**

Append/import in `services/agent_workbench/evidence_collect.py`:

```python
from collections import Counter
from dataclasses import dataclass

from services.agent_workbench.envelope import WorkbenchWarning

MAX_SCAN_BYTES = 500 * 1024
SKIP_DIR_NAMES = {
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
SKIP_FILE_NAMES = {"uv.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"}
SKIP_SUFFIXES = {
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
CONFIG_FILE_NAMES = {
    ".env.example",
    "pyproject.toml",
    "ruff.toml",
    "mypy.ini",
    "pytest.ini",
    "package.json",
    "tsconfig.json",
}


@dataclass(frozen=True)
class SpecEvidenceTarget:
    """Search terms for one normative spec item."""

    spec_item_id: str
    item_type: str
    verification_method: str
    matched_terms: list[str]


def file_kind_for_path(path: Path) -> EvidenceKind:
    """Classify a relative path into an evidence kind."""
    parts = path.parts
    if any(part in {"test", "tests"} for part in parts[:-1]):
        return "test"
    name = path.name
    suffixes = "".join(path.suffixes)
    if (
        name.startswith("test_")
        or name.endswith("_test.py")
        or suffixes.endswith(".test.js")
        or suffixes.endswith(".spec.js")
        or suffixes.endswith(".test.ts")
        or suffixes.endswith(".spec.ts")
        or suffixes.endswith(".test.tsx")
        or suffixes.endswith(".spec.tsx")
    ):
        return "test"
    if path.suffix.lower() in {".md", ".mdx", ".rst", ".txt"} or (
        parts and parts[0] in {"doc", "docs", "documentation"}
    ):
        return "doc"
    if name in CONFIG_FILE_NAMES or path.suffix.lower() in {".toml", ".yaml", ".yml"}:
        return "config"
    return "source"


def _should_skip_file(path: Path) -> bool:
    if path.name in SKIP_FILE_NAMES:
        return True
    return path.suffix.lower() in SKIP_SUFFIXES


def _iter_text_files(repo_path: Path) -> tuple[list[Path], list[WorkbenchWarning]]:
    warnings: list[WorkbenchWarning] = []
    files: list[Path] = []
    for path in sorted(repo_path.rglob("*")):
        if any(part in SKIP_DIR_NAMES for part in path.relative_to(repo_path).parts):
            continue
        if not path.is_file():
            continue
        rel_path = path.relative_to(repo_path)
        if _should_skip_file(path):
            warnings.append(
                WorkbenchWarning(
                    code="EVIDENCE_FILE_SKIPPED",
                    message="Skipped non-source evidence file.",
                    details={"path": str(rel_path), "reason": "skip_pattern"},
                )
            )
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            warnings.append(
                WorkbenchWarning(
                    code="EVIDENCE_FILE_UNREADABLE",
                    message="Could not stat evidence file.",
                    details={"path": str(rel_path), "error": str(exc)},
                )
            )
            continue
        if size > MAX_SCAN_BYTES:
            warnings.append(
                WorkbenchWarning(
                    code="EVIDENCE_FILE_SKIPPED",
                    message="Skipped oversized evidence file.",
                    details={"path": str(rel_path), "size_bytes": size},
                )
            )
            continue
        files.append(path)
    return files, warnings


def _matches_in_file(path: Path, terms: list[str]) -> Counter[str]:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return Counter()
    return Counter({term: content.count(term) for term in terms if content.count(term)})


def collect_repo_evidence(
    *,
    repo_path: Path,
    targets: list[SpecEvidenceTarget],
) -> tuple[list[ReconciliationFinding], list[WorkbenchWarning]]:
    """Collect exact reference evidence for spec targets in a repository."""
    files, warnings = _iter_text_files(repo_path)
    findings: list[ReconciliationFinding] = []
    for target in targets:
        evidence_paths: list[EvidencePath] = []
        for path in files:
            counts = _matches_in_file(path, target.matched_terms)
            if not counts:
                continue
            rel_path = path.relative_to(repo_path)
            evidence_paths.append(
                EvidencePath(
                    path=str(rel_path),
                    kind=file_kind_for_path(rel_path),
                    match_count=sum(counts.values()),
                    matched_terms=sorted(counts),
                )
            )
        status, confidence = classify_finding(
            verification_method=target.verification_method,
            evidence_paths=evidence_paths,
        )
        notes = _finding_notes(
            status=status,
            evidence_paths=evidence_paths,
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
                notes=notes,
            )
        )
    return findings, warnings


def _finding_notes(
    *,
    status: FindingStatus,
    evidence_paths: list[EvidencePath],
    verification_method: str,
) -> list[str]:
    if status == "evidenced":
        return ["Exact reference evidence found. Tests were not executed."]
    if status == "evidence_missing":
        has_test = any(path.kind == "test" for path in evidence_paths)
        if has_test:
            return ["Exact test reference found. No behavior/source reference found."]
        return ["Exact behavior reference found. Required test reference not found."]
    if status == "missing":
        return ["No exact references found. Absence of tags is low-confidence evidence."]
    return [f"Collector could not classify verification method {verification_method!r}."]
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
uv run pytest tests/test_evidence_collect.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/agent_workbench/evidence_collect.py tests/test_evidence_collect.py
git commit -m "feat: collect exact evidence references"
```

---

### Task 3: Authority Target Extraction And Report Import

**Files:**
- Modify: `services/agent_workbench/evidence_collect.py`
- Modify: `tests/test_evidence_collect.py`

- [ ] **Step 1: Add failing tests for target extraction and import validation**

Append to `tests/test_evidence_collect.py`:

```python
import json

import pytest

from services.agent_workbench.evidence_collect import (
    ReconciliationReport,
    import_report_json,
    targets_from_compiled_authority,
)


def test_targets_from_compiled_authority_uses_item_and_invariant_ids() -> None:
    compiled = {
        "spec_version_id": 7,
        "items": [
            {
                "id": "REQ.budget-validation",
                "type": "REQ",
                "verification": "unit-test",
                "relations": [
                    {"type": "verifies", "target": "INV-budget-positive"},
                    {"type": "implements", "target": "INV-budget-cli"},
                ],
            }
        ],
        "invariants": [{"id": "INV-budget-positive"}],
    }

    targets, warnings = targets_from_compiled_authority(compiled)

    assert warnings == []
    assert targets == [
        SpecEvidenceTarget(
            spec_item_id="REQ.budget-validation",
            item_type="REQ",
            verification_method="unit-test",
            matched_terms=[
                "REQ.budget-validation",
                "INV-budget-cli",
                "INV-budget-positive",
            ],
        )
    ]


def test_import_report_json_rejects_authority_fingerprint_mismatch() -> None:
    report = {
        "schema_version": "agileforge.reconciliation_report.v1",
        "project_id": 2,
        "spec_version_id": 7,
        "compiled_authority_fingerprint": "sha256:old",
        "repo": None,
        "generated_at": "2026-05-27T12:00:00Z",
        "collector": {"strategy": "manual", "version": "external.v1"},
        "summary": {"finding_count": 0, "evidenced": 0, "evidence_missing": 0, "missing": 0, "unknown": 0},
        "findings": [],
    }

    with pytest.raises(ValueError, match="authority fingerprint mismatch"):
        import_report_json(
            json.dumps(report),
            project_id=2,
            current_authority_fingerprint="sha256:new",
        )


def test_import_report_json_preserves_null_repo_and_external_collector() -> None:
    report = {
        "schema_version": "agileforge.reconciliation_report.v1",
        "project_id": 2,
        "spec_version_id": 7,
        "compiled_authority_fingerprint": "sha256:current",
        "repo": None,
        "generated_at": "2026-05-27T12:00:00Z",
        "collector": {"strategy": "manual", "version": "external.v1"},
        "summary": {"finding_count": 0, "evidenced": 0, "evidence_missing": 0, "missing": 0, "unknown": 0},
        "findings": [],
    }

    imported, warnings = import_report_json(
        json.dumps(report),
        project_id=2,
        current_authority_fingerprint="sha256:current",
    )

    assert isinstance(imported, ReconciliationReport)
    assert imported.repo is None
    assert imported.collector.strategy == "manual"
    assert [warning.code for warning in warnings] == ["EVIDENCE_REPO_METADATA_MISSING"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_evidence_collect.py -q
```

Expected: FAIL with missing `targets_from_compiled_authority` and `import_report_json`.

- [ ] **Step 3: Implement target extraction and report import**

Append to `services/agent_workbench/evidence_collect.py`:

```python
import json

NORMATIVE_ITEM_TYPES = {"REQ", "QUALITY", "CONSTRAINT", "INTERFACE", "DATA"}


def targets_from_compiled_authority(
    compiled_authority: dict[str, Any],
) -> tuple[list[SpecEvidenceTarget], list[WorkbenchWarning]]:
    """Extract exact search terms from compiled authority JSON."""
    warnings: list[WorkbenchWarning] = []
    raw_items = compiled_authority.get("items")
    if not isinstance(raw_items, list):
        warnings.append(
            WorkbenchWarning(
                code="EVIDENCE_AUTHORITY_ITEMS_MISSING",
                message="Compiled authority has no items list.",
            )
        )
        return [], warnings

    targets: list[SpecEvidenceTarget] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        item_id = str(raw_item.get("id") or "").strip()
        item_type = str(raw_item.get("type") or "").strip()
        if not item_id or item_type not in NORMATIVE_ITEM_TYPES:
            continue
        verification = str(raw_item.get("verification") or "not-yet-defined").strip()
        matched_terms = {item_id}
        relations = raw_item.get("relations")
        if isinstance(relations, list):
            for relation in relations:
                if not isinstance(relation, dict):
                    continue
                target = str(relation.get("target") or relation.get("to") or "").strip()
                if target.startswith("INV-"):
                    matched_terms.add(target)
        targets.append(
            SpecEvidenceTarget(
                spec_item_id=item_id,
                item_type=item_type,
                verification_method=verification,
                matched_terms=sorted(matched_terms),
            )
        )

    if not targets:
        warnings.append(
            WorkbenchWarning(
                code="EVIDENCE_TARGETS_EMPTY",
                message="No supported normative spec items were found.",
            )
        )
    return targets, warnings


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
        raise ValueError("project_id mismatch")
    if report.compiled_authority_fingerprint != current_authority_fingerprint:
        raise ValueError("authority fingerprint mismatch")

    warnings: list[WorkbenchWarning] = []
    if report.repo is None:
        warnings.append(
            WorkbenchWarning(
                code="EVIDENCE_REPO_METADATA_MISSING",
                message="Imported report has no repo metadata.",
            )
        )
    return report, warnings
```

- [ ] **Step 4: Run tests**

Run:

```bash
uv run pytest tests/test_evidence_collect.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/agent_workbench/evidence_collect.py tests/test_evidence_collect.py
git commit -m "feat: import evidence reports"
```

---

### Task 4: Evidence Collection Runner With Workflow State And Idempotency

**Files:**
- Modify: `models/enums.py`
- Modify: `services/agent_workbench/evidence_collect.py`
- Modify: `tests/test_evidence_collect.py`

- [ ] **Step 1: Add failing runner tests**

Append to `tests/test_evidence_collect.py`:

```python
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlmodel import Session, SQLModel, create_engine, select

from models.core import Product
from models.enums import WorkflowEventType
from models.events import WorkflowEvent
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry
from services.agent_workbench.evidence_collect import EvidenceCollectionRunner


class _WorkflowStub:
    def __init__(self) -> None:
        self.state: dict[str, object] = {"fsm_state": "BACKLOG_INTERVIEW"}

    def get_session_status(self, session_id: str) -> dict[str, object]:
        return dict(self.state)

    async def initialize_session(self, *, session_id: str) -> object:
        self.state["fsm_state"] = "BACKLOG_INTERVIEW"
        return session_id

    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, object],
    ) -> None:
        self.state.update(partial_update)


class _ProductRepoStub:
    def get_by_id(self, product_id: int) -> object | None:
        return SimpleNamespace(product_id=product_id, name="Evidence Project")


def _seed_authority(engine: object) -> None:
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
                authority_fingerprint="sha256:authority",
            )
        )
        session.commit()


def test_runner_stores_report_and_event(tmp_path: Path) -> None:
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
    assert "implementation_evidence_cached" in workflow.state
    with Session(engine) as session:
        event = session.exec(select(WorkflowEvent)).one()
        assert event.event_type == WorkflowEventType.EVIDENCE_COLLECTED


def test_runner_rejects_idempotency_key_reuse_with_changed_file(
    tmp_path: Path,
) -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    _seed_authority(engine)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    base = {
        "schema_version": "agileforge.reconciliation_report.v1",
        "project_id": 1,
        "spec_version_id": 1,
        "compiled_authority_fingerprint": "sha256:authority",
        "repo": None,
        "generated_at": "2026-05-27T12:00:00Z",
        "collector": {"strategy": "manual", "version": "external.v1"},
        "summary": {"finding_count": 0, "evidenced": 0, "evidence_missing": 0, "missing": 0, "unknown": 0},
        "findings": [],
    }
    first.write_text(json.dumps(base), encoding="utf-8")
    changed = dict(base)
    changed["generated_at"] = "2026-05-27T12:01:00Z"
    second.write_text(json.dumps(changed), encoding="utf-8")
    workflow = _WorkflowStub()
    runner = EvidenceCollectionRunner(
        engine=engine,
        product_repo=_ProductRepoStub(),
        workflow_service=workflow,
    )

    assert runner.collect(
        project_id=1,
        repo_path=None,
        from_file=str(first),
        idempotency_key="same-key",
    )["ok"] is True
    result = runner.collect(
        project_id=1,
        repo_path=None,
        from_file=str(second),
        idempotency_key="same-key",
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "IDEMPOTENCY_KEY_REUSED"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_evidence_collect.py -q
```

Expected: FAIL with missing `EvidenceCollectionRunner` or `WorkflowEventType.EVIDENCE_COLLECTED`.

- [ ] **Step 3: Add workflow event enum**

Modify `models/enums.py` in `WorkflowEventType`:

```python
    EVIDENCE_COLLECTED = "evidence_collected"
```

- [ ] **Step 4: Implement `EvidenceCollectionRunner`**

Append to `services/agent_workbench/evidence_collect.py`:

```python
import hashlib
import subprocess  # nosec B404

from sqlmodel import Session, select

from models.core import Product
from models.db import get_engine
from models.enums import WorkflowEventType
from models.events import WorkflowEvent
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance
from repositories.product import ProductRepository
from services.agent_workbench.envelope import error_envelope, success_envelope
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.workflow import WorkflowService


class EvidenceCollectionRunner:
    """Collect or import evidence and cache it in workflow state."""

    def __init__(
        self,
        *,
        engine: Any | None = None,
        product_repo: Any | None = None,
        workflow_service: Any | None = None,
    ) -> None:
        self._engine = engine or get_engine()
        self._product_repo = product_repo or ProductRepository()
        self._workflow_service = workflow_service or WorkflowService()

    def collect(
        self,
        *,
        project_id: int,
        repo_path: str | None,
        from_file: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Collect evidence from a repo or import a report file."""
        if bool(repo_path) == bool(from_file):
            return error_envelope(
                command=EVIDENCE_COLLECT_COMMAND,
                error=workbench_error(
                    ErrorCode.INVALID_COMMAND,
                    message="Exactly one of --repo-path or --from-file is required.",
                    details={"repo_path": repo_path, "from_file": from_file},
                ),
            )
        if not idempotency_key.strip():
            return error_envelope(
                command=EVIDENCE_COLLECT_COMMAND,
                error=workbench_error(
                    ErrorCode.INVALID_COMMAND,
                    message="--idempotency-key is required.",
                ),
            )
        if self._product_repo.get_by_id(project_id) is None:
            return error_envelope(
                command=EVIDENCE_COLLECT_COMMAND,
                error=workbench_error(
                    ErrorCode.PROJECT_NOT_FOUND,
                    message=f"Project {project_id} not found.",
                    details={"project_id": project_id},
                ),
            )

        loaded = self._load_authority(project_id)
        if isinstance(loaded, dict):
            return loaded
        authority_fingerprint, spec_version_id, compiled = loaded

        source_mode = "repo_path" if repo_path else "from_file"
        source_fingerprint = self._source_fingerprint(
            repo_path=repo_path,
            from_file=from_file,
        )
        request_fingerprint = canonical_hash(
            {
                "command": EVIDENCE_COLLECT_COMMAND,
                "project_id": project_id,
                "source_mode": source_mode,
                "compiled_authority_fingerprint": authority_fingerprint,
                "source_fingerprint": source_fingerprint,
                "collector_strategy": COLLECTOR_STRATEGY,
                "collector_version": COLLECTOR_VERSION,
            }
        )
        replay = self._idempotent_replay(
            project_id=project_id,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            return replay

        try:
            report, warnings = self._build_report(
                project_id=project_id,
                spec_version_id=spec_version_id,
                authority_fingerprint=authority_fingerprint,
                compiled=compiled,
                repo_path=repo_path,
                from_file=from_file,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return error_envelope(
                command=EVIDENCE_COLLECT_COMMAND,
                error=workbench_error(
                    ErrorCode.MUTATION_FAILED,
                    message=str(exc),
                    details={"project_id": project_id},
                ),
            )

        report_json = canonical_report_json(report)
        fingerprint = report_fingerprint(report)
        self._workflow_service.update_session_status(
            str(project_id),
            {
                IMPLEMENTATION_EVIDENCE_STATE_KEY: report_json,
                "implementation_evidence_fingerprint": fingerprint,
                "implementation_evidence_collected_at": report.generated_at,
                "implementation_evidence_source": source_mode,
            },
        )
        self._record_event(
            project_id=project_id,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            report_fingerprint=fingerprint,
        )
        return success_envelope(
            command=EVIDENCE_COLLECT_COMMAND,
            data={
                "project_id": project_id,
                "report_fingerprint": fingerprint,
                "stored_state_key": IMPLEMENTATION_EVIDENCE_STATE_KEY,
                "report": report.model_dump(mode="json"),
            },
            warnings=warnings,
            source_fingerprint=fingerprint,
        )

    def _load_authority(
        self,
        project_id: int,
    ) -> tuple[str, int, dict[str, Any]] | dict[str, Any]:
        with Session(self._engine) as session:
            accepted = session.exec(
                select(SpecAuthorityAcceptance)
                .where(
                    SpecAuthorityAcceptance.product_id == project_id,
                    SpecAuthorityAcceptance.status == "accepted",
                )
                .order_by(SpecAuthorityAcceptance.decided_at.desc())
            ).first()
            if accepted is None or not accepted.authority_fingerprint:
                return error_envelope(
                    command=EVIDENCE_COLLECT_COMMAND,
                    error=workbench_error(
                        ErrorCode.AUTHORITY_NOT_ACCEPTED,
                        message="No accepted authority fingerprint is available.",
                        details={"project_id": project_id},
                    ),
                )
            authority = session.exec(
                select(CompiledSpecAuthority).where(
                    CompiledSpecAuthority.spec_version_id == accepted.spec_version_id
                )
            ).first()
            if authority is None or not authority.compiled_artifact_json:
                return error_envelope(
                    command=EVIDENCE_COLLECT_COMMAND,
                    error=workbench_error(
                        ErrorCode.AUTHORITY_NOT_COMPILED,
                        message="Accepted authority has no compiled artifact JSON.",
                        details={"project_id": project_id},
                    ),
                )
            return (
                accepted.authority_fingerprint,
                accepted.spec_version_id,
                json.loads(authority.compiled_artifact_json),
            )

    def _source_fingerprint(
        self,
        *,
        repo_path: str | None,
        from_file: str | None,
    ) -> str:
        if from_file:
            return hashlib.sha256(Path(from_file).read_bytes()).hexdigest()
        repo = Path(repo_path or "").resolve()
        return canonical_hash({"repo": self._repo_metadata(repo).model_dump()})

    def _repo_metadata(self, repo: Path) -> RepoMetadata:
        git_commit: str | None = None
        dirty = False
        try:
            commit = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=False,
                capture_output=True,
                text=True,
            )
            if commit.returncode == 0:
                git_commit = commit.stdout.strip() or None
                status = subprocess.run(
                    ["git", "-C", str(repo), "status", "--porcelain"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                dirty = bool(status.stdout.strip()) if status.returncode == 0 else False
        except OSError:
            git_commit = None
            dirty = False
        return RepoMetadata(path=str(repo), git_commit=git_commit, dirty=dirty)

    def _build_report(
        self,
        *,
        project_id: int,
        spec_version_id: int,
        authority_fingerprint: str,
        compiled: dict[str, Any],
        repo_path: str | None,
        from_file: str | None,
    ) -> tuple[ReconciliationReport, list[WorkbenchWarning]]:
        if from_file:
            return import_report_json(
                Path(from_file).read_text(encoding="utf-8"),
                project_id=project_id,
                current_authority_fingerprint=authority_fingerprint,
            )
        repo = Path(repo_path or "")
        if not repo.exists() or not repo.is_dir():
            raise ValueError("repo path is not a readable directory")
        targets, target_warnings = targets_from_compiled_authority(compiled)
        if not targets:
            raise ValueError("no supported evidence targets found")
        findings, scan_warnings = collect_repo_evidence(repo_path=repo, targets=targets)
        repo_metadata = self._repo_metadata(repo.resolve())
        report = ReconciliationReport(
            project_id=project_id,
            spec_version_id=spec_version_id,
            compiled_authority_fingerprint=authority_fingerprint,
            repo=repo_metadata,
            generated_at=utc_now_iso(),
            collector=CollectorMetadata(),
            summary=build_summary(findings),
            findings=findings,
        )
        warnings = [*target_warnings, *scan_warnings]
        if repo_metadata.dirty:
            warnings.append(
                WorkbenchWarning(
                    code="EVIDENCE_REPO_DIRTY",
                    message="Repository has uncommitted changes.",
                    details={"repo_path": repo_metadata.path},
                )
            )
        return report, warnings

    def _idempotent_replay(
        self,
        *,
        project_id: int,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> dict[str, Any] | None:
        with Session(self._engine) as session:
            events = session.exec(
                select(WorkflowEvent)
                .where(WorkflowEvent.product_id == project_id)
                .where(WorkflowEvent.event_type == WorkflowEventType.EVIDENCE_COLLECTED)
            ).all()
            for event in events:
                try:
                    metadata = json.loads(event.event_metadata or "{}")
                except json.JSONDecodeError:
                    continue
                if metadata.get("idempotency_key") != idempotency_key:
                    continue
                if metadata.get("request_fingerprint") != request_fingerprint:
                    return error_envelope(
                        command=EVIDENCE_COLLECT_COMMAND,
                        error=workbench_error(
                            ErrorCode.IDEMPOTENCY_KEY_REUSED,
                            message="Idempotency key was reused with different inputs.",
                            details={"idempotency_key": idempotency_key},
                        ),
                    )
                state = self._workflow_service.get_session_status(str(project_id)) or {}
                raw_report = state.get(IMPLEMENTATION_EVIDENCE_STATE_KEY)
                report = ReconciliationReport.model_validate_json(str(raw_report))
                return success_envelope(
                    command=EVIDENCE_COLLECT_COMMAND,
                    data={
                        "project_id": project_id,
                        "report_fingerprint": metadata.get("report_fingerprint"),
                        "stored_state_key": IMPLEMENTATION_EVIDENCE_STATE_KEY,
                        "idempotent_replay": True,
                        "report": report.model_dump(mode="json"),
                    },
                    source_fingerprint=str(metadata.get("report_fingerprint") or ""),
                )
        return None

    def _record_event(
        self,
        *,
        project_id: int,
        idempotency_key: str,
        request_fingerprint: str,
        report_fingerprint: str,
    ) -> None:
        with Session(self._engine) as session:
            session.add(
                WorkflowEvent(
                    event_type=WorkflowEventType.EVIDENCE_COLLECTED,
                    product_id=project_id,
                    event_metadata=json.dumps(
                        {
                            "action": "evidence_collected",
                            "idempotency_key": idempotency_key,
                            "request_fingerprint": request_fingerprint,
                            "report_fingerprint": report_fingerprint,
                        },
                        sort_keys=True,
                    ),
                )
            )
            session.commit()
```

- [ ] **Step 5: Run tests**

Run:

```bash
uv run pytest tests/test_evidence_collect.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add models/enums.py services/agent_workbench/evidence_collect.py tests/test_evidence_collect.py
git commit -m "feat: cache evidence reports in workflow state"
```

---

### Task 5: Backlog Agent Consumes Implementation Evidence

**Files:**
- Modify: `orchestrator_agent/agent_tools/backlog_primer/schemes.py`
- Modify: `services/backlog_runtime.py`
- Modify: `orchestrator_agent/agent_tools/backlog_primer/instructions.txt`
- Modify: `tests/test_backlog_primer_agent.py`
- Modify: `tests/test_agent_workbench_backlog_phase.py`

- [ ] **Step 1: Add failing schema/runtime tests**

Modify `tests/test_backlog_primer_agent.py` in `test_input_schema_json_roundtrip` payload:

```python
            "implementation_evidence": "NO_EVIDENCE",
```

and after parse:

```python
        assert parsed.implementation_evidence == "NO_EVIDENCE"
```

Modify `tests/test_agent_workbench_backlog_phase.py` in
`test_backlog_generate_hydrates_vision_spec_and_authority_before_agent` fake state:

```python
        state["implementation_evidence_cached"] = '{"schema_version":"agileforge.reconciliation_report.v1","findings":[]}'
```

and expected input context:

```python
                "implementation_evidence": state.get("implementation_evidence_cached"),
```

and assertion:

```python
    assert result["data"]["input_context"]["implementation_evidence"].startswith(
        '{"schema_version"'
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_backlog_primer_agent.py tests/test_agent_workbench_backlog_phase.py -q
```

Expected: FAIL because `implementation_evidence` is not in `InputSchema` or runtime context.

- [ ] **Step 3: Extend backlog input schema**

Modify `orchestrator_agent/agent_tools/backlog_primer/schemes.py` inside `InputSchema` after `prior_backlog_state`:

```python
    implementation_evidence: Annotated[
        str,
        Field(
            description=(
                "Raw ReconciliationReport JSON from implementation_evidence_cached "
                "or NO_EVIDENCE when no evidence report has been collected."
            ),
        ),
    ]
```

- [ ] **Step 4: Populate backlog runtime context**

Modify `services/backlog_runtime.py` in `build_backlog_input_context`:

```python
    implementation_evidence = _as_text(
        state.get("implementation_evidence_cached")
    ).strip()

    return {
        "product_vision_statement": vision_stmt,
        "technical_spec": _as_text(state.get("pending_spec_content")),
        "compiled_authority": _as_text(state.get("compiled_authority_cached")),
        "prior_backlog_state": _normalize_prior_backlog_state(
            state.get("backlog_items")
        ),
        "implementation_evidence": implementation_evidence or "NO_EVIDENCE",
        "user_input": user_input or "",
    }
```

- [ ] **Step 5: Update backlog prompt instructions**

Append to `orchestrator_agent/agent_tools/backlog_primer/instructions.txt`:

```text
Implementation evidence:
- The input field implementation_evidence is either NO_EVIDENCE or a raw agileforge.reconciliation_report.v1 JSON object.
- Treat implementation evidence as advisory reference evidence, not proof of runtime correctness.
- For status=evidenced, avoid creating new implementation work unless accepted authority still clearly requires unresolved work.
- For status=evidence_missing, scope backlog work to verification, hardening, tests, or documentation instead of reimplementation.
- For status=missing, create normal backlog work only when accepted authority requires the behavior, and preserve the low-confidence caveat in technical_note.
- For status=unknown, flag the requirement for Product Owner review instead of treating it as missing.
```

- [ ] **Step 6: Run tests**

Run:

```bash
uv run pytest tests/test_backlog_primer_agent.py tests/test_agent_workbench_backlog_phase.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add orchestrator_agent/agent_tools/backlog_primer/schemes.py services/backlog_runtime.py orchestrator_agent/agent_tools/backlog_primer/instructions.txt tests/test_backlog_primer_agent.py tests/test_agent_workbench_backlog_phase.py
git commit -m "feat: feed evidence into backlog generation"
```

---

### Task 6: Application Facade And CLI Command

**Files:**
- Modify: `services/agent_workbench/application.py`
- Modify: `cli/main.py`
- Modify: `services/agent_workbench/command_registry.py`
- Modify: `tests/test_agent_workbench_phase1_integration.py`

- [ ] **Step 1: Add failing CLI integration test**

Append to `tests/test_agent_workbench_phase1_integration.py`:

```python
def test_evidence_collect_cli_writes_workflow_state(
    session: Session,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_id, _story_id, _spec_version_id = _seed_phase1_project(
        session,
        repo_root=tmp_path,
    )
    authority = session.get(CompiledSpecAuthority, 1)
    assert authority is not None
    authority.compiled_artifact_json = json.dumps(
        {
            "spec_version_id": 1,
            "items": [
                {
                    "id": "REQ.phase1-context",
                    "type": "REQ",
                    "verification": "inspection",
                }
            ],
        }
    )
    session.add(authority)
    session.commit()
    repo = tmp_path / "cartola"
    repo.mkdir()
    (repo / "phase1.py").write_text("# REQ.phase1-context\n", encoding="utf-8")
    engine = cast("Engine", session.get_bind())
    app = _app_for_engine(engine=engine, repo_root=tmp_path)

    payload = _cli_payload(
        [
            "evidence",
            "collect",
            "--project-id",
            str(project_id),
            "--repo-path",
            str(repo),
            "--idempotency-key",
            "evidence-phase1",
        ],
        app=app,
        capsys=capsys,
    )

    assert _mapping(payload["meta"])["command"] == "agileforge evidence collect"
    data = _mapping(payload["data"])
    assert data["stored_state_key"] == "implementation_evidence_cached"
    report = _mapping(data["report"])
    assert report["schema_version"] == "agileforge.reconciliation_report.v1"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
uv run pytest tests/test_agent_workbench_phase1_integration.py::test_evidence_collect_cli_writes_workflow_state -q
```

Expected: FAIL because `evidence` CLI group is not registered.

- [ ] **Step 3: Add application protocol and facade method**

Modify `services/agent_workbench/application.py` near other protocols:

```python
class _EvidenceCollectionRunner(Protocol):
    """Evidence collection commands exposed through the facade."""

    def collect(
        self,
        *,
        project_id: int,
        repo_path: str | None,
        from_file: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Collect or import implementation evidence."""
        ...
```

Modify `AgentWorkbenchApplication.__init__` signature to accept:

```python
        evidence_runner: _EvidenceCollectionRunner | None = None,
```

Store it:

```python
        self._evidence_runner = evidence_runner
```

Add public method near backlog methods:

```python
    def evidence_collect(
        self,
        *,
        project_id: int,
        repo_path: str | None,
        from_file: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Collect or import implementation evidence."""
        return self._get_evidence_runner().collect(
            project_id=project_id,
            repo_path=repo_path,
            from_file=from_file,
            idempotency_key=idempotency_key,
        )
```

Add lazy getter:

```python
    def _get_evidence_runner(self) -> _EvidenceCollectionRunner:
        """Return the evidence runner, constructing the default lazily."""
        if self._evidence_runner is None:
            from services.agent_workbench.evidence_collect import (  # noqa: PLC0415
                EvidenceCollectionRunner,
            )

            self._evidence_runner = EvidenceCollectionRunner()
        return self._evidence_runner
```

- [ ] **Step 4: Add CLI protocol, parser, and handler**

Modify `_Application` protocol in `cli/main.py`:

```python
    def evidence_collect(
        self,
        *,
        project_id: int,
        repo_path: str | None,
        from_file: str | None,
        idempotency_key: str,
    ) -> JsonObject:
        """Collect or import implementation evidence."""
        ...
```

In `build_parser`, add before `context` group:

```python
    evidence = subparsers.add_parser(
        "evidence",
        help="Collect implementation evidence for backlog reconciliation.",
    )
    evidence_sub = evidence.add_subparsers(
        dest="action",
        required=True,
        parser_class=_WorkbenchArgumentParser,
    )
    evidence_collect = evidence_sub.add_parser(
        "collect",
        help="Collect or import implementation evidence.",
    )
    evidence_collect.add_argument("--project-id", type=int, required=True)
    evidence_collect.add_argument("--repo-path")
    evidence_collect.add_argument("--from-file")
    evidence_collect.add_argument("--idempotency-key", required=True)
    evidence_collect.set_defaults(command_handler=_evidence_collect)
```

Add handler near `_backlog_generate`:

```python
def _evidence_collect(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route evidence collect to the application facade."""
    return "agileforge evidence collect", application.evidence_collect(
        project_id=args.project_id,
        repo_path=args.repo_path,
        from_file=args.from_file,
        idempotency_key=args.idempotency_key,
    )
```

- [ ] **Step 5: Register command metadata**

Modify `services/agent_workbench/command_registry.py` by adding to a phase tuple:

```python
    CommandMetadata(
        name="agileforge evidence collect",
        mutates=True,
        phase="phase_2d",
        requires_idempotency_key=True,
        input_required=("project_id", "idempotency_key"),
        input_optional=("repo_path", "from_file"),
        errors=(
            ErrorCode.PROJECT_NOT_FOUND.value,
            ErrorCode.INVALID_COMMAND.value,
            ErrorCode.AUTHORITY_NOT_ACCEPTED.value,
            ErrorCode.AUTHORITY_NOT_COMPILED.value,
            ErrorCode.MUTATION_FAILED.value,
            ErrorCode.IDEMPOTENCY_KEY_REUSED.value,
        ),
    ),
```

- [ ] **Step 6: Run CLI integration test**

Run:

```bash
uv run pytest tests/test_agent_workbench_phase1_integration.py::test_evidence_collect_cli_writes_workflow_state -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add services/agent_workbench/application.py cli/main.py services/agent_workbench/command_registry.py tests/test_agent_workbench_phase1_integration.py
git commit -m "feat: expose evidence collect CLI"
```

---

### Task 7: Final Verification And Documentation

**Files:**
- Modify: `docs/agent-cli-manual.md`

- [ ] **Step 1: Add manual entry**

Add a short section near Backlog phase docs in `docs/agent-cli-manual.md`:

````markdown
### Implementation Evidence Collection

For brownfield projects, collect implementation evidence before generating a
Backlog draft:

```sh
agileforge evidence collect \
  --project-id "$PROJECT_ID" \
  --repo-path /path/to/repo \
  --idempotency-key "evidence-$PROJECT_ID-$(date +%Y%m%d%H%M%S)"
```

The command stores a raw `agileforge.reconciliation_report.v1` JSON report in
workflow state under `implementation_evidence_cached`. Backlog generation reads
that report as advisory evidence. Phase 1a uses exact reference matching only
and does not execute tests.

Manual import is also supported:

```sh
agileforge evidence collect \
  --project-id "$PROJECT_ID" \
  --from-file evidence_report.json \
  --idempotency-key "evidence-import-$PROJECT_ID-$(date +%Y%m%d%H%M%S)"
```
````

- [ ] **Step 2: Run focused tests**

Run:

```bash
uv run pytest \
  tests/test_evidence_collect.py \
  tests/test_backlog_primer_agent.py \
  tests/test_agent_workbench_backlog_phase.py \
  tests/test_agent_workbench_phase1_integration.py::test_evidence_collect_cli_writes_workflow_state \
  -q
```

Expected: PASS.

- [ ] **Step 3: Run boundary tests likely affected by enum/import changes**

Run:

```bash
uv run pytest \
  tests/test_model_package_boundary.py \
  tests/test_agent_tool_runtime_import_boundary.py \
  tests/test_api_runtime_import_boundary.py \
  -q
```

Expected: PASS. If an import-boundary assertion fails because `WorkflowEventType` enum usage changed, update only that assertion to include `EVIDENCE_COLLECTED` where the boundary explicitly enumerates enum members.

- [ ] **Step 4: Run full test suite**

Run:

```bash
uv run pytest -q
```

Expected: PASS.

- [ ] **Step 5: Commit docs and any boundary-test adjustment**

```bash
git add docs/agent-cli-manual.md tests/test_model_package_boundary.py tests/test_agent_tool_runtime_import_boundary.py tests/test_api_runtime_import_boundary.py
git commit -m "docs: document evidence collection CLI"
```

If only `docs/agent-cli-manual.md` changed, commit only that file:

```bash
git add docs/agent-cli-manual.md
git commit -m "docs: document evidence collection CLI"
```

---

## Self-Review Checklist

- The plan implements the approved Phase 1a only: no DB tables, no OpenSpec integration, no `TaskMetadata` v2, no sprint planner evidence model, and no semantic code analysis.
- The collector never emits `strong` confidence and always uses `validation_state = "not_run"`.
- `missing` is low confidence.
- test-only exact references classify as `evidence_missing`.
- imported reports are fingerprinted by content and rejected on authority-fingerprint mismatch.
- idempotency is stored in `WorkflowEventType.EVIDENCE_COLLECTED`.
- backlog generation degrades to `NO_EVIDENCE` when no evidence report exists.
