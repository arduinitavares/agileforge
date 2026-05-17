"""Public helpers for spec-backed story validation support."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, TypedDict, Unpack, cast

from pydantic import BaseModel, Field, ValidationError
from sqlmodel import Session, select

from models.core import Feature, UserStory
from models.db import get_engine
from models.specs import CompiledSpecAuthority, SpecRegistry
from orchestrator_agent.agent_tools.spec_validator_agent.agent import (
    root_agent as spec_validator_agent,
)
from orchestrator_agent.agent_tools.spec_validator_agent.schemes import (
    SpecValidationResult,
)
from services.specs._engine_resolution import resolve_spec_engine
from services.specs.compiler_service import load_compiled_artifact
from utils.adk_runner import invoke_agent_to_text
from utils.failure_artifacts import AgentInvocationError
from utils.runtime_config import SPEC_VALIDATOR_IDENTITY, get_default_validation_mode
from utils.spec_schemas import (
    AlignmentFinding,
    Invariant,
    InvariantType,
    SpecAuthorityCompilationSuccess,
    ValidationEvidence,
    ValidationFailure,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from sqlalchemy.engine import Connection, Engine

logger: logging.Logger = logging.getLogger(name=__name__)
_DEFAULT_GET_ENGINE = get_engine

DEFAULT_VALIDATION_MODE_ENV = "SPEC_VALIDATION_DEFAULT_MODE"
_VALIDATION_MODES = {"deterministic", "llm", "hybrid"}


class LlmValidationResult(TypedDict):
    """Normalized result shape returned by the LLM validation adapter."""

    passed: bool
    issues: list[str]
    suggestions: list[str]
    verdict: str
    critical_gaps: list[str]


class _RunLlmSpecValidationOptions(TypedDict, total=False):
    """Optional dependency-injection hooks for LLM validation."""

    invoke_spec_validator_async_fn: Callable[[str], Coroutine[Any, Any, str]]
    parse_llm_validator_response_fn: Callable[[str], LlmValidationResult]


class _ValidateStoryOptions(TypedDict, total=False):
    """Optional dependency-injection hooks for story validation."""

    tool_context: object | None
    resolve_default_validation_mode: Callable[[], str]
    compute_story_input_hash_fn: Callable[[object], str]
    persist_validation_evidence: Callable[
        [Session, UserStory, ValidationEvidence, bool],
        None,
    ]
    run_structural_story_checks: Callable[
        [UserStory],
        tuple[list[str], list[ValidationFailure], list[str]],
    ]
    run_deterministic_alignment_checks: Callable[
        [UserStory, CompiledSpecAuthority],
        tuple[list[AlignmentFinding], list[AlignmentFinding], list[str]],
    ]
    run_llm_spec_validation: Callable[
        [
            UserStory,
            CompiledSpecAuthority,
            object | None,
            Feature | None,
        ],
        LlmValidationResult,
    ]
    load_compiled_artifact_fn: Callable[
        [CompiledSpecAuthority],
        SpecAuthorityCompilationSuccess | None,
    ]
    render_invariant_summary_fn: Callable[[Invariant], str]
    validator_version: str


class _ValidationDependencies(TypedDict):
    """Resolved helper functions and constants for one validation run."""

    resolve_default_mode: Callable[[], str]
    compute_input_hash: Callable[[object], str]
    persist_evidence: Callable[
        [Session, UserStory, ValidationEvidence, bool],
        None,
    ]
    structural_checks: Callable[
        [UserStory],
        tuple[list[str], list[ValidationFailure], list[str]],
    ]
    deterministic_checks: Callable[
        [UserStory, CompiledSpecAuthority],
        tuple[list[AlignmentFinding], list[AlignmentFinding], list[str]],
    ]
    llm_validation: Callable[
        [
            UserStory,
            CompiledSpecAuthority,
            object | None,
            Feature | None,
        ],
        LlmValidationResult,
    ]
    load_artifact: Callable[
        [CompiledSpecAuthority],
        SpecAuthorityCompilationSuccess | None,
    ]
    render_invariant: Callable[[Invariant], str]
    validator_version: str


class LlmValidatorResponseParseError(ValueError):
    """Raised when an LLM validator response cannot be parsed or recovered."""

    @classmethod
    def unable_to_parse(cls) -> LlmValidatorResponseParseError:
        """Build the canonical parse-failure error."""
        return cls("Unable to parse LLM validator response")

    @classmethod
    def unable_to_recover_non_compliant(
        cls,
    ) -> LlmValidatorResponseParseError:
        """Build the canonical non-compliant recovery error."""
        return cls("Unable to recover non-compliant LLM validator response")


@dataclass(frozen=True)
class _FailedValidationContext:
    """Context required to persist an early validation failure."""

    session: Session
    story: UserStory
    spec_version_id: int
    input_hash: str
    persist_evidence: Callable[[Session, UserStory, ValidationEvidence, bool], None]
    validator_version: str


@dataclass(frozen=True)
class _FailedValidationDetails:
    """Structured details describing one canonical validation failure."""

    rule: str
    expected: str
    actual: str
    message: str
    error: str


@dataclass
class _ValidationCollector:
    """Mutable lists used while accumulating validation findings."""

    failures: list[ValidationFailure]
    warnings: list[str]
    alignment_failures: list[AlignmentFinding]
    alignment_warnings: list[AlignmentFinding]


@dataclass(frozen=True)
class _LlmValidationContext:
    """Inputs required to execute the LLM validation step."""

    session: Session
    story: UserStory
    authority: CompiledSpecAuthority
    artifact: SpecAuthorityCompilationSuccess | None
    llm_validation: Callable[
        [
            UserStory,
            CompiledSpecAuthority,
            object | None,
            Feature | None,
        ],
        LlmValidationResult,
    ]


class ValidateStoryInput(BaseModel):
    """Input schema for validate_story_with_spec_authority service."""

    story_id: int = Field(description="Story ID to validate")
    spec_version_id: int = Field(
        description="Spec version ID to validate against (REQUIRED)"
    )
    mode: Literal["deterministic", "llm", "hybrid"] = Field(
        default="deterministic",
        description=(
            "Validation mode: deterministic (rule-based), llm (spec_validator_agent), "
            "or hybrid (both)."
        ),
    )


def compute_story_input_hash(story: object) -> str:
    """Compute deterministic SHA-256 hash of story content."""
    content = json.dumps(
        {
            "title": getattr(story, "title", "") or "",
            "description": getattr(story, "story_description", "") or "",
            "acceptance_criteria": getattr(story, "acceptance_criteria", "") or "",
        },
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(content.encode()).hexdigest()


def resolve_default_validation_mode() -> str:
    """Resolve default validation mode from environment with safe fallback."""
    raw_value = get_default_validation_mode("deterministic").strip().lower()
    if raw_value in _VALIDATION_MODES:
        return raw_value
    logger.warning(
        "Invalid %s=%r; falling back to 'deterministic'",
        DEFAULT_VALIDATION_MODE_ENV,
        raw_value,
    )
    return "deterministic"


def persist_validation_evidence(
    session: Session,
    story: UserStory,
    evidence: ValidationEvidence,
    passed: bool,
) -> None:
    """Persist validation evidence and update accepted spec version on pass."""
    story.validation_evidence = evidence.model_dump_json()
    if passed:
        story.accepted_spec_version_id = evidence.spec_version_id
    session.add(story)
    session.commit()


def _run_async_task[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run an async coroutine from sync code, even if a loop is already running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(asyncio.run, coro)
        return cast("T", future.result())


def _resolve_engine() -> Engine | Connection | None:
    """Preserve the legacy spec_tools.engine monkeypatch seam for tests."""
    return cast(
        "Engine | Connection | None",
        resolve_spec_engine(
            service_get_engine=get_engine,
            default_service_get_engine=_DEFAULT_GET_ENGINE,
        ),
    )


def _render_invariant_summary(invariant: Invariant) -> str:
    """Render a structured invariant into a stable string for consumers."""
    if invariant.type == InvariantType.FORBIDDEN_CAPABILITY:
        capability = getattr(invariant.parameters, "capability", "")
        return f"FORBIDDEN_CAPABILITY:{capability}"
    if invariant.type == InvariantType.REQUIRED_FIELD:
        field_name = getattr(invariant.parameters, "field_name", "")
        return f"REQUIRED_FIELD:{field_name}"
    if invariant.type == InvariantType.MAX_VALUE:
        field_name = getattr(invariant.parameters, "field_name", "")
        max_value = getattr(invariant.parameters, "max_value", "")
        return f"MAX_VALUE:{field_name}<= {max_value}"
    if invariant.type == InvariantType.RELATION_CONSTRAINT:
        expression = getattr(invariant.parameters, "expression", "")
        return f"RELATION_CONSTRAINT:{expression}"
    return f"INVARIANT:{invariant.type}"


def render_invariant_summary(invariant: Invariant) -> str:
    """Public helper for invariant rendering used by legacy adapters."""
    return _render_invariant_summary(invariant)


def _split_story_segments(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", (text or "").strip())
    if not normalized:
        return []
    parts = re.split(r"(?<=[.!?;:])\s+|\n+", normalized)
    return [part.strip() for part in parts if part.strip()]


def _build_capability_pattern(capability: str) -> re.Pattern[str] | None:
    tokens = [
        re.escape(token)
        for token in re.split(r"[\s_]+", capability.strip().lower())
        if token
    ]
    if not tokens:
        return None
    return re.compile(r"\b" + r"[\s_-]+".join(tokens) + r"\b", flags=re.IGNORECASE)


def _is_policy_only_capability_context(segment: str) -> bool:
    policy_context_patterns = (
        re.compile(r"\bplagiarism policy\b", flags=re.IGNORECASE),
        re.compile(r"\bacademic integrity\b", flags=re.IGNORECASE),
        re.compile(r"\bcitation\b", flags=re.IGNORECASE),
        re.compile(r"\bappropriate(?:ly)? cited?\b", flags=re.IGNORECASE),
        re.compile(r"\bwithout appropriate citation\b", flags=re.IGNORECASE),
        re.compile(r"\brubric\b", flags=re.IGNORECASE),
        re.compile(r"\bgrading\b", flags=re.IGNORECASE),
        re.compile(r"\bsubmission instructions?\b", flags=re.IGNORECASE),
        re.compile(r"\bsubmission requirements?\b", flags=re.IGNORECASE),
    )
    integrity_enforcement_patterns = (
        re.compile(r"\bdetect(?:ion)?\b", flags=re.IGNORECASE),
        re.compile(r"\bchecker\b", flags=re.IGNORECASE),
        re.compile(r"\bscan(?:ning)?\b", flags=re.IGNORECASE),
        re.compile(r"\bflag\b", flags=re.IGNORECASE),
        re.compile(r"\bprevent\b", flags=re.IGNORECASE),
        re.compile(r"\bblock\b", flags=re.IGNORECASE),
        re.compile(r"\benforce\b", flags=re.IGNORECASE),
        re.compile(r"\bmonitor(?:ing)?\b", flags=re.IGNORECASE),
        re.compile(r"\bverify\b", flags=re.IGNORECASE),
        re.compile(r"\bscore\b", flags=re.IGNORECASE),
        re.compile(r"\bcompare\b", flags=re.IGNORECASE),
    )
    if not segment:
        return False
    if not any(pattern.search(segment) for pattern in policy_context_patterns):
        return False
    return not any(
        pattern.search(segment) for pattern in integrity_enforcement_patterns
    )


def _story_mentions_forbidden_capability(
    *,
    story_segments: list[str],
    combined_text: str,
    capability: str,
) -> bool:
    pattern = _build_capability_pattern(capability)
    if pattern is None:
        return False

    if not pattern.search(combined_text):
        return False

    for segment in story_segments:
        if not pattern.search(segment):
            continue
        if _is_policy_only_capability_context(segment):
            continue
        return True

    return False


def run_structural_story_checks(
    story: UserStory,
) -> tuple[list[str], list[ValidationFailure], list[str]]:
    """Run deterministic structural story checks used by all validation modes."""
    rules_checked: list[str] = []
    failures: list[ValidationFailure] = []
    warnings: list[str] = []

    rules_checked.append("RULE_TITLE_REQUIRED")
    if not story.title or not story.title.strip():
        failures.append(
            ValidationFailure(
                rule="RULE_TITLE_REQUIRED",
                expected="Non-empty title",
                actual="Empty or missing",
                message="Story must have a title",
            )
        )

    rules_checked.append("RULE_ACCEPTANCE_CRITERIA_REQUIRED")
    if not story.acceptance_criteria or not story.acceptance_criteria.strip():
        failures.append(
            ValidationFailure(
                rule="RULE_ACCEPTANCE_CRITERIA_REQUIRED",
                expected="Non-empty acceptance criteria",
                actual="Empty or missing",
                message="Story must have acceptance criteria",
            )
        )

    rules_checked.append("RULE_PERSONA_FORMAT")
    title_lower = (story.title or "").lower()
    desc_lower = (story.story_description or "").lower()
    acceptance_lower = (story.acceptance_criteria or "").lower()
    if not (
        "as a " in title_lower
        or "as a " in desc_lower
        or "as an " in title_lower
        or "as an " in desc_lower
    ):
        warnings.append("Story does not follow 'As a [persona], I want...' format")

    rules_checked.append("RULE_CONTRADICTORY_CONNECTIVITY_REQUIREMENTS")
    combined_text = " ".join(
        part for part in [title_lower, desc_lower, acceptance_lower] if part
    )
    if "offline" in combined_text and "cloud sync" in combined_text:
        failures.append(
            ValidationFailure(
                rule="RULE_CONTRADICTORY_CONNECTIVITY_REQUIREMENTS",
                expected="Connectivity requirements are internally consistent",
                actual=(
                    "Story requires both offline operation and cloud sync dependency"
                ),
                message=(
                    "Story contains contradictory connectivity requirements "
                    "(offline operation vs cloud sync dependency)"
                ),
            )
        )

    rules_checked.append("RULE_IMPOSSIBLE_LATENCY_REQUIREMENT")
    if (
        re.search(
            r"\b(?:under|below|less than|<=?|at most)\s*0\s*ms\b",
            acceptance_lower,
        )
        or "0ms (impossible)" in acceptance_lower
    ):
        failures.append(
            ValidationFailure(
                rule="RULE_IMPOSSIBLE_LATENCY_REQUIREMENT",
                expected="Latency constraints are physically plausible",
                actual="Latency constraint requires <= 0ms",
                message="Story defines an impossible latency requirement (<= 0ms)",
            )
        )

    rules_checked.append("RULE_ACCEPTANCE_CRITERIA_SCOPE_MISMATCH")
    normalized_acceptance = " ".join(acceptance_lower.split())
    if (
        "out of scope feature request." in desc_lower
        and normalized_acceptance.startswith("given item, when add, then in cart")
    ):
        failures.append(
            ValidationFailure(
                rule="RULE_ACCEPTANCE_CRITERIA_SCOPE_MISMATCH",
                expected="Acceptance criteria align with story scope",
                actual=(
                    "Story scope and acceptance criteria describe different domains"
                ),
                message=(
                    "Acceptance criteria appear to be copied from an unrelated scope"
                ),
            )
        )

    return rules_checked, failures, warnings


def run_deterministic_alignment_checks(
    story: UserStory,
    authority: CompiledSpecAuthority,
    *,
    load_compiled_artifact_fn: Callable[
        [CompiledSpecAuthority], Any | None
    ] = load_compiled_artifact,
) -> tuple[list[AlignmentFinding], list[AlignmentFinding], list[str]]:
    """Run deterministic alignment checks against compiled authority."""
    alignment_failures: list[AlignmentFinding] = []
    alignment_warnings: list[AlignmentFinding] = []
    warnings: list[str] = []

    artifact = (
        load_compiled_artifact_fn(authority)
        if callable(load_compiled_artifact_fn)
        else None
    )
    if not artifact or not getattr(artifact, "invariants", None):
        return alignment_failures, alignment_warnings, warnings

    title_text = (story.title or "").lower()
    description_text = (story.story_description or "").lower()
    acceptance_text = (story.acceptance_criteria or "").lower()
    combined_text = " ".join(
        part for part in [title_text, description_text, acceptance_text] if part
    )
    normalized_acceptance = acceptance_text.replace("_", " ")
    story_segments = [
        segment
        for part in [
            story.title or "",
            story.story_description or "",
            story.acceptance_criteria or "",
        ]
        for segment in _split_story_segments(part)
    ]

    for invariant in artifact.invariants:
        if invariant.type == InvariantType.FORBIDDEN_CAPABILITY:
            capability = str(
                getattr(invariant.parameters, "capability", "") or ""
            ).strip()
            if not capability:
                continue
            if _story_mentions_forbidden_capability(
                story_segments=story_segments,
                combined_text=combined_text,
                capability=capability,
            ):
                alignment_failures.append(
                    AlignmentFinding(
                        code="FORBIDDEN_CAPABILITY",
                        invariant=invariant.id,
                        capability=capability,
                        message=(
                            f"Story references forbidden capability '{capability}' "
                            f"(invariant {invariant.id})."
                        ),
                        severity="failure",
                        created_at=datetime.now(UTC),
                    )
                )
            continue

        if invariant.type == InvariantType.REQUIRED_FIELD:
            field_name = str(
                getattr(invariant.parameters, "field_name", "") or ""
            ).strip()
            if not field_name:
                continue
            field_lower = field_name.lower()
            field_variants = {
                field_lower,
                field_lower.replace("_", " "),
            }
            has_field_mention = any(
                variant
                and (variant in acceptance_text or variant in normalized_acceptance)
                for variant in field_variants
            )
            if not has_field_mention:
                alignment_warnings.append(
                    AlignmentFinding(
                        code="REQUIRED_FIELD_MISSING",
                        invariant=invariant.id,
                        capability=None,
                        message=(
                            f"Acceptance criteria may be missing required field "
                            f"'{field_name}' (invariant {invariant.id})."
                        ),
                        severity="warning",
                        created_at=datetime.now(UTC),
                    )
                )
            continue

        if invariant.type == InvariantType.MAX_VALUE:
            continue

    return alignment_failures, alignment_warnings, warnings


async def invoke_spec_validator_async(payload_text: str) -> str:
    """Invoke spec_validator_agent and return response text."""
    return await invoke_agent_to_text(
        agent=spec_validator_agent,
        runner_identity=SPEC_VALIDATOR_IDENTITY,
        payload_json=payload_text,
        no_text_error="Spec validator agent returned no text response",
    )


def _build_llm_validation_result(
    *,
    passed: bool,
    issues: list[str],
    suggestions: list[str],
    verdict: str,
    critical_gaps: list[str],
) -> LlmValidationResult:
    """Build a normalized LLM validation result payload."""
    return {
        "passed": passed,
        "issues": issues,
        "suggestions": suggestions,
        "verdict": verdict,
        "critical_gaps": critical_gaps,
    }


def _strip_json_fence(raw_text: str) -> str:
    """Strip optional fenced-code markers from an LLM JSON response."""
    candidate = raw_text.strip()
    if not candidate.startswith("```"):
        return candidate
    candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
    return re.sub(r"\s*```$", "", candidate)


def _extract_string_list_from_partial_response(
    candidate: str,
    field_name: str,
) -> list[str]:
    """Recover a list-of-strings field from truncated JSON text."""
    pattern = rf'"{re.escape(field_name)}"\s*:\s*\[(.*?)(?:\]|$)'
    list_match = re.search(pattern, candidate, flags=re.DOTALL)
    if not list_match:
        return []

    raw_items = re.findall(r'"((?:\\.|[^"\\])*)"', list_match.group(1))
    values: list[str] = []
    for raw_item in raw_items:
        try:
            values.append(json.loads(f'"{raw_item}"'))
        except json.JSONDecodeError:
            values.append(raw_item)
    return values


def _extract_string_from_partial_response(
    candidate: str,
    field_name: str,
) -> str | None:
    """Recover a string field from truncated JSON text."""
    pattern = rf'"{re.escape(field_name)}"\s*:\s*"((?:\\.|[^"\\])*)"'
    value_match = re.search(pattern, candidate, flags=re.DOTALL)
    if not value_match:
        return None

    try:
        return json.loads(f'"{value_match.group(1)}"')
    except json.JSONDecodeError:
        return value_match.group(1)


def _recover_llm_validator_response(
    candidate: str,
    exc: ValidationError,
) -> LlmValidationResult:
    """Recover a normalized result from truncated LLM JSON."""
    compliant_match = re.search(
        r'"is_compliant"\s*:\s*(true|false)',
        candidate,
        flags=re.IGNORECASE,
    )
    if not compliant_match:
        raise LlmValidatorResponseParseError.unable_to_parse() from exc

    is_compliant = compliant_match.group(1).lower() == "true"
    issues = _extract_string_list_from_partial_response(candidate, "issues")
    critical_gaps = _extract_string_list_from_partial_response(
        candidate,
        "critical_gaps",
    )
    suggestions = _extract_string_list_from_partial_response(
        candidate,
        "suggestions",
    )
    verdict = _extract_string_from_partial_response(candidate, "verdict")
    verdict = verdict or "Recovered from truncated JSON response"

    if is_compliant:
        issues = []
        critical_gaps = []
        suggestions = []
    elif not issues and critical_gaps:
        issues = list(critical_gaps)

    if not is_compliant and not issues:
        raise LlmValidatorResponseParseError.unable_to_recover_non_compliant() from exc

    logger.warning("Recovered partial LLM response (truncated JSON)")
    return _build_llm_validation_result(
        passed=is_compliant,
        issues=issues,
        suggestions=suggestions,
        verdict=verdict,
        critical_gaps=critical_gaps,
    )


def parse_llm_validator_response(raw_text: str) -> LlmValidationResult:
    """Parse agent text into the normalized LLM validation result shape."""
    candidate = _strip_json_fence(raw_text)
    try:
        parsed = SpecValidationResult.model_validate_json(candidate)
        critical_gaps = (
            list(parsed.domain_compliance.critical_gaps)
            if parsed.domain_compliance and parsed.domain_compliance.critical_gaps
            else []
        )
        return _build_llm_validation_result(
            passed=parsed.is_compliant,
            issues=list(parsed.issues),
            suggestions=list(parsed.suggestions),
            verdict=parsed.verdict,
            critical_gaps=critical_gaps,
        )
    except ValidationError as exc:
        return _recover_llm_validator_response(candidate, exc)


def run_llm_spec_validation(
    story: UserStory,
    authority: CompiledSpecAuthority,
    artifact: object | None,
    feature: Feature | None = None,
    **options: Unpack[_RunLlmSpecValidationOptions],
) -> LlmValidationResult:
    """Run LLM-based spec validation and normalize result."""
    invoke_async = (
        options.get("invoke_spec_validator_async_fn") or invoke_spec_validator_async
    )
    parse_response = (
        options.get("parse_llm_validator_response_fn") or parse_llm_validator_response
    )

    authority_json = authority.compiled_artifact_json or ""
    if artifact is not None and hasattr(artifact, "model_dump_json"):
        authority_json = cast("Any", artifact).model_dump_json()

    payload = {
        "story_title": story.title or "",
        "story_description": story.story_description or "",
        "acceptance_criteria": story.acceptance_criteria or "",
        "compiled_authority_json": authority_json,
        "spec_version_id": authority.spec_version_id,
        "feature_title": feature.title if feature else None,
        "feature_description": feature.description if feature else None,
    }
    raw_text = _run_async_task(invoke_async(json.dumps(payload)))
    return parse_response(raw_text)


def _normalize_validate_story_params(
    params: dict[str, Any] | ValidateStoryInput,
) -> dict[str, Any]:
    """Normalize validation params from either a model or a raw mapping."""
    if isinstance(params, ValidateStoryInput):
        return params.model_dump()
    return dict(params or {})


def _resolve_validation_dependencies(
    options: _ValidateStoryOptions,
) -> _ValidationDependencies:
    """Resolve injectable helpers while preserving service-owned defaults."""
    resolve_default_mode = (
        options.get("resolve_default_validation_mode")
        or globals()["resolve_default_validation_mode"]
    )
    compute_input_hash = (
        options.get("compute_story_input_hash_fn") or compute_story_input_hash
    )
    persist_evidence = (
        options.get("persist_validation_evidence")
        or globals()["persist_validation_evidence"]
    )
    structural_checks = (
        options.get("run_structural_story_checks")
        or globals()["run_structural_story_checks"]
    )
    llm_validation = options.get("run_llm_spec_validation") or run_llm_spec_validation
    load_artifact = options.get("load_compiled_artifact_fn") or load_compiled_artifact
    render_invariant = (
        options.get("render_invariant_summary_fn") or render_invariant_summary
    )

    injected_deterministic_checks = options.get("run_deterministic_alignment_checks")
    if injected_deterministic_checks is None:

        def deterministic_checks(
            story: UserStory,
            authority: CompiledSpecAuthority,
        ) -> tuple[list[AlignmentFinding], list[AlignmentFinding], list[str]]:
            return run_deterministic_alignment_checks(
                story,
                authority,
                load_compiled_artifact_fn=load_artifact,
            )

    else:
        deterministic_checks = injected_deterministic_checks

    return {
        "resolve_default_mode": resolve_default_mode,
        "compute_input_hash": compute_input_hash,
        "persist_evidence": persist_evidence,
        "structural_checks": structural_checks,
        "deterministic_checks": deterministic_checks,
        "llm_validation": llm_validation,
        "load_artifact": load_artifact,
        "render_invariant": render_invariant,
        "validator_version": options.get("validator_version", "1.0.0"),
    }


def _build_failed_validation_result(
    context: _FailedValidationContext,
    details: _FailedValidationDetails,
) -> dict[str, Any]:
    """Persist and return a canonical failed validation result."""
    evidence = ValidationEvidence(
        spec_version_id=context.spec_version_id,
        validated_at=datetime.now(UTC),
        passed=False,
        rules_checked=[details.rule],
        invariants_checked=[],
        evaluated_invariant_ids=[],
        finding_invariant_ids=[],
        failures=[
            ValidationFailure(
                rule=details.rule,
                expected=details.expected,
                actual=details.actual,
                message=details.message,
            )
        ],
        warnings=[],
        alignment_warnings=[],
        alignment_failures=[],
        validator_version=context.validator_version,
        input_hash=context.input_hash,
    )
    context.persist_evidence(context.session, context.story, evidence, False)
    return {
        "success": False,
        "error": details.error,
        "passed": False,
        "input_hash": context.input_hash,
    }


def _load_feature_for_story(session: Session, story: UserStory) -> Feature | None:
    """Load the story's feature when one is linked."""
    if story.feature_id is None:
        return None
    return session.get(Feature, story.feature_id)


def _build_invariants_checked(
    artifact: SpecAuthorityCompilationSuccess | None,
    render_invariant: Callable[[Invariant], str],
) -> list[str]:
    """Render the subset of invariants surfaced in validation evidence."""
    if not artifact or not artifact.invariants:
        return []
    return [
        render_invariant(inv)
        for inv in artifact.invariants
        if inv.type in ("FORBIDDEN_CAPABILITY", "REQUIRED_FIELD")
    ]


def _append_no_invariants_warning(
    collector: _ValidationCollector,
) -> None:
    """Record the standard no-invariants advisory message."""
    no_invariants_message = (
        "Compiled authority has no invariants; alignment checks are informational only."
    )
    collector.warnings.append(no_invariants_message)
    collector.alignment_warnings.append(
        AlignmentFinding(
            code="NO_INVARIANTS",
            invariant=None,
            capability=None,
            message=no_invariants_message,
            severity="warning",
            created_at=datetime.now(UTC),
        )
    )


def _apply_llm_validation_result(
    llm_result: LlmValidationResult,
    collector: _ValidationCollector,
) -> None:
    """Translate an LLM validation result into persisted findings and warnings."""
    llm_issues = list(llm_result.get("issues", []))
    llm_critical_gaps = list(llm_result.get("critical_gaps", []))

    if not llm_result.get("passed", False) and not llm_critical_gaps and not llm_issues:
        verdict = llm_result.get("verdict")
        if verdict:
            llm_issues = [verdict]

    for issue in llm_issues:
        collector.warnings.append(f"LLM advisory: {issue}")
        collector.alignment_warnings.append(
            AlignmentFinding(
                code="LLM_SPEC_VALIDATION_ISSUE",
                invariant=None,
                capability=None,
                message=issue,
                severity="warning",
                created_at=datetime.now(UTC),
            )
        )

    for gap in llm_critical_gaps:
        collector.failures.append(
            ValidationFailure(
                rule="RULE_LLM_SPEC_VALIDATION",
                expected="Spec-compliant story",
                actual=gap,
                message=gap,
            )
        )
        collector.alignment_failures.append(
            AlignmentFinding(
                code="LLM_SPEC_VALIDATION",
                invariant=None,
                capability=None,
                message=gap,
                severity="failure",
                created_at=datetime.now(UTC),
            )
        )

    for suggestion in llm_result.get("suggestions", []):
        collector.warnings.append(f"LLM suggestion: {suggestion}")
        collector.alignment_warnings.append(
            AlignmentFinding(
                code="LLM_SPEC_VALIDATION_SUGGESTION",
                invariant=None,
                capability=None,
                message=suggestion,
                severity="warning",
                created_at=datetime.now(UTC),
            )
        )


def _run_llm_validation_for_story(
    context: _LlmValidationContext,
    collector: _ValidationCollector,
) -> None:
    """Execute the LLM validation step and fold its output into evidence lists."""
    try:
        llm_result = context.llm_validation(
            context.story,
            context.authority,
            context.artifact,
            _load_feature_for_story(context.session, context.story),
        )
    except (
        AgentInvocationError,
        ValidationError,
        RuntimeError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        collector.failures.append(
            ValidationFailure(
                rule="RULE_LLM_SPEC_VALIDATION",
                expected="LLM validator completes successfully",
                actual=str(exc),
                message="LLM validation execution failed",
            )
        )
        collector.alignment_failures.append(
            AlignmentFinding(
                code="LLM_SPEC_VALIDATION_ERROR",
                invariant=None,
                capability=None,
                message=f"LLM validation execution failed: {exc}",
                severity="failure",
                created_at=datetime.now(UTC),
            )
        )
        return

    _apply_llm_validation_result(llm_result, collector)


def _collect_evaluated_invariant_ids(
    artifact: SpecAuthorityCompilationSuccess | None,
) -> list[str]:
    """Collect invariant IDs that were deterministically evaluated."""
    if not artifact or not artifact.invariants:
        return []
    return [
        inv.id
        for inv in artifact.invariants
        if inv.type in ("FORBIDDEN_CAPABILITY", "REQUIRED_FIELD")
    ]


def _collect_finding_invariant_ids(
    alignment_failures: list[AlignmentFinding],
    alignment_warnings: list[AlignmentFinding],
) -> list[str]:
    """Collect unique invariant IDs referenced by generated findings."""
    finding_invariant_ids: list[str] = []
    for finding in alignment_failures + alignment_warnings:
        if finding.invariant and finding.invariant not in finding_invariant_ids:
            finding_invariant_ids.append(finding.invariant)
    return finding_invariant_ids


def validate_story_with_spec_authority(
    params: dict[str, Any] | ValidateStoryInput,
    **options: Unpack[_ValidateStoryOptions],
) -> dict[str, Any]:
    """Validate a story against an explicit spec version."""
    dependencies = _resolve_validation_dependencies(options)
    raw_params = _normalize_validate_story_params(params)
    if "mode" not in raw_params:
        raw_params["mode"] = dependencies["resolve_default_mode"]()
    parsed = ValidateStoryInput.model_validate(raw_params)

    with Session(_resolve_engine()) as session:
        story = session.get(UserStory, parsed.story_id)
        if not story:
            return {
                "success": False,
                "error": f"Story {parsed.story_id} not found",
            }

        input_hash = dependencies["compute_input_hash"](story)
        failure_context = _FailedValidationContext(
            session=session,
            story=story,
            spec_version_id=parsed.spec_version_id,
            input_hash=input_hash,
            persist_evidence=dependencies["persist_evidence"],
            validator_version=dependencies["validator_version"],
        )

        spec_version = session.get(SpecRegistry, parsed.spec_version_id)
        if not spec_version:
            missing_spec_message = f"Spec version {parsed.spec_version_id} not found"
            return _build_failed_validation_result(
                failure_context,
                _FailedValidationDetails(
                    rule="SPEC_VERSION_EXISTS",
                    expected="Spec version exists",
                    actual="Not found",
                    message=missing_spec_message,
                    error=missing_spec_message,
                ),
            )

        if spec_version.product_id != story.product_id:
            product_match_message = (
                "Spec version belongs to a different product "
                f"(expected {story.product_id}, got {spec_version.product_id})"
            )
            return _build_failed_validation_result(
                failure_context,
                _FailedValidationDetails(
                    rule="SPEC_PRODUCT_MATCH",
                    expected=f"Product {story.product_id}",
                    actual=f"Product {spec_version.product_id}",
                    message=product_match_message,
                    error=(
                        "Product mismatch: story belongs to product "
                        f"{story.product_id}, "
                        f"but spec version {parsed.spec_version_id} belongs to "
                        f"product {spec_version.product_id}"
                    ),
                ),
            )

        authority = session.exec(
            select(CompiledSpecAuthority).where(
                CompiledSpecAuthority.spec_version_id == parsed.spec_version_id
            )
        ).first()

        if not authority:
            not_compiled_message = (
                f"spec_version_id {parsed.spec_version_id} is not compiled"
            )
            return _build_failed_validation_result(
                failure_context,
                _FailedValidationDetails(
                    rule="SPEC_VERSION_COMPILED",
                    expected="Compiled authority exists",
                    actual="Not compiled",
                    message=not_compiled_message,
                    error=not_compiled_message,
                ),
            )

        artifact = dependencies["load_artifact"](authority)
        invariants_checked = _build_invariants_checked(
            artifact,
            dependencies["render_invariant"],
        )

        rules_checked, failures, warnings = dependencies["structural_checks"](story)

        collector = _ValidationCollector(
            failures=failures,
            warnings=warnings,
            alignment_failures=[],
            alignment_warnings=[],
        )
        if not invariants_checked:
            _append_no_invariants_warning(collector)

        if parsed.mode in ("deterministic", "hybrid"):
            (
                deterministic_failures,
                deterministic_warnings,
                deterministic_messages,
            ) = dependencies["deterministic_checks"](story, authority)
            collector.alignment_failures.extend(deterministic_failures)
            collector.alignment_warnings.extend(deterministic_warnings)
            collector.warnings.extend(deterministic_messages)

        if parsed.mode in ("llm", "hybrid"):
            rules_checked.append("RULE_LLM_SPEC_VALIDATION")
            _run_llm_validation_for_story(
                _LlmValidationContext(
                    session=session,
                    story=story,
                    authority=authority,
                    artifact=artifact,
                    llm_validation=dependencies["llm_validation"],
                ),
                collector,
            )

        passed = len(collector.failures) == 0 and not collector.alignment_failures

        evaluated_invariant_ids = _collect_evaluated_invariant_ids(artifact)
        finding_invariant_ids = _collect_finding_invariant_ids(
            collector.alignment_failures,
            collector.alignment_warnings,
        )

        evidence = ValidationEvidence(
            spec_version_id=parsed.spec_version_id,
            validated_at=datetime.now(UTC),
            passed=passed,
            rules_checked=rules_checked,
            invariants_checked=invariants_checked,
            evaluated_invariant_ids=evaluated_invariant_ids,
            finding_invariant_ids=finding_invariant_ids,
            failures=collector.failures,
            warnings=collector.warnings,
            alignment_warnings=collector.alignment_warnings,
            alignment_failures=collector.alignment_failures,
            validator_version=dependencies["validator_version"],
            input_hash=input_hash,
        )
        dependencies["persist_evidence"](session, story, evidence, passed)

        return {
            "success": True,
            "passed": passed,
            "story_id": parsed.story_id,
            "spec_version_id": parsed.spec_version_id,
            "mode": parsed.mode,
            "failures": [failure.model_dump() for failure in collector.failures],
            "alignment_failures": [
                finding.model_dump(mode="json")
                for finding in collector.alignment_failures
            ],
            "alignment_warnings": [
                finding.model_dump(mode="json")
                for finding in collector.alignment_warnings
            ],
            "warnings": collector.warnings,
            "input_hash": input_hash,
            "message": (
                "Validation passed"
                if passed
                else f"Validation failed with {len(collector.failures)} issue(s)"
            ),
        }


__all__ = [
    "ValidateStoryInput",
    "compute_story_input_hash",
    "persist_validation_evidence",
    "render_invariant_summary",
    "resolve_default_validation_mode",
    "run_deterministic_alignment_checks",
    "run_structural_story_checks",
    "validate_story_with_spec_authority",
]
