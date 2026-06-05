"""Evidence collection contracts for backlog reconciliation reports."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess  # nosec B404
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlmodel import Session, select

from models.enums import WorkflowEventType
from models.events import WorkflowEvent
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance
from services.agent_workbench.authority_projection import pending_authority_fingerprint
from services.agent_workbench.envelope import (
    WorkbenchError,
    WorkbenchWarning,
    error_envelope,
    success_envelope,
)
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.agent_workbench.fingerprints import canonical_hash, canonical_json
from services.specs.compiler_service import (
    compiled_authority_schema_unsupported_details,
    compiled_authority_schema_unsupported_remediation,
    load_compiled_artifact,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.engine import Engine

REPORT_SCHEMA_VERSION: str = "agileforge.reconciliation_report.v1"
COLLECTOR_STRATEGY: str = "exact_tag_match"
COLLECTOR_VERSION: str = "agileforge.evidence_collect.v1"
IMPLEMENTATION_EVIDENCE_STATE_KEY: str = "implementation_evidence_cached"
EVIDENCE_COLLECT_COMMAND: str = "agileforge evidence collect"
MAX_SCAN_BYTES: int = 500 * 1024
GIT_BINARY: str = shutil.which("git") or "git"
NORMATIVE_ITEM_TYPES_ORDERED: tuple[str, ...] = (
    "REQ",
    "QUALITY",
    "CONSTRAINT",
    "INTERFACE",
    "DATA",
)
NORMATIVE_ITEM_TYPES: set[str] = set(NORMATIVE_ITEM_TYPES_ORDERED)

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
        ".codegraph",
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
        ".lock",
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


class _ProductRepository(Protocol):
    """Product lookup dependency used by the runner."""

    def get_by_id(self, product_id: int) -> object | None:
        """Fetch a product by ID."""
        ...


class _WorkflowService(Protocol):
    """Workflow-state dependency used by the runner."""

    def get_session_status(self, session_id: str) -> dict[str, object]:
        """Return workflow state for a project session."""
        ...

    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, object],
    ) -> None:
        """Apply a partial workflow-state update."""
        ...


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


def _targets_from_compiled_invariants(
    compiled_authority: dict[str, Any],
) -> list[SpecEvidenceTarget]:
    """Extract exact evidence targets from v2 compiled-authority invariants."""
    raw_invariants = compiled_authority.get("invariants")
    if not isinstance(raw_invariants, list):
        return []
    targets: list[SpecEvidenceTarget] = []
    for raw_invariant in raw_invariants:
        if not isinstance(raw_invariant, Mapping):
            continue
        invariant = cast("Mapping[str, object]", raw_invariant)
        invariant_id = str(invariant.get("id") or "").strip()
        if not invariant_id:
            continue
        parameters = invariant.get("parameters")
        parameter_map = (
            cast("Mapping[str, object]", parameters)
            if isinstance(parameters, Mapping)
            else {}
        )
        source_item_id = str(
            invariant.get("source_item_id")
            or parameter_map.get("source_item_id")
            or invariant_id
        ).strip()
        item_type = source_item_id.split(".", 1)[0]
        if item_type not in NORMATIVE_ITEM_TYPES:
            item_type = "REQ"
        targets.append(
            SpecEvidenceTarget(
                spec_item_id=source_item_id,
                item_type=item_type,
                verification_method="not-yet-defined",
                matched_terms=sorted({source_item_id, invariant_id}),
            )
        )
    return targets


def targets_from_compiled_authority(
    compiled_authority: dict[str, Any],
) -> tuple[list[SpecEvidenceTarget], list[WorkbenchWarning]]:
    """Extract exact evidence targets from compiled authority JSON."""
    raw_items = compiled_authority.get("items")
    if not isinstance(raw_items, list):
        invariant_targets = _targets_from_compiled_invariants(compiled_authority)
        if invariant_targets:
            return (invariant_targets, [])
        return (
            [],
            [
                WorkbenchWarning(
                    code="EVIDENCE_AUTHORITY_ITEMS_MISSING",
                    message="Compiled authority has no items list.",
                    details={
                        "expected_path": "items",
                        "supported_item_types": list(NORMATIVE_ITEM_TYPES_ORDERED),
                        "target_terms": ["spec item id", "related INV-* ids"],
                    },
                    remediation=[
                        "Ensure compiled authority exposes normative items "
                        "under an items list.",
                        "Reference normative item ids or related INV-* ids "
                        "in repo files to create exact evidence matches.",
                    ],
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
                details={
                    "supported_item_types": list(NORMATIVE_ITEM_TYPES_ORDERED),
                    "target_terms": ["spec item id", "related INV-* ids"],
                },
                remediation=[
                    "Ensure compiled authority contains normative items "
                    "with stable ids.",
                    "Reference those ids or related INV-* ids in repo files "
                    "to create exact evidence matches.",
                ],
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


class EvidenceCollectionRunner:
    """Collect or import evidence and cache it in workflow state."""

    def __init__(
        self,
        *,
        engine: Engine | None = None,
        product_repo: _ProductRepository | None = None,
        workflow_service: _WorkflowService | None = None,
    ) -> None:
        """Initialize runner dependencies."""
        if engine is None:
            from models.db import get_engine  # noqa: PLC0415

            engine = get_engine()
        if product_repo is None:
            from repositories.product import ProductRepository  # noqa: PLC0415

            product_repo = ProductRepository()
        if workflow_service is None:
            from services.workflow import WorkflowService  # noqa: PLC0415

            workflow_service = WorkflowService()
        self._engine = engine
        self._product_repo = product_repo
        self._workflow_service = workflow_service

    def collect(
        self,
        *,
        project_id: int,
        repo_path: str | None,
        from_file: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Collect evidence from a repo or import a report file."""
        validation_error = self._validate_request(
            project_id=project_id,
            repo_path=repo_path,
            from_file=from_file,
            idempotency_key=idempotency_key,
        )
        if validation_error is not None:
            return error_envelope(
                command=EVIDENCE_COLLECT_COMMAND,
                error=validation_error,
            )

        authority_result = self._load_authority(project_id)
        if isinstance(authority_result, dict):
            return authority_result
        authority_fingerprint, spec_version_id, compiled = authority_result

        try:
            source_mode, source_fingerprint = self._source_identity(
                repo_path=repo_path,
                from_file=from_file,
            )
        except OSError as exc:
            return error_envelope(
                command=EVIDENCE_COLLECT_COMMAND,
                error=_mutation_failed(str(exc), {"project_id": project_id}),
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
                error=_mutation_failed(str(exc), {"project_id": project_id}),
            )

        fingerprint = report_fingerprint(report)
        self._workflow_service.update_session_status(
            str(project_id),
            {
                IMPLEMENTATION_EVIDENCE_STATE_KEY: canonical_report_json(report),
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
            report=report,
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

    def _validate_request(
        self,
        *,
        project_id: int,
        repo_path: str | None,
        from_file: str | None,
        idempotency_key: str,
    ) -> WorkbenchError | None:
        if bool(repo_path) == bool(from_file):
            return _invalid_command(
                "Exactly one of --repo-path or --from-file is required.",
                {"repo_path": repo_path, "from_file": from_file},
            )
        if not idempotency_key.strip():
            return _invalid_command("--idempotency-key is required.", {})
        if self._product_repo.get_by_id(project_id) is None:
            return _project_not_found(project_id)
        return None

    def _load_authority(  # noqa: PLR0911
        self,
        project_id: int,
    ) -> tuple[str, int, dict[str, Any]] | dict[str, Any]:
        with Session(self._engine) as session:
            accepted = session.exec(
                select(SpecAuthorityAcceptance)
                .where(SpecAuthorityAcceptance.product_id == project_id)
                .where(SpecAuthorityAcceptance.status == "accepted")
                .order_by(cast("Any", SpecAuthorityAcceptance.decided_at).desc())
            ).first()
            if accepted is None or not accepted.authority_fingerprint:
                return error_envelope(
                    command=EVIDENCE_COLLECT_COMMAND,
                    error=_authority_not_accepted(project_id),
                )

            authority = session.exec(
                select(CompiledSpecAuthority).where(
                    CompiledSpecAuthority.spec_version_id == accepted.spec_version_id
                )
            ).first()
            if authority is None or not authority.compiled_artifact_json:
                return error_envelope(
                    command=EVIDENCE_COLLECT_COMMAND,
                    error=_authority_not_compiled(project_id),
                )
            if _authority_mismatches_acceptance(
                authority=authority,
                accepted=accepted,
            ):
                return error_envelope(
                    command=EVIDENCE_COLLECT_COMMAND,
                    error=_authority_acceptance_mismatch(project_id),
                )
            load_result = load_compiled_artifact(authority)
            if load_result.unsupported:
                return error_envelope(
                    command=EVIDENCE_COLLECT_COMMAND,
                    error=_unsupported_authority_schema(
                        project_id=project_id,
                        spec_version_id=accepted.spec_version_id,
                        observed_schema_version=load_result.observed_schema_version,
                    ),
                )
            if load_result.status == "invalid_json":
                return error_envelope(
                    command=EVIDENCE_COLLECT_COMMAND,
                    error=_authority_not_compiled(
                        project_id,
                        message="Accepted authority artifact JSON is invalid.",
                    ),
                )
            if load_result.status == "schema_invalid":
                return error_envelope(
                    command=EVIDENCE_COLLECT_COMMAND,
                    error=_authority_not_compiled(
                        project_id,
                        message="Accepted authority artifact failed schema validation.",
                    ),
                )
            compiled_artifact = (
                load_result.artifact.model_dump(mode="json")
                if load_result.artifact is not None
                else None
            )
            if not isinstance(compiled_artifact, dict):
                return error_envelope(
                    command=EVIDENCE_COLLECT_COMMAND,
                    error=_authority_not_compiled(project_id),
                )
            return (
                str(accepted.authority_fingerprint),
                int(accepted.spec_version_id),
                compiled_artifact,
            )

    def _source_identity(
        self,
        *,
        repo_path: str | None,
        from_file: str | None,
    ) -> tuple[str, str]:
        if from_file:
            digest = canonical_hash({"file_sha256": _file_sha256(Path(from_file))})
            return "from_file", digest
        repo = Path(repo_path or "").resolve()
        return "repo_path", self._repo_source_fingerprint(repo)

    def _build_report(  # noqa: PLR0913
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
            msg = "repo path is not a readable directory"
            raise ValueError(msg)
        targets, target_warnings = targets_from_compiled_authority(compiled)
        if targets:
            findings, scan_warnings = collect_repo_evidence(repo, targets)
        else:
            findings = []
            scan_warnings = []
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

    def _repo_metadata(self, repo: Path) -> RepoMetadata:
        git_commit: str | None = None
        dirty = False
        try:
            commit = subprocess.run(  # noqa: S603  # nosec B603
                [GIT_BINARY, "-C", str(repo), "rev-parse", "HEAD"],
                check=False,
                capture_output=True,
                text=True,
            )
            if commit.returncode == 0:
                git_commit = commit.stdout.strip() or None
                status = subprocess.run(  # noqa: S603  # nosec B603
                    [GIT_BINARY, "-C", str(repo), "status", "--porcelain"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                dirty = bool(status.stdout.strip()) if status.returncode == 0 else False
        except OSError:
            git_commit = None
            dirty = False
        return RepoMetadata(path=str(repo), git_commit=git_commit, dirty=dirty)

    def _repo_source_fingerprint(self, repo: Path) -> str:
        """Return a fingerprint of repo identity plus scanned file content."""
        files, _warnings = _iter_scannable_files(repo)
        file_fingerprints: list[dict[str, str]] = []
        for file_path, relative_path in files:
            if _skip_file_reason(file_path, relative_path) is not None:
                continue
            try:
                digest = _file_sha256(file_path)
            except OSError as exc:
                digest = canonical_hash({"unreadable": str(exc)})
            file_fingerprints.append(
                {"path": relative_path.as_posix(), "sha256": digest}
            )
        return canonical_hash(
            {
                "repo": self._repo_metadata(repo).model_dump(mode="json"),
                "files": file_fingerprints,
            }
        )

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
                metadata = _json_object(event.event_metadata)
                if metadata.get("idempotency_key") != idempotency_key:
                    continue
                if metadata.get("request_fingerprint") != request_fingerprint:
                    return error_envelope(
                        command=EVIDENCE_COLLECT_COMMAND,
                        error=_idempotency_key_reused(idempotency_key),
                    )

                report = self._report_from_event_metadata(metadata, project_id)
                if report is None:
                    return error_envelope(
                        command=EVIDENCE_COLLECT_COMMAND,
                        error=_mutation_failed(
                            "Idempotent replay report is unavailable.",
                            {
                                "project_id": project_id,
                                "idempotency_key": idempotency_key,
                            },
                        ),
                    )
                report_fingerprint_value = str(
                    metadata.get("report_fingerprint") or report_fingerprint(report)
                )
                return success_envelope(
                    command=EVIDENCE_COLLECT_COMMAND,
                    data={
                        "project_id": project_id,
                        "report_fingerprint": report_fingerprint_value,
                        "stored_state_key": IMPLEMENTATION_EVIDENCE_STATE_KEY,
                        "idempotent_replay": True,
                        "report": report.model_dump(mode="json"),
                    },
                    source_fingerprint=report_fingerprint_value,
                )
        return None

    def _record_event(
        self,
        *,
        project_id: int,
        idempotency_key: str,
        request_fingerprint: str,
        report_fingerprint: str,
        report: ReconciliationReport,
    ) -> None:
        with Session(self._engine) as session:
            session.add(
                WorkflowEvent(
                    event_type=WorkflowEventType.EVIDENCE_COLLECTED,
                    product_id=project_id,
                    session_id=str(project_id),
                    event_metadata=json.dumps(
                        {
                            "action": "evidence_collected",
                            "idempotency_key": idempotency_key,
                            "request_fingerprint": request_fingerprint,
                            "report_fingerprint": report_fingerprint,
                            "report": report.model_dump(mode="json"),
                        },
                        sort_keys=True,
                    ),
                )
            )
            session.commit()

    def _report_from_event_metadata(
        self,
        metadata: dict[str, Any],
        project_id: int,
    ) -> ReconciliationReport | None:
        """Return immutable replay report payload from event metadata."""
        report_payload = metadata.get("report")
        if isinstance(report_payload, dict):
            return ReconciliationReport.model_validate(report_payload)

        state = self._workflow_service.get_session_status(str(project_id)) or {}
        raw_report = state.get(IMPLEMENTATION_EVIDENCE_STATE_KEY)
        if not isinstance(raw_report, str):
            return None
        report = ReconciliationReport.model_validate_json(raw_report)
        if report_fingerprint(report) != metadata.get("report_fingerprint"):
            return None
        return report


def _file_sha256(path: Path) -> str:
    """Return a SHA-256 checksum for file content."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_object(value: str | None) -> dict[str, Any]:
    """Decode a JSON object field, returning empty dict on invalid payloads."""
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _invalid_command(message: str, details: dict[str, Any]) -> WorkbenchError:
    return workbench_error(ErrorCode.INVALID_COMMAND, message=message, details=details)


def _project_not_found(project_id: int) -> WorkbenchError:
    return workbench_error(
        ErrorCode.PROJECT_NOT_FOUND,
        message=f"Project {project_id} not found.",
        details={"project_id": project_id},
    )


def _authority_not_accepted(project_id: int) -> WorkbenchError:
    return workbench_error(
        ErrorCode.AUTHORITY_NOT_ACCEPTED,
        message="No accepted authority fingerprint is available.",
        details={"project_id": project_id},
    )


def _authority_mismatches_acceptance(
    *,
    authority: CompiledSpecAuthority,
    accepted: SpecAuthorityAcceptance,
) -> bool:
    """Return whether a compiled authority no longer matches its acceptance."""
    current_fingerprint = pending_authority_fingerprint(authority)
    return (
        authority.authority_id != accepted.pending_authority_id
        or authority.compiler_version != accepted.compiler_version
        or authority.prompt_hash != accepted.prompt_hash
        or current_fingerprint != accepted.authority_fingerprint
    )


def _authority_acceptance_mismatch(project_id: int) -> WorkbenchError:
    return workbench_error(
        ErrorCode.AUTHORITY_ACCEPTANCE_MISMATCH,
        message="Accepted authority decision does not match compiled authority.",
        details={"project_id": project_id},
    )


def _authority_not_compiled(
    project_id: int,
    *,
    message: str = "Accepted authority has no compiled artifact JSON.",
) -> WorkbenchError:
    return workbench_error(
        ErrorCode.AUTHORITY_NOT_COMPILED,
        message=message,
        details={"project_id": project_id},
    )


def _unsupported_authority_schema(
    *,
    project_id: int,
    spec_version_id: int | None,
    observed_schema_version: str | None,
) -> WorkbenchError:
    """Return the standard unsupported compiled-authority schema error."""
    return workbench_error(
        ErrorCode.COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED,
        message="Compiled authority artifact schema is unsupported.",
        details=compiled_authority_schema_unsupported_details(
            project_id=project_id,
            spec_version_id=spec_version_id,
            observed_schema_version=observed_schema_version,
        ),
        remediation=compiled_authority_schema_unsupported_remediation(
            project_id=project_id,
            spec_version_id=spec_version_id,
        ),
    )


def _mutation_failed(message: str, details: dict[str, Any]) -> WorkbenchError:
    return workbench_error(ErrorCode.MUTATION_FAILED, message=message, details=details)


def _idempotency_key_reused(idempotency_key: str) -> WorkbenchError:
    return workbench_error(
        ErrorCode.IDEMPOTENCY_KEY_REUSED,
        message="Idempotency key was reused with different inputs.",
        details={"idempotency_key": idempotency_key},
    )


def _invariant_terms_from_relations(raw_relations: list[object]) -> set[str]:
    """Extract invariant reference terms from authority item relations."""
    invariant_terms: set[str] = set()
    for raw_relation in raw_relations:
        if not isinstance(raw_relation, Mapping):
            continue
        relation = cast("Mapping[str, object]", raw_relation)
        for relation_key in ("target", "to"):
            relation_target = str(relation.get(relation_key) or "").strip()
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
    reason: str | None = None

    try:
        if not file_path.is_file():
            reason = "non_regular_file"
    except OSError:
        reason = "non_regular_file"
    if reason is None and lower_name in SKIP_FILE_NAMES:
        reason = "file_name"
    if reason is None and lower_suffix in SKIP_FILE_SUFFIXES:
        reason = "file_suffix"

    if reason is None:
        try:
            size = file_path.stat().st_size
        except OSError:
            size = 0
        if size > MAX_SCAN_BYTES:
            reason = "file_too_large"
    return reason


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
