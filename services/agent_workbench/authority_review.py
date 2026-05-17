"""Read-only pending authority review packet service."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from json import JSONDecodeError
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from sqlmodel import Session

from models import db as model_db
from models.core import Product
from services.agent_workbench.authority_projection import (
    _AUTHORITY_REQUIREMENTS,
    _iso_z,
    _load_authority_selection,
    _project_not_found_error,
    _schema_error,
    pending_authority_fingerprint,
)
from services.agent_workbench.envelope import error_envelope
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.agent_workbench.schema_readiness import check_schema_readiness
from services.specs.compiler_service import load_compiled_artifact

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from models.specs import CompiledSpecAuthority, SpecRegistry
    from utils.spec_schemas import Invariant

JsonDict = dict[str, Any]

AUTHORITY_REVIEW_COMMAND: Final[str] = "agileforge authority review"
REVIEW_TOKEN_SCHEMA: Final[str] = "agileforge.authority_review.v1"  # noqa: S105
COVERAGE_SCHEMA: Final[str] = "agileforge.authority_coverage_summary.v1"
DEFAULT_REVIEW_SOURCE_LIMIT_BYTES: Final[int] = 262_144

_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_NORMATIVE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b("
    r"must|required|shall|only|never|cannot|forbidden|accepted when|"
    r"rejected when|input|output|schema|field|constraint"
    r")\b",
    re.IGNORECASE,
)
_REQUIREMENT_HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"\b("
    r"requirements|invariants|rules|acceptance|security|scope|out of scope|"
    r"schema|contract"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _SourceLoad:
    """Decoded source bytes and resolved path metadata."""

    raw_bytes: bytes
    text: str
    resolved_path: Path
    disk_sha256: str


@dataclass(frozen=True)
class AuthorityReviewSnapshot:
    """Canonical authority review-token snapshot plus packet render inputs."""

    schema: str
    project_id: int
    pending_authority_id: int | None
    authority_fingerprint: str | None
    source_spec_hash: str
    disk_spec_hash: str
    resolved_spec_path: str
    compiler_version: str
    prompt_hash: str
    fsm_state: str
    setup_status: str
    content_included: bool
    omission_assessment: str
    coverage_summary_fingerprint: str
    project_name: str
    spec_version_id: int | None
    content_ref: str | None
    disk_status: str
    size_bytes: int
    review_source_limit_bytes: int
    source_outline: list[JsonDict]
    coverage_summary: JsonDict
    coverage_diagnostics: list[JsonDict]
    excerpt: str
    content_truncated: bool
    source_content: str | None
    source_content_sha256: str | None
    pending_spec_version_id: int
    compiled_at: str | None
    artifact: JsonDict

    @property
    def payload(self) -> JsonDict:
        """Return the canonical payload used for review-token hashing."""
        return {
            "schema": self.schema,
            "project_id": self.project_id,
            "pending_authority_id": self.pending_authority_id,
            "authority_fingerprint": self.authority_fingerprint,
            "source_spec_hash": self.source_spec_hash,
            "disk_spec_hash": self.disk_spec_hash,
            "resolved_spec_path": self.resolved_spec_path,
            "compiler_version": self.compiler_version,
            "prompt_hash": self.prompt_hash,
            "fsm_state": self.fsm_state,
            "setup_status": self.setup_status,
            "content_included": self.content_included,
            "omission_assessment": self.omission_assessment,
            "coverage_summary_fingerprint": self.coverage_summary_fingerprint,
        }

    @property
    def review_token(self) -> str:
        """Return the deterministic review token."""
        return f"{REVIEW_TOKEN_SCHEMA}:{canonical_json_hash(self.payload)}"

    @property
    def guard_tokens(self) -> JsonDict:
        """Return decision guard tokens derived from the canonical snapshot."""
        return {
            "review_token": self.review_token,
            "pending_authority_id": self.pending_authority_id,
            "expected_authority_fingerprint": self.authority_fingerprint,
            "expected_source_spec_hash": self.source_spec_hash,
            "expected_disk_spec_hash": self.disk_spec_hash,
            "expected_resolved_spec_path": self.resolved_spec_path,
            "expected_state": "SETUP_REQUIRED",
            "expected_setup_status": "authority_pending_review",
            "expected_content_included": self.content_included,
            "expected_omission_assessment": self.omission_assessment,
            "expected_coverage_summary_fingerprint": (
                self.coverage_summary_fingerprint
            ),
        }


@dataclass(frozen=True)
class _ContentBlock:
    """A parsed Markdown content block within a section."""

    text: str
    line_start: int
    line_end: int
    requirement_bearing: bool


@dataclass
class _Section:
    """A Markdown section with parsed content blocks."""

    section_id: str
    heading: str | None
    line_start: int
    line_end: int
    blocks: list[_ContentBlock] = field(default_factory=list)


@dataclass(frozen=True)
class _AuthorityEvidence:
    """Source evidence attached to a normalized authority item."""

    item_id: str
    source_refs: tuple[str, ...]
    source_excerpt: str | None


@dataclass(frozen=True)
class _ClassificationEvidence:
    """Non-authority classification evidence for uncovered source blocks."""

    item_id: str
    text: str
    kind: str


def sha256_prefixed(data: bytes) -> str:
    """Return a SHA-256 digest with the repo-standard prefix."""
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def canonical_json_hash(payload: Mapping[str, Any]) -> str:
    """Return a SHA-256 digest over sorted compact JSON."""
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return sha256_prefixed(encoded)


def coverage_summary_fingerprint(payload: Mapping[str, Any]) -> str:
    """Return the canonical coverage summary fingerprint."""
    return canonical_json_hash(_canonicalize_coverage_payload(payload))


def _canonicalize_coverage_payload(value: object) -> object:
    """Sort nested coverage arrays before hashing."""
    if isinstance(value, Mapping):
        result: JsonDict = {}
        for key, item in value.items():
            if key in {"covered_by", "source_refs", "classification_ids"}:
                result[str(key)] = sorted({str(entry) for entry in _as_list(item)})
            elif key == "source_outline" and isinstance(item, Sequence):
                outline = [
                    _canonicalize_coverage_payload(entry)
                    for entry in item
                    if isinstance(entry, Mapping)
                ]
                result[str(key)] = sorted(
                    outline,
                    key=lambda entry: (
                        _sort_int(entry.get("line_start")),
                        str(entry.get("section_id", "")),
                    ),
                )
            else:
                result[str(key)] = _canonicalize_coverage_payload(item)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonicalize_coverage_payload(item) for item in value]
    return value


def _as_list(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _sort_int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


class AuthorityReviewService:
    """Read-only service that builds pending authority review packets."""

    def __init__(self, *, engine: Engine | None = None) -> None:
        """Initialize the review service with a read-only target engine."""
        self._engine = engine or model_db.get_engine()
        self._repo_root = Path(__file__).resolve().parents[2]

    def review(  # noqa: PLR0911
        self,
        *,
        project_id: int,
        include_spec: str = "auto",
        output_format: str = "json",
    ) -> dict[str, Any]:
        """Return a deterministic review packet for pending authority."""
        if include_spec not in {"auto", "full", "summary"}:
            return _invalid_input_error(
                "include_spec",
                include_spec,
                ["auto", "full", "summary"],
            )
        if output_format != "json":
            return _invalid_input_error("output_format", output_format, ["json"])

        readiness = check_schema_readiness(self._engine, _AUTHORITY_REQUIREMENTS)
        if not readiness.ok:
            return _schema_error(AUTHORITY_REVIEW_COMMAND, readiness)

        with Session(self._engine) as session:
            product = session.get(Product, project_id)
            if product is None:
                return _project_not_found_error(AUTHORITY_REVIEW_COMMAND, project_id)

            selection = _load_authority_selection(session, project_id=project_id)
            latest_spec = selection.latest_spec
            authority = selection.pending_authority
            if latest_spec is None or authority is None:
                return _authority_not_pending_error(project_id)

            snapshot = build_authority_review_snapshot(
                project_id=project_id,
                product=product,
                spec=latest_spec,
                authority=authority,
                include_spec=include_spec,
                repo_root=self._repo_root,
            )
            if isinstance(snapshot, dict):
                return snapshot

            return _success(_render_review_packet(snapshot))


def _success(data: JsonDict) -> JsonDict:
    return {"ok": True, "data": data, "warnings": [], "errors": []}


def _invalid_input_error(field_name: str, value: str, allowed: list[str]) -> JsonDict:
    return error_envelope(
        command=AUTHORITY_REVIEW_COMMAND,
        error=workbench_error(
            ErrorCode.INVALID_COMMAND,
            message=f"Unsupported {field_name}: {value}.",
            details={"field": field_name, "value": value, "allowed": allowed},
            remediation=[f"Use one of: {', '.join(allowed)}."],
        ),
    )


def _authority_not_pending_error(project_id: int) -> JsonDict:
    return error_envelope(
        command=AUTHORITY_REVIEW_COMMAND,
        error=workbench_error(
            ErrorCode.AUTHORITY_NOT_PENDING,
            message="No pending compiled authority exists for this project.",
            details={"project_id": project_id},
            remediation=["Compile a new pending authority before requesting review."],
        ),
    )


def _authority_source_changed_error(
    *,
    raw_path: str | None,
    resolved_path: Path,
    registry_hash: str,
    disk_hash: str,
) -> JsonDict:
    return error_envelope(
        command=AUTHORITY_REVIEW_COMMAND,
        error=workbench_error(
            ErrorCode.AUTHORITY_SOURCE_CHANGED,
            message=(
                "Stored specification path content does not match the latest "
                "registry spec hash."
            ),
            details={
                "path": raw_path,
                "resolved_path": str(resolved_path),
                "registry_spec_hash": registry_hash,
                "disk_spec_hash": disk_hash,
            },
            remediation=[
                "Re-register or recompile the specification before review."
            ],
        ),
    )


def _spec_file_not_found_error(
    raw_path: str | None,
    resolved_path: Path | None,
) -> JsonDict:
    return error_envelope(
        command=AUTHORITY_REVIEW_COMMAND,
        error=workbench_error(
            ErrorCode.SPEC_FILE_NOT_FOUND,
            message="Stored specification path could not be found on disk.",
            details={
                "path": raw_path,
                "resolved_path": str(resolved_path) if resolved_path else None,
            },
            remediation=["Restore the specification file or update the stored path."],
        ),
    )


def _spec_file_invalid_error(
    raw_path: str | None,
    resolved_path: Path,
    reason: str,
) -> JsonDict:
    return error_envelope(
        command=AUTHORITY_REVIEW_COMMAND,
        error=workbench_error(
            ErrorCode.SPEC_FILE_INVALID,
            message="Stored specification file is not valid strict UTF-8 text.",
            details={
                "path": raw_path,
                "resolved_path": str(resolved_path),
                "reason": reason,
            },
            remediation=["Save the specification as valid UTF-8 and retry review."],
        ),
    )


def _load_source_from_latest_spec(
    spec: SpecRegistry,
    *,
    repo_root: Path,
) -> _SourceLoad | JsonDict:
    raw_path = spec.content_ref
    if not raw_path or not raw_path.strip():
        return _spec_file_not_found_error(raw_path, None)
    path = Path(raw_path).expanduser()
    resolved_path = path if path.is_absolute() else (repo_root / path)
    resolved_path = resolved_path.expanduser().resolve(strict=False)
    if not resolved_path.is_file():
        return _spec_file_not_found_error(raw_path, resolved_path)
    try:
        raw_bytes = resolved_path.read_bytes()
    except OSError as exc:
        return _spec_file_invalid_error(raw_path, resolved_path, str(exc))
    disk_hash = sha256_prefixed(raw_bytes)
    registry_hash = _normalize_sha256_hash(spec.spec_hash)
    if disk_hash != registry_hash:
        return _authority_source_changed_error(
            raw_path=raw_path,
            resolved_path=resolved_path,
            registry_hash=registry_hash,
            disk_hash=disk_hash,
        )
    try:
        text = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        return _spec_file_invalid_error(raw_path, resolved_path, str(exc))
    return _SourceLoad(
        raw_bytes=raw_bytes,
        text=text,
        resolved_path=resolved_path,
        disk_sha256=disk_hash,
    )


def _normalize_sha256_hash(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith("sha256:"):
        return f"sha256:{stripped.removeprefix('sha256:').lower()}"
    return f"sha256:{stripped.lower()}"


def build_authority_review_snapshot(  # noqa: PLR0913
    *,
    project_id: int,
    product: Product | None = None,
    spec: SpecRegistry | None = None,
    authority: CompiledSpecAuthority | None = None,
    include_spec: str = "auto",
    repo_root: Path | None = None,
    engine: Engine | None = None,
) -> AuthorityReviewSnapshot | JsonDict:
    """Build the canonical review snapshot without rendering a packet."""
    if product is None or spec is None or authority is None:
        review_engine = engine or model_db.get_engine()
        with Session(review_engine) as session:
            product = session.get(Product, project_id)
            if product is None:
                return _project_not_found_error(AUTHORITY_REVIEW_COMMAND, project_id)
            selection = _load_authority_selection(session, project_id=project_id)
            spec = selection.latest_spec
            authority = selection.pending_authority
            if spec is None or authority is None:
                return _authority_not_pending_error(project_id)
            return build_authority_review_snapshot(
                project_id=project_id,
                product=product,
                spec=spec,
                authority=authority,
                include_spec=include_spec,
                repo_root=repo_root,
            )

    source = _load_source_from_latest_spec(
        spec,
        repo_root=repo_root or Path(__file__).resolve().parents[2],
    )
    if isinstance(source, dict):
        return source

    source_limit = _review_source_limit()
    content_included = include_spec == "full" or (
        include_spec == "auto" and len(source.raw_bytes) <= source_limit
    )
    content_truncated = not content_included and len(source.raw_bytes) > source_limit
    artifact, authority_evidence, classification_evidence = (
        _authority_artifact_payload(authority)
    )
    outline, coverage_summary, diagnostics = _coverage_payload(
        text=source.text,
        authority_evidence=authority_evidence,
        classification_evidence=classification_evidence,
    )
    source_content_sha256 = (
        sha256_prefixed(source.text.encode("utf-8")) if content_included else None
    )
    coverage_payload = {
        "schema": COVERAGE_SCHEMA,
        "spec_version_id": spec.spec_version_id,
        "resolved_spec_path": str(source.resolved_path),
        "source_content_sha256": source_content_sha256,
        "content_included": content_included,
        "content_truncated": content_truncated,
        "source_outline": outline,
        "coverage_summary": coverage_summary,
    }
    coverage_fingerprint = coverage_summary_fingerprint(coverage_payload)
    authority_fingerprint = pending_authority_fingerprint(authority)
    pending_authority_id = authority.authority_id
    source_spec_hash = _normalize_sha256_hash(spec.spec_hash)
    fsm_state = "SETUP_REQUIRED"
    setup_status = "authority_pending_review"
    omission_assessment = coverage_summary["omission_assessment"]

    return AuthorityReviewSnapshot(
        schema=REVIEW_TOKEN_SCHEMA,
        project_id=project_id,
        pending_authority_id=pending_authority_id,
        authority_fingerprint=authority_fingerprint,
        source_spec_hash=source_spec_hash,
        disk_spec_hash=source.disk_sha256,
        resolved_spec_path=str(source.resolved_path),
        compiler_version=authority.compiler_version,
        prompt_hash=authority.prompt_hash,
        fsm_state=fsm_state,
        setup_status=setup_status,
        content_included=content_included,
        omission_assessment=omission_assessment,
        coverage_summary_fingerprint=coverage_fingerprint,
        project_name=product.name,
        spec_version_id=spec.spec_version_id,
        content_ref=spec.content_ref,
        disk_status="readable",
        size_bytes=len(source.raw_bytes),
        review_source_limit_bytes=source_limit,
        source_outline=outline,
        coverage_summary=coverage_summary,
        coverage_diagnostics=diagnostics,
        excerpt=_bounded_excerpt(source.text),
        content_truncated=content_truncated,
        source_content=source.text if content_included else None,
        source_content_sha256=source_content_sha256,
        pending_spec_version_id=authority.spec_version_id,
        compiled_at=_iso_z(authority.compiled_at),
        artifact=artifact,
    )


def _render_review_packet(snapshot: AuthorityReviewSnapshot) -> JsonDict:
    spec_payload = {
        "spec_version_id": snapshot.spec_version_id,
        "content_ref": snapshot.content_ref,
        "resolved_path": snapshot.resolved_spec_path,
        "spec_hash": snapshot.source_spec_hash,
        "disk_status": snapshot.disk_status,
        "disk_sha256": snapshot.disk_spec_hash,
        "size_bytes": snapshot.size_bytes,
        "review_source_limit_bytes": snapshot.review_source_limit_bytes,
        "source_outline": snapshot.source_outline,
        "coverage_summary": snapshot.coverage_summary,
        "coverage_summary_fingerprint": snapshot.coverage_summary_fingerprint,
        "coverage_diagnostics": snapshot.coverage_diagnostics,
        "excerpt": snapshot.excerpt,
        "content_included": snapshot.content_included,
        "content_truncated": snapshot.content_truncated,
        "source_content": snapshot.source_content,
        "source_content_sha256": snapshot.source_content_sha256,
    }

    return {
        "project": {
            "project_id": snapshot.project_id,
            "name": snapshot.project_name,
            "fsm_state": snapshot.fsm_state,
            "setup_status": snapshot.setup_status,
        },
        "spec": spec_payload,
        "pending_authority": {
            "authority_id": snapshot.pending_authority_id,
            "spec_version_id": snapshot.pending_spec_version_id,
            "authority_fingerprint": snapshot.authority_fingerprint,
            "compiler_version": snapshot.compiler_version,
            "prompt_hash": snapshot.prompt_hash,
            "compiled_at": snapshot.compiled_at,
            "artifact": snapshot.artifact,
        },
        "review_guidance": _review_guidance(),
        "next_actions": [
            {
                "command": (
                    "agileforge authority accept --project-id "
                    f"{snapshot.project_id} --review-token {snapshot.review_token}"
                ),
                "mode": "human",
                "reason": "Record the reviewed pending authority as canonical.",
            },
            {
                "command": (
                    "agileforge authority reject --project-id "
                    f"{snapshot.project_id} --review-token "
                    f'{snapshot.review_token} --reason "..."'
                ),
                "mode": "human",
                "reason": "Record that the pending authority must not be used.",
            },
        ],
        "guard_tokens": snapshot.guard_tokens,
    }


def _review_source_limit() -> int:
    configured = os.environ.get("AGILEFORGE_AUTHORITY_REVIEW_SOURCE_LIMIT_BYTES")
    if configured is None:
        return DEFAULT_REVIEW_SOURCE_LIMIT_BYTES
    try:
        parsed = int(configured)
    except ValueError:
        return DEFAULT_REVIEW_SOURCE_LIMIT_BYTES
    return parsed if parsed >= 0 else DEFAULT_REVIEW_SOURCE_LIMIT_BYTES


def _bounded_excerpt(text: str, limit: int = 2_000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit]


def _authority_artifact_payload(
    authority: CompiledSpecAuthority,
) -> tuple[JsonDict, list[_AuthorityEvidence], list[_ClassificationEvidence]]:
    artifact = load_compiled_artifact(authority)
    if artifact is None:
        return _fallback_authority_artifact(authority), [], []

    source_map_by_id: dict[str, list[Any]] = {}
    for entry in artifact.source_map:
        source_map_by_id.setdefault(entry.invariant_id, []).append(entry)

    invariants: list[JsonDict] = []
    evidence: list[_AuthorityEvidence] = []
    for invariant in artifact.invariants:
        entries = source_map_by_id.get(invariant.id, [])
        refs = _dedupe_sorted(
            entry.location or entry.excerpt for entry in entries if entry.excerpt
        )
        excerpts = [entry.excerpt for entry in entries if entry.excerpt]
        source_excerpt = excerpts[0] if excerpts else None
        support = "direct" if refs or source_excerpt else "inferred"
        invariants.append(
            {
                "id": invariant.id,
                "text": _invariant_text(invariant),
                "support": support,
                "source_refs": refs,
                "source_excerpt": source_excerpt,
            }
        )
        evidence.append(
            _AuthorityEvidence(
                item_id=invariant.id,
                source_refs=tuple(refs),
                source_excerpt=source_excerpt,
            )
        )

    gaps = [
        _plain_item(item_id=f"GAP-{index}", text=gap)
        for index, gap in enumerate(artifact.gaps, start=1)
    ]
    assumptions = [
        _plain_item(item_id=f"ASM-{index}", text=assumption)
        for index, assumption in enumerate(artifact.assumptions, start=1)
    ]
    rejected_features = _normalized_persisted_items(
        _json_list(authority.rejected_features),
        prefix="REJ",
    )
    classification_evidence = [
        *[
            _ClassificationEvidence(
                item_id=str(item["id"]),
                text=str(item["text"]),
                kind="gap",
            )
            for item in gaps
        ],
        *[
            _ClassificationEvidence(
                item_id=str(item["id"]),
                text=str(item["text"]),
                kind="assumption",
            )
            for item in assumptions
        ],
        *[
            _ClassificationEvidence(
                item_id=str(item["id"]),
                text=str(item["text"]),
                kind="rejected_feature",
            )
            for item in rejected_features
        ],
    ]

    return (
        {
            "domain": artifact.domain,
            "scope_themes": list(artifact.scope_themes),
            "invariants": invariants,
            "eligible_feature_rules": [
                _plain_item(
                    item_id=f"ELIG-{index}",
                    text=rule.rule,
                )
                for index, rule in enumerate(artifact.eligible_feature_rules, start=1)
            ],
            "rejected_features": rejected_features,
            "gaps": gaps,
            "assumptions": assumptions,
            "source_map": {
                key: [
                    {
                        "excerpt": entry.excerpt,
                        "location": entry.location,
                    }
                    for entry in entries
                ]
                for key, entries in sorted(source_map_by_id.items())
            },
        },
        evidence,
        classification_evidence,
    )


def _fallback_authority_artifact(authority: CompiledSpecAuthority) -> JsonDict:
    assumptions = _normalized_persisted_items(
        _fallback_assumption_items(authority.compiled_artifact_json),
        prefix="ASM",
    )
    return {
        "domain": None,
        "scope_themes": _json_list(authority.scope_themes),
        "invariants": _normalized_persisted_items(
            _json_list(authority.invariants),
            prefix="INV",
        ),
        "eligible_feature_rules": _normalized_persisted_items(
            _json_list(authority.eligible_feature_ids),
            prefix="ELIG",
        ),
        "rejected_features": _normalized_persisted_items(
            _json_list(authority.rejected_features),
            prefix="REJ",
        ),
        "gaps": _normalized_persisted_items(
            _json_list(authority.spec_gaps),
            prefix="GAP",
        ),
        "assumptions": assumptions,
        "source_map": {},
    }


def _json_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _json_mapping(raw: str | None) -> Mapping[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _fallback_assumption_items(raw: str | None) -> list[Any]:
    parsed = _json_mapping(raw)
    assumptions = parsed.get("assumptions")
    if isinstance(assumptions, list):
        return assumptions
    result = parsed.get("result")
    if isinstance(result, Mapping) and isinstance(result.get("assumptions"), list):
        return list(result["assumptions"])
    root = parsed.get("root")
    if isinstance(root, Mapping) and isinstance(root.get("assumptions"), list):
        return list(root["assumptions"])
    return []


def _normalized_persisted_items(items: list[Any], *, prefix: str) -> list[JsonDict]:
    normalized: list[JsonDict] = []
    for index, item in enumerate(items, start=1):
        fallback_id = f"{prefix}-{index}"
        if isinstance(item, Mapping):
            source_refs = _dedupe_sorted(_as_list(item.get("source_refs")))
            source_excerpt = item.get("source_excerpt")
            normalized.append(
                {
                    "id": str(item.get("id") or fallback_id),
                    "text": _persisted_item_text(item),
                    "support": _persisted_item_support(item, source_refs),
                    "source_refs": source_refs,
                    "source_excerpt": (
                        str(source_excerpt) if source_excerpt is not None else None
                    ),
                }
            )
        else:
            normalized.append(_plain_item(item_id=fallback_id, text=str(item)))
    return normalized


def _persisted_item_text(item: Mapping[str, Any]) -> str:
    for key in ("text", "feature", "title", "reason", "rationale"):
        value = item.get(key)
        if value is not None and str(value):
            return str(value)
    return str(dict(item))


def _persisted_item_support(
    item: Mapping[str, Any],
    source_refs: list[str],
) -> str:
    support = item.get("support")
    if support in {"direct", "inferred"}:
        return str(support)
    if source_refs or item.get("source_excerpt"):
        return "direct"
    return "inferred"


def _plain_item(item_id: str, text: str) -> JsonDict:
    return {
        "id": item_id,
        "text": text,
        "support": "inferred",
        "source_refs": [],
        "source_excerpt": None,
    }


def _invariant_text(invariant: Invariant) -> str:
    parameters = invariant.parameters.model_dump()
    if parameters:
        parameter_text = ",".join(f"{key}={value}" for key, value in parameters.items())
        return f"{invariant.type.value}:{parameter_text}"
    return str(invariant.type.value)


def _coverage_payload(
    *,
    text: str,
    authority_evidence: list[_AuthorityEvidence],
    classification_evidence: list[_ClassificationEvidence],
) -> tuple[list[JsonDict], JsonDict, list[JsonDict]]:
    sections, diagnostics = _parse_markdown_sections(text)
    outline: list[JsonDict] = []
    unclassified_blocks = 0
    counts = {
        "covered_sections": 0,
        "partial_sections": 0,
        "intentionally_classified_sections": 0,
        "uncovered_sections": 0,
    }
    for section in sections:
        status, covered_by, classification_reason, section_unclassified = (
            _classify_section(
                section,
                authority_evidence,
                classification_evidence,
            )
        )
        unclassified_blocks += section_unclassified
        counts[f"{status}_sections"] += 1
        outline.append(
            {
                "section_id": section.section_id,
                "heading": section.heading,
                "line_start": section.line_start,
                "line_end": section.line_end,
                "coverage_status": status,
                "covered_by": covered_by,
                "classification_reason": classification_reason,
            }
        )
    complete = (
        not diagnostics
        and unclassified_blocks == 0
        and all(
            entry["coverage_status"] in {"covered", "intentionally_classified"}
            for entry in outline
        )
    )
    coverage_summary = {
        **counts,
        "unclassified_content_blocks": unclassified_blocks,
        "omission_assessment": "complete" if complete else "incomplete",
    }
    return outline, coverage_summary, diagnostics


def _parse_markdown_sections(text: str) -> tuple[list[_Section], list[JsonDict]]:
    lines = text.splitlines()
    sections: list[_Section] = []
    current = _Section("ROOT", None, 1, max(len(lines), 1))
    content_before_heading = False
    section_number = 0
    in_fence = False
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            content_before_heading = True
            continue
        if in_fence:
            if stripped:
                content_before_heading = True
            continue
        match = _HEADING_RE.match(line)
        if match is None:
            if stripped:
                content_before_heading = True
            continue
        if current.section_id != "ROOT" or content_before_heading:
            current.line_end = index - 1
            sections.append(current)
        section_number += 1
        current = _Section(
            section_id=f"S{section_number}",
            heading=match.group(2).strip(),
            line_start=index,
            line_end=len(lines),
        )
        content_before_heading = False
    if current.section_id != "ROOT" or content_before_heading or not sections:
        current.line_end = len(lines) if lines else 1
        sections.append(current)

    diagnostics: list[JsonDict] = []
    for section in sections:
        blocks, section_diagnostics = _parse_section_blocks(lines, section)
        section.blocks = blocks
        diagnostics.extend(section_diagnostics)
    diagnostics.sort(
        key=lambda item: (item["section_id"], item["code"], item["message"])
    )
    return sections, diagnostics


def _parse_section_blocks(  # noqa: C901
    lines: list[str],
    section: _Section,
) -> tuple[list[_ContentBlock], list[JsonDict]]:
    blocks: list[_ContentBlock] = []
    diagnostics: list[JsonDict] = []
    paragraph: list[tuple[int, str]] = []
    fence_lines: list[tuple[int, str]] = []
    in_fence = False
    fence_start = 0

    def flush_paragraph() -> None:
        if not paragraph:
            return
        line_start = paragraph[0][0]
        line_end = paragraph[-1][0]
        block_text = "\n".join(line for _line_no, line in paragraph).strip()
        blocks.append(_content_block(block_text, line_start, line_end, section.heading))
        paragraph.clear()

    for line_number in range(section.line_start, section.line_end + 1):
        line = lines[line_number - 1] if 0 <= line_number - 1 < len(lines) else ""
        if line_number == section.line_start and _HEADING_RE.match(line):
            continue
        stripped = line.strip()
        if stripped.startswith("```"):
            flush_paragraph()
            if in_fence:
                if fence_lines:
                    block_text = "\n".join(line for _line_no, line in fence_lines)
                    blocks.append(
                        _content_block(
                            block_text,
                            fence_start,
                            line_number,
                            section.heading,
                        )
                    )
                    fence_lines.clear()
                in_fence = False
            else:
                in_fence = True
                fence_start = line_number
            continue
        if in_fence:
            if stripped:
                fence_lines.append((line_number, line))
            continue
        if not stripped:
            flush_paragraph()
            continue
        if stripped.startswith(("- ", "* ", "+ ", "|")):
            flush_paragraph()
            blocks.append(
                _content_block(stripped, line_number, line_number, section.heading)
            )
            continue
        paragraph.append((line_number, line))
    flush_paragraph()
    if in_fence:
        if fence_lines:
            block_text = "\n".join(line for _line_no, line in fence_lines)
            blocks.append(
                _content_block(
                    block_text,
                    fence_start,
                    section.line_end,
                    section.heading,
                )
            )
        diagnostics.append(
            {
                "section_id": section.section_id,
                "code": "MARKDOWN_FENCE_UNCLOSED",
                "message": "Fenced code block was not closed before end of file.",
            }
        )
    return blocks, diagnostics


def _content_block(
    text: str,
    line_start: int,
    line_end: int,
    heading: str | None,
) -> _ContentBlock:
    requirement_bearing = bool(_NORMATIVE_RE.search(text)) or bool(
        heading and _REQUIREMENT_HEADING_RE.search(heading)
    )
    return _ContentBlock(
        text=text,
        line_start=line_start,
        line_end=line_end,
        requirement_bearing=requirement_bearing,
    )


def _classify_section(
    section: _Section,
    authority_evidence: list[_AuthorityEvidence],
    classification_evidence: list[_ClassificationEvidence],
) -> tuple[str, list[str], str | None, int]:
    requirement_blocks = [
        block for block in section.blocks if block.requirement_bearing
    ]
    if not requirement_blocks:
        return "covered", [], None, 0

    covered_blocks = 0
    classified_blocks = 0
    covered_by: set[str] = set()
    classification_reasons: set[str] = set()
    unclassified_blocks = 0
    for block in requirement_blocks:
        block_covered_by = _covered_by(block, authority_evidence)
        if block_covered_by:
            covered_blocks += 1
            covered_by.update(block_covered_by)
        else:
            classification_reason = _classification_reason(
                block,
                section,
                classification_evidence,
            )
            if classification_reason is None:
                unclassified_blocks += 1
            else:
                classified_blocks += 1
                classification_reasons.add(classification_reason)

    if covered_blocks == len(requirement_blocks):
        return "covered", sorted(covered_by), None, 0
    if covered_blocks + classified_blocks == len(requirement_blocks):
        return (
            "intentionally_classified",
            sorted(covered_by),
            "; ".join(sorted(classification_reasons)),
            0,
        )
    if covered_blocks > 0:
        return "partial", sorted(covered_by), None, unclassified_blocks
    return "uncovered", [], None, unclassified_blocks


def _covered_by(
    block: _ContentBlock,
    authority_evidence: list[_AuthorityEvidence],
) -> list[str]:
    matches: list[str] = []
    normalized_block = _normalize_evidence_text(block.text)
    for evidence in authority_evidence:
        candidates = [evidence.source_excerpt, *evidence.source_refs]
        for candidate in candidates:
            if not candidate:
                continue
            normalized_candidate = _normalize_evidence_text(candidate)
            if (
                normalized_candidate in normalized_block
                or normalized_block in normalized_candidate
            ):
                matches.append(evidence.item_id)
                break
    return sorted(set(matches))


def _classification_reason(
    block: _ContentBlock,
    section: _Section,
    classification_evidence: list[_ClassificationEvidence],
) -> str | None:
    """Return the reason a non-covered block is intentionally classified."""
    if section.heading and "out of scope" in section.heading.casefold():
        return f"out_of_scope_heading:{section.heading}"
    normalized_block = _normalize_evidence_text(block.text)
    for evidence in classification_evidence:
        normalized_evidence = _normalize_evidence_text(evidence.text)
        if (
            normalized_evidence in normalized_block
            or normalized_block in normalized_evidence
        ):
            return f"{evidence.kind}:{evidence.item_id}"
    return None


def _normalize_evidence_text(text: str) -> str:
    return " ".join(text.casefold().split())


def _dedupe_sorted(values: Iterable[object]) -> list[str]:
    return sorted({str(value) for value in values if value is not None and str(value)})


def _review_guidance() -> JsonDict:
    return {
        "decision_question": (
            "Does this compiled interpretation correctly represent the spec?"
        ),
        "acceptance_statement": (
            "Yes, this compiled interpretation correctly represents the spec. "
            "Use it as the canonical authority for later phases."
        ),
        "checklist": [
            (
                "Every mandatory requirement in the spec appears in the authority "
                "or is intentionally represented by a broader invariant."
            ),
            (
                "No authority invariant invents a requirement that is absent from "
                "the spec."
            ),
            "Forbidden capabilities and security constraints are captured.",
            "Known gaps are real gaps, not missed requirements.",
            "The source map points back to directly supporting spec sections.",
        ],
        "assessment_schema": {
            "recommendation": "accept | reject | needs_human",
            "confidence": "high | medium | low",
            "summary": "string",
            "blocking_findings": [],
            "non_blocking_findings": [],
            "missing_requirements": [],
            "invented_requirements": [],
            "gap_assessment": [],
            "decision_rationale": "string",
        },
    }
