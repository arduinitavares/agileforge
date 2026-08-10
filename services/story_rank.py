"""Canonical Story rank parsing shared across planning boundaries."""

from __future__ import annotations


def parse_story_rank(value: str | None) -> int:
    """Return one canonical positive base-10 Story rank."""
    if (
        value is None
        or not value
        or not value.isascii()
        or not value.isdigit()
        or value.startswith("0")
    ):
        message = "Story rank must be a canonical positive base-10 integer string."
        raise ValueError(message)
    return int(value)


__all__ = ["parse_story_rank"]
