"""Installed and checkout runtimes write diagnostics to their owned state."""

from __future__ import annotations

from pathlib import Path

import pytest

from utils import runtime_ownership


def test_production_output_root_uses_owned_profile_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database URL cannot redirect production output outside profile state."""
    monkeypatch.setenv("AGILEFORGE_PRODUCTION_PROFILE", "default")
    monkeypatch.setenv("AGILEFORGE_DB_URL", "sqlite:////unrelated/business.sqlite3")
    assert runtime_ownership.runtime_output_root() == Path(
        "/var/lib/agileforge/profiles/default"
    )


def test_checkout_child_output_root_is_the_selected_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each checkout child keeps logs and failure artifacts with its own DB."""
    monkeypatch.delenv("AGILEFORGE_PRODUCTION_PROFILE", raising=False)
    monkeypatch.setenv("AGILEFORGE_LAUNCHER_CHILD", "1")
    monkeypatch.setenv("AGILEFORGE_DB_URL", f"sqlite:///{tmp_path}/business.sqlite3")
    assert runtime_ownership.runtime_output_root() == tmp_path


def test_child_output_root_rejects_relative_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed child configuration must not fall back to package writes."""
    monkeypatch.delenv("AGILEFORGE_PRODUCTION_PROFILE", raising=False)
    monkeypatch.setenv("AGILEFORGE_LAUNCHER_CHILD", "1")
    monkeypatch.setenv("AGILEFORGE_DB_URL", "sqlite:///relative.sqlite3")
    with pytest.raises(ValueError, match="absolute SQLite"):
        runtime_ownership.runtime_output_root()
