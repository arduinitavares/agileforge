"""Facts-only authority review snapshot for guarded workflow decisions."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping, Sequence, Set
from dataclasses import asdict, dataclass
from json import JSONDecodeError
from typing import TYPE_CHECKING, Any, Final, cast

from pydantic import ValidationError
from sqlmodel import Session, col, select

from models.agent_workbench import (
    DiscoveryChallengeArtifact,
    DiscoveryPrd,
    DiscoverySpecAmendmentDraft,
)
from models.core import Project
from models.specs import SpecRegistry
from services.agent_workbench.authority_projection import (
    _iso_z,
    _load_authority_selection,
    _project_not_found_error,
    pending_authority_fingerprint,
)
from services.agent_workbench.envelope import error_envelope
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.specs.compiler_service import (
    compiled_authority_read_failure,
    compiled_authority_schema_unsupported_details,
    compiled_authority_schema_unsupported_remediation,
)
from services.specs.compiler_service import (
    load_compiled_artifact as load_stored_compiled_artifact,
)
from services.specs.profile_content import (
    SpecContentNormalizationError,
    normalize_spec_content_for_registry,
)
from utils import spec_authority_ir as authority_ir
from utils.agileforge_spec_profile import (
    TechnicalSpecArtifact,
    canonical_spec_hash,
    render_markdown,
    rendered_markdown_hash,
)
from utils.spec_authority_assumptions import (
    AUTHORITY_ASSUMPTION_ADAPTER,
    AcceptedNormativeCountAssumptionClaim,
    AcceptedNormativeSetAssumptionClaim,
    AuthorityAssumption,
    GroundingFailure,
    ItemStatusAssumptionClaim,
    StructuredAuthorityAssumption,
    canonical_assumption_key,
    ground_assumption,
    is_structured_assumption,
    render_assumption_text,
)
from utils.spec_authority_ir import (
    ContentBlock as _ContentBlock,
)
from utils.spec_authority_ir import (
    Section as _Section,
)
from utils.spec_authority_ir import (
    parse_markdown_sections as _parse_markdown_sections,
)

if TYPE_CHECKING:
    from pathlib import Path

    from models.specs import CompiledSpecAuthority
    from utils.spec_schemas import (
        Invariant,
        SpecAuthorityCompilationSuccess,
        SpecAuthorityMapping,
        SpecAuthorityRequirementCandidate,
        SpecAuthoritySourceUnit,
    )

JsonDict = dict[str, Any]

AUTHORITY_REVIEW_COMMAND: Final[str] = "agileforge authority review"
_REVIEW_SCHEMA: Final[str] = "agileforge.authority_review.v1"
COVERAGE_SCHEMA: Final[str] = "agileforge.authority_coverage_summary.v1"
DEFAULT_REVIEW_SOURCE_LIMIT_BYTES: Final[int] = 262_144
STRUCTURED_SPEC_ITEM_PREFIXES: Final[tuple[str, ...]] = (
    "GOAL.",
    "NON_GOAL.",
    "REQ.",
    "QUALITY.",
    "CONSTRAINT.",
    "INTERFACE.",
    "DATA.",
    "DECISION.",
    "ASSUMPTION.",
    "RISK.",
    "EXAMPLE.",
    "OPEN_QUESTION.",
)


@dataclass(frozen=True)
class _SourceLoad:
    """Canonical content loaded from the authoritative spec registry."""

    raw_bytes: bytes
    text: str


@dataclass(frozen=True)
class _ReviewInputs:
    """Exact durable records needed to derive one review snapshot."""

    session: Session
    project_id: int
    project: Project
    spec: SpecRegistry
    authority: CompiledSpecAuthority
    include_spec: str


@dataclass(frozen=True)
class AuthorityReviewSnapshot:
    """Canonical facts-only authority review snapshot."""

    schema: str
    project_id: int
    pending_authority_id: int | None
    authority_fingerprint: str | None
    source_spec_hash: str
    compiler_version: str
    prompt_hash: str
    content_included: bool
    omission_assessment: str
    coverage_summary_fingerprint: str
    project_name: str
    spec_version_id: int | None
    size_bytes: int
    review_source_limit_bytes: int
    source_outline: list[JsonDict]
    coverage_summary: JsonDict
    coverage_diagnostics: list[JsonDict]
    source_units: list[JsonDict]
    authority_mappings: list[JsonDict]
    review_findings: list[JsonDict]
    ir_provenance: str
    ir_packet_limits: JsonDict
    ir_coverage_summary: JsonDict
    excerpt: str
    content_truncated: bool
    source_content: str | None
    source_content_sha256: str | None
    structured_spec_snapshot: JsonDict | None
    pending_spec_version_id: int
    compiled_at: str | None
    scope_discovery: JsonDict | None
    artifact: JsonDict

    @property
    def payload(self) -> JsonDict:
        """Return the canonical payload used for review-token hashing."""
        return {
            "schema": self.schema,
            "project": {
                "project_id": self.project_id,
                "project_name": self.project_name,
            },
            "specification": {
                "spec_version_id": self.spec_version_id,
                "pending_spec_version_id": self.pending_spec_version_id,
                "source_spec_hash": self.source_spec_hash,
                "size_bytes": self.size_bytes,
                "content_included": self.content_included,
                "content_truncated": self.content_truncated,
                "source_content": self.source_content,
                "source_content_sha256": self.source_content_sha256,
                "structured_spec_snapshot": self.structured_spec_snapshot,
                "excerpt": self.excerpt,
            },
            "pending_authority": {
                "pending_authority_id": self.pending_authority_id,
                "authority_fingerprint": self.authority_fingerprint,
                "compiler_version": self.compiler_version,
                "prompt_hash": self.prompt_hash,
                "compiled_at": self.compiled_at,
                "artifact": self.artifact,
                "authority_mappings": self.authority_mappings,
            },
            "review": {
                "review_source_limit_bytes": self.review_source_limit_bytes,
                "omission_assessment": self.omission_assessment,
                "coverage_summary_fingerprint": self.coverage_summary_fingerprint,
                "source_outline": self.source_outline,
                "coverage_summary": self.coverage_summary,
                "coverage_diagnostics": self.coverage_diagnostics,
                "source_units": self.source_units,
                "review_findings": self.review_findings,
                "ir_provenance": self.ir_provenance,
                "ir_packet_limits": self.ir_packet_limits,
                "ir_coverage_summary": self.ir_coverage_summary,
            },
            "scope_discovery_fingerprint": (
                self.scope_discovery.get("scope_discovery_fingerprint")
                if self.scope_discovery is not None
                else None
            ),
            "scope_discovery": self.scope_discovery,
        }

    @property
    def review_fingerprint(self) -> str:
        """Return the complete deterministic review fingerprint."""
        return authority_review_fingerprint(self)

    @property
    def fingerprint_payload(self) -> JsonDict:
        """Return authoritative review inputs from the registry-backed packet."""
        return self.payload

    @property
    def review_token(self) -> str:
        """Return the schema-qualified complete review fingerprint."""
        return f"{_REVIEW_SCHEMA}:{self.review_fingerprint}"

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


def authority_review_fingerprint(snapshot: AuthorityReviewSnapshot) -> str:
    """Hash authoritative review inputs while excluding provenance metadata."""
    return canonical_json_hash(snapshot.fingerprint_payload)


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


def _unsupported_compiled_authority_error(
    *,
    project_id: int,
    spec_version_id: int | None,
    observed_schema_version: str | None,
) -> JsonDict:
    """Return the fail-closed review error for unsupported artifacts."""
    return error_envelope(
        command=AUTHORITY_REVIEW_COMMAND,
        error=workbench_error(
            ErrorCode.COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED,
            details=compiled_authority_schema_unsupported_details(
                project_id=project_id,
                spec_version_id=spec_version_id,
                observed_schema_version=observed_schema_version,
            ),
            remediation=compiled_authority_schema_unsupported_remediation(
                project_id=project_id,
                spec_version_id=spec_version_id,
            ),
        ),
    )


def _authority_source_changed_error(
    *,
    registry_hash: str,
    content_hash: str,
) -> JsonDict:
    return error_envelope(
        command=AUTHORITY_REVIEW_COMMAND,
        error=workbench_error(
            ErrorCode.AUTHORITY_SOURCE_CHANGED,
            message=(
                "Stored canonical specification content does not match its "
                "registry spec hash."
            ),
            details={
                "registry_spec_hash": registry_hash,
                "content_spec_hash": content_hash,
            },
            remediation=["Re-register or recompile the specification before review."],
        ),
    )


def _spec_file_invalid_error(reason: str) -> JsonDict:
    return error_envelope(
        command=AUTHORITY_REVIEW_COMMAND,
        error=workbench_error(
            ErrorCode.SPEC_FILE_INVALID,
            message="Stored canonical specification content is invalid.",
            details={
                "reason": reason,
            },
            remediation=[
                "Re-register valid canonical specification content and retry review."
            ],
        ),
    )


def _load_source_from_registry(spec: SpecRegistry) -> _SourceLoad | JsonDict:
    """Normalize the exact approved specification stored in the registry."""
    try:
        normalized = normalize_spec_content_for_registry(spec.content)
    except SpecContentNormalizationError as exc:
        return _spec_file_invalid_error(str(exc))
    content_hash = _normalize_sha256_hash(normalized.spec_hash)
    registry_hash = _normalize_sha256_hash(spec.spec_hash)
    if content_hash != registry_hash:
        return _authority_source_changed_error(
            registry_hash=registry_hash,
            content_hash=content_hash,
        )
    source_bytes = normalized.content.encode("utf-8")
    return _SourceLoad(
        raw_bytes=source_bytes,
        text=normalized.content,
    )


def _normalize_sha256_hash(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith("sha256:"):
        return f"sha256:{stripped.removeprefix('sha256:').lower()}"
    return f"sha256:{stripped.lower()}"


def build_authority_review_snapshot_in_session(
    session: Session,
    *,
    project_id: int,
    include_spec: str = "auto",
    repo_root: Path | None = None,
) -> AuthorityReviewSnapshot | JsonDict:
    """Build a review snapshot using only the caller-owned session."""
    del repo_root
    project = session.get(Project, project_id)
    if project is None:
        return _project_not_found_error(AUTHORITY_REVIEW_COMMAND, project_id)
    approved_specs = tuple(
        session.exec(
            select(SpecRegistry)
            .where(
                SpecRegistry.project_id == project_id,
                SpecRegistry.status == "approved",
            )
            .order_by(col(SpecRegistry.spec_version_id).desc())
        ).all()
    )
    if len(approved_specs) != 1:
        return _authority_not_pending_error(project_id)
    spec = approved_specs[0]
    selection = _load_authority_selection(session, project_id=project_id)
    authority = selection.pending_authority
    if authority is None or authority.spec_version_id != spec.spec_version_id:
        return _authority_not_pending_error(project_id)
    load_result = load_stored_compiled_artifact(authority)
    if load_result.unsupported:
        return _unsupported_compiled_authority_error(
            project_id=project_id,
            spec_version_id=authority.spec_version_id,
            observed_schema_version=load_result.observed_schema_version,
        )
    return _build_authority_review_snapshot(
        _ReviewInputs(
            session=session,
            project_id=project_id,
            project=project,
            spec=spec,
            authority=authority,
            include_spec=include_spec,
        )
    )


def _build_authority_review_snapshot(
    inputs: _ReviewInputs,
) -> AuthorityReviewSnapshot | JsonDict:
    """Build the canonical review snapshot without routing recommendations."""
    session = inputs.session
    project_id = inputs.project_id
    project = inputs.project
    spec = inputs.spec
    authority = inputs.authority
    include_spec = inputs.include_spec
    source = _load_source_from_registry(spec)
    if not isinstance(source, _SourceLoad):
        return cast("JsonDict", source)
    load_result = load_stored_compiled_artifact(authority)
    if load_result.unsupported:
        return _unsupported_compiled_authority_error(
            project_id=project_id,
            spec_version_id=authority.spec_version_id,
            observed_schema_version=load_result.observed_schema_version,
        )

    source_limit = _review_source_limit()
    content_included = include_spec == "full" or (
        include_spec == "auto" and len(source.raw_bytes) <= source_limit
    )
    content_truncated = not content_included and len(source.raw_bytes) > source_limit
    compiled_artifact = _load_compiled_artifact(authority)
    artifact, authority_evidence, classification_evidence = _authority_artifact_payload(
        authority
    )
    artifact_shape_findings = _compiled_artifact_shape_findings(
        authority,
        project_id=project_id,
    )
    structured_artifact = _structured_artifact_from_text(source.text)
    if structured_artifact is not None:
        outline: list[JsonDict] = []
        coverage_summary: JsonDict = {
            "covered_sections": 0,
            "partial_sections": 0,
            "uncovered_sections": 0,
            "intentionally_classified_sections": 0,
            "unclassified_content_blocks": 0,
            "omission_assessment": "complete",
        }
        diagnostics: list[JsonDict] = []
    else:
        outline, coverage_summary, diagnostics = _coverage_payload(
            text=source.text,
            authority_evidence=authority_evidence,
            classification_evidence=classification_evidence,
        )
    ir_payload = _authority_ir_payload(
        diagnostics=diagnostics,
        artifact=artifact,
        compiled_artifact=compiled_artifact,
        structured_artifact=structured_artifact,
        artifact_shape_findings=artifact_shape_findings,
    )
    if structured_artifact is None:
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
    scope_discovery = _scope_discovery_provenance(
        session=session,
        project_id=project_id,
        amended_spec_hash=source_spec_hash,
    )
    omission_assessment = coverage_summary["omission_assessment"]

    return AuthorityReviewSnapshot(
        schema=_REVIEW_SCHEMA,
        project_id=project_id,
        pending_authority_id=pending_authority_id,
        authority_fingerprint=authority_fingerprint,
        source_spec_hash=source_spec_hash,
        compiler_version=authority.compiler_version,
        prompt_hash=authority.prompt_hash,
        content_included=content_included,
        omission_assessment=omission_assessment,
        coverage_summary_fingerprint=coverage_fingerprint,
        project_name=project.name,
        spec_version_id=spec.spec_version_id,
        size_bytes=len(source.raw_bytes),
        review_source_limit_bytes=source_limit,
        source_outline=outline,
        coverage_summary=coverage_summary,
        coverage_diagnostics=diagnostics,
        source_units=cast("list[JsonDict]", ir_payload["source_units"]),
        authority_mappings=cast("list[JsonDict]", ir_payload["authority_mappings"]),
        review_findings=cast("list[JsonDict]", ir_payload["review_findings"]),
        ir_provenance=str(ir_payload["ir_provenance"]),
        ir_packet_limits=cast("JsonDict", ir_payload["ir_packet_limits"]),
        ir_coverage_summary=cast("JsonDict", ir_payload["coverage_summary"]),
        excerpt=_bounded_excerpt(source.text),
        content_truncated=content_truncated,
        source_content=source.text if content_included else None,
        source_content_sha256=source_content_sha256,
        structured_spec_snapshot=_structured_spec_snapshot(source.text),
        pending_spec_version_id=authority.spec_version_id,
        compiled_at=_iso_z(authority.compiled_at),
        scope_discovery=scope_discovery,
        artifact=artifact,
    )


def _scope_discovery_provenance(
    *,
    session: Session,
    project_id: int,
    amended_spec_hash: str,
) -> JsonDict | None:
    """Return Scope Discovery provenance for a pending discovered authority."""
    draft = session.exec(
        select(DiscoverySpecAmendmentDraft)
        .where(
            DiscoverySpecAmendmentDraft.project_id == project_id,
            DiscoverySpecAmendmentDraft.amended_spec_hash == amended_spec_hash,
        )
        .order_by(col(DiscoverySpecAmendmentDraft.spec_amendment_draft_id).desc())
    ).first()
    if draft is None:
        return None
    prd = session.get(DiscoveryPrd, draft.prd_id)
    challenge = session.get(
        DiscoveryChallengeArtifact,
        draft.challenge_artifact_id,
    )
    if prd is None or challenge is None:
        return None
    challenge_payload = _json_object(challenge.content_json)
    validation_payload = _json_object(draft.validation_json)
    provenance: JsonDict = {
        "challenge_artifact": {
            "challenge_artifact_id": challenge.challenge_artifact_id,
            "producer": challenge.producer,
            "readiness": challenge.readiness,
            "original_idea": challenge.original_idea,
            "artifact_fingerprint": challenge.artifact_fingerprint,
            "assumptions": _list_value(challenge_payload, "assumptions"),
            "non_goals": _list_value(challenge_payload, "non_goals"),
            "risks": _list_value(challenge_payload, "risks"),
            "evidence_conflicts": _list_value(
                challenge_payload,
                "evidence_conflicts",
            ),
            "open_questions": _list_value(challenge_payload, "open_questions"),
            "glossary_changes": _list_value(challenge_payload, "glossary_changes"),
        },
        "prd": {
            "prd_id": prd.prd_id,
            "producer": prd.producer,
            "status": prd.status,
            "version": prd.version,
            "title": prd.title,
            "artifact_fingerprint": prd.artifact_fingerprint,
            "reviewed_by": prd.reviewed_by,
        },
        "spec_amendment": {
            "spec_amendment_draft_id": draft.spec_amendment_draft_id,
            "status": draft.status,
            "artifact_fingerprint": draft.artifact_fingerprint,
            "base_spec_version_id": draft.base_spec_version_id,
            "base_spec_hash": draft.base_spec_hash,
            "amended_spec_hash": draft.amended_spec_hash,
            "validation": validation_payload,
        },
        "readiness": {
            "challenge_readiness": challenge.readiness,
            "prd_status": prd.status,
            "spec_amendment_status": draft.status,
            "open_questions_status": (
                "open" if _list_value(challenge_payload, "open_questions") else "closed"
            ),
            "evidence_conflict_count": len(
                _list_value(challenge_payload, "evidence_conflicts")
            ),
        },
    }
    provenance["scope_discovery_fingerprint"] = canonical_json_hash(provenance)
    return provenance


def _json_object(raw_json: str | None) -> JsonDict:
    """Decode JSON object text, returning an empty object on malformed content."""
    if not raw_json:
        return {}
    try:
        value = json.loads(raw_json)
    except JSONDecodeError:
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _list_value(payload: Mapping[str, Any], field: str) -> list[Any]:
    """Return a list-valued discovery payload field."""
    value = payload.get(field)
    return list(value) if isinstance(value, list) else []


def _authority_ir_payload(
    *,
    diagnostics: Sequence[Mapping[str, Any]],
    artifact: Mapping[str, Any],
    compiled_artifact: SpecAuthorityCompilationSuccess | None,
    structured_artifact: TechnicalSpecArtifact | None,
    artifact_shape_findings: Sequence[Mapping[str, Any]],
) -> JsonDict:
    """Build public review metadata without host semantic candidate coverage."""
    diagnostic_findings = _diagnostic_review_findings(diagnostics)
    source_ref_findings = _structured_source_ref_findings(
        artifact=artifact,
        spec_artifact=structured_artifact,
    )
    assumption_findings = _compiled_assumption_findings(
        artifact=compiled_artifact,
        spec_artifact=structured_artifact,
    )
    rendered_findings = [
        *[_finding_payload(finding) for finding in diagnostic_findings],
        *[dict(finding) for finding in artifact_shape_findings],
        *source_ref_findings,
        *assumption_findings,
    ]
    return {
        "source_units": [],
        "authority_mappings": [],
        "review_findings": rendered_findings,
        "ir_provenance": "not_applicable",
        "coverage_summary": {
            "blocking_finding_count": sum(
                1
                for finding in rendered_findings
                if finding.get("severity") == "blocking"
            ),
            "mapping_count": 0,
            "covered_mapping_count": 0,
            "weak_mapping_count": 0,
            "intentionally_classified_mapping_count": 0,
            "partial_mapping_count": 0,
            "has_incomplete_coverage": False,
        },
        "coverage_diagnostics": diagnostics,
        "ir_packet_limits": {
            "max_findings": authority_ir.MAX_REVIEW_FINDINGS,
            "truncated": False,
        },
    }


def _ir_source_unit(unit: SpecAuthoritySourceUnit) -> authority_ir.SourceUnit:
    """Convert compact schema source unit to shared IR dataclass."""
    return authority_ir.SourceUnit(
        unit_id=unit.unit_id,
        section_id=unit.section_id,
        heading_path=tuple(unit.heading_path),
        kind=unit.kind,
        line_start=unit.line_start,
        line_end=unit.line_end,
        text_hash=unit.text_hash,
        text_excerpt=unit.text_excerpt,
        requirement_bearing=True,
        disposition=unit.disposition,
        disposition_reason=unit.disposition_reason,
    )


def _ir_requirement_candidate(
    candidate: SpecAuthorityRequirementCandidate,
) -> authority_ir.RequirementCandidate:
    """Convert compact schema candidate to shared IR dataclass."""
    return authority_ir.RequirementCandidate(
        candidate_id=candidate.candidate_id,
        source_unit_id=candidate.source_unit_id,
        statement=candidate.statement,
        source_quote=candidate.source_quote,
        quote_hash=candidate.quote_hash,
        line_start=candidate.line_start,
        line_end=candidate.line_end,
        classification=candidate.classification,
        provenance=candidate.provenance,
    )


def _ir_authority_mapping(
    mapping: SpecAuthorityMapping,
) -> authority_ir.AuthorityMapping:
    """Convert compact schema mapping to shared IR dataclass."""
    return authority_ir.AuthorityMapping(
        candidate_id=mapping.candidate_id,
        authority_item_id=mapping.authority_item_id,
        authority_target_kind=mapping.authority_target_kind,
        mapping_status=mapping.mapping_status,
        mapping_rationale=mapping.mapping_rationale,
        source_quote_hash=mapping.source_quote_hash,
        mapping_provenance=mapping.mapping_provenance,
    )


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


def _finding_payload(finding: authority_ir.AuthorityReviewFinding) -> JsonDict:
    return asdict(finding)


def _structured_artifact_from_text(text: str) -> TechnicalSpecArtifact | None:
    try:
        return TechnicalSpecArtifact.model_validate_json(text)
    except (ValueError, ValidationError):
        return None


def _structured_spec_snapshot(spec_content: str) -> JsonDict | None:
    """Return metadata for canonical AgileForge spec JSON, if present."""
    artifact = _structured_artifact_from_text(spec_content)
    if artifact is None:
        return None

    rendered_markdown = render_markdown(artifact)
    return {
        "format": artifact.schema_version,
        "artifact_id": artifact.artifact_id,
        "canonical_spec_sha256": canonical_spec_hash(artifact),
        "render_profile": artifact.rendering.markdown_profile,
        "rendered_markdown_sha256": rendered_markdown_hash(rendered_markdown),
        "item_count": len(artifact.items),
        "relation_count": len(artifact.relations),
    }


def _source_ref_item_id(
    location: object,
    *,
    known_item_ids: Set[str] | None = None,
) -> str | None:
    if not isinstance(location, str) or not location.strip():
        return None
    value = location.strip()
    if known_item_ids is not None:
        if value in known_item_ids:
            return value
        candidate = value
        while "." in candidate:
            candidate = candidate.rsplit(".", maxsplit=1)[0]
            if candidate in known_item_ids:
                return candidate
    if not value.startswith(STRUCTURED_SPEC_ITEM_PREFIXES):
        return None

    candidate = value.rsplit(".", maxsplit=1)[0]
    return candidate if "." in candidate else value


def _source_map_entries(source_map: object) -> list[Mapping[str, Any]] | None:
    if isinstance(source_map, Sequence) and not isinstance(
        source_map,
        (str, bytes, bytearray),
    ):
        return [
            cast("Mapping[str, Any]", entry)
            for entry in source_map
            if isinstance(entry, Mapping)
        ]
    if isinstance(source_map, Mapping):
        entries: list[Mapping[str, Any]] = []
        for value in source_map.values():
            if isinstance(value, Mapping):
                entries.append(cast("Mapping[str, Any]", value))
            elif isinstance(value, Sequence) and not isinstance(
                value,
                (str, bytes, bytearray),
            ):
                entries.extend(
                    cast("Mapping[str, Any]", entry)
                    for entry in value
                    if isinstance(entry, Mapping)
                )
        return entries
    return None


def _structured_source_ref_findings(
    *,
    artifact: Mapping[str, Any],
    spec_artifact: TechnicalSpecArtifact | None,
) -> list[JsonDict]:
    if spec_artifact is None:
        return []
    source_map = artifact.get("source_map")
    source_entries = _source_map_entries(source_map)
    if source_entries is None:
        return [
            {
                "finding_id": "SOURCE_REFS_MISSING",
                "severity": "warning",
                "code": "SOURCE_REFS_MISSING",
                "message": "Compiled authority has no source_map review evidence.",
                "candidate_ids": [],
                "source_unit_ids": [],
                "override_allowed": True,
            }
        ]
    item_ids = {item.id for item in spec_artifact.items}
    invalid_locations: list[str] = []
    usable_locations = 0
    for entry in source_entries:
        item_id = _source_ref_item_id(
            entry.get("location"),
            known_item_ids=item_ids,
        )
        if item_id is None:
            continue
        usable_locations += 1
        if item_id not in item_ids:
            invalid_locations.append(str(entry.get("location")))
    if invalid_locations:
        return [
            {
                "finding_id": "SOURCE_REF_INVALID",
                "severity": "blocking",
                "code": "SOURCE_REF_INVALID",
                "message": (
                    "Compiled authority source_map references unknown spec item IDs."
                ),
                "candidate_ids": [],
                "source_unit_ids": [],
                "override_allowed": False,
                "details": {"invalid_locations": sorted(set(invalid_locations))},
            }
        ]
    if usable_locations == 0:
        return [
            {
                "finding_id": "SOURCE_REFS_MISSING",
                "severity": "warning",
                "code": "SOURCE_REFS_MISSING",
                "message": (
                    "Compiled authority source_map has no structured spec item "
                    "references."
                ),
                "candidate_ids": [],
                "source_unit_ids": [],
                "override_allowed": True,
            }
        ]
    return []


def _compiled_assumption_findings(
    *,
    artifact: SpecAuthorityCompilationSuccess | None,
    spec_artifact: TechnicalSpecArtifact | None,
) -> list[JsonDict]:
    """Return non-overrideable findings for ungrounded structured claims."""
    if artifact is None:
        return []
    findings: list[JsonDict] = []
    for index, assumption in enumerate(artifact.assumptions, start=1):
        if not is_structured_assumption(assumption):
            continue
        if spec_artifact is None:
            findings.append(
                _compiled_claim_source_unavailable_finding(
                    assumption_index=index,
                    assumption=assumption,
                )
            )
            continue
        grounded = ground_assumption(assumption, spec_artifact)
        if isinstance(grounded, GroundingFailure):
            findings.append(
                _compiled_claim_mismatch_finding(
                    assumption_index=index,
                    assumption=assumption,
                    failure=grounded,
                )
            )
    return findings


def _compiled_claim_source_unavailable_finding(
    *,
    assumption_index: int,
    assumption: StructuredAuthorityAssumption,
) -> JsonDict:
    provenance = assumption.provenance
    return _compiled_claim_finding(
        code="COMPILER_ASSUMPTION_CLAIM_SOURCE_UNAVAILABLE",
        assumption_index=assumption_index,
        assumption=assumption,
        details={
            "claimed_value": _assumption_claimed_value(assumption),
            "actual_value": None,
            "artifact_id": provenance.artifact_id,
            "claimed_source_item_ids": list(provenance.source_item_ids),
            "actual_source_item_ids": [],
        },
        message=(
            "Compiler assumption requires a parsed structured specification "
            "source that is unavailable during review."
        ),
    )


def _compiled_claim_mismatch_finding(
    *,
    assumption_index: int,
    assumption: AuthorityAssumption,
    failure: GroundingFailure,
) -> JsonDict:
    return _compiled_claim_finding(
        code="COMPILER_ASSUMPTION_CLAIM_MISMATCH",
        assumption_index=assumption_index,
        assumption=assumption,
        details={
            "claimed_value": failure.claimed_value,
            "actual_value": failure.actual_value,
            "artifact_id": failure.artifact_id,
            "claimed_source_item_ids": list(failure.claimed_source_item_ids),
            "actual_source_item_ids": list(failure.actual_source_item_ids),
            "grounding_reason": failure.reason,
        },
        message=(
            "Compiler assumption does not match the reviewed structured specification."
        ),
    )


def _compiled_claim_finding(
    *,
    code: str,
    assumption_index: int,
    assumption: AuthorityAssumption,
    details: Mapping[str, object],
    message: str,
) -> JsonDict:
    """Build a stable non-overrideable typed-claim finding."""
    payload = {
        "assumption_key": canonical_assumption_key(assumption),
        "code": code,
        "claimed_source_item_ids": details["claimed_source_item_ids"],
    }
    finding_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "finding_id": f"ARF-{finding_hash}",
        "severity": "blocking",
        "code": code,
        "message": message,
        "candidate_ids": [],
        "source_unit_ids": [],
        "override_allowed": False,
        "details": {
            "assumption_index": assumption_index,
            "claim_kind": assumption.kind,
            **details,
        },
    }


def _assumption_claimed_value(assumption: StructuredAuthorityAssumption) -> object:
    """Return the claimed scalar or set value for an already typed claim."""
    if isinstance(assumption, ItemStatusAssumptionClaim):
        return assumption.status.value
    if isinstance(assumption, AcceptedNormativeCountAssumptionClaim):
        return assumption.count
    if isinstance(assumption, AcceptedNormativeSetAssumptionClaim):
        return list(assumption.item_ids)
    msg = f"unsupported structured assumption: {type(assumption).__name__}"
    raise TypeError(msg)


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


def _rendered_assumption_items(value: object) -> list[JsonDict]:
    """Render valid typed assumptions and safely preserve malformed entries."""
    rendered: list[JsonDict] = []
    for index, raw_assumption in enumerate(_as_list(value), start=1):
        try:
            assumption = AUTHORITY_ASSUMPTION_ADAPTER.validate_python(raw_assumption)
        except ValidationError:
            rendered.append(
                _normalized_persisted_item(
                    raw_assumption,
                    fallback_id=f"ASM-{index}",
                )
            )
            continue
        item = _plain_item(
            item_id=f"ASM-{index}",
            text=render_assumption_text(assumption),
        )
        item["assumption_key"] = canonical_assumption_key(assumption)
        rendered.append(item)
    return rendered


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
    load_result = load_stored_compiled_artifact(authority)
    return load_result.artifact if load_result.ok else None


def _compiled_artifact_shape_findings(
    authority: CompiledSpecAuthority,
    *,
    project_id: int,
) -> list[JsonDict]:
    """Return non-overrideable findings for malformed compiler artifacts."""
    load_result = load_stored_compiled_artifact(authority)
    if load_result.ok:
        return []
    failure = compiled_authority_read_failure(
        load_result,
        project_id=project_id,
        spec_version_id=authority.spec_version_id,
        authority_id=authority.authority_id,
    )
    if failure is None:
        return []
    return [
        {
            "finding_id": failure.error_code,
            "severity": "blocking",
            "code": failure.error_code,
            "message": failure.message,
            "candidate_ids": [],
            "source_unit_ids": [],
            "override_allowed": False,
            "details": dict(failure.details),
            "remediation": list(failure.remediation),
        }
    ]


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
    authority_quality = (
        artifact.authority_quality.model_dump(mode="json")
        if artifact.authority_quality is not None
        else None
    )

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
        assumption.model_dump(mode="json") for assumption in artifact.assumptions
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
            for item in _rendered_assumption_items(assumptions)
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
            "authority_quality": authority_quality,
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
    assumptions = _rendered_assumption_items(
        _fallback_assumption_items(authority.compiled_artifact_json)
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
        "authority_quality": None,
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
        if key == "assumptions":
            items = _rendered_assumption_items(items)
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
        normalized.append(
            _normalized_persisted_item(item, fallback_id=f"{prefix}-{index}")
        )
    return normalized


def _normalized_persisted_item(item: object, *, fallback_id: str) -> JsonDict:
    """Return the established safe display object for one persisted item."""
    if not isinstance(item, Mapping):
        return _plain_item(item_id=fallback_id, text=str(item))
    item_mapping = cast("Mapping[str, Any]", item)
    source_refs = _dedupe_sorted(_as_list(item_mapping.get("source_refs")))
    source_excerpt = item_mapping.get("source_excerpt")
    return {
        "id": str(item_mapping.get("id") or fallback_id),
        "text": _persisted_item_text(item_mapping),
        "support": _persisted_item_support(item_mapping, source_refs),
        "source_refs": source_refs,
        "source_excerpt": (str(source_excerpt) if source_excerpt is not None else None),
    }


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
