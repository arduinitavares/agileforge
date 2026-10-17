# tests/services/conftest.py
"""Confine selected service transport tests' runtime files to disposable state."""

from pathlib import Path

import pytest

from utils import logging_config, runtime_ownership

_ISOLATED_TRANSPORT_MODULES: frozenset[str] = frozenset(
    {
        "test_story_sprint_selection.py",
        "test_story_validation_application.py",
    }
)


@pytest.fixture(autouse=True)
def _isolate_service_transport_runtime_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Keep the real runtime fence and all log paths inside the test's directory."""
    if request.path.name not in _ISOLATED_TRANSPORT_MODULES:
        return
    runtime_root = tmp_path.resolve()
    logs_root = runtime_root / "logs"

    def isolated_runtime_roots() -> tuple[Path, ...]:
        return (runtime_root,)

    monkeypatch.setattr(runtime_ownership, "runtime_roots", isolated_runtime_roots)
    monkeypatch.setattr(logging_config, "LOGS_DIR", logs_root)
    monkeypatch.setattr(logging_config, "APP_LOG_PATH", logs_root / "app.log")
    monkeypatch.setattr(logging_config, "ERROR_LOG_PATH", logs_root / "error.log")
