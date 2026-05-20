"""Tests for authority quality benchmark helpers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from scripts.authority_quality_benchmark import (
    build_source_meta,
    normalize_source_text,
    sha256_text,
    write_json,
    write_text,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_normalize_source_text_converts_crlf_and_ensures_trailing_newline() -> None:
    """Source normalization converts line endings and leaves one final newline."""
    raw = "# Title\r\n\r\nLine one\r\n"

    normalized = normalize_source_text(raw)

    assert normalized == "# Title\n\nLine one\n"
    assert sha256_text(normalized).startswith("sha256:")
    assert len(sha256_text(normalized)) == len("sha256:") + 64


def test_normalize_source_text_preserves_trailing_spaces_before_newline() -> None:
    """Source normalization preserves spaces and tabs before the final newline."""
    raw = "Line with spaces  \t\r\n"

    normalized = normalize_source_text(raw)

    assert normalized == "Line with spaces  \t\n"


def test_build_source_meta_records_hashes_and_license_note() -> None:
    """Source metadata records artifacts, hashes, normalization, and license."""
    raw = "# Raw\n"
    normalized = "# Normalized\n"

    meta = build_source_meta(
        source_url="https://example.test/source",
        fetched_at="2026-05-20T12:00:00Z",
        raw_artifact="source/raw/source.raw.md",
        raw_text=raw,
        normalized_artifact="source/source.md",
        normalized_text=normalized,
        normalization_method="raw-markdown-copy",
        normalization_tool="manual",
        normalization_tool_version="n/a",
        normalization_notes="Line endings normalized to LF.",
        license_note="Public fixture retained for benchmark review.",
    )

    assert meta["source_url"] == "https://example.test/source"
    assert meta["raw_sha256"] == sha256_text(raw)
    assert meta["normalized_sha256"] == sha256_text(normalized)
    assert meta["normalization"]["method"] == "raw-markdown-copy"
    assert meta["license_note"] == "Public fixture retained for benchmark review."


def test_write_json_sorts_keys_and_adds_newline(tmp_path: Path) -> None:
    """Stable JSON output sorts keys and writes a final newline."""
    output = tmp_path / "meta.json"

    write_json(output, {"b": 2, "a": 1})

    assert output.read_text(encoding="utf-8") == '{\n  "a": 1,\n  "b": 2\n}\n'
    assert json.loads(output.read_text(encoding="utf-8")) == {"a": 1, "b": 2}


def test_write_text_creates_parent_directories_and_writes_utf8(tmp_path: Path) -> None:
    """Text output creates parent directories and writes UTF-8 content."""
    output = tmp_path / "nested" / "source.md"

    write_text(output, "Café\n")

    assert output.read_text(encoding="utf-8") == "Café\n"
