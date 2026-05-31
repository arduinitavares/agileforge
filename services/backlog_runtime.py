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

_ALLOWED_TITLE_PREFIXES_BY_STATUS: dict[str, tuple[str, ...]] = {
    "observed": ("Verify", "Document", "Monitor", "Preserve"),
    "observed_with_missing_evidence": (
        "Verify",
        "Validate",
        "Harden",
        "Formalize",
        "Add Evidence For",
    ),
    "contradicted": ("Resolve", "Align", "Correct"),
    "unclear": ("Discover", "Investigate", "Clarify"),
    "not_observed": ("Build", "Add", "Implement", "Create"),
}


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

    by_authority_ref: dict[str, tuple[CapabilityAssessment, ...]]
    by_exact_authority_ref: dict[str, CapabilityAssessment]
    ambiguous_authority_refs: set[str]
    by_normalized_key: dict[str, CapabilityAssessment]
    ambiguous_normalized_keys: set[str]


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


def _normalize_title_prefix(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


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


def _build_capability_index(
    assessment: AsBuiltAssessment,
) -> _CapabilityIndex:
    grouped_by_authority_ref: dict[str, list[CapabilityAssessment]] = {}
    by_exact_authority_ref: dict[str, CapabilityAssessment] = {}
    ambiguous_authority_refs: set[str] = set()
    by_normalized_key: dict[str, CapabilityAssessment] = {}
    ambiguous_normalized_keys: set[str] = set()
    for capability in assessment.capability_assessments:
        grouped_by_authority_ref.setdefault(capability.authority_ref, []).append(
            capability
        )

        exact_existing = by_exact_authority_ref.get(capability.authority_ref)
        if exact_existing is not None and exact_existing is not capability:
            if not _same_backlog_contract(exact_existing, capability):
                ambiguous_authority_refs.add(capability.authority_ref)
                by_exact_authority_ref.pop(capability.authority_ref, None)
        elif capability.authority_ref not in ambiguous_authority_refs:
            by_exact_authority_ref[capability.authority_ref] = capability

        for candidate in (capability.authority_ref, capability.capability_title):
            normalized = _normalize_brownfield_text(candidate)
            if not normalized:
                continue
            if normalized in ambiguous_normalized_keys:
                continue
            existing = by_normalized_key.get(normalized)
            if existing is not None and existing is not capability:
                if not _same_backlog_contract(existing, capability):
                    ambiguous_normalized_keys.add(normalized)
                    by_normalized_key.pop(normalized, None)
                continue
            by_normalized_key[normalized] = capability
    return _CapabilityIndex(
        by_authority_ref={
            authority_ref: tuple(capabilities)
            for authority_ref, capabilities in grouped_by_authority_ref.items()
        },
        by_exact_authority_ref=by_exact_authority_ref,
        ambiguous_authority_refs=ambiguous_authority_refs,
        by_normalized_key=by_normalized_key,
        ambiguous_normalized_keys=ambiguous_normalized_keys,
    )


def _matches_item_selectors(
    capability: CapabilityAssessment,
    item: BacklogItem,
) -> bool:
    if item.capability_name is not None and _normalize_brownfield_text(
        item.capability_name
    ) != _normalize_brownfield_text(capability.capability_title):
        return False
    if item.as_built_status is not None and item.as_built_status != capability.status:
        return False
    return not (
        item.recommended_backlog_treatment is not None
        and item.recommended_backlog_treatment
        != capability.recommended_backlog_treatment
    )


def _select_authority_ref_capability(
    *,
    item: BacklogItem,
    capabilities: tuple[CapabilityAssessment, ...],
) -> CapabilityAssessment:
    if len(capabilities) == 1:
        return capabilities[0]

    matches = [
        capability
        for capability in capabilities
        if _matches_item_selectors(capability, item)
    ]
    if not matches:
        message = "authority_ref metadata does not match As-Built capability"
        raise ValueError(message)

    selected = matches[0]
    if all(_same_backlog_contract(selected, match) for match in matches):
        return selected

    message = "ambiguous As-Built authority_ref"
    raise ValueError(message)


def _mapped_capability(
    item: BacklogItem,
    capability_index: _CapabilityIndex,
) -> CapabilityAssessment | None:
    if item.authority_ref is not None:
        authority_capabilities = capability_index.by_authority_ref.get(
            item.authority_ref
        )
        if authority_capabilities is not None:
            return _select_authority_ref_capability(
                item=item,
                capabilities=authority_capabilities,
            )
        capability = capability_index.by_exact_authority_ref.get(item.authority_ref)
        if capability is not None:
            return capability

    for candidate in (item.authority_ref, item.capability_name, item.requirement):
        if candidate is None:
            continue
        normalized = _normalize_brownfield_text(candidate)
        if normalized in capability_index.ambiguous_normalized_keys:
            message = "duplicate ambiguous As-Built capability key"
            raise ValueError(message)
        capability = capability_index.by_normalized_key.get(normalized)
        if capability is not None:
            return capability
    return None


def _has_brownfield_metadata(item: BacklogItem) -> bool:
    return any(
        value is not None
        for value in (
            item.capability_name,
            item.authority_ref,
            item.as_built_status,
            item.recommended_backlog_treatment,
        )
    )


def _title_has_allowed_prefix(*, requirement: str, status: str) -> bool:
    normalized_requirement = _normalize_title_prefix(requirement)
    for prefix in _ALLOWED_TITLE_PREFIXES_BY_STATUS.get(status, ()):
        normalized_prefix = _normalize_title_prefix(prefix)
        if normalized_requirement == normalized_prefix:
            return True
        if normalized_requirement.startswith(f"{normalized_prefix} "):
            return True
    return False


def _validate_mapped_brownfield_metadata(
    *,
    prefix: str,
    item: BacklogItem,
    capability: CapabilityAssessment,
) -> list[str]:
    errors: list[str] = []

    if item.as_built_status is None:
        errors.append(f"{prefix} missing as_built_status")
    elif item.as_built_status != capability.status:
        errors.append(f"{prefix} as_built_status must equal {capability.status!r}")

    if item.recommended_backlog_treatment is None:
        errors.append(f"{prefix} missing recommended_backlog_treatment")
    elif item.recommended_backlog_treatment != capability.recommended_backlog_treatment:
        errors.append(
            f"{prefix} recommended_backlog_treatment must equal "
            f"{capability.recommended_backlog_treatment!r}"
        )

    return errors


def _validate_mapped_brownfield_item(
    *,
    index: int,
    item: BacklogItem,
    capability: CapabilityAssessment,
) -> list[str]:
    prefix = f"backlog_items[{index}]"
    errors: list[str] = []

    if item.capability_name is None:
        errors.append(f"{prefix} missing capability_name")
    elif _normalize_brownfield_text(item.capability_name) != (
        _normalize_brownfield_text(capability.capability_title)
    ):
        errors.append(
            f"{prefix} capability_name must match {capability.capability_title!r}"
        )

    if item.authority_ref is None:
        errors.append(f"{prefix} missing authority_ref")
    elif _normalize_brownfield_text(item.authority_ref) != (
        _normalize_brownfield_text(capability.authority_ref)
    ):
        errors.append(
            f"{prefix} authority_ref must match {capability.authority_ref!r}"
        )

    errors.extend(
        _validate_mapped_brownfield_metadata(
            prefix=prefix,
            item=item,
            capability=capability,
        )
    )

    normalized_requirement = _normalize_brownfield_text(item.requirement)
    if normalized_requirement == _normalize_brownfield_text(
        capability.capability_title
    ) or (
        item.capability_name is not None
        and normalized_requirement == _normalize_brownfield_text(item.capability_name)
    ):
        errors.append(f"{prefix} requirement must not equal capability title/name")

    if not _title_has_allowed_prefix(
        requirement=item.requirement,
        status=capability.status,
    ):
        allowed = ", ".join(_ALLOWED_TITLE_PREFIXES_BY_STATUS[capability.status])
        errors.append(
            f"{prefix} title prefix must match status {capability.status!r}; "
            f"allowed prefixes: {allowed}"
        )

    return errors


def _validate_brownfield_contract(
    *,
    output_model: OutputSchema,
    input_context: BacklogInputContext,
) -> None:
    """Validate brownfield backlog metadata against authoritative As-Built input."""
    raw_assessment = input_context.get("as_built_assessment")
    if raw_assessment == "NO_AS_BUILT_ASSESSMENT":
        return
    if not isinstance(raw_assessment, str):
        msg = "as_built_assessment must be a serialized As-Built JSON string"
        raise TypeError(msg)

    assessment = AsBuiltAssessment.model_validate_json(raw_assessment)
    capability_index = _build_capability_index(assessment)
    errors: list[str] = []

    for index, item in enumerate(output_model.backlog_items, start=1):
        capability = _mapped_capability(item, capability_index)
        if capability is None:
            if _has_brownfield_metadata(item):
                errors.append(
                    f"backlog_items[{index}] brownfield metadata does not match "
                    "As-Built capability"
                )
            continue

        errors.extend(
            _validate_mapped_brownfield_item(
                index=index,
                item=item,
                capability=capability,
            )
        )

    if errors:
        raise ValueError("; ".join(errors))


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
    }


async def _invoke_backlog_agent(payload: InputSchema) -> str:
    return await invoke_agent_to_text(
        agent=backlog_agent,
        runner_identity=BACKLOG_RUNNER_IDENTITY,
        payload_json=payload.model_dump_json(),
        no_text_error="Backlog agent returned no text response",
    )


def _failure(
    *,
    project_id: int,
    input_context: BacklogInputContext,
    failure_stage: str,
    details: _FailureDetails,
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

    return {
        "success": False,
        "input_context": input_context,
        "output_artifact": artifact,
        "is_complete": None,
        "error": message,
        **metadata,
    }


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

    try:
        raw_text: str = await _invoke_backlog_agent(payload)
    except (AgentInvocationError, ValueError) as exc:
        raw_output = (
            exc.partial_output if isinstance(exc, AgentInvocationError) else None
        )
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage="invocation_exception",
            details=_FailureDetails(
                message=f"Backlog runtime failed: {exc}",
                raw_text=raw_output,
                exception=exc,
            ),
        )

    parsed: dict[str, Any] | None = parse_json_payload(raw_text)
    if parsed is None:
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage="invalid_json",
            details=_FailureDetails(
                message="Backlog response is not valid JSON",
                raw_text=raw_text,
            ),
        )

    try:
        output_model: OutputSchema = OutputSchema.model_validate(parsed)
    except ValidationError as exc:
        return _failure(
            project_id=project_id,
            input_context=input_context,
            failure_stage="output_validation",
            details=_FailureDetails(
                message=f"Backlog output validation failed: {exc}",
                raw_text=raw_text,
                validation_errors=_normalize_validation_errors(exc.errors()),
                exception=exc,
            ),
        )

    try:
        _validate_brownfield_contract(
            output_model=output_model,
            input_context=input_context,
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
            failure_stage="brownfield_contract_validation",
            details=_FailureDetails(
                message=f"Backlog brownfield contract validation failed: {exc}",
                raw_text=raw_text,
                validation_errors=validation_errors,
                exception=exc,
            ),
        )

    output_artifact: dict[str, Any] = output_model.model_dump(exclude_none=True)
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
