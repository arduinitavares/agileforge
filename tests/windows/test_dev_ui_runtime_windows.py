"""Real Windows venv UI ownership and readiness regressions."""

from __future__ import annotations

import json
import shutil
import socket
import sys
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import TYPE_CHECKING
from urllib.request import urlopen

import psutil
import pytest
from git import Repo
from sqlmodel import create_engine

from cli import dev_main as main
from cli import dev_server as server
from cli.dev_profiles import (
    initialize_profile_record,
    profile_paths,
    reset_profile,
)
from models.db import ensure_business_db_ready

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from cli.dev_profiles import RuntimeProfile

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows interpreter ownership"),
    pytest.mark.allow_hosts(["127.0.0.1"]),
]


@pytest.fixture
def profile(tmp_path: Path) -> Iterator[RuntimeProfile]:
    """Copy only source inputs into a synthetic checkout; create fresh DBs."""
    source = Path(main.__file__).resolve().parents[1]
    root = tmp_path / "checkout"
    root.mkdir()
    for relative in ("api.py", "agile_sqlmodel.py", "config/models.yaml"):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    # A fixture-only route proves that launch preserves this process's venv.
    with (root / "api.py").open("a", encoding="utf-8") as stream:
        stream.write(
            '\n@app.get("/__test/runtime")\n'
            "def fixture_runtime():\n"
            "    import sys\n"
            '    return {"executable": sys.executable, "prefix": sys.prefix,\n'
            '            "launcher_present": "__PYVENV_LAUNCHER__" in os.environ}\n'
        )
    (root / ".gitignore").write_text(".agileforge/\n__pycache__/\n", encoding="utf-8")
    with Repo.init(root) as repo:
        with repo.config_writer() as config:
            config.set_value("user", "name", "UI Runtime Test")
            config.set_value("user", "email", "ui-runtime@example.invalid")
        repo.index.add(
            ["api.py", "agile_sqlmodel.py", "config/models.yaml", ".gitignore"]
        )
        repo.index.commit("synthetic UI runtime fixture")
    result = initialize_profile_record(root, "owned")
    engine = create_engine(f"sqlite:///{result.business_database.as_posix()}")
    try:
        ensure_business_db_ready(engine)
    finally:
        engine.dispose()
    sentinel = profile_paths(root, result.name).artifacts / "sentinel.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    try:
        yield result
    finally:
        assert sentinel.read_text(encoding="utf-8") == "preserve"
        reset_profile(root, result.name, result.name)
        assert not profile_paths(root, result.name).root.exists()


def _environment(profile: RuntimeProfile, nonce: str) -> dict[str, str]:
    environment = main._launcher_child_environment(profile)
    environment["AGILEFORGE_UI_LAUNCH_NONCE"] = nonce
    assert "OPEN_ROUTER_API_KEY" not in environment
    return environment


def _expected(
    profile: RuntimeProfile, child: server.UIChild, nonce: str, *, reload: bool = False
) -> server.ExpectedUIRuntime:
    return server.ExpectedUIRuntime(
        checkout_root=profile.checkout.root,
        commit=profile.checkout.commit,
        business_database=profile.business_database,
        trace_database=profile.trace_database,
        process_id=None if reload else child.process.pid,
        launch_nonce=nonce,
    )


def _processes(child: server.UIChild) -> list[psutil.Process]:
    try:
        process = psutil.Process(child.process.pid)
        return [process, *process.children(recursive=True)]
    except psutil.NoSuchProcess:
        return []


def _assert_stopped(processes: list[psutil.Process]) -> None:
    _gone, alive = psutil.wait_procs(processes, timeout=5)
    assert not alive, f"owned processes survived cleanup: {[p.pid for p in alive]}"


def _assert_port_closed(port: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2.5):
                pass
        except ConnectionRefusedError:
            return
        time.sleep(0.05)
    message = f"owned endpoint survived cleanup on port {port}"
    raise AssertionError(message)


def test_real_windows_ui_owns_the_serving_interpreter(profile: RuntimeProfile) -> None:
    """Fail on the redirector PID mismatch; pass without changing validation."""
    assert sys.version_info[:3] == (3, 13, 15)
    assert sys.executable != sys._base_executable  # ty: ignore[unresolved-attribute]
    port = server.select_loopback_port()
    child = server.start_ui(
        checkout_root=profile.checkout.root,
        environment=_environment(profile, "owned-nonce"),
        port=port,
        reload=False,
    )
    owned = _processes(child)
    try:
        config = server.wait_for_readiness(
            child, expected=_expected(profile, child, "owned-nonce"), timeout=30
        )
        owned = _processes(child)
        assert config.process_id == child.process.pid
        assert len(owned) == 1
        with urlopen(f"{child.url}/__test/runtime", timeout=2) as response:  # noqa: S310  # nosec B310
            runtime = json.load(response)
        assert Path(runtime["executable"]) == Path(sys.executable)
        assert Path(runtime["prefix"]) == Path(sys.prefix)
        assert runtime["launcher_present"] is False
    finally:
        # Also capture the redirector's child on the intentionally failing run.
        if child.process.poll() is None:
            owned = _processes(child)
        server.stop_ui(child)
        _assert_stopped(owned)
        _assert_port_closed(port)


@pytest.mark.parametrize("port_mode", ["fixed", "auto"])
def test_real_ui_lifecycle_starts_once_and_cleans_up(
    profile: RuntimeProfile, monkeypatch: pytest.MonkeyPatch, port_mode: str
) -> None:
    """The production API passes the real lifecycle without retrying its own PID."""
    children: list[server.UIChild] = []
    processes: list[psutil.Process] = []
    original_start = main.start_ui

    def capture_start(
        *, checkout_root: Path, environment: Mapping[str, str], port: int, reload: bool
    ) -> server.UIChild:
        child = original_start(
            checkout_root=checkout_root,
            environment=environment,
            port=port,
            reload=reload,
        )
        children.append(child)
        return child

    monkeypatch.setattr(main, "start_ui", capture_start)
    port = "auto" if port_mode == "auto" else str(server.select_loopback_port())
    request = main.UiRequest(profile.name, None, False, port, False, True, 30)
    try:
        with main._ready_ui_lifecycle(
            profile=profile,
            current_commit=profile.checkout.commit,
            environment=main._launcher_child_environment(profile),
            request=request,
        ) as (child, nonce):
            processes = _processes(child)
            assert nonce
            assert len(children) == 1
            config = server.wait_for_readiness(
                child, expected=_expected(profile, child, nonce), timeout=2
            )
            assert config.process_id == child.process.pid
    finally:
        for child in children:
            server.stop_ui(child)
            _assert_port_closed(child.port)
        _assert_stopped(processes)


@pytest.mark.parametrize(
    "field",
    [
        "process_id",
        "launch_nonce",
        "commit",
        "checkout_root",
        "business_database",
        "trace_database",
    ],
)
def test_foreign_identity_rejected_without_stopping_foreign_server(
    profile: RuntimeProfile, field: str
) -> None:
    """Real foreign server remains live after exact identity rejection."""
    foreign = server.start_ui(
        checkout_root=profile.checkout.root,
        environment=_environment(profile, "foreign-nonce"),
        port=server.select_loopback_port(),
        reload=False,
    )
    owned: server.UIChild | None = None
    processes = _processes(foreign)
    try:
        correct = _expected(profile, foreign, "foreign-nonce")
        server.wait_for_readiness(foreign, expected=correct, timeout=30)
        owned = server.start_ui(
            checkout_root=profile.checkout.root,
            environment=_environment(profile, "owned-nonce"),
            port=server.select_loopback_port(),
            reload=False,
        )
        server.wait_for_readiness(
            owned, expected=_expected(profile, owned, "owned-nonce"), timeout=30
        )
        owned_processes = _processes(owned)
        wrong = {
            "process_id": replace(correct, process_id=owned.process.pid),
            "launch_nonce": replace(correct, launch_nonce="owned-nonce"),
            "commit": replace(correct, commit="0" * 40),
            "checkout_root": replace(
                correct, checkout_root=profile.checkout.root.parent
            ),
            "business_database": replace(
                correct,
                business_database=profile.business_database.with_name(
                    "foreign.sqlite3"
                ),
            ),
            "trace_database": replace(
                correct,
                trace_database=profile.trace_database.with_name("foreign.sqlite3"),
            ),
        }
        expected = wrong[field]
        # The tracked owned process probes the foreign server's address.
        probe = server.UIChild(owned.process, foreign.port)
        with pytest.raises(server.UIRuntimeMismatchError, match="identity mismatch"):
            server.wait_for_readiness(probe, expected=expected, timeout=2)
        server.stop_ui(probe)
        _assert_stopped(owned_processes)
        _assert_port_closed(owned.port)
        assert foreign.process.poll() is None
        server.wait_for_readiness(foreign, expected=correct, timeout=2)
    finally:
        if owned is not None:
            server.stop_ui(owned)
        server.stop_ui(foreign)
        _assert_stopped(processes)
        _assert_port_closed(foreign.port)


def test_port_cleanup_rejects_a_live_http_error_listener() -> None:
    """An HTTP failure must never count as a closed TCP listener."""

    class UnhealthyHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            """Return an error from a server that is still listening."""
            self.send_response(503)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            """Keep the synthetic server silent."""

    with HTTPServer(("127.0.0.1", 0), UnhealthyHandler) as listener:
        worker = Thread(target=listener.serve_forever, daemon=True)
        worker.start()
        try:
            with pytest.raises(AssertionError, match="owned endpoint survived cleanup"):
                _assert_port_closed(listener.server_port)
        finally:
            listener.shutdown()
            worker.join(timeout=2)
            assert not worker.is_alive()
