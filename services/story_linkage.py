"""Shared helpers for deterministic backlog/refinement linkage."""

from __future__ import annotations

import re

SIGNIFICANT_TITLE_SIMILARITY_THRESHOLD = 0.5


def normalize_requirement_key(text: str) -> str:
    """Normalize requirement text for stable deterministic linkage keys."""
    if not text:
        return ""
    return " ".join(text.strip().lower().split())


def title_changed_significantly(
    previous_title: str | None, new_title: str | None
) -> bool:
    """
    Heuristic drift detector for slot/title stability warnings.

    This is intentionally simple and deterministic:
    - normalize casing/whitespace
    - strip punctuation
    - compare token overlap ratio
    """
    old = normalize_requirement_key(previous_title or "")
    new = normalize_requirement_key(new_title or "")
    if not old or not new:
        return False
    if old == new:
        return False

    old_tokens = set(re.findall(r"[a-z0-9]+", old))
    new_tokens = set(re.findall(r"[a-z0-9]+", new))
    if not old_tokens or not new_tokens:
        return False

    overlap = len(old_tokens.intersection(new_tokens))
    union = len(old_tokens.union(new_tokens))
    similarity = overlap / union if union else 1.0
    return similarity < SIGNIFICANT_TITLE_SIMILARITY_THRESHOLD
