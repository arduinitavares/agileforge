# tests/container_runtime/test_provider_index_manifest.py
"""Installed profiles require explicit, verified provider-index upgrades."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess  # nosec B404 - bounded fake transport for rendered runtime hints.
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlmodel import create_engine

from cli import container_runtime
from cli.container_runtime import main
from cli.production_state import (
    ProductionStateError,
    ProductionStateManifest,
    database_schema_sha256,
    load_production_state,
    production_state_paths,
)
from cli.state_transfer import verify_backup
from models.db import ensure_business_db_ready
from utils.build_identity import BuildIdentity

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX profile ownership")
_FIXTURES: Path = Path(__file__).parents[1] / "fixtures"
_PRE_RETRY: Path = _FIXTURES / "issue_260" / "pre_retry_business_schema_da3dbf63.sql"
_PRIOR_CURRENT: Path = (
    _FIXTURES / "issue_230" / "prior_current_retry_additions_8b4ee1f.sql"
)
_ERROR_EXIT: int = 2
_GUARDED_UPGRADE_COMMAND: str = (
    'docker compose --project-name "${project_name:?set project_name to your '
    'selected Compose project}" run --rm production upgrade --profile default'
)


def _build() -> BuildIdentity:
    return BuildIdentity(
        schema_version="agileforge.build.v1",
        revision="1" * 40,
        source_sha256="a" * 64,
        lock_sha256="b" * 64,
        python_version="3.13.15",
        uv_version="0.12.8",
        package_version="0.1.0",
    )


def _prior_profile(deployment: Path, *, raw: bool = False) -> ProductionStateManifest:
    """Build the independently frozen prior-current profile without a child."""
    root = deployment / "profiles" / "default"
    root.mkdir(mode=0o700, parents=True)
    paths = production_state_paths(root)
    paths.artifacts.mkdir(mode=0o700)
    paths.config_directory.mkdir(mode=0o700)
    paths.model_config.write_text("models:\n  default: test/model\n", encoding="utf-8")
    paths.model_config.chmod(0o600)
    with sqlite3.connect(paths.business_database) as connection:
        ddl = (
            (_FIXTURES / "issue_230" / "master_b3a4fb4_raw_schema.sql").read_text(
                encoding="utf-8"
            )
            if raw
            else _PRE_RETRY.read_text(encoding="utf-8")
            + "\n"
            + _PRIOR_CURRENT.read_text(encoding="utf-8")
        )
        connection.executescript(ddl)
        connection.execute("INSERT INTO projects (name) VALUES ('History')")
    paths.business_database.chmod(0o600)
    state = ProductionStateManifest(
        schema_version="agileforge.production-state.v1",
        state_id=UUID("d931ea27-e9b5-4cba-b047-84570d2de24f"),
        profile_name=root.name,
        profile_root=root,
        business_database=paths.business_database,
        trace_database=paths.trace_database,
        trace_database_present=False,
        artifacts=paths.artifacts,
        model_config_path=paths.model_config,
        model_config_sha256=hashlib.sha256(paths.model_config.read_bytes()).hexdigest(),
        business_schema_sha256=database_schema_sha256(paths.business_database),
        created_at=datetime(2026, 9, 16, tzinfo=UTC),
        created_by_build_revision="1" * 40,
        provenance="initialized",
        source_backup_sha256=None,
        repository_relocations_sha256=None,
    )
    paths.manifest.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    paths.manifest.chmod(0o600)
    return state


def _run(deployment: Path, arguments: list[str]) -> int:
    build_path = deployment / "build.json"
    if not build_path.exists():
        build_path.write_text(_build().model_dump_json(), encoding="utf-8")
        build_path.chmod(0o444)
    try:
        return main(
            arguments,
            deployment_root=deployment,
            build_path=build_path,
            expected_build_owner_uid=os.getuid(),
            expected_state_owner_uid=os.getuid(),
        )
    except SystemExit as error:
        return int(error.code or 0)


def _api_startup(state: ProductionStateManifest, build: BuildIdentity) -> None:
    """Model the API's real schema ensure followed by strict readiness load."""
    engine = create_engine(f"sqlite:///{state.business_database.as_posix()}")
    try:
        ensure_business_db_ready(engine)
    finally:
        engine.dispose()
    load_production_state(state.profile_root, build=build)


def test_explicit_upgrade_publishes_indexes_before_startup_and_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Maintenance publishes once before shared-fence runtime commands start."""
    before = _prior_profile(tmp_path)
    observed: list[ProductionStateManifest] = []

    def fake_serve(
        state: ProductionStateManifest, build: BuildIdentity, **_options: object
    ) -> int:
        _api_startup(state, build)
        observed.append(state)
        return 0

    monkeypatch.setattr(container_runtime, "serve_production", fake_serve)
    monkeypatch.setattr(container_runtime, "run_product_cli", fake_serve)

    assert _run(tmp_path, ["upgrade", "--profile", "default", "--json"]) == 0

    assert _run(tmp_path, ["serve", "--profile", "default"]) == 0
    assert _run(tmp_path, ["info", "--profile", "default", "--json"]) == 0
    assert _run(tmp_path, ["cli", "--profile", "default"]) == 0

    after = load_production_state(before.profile_root, build=_build())
    assert observed == [after, after]
    assert after.business_schema_sha256 != before.business_schema_sha256
    assert after.model_dump(exclude={"business_schema_sha256"}) == before.model_dump(
        exclude={"business_schema_sha256"}
    )
    bundle = tmp_path / "backup"
    assert (
        _run(
            tmp_path,
            ["backup", "--profile", "default", "--destination", str(bundle), "--json"],
        )
        == 0
    )
    verify_backup(bundle)
    recorded = ProductionStateManifest.model_validate_json(
        (bundle / "provenance" / "runtime.json").read_bytes()
    )
    assert recorded == after
    assert database_schema_sha256(bundle / "business.sqlite3") == (
        recorded.business_schema_sha256
    )
    bundle_bytes = {
        path.relative_to(bundle): path.read_bytes()
        for path in bundle.rglob("*")
        if path.is_file()
    }
    restored = container_runtime.restore_production_state(
        bundle,
        tmp_path / "profiles" / "restored",
        build=_build(),
        deployment_root=tmp_path,
    )
    assert load_production_state(restored.profile_root, build=_build()) == restored
    assert restored.state_id == after.state_id
    assert restored.business_schema_sha256 == after.business_schema_sha256
    assert {
        path.relative_to(bundle): path.read_bytes()
        for path in bundle.rglob("*")
        if path.is_file()
    } == bundle_bytes


@pytest.mark.parametrize("command", ["info", "serve", "cli", "configure-models"])
def test_prior_schema_needs_actionable_explicit_upgrade(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    """Refuse prior schemas before a normal command can change state."""
    before = _prior_profile(tmp_path)
    original = (before.profile_root / "runtime.json").read_bytes()
    arguments = [command, "--profile", "default", "--json"]
    if command == "configure-models":
        arguments.extend(
            [
                "--model-config",
                str(before.model_config_path),
                "--backup-directory",
                str(tmp_path / "models-backup"),
            ]
        )
    assert _run(tmp_path, arguments) == _ERROR_EXIT
    payload = json.loads(capsys.readouterr().out)
    assert _GUARDED_UPGRADE_COMMAND in payload["error"]
    assert (
        database_schema_sha256(before.business_database)
        == before.business_schema_sha256
    )
    assert (before.profile_root / "runtime.json").read_bytes() == original
    with pytest.raises(
        ProductionStateError, match="production upgrade --profile default"
    ):
        load_production_state(before.profile_root, build=_build())


@pytest.mark.parametrize("project_name", ["agileforge-secondary", None, ""])
def test_rendered_upgrade_hint_requires_explicit_compose_project(
    tmp_path: Path, project_name: str | None
) -> None:
    """The rendered command cannot invoke Docker with an unset or empty project."""
    source = _prior_profile(tmp_path)
    with pytest.raises(ProductionStateError) as captured:
        load_production_state(source.profile_root, build=_build())
    message = str(captured.value)
    command = message[message.index("docker compose ") :]
    transport = tmp_path / "fake-transport"
    transport.mkdir()
    docker = transport / "docker"
    docker.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$FAKE_DOCKER_ARGS"\n',
        encoding="utf-8",
    )
    docker.chmod(0o700)
    recorded = tmp_path / "docker-arguments"
    environment = {"PATH": str(transport), "FAKE_DOCKER_ARGS": str(recorded)}
    if project_name is not None:
        environment["project_name"] = project_name
    result = subprocess.run(  # noqa: S603 - trusted synthetic-profile error command.
        ["/bin/sh", "-c", command],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if project_name:
        assert result.returncode == 0
        assert recorded.read_text(encoding="utf-8").splitlines() == [
            "compose",
            "--project-name",
            "agileforge-secondary",
            "run",
            "--rm",
            "production",
            "upgrade",
            "--profile",
            "default",
        ]
    else:
        assert result.returncode != 0
        assert not recorded.exists()
        assert "project_name" in result.stderr


def test_backup_accepts_a_verified_registered_prior_profile(tmp_path: Path) -> None:
    """Keep independently verified rollback available before explicit maintenance."""
    before = _prior_profile(tmp_path)
    bundle = tmp_path / "before-upgrade"
    assert (
        _run(tmp_path, ["backup", "--profile", "default", "--destination", str(bundle)])
        == 0
    )
    verify_backup(bundle)
    assert (bundle / "provenance" / "runtime.json").read_bytes() == (
        before.profile_root / "runtime.json"
    ).read_bytes()
    assert (
        database_schema_sha256(bundle / "business.sqlite3")
        == before.business_schema_sha256
    )
