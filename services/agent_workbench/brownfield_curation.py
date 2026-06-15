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

from sqlmodel import Session, select

from models.brownfield import (
    BrownfieldScanAttempt,
    BrownfieldSourceArtifact,
    BrownfieldSpecDraftAttempt,
)
from models.core import Product
from models.db import get_engine
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.agent_workbench.fingerprints import canonical_hash
from services.agent_workbench.mutation_ledger import MutationLedgerRepository
from services.specs.profile_content import (
    SpecContentNormalizationError,
    normalize_spec_content_for_registry,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

BROWNFIELD_SOURCE_IMPORT_COMMAND = "agileforge brownfield source import"
BROWNFIELD_SCAN_COMMAND = "agileforge brownfield scan"
BROWNFIELD_SPEC_DRAFT_COMMAND = "agileforge brownfield spec draft"
BROWNFIELD_SPEC_IMPORT_COMMAND = "agileforge brownfield spec import"
BROWNFIELD_COMMAND_VERSION = "brownfield-curation.v1"
BROWNFIELD_SOURCE_FILE_NOT_FOUND = "BROWNFIELD_SOURCE_FILE_NOT_FOUND"
BROWNFIELD_REPO_PATH_NOT_FOUND = "BROWNFIELD_REPO_PATH_NOT_FOUND"
BROWNFIELD_SOURCE_NOT_FOUND = "BROWNFIELD_SOURCE_NOT_FOUND"
BROWNFIELD_SCAN_NOT_FOUND = "BROWNFIELD_SCAN_NOT_FOUND"
BROWNFIELD_DRAFT_NOT_FOUND = "BROWNFIELD_DRAFT_NOT_FOUND"
BROWNFIELD_APPROVAL_CHAIN_MISMATCH = "BROWNFIELD_APPROVAL_CHAIN_MISMATCH"
BROWNFIELD_SPEC_IMPLEMENTATION_HEAVY = "BROWNFIELD_SPEC_IMPLEMENTATION_HEAVY"
NO_SOURCE_FINGERPRINT = "sha256:no-source"
MAX_SCAN_FILE_BYTES = 200_000
MAX_SCAN_MANIFEST_FILES = 1_000
GIT_BINARY = "git"
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


class BrownfieldCurationRunner:
    """Run brownfield source and scan commands against durable rows."""

    def __init__(self, *, engine: Engine | None = None) -> None:
        """Initialize runner with explicit or default business DB engine."""
        self._engine = engine or get_engine()
        self._ledger = MutationLedgerRepository(engine=self._engine)

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

    def _project_exists(self, project_id: int) -> bool:
        """Return whether the project id exists."""
        with Session(self._engine) as session:
            return session.get(Product, project_id) is not None
