"""Runtime helpers for invoking the backlog agent from workflow state."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from orchestrator_agent.agent_tools.as_built_assessor.schemes import (
    AsBuiltAssessment,
    CapabilityAssessment,
)
from orchestrator_agent.agent_tools.backlog_primer.agent import (
    root_agent as backlog_agent,
)
from orchestrator_agent.agent_tools.backlog_primer.schemes import (
    BacklogItem,
    InputSchema,
    OutputSchema,
)
from services.agent_workbench.as_built_assessment import cached_assessment_for_backlog
from utils.adk_runner import (
    get_agent_model_info,
    invoke_agent_to_text,
    parse_json_payload,
)
from utils.brownfield_annotations import (
    BrownfieldAnnotation,
    BrownfieldDisagreement,
    BrownfieldMatchTier,
    BrownfieldModelAssertion,
    BrownfieldSelectedCapability,
    BrownfieldWarning,
    BrownfieldWarningCode,
    BrownfieldWarningSeverity,
)
from utils.failure_artifacts import (
    AgentInvocationError,
    FailureArtifactResult,
    FailureMetadataDict,
    write_failure_artifact,
)
from utils.runtime_config import BACKLOG_RUNNER_IDENTITY

logger: logging.Logger = logging.getLogger(name=__name__)

type BacklogInputContext = dict[str, object]
type ValidationErrors = list[dict[str, object]]
type ModelAssertionsByIndex = dict[int, BrownfieldModelAssertion]

_BROWNFIELD_TOKEN_STOPWORDS = frozenset({"only"})
_MIN_BROWNFIELD_TOKEN_LENGTH = 3
_PLURAL_TRIM_TOKEN_LENGTH = 4
_COMPACT_MATCH_TOKEN_LENGTH = 5
_PREFIX_MATCH_TOKEN_LENGTH = 4
_AUTHORITY_REVIEW_REQUIRED_MESSAGE = (
    "AUTHORITY_REVIEW_REQUIRED: scope extension authority must be accepted "
    "before backlog generation."
)


@dataclass(frozen=True)
class _FailureDetails:
    """Structured details describing a backlog-runtime failure."""

    message: str
    raw_text: str | None = None
    validation_errors: ValidationErrors | None = None
    exception: BaseException | None = None


@dataclass(frozen=True)
class _CapabilityIndex:
    """Lookup tables for authoritative As-Built capabilities."""

    capabilities: tuple[CapabilityAssessment, ...]
    by_authority_ref: dict[str, tuple[CapabilityAssessment, ...]]
    by_exact_authority_ref: dict[str, CapabilityAssessment]
    ambiguous_authority_refs: set[str]
    by_invariant_ref: dict[str, CapabilityAssessment]
    ambiguous_invariant_refs: set[str]
    by_normalized_key: dict[str, CapabilityAssessment]
    ambiguous_normalized_keys: set[str]


@dataclass(frozen=True)
class _BrownfieldAnnotationResult:
    """Host-derived annotations and warnings for one backlog output."""

    annotations_by_index: dict[int, BrownfieldAnnotation]
    warnings: list[BrownfieldWarning]


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _normalize_prior_backlog_state(value: object) -> str:
    if value is None:
        return "NO_HISTORY"
    if isinstance(value, str):
        text = value.strip()
        return text if text else "NO_HISTORY"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return "NO_HISTORY"


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _existing_backlog_item_count(state: Mapping[str, Any]) -> int | None:
    backlog_items = state.get("backlog_items")
    if isinstance(backlog_items, list):
        return len(backlog_items)
    assessment = state.get("product_backlog_assessment")
    if isinstance(assessment, Mapping):
        assessment_items = assessment.get("backlog_items")
        if isinstance(assessment_items, list):
            return len(assessment_items)
    return None


def _scope_extension_authority_not_ready(state: Mapping[str, Any]) -> bool:
    setup_status = state.get("setup_status")
    if isinstance(setup_status, str) and setup_status.strip().lower() in {
        "authority_compile_required",
        "authority_compiling",
        "authority_compile_failed",
        "authority_pending_review",
        "authority_rejected",
    }:
        return True
    fsm_state = state.get("fsm_state")
    return isinstance(fsm_state, str) and fsm_state.strip().upper() == "SETUP_REQUIRED"


def _scope_extension_generation_metadata(
    state: Mapping[str, Any],
) -> dict[str, object]:
    context = state.get("scope_extension_context")
    if not isinstance(context, Mapping):
        return {}
    if context.get("backlog_extension_saved_at"):
        return {}
    added_source_item_ids = _string_list(context.get("added_source_item_ids"))
    if not added_source_item_ids:
        return {}

    scope_extension: dict[str, object] = {}
    for key in (
        "schema",
        "base_spec_version_id",
        "base_spec_hash",
        "amended_spec_version_id",
        "amended_spec_hash",
    ):
        if key in context:
            scope_extension[key] = context[key]
    scope_extension["added_source_item_ids"] = added_source_item_ids
    existing_count = _existing_backlog_item_count(state)
    if existing_count is not None:
        scope_extension["existing_backlog_item_count"] = existing_count

    return {
        "generation_mode": "scope_extension",
        "scope_extension": scope_extension,
        "authority_scope_filter": {"source_item_ids": added_source_item_ids},
    }


def _normalize_validation_errors(errors: object) -> ValidationErrors:
    normalized: ValidationErrors = []
    if not isinstance(errors, list):
        return normalized

    for error in errors:
        if not isinstance(error, Mapping):
            continue
        normalized.append({str(key): value for key, value in error.items()})
    return normalized


def _normalize_brownfield_text(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).casefold())


def _brownfield_tokens(value: object) -> tuple[str, ...]:
    tokens: list[str] = []
    for raw_token in re.findall(r"[a-z0-9]+", str(value).casefold()):
        if (
            len(raw_token) < _MIN_BROWNFIELD_TOKEN_LENGTH
            or raw_token in _BROWNFIELD_TOKEN_STOPWORDS
        ):
            continue
        token = raw_token
        if token.endswith("s") and len(token) > _PLURAL_TRIM_TOKEN_LENGTH:
            token = raw_token[:-1]
        tokens.append(token)
    return tuple(tokens)


def _brownfield_token_matches(
    token: str,
    *,
    item_tokens: tuple[str, ...],
    item_compact: str,
) -> bool:
    if len(token) >= _COMPACT_MATCH_TOKEN_LENGTH and token in item_compact:
        return True
    return any(
        token == item_token
        or (
            len(token) >= _PREFIX_MATCH_TOKEN_LENGTH
            and len(item_token) >= _PREFIX_MATCH_TOKEN_LENGTH
            and (token.startswith(item_token) or item_token.startswith(token))
        )
        for item_token in item_tokens
    )


def _matches_capability_title_terms(
    *,
    capability: CapabilityAssessment,
    item: BacklogItem,
) -> bool:
    capability_tokens = _brownfield_tokens(capability.capability_title)
    minimum_title_tokens = 2
    if len(capability_tokens) < minimum_title_tokens:
        return False

    item_text = (
        f"{item.requirement} {item.capability_hint or ''} {item.technical_note or ''}"
    )
    item_tokens = _brownfield_tokens(item_text)
    item_compact = _normalize_brownfield_text(item_text)
    return all(
        _brownfield_token_matches(
            token,
            item_tokens=item_tokens,
            item_compact=item_compact,
        )
        for token in capability_tokens
    )


def _same_backlog_contract(
    left: CapabilityAssessment,
    right: CapabilityAssessment,
) -> bool:
    return (
        left.authority_ref == right.authority_ref
        and _normalize_brownfield_text(left.capability_title)
        == _normalize_brownfield_text(right.capability_title)
        and left.status == right.status
        and left.recommended_backlog_treatment == right.recommended_backlog_treatment
    )


def _store_unique_capability_reference(
    *,
    key: str,
    capability: CapabilityAssessment,
    by_key: dict[str, CapabilityAssessment],
    ambiguous_keys: set[str],
) -> None:
    if not key or key in ambiguous_keys:
        return

    existing = by_key.get(key)
    if existing is None or existing is capability:
        by_key[key] = capability
        return

    if not _same_backlog_contract(existing, capability):
        ambiguous_keys.add(key)
        by_key.pop(key, None)


def _build_capability_index(
    assessment: AsBuiltAssessment,
) -> _CapabilityIndex:
    grouped_by_authority_ref: dict[str, list[CapabilityAssessment]] = {}
    unique_capabilities: dict[tuple[str, str, str, str], CapabilityAssessment] = {}
    by_exact_authority_ref: dict[str, CapabilityAssessment] = {}
    ambiguous_authority_refs: set[str] = set()
    by_invariant_ref: dict[str, CapabilityAssessment] = {}
    ambiguous_invariant_refs: set[str] = set()
    by_normalized_key: dict[str, CapabilityAssessment] = {}
    ambiguous_normalized_keys: set[str] = set()
    for capability in assessment.capability_assessments:
        unique_capabilities.setdefault(
            (
                capability.authority_ref,
                _normalize_brownfield_text(capability.capability_title),
                capability.status,
                capability.recommended_backlog_treatment,
            ),
            capability,
        )
        grouped_by_authority_ref.setdefault(capability.authority_ref, []).append(
            capability
        )

        _store_unique_capability_reference(
            key=capability.authority_ref,
            capability=capability,
            by_key=by_exact_authority_ref,
            ambiguous_keys=ambiguous_authority_refs,
        )

        for invariant_ref in capability.invariant_refs:
            _store_unique_capability_reference(
                key=_normalize_brownfield_text(invariant_ref),
                capability=capability,
                by_key=by_invariant_ref,
                ambiguous_keys=ambiguous_invariant_refs,
            )

        for candidate in (capability.authority_ref, capability.capability_title):
            _store_unique_capability_reference(
                key=_normalize_brownfield_text(candidate),
                capability=capability,
                by_key=by_normalized_key,
                ambiguous_keys=ambiguous_normalized_keys,
            )
    return _CapabilityIndex(
        capabilities=tuple(unique_capabilities.values()),
        by_authority_ref={
            authority_ref: tuple(capabilities)
            for authority_ref, capabilities in grouped_by_authority_ref.items()
        },
        by_exact_authority_ref=by_exact_authority_ref,
        ambiguous_authority_refs=ambiguous_authority_refs,
        by_invariant_ref=by_invariant_ref,
        ambiguous_invariant_refs=ambiguous_invariant_refs,
        by_normalized_key=by_normalized_key,
        ambiguous_normalized_keys=ambiguous_normalized_keys,
    )


def _selected_capability(
    capability: CapabilityAssessment,
) -> BrownfieldSelectedCapability:
    return BrownfieldSelectedCapability(
        authority_ref=capability.authority_ref,
        capability_title=capability.capability_title,
        invariant_refs=list(capability.invariant_refs),
        as_built_status=capability.status,
        recommended_backlog_treatment=capability.recommended_backlog_treatment,
        confidence=capability.confidence,
    )


def _model_assertion(
    item: BacklogItem,
    assertion: BrownfieldModelAssertion | None = None,
) -> BrownfieldModelAssertion:
    if assertion is not None:
        return BrownfieldModelAssertion(
            authority_ref=assertion.authority_ref or item.authority_ref,
            capability_hint=assertion.capability_hint or item.capability_hint,
            as_built_status=assertion.as_built_status,
            recommended_backlog_treatment=assertion.recommended_backlog_treatment,
        )
    return BrownfieldModelAssertion(
        authority_ref=item.authority_ref,
        capability_hint=item.capability_hint,
    )


def _sanitize_backlog_output_payload(
    parsed: dict[str, Any],
) -> tuple[dict[str, Any], ModelAssertionsByIndex]:
    """Strip host-owned/legacy fields before validating model output."""
    sanitized = dict(parsed)
    assertions: ModelAssertionsByIndex = {}
    items = sanitized.get("backlog_items")
    if not isinstance(items, list):
        return sanitized, assertions

    sanitized_items: list[object] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            sanitized_items.append(item)
            continue
        sanitized_item = dict(item)
        legacy_capability_name = sanitized_item.pop("capability_name", None)
        legacy_status = sanitized_item.pop("as_built_status", None)
        legacy_treatment = sanitized_item.pop("recommended_backlog_treatment", None)
        sanitized_item.pop("as_built_annotation", None)

        if sanitized_item.get("capability_hint") is None and isinstance(
            legacy_capability_name,
            str,
        ):
            sanitized_item["capability_hint"] = legacy_capability_name

        if any(
            value is not None
            for value in (legacy_capability_name, legacy_status, legacy_treatment)
        ):
            assertions[index] = BrownfieldModelAssertion.model_validate(
                {
                    "authority_ref": sanitized_item.get("authority_ref"),
                    "capability_hint": sanitized_item.get("capability_hint")
                    or legacy_capability_name,
                    "as_built_status": legacy_status,
                    "recommended_backlog_treatment": legacy_treatment,
                }
            )
        sanitized_items.append(sanitized_item)

    sanitized["backlog_items"] = sanitized_items
    return sanitized, assertions


def _unique_contract_candidates(
    capabilities: tuple[CapabilityAssessment, ...],
) -> list[CapabilityAssessment]:
    unique: list[CapabilityAssessment] = []
    for capability in capabilities:
        if not any(_same_backlog_contract(capability, existing) for existing in unique):
            unique.append(capability)
    return unique


def _capability_text_overlaps_item(
    *,
    capability: CapabilityAssessment,
    item: BacklogItem,
) -> bool:
    capability_tokens = _brownfield_tokens(capability.capability_title)
    if not capability_tokens:
        return True
    item_text = f"{item.requirement} {item.capability_hint or ''}"
    item_tokens = _brownfield_tokens(item_text)
    item_compact = _normalize_brownfield_text(item_text)
    return any(
        _brownfield_token_matches(
            token,
            item_tokens=item_tokens,
            item_compact=item_compact,
        )
        for token in capability_tokens
    )


def _make_warning(  # noqa: PLR0913
    *,
    code: BrownfieldWarningCode,
    item_index: int,
    match_tier: BrownfieldMatchTier,
    message: str,
    severity: BrownfieldWarningSeverity = "review",
    capability: CapabilityAssessment | None = None,
    details: dict[str, object] | None = None,
) -> BrownfieldWarning:
    return BrownfieldWarning(
        code=code,
        item_index=item_index,
        severity=severity,
        match_tier=match_tier,
        authority_ref=capability.authority_ref if capability is not None else None,
        invariant_refs=list(capability.invariant_refs)
        if capability is not None
        else [],
        message=message,
        details=details or {},
    )


def _limited_item_warnings(
    warnings: list[BrownfieldWarning],
    *,
    max_warnings: int = 3,
) -> list[BrownfieldWarning]:
    """Cap per-item warnings without dropping save-blocking diagnostics."""
    blocking = [
        warning for warning in warnings if warning.severity == "block_on_save"
    ]
    non_blocking = [
        warning for warning in warnings if warning.severity != "block_on_save"
    ]
    if len(blocking) >= max_warnings:
        return blocking
    return [*blocking, *non_blocking[: max_warnings - len(blocking)]]


def _exact_authority_annotation(  # noqa: C901, PLR0912
    *,
    item: BacklogItem,
    item_index: int,
    capability_index: _CapabilityIndex,
    model_assertion: BrownfieldModelAssertion | None,
) -> tuple[BrownfieldAnnotation | None, list[BrownfieldWarning]]:
    if item.authority_ref is None:
        return None, []

    warnings: list[BrownfieldWarning] = []
    candidates: list[CapabilityAssessment] = []
    selected: CapabilityAssessment | None = None
    match_basis = "authority_ref"
    conflict = False

    authority_capabilities = capability_index.by_authority_ref.get(item.authority_ref)
    if authority_capabilities is not None:
        candidates = _unique_contract_candidates(authority_capabilities)
        if len(candidates) == 1:
            selected = candidates[0]
        else:
            conflict = True
    else:
        normalized_ref = _normalize_brownfield_text(item.authority_ref)
        capability = capability_index.by_invariant_ref.get(normalized_ref)
        if capability is not None:
            selected = capability
            candidates = [capability]
            match_basis = "invariant_ref"
        elif normalized_ref in capability_index.ambiguous_invariant_refs:
            conflict = True
            match_basis = "invariant_ref"

    if selected is None and not candidates and not conflict:
        warning = _make_warning(
            code="asserted_authority_ref_unmatched",
            item_index=item_index,
            match_tier="none",
            severity="block_on_save",
            message=(
                "Model asserted an authority_ref that does not match the "
                "cached As-Built assessment."
            ),
            details={"authority_ref": item.authority_ref},
        )
        annotation = BrownfieldAnnotation(
            schema_version="agileforge.brownfield_annotation.v1",
            match_tier="none",
            match_basis=["authority_ref"],
            selected=None,
            candidates=[],
            model_assertion=_model_assertion(item, model_assertion),
            warning_codes=[warning.code],
        )
        return annotation, [warning]

    if conflict:
        warning = _make_warning(
            code="conflicting_invariants",
            item_index=item_index,
            match_tier="exact",
            message=(
                "Exact authority_ref maps to multiple As-Built invariant "
                "contracts with different status or treatment."
            ),
            details={"authority_ref": item.authority_ref},
        )
        annotation = BrownfieldAnnotation(
            schema_version="agileforge.brownfield_annotation.v1",
            match_tier="exact",
            match_basis=[match_basis],
            conflict=True,
            selected=None,
            candidates=[_selected_capability(capability) for capability in candidates],
            model_assertion=_model_assertion(item, model_assertion),
            warning_codes=[warning.code],
        )
        return annotation, [warning]

    if selected is None:
        msg = "expected exact As-Built capability selection"
        raise RuntimeError(msg)
    warnings.append(
        _make_warning(
            code="metadata_filled_by_host",
            item_index=item_index,
            match_tier="exact",
            severity="info",
            capability=selected,
            message="Host filled brownfield annotation from exact As-Built match.",
        )
    )
    assertion = _model_assertion(item, model_assertion)
    disagreements: list[BrownfieldDisagreement] = []
    if (
        assertion.as_built_status is not None
        and assertion.as_built_status != selected.status
    ):
        warnings.append(
            _make_warning(
                code="status_disagreement",
                item_index=item_index,
                match_tier="exact",
                capability=selected,
                message=(
                    "Model asserted an As-Built status that differs from the "
                    "host-selected capability."
                ),
                details={
                    "model_value": assertion.as_built_status,
                    "host_value": selected.status,
                },
            )
        )
        disagreements.append(
            BrownfieldDisagreement(
                field="as_built_status",
                model_value=assertion.as_built_status,
                host_value=selected.status,
                code="status_disagreement",
            )
        )
    if (
        assertion.recommended_backlog_treatment is not None
        and assertion.recommended_backlog_treatment
        != selected.recommended_backlog_treatment
    ):
        warnings.append(
            _make_warning(
                code="treatment_disagreement",
                item_index=item_index,
                match_tier="exact",
                capability=selected,
                message=(
                    "Model asserted a backlog treatment that differs from the "
                    "host-selected capability."
                ),
                details={
                    "model_value": assertion.recommended_backlog_treatment,
                    "host_value": selected.recommended_backlog_treatment,
                },
            )
        )
        disagreements.append(
            BrownfieldDisagreement(
                field="recommended_backlog_treatment",
                model_value=assertion.recommended_backlog_treatment,
                host_value=selected.recommended_backlog_treatment,
                code="treatment_disagreement",
            )
        )
    if not _capability_text_overlaps_item(capability=selected, item=item):
        warnings.append(
            _make_warning(
                code="capability_disagreement",
                item_index=item_index,
                match_tier="exact",
                capability=selected,
                message=(
                    "Model authority_ref points at an As-Built capability whose "
                    "title does not overlap the item title or capability_hint."
                ),
                details={
                    "model_value": item.capability_hint or item.requirement,
                    "host_value": selected.capability_title,
                },
            )
        )
        disagreements.append(
            BrownfieldDisagreement(
                field="capability_hint",
                model_value=item.capability_hint or item.requirement,
                host_value=selected.capability_title,
                code="capability_disagreement",
            )
        )

    warning_codes = [warning.code for warning in _limited_item_warnings(warnings)]
    annotation = BrownfieldAnnotation(
        schema_version="agileforge.brownfield_annotation.v1",
        match_tier="exact",
        match_basis=[match_basis],
        selected=_selected_capability(selected),
        candidates=[],
        model_assertion=assertion,
        disagreements=disagreements,
        warning_codes=warning_codes,
    )
    return annotation, _limited_item_warnings(warnings)


def _possible_unmapped_capability_matches(
    item: BacklogItem,
    capability_index: _CapabilityIndex,
) -> list[CapabilityAssessment]:
    return [
        capability
        for capability in capability_index.capabilities
        if _matches_capability_title_terms(capability=capability, item=item)
    ]


def derive_brownfield_annotations(
    *,
    output_model: OutputSchema,
    input_context: BacklogInputContext,
    model_assertions: ModelAssertionsByIndex | None = None,
) -> _BrownfieldAnnotationResult:
    """Derive host-owned brownfield annotations from authoritative As-Built input."""
    raw_assessment = input_context.get("as_built_assessment")
    if raw_assessment == "NO_AS_BUILT_ASSESSMENT":
        return _BrownfieldAnnotationResult(annotations_by_index={}, warnings=[])
    if not isinstance(raw_assessment, str):
        msg = "as_built_assessment must be a serialized As-Built JSON string"
        raise TypeError(msg)

    assessment = AsBuiltAssessment.model_validate_json(raw_assessment)
    capability_index = _build_capability_index(assessment)
    annotations_by_index: dict[int, BrownfieldAnnotation] = {}
    warnings: list[BrownfieldWarning] = []

    for index, item in enumerate(output_model.backlog_items):
        annotation, item_warnings = _exact_authority_annotation(
            item=item,
            item_index=index,
            capability_index=capability_index,
            model_assertion=(model_assertions or {}).get(index),
        )
        if annotation is not None:
            annotations_by_index[index] = annotation
            warnings.extend(item_warnings)
            continue

        possible_matches = _possible_unmapped_capability_matches(
            item,
            capability_index,
        )
        if not possible_matches:
            continue

        item_warning = _make_warning(
            code="possible_mapping",
            item_index=index,
            match_tier="fuzzy",
            message=(
                "Backlog item resembles As-Built capability text but has no "
                "exact authority_ref or invariant_ref."
            ),
            details={
                "candidate_authority_refs": [
                    capability.authority_ref for capability in possible_matches[:3]
                ]
            },
        )
        annotations_by_index[index] = BrownfieldAnnotation(
            schema_version="agileforge.brownfield_annotation.v1",
            match_tier="fuzzy",
            match_basis=["capability_title_terms"],
            selected=None,
            candidates=[
                _selected_capability(capability) for capability in possible_matches[:3]
            ],
            model_assertion=_model_assertion(
                item,
                (model_assertions or {}).get(index),
            ),
            warning_codes=[item_warning.code],
        )
        warnings.append(item_warning)

    return _BrownfieldAnnotationResult(
        annotations_by_index=annotations_by_index,
        warnings=warnings,
    )


def build_backlog_input_context(
    state: dict[str, Any],
    *,
    user_input: str | None,
) -> BacklogInputContext:
    """Build the serialized backlog-agent input payload from workflow state."""
    vision_assessment = state.get("product_vision_assessment") or {}
    vision_stmt = vision_assessment.get("product_vision_statement") or ""
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
        "as_built_assessment": cached_assessment_for_backlog(state),
        "implementation_evidence": implementation_evidence or "NO_EVIDENCE",
        "user_input": user_input or "",
        **_scope_extension_generation_metadata(state),
    }


async def _invoke_backlog_agent(payload: InputSchema) -> str:
    return await invoke_agent_to_text(
        agent=backlog_agent,
        runner_identity=BACKLOG_RUNNER_IDENTITY,
        payload_json=payload.model_dump_json(exclude_none=True),
        no_text_error="Backlog agent returned no text response",
    )


async def _invoke_and_validate_output(
    *,
    payload: InputSchema,
) -> tuple[str, OutputSchema, ModelAssertionsByIndex] | dict[str, _FailureDetails]:
    try:
        raw_text: str = await _invoke_backlog_agent(payload)
    except (AgentInvocationError, ValueError) as exc:
        raw_output = (
            exc.partial_output if isinstance(exc, AgentInvocationError) else None
        )
        return {
            "invocation_exception": _FailureDetails(
                message=f"Backlog runtime failed: {exc}",
                raw_text=raw_output,
                exception=exc,
            )
        }

    parsed: dict[str, Any] | None = parse_json_payload(raw_text)
    if parsed is None:
        return {
            "invalid_json": _FailureDetails(
                message="Backlog response is not valid JSON",
                raw_text=raw_text,
            )
        }

    try:
        sanitized, model_assertions = _sanitize_backlog_output_payload(parsed)
        output_model: OutputSchema = OutputSchema.model_validate(sanitized)
    except ValidationError as exc:
        return {
            "output_validation": _FailureDetails(
                message=f"Backlog output validation failed: {exc}",
                raw_text=raw_text,
                validation_errors=_normalize_validation_errors(exc.errors()),
                exception=exc,
            )
        }

    return raw_text, output_model, model_assertions


def _failure(
    *,
    project_id: int,
    input_context: BacklogInputContext,
    failure_stage: str,
    details: _FailureDetails,
    extra: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    message: str = details.message
    artifact_result: FailureArtifactResult = write_failure_artifact(
        phase="backlog",
        project_id=project_id,
        failure_stage=failure_stage,
        failure_summary=message,
        raw_output=details.raw_text,
        context={"input_context": input_context},
        model_info={
            **get_agent_model_info(backlog_agent),
            "app_name": BACKLOG_RUNNER_IDENTITY.app_name,
            "user_id": BACKLOG_RUNNER_IDENTITY.user_id,
        },
        validation_errors=details.validation_errors,
        exception=details.exception,
        extra=extra,
    )
    metadata: FailureMetadataDict = artifact_result["metadata"]
    if details.exception is not None:
        logger.exception(
            "Backlog generation failed [artifact_id=%s stage=%s]: %s",
            metadata["failure_artifact_id"],
            failure_stage,
            message,
        )
    else:
        logger.error(
            "Backlog generation failed [artifact_id=%s stage=%s]: %s",
            metadata["failure_artifact_id"],
            failure_stage,
            message,
        )

    artifact: dict[str, Any] = {
        "error": "BACKLOG_GENERATION_FAILED",
        "message": message,
        "is_complete": False,
        "clarifying_questions": [],
        "failure_artifact_id": metadata["failure_artifact_id"],
        "failure_stage": metadata["failure_stage"],
        "failure_summary": metadata["failure_summary"],
        "raw_output_preview": metadata["raw_output_preview"],
        "has_full_artifact": metadata["has_full_artifact"],
    }

    result: dict[str, Any] = {
        "success": False,
        "input_context": input_context,
        "output_artifact": artifact,
        "is_complete": None,
        "error": message,
        **metadata,
    }
    if extra:
        result.update(extra)
    return result


async def run_backlog_agent_from_state(
    state: dict[str, Any],
    *,
    project_id: int,
    user_input: str | None,
) -> dict[str, Any]:
    """Run the backlog agent from stored workflow state and normalize failures."""
    input_context: BacklogInputContext = build_backlog_input_context(
        state,
        user_input=user_input,
    )
    if (
        input_context.get("generation_mode") == "scope_extension"
        and _scope_extension_authority_not_ready(state)
    ):
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage="authority_review_required",
            details=_FailureDetails(message=_AUTHORITY_REVIEW_REQUIRED_MESSAGE),
        )

    try:
        payload: InputSchema = InputSchema.model_validate(input_context)
    except ValidationError as exc:
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage="input_validation",
            details=_FailureDetails(
                message=f"Backlog input validation failed: {exc}",
                validation_errors=_normalize_validation_errors(exc.errors()),
                exception=exc,
            ),
        )

    validated = await _invoke_and_validate_output(payload=payload)
    if isinstance(validated, dict):
        failure_stage, failure_details = next(iter(validated.items()))
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage=failure_stage,
            details=failure_details,
        )
    _raw_text, output_model, model_assertions = validated

    try:
        annotation_result = derive_brownfield_annotations(
            output_model=output_model,
            input_context=input_context,
            model_assertions=model_assertions,
        )
    except (TypeError, ValidationError, ValueError) as exc:
        validation_errors = (
            _normalize_validation_errors(exc.errors())
            if isinstance(exc, ValidationError)
            else None
        )
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage="brownfield_annotation",
            details=_FailureDetails(
                message=f"Backlog brownfield annotation failed: {exc}",
                validation_errors=validation_errors,
                exception=exc,
            ),
        )

    output_artifact: dict[str, Any] = output_model.model_dump(exclude_none=True)
    for index, annotation in annotation_result.annotations_by_index.items():
        try:
            item = output_artifact["backlog_items"][index]
        except (KeyError, IndexError, TypeError):
            continue
        if isinstance(item, dict):
            item["as_built_annotation"] = annotation.model_dump(exclude_none=False)
    output_artifact["brownfield_warnings"] = [
        warning.model_dump(exclude_none=False) for warning in annotation_result.warnings
    ]
    if _has_clarifying_questions(output_artifact):
        output_artifact["is_complete"] = False
    return {
        "success": True,
        "input_context": input_context,
        "output_artifact": output_artifact,
        "is_complete": bool(output_artifact.get("is_complete", False)),
        "error": None,
        "failure_artifact_id": None,
        "failure_stage": None,
        "failure_summary": None,
        "raw_output_preview": None,
        "has_full_artifact": False,
    }


def _has_clarifying_questions(output_artifact: dict[str, Any]) -> bool:
    """Return whether output still contains blocking clarification questions."""
    questions = output_artifact.get("clarifying_questions")
    return isinstance(questions, list) and any(
        isinstance(question, str) and bool(question.strip()) for question in questions
    )
