"""Application entrypoints respect maintenance and authoritative identity."""

from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

import api
from cli import dev_main, repository_transfer
from cli import main as product_cli
from models import db
from utils import runtime_ownership
from utils.runtime_fence import FenceError, runtime_fence

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX maintenance fencing")
CLI_ERROR = 2

if TYPE_CHECKING:
    from collections.abc import Iterator


def test_api_maintenance_refuses_before_schema_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exclusive owner prevents even startup schema writes."""
    initialized: list[bool] = []
    monkeypatch.setattr(runtime_ownership, "runtime_roots", lambda: (tmp_path,))
    monkeypatch.setattr(
        db, "ensure_business_db_ready", lambda: initialized.append(True)
    )

    async def startup() -> None:
        async with api.lifespan(api.app):
            pytest.fail("API started during maintenance")  # ty: ignore[invalid-argument-type]

    with runtime_fence(tmp_path, exclusive=True), pytest.raises(FenceError):
        asyncio.run(startup())
    assert initialized == []


def test_api_holds_fence_through_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Maintenance remains blocked for the entire service lifetime."""
    monkeypatch.setattr(runtime_ownership, "runtime_roots", lambda: (tmp_path,))
    monkeypatch.setattr(db, "ensure_business_db_ready", lambda: None)

    async def run_service() -> None:
        async with api.lifespan(api.app):
            with pytest.raises(FenceError), runtime_fence(tmp_path, exclusive=True):
                pytest.fail(
                    "maintenance entered a running service"  # ty: ignore[invalid-argument-type]
                )

    asyncio.run(run_service())
    with runtime_fence(tmp_path, exclusive=True):
        pass


def test_cli_refuses_before_application_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI errors are structured and initialization stays behind the fence."""
    initialized: list[bool] = []
    monkeypatch.setattr(runtime_ownership, "runtime_roots", lambda: (tmp_path,))
    monkeypatch.setattr(
        product_cli, "production_application", lambda: initialized.append(True)
    )
    with runtime_fence(tmp_path, exclusive=True):
        result = product_cli.main(["project", "list"])
    assert result == CLI_ERROR
    assert initialized == []
    assert "runtime fence is busy" in capsys.readouterr().out


def test_production_readiness_uses_records_not_identity_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Spoofed identity variables cannot change readiness provenance."""
    build = SimpleNamespace(revision="a" * 40)
    state = SimpleNamespace(
        state_id="durable-state-id",
        business_database=tmp_path / "business.sqlite3",
        trace_database=tmp_path / "trace.sqlite3",
    )
    monkeypatch.setattr(api, "production_runtime_identity", lambda: (build, state))
    monkeypatch.setenv("AGILEFORGE_BUILD_REVISION", "b" * 40)
    monkeypatch.setenv("AGILEFORGE_STATE_ID", "spoofed")
    config = api.get_dashboard_config()
    assert config.commit == "a" * 40
    assert config.state_id == "durable-state-id"
    assert config.checkout_root == Path("/opt/agileforge")
    assert config.business_database == state.business_database


def test_production_environment_mismatch_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Selecting a real profile cannot authorize writing another database."""
    state = SimpleNamespace(
        business_database=tmp_path / "business.sqlite3",
        trace_database=tmp_path / "trace.sqlite3",
        model_config_path=tmp_path / "models.yaml",
    )
    monkeypatch.setenv("AGILEFORGE_PRODUCTION_PROFILE", "default")
    monkeypatch.setenv("AGILEFORGE_DB_URL", "sqlite:////wrong.sqlite3")
    monkeypatch.setattr(runtime_ownership, "runtime_build_identity", object)
    monkeypatch.setattr(
        runtime_ownership, "load_production_state", lambda *_args, **_kwargs: state
    )
    with pytest.raises(ValueError, match="AGILEFORGE_DB_URL"):
        runtime_ownership.production_runtime_identity()


def test_dev_launcher_holds_shared_fence_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The launcher fences initialization and its child process lifetime."""
    entries: list[str] = []

    @contextmanager
    def fence(_root: Path) -> Iterator[None]:
        entries.append("enter")
        yield
        entries.append("exit")

    def initialize(**_kwargs: object) -> None:
        assert entries == ["enter"]
        entries.append("initialize")

    monkeypatch.setattr(dev_main, "development_access", fence)
    monkeypatch.setattr(dev_main, "resolve_checkout_root", lambda _root: tmp_path)
    monkeypatch.setattr(dev_main, "_initialize_profile", initialize)
    monkeypatch.setattr(dev_main, "_emit_init", lambda *_args, **_kwargs: None)
    assert dev_main.main(["init", "--profile", "test"]) == 0
    assert entries == ["enter", "initialize", "exit"]


def test_native_installed_runtime_fences_database_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pre-cutover installed CLI must participate in the same maintenance lock."""
    monkeypatch.setattr(
        runtime_ownership,
        "__file__",
        str(tmp_path / "wheel/utils/runtime_ownership.py"),
    )
    monkeypatch.delenv("AGILEFORGE_PRODUCTION_PROFILE", raising=False)
    monkeypatch.setenv("AGILEFORGE_DB_URL", f"sqlite:///{tmp_path}/business.sqlite3")
    monkeypatch.setenv(
        "AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL", f"sqlite:///{tmp_path}/trace.sqlite3"
    )
    from utils.runtime_config import clear_runtime_config_cache  # noqa: PLC0415

    clear_runtime_config_cache()
    try:
        assert runtime_ownership.runtime_roots() == (tmp_path,)
        with (
            runtime_fence(tmp_path, exclusive=True),
            pytest.raises(FenceError),
            runtime_ownership.runtime_access(),
        ):
            pytest.fail("installed writer bypassed maintenance")  # ty: ignore[invalid-argument-type]
    finally:
        clear_runtime_config_cache()


def test_runtime_blocks_pending_relocation_before_application_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restored profile must not resume writes against old repository paths."""
    monkeypatch.setattr(runtime_ownership, "runtime_roots", lambda: (tmp_path,))
    monkeypatch.setattr(runtime_ownership, "runtime_output_root", lambda: tmp_path)
    monkeypatch.setattr(runtime_ownership, "production_runtime_identity", lambda: None)
    (tmp_path / "repository-relocations.json").write_text("{}")
    relocation = repository_transfer.RepositoryRelocation(1, "/old", "/restored")
    monkeypatch.setattr(
        repository_transfer, "pending_relocations", lambda *_: (relocation,)
    )
    with (
        pytest.raises(FenceError, match="repository relocation"),
        runtime_ownership.runtime_access(),
    ):
        pytest.fail("runtime accepted pending relocation")  # ty: ignore[invalid-argument-type]
    with runtime_ownership.runtime_access(allow_repository_attach=True):
        pass
