"""Brownfield product-spec curation source and scan commands."""

# ruff: noqa: PLR0913

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import update
from sqlmodel import Session, select

from models.agent_workbench import CliMutationLedger
from models.brownfield import (
    BrownfieldScanAttempt,
    BrownfieldSourceArtifact,
    BrownfieldSpecApproval,
    BrownfieldSpecDraftAttempt,
)
from models.core import Product
from models.db import get_engine
from models.specs import SpecRegistry
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.agent_workbench.fingerprints import canonical_hash
from services.agent_workbench.mutation_ledger import (
    MutationLedgerRepository,
    MutationStatus,
    RecoveryAction,
)
from services.specs.pending_authority_service import (
    ensure_pending_spec_version_for_project,
)
from services.specs.profile_content import (
    SpecContentNormalizationError,
    normalize_spec_content_for_registry,
)
from utils.runtime_config import get_config_root

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

BROWNFIELD_SOURCE_IMPORT_COMMAND = "agileforge brownfield source import"
BROWNFIELD_SCAN_COMMAND = "agileforge brownfield scan"
BROWNFIELD_SPEC_DRAFT_COMMAND = "agileforge brownfield spec draft"
BROWNFIELD_SPEC_IMPORT_COMMAND = "agileforge brownfield spec import"
BROWNFIELD_COMMAND_VERSION = "brownfield-curation.v1"
BROWNFIELD_SOURCE_FILE_NOT_FOUND = ErrorCode.BROWNFIELD_SOURCE_FILE_NOT_FOUND.value
BROWNFIELD_REPO_PATH_NOT_FOUND = ErrorCode.BROWNFIELD_REPO_PATH_NOT_FOUND.value
BROWNFIELD_SOURCE_NOT_FOUND = ErrorCode.BROWNFIELD_SOURCE_NOT_FOUND.value
BROWNFIELD_SCAN_NOT_FOUND = ErrorCode.BROWNFIELD_SCAN_NOT_FOUND.value
BROWNFIELD_DRAFT_NOT_FOUND = ErrorCode.BROWNFIELD_DRAFT_NOT_FOUND.value
BROWNFIELD_DRAFT_STALE = ErrorCode.BROWNFIELD_DRAFT_STALE.value
BROWNFIELD_DRAFT_INCOMPLETE = ErrorCode.BROWNFIELD_DRAFT_INCOMPLETE.value
BROWNFIELD_SOURCE_SUPERSEDED = ErrorCode.BROWNFIELD_SOURCE_SUPERSEDED.value
BROWNFIELD_APPROVAL_CHAIN_MISMATCH = (
    ErrorCode.BROWNFIELD_APPROVAL_CHAIN_MISMATCH.value
)
BROWNFIELD_CURATED_SPEC_ALREADY_REGISTERED = (
    ErrorCode.BROWNFIELD_CURATED_SPEC_ALREADY_REGISTERED.value
)
BROWNFIELD_APPROVAL_STALE_GUARD = ErrorCode.BROWNFIELD_APPROVAL_STALE_GUARD.value
BROWNFIELD_SPEC_IMPLEMENTATION_HEAVY = "BROWNFIELD_SPEC_IMPLEMENTATION_HEAVY"
NO_SOURCE_FINGERPRINT = "sha256:no-source"
MAX_SCAN_FILE_BYTES = 200_000
MAX_SCAN_MANIFEST_FILES = 1_000
GIT_BINARY = "git"
LEDGER_MUTATION_EVENT_ID: Any = CliMutationLedger.mutation_event_id
LEDGER_STATUS: Any = CliMutationLedger.status
LEDGER_LEASE_OWNER: Any = CliMutationLedger.lease_owner
LEDGER_LEASE_EXPIRES_AT: Any = CliMutationLedger.lease_expires_at
PRODUCT_ITEM_TYPES = {
    "GOAL",
    "REQ",
    "QUALITY",
    "CONSTRAINT",
    "INTERFACE",
    "DATA",
    "DECISION",
    "NON_GOAL",
    "RISK",
    "OPEN_QUESTION",
}
NORMATIVE_ITEM_TYPES = {"REQ", "QUALITY", "CONSTRAINT", "INTERFACE", "DATA"}
IMPLEMENTATION_TERMS = {
    "route",
    "endpoint",
    "table",
    "column",
    "model",
    "serializer",
    "controller",
    "worker",
    "queue",
    "framework",
}


def _now() -> datetime:
    """Return the current UTC timestamp."""
    return datetime.now(UTC)


def _db_datetime(value: datetime) -> datetime:
    """Normalize a timestamp for SQLite persistence."""
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value.replace(tzinfo=None)


def _success(data: dict[str, Any]) -> dict[str, Any]:
    """Return the standard workbench success envelope."""
    return {"ok": True, "data": data, "warnings": [], "errors": []}


def _error(
    code: ErrorCode | str,
    *,
    details: dict[str, Any] | None = None,
    remediation: list[str] | None = None,
) -> dict[str, Any]:
    """Return the standard workbench error envelope."""
    return {
        "ok": False,
        "data": None,
        "warnings": [],
        "errors": [
            workbench_error(
                code,
                details=details,
                remediation=remediation,
            ).to_dict()
        ],
    }


def _json_dump(value: object) -> str:
    """Serialize deterministic JSON for artifact rows."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _managed_approved_spec_path(
    *,
    project_id: int,
    approval_attempt_id: str,
) -> Path:
    """Return the managed path for an approved curated brownfield spec."""
    return (
        get_config_root()
        / "artifacts"
        / "brownfield"
        / str(project_id)
        / "approvals"
        / approval_attempt_id
        / "spec.json"
    )


def _authority_compile_action(
    *,
    project_id: int,
    spec_version_id: int,
    spec_hash: str,
) -> dict[str, Any]:
    """Return the next command after brownfield approval registers a spec."""
    return {
        "command": "agileforge authority compile",
        "args": {
            "project_id": project_id,
            "spec_version_id": spec_version_id,
            "expected_spec_hash": spec_hash,
            "expected_state": "SETUP_REQUIRED",
            "expected_setup_status": "authority_compile_required",
        },
        "reason": "Compile approved brownfield spec before authority review.",
    }


def _file_sha256(file_path: Path) -> str:
    """Return a real SHA-256 digest for a file."""
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _preview_text(file_path: Path, limit: int = 1000) -> str:
    """Return a bounded UTF-8 preview for a raw source artifact."""
    return file_path.read_text(encoding="utf-8", errors="replace")[:limit]


def _repo_metadata(repo_path: Path) -> dict[str, Any]:
    """Return best-effort git metadata without requiring a git repository."""
    git_commit: str | None = None
    dirty = False
    commit = subprocess.run(  # noqa: S603
        [GIT_BINARY, "-C", str(repo_path), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if commit.returncode == 0:
        git_commit = commit.stdout.strip() or None
        status = subprocess.run(  # noqa: S603
            [GIT_BINARY, "-C", str(repo_path), "status", "--porcelain"],
            check=False,
            capture_output=True,
            text=True,
        )
        dirty = bool(status.stdout.strip()) if status.returncode == 0 else False
    return {"repo_commit": git_commit, "repo_dirty": dirty}


def _is_secret_or_env_file(relative_path: str) -> bool:
    """Return whether a relative path should be skipped as secret-looking."""
    parts = relative_path.split("/")
    if any(part == ".git" for part in parts):
        return True
    name = parts[-1].lower()
    if name == ".env" or name.startswith(".env."):
        return True
    secret_tokens = (
        "secret",
        "secrets",
        "password",
        "passwd",
        "token",
        "credential",
        "credentials",
    )
    secret_suffixes = (".pem", ".key", ".p12", ".pfx")
    return any(token in name for token in secret_tokens) or name.endswith(
        secret_suffixes
    )


def _file_manifest(
    repo_path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return a bounded deterministic manifest and skip warnings."""
    manifest: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for root, dirnames, filenames in os.walk(repo_path):
        dirnames[:] = sorted(dirname for dirname in dirnames if dirname != ".git")
        for filename in sorted(filenames):
            file_path = Path(root) / filename
            if file_path.is_symlink() or not file_path.is_file():
                continue
            relative = file_path.relative_to(repo_path).as_posix()
            skipped = _scan_file_skip_reason(relative, file_path)
            if skipped is not None:
                warnings.append(skipped)
                continue
            file_size = file_path.stat().st_size
            manifest.append(
                {
                    "path": relative,
                    "sha256": _file_sha256(file_path),
                    "size_bytes": file_size,
                }
            )
            if len(manifest) >= MAX_SCAN_MANIFEST_FILES:
                warnings.append(
                    {
                        "reason": "manifest_file_limit_reached",
                        "limit": MAX_SCAN_MANIFEST_FILES,
                    }
                )
                return manifest, warnings
    return manifest, warnings


def _scan_file_skip_reason(
    relative: str,
    file_path: Path,
) -> dict[str, Any] | None:
    """Return a manifest skip reason for files outside scan bounds."""
    if _is_secret_or_env_file(relative):
        return {"path": relative, "reason": "secret_or_env_file"}
    file_size = file_path.stat().st_size
    if file_size > MAX_SCAN_FILE_BYTES:
        return {
            "path": relative,
            "reason": "file_too_large",
            "size_bytes": file_size,
        }
    return None


def _typed_item(line: str, index: int) -> dict[str, Any] | None:
    """Parse a product-level typed source line into a spec item."""
    prefix, separator, body = line.partition(":")
    item_type = prefix.strip().upper()
    statement = body.strip()
    if separator != ":" or item_type not in PRODUCT_ITEM_TYPES or not statement:
        return None
    item: dict[str, Any] = {
        "id": f"{item_type}.brownfield.{index:03d}",
        "type": item_type,
        "status": "proposed",
        "title": statement[:80],
        "statement": statement,
        "verification": "manual-review",
        "acceptance": [statement],
    }
    if item_type in NORMATIVE_ITEM_TYPES:
        item["level"] = "MUST"
    return item


def _candidate_spec_from_source(
    *,
    project_id: int,
    source_text: str,
    user_input: str | None,
) -> tuple[dict[str, Any], str, list[str]]:
    """Build a deterministic candidate agileforge.spec.v1 artifact."""
    warnings: list[str] = []
    items = [
        item
        for index, line in enumerate(source_text.splitlines(), start=1)
        if (item := _typed_item(line, index)) is not None
    ]
    implementation_hits = sum(
        source_text.lower().count(term) for term in IMPLEMENTATION_TERMS
    )
    if implementation_hits >= max(3, len(items) * 2):
        warnings.append(BROWNFIELD_SPEC_IMPLEMENTATION_HEAVY)
    if not items:
        items = [
            {
                "id": "OPEN_QUESTION.brownfield.001",
                "type": "OPEN_QUESTION",
                "status": "proposed",
                "title": "Curated product requirements needed",
                "statement": (
                    "Human review must provide product-level requirements before "
                    "approval."
                ),
                "verification": "manual-review",
                "acceptance": ["A human imports a curated agileforge.spec.v1 file."],
            }
        ]

    spec = {
        "schema_version": "agileforge.spec.v1",
        "artifact_id": f"SPEC.brownfield.{project_id}",
        "title": f"Brownfield Curated Spec {project_id}",
        "status": "draft",
        "version": "0.1",
        "created_at": "2026-06-15",
        "updated_at": "2026-06-15",
        "summary": user_input or "Curated brownfield product specification.",
        "problem_statement": (
            "Brownfield setup needs reviewed product requirements before authority "
            "compilation."
        ),
        "items": items,
        "relations": [],
        "controlled_terms": [],
        "external_references": [],
        "rendering": {
            "markdown_profile": "agileforge.spec_markdown.v1",
            "rendered_markdown_sha256": None,
        },
    }
    status = (
        "complete"
        if any(item["type"] != "OPEN_QUESTION" for item in items)
        else "incomplete"
    )
    return spec, status, warnings


def _normalization_error_response(exc: SpecContentNormalizationError) -> dict[str, Any]:
    """Return a workbench error response for invalid curated spec content."""
    return _error(exc.error_code, details={"message": str(exc)})


def _finalize_ledger_response(
    *,
    engine: Engine,
    mutation_event_id: int,
    lease_owner: str,
    status: MutationStatus,
    response: dict[str, Any],
) -> bool:
    """Store a non-success response for replayable approval guard failures."""
    now = _now()
    db_now = _db_datetime(now)
    with Session(engine) as session:
        result = session.exec(
            update(CliMutationLedger)
            .where(mutation_event_id == LEDGER_MUTATION_EVENT_ID)
            .where(MutationStatus.PENDING.value == LEDGER_STATUS)
            .where(lease_owner == LEDGER_LEASE_OWNER)
            .where(db_now < LEDGER_LEASE_EXPIRES_AT)
            .values(
                status=status.value,
                response_json=_json_dump(response),
                recovery_action=RecoveryAction.NONE.value,
                recovery_safe_to_auto_resume=False,
                lease_owner=None,
                lease_acquired_at=None,
                last_heartbeat_at=None,
                lease_expires_at=None,
                updated_at=db_now,
            )
        )
        session.commit()
        return result.rowcount == 1


class BrownfieldWorkflowPort:
    """Workflow state operations used by brownfield approval."""

    def get_session_status(self, session_id: str) -> dict[str, Any]:
        """Return workflow state for a project session."""
        raise NotImplementedError

    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, Any],
    ) -> None:
        """Patch workflow state for a project session."""
        raise NotImplementedError


class SyncBrownfieldWorkflowAdapter(BrownfieldWorkflowPort):
    """Synchronous adapter over WorkflowService."""

    def __init__(self) -> None:
        """Initialize the default workflow service adapter."""
        from services.workflow import WorkflowService  # noqa: PLC0415

        self._workflow = WorkflowService()

    def get_session_status(self, session_id: str) -> dict[str, Any]:
        """Return persisted workflow state."""
        return self._workflow.get_session_status(session_id)

    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, Any],
    ) -> None:
        """Patch persisted workflow state."""
        self._workflow.update_session_status(session_id, partial_update)


def brownfield_progress(*, engine: Engine, project_id: int) -> dict[str, Any]:
    """Return derived brownfield progress from artifact rows."""
    with Session(engine) as session:
        source = session.exec(
            select(BrownfieldSourceArtifact)
            .where(BrownfieldSourceArtifact.project_id == project_id)
            .where(BrownfieldSourceArtifact.status == "complete")
            .order_by(BrownfieldSourceArtifact.created_at.desc())
        ).first()
        scan = session.exec(
            select(BrownfieldScanAttempt)
            .where(BrownfieldScanAttempt.project_id == project_id)
            .where(BrownfieldScanAttempt.status == "complete")
            .order_by(BrownfieldScanAttempt.created_at.desc())
        ).first()
        draft = session.exec(
            select(BrownfieldSpecDraftAttempt)
            .where(BrownfieldSpecDraftAttempt.project_id == project_id)
            .where(BrownfieldSpecDraftAttempt.status == "complete")
            .order_by(BrownfieldSpecDraftAttempt.created_at.desc())
        ).first()
    return {
        "source": "current" if source is not None else "missing",
        "scan": "current" if scan is not None else "missing",
        "draft": "ready" if draft is not None else "missing",
        "approval": "required" if draft is not None else "blocked",
        "recommended_draft_attempt_id": draft.attempt_id
        if draft is not None
        else None,
    }


class BrownfieldCurationRunner:
    """Run brownfield source and scan commands against durable rows."""

    def __init__(
        self,
        *,
        engine: Engine | None = None,
        workflow: BrownfieldWorkflowPort | None = None,
    ) -> None:
        """Initialize runner with explicit or default business DB engine."""
        self._engine = engine or get_engine()
        self._ledger = MutationLedgerRepository(engine=self._engine)
        self._workflow = workflow or SyncBrownfieldWorkflowAdapter()

    def source_import(
        self,
        *,
        project_id: int,
        source_file: str,
        source_kind: str = "source_file",
        idempotency_key: str,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Record a raw, non-authoritative brownfield source file."""
        if not self._project_exists(project_id):
            return _error(
                ErrorCode.PROJECT_NOT_FOUND,
                details={"project_id": project_id},
            )
        resolved = Path(source_file).expanduser().resolve()
        if not resolved.is_file():
            return _error(
                BROWNFIELD_SOURCE_FILE_NOT_FOUND,
                details={"source_file": str(resolved)},
            )

        source_sha256 = _file_sha256(resolved)
        request_hash = canonical_hash(
            {
                "command": BROWNFIELD_SOURCE_IMPORT_COMMAND,
                "project_id": project_id,
                "source_file": str(resolved),
                "source_sha256": source_sha256,
                "source_kind": source_kind,
                "changed_by": changed_by,
            }
        )
        lease_owner = f"brownfield-source:{idempotency_key}"
        loaded = self._ledger.create_or_load(
            command=BROWNFIELD_SOURCE_IMPORT_COMMAND,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            project_id=project_id,
            correlation_id=correlation_id or idempotency_key,
            changed_by=changed_by,
            lease_owner=lease_owner,
            now=_now(),
        )
        if loaded.response is not None:
            return loaded.response
        if loaded.error_code is not None:
            return _error(
                loaded.error_code,
                details={"idempotency_key": idempotency_key},
            )

        mutation_event_id = loaded.ledger.mutation_event_id
        if mutation_event_id is None:
            message = "Brownfield source mutation event id was not persisted."
            raise RuntimeError(message)
        attempt_id = f"source-{mutation_event_id}"
        artifact_fingerprint = canonical_hash(
            {
                "project_id": project_id,
                "attempt_id": attempt_id,
                "source_sha256": source_sha256,
                "source_kind": source_kind,
                "tool_version": BROWNFIELD_COMMAND_VERSION,
            }
        )
        data = {
            "project_id": project_id,
            "attempt_id": attempt_id,
            "artifact_fingerprint": artifact_fingerprint,
            "source_kind": source_kind,
            "source_file": str(resolved),
            "source_sha256": source_sha256,
            "status": "complete",
            "mutation_event_id": mutation_event_id,
        }
        with Session(self._engine) as session:
            session.add(
                BrownfieldSourceArtifact(
                    project_id=project_id,
                    attempt_id=attempt_id,
                    artifact_fingerprint=artifact_fingerprint,
                    source_kind=source_kind,
                    source_file_path=str(resolved),
                    source_sha256=source_sha256,
                    content_preview=_preview_text(resolved),
                    request_hash=request_hash,
                    tool_version=BROWNFIELD_COMMAND_VERSION,
                )
            )
            session.commit()

        response = _success(data)
        self._ledger.finalize_success(
            mutation_event_id=mutation_event_id,
            lease_owner=lease_owner,
            after=data,
            response=response,
            now=_now(),
        )
        return response

    def scan(
        self,
        *,
        project_id: int,
        repo_path: str,
        source_attempt_id: str | None = None,
        idempotency_key: str,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Record a bounded repository scan for brownfield curation."""
        if not self._project_exists(project_id):
            return _error(
                ErrorCode.PROJECT_NOT_FOUND,
                details={"project_id": project_id},
            )
        resolved_repo = Path(repo_path).expanduser().resolve()
        if not resolved_repo.is_dir():
            return _error(
                BROWNFIELD_REPO_PATH_NOT_FOUND,
                details={"repo_path": str(resolved_repo)},
            )

        source_fingerprint = NO_SOURCE_FINGERPRINT
        if source_attempt_id is not None:
            with Session(self._engine) as session:
                source = session.exec(
                    select(BrownfieldSourceArtifact).where(
                        BrownfieldSourceArtifact.project_id == project_id,
                        BrownfieldSourceArtifact.attempt_id == source_attempt_id,
                        BrownfieldSourceArtifact.status == "complete",
                    )
                ).first()
            if source is None:
                return _error(
                    BROWNFIELD_SOURCE_NOT_FOUND,
                    details={
                        "project_id": project_id,
                        "source_attempt_id": source_attempt_id,
                    },
                )
            source_fingerprint = source.artifact_fingerprint

        metadata = _repo_metadata(resolved_repo)
        manifest, skip_warnings = _file_manifest(resolved_repo)
        manifest_hash = canonical_hash({"files": manifest})
        request_hash = canonical_hash(
            {
                "command": BROWNFIELD_SCAN_COMMAND,
                "project_id": project_id,
                "repo_path": str(resolved_repo),
                "repo_commit": metadata["repo_commit"],
                "repo_dirty": metadata["repo_dirty"],
                "manifest_hash": manifest_hash,
                "source_attempt_id": source_attempt_id,
                "source_fingerprint": source_fingerprint,
                "changed_by": changed_by,
            }
        )
        lease_owner = f"brownfield-scan:{idempotency_key}"
        loaded = self._ledger.create_or_load(
            command=BROWNFIELD_SCAN_COMMAND,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            project_id=project_id,
            correlation_id=correlation_id or idempotency_key,
            changed_by=changed_by,
            lease_owner=lease_owner,
            now=_now(),
        )
        if loaded.response is not None:
            return loaded.response
        if loaded.error_code is not None:
            return _error(
                loaded.error_code,
                details={"idempotency_key": idempotency_key},
            )

        mutation_event_id = loaded.ledger.mutation_event_id
        if mutation_event_id is None:
            message = "Brownfield scan mutation event id was not persisted."
            raise RuntimeError(message)
        attempt_id = f"scan-{mutation_event_id}"
        artifact_fingerprint = canonical_hash(
            {
                "project_id": project_id,
                "attempt_id": attempt_id,
                "source_fingerprint": source_fingerprint,
                "repo": metadata,
                "manifest_hash": manifest_hash,
                "tool_version": BROWNFIELD_COMMAND_VERSION,
            }
        )
        facts = [{"kind": "file", "path": item["path"]} for item in manifest[:200]]
        data = {
            "project_id": project_id,
            "attempt_id": attempt_id,
            "artifact_fingerprint": artifact_fingerprint,
            "source_attempt_id": source_attempt_id,
            "source_fingerprint": source_fingerprint,
            "repo_path": str(resolved_repo),
            "repo_commit": metadata["repo_commit"],
            "repo_dirty": metadata["repo_dirty"],
            "manifest_hash": manifest_hash,
            "manifest": manifest,
            "implementation_facts": facts,
            "status": "complete",
            "mutation_event_id": mutation_event_id,
        }
        with Session(self._engine) as session:
            session.add(
                BrownfieldScanAttempt(
                    project_id=project_id,
                    attempt_id=attempt_id,
                    artifact_fingerprint=artifact_fingerprint,
                    source_attempt_id=source_attempt_id,
                    source_fingerprint=source_fingerprint,
                    repo_path=str(resolved_repo),
                    repo_commit=metadata["repo_commit"],
                    repo_dirty=bool(metadata["repo_dirty"]),
                    file_manifest_json=_json_dump(manifest),
                    implementation_facts_json=_json_dump(facts),
                    request_hash=request_hash,
                    warning_metadata_json=_json_dump(skip_warnings),
                    tool_version=BROWNFIELD_COMMAND_VERSION,
                )
            )
            session.commit()

        response = _success(data)
        self._ledger.finalize_success(
            mutation_event_id=mutation_event_id,
            lease_owner=lease_owner,
            after=data,
            response=response,
            now=_now(),
        )
        return response

    def spec_draft(
        self,
        *,
        project_id: int,
        scan_attempt_id: str,
        user_input: str | None = None,
        idempotency_key: str,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Create a deterministic generated curated-spec draft candidate."""
        if not self._project_exists(project_id):
            return _error(
                ErrorCode.PROJECT_NOT_FOUND,
                details={"project_id": project_id},
            )

        with Session(self._engine) as session:
            scan = session.exec(
                select(BrownfieldScanAttempt).where(
                    BrownfieldScanAttempt.project_id == project_id,
                    BrownfieldScanAttempt.attempt_id == scan_attempt_id,
                    BrownfieldScanAttempt.status == "complete",
                )
            ).first()
            if scan is None:
                return _error(
                    BROWNFIELD_SCAN_NOT_FOUND,
                    details={
                        "project_id": project_id,
                        "scan_attempt_id": scan_attempt_id,
                    },
                )
            scan_fingerprint = scan.artifact_fingerprint
            source_fingerprint = scan.source_fingerprint
            source_attempt_id = scan.source_attempt_id
            source_text = ""
            if source_attempt_id is not None:
                source = session.exec(
                    select(BrownfieldSourceArtifact).where(
                        BrownfieldSourceArtifact.project_id == project_id,
                        BrownfieldSourceArtifact.attempt_id == source_attempt_id,
                        BrownfieldSourceArtifact.status == "complete",
                    )
                ).first()
                if source is not None and source.content_preview:
                    source_text = source.content_preview

        spec, draft_status, warning_codes = _candidate_spec_from_source(
            project_id=project_id,
            source_text=source_text,
            user_input=user_input,
        )
        try:
            normalized = normalize_spec_content_for_registry(_json_dump(spec))
        except SpecContentNormalizationError as exc:
            return _normalization_error_response(exc)

        request_hash = canonical_hash(
            {
                "command": BROWNFIELD_SPEC_DRAFT_COMMAND,
                "project_id": project_id,
                "scan_attempt_id": scan_attempt_id,
                "scan_fingerprint": scan_fingerprint,
                "source_fingerprint": source_fingerprint,
                "user_input": user_input,
                "changed_by": changed_by,
                "tool_version": BROWNFIELD_COMMAND_VERSION,
            }
        )
        lease_owner = f"brownfield-draft:{idempotency_key}"
        loaded = self._ledger.create_or_load(
            command=BROWNFIELD_SPEC_DRAFT_COMMAND,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            project_id=project_id,
            correlation_id=correlation_id or idempotency_key,
            changed_by=changed_by,
            lease_owner=lease_owner,
            now=_now(),
        )
        if loaded.response is not None:
            return loaded.response
        if loaded.error_code is not None:
            return _error(
                loaded.error_code,
                details={"idempotency_key": idempotency_key},
            )

        mutation_event_id = loaded.ledger.mutation_event_id
        if mutation_event_id is None:
            message = "Brownfield draft mutation event id was not persisted."
            raise RuntimeError(message)
        attempt_id = f"draft-{mutation_event_id}"
        artifact_fingerprint = canonical_hash(
            {
                "project_id": project_id,
                "attempt_id": attempt_id,
                "origin": "generated",
                "status": draft_status,
                "scan_fingerprint": scan_fingerprint,
                "source_fingerprint": source_fingerprint,
                "spec_hash": normalized.spec_hash,
                "tool_version": BROWNFIELD_COMMAND_VERSION,
            }
        )
        data = {
            "project_id": project_id,
            "attempt_id": attempt_id,
            "artifact_fingerprint": artifact_fingerprint,
            "origin": "generated",
            "status": draft_status,
            "scan_attempt_id": scan_attempt_id,
            "scan_fingerprint": scan_fingerprint,
            "source_fingerprint": source_fingerprint,
            "spec_hash": normalized.spec_hash,
            "warnings": warning_codes,
            "mutation_event_id": mutation_event_id,
        }
        with Session(self._engine) as session:
            session.add(
                BrownfieldSpecDraftAttempt(
                    project_id=project_id,
                    attempt_id=attempt_id,
                    artifact_fingerprint=artifact_fingerprint,
                    origin="generated",
                    status=draft_status,
                    source_fingerprint=source_fingerprint,
                    scan_attempt_id=scan_attempt_id,
                    scan_fingerprint=scan_fingerprint,
                    spec_hash=normalized.spec_hash,
                    curated_spec_json=normalized.content,
                    request_hash=request_hash,
                    user_input_hash=canonical_hash({"user_input": user_input}),
                    warning_metadata_json=_json_dump(warning_codes),
                    tool_version=BROWNFIELD_COMMAND_VERSION,
                )
            )
            session.commit()

        response = _success(data)
        response["warnings"] = [{"code": code} for code in warning_codes]
        self._ledger.finalize_success(
            mutation_event_id=mutation_event_id,
            lease_owner=lease_owner,
            after=data,
            response=response,
            now=_now(),
        )
        return response

    def spec_import(  # noqa: PLR0911
        self,
        *,
        project_id: int,
        curated_spec_file: str,
        expected_scan_fingerprint: str,
        parent_draft_attempt_id: str | None = None,
        idempotency_key: str,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Record a human-imported curated spec candidate."""
        if not self._project_exists(project_id):
            return _error(
                ErrorCode.PROJECT_NOT_FOUND,
                details={"project_id": project_id},
            )
        resolved = Path(curated_spec_file).expanduser().resolve()
        if not resolved.is_file():
            return _error(
                ErrorCode.SPEC_FILE_NOT_FOUND,
                details={"curated_spec_file": str(resolved)},
            )
        try:
            normalized = normalize_spec_content_for_registry(
                resolved.read_text(encoding="utf-8")
            )
        except SpecContentNormalizationError as exc:
            return _normalization_error_response(exc)

        with Session(self._engine) as session:
            scan = session.exec(
                select(BrownfieldScanAttempt).where(
                    BrownfieldScanAttempt.project_id == project_id,
                    BrownfieldScanAttempt.artifact_fingerprint
                    == expected_scan_fingerprint,
                    BrownfieldScanAttempt.status == "complete",
                )
            ).first()
            if scan is None:
                return _error(
                    BROWNFIELD_APPROVAL_CHAIN_MISMATCH,
                    details={
                        "project_id": project_id,
                        "expected_scan_fingerprint": expected_scan_fingerprint,
                    },
                )
            scan_attempt_id = scan.attempt_id
            scan_fingerprint = scan.artifact_fingerprint
            source_fingerprint = scan.source_fingerprint
            if parent_draft_attempt_id is not None:
                parent = session.exec(
                    select(BrownfieldSpecDraftAttempt).where(
                        BrownfieldSpecDraftAttempt.project_id == project_id,
                        BrownfieldSpecDraftAttempt.attempt_id
                        == parent_draft_attempt_id,
                    )
                ).first()
                if parent is None:
                    return _error(
                        BROWNFIELD_DRAFT_NOT_FOUND,
                        details={
                            "project_id": project_id,
                            "parent_draft_attempt_id": parent_draft_attempt_id,
                        },
                    )

        request_hash = canonical_hash(
            {
                "command": BROWNFIELD_SPEC_IMPORT_COMMAND,
                "project_id": project_id,
                "curated_spec_file": str(resolved),
                "spec_hash": normalized.spec_hash,
                "expected_scan_fingerprint": expected_scan_fingerprint,
                "parent_draft_attempt_id": parent_draft_attempt_id,
                "changed_by": changed_by,
                "tool_version": BROWNFIELD_COMMAND_VERSION,
            }
        )
        lease_owner = f"brownfield-import:{idempotency_key}"
        loaded = self._ledger.create_or_load(
            command=BROWNFIELD_SPEC_IMPORT_COMMAND,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            project_id=project_id,
            correlation_id=correlation_id or idempotency_key,
            changed_by=changed_by,
            lease_owner=lease_owner,
            now=_now(),
        )
        if loaded.response is not None:
            return loaded.response
        if loaded.error_code is not None:
            return _error(
                loaded.error_code,
                details={"idempotency_key": idempotency_key},
            )

        mutation_event_id = loaded.ledger.mutation_event_id
        if mutation_event_id is None:
            message = "Brownfield import mutation event id was not persisted."
            raise RuntimeError(message)
        attempt_id = f"draft-import-{mutation_event_id}"
        artifact_fingerprint = canonical_hash(
            {
                "project_id": project_id,
                "attempt_id": attempt_id,
                "origin": "human_import",
                "scan_fingerprint": scan_fingerprint,
                "source_fingerprint": source_fingerprint,
                "spec_hash": normalized.spec_hash,
                "tool_version": BROWNFIELD_COMMAND_VERSION,
            }
        )
        data = {
            "project_id": project_id,
            "attempt_id": attempt_id,
            "artifact_fingerprint": artifact_fingerprint,
            "origin": "human_import",
            "status": "complete",
            "scan_attempt_id": scan_attempt_id,
            "scan_fingerprint": scan_fingerprint,
            "source_fingerprint": source_fingerprint,
            "spec_hash": normalized.spec_hash,
            "mutation_event_id": mutation_event_id,
        }
        with Session(self._engine) as session:
            session.add(
                BrownfieldSpecDraftAttempt(
                    project_id=project_id,
                    attempt_id=attempt_id,
                    artifact_fingerprint=artifact_fingerprint,
                    origin="human_import",
                    status="complete",
                    source_fingerprint=source_fingerprint,
                    scan_attempt_id=scan_attempt_id,
                    scan_fingerprint=scan_fingerprint,
                    parent_draft_attempt_id=parent_draft_attempt_id,
                    spec_hash=normalized.spec_hash,
                    curated_spec_json=normalized.content,
                    imported_file_path=str(resolved),
                    request_hash=request_hash,
                    tool_version=BROWNFIELD_COMMAND_VERSION,
                )
            )
            session.commit()

        response = _success(data)
        self._ledger.finalize_success(
            mutation_event_id=mutation_event_id,
            lease_owner=lease_owner,
            after=data,
            response=response,
            now=_now(),
        )
        return response

    def spec_approve(  # noqa: C901, PLR0911, PLR0915
        self,
        *,
        project_id: int,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        expected_setup_status: str,
        idempotency_key: str,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        """Approve one exact curated draft into the compileable spec registry."""
        if not self._project_exists(project_id):
            return _error(
                ErrorCode.PROJECT_NOT_FOUND,
                details={"project_id": project_id},
            )

        with Session(self._engine) as session:
            draft = session.exec(
                select(BrownfieldSpecDraftAttempt).where(
                    BrownfieldSpecDraftAttempt.project_id == project_id,
                    BrownfieldSpecDraftAttempt.attempt_id == attempt_id,
                )
            ).first()
            if draft is None:
                return _error(
                    BROWNFIELD_DRAFT_NOT_FOUND,
                    details={"project_id": project_id, "attempt_id": attempt_id},
                )
            draft_fingerprint = draft.artifact_fingerprint
            draft_status = draft.status
            draft_spec_json = draft.curated_spec_json
            draft_spec_hash = draft.spec_hash
            draft_scan_fingerprint = draft.scan_fingerprint
            draft_source_fingerprint = draft.source_fingerprint
            draft_scan_attempt_id = draft.scan_attempt_id

        request_hash = canonical_hash(
            {
                "command": "agileforge brownfield spec approve",
                "project_id": project_id,
                "attempt_id": attempt_id,
                "expected_artifact_fingerprint": expected_artifact_fingerprint,
                "expected_state": expected_state,
                "expected_setup_status": expected_setup_status,
                "draft_fingerprint": draft_fingerprint,
                "spec_hash": draft_spec_hash,
                "scan_fingerprint": draft_scan_fingerprint,
                "source_fingerprint": draft_source_fingerprint,
                "changed_by": changed_by,
                "tool_version": BROWNFIELD_COMMAND_VERSION,
            }
        )
        lease_owner = f"brownfield-approve:{idempotency_key}"
        loaded = self._ledger.create_or_load(
            command="agileforge brownfield spec approve",
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            project_id=project_id,
            correlation_id=correlation_id or idempotency_key,
            changed_by=changed_by,
            lease_owner=lease_owner,
            now=_now(),
        )
        if loaded.response is not None:
            return loaded.response
        if loaded.error_code is not None:
            return _error(
                loaded.error_code,
                details={"idempotency_key": idempotency_key},
            )
        mutation_event_id = loaded.ledger.mutation_event_id
        if mutation_event_id is None:
            message = "Brownfield approval mutation event id was not persisted."
            raise RuntimeError(message)

        validation = self._approval_validation_error(
            project_id=project_id,
            attempt_id=attempt_id,
            expected_artifact_fingerprint=expected_artifact_fingerprint,
            draft_fingerprint=draft_fingerprint,
            draft_status=draft_status,
            draft_spec_json=draft_spec_json,
            draft_spec_hash=draft_spec_hash,
            draft_scan_attempt_id=draft_scan_attempt_id,
            draft_scan_fingerprint=draft_scan_fingerprint,
            draft_source_fingerprint=draft_source_fingerprint,
            expected_state=expected_state,
            expected_setup_status=expected_setup_status,
        )
        if validation is not None:
            response, status = validation
            _finalize_ledger_response(
                engine=self._engine,
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                status=status,
                response=response,
            )
            return response

        approval_attempt_id = f"approval-{mutation_event_id}"
        approval_fingerprint = canonical_hash(
            {
                "project_id": project_id,
                "approval_attempt_id": approval_attempt_id,
                "draft_fingerprint": draft_fingerprint,
                "scan_fingerprint": draft_scan_fingerprint,
                "source_fingerprint": draft_source_fingerprint,
                "spec_hash": draft_spec_hash,
                "tool_version": BROWNFIELD_COMMAND_VERSION,
            }
        )
        managed_path = _managed_approved_spec_path(
            project_id=project_id,
            approval_attempt_id=approval_attempt_id,
        )
        managed_path.parent.mkdir(parents=True, exist_ok=True)
        if draft_spec_json is None or draft_spec_hash is None:
            return _error(
                BROWNFIELD_DRAFT_INCOMPLETE,
                details={"project_id": project_id, "attempt_id": attempt_id},
            )
        managed_path.write_text(draft_spec_json, encoding="utf-8")

        with Session(self._engine) as session:
            approval = BrownfieldSpecApproval(
                project_id=project_id,
                approval_attempt_id=approval_attempt_id,
                approval_fingerprint=approval_fingerprint,
                draft_attempt_id=attempt_id,
                draft_fingerprint=draft_fingerprint,
                scan_fingerprint=draft_scan_fingerprint,
                source_fingerprint=draft_source_fingerprint,
                spec_hash=draft_spec_hash,
                managed_spec_file_path=str(managed_path),
                mutation_event_id=mutation_event_id,
                status="started",
            )
            session.add(approval)
            session.commit()

            result = ensure_pending_spec_version_for_project(
                session=session,
                product_id=project_id,
                spec_path=managed_path,
                approved_by="brownfield-spec-approve",
                lease_guard=lambda _boundary: self._ledger.require_active_owner(
                    mutation_event_id=mutation_event_id,
                    lease_owner=lease_owner,
                    now=_now(),
                ),
                record_progress=lambda boundary: self._ledger.mark_step_complete(
                    mutation_event_id=mutation_event_id,
                    lease_owner=lease_owner,
                    step=boundary,
                    next_step=boundary,
                    now=_now(),
                ),
            )
            if not result.ok or result.spec_version_id is None:
                approval.status = "recovery_required"
                approval.error_metadata_json = _json_dump(
                    {
                        "error_code": result.error_code,
                        "error": result.error,
                    }
                )
                approval.updated_at = _now()
                session.add(approval)
                session.commit()
                self._ledger.mark_recovery_required(
                    mutation_event_id=mutation_event_id,
                    lease_owner=lease_owner,
                    recovery_action=RecoveryAction.RECONCILE_THEN_RESUME,
                    safe_to_auto_resume=True,
                    last_error={
                        "code": result.error_code
                        or ErrorCode.MUTATION_RECOVERY_REQUIRED.value,
                        "error": result.error,
                    },
                    now=_now(),
                )
                return _error(
                    result.error_code or ErrorCode.MUTATION_RECOVERY_REQUIRED,
                    details={"mutation_event_id": mutation_event_id},
                )

            spec_version_id = int(result.spec_version_id)
            approval.spec_version_id = spec_version_id
            approval.status = "spec_registered"
            approval.updated_at = _now()
            session.add(approval)
            session.commit()

        next_actions = [
            _authority_compile_action(
                project_id=project_id,
                spec_version_id=spec_version_id,
                spec_hash=draft_spec_hash,
            )
        ]
        required_state = {
            "fsm_state": "SETUP_REQUIRED",
            "setup_mode": "brownfield",
            "setup_status": "authority_compile_required",
            "setup_error": None,
            "setup_spec_file_path": str(managed_path),
            "setup_spec_hash": draft_spec_hash,
            "setup_spec_version_id": spec_version_id,
            "setup_next_actions": next_actions,
        }
        try:
            self._workflow.update_session_status(str(project_id), required_state)
        except Exception as exc:  # noqa: BLE001
            with Session(self._engine) as session:
                approval = session.exec(
                    select(BrownfieldSpecApproval).where(
                        BrownfieldSpecApproval.approval_fingerprint
                        == approval_fingerprint
                    )
                ).one()
                approval.status = "recovery_required"
                approval.error_metadata_json = _json_dump(
                    {"error_code": "WORKFLOW_SESSION_FAILED", "error": str(exc)}
                )
                approval.updated_at = _now()
                session.add(approval)
                session.commit()
            self._ledger.mark_recovery_required(
                mutation_event_id=mutation_event_id,
                lease_owner=lease_owner,
                recovery_action=RecoveryAction.RECONCILE_THEN_RESUME,
                safe_to_auto_resume=True,
                last_error={
                    "code": "WORKFLOW_SESSION_FAILED",
                    "error": str(exc),
                    "spec_version_id": spec_version_id,
                },
                now=_now(),
            )
            return _error(
                ErrorCode.MUTATION_RECOVERY_REQUIRED,
                details={
                    "mutation_event_id": mutation_event_id,
                    "spec_version_id": spec_version_id,
                },
            )

        with Session(self._engine) as session:
            approval = session.exec(
                select(BrownfieldSpecApproval).where(
                    BrownfieldSpecApproval.approval_fingerprint == approval_fingerprint
                )
            ).one()
            approval.status = "complete"
            approval.updated_at = _now()
            session.add(approval)
            session.commit()

        data = {
            "project_id": project_id,
            "approval_attempt_id": approval_attempt_id,
            "approval_fingerprint": approval_fingerprint,
            "setup_status": "authority_compile_required",
            "setup_spec_file_path": str(managed_path),
            "spec_hash": draft_spec_hash,
            "spec_version_id": spec_version_id,
            "mutation_event_id": mutation_event_id,
            "next_actions": next_actions,
        }
        response = _success(data)
        if not self._ledger.finalize_success(
            mutation_event_id=mutation_event_id,
            lease_owner=lease_owner,
            after=data,
            response=response,
            now=_now(),
        ):
            return _error(
                ErrorCode.MUTATION_RESUME_CONFLICT,
                details={"mutation_event_id": mutation_event_id},
            )
        return response

    def _approval_validation_error(  # noqa: PLR0911
        self,
        *,
        project_id: int,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        draft_fingerprint: str,
        draft_status: str,
        draft_spec_json: str | None,
        draft_spec_hash: str | None,
        draft_scan_attempt_id: str,
        draft_scan_fingerprint: str,
        draft_source_fingerprint: str,
        expected_state: str,
        expected_setup_status: str,
    ) -> tuple[dict[str, Any], MutationStatus] | None:
        """Return a replayable validation error for brownfield approval."""
        if draft_status != "complete" or not draft_spec_json or not draft_spec_hash:
            return (
                _error(
                    BROWNFIELD_DRAFT_INCOMPLETE,
                    details={"project_id": project_id, "attempt_id": attempt_id},
                ),
                MutationStatus.VALIDATION_FAILED,
            )
        if draft_fingerprint != expected_artifact_fingerprint:
            return (
                _error(
                    BROWNFIELD_DRAFT_STALE,
                    details={
                        "project_id": project_id,
                        "attempt_id": attempt_id,
                        "expected_artifact_fingerprint": (
                            expected_artifact_fingerprint
                        ),
                        "actual_artifact_fingerprint": draft_fingerprint,
                    },
                ),
                MutationStatus.GUARD_REJECTED,
            )

        with Session(self._engine) as session:
            latest_source = session.exec(
                select(BrownfieldSourceArtifact)
                .where(BrownfieldSourceArtifact.project_id == project_id)
                .where(BrownfieldSourceArtifact.status == "complete")
                .order_by(BrownfieldSourceArtifact.created_at.desc())
            ).first()
            if (
                latest_source is not None
                and latest_source.artifact_fingerprint != draft_source_fingerprint
            ):
                return (
                    _error(
                        BROWNFIELD_SOURCE_SUPERSEDED,
                        details={
                            "project_id": project_id,
                            "attempt_id": attempt_id,
                            "current_source_fingerprint": (
                                latest_source.artifact_fingerprint
                            ),
                            "draft_source_fingerprint": draft_source_fingerprint,
                        },
                    ),
                    MutationStatus.GUARD_REJECTED,
                )

            current_scan = session.exec(
                select(BrownfieldScanAttempt)
                .where(BrownfieldScanAttempt.project_id == project_id)
                .where(BrownfieldScanAttempt.status == "complete")
                .order_by(BrownfieldScanAttempt.created_at.desc())
            ).first()
            if (
                current_scan is None
                or current_scan.artifact_fingerprint != draft_scan_fingerprint
                or current_scan.attempt_id != draft_scan_attempt_id
                or current_scan.source_fingerprint != draft_source_fingerprint
            ):
                return (
                    _error(
                        BROWNFIELD_APPROVAL_CHAIN_MISMATCH,
                        details={
                            "project_id": project_id,
                            "attempt_id": attempt_id,
                            "draft_scan_fingerprint": draft_scan_fingerprint,
                            "current_scan_fingerprint": (
                                current_scan.artifact_fingerprint
                                if current_scan is not None
                                else None
                            ),
                        },
                    ),
                    MutationStatus.GUARD_REJECTED,
                )
            existing_spec = session.exec(
                select(SpecRegistry).where(SpecRegistry.product_id == project_id)
            ).first()
            existing_approval = session.exec(
                select(BrownfieldSpecApproval).where(
                    BrownfieldSpecApproval.project_id == project_id,
                    BrownfieldSpecApproval.status == "complete",
                )
            ).first()
            if existing_spec is not None or existing_approval is not None:
                return (
                    _error(
                        BROWNFIELD_CURATED_SPEC_ALREADY_REGISTERED,
                        details={
                            "project_id": project_id,
                            "attempt_id": attempt_id,
                        },
                    ),
                    MutationStatus.VALIDATION_FAILED,
                )

        workflow_state = self._workflow.get_session_status(str(project_id))
        if workflow_state.get("fsm_state") != expected_state:
            return (
                _error(
                    ErrorCode.STALE_STATE,
                    details={
                        "project_id": project_id,
                        "expected_state": expected_state,
                        "actual_state": workflow_state.get("fsm_state"),
                    },
                ),
                MutationStatus.GUARD_REJECTED,
            )
        if workflow_state.get("setup_status") != expected_setup_status:
            return (
                _error(
                    ErrorCode.STALE_SETUP_STATUS,
                    details={
                        "project_id": project_id,
                        "expected_setup_status": expected_setup_status,
                        "actual_setup_status": workflow_state.get("setup_status"),
                    },
                ),
                MutationStatus.GUARD_REJECTED,
            )
        if expected_setup_status != "brownfield_curation_required":
            return (
                _error(
                    BROWNFIELD_APPROVAL_STALE_GUARD,
                    details={
                        "project_id": project_id,
                        "expected_setup_status": expected_setup_status,
                    },
                ),
                MutationStatus.GUARD_REJECTED,
            )
        return None

    def _project_exists(self, project_id: int) -> bool:
        """Return whether the project id exists."""
        with Session(self._engine) as session:
            return session.get(Product, project_id) is not None
