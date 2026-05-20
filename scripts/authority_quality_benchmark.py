# scripts/authority_quality_benchmark.py
"""Helpers for AgileForge authority quality benchmark artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


JsonObject = dict[str, Any]


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
) -> JsonObject:
    """Build source metadata for a normalized benchmark fixture source."""
    return {
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


def _redact_command(command: str) -> str:
    redacted = re.sub(r"(--project-id=)\S+", r"\1REDACTED", command)
    return re.sub(r"(--project-id\s+)\S+", r"\1REDACTED", redacted)
