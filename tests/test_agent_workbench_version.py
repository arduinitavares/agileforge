"""Tests for agent workbench version metadata."""

from importlib import metadata as importlib_metadata

import pytest

from services.agent_workbench import version


def test_version_constants_are_stable() -> None:
    """Expose stable command and storage schema versions."""
    assert version.COMMAND_VERSION == "1"
    assert version.STORAGE_SCHEMA_VERSION == "9"


def test_agileforge_version_uses_package_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read the installed AgileForge package version when available."""

    def fake_version(package_name: str) -> str:
        assert package_name == "agileforge"
        return "2.3.4"

    monkeypatch.setattr(version.importlib_metadata, "version", fake_version)

    assert version.agileforge_version() == "2.3.4"


def test_agileforge_version_falls_back_to_dev(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return dev when package metadata is unavailable."""

    def missing_version(package_name: str) -> str:
        assert package_name == "agileforge"
        raise importlib_metadata.PackageNotFoundError(package_name)

    monkeypatch.setattr(version.importlib_metadata, "version", missing_version)

    assert version.agileforge_version() == "dev"
