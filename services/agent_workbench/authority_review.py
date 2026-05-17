"""Read-only pending authority review packet service."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from json import JSONDecodeError
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from pydantic import ValidationError
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
from utils import spec_authority_ir as authority_ir
from utils.spec_authority_ir import ContentBlock as _ContentBlock
from utils.spec_authority_ir import (
    Section as _Section,
)
from utils.spec_authority_ir import (
    parse_markdown_sections as _parse_markdown_sections,
)
from utils.spec_schemas import (
    SpecAuthorityCompilationFailure,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerOutput,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from models.specs import CompiledSpecAuthority, SpecRegistry
    from utils.spec_schemas import Invariant

JsonDict = dict[str, Any]

AUTHORITY_REVIEW_COMMAND: Final[str] = "agileforge authority review"
REVIEW_TOKEN_SCHEMA: Final[str] = "agileforge.authority_review.v1"  # noqa: S105
COVERAGE_SCHEMA: Final[str] = "agileforge.authority_coverage_summary.v1"
DEFAULT_REVIEW_SOURCE_LIMIT_BYTES: Final[int] = 262_144


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
    source_units: list[JsonDict]
    requirement_candidates: list[JsonDict]
    authority_mappings: list[JsonDict]
    review_findings: list[JsonDict]
    ir_provenance: str
    ir_packet_limits: JsonDict
    ir_coverage_summary: JsonDict
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
    canonical_payload = cast(
        "Mapping[str, Any]",
        _canonicalize_coverage_payload(payload),
    )
    return canonical_json_hash(canonical_payload)


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
        return cast("list[object]", value)
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
        if output_format not in {"json", "text"}:
            return _invalid_input_error(
                "output_format",
                output_format,
                ["json", "text"],
            )

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
            if not isinstance(snapshot, AuthorityReviewSnapshot):
                return cast("JsonDict", snapshot)

            packet = _render_review_packet(snapshot)
            if output_format == "text":
                packet["text"] = _render_review_text(packet)
            return _success(packet)


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
    if not isinstance(source, _SourceLoad):
        return cast("JsonDict", source)

    source_limit = _review_source_limit()
    content_included = include_spec == "full" or (
        include_spec == "auto" and len(source.raw_bytes) <= source_limit
    )
    content_truncated = not content_included and len(source.raw_bytes) > source_limit
    artifact, authority_evidence, classification_evidence = (
        _authority_artifact_payload(authority)
    )
    ir_payload = _authority_ir_payload(text=source.text, authority=authority)
    outline, coverage_summary, diagnostics = _coverage_payload(
        text=source.text,
        authority_evidence=authority_evidence,
        classification_evidence=classification_evidence,
    )
    artifact = _artifact_with_coverage_gaps(
        artifact,
        outline=outline,
        coverage_summary=coverage_summary,
        diagnostics=diagnostics,
    )
    artifact = _artifact_with_review_findings(
        artifact,
        review_findings=ir_payload["review_findings"],
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
        source_units=cast("list[JsonDict]", ir_payload["source_units"]),
        requirement_candidates=cast(
            "list[JsonDict]",
            ir_payload["requirement_candidates"],
        ),
        authority_mappings=cast("list[JsonDict]", ir_payload["authority_mappings"]),
        review_findings=cast("list[JsonDict]", ir_payload["review_findings"]),
        ir_provenance=str(ir_payload["ir_provenance"]),
        ir_packet_limits=cast("JsonDict", ir_payload["ir_packet_limits"]),
        ir_coverage_summary=cast("JsonDict", ir_payload["coverage_summary"]),
        excerpt=_bounded_excerpt(source.text),
        content_truncated=content_truncated,
        source_content=source.text if content_included else None,
        source_content_sha256=source_content_sha256,
        pending_spec_version_id=authority.spec_version_id,
        compiled_at=_iso_z(authority.compiled_at),
        artifact=artifact,
    )


def _authority_ir_payload(
    *,
    text: str,
    authority: CompiledSpecAuthority,
) -> JsonDict:
    """Build the host-derived review IR packet for rendering and decisions."""
    sections, diagnostics = authority_ir.parse_markdown_sections(text)
    source_units = authority_ir.source_units_from_sections(sections)
    candidates = authority_ir.extract_requirement_candidates(source_units)
    success = _load_compiled_artifact(authority)
    authority_items = _review_authority_items(success, authority)
    mapping_entries = _review_mapping_entries(success, candidates)
    mappings = authority_ir.build_authority_mappings(
        candidates,
        authority_items,
        mapping_entries,
    )
    ir_provenance = (
        success.ir_provenance
        if success is not None and success.ir_provenance is not None
        else authority_ir.IrProvenance.HOST_PARSED
    )
    findings = authority_ir.derive_review_findings(
        source_units,
        candidates,
        mappings,
        ir_provenance,
    )
    findings.extend(_diagnostic_review_findings(diagnostics))
    packet_truncated = _packet_would_truncate(source_units, candidates, findings)
    if packet_truncated:
        findings = [
            authority_ir.AuthorityReviewFinding(
                finding_id="AUTHORITY_REVIEW_PACKET_TRUNCATED:0",
                severity="blocking",
                code="AUTHORITY_REVIEW_PACKET_TRUNCATED",
                message=(
                    "Authority review packet exceeded render limits and cannot be "
                    "accepted with incomplete-review overrides."
                ),
                candidate_ids=[],
                source_unit_ids=[],
                override_allowed=False,
            ),
            *findings,
        ]

    rendered_findings = [_finding_payload(finding) for finding in findings]
    return {
        "source_units": _render_source_units(source_units),
        "requirement_candidates": _render_candidates(candidates, findings, mappings),
        "authority_mappings": _render_mappings(mappings),
        "review_findings": rendered_findings[: authority_ir.MAX_REVIEW_FINDINGS],
        "ir_provenance": str(ir_provenance),
        "coverage_summary": authority_ir.coverage_summary_from_findings(
            findings,
            mappings,
        ),
        "coverage_diagnostics": diagnostics,
        "ir_packet_limits": {
            "max_source_units": authority_ir.MAX_REVIEW_SOURCE_UNITS,
            "max_candidates": authority_ir.MAX_REVIEW_CANDIDATES,
            "max_findings": authority_ir.MAX_REVIEW_FINDINGS,
            "max_excerpt_bytes": authority_ir.MAX_REVIEW_EXCERPT_BYTES,
            "truncated": packet_truncated,
        },
    }


def _diagnostic_review_findings(
    diagnostics: Sequence[Mapping[str, Any]],
) -> list[authority_ir.AuthorityReviewFinding]:
    """Convert parser diagnostics into non-overrideable review blockers."""
    findings: list[authority_ir.AuthorityReviewFinding] = []
    for diagnostic in diagnostics:
        code = str(diagnostic.get("code") or "UNKNOWN_DIAGNOSTIC")
        section_id = str(diagnostic.get("section_id") or "")
        message = str(diagnostic.get("message") or "Source parser diagnostic.")
        findings.append(
            authority_ir.AuthorityReviewFinding(
                finding_id=f"AUTHORITY_REVIEW_SOURCE_DIAGNOSTIC:{code}:{section_id}",
                severity="blocking",
                code="AUTHORITY_REVIEW_SOURCE_DIAGNOSTIC",
                message=f"Source parser diagnostic {code}: {message}",
                candidate_ids=[],
                source_unit_ids=[section_id] if section_id else [],
                override_allowed=False,
            )
        )
    return findings


def _review_authority_items(
    success: SpecAuthorityCompilationSuccess | None,
    authority: CompiledSpecAuthority,
) -> list[JsonDict]:
    """Return authority targets with review-compatible target kinds."""
    if success is None:
        artifact = _fallback_authority_artifact(authority)
        return [
            *[
                {**item, "kind": "invariant"}
                for item in _mapping_items(artifact.get("invariants"))
            ],
            *[
                {**item, "kind": "eligible_feature_rule"}
                for item in _mapping_items(artifact.get("eligible_feature_rules"))
            ],
            *[
                {**item, "kind": "rejected_feature"}
                for item in _mapping_items(artifact.get("rejected_features"))
            ],
            *[{**item, "kind": "gap"} for item in _mapping_items(artifact.get("gaps"))],
            *[
                {**item, "kind": "assumption"}
                for item in _mapping_items(artifact.get("assumptions"))
            ],
        ]

    items: list[JsonDict] = [
        {
            "id": invariant.id,
            "kind": "invariant",
            "text": _invariant_text(invariant),
        }
        for invariant in success.invariants
    ]
    items.extend(
        {
            "id": f"ELIG-{index}",
            "kind": "eligible_feature_rule",
            "text": rule.rule,
        }
        for index, rule in enumerate(success.eligible_feature_rules, start=1)
    )
    items.extend(
        {"id": f"REJ-{index}", "kind": "rejected_feature", "text": feature}
        for index, feature in enumerate(success.rejected_features, start=1)
    )
    items.extend(
        {"id": f"GAP-{index}", "kind": "gap", "text": gap}
        for index, gap in enumerate(success.gaps, start=1)
    )
    items.extend(
        {"id": f"ASM-{index}", "kind": "assumption", "text": assumption}
        for index, assumption in enumerate(success.assumptions, start=1)
    )
    return items


def _mapping_items(value: object) -> list[JsonDict]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [
        {str(key): item_value for key, item_value in item.items()}
        for item in value
        if isinstance(item, Mapping)
    ]


def _review_mapping_entries(
    success: SpecAuthorityCompilationSuccess | None,
    candidates: list[authority_ir.RequirementCandidate],
) -> list[JsonDict]:
    """Return candidate-keyed mapping entries from compact IR or source map."""
    if success is None:
        return []
    if success.authority_mappings:
        return [
            cast("JsonDict", mapping.model_dump(mode="json"))
            for mapping in success.authority_mappings
        ]

    entries: list[JsonDict] = []
    candidates_by_quote = {
        _normalized_quote(candidate.source_quote): candidate for candidate in candidates
    }
    for source_map in success.source_map:
        candidate = candidates_by_quote.get(_normalized_quote(source_map.excerpt))
        if candidate is None:
            continue
        entries.append(
            {
                "candidate_id": candidate.candidate_id,
                "authority_item_id": source_map.invariant_id,
                "authority_target_kind": "invariant",
                "source_quote_hash": candidate.quote_hash,
                "mapping_provenance": "model_quote",
            }
        )
    return entries


def _normalized_quote(value: str) -> str:
    return " ".join(value.split())


def _packet_would_truncate(
    source_units: list[authority_ir.SourceUnit],
    candidates: list[authority_ir.RequirementCandidate],
    findings: list[authority_ir.AuthorityReviewFinding],
) -> bool:
    return (
        len(source_units) > authority_ir.MAX_REVIEW_SOURCE_UNITS
        or len(candidates) > authority_ir.MAX_REVIEW_CANDIDATES
        or len(findings) > authority_ir.MAX_REVIEW_FINDINGS
        or any(
            len(unit.text_excerpt.encode("utf-8"))
            > authority_ir.MAX_REVIEW_EXCERPT_BYTES
            for unit in source_units
        )
        or any(
            len(candidate.source_quote.encode("utf-8"))
            > authority_ir.MAX_REVIEW_EXCERPT_BYTES
            for candidate in candidates
        )
    )


def _render_source_units(source_units: list[authority_ir.SourceUnit]) -> list[JsonDict]:
    rendered: list[JsonDict] = []
    for unit in source_units[: authority_ir.MAX_REVIEW_SOURCE_UNITS]:
        payload = asdict(unit)
        payload["disposition"] = str(unit.disposition)
        payload["heading_path"] = list(unit.heading_path)
        payload["text_excerpt"] = _bounded_utf8(
            unit.text_excerpt,
            authority_ir.MAX_REVIEW_EXCERPT_BYTES,
        )
        rendered.append(payload)
    return rendered


def _render_candidates(
    candidates: list[authority_ir.RequirementCandidate],
    findings: list[authority_ir.AuthorityReviewFinding],
    mappings: list[authority_ir.AuthorityMapping],
) -> list[JsonDict]:
    sorted_candidates = sorted(
        candidates,
        key=lambda candidate: _candidate_sort_key(candidate, findings, mappings),
    )
    rendered: list[JsonDict] = []
    for candidate in sorted_candidates[: authority_ir.MAX_REVIEW_CANDIDATES]:
        payload = asdict(candidate)
        payload["provenance"] = str(candidate.provenance)
        payload["statement"] = _bounded_utf8(
            candidate.statement,
            authority_ir.MAX_REVIEW_EXCERPT_BYTES,
        )
        payload["source_quote"] = _bounded_utf8(
            candidate.source_quote,
            authority_ir.MAX_REVIEW_EXCERPT_BYTES,
        )
        rendered.append(payload)
    return rendered


def _candidate_sort_key(
    candidate: authority_ir.RequirementCandidate,
    findings: list[authority_ir.AuthorityReviewFinding],
    mappings: list[authority_ir.AuthorityMapping],
) -> tuple[int, int, str]:
    candidate_findings = [
        finding
        for finding in findings
        if candidate.candidate_id in finding.candidate_ids
    ]
    if any(finding.severity == "blocking" for finding in candidate_findings):
        return (0, candidate.line_start, candidate.candidate_id)
    candidate_mappings = [
        mapping
        for mapping in mappings
        if mapping.candidate_id == candidate.candidate_id
    ]
    if any(
        mapping.mapping_status == authority_ir.CoverageStatus.WEAK_MAPPING
        for mapping in candidate_mappings
    ):
        return (1, candidate.line_start, candidate.candidate_id)
    if any(
        finding.code == "AUTHORITY_CANDIDATE_UNCOVERED"
        for finding in candidate_findings
    ):
        return (2, candidate.line_start, candidate.candidate_id)
    if any(
        finding.code == "AUTHORITY_CANDIDATE_UNCERTAIN"
        for finding in candidate_findings
    ):
        return (3, candidate.line_start, candidate.candidate_id)
    return (4, candidate.line_start, candidate.candidate_id)


def _render_mappings(mappings: list[authority_ir.AuthorityMapping]) -> list[JsonDict]:
    rendered: list[JsonDict] = []
    for mapping in mappings:
        payload = asdict(mapping)
        payload["authority_target_kind"] = str(mapping.authority_target_kind)
        payload["mapping_status"] = str(mapping.mapping_status)
        payload["mapping_provenance"] = str(mapping.mapping_provenance)
        rendered.append(payload)
    return rendered


def _finding_payload(finding: authority_ir.AuthorityReviewFinding) -> JsonDict:
    return asdict(finding)


def _bounded_utf8(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore")


def _artifact_with_review_findings(
    artifact: JsonDict,
    *,
    review_findings: Sequence[Mapping[str, Any]],
) -> JsonDict:
    """Add host-derived blocking findings to rendered gaps."""
    blocking = [
        finding
        for finding in review_findings
        if finding.get("severity") == "blocking"
        and finding.get("code") != "AUTHORITY_COVERAGE_INCOMPLETE"
    ]
    if not blocking:
        return artifact
    gaps = list(cast("Sequence[Mapping[str, Any]]", artifact.get("gaps") or []))
    existing_texts = {str(gap.get("text", "")) for gap in gaps}
    appended: list[JsonDict] = []
    for index, finding in enumerate(blocking, start=1):
        code = str(finding.get("code") or "")
        if any(code in text for text in existing_texts):
            continue
        candidate_ids = [
            str(candidate_id) for candidate_id in _as_list(finding.get("candidate_ids"))
        ]
        suffix = (
            f" Affected candidates: {', '.join(candidate_ids)}."
            if candidate_ids
            else ""
        )
        appended.append(
            {
                "id": f"GAP-REVIEW-{index}",
                "text": f"{code}: {finding.get('message')}.{suffix}",
                "support": "inferred",
                "source_refs": candidate_ids,
                "source_excerpt": None,
            }
        )
    if not appended:
        return artifact
    return {**artifact, "gaps": [*gaps, *appended]}


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
        "source_units": snapshot.source_units,
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
            "ir_provenance": snapshot.ir_provenance,
            "requirement_candidates": snapshot.requirement_candidates,
            "authority_mappings": snapshot.authority_mappings,
            "review_findings": snapshot.review_findings,
            "coverage_summary": snapshot.ir_coverage_summary,
            "ir_packet_limits": snapshot.ir_packet_limits,
        },
        "review_findings": snapshot.review_findings,
        "review_guidance": _review_guidance(),
        "next_actions": [
            {
                "command": (
                    "agileforge authority accept --project-id "
                    f"{snapshot.project_id} --review-token {snapshot.review_token} "
                    "--idempotency-key <idempotency_key>"
                ),
                "mode": "human",
                "installed": True,
                "requires_cli_installation": False,
                "requires": ["review_token", "idempotency_key"],
                "reason": "Record the reviewed pending authority as canonical.",
            },
            {
                "command": (
                    "agileforge authority reject --project-id "
                    f"{snapshot.project_id} --review-token "
                    f'{snapshot.review_token} --reason "..." '
                    "--idempotency-key <idempotency_key>"
                ),
                "mode": "human",
                "installed": True,
                "requires_cli_installation": False,
                "requires": ["review_token", "reason", "idempotency_key"],
                "reason": "Record that the pending authority must not be used.",
            },
        ],
        "guard_tokens": snapshot.guard_tokens,
    }


def _render_review_text(packet: JsonDict) -> str:
    """Return a compact human-readable review summary."""
    project = _mapping_or_none(packet.get("project"))
    spec = _mapping_or_none(packet.get("spec"))
    pending = _mapping_or_none(packet.get("pending_authority"))
    guards = _mapping_or_none(packet.get("guard_tokens"))
    next_actions = packet.get("next_actions")
    actions = next_actions if isinstance(next_actions, list) else []
    lines = [
        "Authority review",
        f"Project: {_mapping_value(project, 'project_id')}",
        f"Project name: {_mapping_value(project, 'name')}",
        f"FSM state: {_mapping_value(project, 'fsm_state')}",
        f"Setup status: {_mapping_value(project, 'setup_status')}",
        f"Pending authority: {_mapping_value(pending, 'authority_id')}",
        (
            "Authority fingerprint: "
            f"{_mapping_value(pending, 'authority_fingerprint')}"
        ),
        f"Spec path: {_mapping_value(spec, 'resolved_path')}",
        f"Spec hash: {_mapping_value(spec, 'spec_hash')}",
        (
            "Omission assessment: "
            f"{_mapping_value(guards, 'expected_omission_assessment')}"
        ),
        f"Review token: {_mapping_value(guards, 'review_token')}",
        f"ACCEPT: {_action_command(actions, index=0)}",
        f"REJECT: {_action_command(actions, index=1)}",
    ]
    return "\n".join(lines)


def _action_command(actions: list[object], *, index: int) -> str:
    if index >= len(actions):
        return ""
    action = _mapping_or_none(actions[index])
    if action is None:
        return ""
    return str(action.get("command", ""))


def _mapping_value(mapping: Mapping[object, object] | None, key: str) -> object:
    if mapping is None:
        return ""
    return mapping.get(key, "")


def _mapping_or_none(value: object) -> Mapping[object, object] | None:
    if isinstance(value, Mapping):
        return cast("Mapping[object, object]", value)
    return None


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


def _load_compiled_artifact(
    authority: CompiledSpecAuthority,
) -> SpecAuthorityCompilationSuccess | None:
    """Load normalized compiled artifact JSON if present and valid."""
    artifact_json = getattr(authority, "compiled_artifact_json", None)
    if not artifact_json:
        return None
    try:
        parsed = SpecAuthorityCompilerOutput.model_validate_json(artifact_json)
    except (ValidationError, ValueError):
        return None
    if isinstance(parsed.root, SpecAuthorityCompilationFailure):
        return None
    return parsed.root


def _authority_artifact_payload(
    authority: CompiledSpecAuthority,
) -> tuple[JsonDict, list[_AuthorityEvidence], list[_ClassificationEvidence]]:
    artifact = _load_compiled_artifact(authority)
    if artifact is None:
        fallback = _fallback_authority_artifact(authority)
        return (
            fallback,
            _fallback_authority_evidence(fallback),
            _fallback_classification_evidence(fallback),
        )

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


def _fallback_authority_evidence(
    artifact: Mapping[str, Any],
) -> list[_AuthorityEvidence]:
    """Return coverage evidence from persisted fallback authority items."""
    evidence: list[_AuthorityEvidence] = []
    for key in ("invariants", "eligible_feature_rules"):
        items = artifact.get(key)
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            item_id = item.get("id")
            if item_id is None:
                continue
            evidence.append(
                _AuthorityEvidence(
                    item_id=str(item_id),
                    source_refs=tuple(
                        _dedupe_sorted(_as_list(item.get("source_refs")))
                    ),
                    source_excerpt=(
                        str(item["source_excerpt"])
                        if item.get("source_excerpt") is not None
                        else None
                    ),
                )
            )
    return evidence


def _fallback_classification_evidence(
    artifact: Mapping[str, Any],
) -> list[_ClassificationEvidence]:
    """Return coverage classification evidence from persisted fallback items."""
    evidence: list[_ClassificationEvidence] = []
    for key, kind in (
        ("gaps", "gap"),
        ("assumptions", "assumption"),
        ("rejected_features", "rejected_feature"),
    ):
        items = artifact.get(key)
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            item_id = item.get("id")
            text = item.get("text")
            if item_id is None or text is None:
                continue
            evidence.append(
                _ClassificationEvidence(
                    item_id=str(item_id),
                    text=str(text),
                    kind=kind,
                )
            )
    return evidence


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


def _artifact_with_coverage_gaps(
    artifact: JsonDict,
    *,
    outline: Sequence[Mapping[str, Any]],
    coverage_summary: Mapping[str, Any],
    diagnostics: Sequence[Mapping[str, Any]],
) -> JsonDict:
    """Add actionable review gaps when coverage proves the packet incomplete."""
    if coverage_summary.get("omission_assessment") == "complete":
        return artifact

    gaps = list(cast("Sequence[Mapping[str, Any]]", artifact.get("gaps") or []))
    existing_texts = {str(gap.get("text", "")) for gap in gaps}
    if any("AUTHORITY_COVERAGE_INCOMPLETE" in text for text in existing_texts):
        return artifact

    incomplete_sections = [
        entry
        for entry in outline
        if entry.get("coverage_status") in {"partial", "uncovered"}
    ]
    source_refs = _coverage_gap_source_refs(incomplete_sections)
    summary_parts = [
        f"uncovered_sections={coverage_summary.get('uncovered_sections', 0)}",
        f"partial_sections={coverage_summary.get('partial_sections', 0)}",
        "unclassified_content_blocks="
        f"{coverage_summary.get('unclassified_content_blocks', 0)}",
    ]
    if diagnostics:
        codes = _dedupe_sorted(diagnostic.get("code") for diagnostic in diagnostics)
        summary_parts.append(f"diagnostics={','.join(codes)}")
    section_summary = (
        f" Affected sections: {', '.join(source_refs)}." if source_refs else ""
    )
    gap = {
        "id": "GAP-COVERAGE-INCOMPLETE",
        "text": (
            "AUTHORITY_COVERAGE_INCOMPLETE: Review coverage is incomplete; "
            f"{'; '.join(summary_parts)}.{section_summary}"
        ),
        "support": "inferred",
        "source_refs": source_refs,
        "source_excerpt": None,
    }
    return {**artifact, "gaps": [*gaps, gap]}


def _coverage_gap_source_refs(
    incomplete_sections: Sequence[Mapping[str, Any]],
) -> list[str]:
    refs: list[str] = []
    for entry in incomplete_sections[:10]:
        heading = entry.get("heading")
        section_id = entry.get("section_id")
        if isinstance(heading, str) and heading.strip():
            refs.append(heading.strip())
        elif isinstance(section_id, str) and section_id.strip():
            refs.append(section_id.strip())
    return _dedupe_sorted(refs)


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
            "Accept only if this compiled interpretation correctly represents "
            "the spec. Reject if invariants are invented, duplicated, "
            "incorrectly sourced, or omit mandatory requirements."
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
