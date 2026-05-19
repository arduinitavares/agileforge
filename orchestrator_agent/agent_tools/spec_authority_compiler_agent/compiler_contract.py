"""Deterministic helpers for spec authority compilation contract."""

from __future__ import annotations

import hashlib
import re

from utils.spec_schemas import (
    ForbiddenCapabilityParams,
    Invariant,
    InvariantParameters,
    InvariantType,
    MaxValueParams,
    RelationConstraintParams,
    RequiredFieldParams,
)


def _normalize_text(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _normalize_token(token: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_\-\s]", "", token)
    collapsed = " ".join(cleaned.strip().lower().split())
    return collapsed.replace(" ", "_")


def compute_prompt_hash(prompt_text: str) -> str:
    """Compute SHA-256 hash of the prompt/instructions."""
    normalized = _normalize_text(prompt_text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def compute_spec_hash(spec_content: str) -> str:
    """Compute SHA-256 hash of spec content (normalized)."""
    normalized = _normalize_text(spec_content)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _canonical_parameter_seed(parameters: InvariantParameters | None) -> str:
    """Return a stable parameter seed for invariant ID uniqueness."""
    if parameters is None:
        return ""
    dumped = parameters.model_dump(mode="json")
    parts = [f"{key}={dumped[key]}" for key in sorted(dumped)]
    return "|".join(parts)


def compute_invariant_id(
    excerpt: str,
    invariant_type: InvariantType,
    parameters: InvariantParameters | None = None,
) -> str:
    """Compute deterministic invariant ID from excerpt, type, and parameters."""
    normalized_excerpt = _normalize_text(excerpt)
    parameter_seed = _canonical_parameter_seed(parameters)
    seed = f"{normalized_excerpt}|{invariant_type.value}|{parameter_seed}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f"INV-{digest[:16]}"


def compute_invariant_id_from_payload(
    invariant_type: InvariantType,
    parameters: InvariantParameters | None = None,
) -> str:
    """Compute deterministic invariant ID from invariant semantics only."""
    parameter_seed = _canonical_parameter_seed(parameters)
    seed = f"{invariant_type.value}|{parameter_seed}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f"INV-{digest[:16]}"


def classify_invariant_from_text(text: str) -> Invariant | None:
    """Classify a single invariant from a spec sentence using deterministic rules."""
    if not text or not text.strip():
        return None

    original_excerpt = text.strip()

    forbidden_match = re.search(
        r"must\s+not\s+use\s+(.+?)(?:[\.;]|$)",
        original_excerpt,
        flags=re.IGNORECASE,
    )
    if forbidden_match:
        capability = _normalize_token(forbidden_match.group(1))
        parameters = ForbiddenCapabilityParams(capability=capability)
        invariant_id = compute_invariant_id(
            original_excerpt,
            InvariantType.FORBIDDEN_CAPABILITY,
            parameters,
        )
        return Invariant(
            id=invariant_id,
            type=InvariantType.FORBIDDEN_CAPABILITY,
            parameters=parameters,
        )

    required_match = re.search(
        r"must\s+include\s+(.+?)(?:[\.;]|$)",
        original_excerpt,
        flags=re.IGNORECASE,
    )
    if required_match:
        field_name = _normalize_token(required_match.group(1))
        parameters = RequiredFieldParams(field_name=field_name)
        invariant_id = compute_invariant_id(
            original_excerpt,
            InvariantType.REQUIRED_FIELD,
            parameters,
        )
        return Invariant(
            id=invariant_id,
            type=InvariantType.REQUIRED_FIELD,
            parameters=parameters,
        )

    max_match = re.search(
        r"(?P<field>[a-zA-Z0-9_\-\s]+?)\s+must\s+be\s*<=\s*(?P<value>\d+(?:\.\d+)?)",
        original_excerpt,
        flags=re.IGNORECASE,
    )
    if max_match:
        field_name = _normalize_token(max_match.group("field"))
        value_raw = max_match.group("value")
        max_value = int(value_raw) if value_raw.isdigit() else float(value_raw)
        parameters = MaxValueParams(field_name=field_name, max_value=max_value)
        invariant_id = compute_invariant_id(
            original_excerpt,
            InvariantType.MAX_VALUE,
            parameters,
        )
        return Invariant(
            id=invariant_id,
            type=InvariantType.MAX_VALUE,
            parameters=parameters,
        )

    relation_match = re.search(
        r"(?P<expression>[a-zA-Z0-9_\-\s]+(?:<=|>=|==|=|<|>)\s*[a-zA-Z_][a-zA-Z0-9_\-\s]*)",
        original_excerpt,
        flags=re.IGNORECASE,
    )
    if relation_match:
        expression = " ".join(relation_match.group("expression").split())
        parameters = RelationConstraintParams(expression=expression)
        invariant_id = compute_invariant_id(
            original_excerpt,
            InvariantType.RELATION_CONSTRAINT,
            parameters,
        )
        return Invariant(
            id=invariant_id,
            type=InvariantType.RELATION_CONSTRAINT,
            parameters=parameters,
        )

    return None
