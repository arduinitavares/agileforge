"""Tests for pinned runtime dependencies and package discovery."""

from __future__ import annotations

import importlib.metadata
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def _assert_exact_requirement(
    dependencies: list[str],
    *,
    distribution: str,
    version: str,
) -> None:
    normalized_distribution = canonicalize_name(distribution)
    parsed_requirements = [Requirement(dependency) for dependency in dependencies]
    matching_requirements = [
        requirement
        for requirement in parsed_requirements
        if canonicalize_name(requirement.name) == normalized_distribution
    ]
    expected_requirement = Requirement(f"{distribution}=={version}")
    assert matching_requirements == [expected_requirement], (
        f"{distribution} must have exactly one requirement: {expected_requirement}"
    )


def test_runtime_dependencies_are_exactly_pinned() -> None:
    """Require exact runtime versions in project metadata and the environment."""
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]
    _assert_exact_requirement(
        dependencies,
        distribution="google-adk",
        version="2.2.0",
    )
    _assert_exact_requirement(
        dependencies,
        distribution="pytest-socket",
        version="0.8.0",
    )
    assert importlib.metadata.version("google-adk") == "2.2.0"
    assert importlib.metadata.version("pytest-socket") == "0.8.0"


@pytest.mark.parametrize(
    ("distribution", "version", "requirements"),
    [
        (
            "google-adk",
            "2.2.0",
            ["google-adk==2.2.0", "google_adk==2.2.0"],
        ),
        (
            "google-adk",
            "2.2.0",
            ["google-adk==2.2.0", "google-adk>=2.0.0"],
        ),
        (
            "google-adk",
            "2.2.0",
            ["google-adk==2.2.0", "google-adk==2.1.0"],
        ),
        (
            "pytest-socket",
            "0.8.0",
            ["pytest-socket==0.8.0", "pytest_socket==0.8.0"],
        ),
        (
            "pytest-socket",
            "0.8.0",
            ["pytest-socket==0.8.0", "pytest-socket>=0.7.0"],
        ),
        (
            "pytest-socket",
            "0.8.0",
            ["pytest-socket==0.8.0", "pytest-socket==0.7.0"],
        ),
    ],
)
def test_exact_runtime_requirement_rejects_ambiguous_entries(
    distribution: str,
    version: str,
    requirements: list[str],
) -> None:
    """Reject duplicate, broad, or conflicting requirements for a runtime pin."""
    with pytest.raises(AssertionError):
        _assert_exact_requirement(
            requirements,
            distribution=distribution,
            version=version,
        )


def test_domain_and_adapter_packages_are_discovered() -> None:
    """Require workflow and adapter packages in setuptools discovery."""
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    included = set(project["tool"]["setuptools"]["packages"]["find"]["include"])
    assert {"workflow", "workflow.*", "adapters", "adapters.*"} <= included
