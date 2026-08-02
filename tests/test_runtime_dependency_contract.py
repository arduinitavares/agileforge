"""Tests for pinned runtime dependencies and package discovery."""

from __future__ import annotations

import importlib.metadata
import tomllib
from pathlib import Path


def test_runtime_dependencies_are_exactly_pinned() -> None:
    """Require exact runtime versions in project metadata and the environment."""
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert "google-adk==2.2.0" in project["project"]["dependencies"]
    assert "pytest-socket==0.8.0" in project["project"]["dependencies"]
    assert importlib.metadata.version("google-adk") == "2.2.0"
    assert importlib.metadata.version("pytest-socket") == "0.8.0"


def test_domain_and_adapter_packages_are_discovered() -> None:
    """Require workflow and adapter packages in setuptools discovery."""
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    included = set(project["tool"]["setuptools"]["packages"]["find"]["include"])
    assert {"workflow", "workflow.*", "adapters", "adapters.*"} <= included
