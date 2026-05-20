# scripts/authority_quality_benchmark.py
"""Helpers for AgileForge authority quality benchmark artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


JsonObject = dict[str, Any]


def normalize_source_text(raw_text: str) -> str:
    """Normalize source text without changing its semantic wording."""
    normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.rstrip() + "\n"


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


def build_source_meta(
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
