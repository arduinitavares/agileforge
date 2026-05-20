# scripts/authority_quality_benchmark.py
"""Helpers for AgileForge authority quality benchmark artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Final

JsonObject = dict[str, Any]
LOCAL_COMMAND_FLAGS = ("project-id", "idempotency-key", "review-token")
MIN_STRUCTURED_SOURCE_REF_PARTS: Final[int] = 2
STRUCTURED_ITEM_PREFIXES: Final[frozenset[str]] = frozenset(
    {
        "ASSUMPTION",
        "CONSTRAINT",
        "DATA",
        "DECISION",
        "EXAMPLE",
        "GOAL",
        "INTERFACE",
        "NON_GOAL",
        "OPEN_QUESTION",
        "QUALITY",
        "REQ",
        "RISK",
    }
)
_AUTHORITY_ASSERTION_PREFIXES: Final[tuple[str, ...]] = (
    "FORBIDDEN_CAPABILITY:",
    "MAX_VALUE:",
    "RELATION_CONSTRAINT:",
    "REQUIRED_FIELD:",
)
_TODOMVC_DEFERABLE_MUST_GAP_ITEMS: Final[frozenset[str]] = frozenset(
    {
        "REQ.readme",
        "REQ.dependency-management",
    }
)
_TODOMVC_REQUIRED_FIELD_COMPRESSION_ITEMS: Final[frozenset[str]] = frozenset(
    {
        "REQ.new-todo",
        "REQ.toggle-all",
        "REQ.item-interactions",
        "REQ.editing",
        "REQ.counter",
        "REQ.clear-completed",
        "REQ.persistence",
        "REQ.routing",
        "REQ.filtered-state",
    }
)
_TODOMVC_REQUIRED_CONCEPTS: Final[dict[str, tuple[tuple[str, ...], ...]]] = {
    "CONSTRAINT.code-style-rules": (
        ("double-quotes", "double", "quotes"),
        ("single-quotes", "single", "quotes"),
        ("keycode", "key", "codes", "constants"),
        ("npm", "packages"),
    ),
    "REQ.readme": (
        ("readme",),
        ("framework",),
        ("implementation",),
        ("build",),
    ),
    "REQ.dependency-management": (
        ("package.json", "package"),
        ("todomvc-common",),
        ("todomvc-app-css",),
    ),
    "REQ.new-todo": (
        ("enter",),
        ("trim", "trimmed"),
        ("empty", "non-empty"),
        ("append", "appended"),
        ("clear", "cleared"),
    ),
    "REQ.toggle-all": (
        ("all",),
        ("complete", "completed"),
        ("clear completed", "clear-completed"),
        ("single", "individual"),
    ),
    "REQ.item-interactions": (
        ("checkbox",),
        ("completed",),
        ("double-click", "double", "dblclick"),
        ("editing",),
        ("hover",),
        ("destroy", "remove"),
    ),
    "REQ.editing": (
        ("focus", "focused"),
        ("blur",),
        ("enter",),
        ("trim", "trimmed"),
        ("destroy", "removed"),
        ("escape",),
        ("discard", "discarded"),
    ),
    "REQ.counter": (
        ("active",),
        ("strong",),
        ("plural", "pluralize", "items", "item"),
    ),
    "REQ.clear-completed": (
        ("completed",),
        ("remove", "removed"),
        ("hidden", "hide"),
    ),
    "REQ.persistence": (
        ("persist", "persisted"),
        ("localstorage", "localStorage"),
        ("reload", "restore", "restored"),
        ("framework",),
    ),
    "REQ.routing": (
        ("#/", "all", "default"),
        ("#/active", "active"),
        ("#/completed", "completed"),
        ("flatiron", "director"),
        ("framework",),
    ),
    "REQ.filtered-state": (
        ("route", "filter", "filtered"),
        ("model",),
        ("selected",),
        ("visibility", "hidden", "shown"),
        ("reload", "persist", "restored"),
    ),
}
_TODOMVC_SOURCE_IDENTITY_CONCEPTS: Final[dict[str, tuple[str, ...]]] = {
    "CONSTRAINT.browser-support": ("browser", "support"),
    "REQ.clear-completed": ("clear", "completed"),
    "REQ.filtered-state": ("filter", "route", "selected"),
}


def normalize_source_text(raw_text: str) -> str:
    """Normalize source text without changing its semantic wording."""
    normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.rstrip("\n") + "\n"


def sha256_text(text: str) -> str:
    """Return a prefixed SHA-256 hash for UTF-8 text."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def write_json(path: Path, payload: JsonObject) -> None:
    """Write stable pretty JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_text(path: Path, text: str) -> None:
    """Write UTF-8 text, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_source_meta(  # noqa: PLR0913
    *,
    source_url: str,
    fetched_at: str,
    raw_artifact: str,
    raw_text: str,
    normalized_artifact: str,
    normalized_text: str,
    normalization_method: str,
    normalization_tool: str,
    normalization_tool_version: str,
    normalization_notes: str,
    license_note: str,
    immutable_source_url: str | None = None,
    upstream_commit: str | None = None,
) -> JsonObject:
    """Build source metadata for a normalized benchmark fixture source."""
    meta: JsonObject = {
        "source_url": source_url,
        "fetched_at": fetched_at,
        "raw_artifact": raw_artifact,
        "raw_sha256": sha256_text(raw_text),
        "normalized_artifact": normalized_artifact,
        "normalized_sha256": sha256_text(normalized_text),
        "normalization": {
            "method": normalization_method,
            "tool": normalization_tool,
            "tool_version": normalization_tool_version,
            "notes": normalization_notes,
        },
        "license_note": license_note,
    }
    if immutable_source_url:
        meta["immutable_source_url"] = immutable_source_url
    if upstream_commit:
        meta["upstream_commit"] = upstream_commit
    return meta


def sanitize_review_packet(packet: JsonObject) -> JsonObject:
    """Return a committed-safe review summary from an authority review packet."""
    if not isinstance(packet, dict):
        return {"review_summary": None, "review_findings": []}

    data = packet.get("data")
    if not isinstance(data, dict):
        return {"review_summary": None, "review_findings": []}

    review_summary = data.get("review_summary")
    review_findings = data.get("review_findings", [])
    return {
        "review_summary": review_summary if isinstance(review_summary, dict) else None,
        "review_findings": review_findings if isinstance(review_findings, list) else [],
        "pending_authority_summary": _pending_authority_summary(data),
    }


def _pending_authority_summary(data: JsonObject) -> JsonObject:
    pending = data.get("pending_authority")
    if not isinstance(pending, dict):
        return {}

    artifact = pending.get("artifact")
    if not isinstance(artifact, dict):
        artifact = {}

    return {
        "assumption_count": _list_count(artifact.get("assumptions")),
        "eligible_feature_rule_count": _list_count(
            artifact.get("eligible_feature_rules")
        ),
        "gap_count": _list_count(artifact.get("gaps")),
        "invariant_count": _list_count(artifact.get("invariants")),
        "rejected_feature_count": _list_count(artifact.get("rejected_features")),
    }


def _list_count(value: object) -> int:
    return len(value) if isinstance(value, list) else 0


def extract_compiled_authority(packet: JsonObject) -> JsonObject:
    """Extract compiled authority artifact from an authority review packet."""
    if not isinstance(packet, dict):
        return {}

    data = packet.get("data")
    if not isinstance(data, dict):
        return {}

    pending = data.get("pending_authority")
    if not isinstance(pending, dict):
        return {}

    artifact = pending.get("artifact")
    if not isinstance(artifact, dict):
        return {}
    return artifact


def build_run_manifest(  # noqa: PLR0913
    *,
    agileforge_commit: str,
    agileforge_branch: str,
    schema_version: str,
    compiler_version: str,
    spec_generation_model: str,
    authority_compiler_model: str,
    prompt_versions: list[str],
    normalized_source_text: str,
    gold_spec_text: str,
    compiled_authority_text: str,
    create_command: str,
    review_command: str,
    extraction_command: str,
    generated_at: str,
    acceptance_mutation_status: str,
) -> JsonObject:
    """Build sanitized benchmark run metadata."""
    return {
        "acceptance_mutation_status": acceptance_mutation_status,
        "agileforge_branch": agileforge_branch,
        "agileforge_commit": agileforge_commit,
        "authority_compiler_model": authority_compiler_model,
        "commands": {
            "create": _redact_command(create_command),
            "extraction": _redact_command(extraction_command),
            "review": _redact_command(review_command),
        },
        "compiled_authority_sha256": sha256_text(compiled_authority_text),
        "compiler_version": compiler_version,
        "generated_at": generated_at,
        "gold_spec_sha256": sha256_text(gold_spec_text),
        "normalized_source_sha256": sha256_text(
            normalize_source_text(normalized_source_text)
        ),
        "prompt_versions": prompt_versions,
        "schema_version": schema_version,
        "spec_generation_model": spec_generation_model,
    }


def evaluate_todomvc_authority_guardrails(
    *,
    gold_spec: JsonObject,
    authority: JsonObject,
    review_summary: JsonObject,
) -> JsonObject:
    """Evaluate TodoMVC fixture authority against human-adjudicated guardrails.

    This is a benchmark oracle for the TodoMVC fixture. It uses stable structured
    spec item IDs plus human-selected concept groups; it is not a general prose
    extractor and should not be used as the product semantic judge.
    """
    source_items = _gold_items_by_id(gold_spec)
    authority_items = _authority_invariants(authority)
    authority_by_source_item = _authority_text_by_source_item(authority_items)
    gap_source_item_ids = _authority_gap_source_item_ids(authority)
    weak_or_missing = _weak_or_missing_todomvc_must_items(
        source_items=source_items,
        authority_by_source_item=authority_by_source_item,
        gap_source_item_ids=gap_source_item_ids,
    )
    findings: list[JsonObject] = []

    if weak_or_missing:
        findings.append(
            _finding(
                code="MISSING_MUST_AUTHORITY",
                severity="blocking",
                message=(
                    "TodoMVC MUST-level source items are missing or only weakly "
                    "represented in the compiled authority."
                ),
                source_refs=weak_or_missing,
                details={"items": weak_or_missing},
            )
        )

    unsafe_required_fields = _unsafe_required_field_compressions(authority_items)
    if unsafe_required_fields:
        findings.append(
            _finding(
                code="UNSAFE_REQUIRED_FIELD_COMPRESSION",
                severity="blocking",
                message=(
                    "Rich TodoMVC behavior is compressed into REQUIRED_FIELD "
                    "existence checks."
                ),
                source_refs=_finding_source_refs(unsafe_required_fields),
                details={"invariant_ids": _finding_ids(unsafe_required_fields)},
            )
        )

    source_mismatches = _source_ref_semantic_mismatches(authority_items)
    if source_mismatches:
        findings.append(
            _finding(
                code="SOURCE_REF_SEMANTIC_MISMATCH",
                severity="blocking",
                message=(
                    "Authority items point to structured source IDs whose core "
                    "concepts do not match the authority text."
                ),
                source_refs=_finding_source_refs(source_mismatches),
                details={"invariant_ids": _finding_ids(source_mismatches)},
            )
        )

    modality_promotions = _modality_over_promotions(
        source_items=source_items,
        authority_items=authority_items,
    )
    if modality_promotions:
        findings.append(
            _finding(
                code="MODALITY_OVER_PROMOTION",
                severity="blocking",
                message=(
                    "SHOULD-level source guidance is compiled as hard authority."
                ),
                source_refs=_finding_source_refs(modality_promotions),
                details={"invariant_ids": _finding_ids(modality_promotions)},
            )
        )

    example_promotions = _example_used_as_normative_source(authority_items)
    if example_promotions:
        findings.append(
            _finding(
                code="EXAMPLE_USED_AS_NORMATIVE_SOURCE",
                severity="blocking",
                message=(
                    "Illustrative EXAMPLE items are used as source evidence for "
                    "normative authority."
                ),
                source_refs=_finding_source_refs(example_promotions),
                details={"invariant_ids": _finding_ids(example_promotions)},
            )
        )

    if _review_summary_status(review_summary) == "accept_ready" and findings:
        findings.append(
            _finding(
                code="FALSE_POSITIVE_ACCEPT_READY",
                severity="blocking",
                message=(
                    "Sanitized review summary reports accept_ready despite "
                    "semantic benchmark guardrail failures."
                ),
                source_refs=[],
                details={},
            )
        )

    return {
        "fixture": "todomvc",
        "verdict": "REJECT" if _has_blocking_findings(findings) else "ACCEPT",
        "findings": findings,
        "weak_or_missing_must_items": weak_or_missing,
    }


def _gold_items_by_id(gold_spec: JsonObject) -> dict[str, JsonObject]:
    items = gold_spec.get("items")
    if not isinstance(items, list):
        return {}
    result: dict[str, JsonObject] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        if isinstance(item_id, str) and item_id:
            result[item_id] = item
    return result


def _authority_invariants(authority: JsonObject) -> list[JsonObject]:
    invariants = authority.get("invariants")
    if not isinstance(invariants, list):
        return []
    source_refs_by_invariant = _source_map_refs_by_invariant(authority)
    result: list[JsonObject] = []
    for item in invariants:
        if not isinstance(item, dict):
            continue
        item_copy = dict(item)
        item_id = item_copy.get("id")
        source_refs = _authority_source_refs(item_copy)
        if isinstance(item_id, str):
            source_refs.extend(source_refs_by_invariant.get(item_id, []))
        parameters = item_copy.get("parameters")
        if isinstance(parameters, dict):
            source_item_id = parameters.get("source_item_id")
            if isinstance(source_item_id, str):
                source_refs.append(source_item_id)
        item_copy["_benchmark_source_refs"] = sorted(set(source_refs))
        result.append(item_copy)
    return result


def _source_map_refs_by_invariant(authority: JsonObject) -> dict[str, list[str]]:
    source_map = authority.get("source_map")
    if not isinstance(source_map, list):
        return {}

    by_invariant: dict[str, list[str]] = {}
    for entry in source_map:
        if not isinstance(entry, dict):
            continue
        invariant_id = entry.get("invariant_id")
        location = entry.get("location")
        if not isinstance(invariant_id, str) or not isinstance(location, str):
            continue
        by_invariant.setdefault(invariant_id, []).append(location)
    return by_invariant


def _authority_gap_source_item_ids(authority: JsonObject) -> set[str]:
    gaps = authority.get("gaps")
    if not isinstance(gaps, list):
        return set()

    source_item_ids: set[str] = set()
    for gap in gaps:
        if isinstance(gap, str):
            source_item_ids.update(_source_item_ids_mentioned_in_text(gap))
            continue
        if not isinstance(gap, dict):
            continue
        for field_name in ("id", "text", "source_excerpt"):
            value = gap.get(field_name)
            if isinstance(value, str):
                source_item_ids.update(_source_item_ids_mentioned_in_text(value))
        source_refs = gap.get("source_refs")
        if isinstance(source_refs, list):
            for source_ref in source_refs:
                if isinstance(source_ref, str) and (
                    source_item_id := _source_item_id(source_ref)
                ):
                    source_item_ids.add(source_item_id)
    return source_item_ids


def _source_item_ids_mentioned_in_text(text: str) -> set[str]:
    prefixes = "|".join(re.escape(prefix) for prefix in STRUCTURED_ITEM_PREFIXES)
    pattern = re.compile(rf"\b(?:{prefixes})\.[A-Za-z0-9_-]+")
    return {match.group(0) for match in pattern.finditer(text)}


def _authority_text_by_source_item(
    authority_items: list[JsonObject],
) -> dict[str, list[str]]:
    by_source_item: dict[str, list[str]] = {}
    for item in authority_items:
        text = _authority_text(item)
        for source_ref in _authority_source_refs(item):
            source_item_id = _source_item_id(source_ref)
            if source_item_id is None:
                continue
            by_source_item.setdefault(source_item_id, []).append(text)
    return by_source_item


def _authority_text(item: JsonObject) -> str:
    text = item.get("text")
    if isinstance(text, str):
        return text

    invariant_type = item.get("type")
    parameters = item.get("parameters")
    if isinstance(invariant_type, str) and isinstance(parameters, dict):
        return f"{invariant_type}:{json.dumps(parameters, sort_keys=True)}"
    return json.dumps(item, sort_keys=True)


def _authority_source_refs(item: JsonObject) -> list[str]:
    benchmark_source_refs = item.get("_benchmark_source_refs")
    if isinstance(benchmark_source_refs, list):
        return [
            source_ref
            for source_ref in benchmark_source_refs
            if isinstance(source_ref, str)
        ]

    source_refs = item.get("source_refs")
    if not isinstance(source_refs, list):
        return []
    return [source_ref for source_ref in source_refs if isinstance(source_ref, str)]


def _source_item_id(source_ref: str) -> str | None:
    parts = source_ref.split(".")
    if (
        len(parts) < MIN_STRUCTURED_SOURCE_REF_PARTS
        or parts[0] not in STRUCTURED_ITEM_PREFIXES
    ):
        return None
    return ".".join(parts[:MIN_STRUCTURED_SOURCE_REF_PARTS])


def _weak_or_missing_todomvc_must_items(
    *,
    source_items: dict[str, JsonObject],
    authority_by_source_item: dict[str, list[str]],
    gap_source_item_ids: set[str],
) -> list[str]:
    weak_or_missing: list[str] = []
    for item_id, concept_groups in _TODOMVC_REQUIRED_CONCEPTS.items():
        source_item = source_items.get(item_id)
        if source_item is None or source_item.get("level") != "MUST":
            continue
        if (
            item_id in _TODOMVC_DEFERABLE_MUST_GAP_ITEMS
            and item_id in gap_source_item_ids
        ):
            continue
        authority_text = " ".join(authority_by_source_item.get(item_id, []))
        if not authority_text:
            weak_or_missing.append(item_id)
            continue
        if _missing_concept_groups(authority_text, concept_groups):
            weak_or_missing.append(item_id)
    return sorted(weak_or_missing)


def _missing_concept_groups(
    text: str,
    concept_groups: tuple[tuple[str, ...], ...],
) -> list[str]:
    return [
        "/".join(group)
        for group in concept_groups
        if not any(_text_has_concept(text, concept) for concept in group)
    ]


def _text_has_concept(text: str, concept: str) -> bool:
    lowered = text.casefold()
    lowered_concept = concept.casefold()
    if lowered_concept in lowered:
        return True
    return lowered_concept in _tokens(text)


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.split(r"[^a-zA-Z0-9#/_-]+", text.casefold())
        if token
    }


def _unsafe_required_field_compressions(
    authority_items: list[JsonObject],
) -> list[JsonObject]:
    unsafe: list[JsonObject] = []
    for item in authority_items:
        text = _authority_text(item)
        if not text.startswith("REQUIRED_FIELD:"):
            continue
        source_items = {
            source_item_id
            for source_ref in _authority_source_refs(item)
            if (source_item_id := _source_item_id(source_ref)) is not None
        }
        if any(
            source_item_id in _TODOMVC_REQUIRED_FIELD_COMPRESSION_ITEMS
            for source_item_id in source_items
        ):
            unsafe.append(item)
    return unsafe


def _source_ref_semantic_mismatches(
    authority_items: list[JsonObject],
) -> list[JsonObject]:
    mismatches: list[JsonObject] = []
    for item in authority_items:
        text = _authority_text(item)
        for source_ref in _authority_source_refs(item):
            source_item_id = _source_item_id(source_ref)
            if source_item_id is None:
                continue
            identity_concepts = _TODOMVC_SOURCE_IDENTITY_CONCEPTS.get(source_item_id)
            if identity_concepts is None:
                continue
            if not any(
                _text_has_concept(text, concept) for concept in identity_concepts
            ):
                mismatches.append(item)
                break
    return mismatches


def _modality_over_promotions(
    *,
    source_items: dict[str, JsonObject],
    authority_items: list[JsonObject],
) -> list[JsonObject]:
    promotions: list[JsonObject] = []
    for item in authority_items:
        text = _authority_text(item)
        if not text.startswith("FORBIDDEN_CAPABILITY:"):
            continue
        for source_ref in _authority_source_refs(item):
            source_item_id = _source_item_id(source_ref)
            if source_item_id is None:
                continue
            source_item = source_items.get(source_item_id)
            if source_item is None:
                continue
            if source_item.get("type") == "NON_GOAL":
                continue
            if source_item.get("level") not in {"MUST", "MUST_NOT"}:
                promotions.append(item)
                break
    return promotions


def _example_used_as_normative_source(
    authority_items: list[JsonObject],
) -> list[JsonObject]:
    promoted: list[JsonObject] = []
    for item in authority_items:
        text = _authority_text(item)
        if not text.startswith(_AUTHORITY_ASSERTION_PREFIXES):
            continue
        if any(
            source_ref.startswith("EXAMPLE.")
            for source_ref in _authority_source_refs(item)
        ):
            promoted.append(item)
    return promoted


def _review_summary_status(review_summary: JsonObject) -> str | None:
    summary = review_summary.get("review_summary")
    if not isinstance(summary, dict):
        summary = review_summary
    status = summary.get("acceptance_status")
    return status if isinstance(status, str) else None


def _has_blocking_findings(findings: list[JsonObject]) -> bool:
    return any(finding.get("severity") == "blocking" for finding in findings)


def _finding(
    *,
    code: str,
    severity: str,
    message: str,
    source_refs: list[str],
    details: JsonObject,
) -> JsonObject:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "source_refs": sorted(set(source_refs)),
        "details": details,
    }


def _finding_source_refs(items: list[JsonObject]) -> list[str]:
    refs: set[str] = set()
    for item in items:
        refs.update(_authority_source_refs(item))
    return sorted(refs)


def _finding_ids(items: list[JsonObject]) -> list[str]:
    ids = [item.get("id") for item in items]
    return sorted(item_id for item_id in ids if isinstance(item_id, str))


def _redact_command(command: str) -> str:
    flag_pattern = "|".join(re.escape(flag) for flag in LOCAL_COMMAND_FLAGS)
    redacted = re.sub(rf"(--(?:{flag_pattern})=)\S+", r"\1REDACTED", command)
    return re.sub(rf"(--(?:{flag_pattern})\s+)\S+", r"\1REDACTED", redacted)


def _load_json(path: Path) -> JsonObject:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {}
    return payload


def _cmd_init_source(args: argparse.Namespace) -> int:
    fixture_dir = Path(args.fixture_dir)
    raw_input = Path(args.raw_input)
    raw_text = raw_input.read_text(encoding="utf-8", newline="")
    normalized = normalize_source_text(raw_text)
    raw_relative = f"source/raw/{args.raw_artifact_name}"

    write_text(fixture_dir / raw_relative, raw_text)
    write_text(fixture_dir / "source/source.md", normalized)
    write_text(
        fixture_dir / "source/source.sha256",
        sha256_text(normalized) + "\n",
    )
    meta = build_source_meta(
        source_url=args.source_url,
        fetched_at=args.fetched_at,
        raw_artifact=raw_relative,
        raw_text=raw_text,
        normalized_artifact="source/source.md",
        normalized_text=normalized,
        normalization_method=args.normalization_method,
        normalization_tool=args.normalization_tool,
        normalization_tool_version=args.normalization_tool_version,
        normalization_notes=args.normalization_notes,
        license_note=args.license_note,
        immutable_source_url=args.immutable_source_url,
        upstream_commit=args.upstream_commit,
    )
    write_json(fixture_dir / "source/source.meta.json", meta)
    return 0


def _cmd_extract_review(args: argparse.Namespace) -> int:
    fixture_dir = Path(args.fixture_dir)
    packet = _load_json(Path(args.review_packet))
    write_json(
        fixture_dir / "agileforge/compiled-authority.json",
        extract_compiled_authority(packet),
    )
    write_json(
        fixture_dir / "agileforge/review-summary.json",
        sanitize_review_packet(packet),
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build AgileForge authority quality benchmark artifacts."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_source = subparsers.add_parser("init-source")
    init_source.add_argument("--fixture-dir", required=True)
    init_source.add_argument("--source-url", required=True)
    init_source.add_argument("--immutable-source-url")
    init_source.add_argument("--upstream-commit")
    init_source.add_argument("--raw-input", required=True)
    init_source.add_argument("--raw-artifact-name", required=True)
    init_source.add_argument("--fetched-at", required=True)
    init_source.add_argument("--normalization-method", required=True)
    init_source.add_argument("--normalization-tool", required=True)
    init_source.add_argument("--normalization-tool-version", required=True)
    init_source.add_argument("--normalization-notes", required=True)
    init_source.add_argument("--license-note", required=True)
    init_source.set_defaults(func=_cmd_init_source)

    extract_review = subparsers.add_parser("extract-review")
    extract_review.add_argument("--fixture-dir", required=True)
    extract_review.add_argument("--review-packet", required=True)
    extract_review.set_defaults(func=_cmd_extract_review)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the benchmark helper CLI."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
