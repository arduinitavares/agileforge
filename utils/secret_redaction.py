"""Exact credential redaction for formatted logs and chunked child output."""

from __future__ import annotations

import codecs
import json
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import BinaryIO, TextIO


def _ordered_values(values: tuple[str, ...]) -> list[str]:
    """Return raw and serialized credential forms, longest first."""
    variants: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        normalized = value.replace("\r\n", "\n").replace("\r", "\n")
        newline_variants = (value, normalized, normalized.replace("\n", "\r\n"))
        for newline_variant in newline_variants:
            serialized_variants = (
                newline_variant,
                json.dumps(newline_variant, ensure_ascii=True)[1:-1],
                json.dumps(newline_variant, ensure_ascii=False)[1:-1],
                repr(newline_variant)[1:-1],
            )
            for variant in serialized_variants:
                if variant and variant not in seen:
                    seen.add(variant)
                    variants.append(variant)
    ordered: list[str] = variants
    ordered.sort(key=len, reverse=True)
    return ordered


def redact_text(text: str, values: tuple[str, ...]) -> str:
    """Remove explicit credential values, longest first, without logging them."""
    for value in _ordered_values(values):
        text = text.replace(value, "[REDACTED]")
    return text


class SecretRedactor:
    """Hold incomplete matches across bounded binary reads."""

    def __init__(self, values: tuple[str, ...]) -> None:
        """Retain only explicit nonempty credentials and the required overlap."""
        self.values = tuple(value.encode() for value in _ordered_values(values))
        self.overlap = max((len(value) for value in self.values), default=1) - 1
        self.pending = b""

    def _replace(self, payload: bytes) -> bytes:
        for value in self.values:
            payload = payload.replace(value, b"[REDACTED]")
        return payload

    def feed(self, payload: bytes) -> bytes:
        """Emit only bytes that cannot begin an incomplete credential."""
        self.pending += payload
        cutoff = max(0, len(self.pending) - self.overlap)
        for value in self.values:
            start = self.pending.find(value)
            while 0 <= start < cutoff:
                if start + len(value) > cutoff:
                    cutoff = start
                    break
                start = self.pending.find(value, start + 1)
        result = self._replace(self.pending[:cutoff])
        self.pending = self.pending[cutoff:]
        return result

    def finish(self) -> bytes:
        """Redact the final buffered bytes after EOF."""
        result = self._replace(self.pending)
        self.pending = b""
        return result


def forward_redacted_output(
    source: BinaryIO, destination: TextIO, values: tuple[str, ...]
) -> None:
    """Drain a supervised pipe without exposing credentials at read boundaries."""
    redactor = SecretRedactor(values)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        while payload := os.read(source.fileno(), 4096):
            destination.write(decoder.decode(redactor.feed(payload)))
            destination.flush()
        destination.write(decoder.decode(redactor.finish(), final=True))
        destination.flush()
    finally:
        source.close()
