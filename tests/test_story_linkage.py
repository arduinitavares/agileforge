"""Tests for deterministic story and requirement linkage helpers."""

import pytest

from services.story_linkage import (
    normalize_requirement_key,
    title_changed_significantly,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  User   Authentication  ", "user authentication"),
        ("", ""),
    ],
)
def test_normalize_requirement_key(raw: str, expected: str) -> None:
    """Normalize case and whitespace for stable linkage keys."""
    assert normalize_requirement_key(raw) == expected


@pytest.mark.parametrize(
    ("previous", "new", "expected"),
    [
        (None, "User authentication", False),
        ("User authentication", " user  authentication ", False),
        ("User authentication flow", "Authentication user flow", False),
        ("User authentication", "Export billing report", True),
    ],
)
def test_title_changed_significantly(
    previous: str | None,
    new: str | None,
    expected: bool,
) -> None:
    """Detect only substantial deterministic title drift."""
    assert title_changed_significantly(previous, new) is expected
