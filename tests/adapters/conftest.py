# tests/adapters/conftest.py
"""Isolate runtime fencing and logs for selected transport suites."""

from pathlib import Path

import pytest

from utils import logging_config, runtime_ownership

_ISOLATED_TRANSPORT_MODULES: frozenset[str] = frozenset(
    {
        "test_cli_task_completion.py",
        "test_cli_workflow_domain.py",
        "test_api_workflow_domain.py",
        "test_task_completion_repository_transport.py",
        "test_api_sprint_retry.py",
        "test_cli_sprint_retry.py",
        "test_cli_sprint_triage.py",
        "test_command_renderer.py",
        "test_vision_bootstrap_api.py",
        "test_vision_bootstrap_cli.py",
    }
)


@pytest.fixture(autouse=True)
def _isolate_transport_runtime_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Retain the runtime fence while confining its files to disposable state."""
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
