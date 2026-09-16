"""Container listeners, identity, and complete owned-process cleanup."""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404
import sys
import time
from contextlib import suppress
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from cli import dev_main, dev_server

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def test_container_listener_keeps_readiness_on_loopback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Wildcard listening must not turn the identity probe into a remote URL."""
    calls: list[tuple[str, ...]] = []

    def spawn(arguments: tuple[str, ...], **_options: object) -> SimpleNamespace:
        calls.append(arguments)
        return SimpleNamespace(pid=1234, returncode=None)

    monkeypatch.setattr(dev_server.subprocess, "Popen", spawn)
    child = dev_server.start_ui(
        checkout_root=tmp_path,
        environment={},
        port=8765,
        reload=False,
        host=dev_server.CONTAINER_HOST,
    )
    assert calls[0][calls[0].index("--host") + 1] == dev_server.CONTAINER_HOST
    assert child.url == "http://127.0.0.1:8765"


def test_launcher_accepts_explicit_container_listener() -> None:
    """The checkout launcher exposes the container listener deliberately."""
    arguments = dev_main.build_parser().parse_args(
        ["ui", "--profile", "local", "--host", dev_server.CONTAINER_HOST]
    )
    assert arguments.host == dev_server.CONTAINER_HOST
    default = dev_main.build_parser().parse_args(["ui", "--profile", "local"])
    assert default.host == "127.0.0.1"


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group ownership")
def test_loopback_launcher_also_owns_its_process_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Loopback and reload must not release the fence with descendants alive."""
    calls: list[dict[str, object]] = []

    def start(**kwargs: object) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(dev_main, "start_ui", start)
    monkeypatch.setattr(dev_main, "stop_ui", lambda _child: None)
    with dev_main._managed_ui_child(
        checkout_root=tmp_path, environment={}, port=8765, reload=True
    ):
        pass
    assert calls[0].get("owned_process_group") is True


@pytest.mark.parametrize("host", ["example.com", "192.168.1.1", "::", ""])
def test_listener_rejects_unapproved_addresses(host: str, tmp_path: Path) -> None:
    """Container mode does not open an arbitrary remote-listener interface."""
    with pytest.raises(ValueError, match="host"):
        dev_server.start_ui(
            checkout_root=tmp_path,
            environment={},
            port=8765,
            reload=False,
            host=host,
        )


def test_readiness_rejects_another_production_state(tmp_path: Path) -> None:
    """Matching image and database paths cannot hide a different state ID."""
    payload = {
        "status": "ready",
        "process_id": 1234,
        "checkout_root": str(tmp_path),
        "commit": "a" * 40,
        "business_database": str(tmp_path / "business.sqlite3"),
        "trace_database": str(tmp_path / "trace.sqlite3"),
        "launch_nonce": "nonce",
        "state_id": "another-state",
    }
    config = dev_server._parse_dashboard_config(payload)
    expected = dev_server.ExpectedUIRuntime(
        checkout_root=tmp_path,
        commit="a" * 40,
        business_database=tmp_path / "business.sqlite3",
        trace_database=tmp_path / "trace.sqlite3",
        process_id=1234,
        launch_nonce="nonce",
        state_id="expected-state",
    )
    with pytest.raises(dev_server.UIRuntimeMismatchError):
        dev_server._validate_runtime_identity(config, expected)


@pytest.fixture
def orphan_group(tmp_path: Path) -> Iterator[tuple[subprocess.Popen[str], int]]:
    """Create a real exited leader with a still-running owned descendant."""
    child_file = tmp_path / "child.json"
    child_code = "import time; time.sleep(60)"
    leader_code = (
        "import json, pathlib, subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', sys.argv[2]]); "
        "pathlib.Path(sys.argv[1]).write_text(json.dumps(child.pid))"
    )
    leader = subprocess.Popen(  # noqa: S603  # nosec B603
        [sys.executable, "-c", leader_code, str(child_file), child_code],
        start_new_session=True,
        text=True,
    )
    leader.wait(timeout=5)
    child_pid = int(json.loads(child_file.read_text(encoding="utf-8")))
    try:
        yield leader, child_pid
    finally:
        with suppress(ProcessLookupError):
            if hasattr(os, "killpg"):
                os.killpg(leader.pid, 9)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group ownership")
def test_shutdown_cleans_descendant_after_leader_exits(
    orphan_group: tuple[subprocess.Popen[str], int],
) -> None:
    """A reaped parent must not cause shutdown to skip its live child."""
    leader, child_pid = orphan_group
    process = dev_server.PosixProcessGroup(leader)
    assert process.poll() is None
    dev_server.stop_ui(dev_server.UIChild(process=process, port=0), timeout=5)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail("owned descendant survived shutdown")  # ty: ignore[invalid-argument-type]
