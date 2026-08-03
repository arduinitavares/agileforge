"""Package-resource tests for the dashboard frontend."""

from __future__ import annotations

import importlib
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi.staticfiles import StaticFiles
from starlette.routing import Mount

import frontend

if TYPE_CHECKING:
    import pytest


def test_frontend_assets_are_available_as_package_resources() -> None:
    """Resolve every shipped dashboard asset without depending on CWD."""
    assert frontend.__file__ is not None
    frontend_root = files("frontend")

    assert frontend_root.joinpath("index.html").is_file()
    assert frontend_root.joinpath("project.html").is_file()
    assert frontend_root.joinpath("app.js").is_file()
    assert frontend_root.joinpath("project.js").is_file()


def test_dashboard_mount_is_independent_of_process_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Mount StaticFiles from the frontend package rather than a relative path."""
    monkeypatch.chdir(tmp_path)

    api = importlib.import_module("api")
    dashboard = next(
        route
        for route in api.app.routes
        if isinstance(route, Mount) and route.name == "frontend"
    )

    assert isinstance(dashboard.app, StaticFiles)
    assert dashboard.app.directory is not None
    assert Path(dashboard.app.directory) == Path(str(files("frontend")))
