"""As-Built Assessment evidence pack and cache helpers."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess  # nosec B404
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

import anyio
from pydantic import ValidationError
from sqlmodel import Session, select

from models.enums import WorkflowEventType
from models.events import WorkflowEvent
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance
from orchestrator_agent.agent_tools.as_built_assessor.schemes import (
    AGENT_VERSION,
    ASSESSMENT_SCHEMA_VERSION,
    EVIDENCE_PACK_BUILDER_VERSION,
    EVIDENCE_PACK_SCHEMA_VERSION,
    AsBuiltAssessment,
    AsBuiltAssessmentCacheMeta,
    AsBuiltAssessorInput,
    AuthorityTarget,
    CapabilityAssessment,
    EvidenceKind,
    EvidencePack,
    EvidenceSnippet,
    EvidenceWarning,
    OpenSpecContext,
    OriginalSpecContext,
    RepoSnapshot,
    SearchObservation,
    SpecMode,
)
from services.agent_workbench.authority_projection import pending_authority_fingerprint
from services.agent_workbench.envelope import (
    WorkbenchError,
    WorkbenchWarning,
    error_envelope,
    success_envelope,
)
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.agent_workbench.fingerprints import canonical_hash, canonical_json
from utils.adk_runner import invoke_agent_to_text, parse_json_payload
from utils.runtime_config import (
    AS_BUILT_RUNNER_IDENTITY,
    RuntimeConfigError,
    get_as_built_assessor_batch_size,
    get_as_built_assessor_timeout_seconds,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

AS_BUILT_ASSESS_COMMAND: str = "agileforge as-built assess"
AS_BUILT_ASSESSMENT_STATE_KEY: str = "as_built_assessment_cached"
AS_BUILT_ASSESSMENT_META_STATE_KEY: str = "as_built_assessment_cache_meta"
MAX_SCAN_BYTES: int = 500 * 1024
MAX_AUTHORITY_TARGETS: int = 150
MAX_SNIPPETS_PER_TARGET: int = 5
MAX_SNIPPET_LINES: int = 40
MAX_SNIPPET_BYTES: int = 8 * 1024
MAX_PACK_BYTES: int = 750 * 1024
MAX_FILE_MANIFEST_ENTRIES: int = 300
GIT_BINARY: str = shutil.which("git") or "git"

_SKIP_DIR_NAMES: frozenset[str] = frozenset(
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
_SKIP_FILE_NAMES: frozenset[str] = frozenset(
    {"uv.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"}
)
_SKIP_SUFFIXES: frozenset[str] = frozenset(
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
_DOC_DIR_NAMES: frozenset[str] = frozenset({"doc", "docs", "documentation"})
_DOC_SUFFIXES: frozenset[str] = frozenset({".md", ".mdx", ".rst", ".txt"})
_CONFIG_NAMES: frozenset[str] = frozenset(
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
_CONFIG_SUFFIXES: frozenset[str] = frozenset({".toml", ".yaml", ".yml"})
_TEST_SUFFIXES: tuple[str, ...] = (
    ".test.js",
    ".spec.js",
    ".test.ts",
    ".spec.ts",
    ".test.tsx",
    ".spec.tsx",
)
_ID_TERM_PREFIXES: tuple[str, ...] = (
    "INV-",
    "REQ.",
    "QUALITY.",
    "CONSTRAINT.",
    "INTERFACE.",
    "DATA.",
)
_ID_BOUNDARY_CHARS: frozenset[str] = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "_.-"
)


@dataclass(frozen=True)
class _ScannedFile:
    """One already-read repository file for evidence matching."""

    relative_path: Path
    kind: EvidenceKind
    text: str
    lower_text: str


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


class _AgentInvoker(Protocol):
    """Agent invocation dependency used by runner tests."""

    def __call__(self, payload: AsBuiltAssessorInput) -> AsBuiltAssessment:
        """Return a validated As-Built Assessment."""
        ...


def utc_now_iso() -> str:
    """Return canonical UTC timestamp."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def assessment_fingerprint(assessment: AsBuiltAssessment) -> str:
    """Return a canonical fingerprint for an assessment."""
    return canonical_hash(assessment.model_dump(mode="json"))


def cache_meta_for_assessment(
    assessment: AsBuiltAssessment,
) -> AsBuiltAssessmentCacheMeta:
    """Build workflow-state cache metadata for an assessment."""
    return AsBuiltAssessmentCacheMeta(
        schema_version=ASSESSMENT_SCHEMA_VERSION,
        agent_version=assessment.agent_version,
        evidence_pack_builder_version=assessment.evidence_pack_builder_version,
        authority_fingerprint=assessment.authority_fingerprint,
        repo_git_commit=assessment.repo_snapshot.git_commit,
        repo_dirty=assessment.repo_snapshot.dirty,
        evidence_pack_fingerprint=assessment.evidence_pack_fingerprint,
        assessment_fingerprint=assessment_fingerprint(assessment),
        generated_at=assessment.generated_at,
    )


def cached_assessment_for_backlog(state: dict[str, Any]) -> str:
    """Return cached assessment JSON for backlog input when internally fresh."""
    raw_assessment = state.get(AS_BUILT_ASSESSMENT_STATE_KEY)
    raw_meta = state.get(AS_BUILT_ASSESSMENT_META_STATE_KEY)
    if not isinstance(raw_assessment, str) or not isinstance(raw_meta, dict):
        return "NO_AS_BUILT_ASSESSMENT"
    try:
        assessment = AsBuiltAssessment.model_validate_json(raw_assessment)
        meta = AsBuiltAssessmentCacheMeta.model_validate(raw_meta)
    except ValueError:
        return "NO_AS_BUILT_ASSESSMENT"
    if meta.evidence_pack_builder_version != EVIDENCE_PACK_BUILDER_VERSION:
        return "NO_AS_BUILT_ASSESSMENT"
    if meta.assessment_fingerprint != assessment_fingerprint(assessment):
        return "NO_AS_BUILT_ASSESSMENT"
    if (
        meta.agent_version != assessment.agent_version
        or meta.evidence_pack_builder_version
        != assessment.evidence_pack_builder_version
        or meta.authority_fingerprint != assessment.authority_fingerprint
        or meta.repo_git_commit != assessment.repo_snapshot.git_commit
        or meta.repo_dirty != assessment.repo_snapshot.dirty
        or meta.evidence_pack_fingerprint != assessment.evidence_pack_fingerprint
    ):
        return "NO_AS_BUILT_ASSESSMENT"
    return canonical_json(assessment.model_dump(mode="json"))


class AsBuiltAssessmentRunner:
    """Assess current implementation state and cache the assessment."""

    def __init__(
        self,
        *,
        product_repo: _ProductRepository | None = None,
        workflow_service: _WorkflowService | None = None,
        engine: Engine | None = None,
        invoke_agent: _AgentInvoker | None = None,
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
        self._invoke_agent = invoke_agent or _default_invoke_agent

    def assess(  # noqa: PLR0911, PLR0913
        self,
        *,
        project_id: int,
        repo_path: str,
        spec_file: str | None,
        spec_mode: str,
        user_input: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Assess current implementation state and cache it in workflow state."""
        validation_error = self._validate_request(
            project_id=project_id,
            repo_path=repo_path,
            spec_mode=spec_mode,
            idempotency_key=idempotency_key,
        )
        if validation_error is not None:
            return error_envelope(
                command=AS_BUILT_ASSESS_COMMAND,
                error=validation_error,
            )

        authority_result = self._load_authority(project_id)
        if isinstance(authority_result, dict):
            return authority_result
        authority_fingerprint, compiled = authority_result

        try:
            pack = build_evidence_pack(
                project_id=project_id,
                authority_fingerprint=authority_fingerprint,
                compiled_authority=compiled,
                repo_path=Path(repo_path),
                spec_mode=_normalize_spec_mode(spec_mode),
                spec_file=Path(spec_file) if spec_file else None,
            )
        except (OSError, ValueError) as exc:
            return error_envelope(
                command=AS_BUILT_ASSESS_COMMAND,
                error=_mutation_failed(str(exc), {"project_id": project_id}),
            )

        try:
            batch_size = get_as_built_assessor_batch_size()
        except (RuntimeConfigError, ValueError) as exc:
            return error_envelope(
                command=AS_BUILT_ASSESS_COMMAND,
                error=_mutation_failed(
                    "As-built assessment configuration is invalid.",
                    {"project_id": project_id, "detail": str(exc)},
                ),
            )

        request_fingerprint = canonical_hash(
            {
                "command": AS_BUILT_ASSESS_COMMAND,
                "project_id": project_id,
                "repo_path": str(Path(repo_path).resolve()),
                "repo_git_commit": pack.repo_snapshot.git_commit,
                "repo_dirty": pack.repo_snapshot.dirty,
                "spec_file_fingerprint": _spec_file_fingerprint(spec_file),
                "spec_mode": _normalize_spec_mode(spec_mode),
                "authority_fingerprint": authority_fingerprint,
                "evidence_pack_fingerprint": pack.evidence_pack_fingerprint,
                "agent_version": AGENT_VERSION,
                "evidence_pack_builder_version": EVIDENCE_PACK_BUILDER_VERSION,
                "assessor_batch_size": batch_size,
                "user_input": user_input or "",
            }
        )
        replay = self._idempotent_replay(
            project_id=project_id,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )
        if replay is not None:
            return replay

        batch_packs = split_evidence_pack_for_assessment(
            pack,
            batch_size=batch_size,
        )
        assessment_id = _assessment_id(project_id, pack.evidence_pack_fingerprint)
        try:
            assessment = self._invoke_agent_batches(
                project_id=project_id,
                assessment_id=assessment_id,
                compiled=compiled,
                spec_file=Path(spec_file) if spec_file else None,
                spec_mode=_normalize_spec_mode(spec_mode),
                full_pack=pack,
                batch_packs=batch_packs,
                batch_size=batch_size,
                user_input=user_input,
            )
        except (RuntimeError, ValidationError, ValueError) as exc:
            return error_envelope(
                command=AS_BUILT_ASSESS_COMMAND,
                error=_mutation_failed(
                    "As-built assessment agent failed.",
                    _batch_failure_details(
                        project_id=project_id,
                        detail=str(exc),
                        full_pack=pack,
                        batch_count=len(batch_packs),
                        batch_size=batch_size,
                    ),
                ),
            )
        assessment_identity_error = _validate_assessment_identity(
            assessment=assessment,
            pack=pack,
            project_id=project_id,
            assessment_id=assessment_id,
        )
        if assessment_identity_error is not None:
            return error_envelope(
                command=AS_BUILT_ASSESS_COMMAND,
                error=assessment_identity_error,
            )

        fingerprint = assessment_fingerprint(assessment)
        meta = cache_meta_for_assessment(assessment)
        self._workflow_service.update_session_status(
            str(project_id),
            {
                AS_BUILT_ASSESSMENT_STATE_KEY: canonical_assessment_json(assessment),
                AS_BUILT_ASSESSMENT_META_STATE_KEY: meta.model_dump(mode="json"),
            },
        )
        self._record_event(
            project_id=project_id,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            assessment_fingerprint_value=fingerprint,
            evidence_pack_fingerprint=pack.evidence_pack_fingerprint,
            assessment=assessment,
        )
        return success_envelope(
            command=AS_BUILT_ASSESS_COMMAND,
            data={
                "project_id": project_id,
                "assessment_fingerprint": fingerprint,
                "evidence_pack_fingerprint": pack.evidence_pack_fingerprint,
                "stored_state_key": AS_BUILT_ASSESSMENT_STATE_KEY,
                "stored_meta_key": AS_BUILT_ASSESSMENT_META_STATE_KEY,
                "idempotent_replay": False,
                "authority_target_count": len(pack.authority_targets),
                "batch_count": len(batch_packs),
                "batch_size": batch_size,
                "assessment": assessment.model_dump(mode="json"),
            },
            warnings=_workbench_warnings(pack.warnings),
            source_fingerprint=fingerprint,
        )

    def _validate_request(
        self,
        *,
        project_id: int,
        repo_path: str,
        spec_mode: str,
        idempotency_key: str,
    ) -> WorkbenchError | None:
        if not idempotency_key.strip():
            return _invalid_command("--idempotency-key is required.", {})
        if not repo_path.strip():
            return _invalid_command("--repo-path is required.", {})
        if spec_mode not in {
            "current_state",
            "desired_state",
            "proposed_change",
            "unknown",
        }:
            return _invalid_command(
                (
                    "--spec-mode must be current_state, desired_state, "
                    "proposed_change, or unknown."
                ),
                {"spec_mode": spec_mode},
            )
        if self._product_repo.get_by_id(project_id) is None:
            return _project_not_found(project_id)
        return None

    def _load_authority(
        self,
        project_id: int,
    ) -> tuple[str, dict[str, Any]] | dict[str, Any]:
        with Session(self._engine) as session:
            accepted = session.exec(
                select(SpecAuthorityAcceptance)
                .where(SpecAuthorityAcceptance.product_id == project_id)
                .where(SpecAuthorityAcceptance.status == "accepted")
                .order_by(cast("Any", SpecAuthorityAcceptance.decided_at).desc())
            ).first()
            if accepted is None or not accepted.authority_fingerprint:
                return error_envelope(
                    command=AS_BUILT_ASSESS_COMMAND,
                    error=_authority_not_accepted(project_id),
                )

            authority = session.exec(
                select(CompiledSpecAuthority).where(
                    CompiledSpecAuthority.spec_version_id == accepted.spec_version_id
                )
            ).first()
            if authority is None or not authority.compiled_artifact_json:
                return error_envelope(
                    command=AS_BUILT_ASSESS_COMMAND,
                    error=_authority_not_compiled(project_id),
                )
            current_fingerprint = pending_authority_fingerprint(authority)
            if current_fingerprint != accepted.authority_fingerprint:
                return error_envelope(
                    command=AS_BUILT_ASSESS_COMMAND,
                    error=_authority_not_compiled(
                        project_id,
                        message="Accepted authority does not match compiled authority.",
                    ),
                )
            try:
                compiled = json.loads(authority.compiled_artifact_json)
            except json.JSONDecodeError:
                return error_envelope(
                    command=AS_BUILT_ASSESS_COMMAND,
                    error=_authority_not_compiled(
                        project_id,
                        message="Accepted authority artifact JSON is invalid.",
                    ),
                )
            if not isinstance(compiled, dict):
                return error_envelope(
                    command=AS_BUILT_ASSESS_COMMAND,
                    error=_authority_not_compiled(
                        project_id,
                        message="Accepted authority artifact JSON is not an object.",
                    ),
                )
            return str(accepted.authority_fingerprint), compiled

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
                .where(WorkflowEvent.event_type == WorkflowEventType.AS_BUILT_ASSESSED)
            ).all()
            for event in events:
                metadata = _json_object(event.event_metadata)
                if metadata.get("idempotency_key") != idempotency_key:
                    continue
                if metadata.get("request_fingerprint") != request_fingerprint:
                    return error_envelope(
                        command=AS_BUILT_ASSESS_COMMAND,
                        error=_idempotency_key_reused(idempotency_key),
                    )
                assessment = AsBuiltAssessment.model_validate(metadata["assessment"])
                fingerprint = str(
                    metadata.get("assessment_fingerprint")
                    or assessment_fingerprint(assessment)
                )
                return success_envelope(
                    command=AS_BUILT_ASSESS_COMMAND,
                    data={
                        "project_id": project_id,
                        "assessment_fingerprint": fingerprint,
                        "evidence_pack_fingerprint": str(
                            metadata.get("evidence_pack_fingerprint")
                            or assessment.evidence_pack_fingerprint
                        ),
                        "stored_state_key": AS_BUILT_ASSESSMENT_STATE_KEY,
                        "stored_meta_key": AS_BUILT_ASSESSMENT_META_STATE_KEY,
                        "idempotent_replay": True,
                        "assessment": assessment.model_dump(mode="json"),
                    },
                    source_fingerprint=fingerprint,
                )
        return None

    def _record_event(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        idempotency_key: str,
        request_fingerprint: str,
        assessment_fingerprint_value: str,
        evidence_pack_fingerprint: str,
        assessment: AsBuiltAssessment,
    ) -> None:
        with Session(self._engine) as session:
            session.add(
                WorkflowEvent(
                    event_type=WorkflowEventType.AS_BUILT_ASSESSED,
                    product_id=project_id,
                    session_id=str(project_id),
                    event_metadata=json.dumps(
                        {
                            "action": "as_built_assessed",
                            "idempotency_key": idempotency_key,
                            "request_fingerprint": request_fingerprint,
                            "assessment_fingerprint": assessment_fingerprint_value,
                            "evidence_pack_fingerprint": evidence_pack_fingerprint,
                            "assessment": assessment.model_dump(mode="json"),
                        },
                        sort_keys=True,
                    ),
                )
            )
            session.commit()

    def _invoke_agent_batches(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        assessment_id: str,
        compiled: dict[str, Any],
        spec_file: Path | None,
        spec_mode: SpecMode,
        full_pack: EvidencePack,
        batch_packs: list[EvidencePack],
        batch_size: int,
        user_input: str | None,
    ) -> AsBuiltAssessment:
        batch_assessments: list[AsBuiltAssessment] = []
        batch_count = len(batch_packs)
        for index, batch_pack in enumerate(batch_packs, start=1):
            batch_input = _assessor_input_for_pack(
                project_id=project_id,
                assessment_id=_assessment_id(
                    project_id,
                    batch_pack.evidence_pack_fingerprint,
                    batch_index=index,
                    batch_count=batch_count,
                ),
                compiled=compiled,
                spec_file=spec_file,
                spec_mode=spec_mode,
                pack=batch_pack,
                user_input=_batch_user_input(
                    user_input=user_input,
                    index=index,
                    batch_count=batch_count,
                    batch_size=batch_size,
                ),
            )
            try:
                batch_assessment = self._invoke_agent(batch_input)
            except (RuntimeError, ValidationError, ValueError) as exc:
                msg = (
                    f"Batch {index}/{batch_count} failed after "
                    f"{len(batch_assessments)} completed batch(es): {exc}"
                )
                raise RuntimeError(msg) from exc
            identity_error = _validate_assessment_identity(
                assessment=batch_assessment,
                pack=batch_pack,
                project_id=project_id,
                assessment_id=batch_input.assessment_id,
            )
            if identity_error is not None:
                msg = (
                    f"Batch {index}/{batch_count} failed identity validation: "
                    f"{identity_error.message}"
                )
                raise ValueError(msg)
            batch_assessments.append(batch_assessment)
        return merge_batch_assessments(
            project_id=project_id,
            assessment_id=assessment_id,
            full_pack=full_pack,
            batch_assessments=batch_assessments,
        )


def _default_invoke_agent(payload: AsBuiltAssessorInput) -> AsBuiltAssessment:
    """Invoke the ADK assessor synchronously for CLI callers."""
    return anyio.run(_invoke_agent_async, payload)


async def _invoke_agent_async(payload: AsBuiltAssessorInput) -> AsBuiltAssessment:
    from orchestrator_agent.agent_tools.as_built_assessor.agent import (  # noqa: PLC0415
        root_agent,
    )

    return await _invoke_agent_payload_async(agent=root_agent, payload=payload)


async def _invoke_agent_payload_async(
    *,
    agent: object,
    payload: AsBuiltAssessorInput,
) -> AsBuiltAssessment:
    """Invoke an as-built assessor agent with a bounded runtime."""
    timeout_seconds = get_as_built_assessor_timeout_seconds()
    try:
        with anyio.fail_after(timeout_seconds):
            raw_text = await invoke_agent_to_text(
                agent=agent,
                runner_identity=AS_BUILT_RUNNER_IDENTITY,
                payload_json=payload.model_dump_json(by_alias=True),
                no_text_error="As-Built assessor returned no text response",
            )
    except TimeoutError as exc:
        msg = f"As-Built assessor timed out after {timeout_seconds:g} seconds."
        raise RuntimeError(msg) from exc
    parsed = parse_json_payload(raw_text)
    if parsed is None:
        msg = "As-Built assessor returned invalid JSON."
        raise ValueError(msg)
    return AsBuiltAssessment.model_validate(parsed)


def _assessment_id(
    project_id: int,
    evidence_pack_fingerprint: str,
    *,
    batch_index: int | None = None,
    batch_count: int | None = None,
) -> str:
    suffix = evidence_pack_fingerprint.replace("sha256:", "")[:12]
    base = f"as-built-{project_id}-{suffix}"
    if batch_index is None or batch_count is None:
        return base
    return f"{base}-batch-{batch_index:03d}-of-{batch_count:03d}"


def _normalize_spec_mode(value: str) -> SpecMode:
    if value in {"current_state", "desired_state", "proposed_change", "unknown"}:
        return cast("SpecMode", value)
    return "unknown"


def _original_spec_context(
    *,
    spec_file: Path | None,
    spec_mode: SpecMode,
) -> OriginalSpecContext:
    if spec_file is None:
        return OriginalSpecContext(spec_mode=spec_mode, json="", markdown="")
    try:
        text = spec_file.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if spec_file.suffix.lower() == ".json":
        return OriginalSpecContext(spec_mode=spec_mode, json=text, markdown="")
    return OriginalSpecContext(spec_mode=spec_mode, json="", markdown=text)


def _assessor_input_for_pack(  # noqa: PLR0913
    *,
    project_id: int,
    assessment_id: str,
    compiled: dict[str, Any],
    spec_file: Path | None,
    spec_mode: SpecMode,
    pack: EvidencePack,
    user_input: str | None,
) -> AsBuiltAssessorInput:
    return AsBuiltAssessorInput(
        project_id=project_id,
        assessment_id=assessment_id,
        compiled_authority=canonical_json(compiled),
        original_spec=_original_spec_context(
            spec_file=spec_file,
            spec_mode=spec_mode,
        ),
        repo_evidence_pack=pack,
        openspec_context=OpenSpecContext(
            present=False,
            spec_summaries=[],
            change_summaries=[],
        ),
        prior_as_built_assessment="NO_HISTORY",
        user_input=user_input or "",
    )


def _spec_file_fingerprint(spec_file: str | None) -> str | None:
    if not spec_file:
        return None
    path = Path(spec_file)
    try:
        return canonical_hash(
            {"path": str(path.resolve()), "sha256": _file_sha256(path)}
        )
    except OSError as exc:
        return canonical_hash({"path": str(path), "error": str(exc)})


def _workbench_warnings(warnings: list[EvidenceWarning]) -> list[WorkbenchWarning]:
    return [
        WorkbenchWarning(
            code=warning.code,
            message=warning.message,
            details=warning.details,
        )
        for warning in warnings
    ]


def _batch_user_input(
    *,
    user_input: str | None,
    index: int,
    batch_count: int,
    batch_size: int,
) -> str:
    prefix = (
        f"Assess only the authority_targets in this evidence pack. "
        f"This is batch {index} of {batch_count}; configured batch size is "
        f"{batch_size}."
    )
    if user_input and user_input.strip():
        return f"{prefix}\n\nUser input: {user_input.strip()}"
    return prefix


def _batch_failure_details(
    *,
    project_id: int,
    detail: str,
    full_pack: EvidencePack,
    batch_count: int,
    batch_size: int,
) -> dict[str, Any]:
    failed_batch_index = _extract_failed_batch_index(detail)
    completed_batches = max(failed_batch_index - 1, 0) if failed_batch_index else 0
    return {
        "project_id": project_id,
        "detail": detail,
        "authority_target_count": len(full_pack.authority_targets),
        "batch_count": batch_count,
        "batch_size": batch_size,
        "completed_batches": completed_batches,
        "failed_batch_index": failed_batch_index,
        "evidence_pack_fingerprint": full_pack.evidence_pack_fingerprint,
    }


def _extract_failed_batch_index(detail: str) -> int | None:
    marker = "Batch "
    if marker not in detail:
        return None
    suffix = detail.split(marker, 1)[1]
    raw_index = suffix.split("/", 1)[0]
    try:
        return int(raw_index)
    except ValueError:
        return None


def _validate_assessment_identity(
    *,
    assessment: AsBuiltAssessment,
    pack: EvidencePack,
    project_id: int,
    assessment_id: str,
) -> WorkbenchError | None:
    expected: dict[str, object] = {
        "project_id": project_id,
        "assessment_id": assessment_id,
        "agent_version": AGENT_VERSION,
        "evidence_pack_builder_version": EVIDENCE_PACK_BUILDER_VERSION,
        "authority_fingerprint": pack.authority_fingerprint,
        "evidence_pack_fingerprint": pack.evidence_pack_fingerprint,
        "repo_snapshot.path": pack.repo_snapshot.path,
        "repo_snapshot.git_commit": pack.repo_snapshot.git_commit,
        "repo_snapshot.dirty": pack.repo_snapshot.dirty,
    }
    actual: dict[str, object] = {
        "project_id": assessment.project_id,
        "assessment_id": assessment.assessment_id,
        "agent_version": assessment.agent_version,
        "evidence_pack_builder_version": assessment.evidence_pack_builder_version,
        "authority_fingerprint": assessment.authority_fingerprint,
        "evidence_pack_fingerprint": assessment.evidence_pack_fingerprint,
        "repo_snapshot.path": assessment.repo_snapshot.path,
        "repo_snapshot.git_commit": assessment.repo_snapshot.git_commit,
        "repo_snapshot.dirty": assessment.repo_snapshot.dirty,
    }
    mismatches = [
        field
        for field, expected_value in expected.items()
        if actual[field] != expected_value
    ]
    if not mismatches:
        return None
    return _mutation_failed(
        "As-built assessment identity does not match the host evidence pack.",
        {
            "project_id": project_id,
            "mismatches": mismatches,
        },
    )


def _invalid_command(message: str, details: dict[str, Any]) -> WorkbenchError:
    return workbench_error(
        ErrorCode.INVALID_COMMAND,
        message=message,
        details=details,
    )


def _project_not_found(project_id: int) -> WorkbenchError:
    return workbench_error(
        ErrorCode.PROJECT_NOT_FOUND,
        message=f"Project {project_id} was not found.",
        details={"project_id": project_id},
        remediation=["agileforge project list"],
    )


def _authority_not_accepted(project_id: int) -> WorkbenchError:
    return workbench_error(
        ErrorCode.AUTHORITY_NOT_ACCEPTED,
        message="Project has no accepted authority.",
        details={"project_id": project_id},
        remediation=["agileforge authority status --project-id <id>"],
    )


def _authority_not_compiled(
    project_id: int,
    *,
    message: str = "Accepted authority is not compiled.",
) -> WorkbenchError:
    return workbench_error(
        ErrorCode.AUTHORITY_NOT_COMPILED,
        message=message,
        details={"project_id": project_id},
        remediation=["agileforge authority status --project-id <id>"],
    )


def _mutation_failed(message: str, details: dict[str, Any]) -> WorkbenchError:
    return workbench_error(
        ErrorCode.MUTATION_FAILED,
        message=message,
        details=details,
    )


def _idempotency_key_reused(idempotency_key: str) -> WorkbenchError:
    return workbench_error(
        ErrorCode.IDEMPOTENCY_KEY_REUSED,
        message="Idempotency key was already used for different inputs.",
        details={"idempotency_key": idempotency_key},
        remediation=["Use a new --idempotency-key for changed assessment inputs."],
    )


def build_authority_targets(
    compiled: dict[str, Any],
) -> tuple[list[AuthorityTarget], list[EvidenceWarning], list[str]]:
    """Extract assessment targets from accepted authority."""
    warnings: list[EvidenceWarning] = []
    limitations: list[str] = []
    targets = _targets_from_invariants(compiled)
    if not targets:
        targets = _targets_from_items(compiled)

    if len(targets) > MAX_AUTHORITY_TARGETS:
        warnings.append(
            EvidenceWarning(
                code="AS_BUILT_AUTHORITY_TRUNCATED",
                message="Authority target list exceeded the Phase 1 cap.",
                details={
                    "target_count": len(targets),
                    "max_authority_targets": MAX_AUTHORITY_TARGETS,
                },
            )
        )
        targets = targets[:MAX_AUTHORITY_TARGETS]

    if not targets:
        warnings.append(
            EvidenceWarning(
                code="AS_BUILT_NO_AUTHORITY_TARGETS",
                message="No authority targets were extracted.",
                details={
                    "target_sources": [
                        "compiled_authority.invariants[]",
                        "compiled_authority.items[]",
                    ]
                },
            )
        )
        limitations.append("No authority targets were extracted.")

    return targets, warnings, limitations


def build_evidence_pack(  # noqa: PLR0913
    *,
    project_id: int,
    authority_fingerprint: str,
    compiled_authority: dict[str, Any],
    repo_path: Path,
    spec_mode: SpecMode,
    spec_file: Path | None,
) -> EvidencePack:
    """Build a bounded host-side evidence pack for the assessment agent."""
    repo = repo_path.resolve()
    if not repo.exists() or not repo.is_dir():
        msg = "repo path is not a readable directory"
        raise ValueError(msg)

    targets, target_warnings, limitations = build_authority_targets(
        compiled_authority
    )
    snapshot = _repo_snapshot(repo)
    files, skipped_counts = _scannable_files(repo)
    source_snippets, test_snippets, doc_snippets, search_observations = (
        _collect_target_evidence(
            files=files,
            targets=targets,
            skipped_counts=skipped_counts,
        )
    )

    warnings = [*target_warnings]
    if snapshot.dirty:
        warnings.append(
            EvidenceWarning(
                code="AS_BUILT_REPO_DIRTY",
                message="Repository has uncommitted changes.",
                details={"repo_path": snapshot.path},
            )
        )
    if skipped_counts:
        warnings.append(
            EvidenceWarning(
                code="AS_BUILT_SKIPPED_PATHS",
                message="Some repository paths were skipped during bounded scanning.",
                details={"counts": skipped_counts},
            )
        )
    truncated_count = skipped_counts.get("manifest_truncated", 0)
    if truncated_count:
        warnings.append(
            EvidenceWarning(
                code="AS_BUILT_FILE_MANIFEST_TRUNCATED",
                message="Repository file manifest exceeded the Phase 1 cap.",
                details={
                    "omitted_file_count": truncated_count,
                    "max_file_manifest_entries": MAX_FILE_MANIFEST_ENTRIES,
                },
            )
        )
        limitations.append(
            "File manifest was truncated by the Phase 1 scan cap; omitted files "
            "were not searched."
        )

    summary = _manifest_summary(
        files=files,
        skipped_counts=skipped_counts,
        spec_file=spec_file,
        spec_mode=spec_mode,
        project_id=project_id,
    )
    pack = EvidencePack(
        schema_version=EVIDENCE_PACK_SCHEMA_VERSION,
        builder_version=EVIDENCE_PACK_BUILDER_VERSION,
        authority_fingerprint=authority_fingerprint,
        evidence_pack_fingerprint="sha256:pending",
        generated_at=utc_now_iso(),
        repo_snapshot=snapshot,
        warnings=warnings,
        file_manifest_summary=summary,
        authority_targets=targets,
        source_snippets=list(source_snippets.values()),
        test_snippets=list(test_snippets.values()),
        doc_snippets=list(doc_snippets.values()),
        cli_observations=[],
        search_observations=search_observations,
        limitations=limitations,
    )
    return _finalize_pack(pack)


def split_evidence_pack_for_assessment(
    pack: EvidencePack,
    *,
    batch_size: int,
) -> list[EvidencePack]:
    """Split one full evidence pack into deterministic assessor batch packs."""
    if batch_size < 1:
        msg = "batch_size must be at least 1"
        raise ValueError(msg)
    if not pack.authority_targets or len(pack.authority_targets) <= batch_size:
        return [pack]

    batches: list[EvidencePack] = []
    for start in range(0, len(pack.authority_targets), batch_size):
        end = start + batch_size
        batches.append(_slice_evidence_pack(pack=pack, start=start, end=end))
    return batches


def merge_batch_assessments(
    *,
    project_id: int,
    assessment_id: str,
    full_pack: EvidencePack,
    batch_assessments: list[AsBuiltAssessment],
) -> AsBuiltAssessment:
    """Merge validated batch assessments into one full-pack assessment."""
    expected_keys = [_target_key(target) for target in full_pack.authority_targets]
    capabilities: dict[tuple[str, tuple[str, ...]], CapabilityAssessment] = {}
    duplicates: set[tuple[str, tuple[str, ...]]] = set()
    cross_cutting_findings: list[str] = []
    open_questions: list[str] = []
    clarifying_questions: list[str] = []
    for index, assessment in enumerate(batch_assessments, start=1):
        for capability in assessment.capability_assessments:
            key = _capability_key(capability)
            if key in capabilities:
                duplicates.add(key)
            else:
                capabilities[key] = capability
        cross_cutting_findings.extend(
            f"[batch {index}] {item}" for item in assessment.cross_cutting_findings
        )
        open_questions.extend(
            f"[batch {index}] {item}" for item in assessment.open_questions
        )
        clarifying_questions.extend(
            f"[batch {index}] {item}" for item in assessment.clarifying_questions
        )

    missing = [key for key in expected_keys if key not in capabilities]
    extra = [key for key in capabilities if key not in expected_keys]
    if missing or extra or duplicates:
        msg = (
            "Batch assessment coverage did not match authority targets: "
            f"missing={len(missing)} extra={len(extra)} "
            f"duplicates={len(duplicates)}"
        )
        raise ValueError(msg)

    ordered_capabilities = [capabilities[key] for key in expected_keys]
    batch_count = len(batch_assessments)
    return AsBuiltAssessment(
        schema_version=ASSESSMENT_SCHEMA_VERSION,
        project_id=project_id,
        assessment_id=assessment_id,
        agent_version=AGENT_VERSION,
        evidence_pack_builder_version=EVIDENCE_PACK_BUILDER_VERSION,
        authority_fingerprint=full_pack.authority_fingerprint,
        evidence_pack_fingerprint=full_pack.evidence_pack_fingerprint,
        generated_at=utc_now_iso(),
        assessment_summary=(
            f"Merged {batch_count} As-Built assessment batch(es) covering "
            f"{len(ordered_capabilities)} authority target(s)."
        ),
        repo_snapshot=full_pack.repo_snapshot,
        capability_assessments=ordered_capabilities,
        cross_cutting_findings=cross_cutting_findings,
        open_questions=open_questions,
        is_complete=all(assessment.is_complete for assessment in batch_assessments),
        clarifying_questions=clarifying_questions,
    )


def _target_key(target: AuthorityTarget) -> tuple[str, tuple[str, ...]]:
    return (target.authority_ref, tuple(target.invariant_refs))


def _capability_key(
    capability: CapabilityAssessment,
) -> tuple[str, tuple[str, ...]]:
    return (capability.authority_ref, tuple(capability.invariant_refs))


def _slice_evidence_pack(
    *,
    pack: EvidencePack,
    start: int,
    end: int,
) -> EvidencePack:
    selected_observations = pack.search_observations[start:end]
    referenced_paths = {
        path
        for observation in selected_observations
        for path in observation.paths
    }
    sliced = pack.model_copy(
        update={
            "evidence_pack_fingerprint": "sha256:pending",
            "authority_targets": pack.authority_targets[start:end],
            "source_snippets": [
                snippet
                for snippet in pack.source_snippets
                if snippet.path in referenced_paths
            ],
            "test_snippets": [
                snippet
                for snippet in pack.test_snippets
                if snippet.path in referenced_paths
            ],
            "doc_snippets": [
                snippet
                for snippet in pack.doc_snippets
                if snippet.path in referenced_paths
            ],
            "search_observations": selected_observations,
        }
    )
    return _finalize_pack(sliced)


def _collect_target_evidence(
    *,
    files: list[tuple[Path, Path, EvidenceKind]],
    targets: list[AuthorityTarget],
    skipped_counts: dict[str, int],
) -> tuple[
    dict[str, EvidenceSnippet],
    dict[str, EvidenceSnippet],
    dict[str, EvidenceSnippet],
    list[SearchObservation],
]:
    snippet_buckets: dict[str, dict[str, EvidenceSnippet]] = {
        "source": {},
        "test": {},
        "doc": {},
    }
    search_observations: list[SearchObservation] = []
    scanned_files = _read_scannable_file_contents(
        files=files,
        skipped_counts=skipped_counts,
    )

    for target in targets:
        target_matches = 0
        matched_paths: list[str] = []
        target_snippet_count = 0
        for scanned_file in scanned_files:
            matches = _matched_terms(
                scanned_file.text,
                target.terms,
                lower_text=scanned_file.lower_text,
            )
            if not matches:
                continue
            target_matches += 1
            matched_paths.append(scanned_file.relative_path.as_posix())
            if target_snippet_count >= MAX_SNIPPETS_PER_TARGET:
                continue
            target_snippet_count += 1
            snippet = _snippet_for_match(
                text=scanned_file.text,
                relative_path=scanned_file.relative_path,
                kind=scanned_file.kind,
                matched_terms=matches,
            )
            bucket = (
                "test"
                if scanned_file.kind == "test"
                else "doc"
                if scanned_file.kind == "doc"
                else "source"
            )
            snippet_buckets[bucket].setdefault(
                f"{scanned_file.kind}:{scanned_file.relative_path.as_posix()}",
                snippet,
            )
        search_observations.append(
            SearchObservation(
                query=target.authority_ref,
                match_count=target_matches,
                paths=matched_paths[:MAX_SNIPPETS_PER_TARGET],
            )
        )
    return (
        snippet_buckets["source"],
        snippet_buckets["test"],
        snippet_buckets["doc"],
        search_observations,
    )


def _read_scannable_file_contents(
    *,
    files: list[tuple[Path, Path, EvidenceKind]],
    skipped_counts: dict[str, int],
) -> list[_ScannedFile]:
    """Read each candidate file once before matching authority targets."""
    scanned_files: list[_ScannedFile] = []
    for file_path, relative_path, kind in files:
        try:
            text = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            skipped_counts["unreadable"] = skipped_counts.get("unreadable", 0) + 1
            continue
        scanned_files.append(
            _ScannedFile(
                relative_path=relative_path,
                kind=kind,
                text=text,
                lower_text=text.lower(),
            )
        )
    return scanned_files


def _targets_from_invariants(compiled: dict[str, Any]) -> list[AuthorityTarget]:
    invariants = compiled.get("invariants")
    if not isinstance(invariants, list):
        return []
    source_terms = _source_map_terms(compiled)
    targets: list[AuthorityTarget] = []
    for invariant in invariants:
        if not isinstance(invariant, dict):
            continue
        invariant_id = _str_or_none(invariant.get("id"))
        if not invariant_id:
            continue
        invariant_type = _str_or_none(invariant.get("type"))
        parameters = _dict_or_empty(invariant.get("parameters"))
        source_requirement_id = _str_or_none(parameters.get("source_item_id"))
        authority_ref = source_requirement_id or invariant_id
        terms = _unique_terms(
            [
                invariant_id,
                authority_ref,
                invariant_type,
                *source_terms.get(authority_ref, []),
                *_flatten_terms(parameters),
            ]
        )
        targets.append(
            AuthorityTarget(
                authority_ref=authority_ref,
                invariant_refs=[invariant_id],
                title=_title_from_ref(authority_ref),
                invariant_type=invariant_type,
                source_requirement_id=source_requirement_id,
                terms=terms,
                parameters=parameters,
            )
        )
    return targets


def _targets_from_items(compiled: dict[str, Any]) -> list[AuthorityTarget]:
    items = compiled.get("items")
    if not isinstance(items, list):
        return []
    targets: list[AuthorityTarget] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = _str_or_none(item.get("id"))
        if not item_id:
            continue
        item_type = _str_or_none(item.get("type"))
        targets.append(
            AuthorityTarget(
                authority_ref=item_id,
                invariant_refs=[],
                title=_title_from_ref(item_id),
                invariant_type=item_type,
                source_requirement_id=item_id,
                terms=_unique_terms([item_id, item_type, *_flatten_terms(item)]),
                parameters=item,
            )
        )
    return targets


def _source_map_terms(compiled: dict[str, Any]) -> dict[str, list[str]]:
    source_map = compiled.get("source_map")
    if not isinstance(source_map, list):
        return {}
    result: dict[str, list[str]] = {}
    for entry in source_map:
        if not isinstance(entry, dict):
            continue
        source_id = _str_or_none(entry.get("source_item_id"))
        if not source_id:
            continue
        result.setdefault(source_id, []).extend(_flatten_terms(entry))
    return result


def _flatten_terms(value: object) -> list[str]:
    if value is None or isinstance(value, bool | int | float):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        terms: list[str] = []
        for item in value.values():
            terms.extend(_flatten_terms(item))
        return terms
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        terms = []
        for item in value:
            terms.extend(_flatten_terms(item))
        return terms
    return []


def _unique_terms(values: Iterable[str | None]) -> list[str]:
    seen: set[str] = set()
    terms: list[str] = []
    for value in values:
        if value is None:
            continue
        stripped = value.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        terms.append(stripped)
    return terms


def _title_from_ref(ref: str) -> str:
    tail = ref.split(".", 1)[-1]
    return tail.replace("-", " ").replace("_", " ").title()


def _str_or_none(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _dict_or_empty(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _repo_snapshot(repo: Path) -> RepoSnapshot:
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
    return RepoSnapshot(path=str(repo), git_commit=git_commit, dirty=dirty)


def _scannable_files(
    repo: Path,
) -> tuple[list[tuple[Path, Path, EvidenceKind]], dict[str, int]]:
    files: list[tuple[Path, Path, EvidenceKind]] = []
    skipped: dict[str, int] = {}
    for root, dir_names, file_names in repo.walk():
        skipped_dirs = [name for name in dir_names if name in _SKIP_DIR_NAMES]
        if skipped_dirs:
            skipped["runtime_dir"] = skipped.get("runtime_dir", 0) + len(skipped_dirs)
        dir_names[:] = [name for name in dir_names if name not in _SKIP_DIR_NAMES]
        for file_name in sorted(file_names):
            file_path = root / file_name
            relative_path = file_path.relative_to(repo)
            reason = _skip_file_reason(file_path, relative_path)
            if reason is not None:
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            files.append((file_path, relative_path, _file_kind(relative_path)))
    files.sort(key=lambda entry: entry[1].as_posix())
    if len(files) > MAX_FILE_MANIFEST_ENTRIES:
        skipped["manifest_truncated"] = len(files) - MAX_FILE_MANIFEST_ENTRIES
    return files[:MAX_FILE_MANIFEST_ENTRIES], skipped


def _skip_file_reason(  # noqa: PLR0911
    file_path: Path,
    relative_path: Path,
) -> str | None:
    if file_path.is_symlink():
        return "symlink"
    if not file_path.is_file():
        return "non_regular"
    if file_path.name in _SKIP_FILE_NAMES:
        return "lockfile"
    if file_path.suffix.lower() in _SKIP_SUFFIXES:
        return "unsupported_suffix"
    if file_path.stat().st_size > MAX_SCAN_BYTES:
        return "oversized"
    if any(part in _SKIP_DIR_NAMES for part in relative_path.parts):
        return "runtime_dir"
    return None


def _file_kind(relative_path: Path) -> EvidenceKind:
    parts = set(relative_path.parts[:-1])
    name = relative_path.name
    suffix = relative_path.suffix.lower()
    if "tests" in parts or "test" in parts:
        return "test"
    if name.startswith("test_") and suffix == ".py":
        return "test"
    if name.endswith("_test.py") or name.endswith(_TEST_SUFFIXES):
        return "test"
    if parts & _DOC_DIR_NAMES or suffix in _DOC_SUFFIXES:
        return "doc"
    if name in _CONFIG_NAMES or suffix in _CONFIG_SUFFIXES:
        return "config"
    return "source"


def _matched_terms(
    text: str,
    terms: list[str],
    *,
    lower_text: str | None = None,
) -> list[str]:
    """Return terms found in text, reusing lowercased content when supplied."""
    lowered = text.lower() if lower_text is None else lower_text
    return [term for term in terms if _term_matches(text, lowered, term)]


def _term_matches(text: str, lower_text: str, term: str) -> bool:
    if term.startswith(_ID_TERM_PREFIXES):
        return _id_term_matches(text, term)
    return term.lower() in lower_text


def _id_term_matches(text: str, term: str) -> bool:
    """Return whether an authority ID term appears with token boundaries."""
    start = 0
    term_length = len(term)
    while True:
        index = text.find(term, start)
        if index < 0:
            return False
        before_index = index - 1
        after_index = index + term_length
        before_ok = before_index < 0 or text[before_index] not in _ID_BOUNDARY_CHARS
        after_ok = (
            after_index >= len(text)
            or text[after_index] not in _ID_BOUNDARY_CHARS
        )
        if before_ok and after_ok:
            return True
        start = index + 1


def _snippet_for_match(
    *,
    text: str,
    relative_path: Path,
    kind: EvidenceKind,
    matched_terms: list[str],
) -> EvidenceSnippet:
    lines = text.splitlines()
    first_match_line = _first_match_line(lines, matched_terms)
    start = max(first_match_line - 3, 1)
    end = min(start + MAX_SNIPPET_LINES - 1, len(lines) or 1)
    snippet_text = "\n".join(lines[start - 1 : end])
    encoded = snippet_text.encode("utf-8")
    if len(encoded) > MAX_SNIPPET_BYTES:
        snippet_text = encoded[:MAX_SNIPPET_BYTES].decode("utf-8", errors="ignore")
    return EvidenceSnippet(
        kind=kind,
        path=relative_path.as_posix(),
        line_start=start,
        line_end=end,
        matched_terms=matched_terms[:MAX_SNIPPETS_PER_TARGET],
        text=snippet_text,
        summary=f"Matched {len(matched_terms)} authority term(s).",
    )


def _first_match_line(lines: list[str], terms: list[str]) -> int:
    for index, line in enumerate(lines, start=1):
        if _matched_terms(line, terms):
            return index
    return 1


def _manifest_summary(
    *,
    files: list[tuple[Path, Path, EvidenceKind]],
    skipped_counts: dict[str, int],
    spec_file: Path | None,
    spec_mode: SpecMode,
    project_id: int,
) -> dict[str, Any]:
    kind_counts: dict[str, int] = {}
    for _path, _relative, kind in files:
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
    summary: dict[str, Any] = {
        "project_id": project_id,
        "spec_mode": spec_mode,
        "total_files": len(files) + sum(skipped_counts.values()),
        "included_files": len(files),
        "skipped_files": sum(skipped_counts.values()),
        "skipped_counts": skipped_counts,
        "kind_counts": kind_counts,
    }
    if spec_file is not None:
        try:
            summary["spec_file"] = {
                "path": str(spec_file),
                "sha256": _file_sha256(spec_file),
            }
        except OSError as exc:
            summary["spec_file"] = {
                "path": str(spec_file),
                "error": str(exc),
            }
    return summary


def _finalize_pack(pack: EvidencePack) -> EvidencePack:
    current = pack
    warnings = list(current.warnings)
    while len(canonical_json(_pack_fingerprint_payload(current))) > MAX_PACK_BYTES:
        if current.doc_snippets:
            current = current.model_copy(
                update={"doc_snippets": current.doc_snippets[:-1]}
            )
        elif current.test_snippets:
            current = current.model_copy(
                update={"test_snippets": current.test_snippets[:-1]}
            )
        elif current.source_snippets:
            current = current.model_copy(
                update={"source_snippets": current.source_snippets[:-1]}
            )
        else:
            break
        if not any(warning.code == "AS_BUILT_PACK_TRUNCATED" for warning in warnings):
            warnings.append(
                EvidenceWarning(
                    code="AS_BUILT_PACK_TRUNCATED",
                    message=(
                        "Evidence pack exceeded size cap and snippets were truncated."
                    ),
                    details={"max_pack_bytes": MAX_PACK_BYTES},
                )
            )
        current = current.model_copy(update={"warnings": warnings})
    fingerprint = canonical_hash(_pack_fingerprint_payload(current))
    return current.model_copy(update={"evidence_pack_fingerprint": fingerprint})


def _pack_fingerprint_payload(pack: EvidencePack) -> dict[str, Any]:
    payload = pack.model_dump(mode="json")
    payload.pop("evidence_pack_fingerprint", None)
    payload.pop("generated_at", None)
    return payload


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_assessment_json(assessment: AsBuiltAssessment) -> str:
    """Return canonical JSON for persisted assessment state."""
    return canonical_json(assessment.model_dump(mode="json"))


def assessment_from_json(value: str) -> AsBuiltAssessment:
    """Parse one persisted assessment JSON string."""
    return AsBuiltAssessment.model_validate_json(value)


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return cast("dict[str, Any]", decoded) if isinstance(decoded, dict) else {}
