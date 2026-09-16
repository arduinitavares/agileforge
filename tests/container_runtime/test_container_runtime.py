"""Explicit installed-runtime lifecycle behavior."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import pytest
from git import Actor, Repo

from cli import container_runtime
from cli.container_runtime import (
    ContainerRuntimeError,
    backup_production_state,
    build_parser,
    load_runtime_secrets,
    main,
    production_environment,
    restore_production_state,
    run_product_cli,
    serve_production,
)
from cli.dev_server import CONTAINER_HOST, UIChild, UIRuntimeMismatchError
from cli.production_state import (
    ProductionStateError,
    ProductionStateManifest,
    initialize_production_state,
    load_production_state,
)
from cli.repository_transfer import pending_relocations
from cli.state_transfer import TransferError, verify_backup
from utils.build_identity import BuildIdentity
from utils.runtime_fence import runtime_fence

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="installed production runtime is supported only in Linux containers",
)
_ERROR_EXIT = 2


def _current_uid() -> int:
    """Return the current POSIX user ID used by Linux-only fixtures."""
    getter = cast("Callable[[], int] | None", getattr(os, "getuid", None))
    if getter is None:
        message = "Linux-only fixture requires a POSIX user ID"
        raise RuntimeError(message)
    return getter()


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


def _stop_ui(child: UIChild) -> None:
    """Provide a typed no-op lifecycle cleanup fixture."""
    del child


def _state(root: Path) -> ProductionStateManifest:
    return ProductionStateManifest(
        schema_version="agileforge.production-state.v1",
        state_id=UUID("d931ea27-e9b5-4cba-b047-84570d2de24f"),
        profile_name="default",
        profile_root=root,
        business_database=root / "business.sqlite3",
        trace_database=root / "adk-trace.sqlite3",
        trace_database_present=False,
        artifacts=root / "artifacts",
        model_config_path=root / "config" / "models.yaml",
        model_config_sha256="c" * 64,
        business_schema_sha256="d" * 64,
        created_at=datetime(2026, 9, 16, tzinfo=UTC),
        created_by_build_revision="1" * 40,
        provenance="initialized",
        source_backup_sha256=None,
        repository_relocations_sha256=None,
    )


def _committed_repository(path: Path) -> Repo:
    repository = Repo.init(path)
    (path / "tracked.txt").write_text("committed\n", encoding="utf-8")
    repository.index.add(["tracked.txt"])
    actor = Actor("Synthetic fixture", "fixture@example.invalid")
    repository.index.commit("initial", author=actor, committer=actor)
    return repository


def _register_active_repository(database: Path, repository: Repo) -> int:
    worktree = Path(repository.working_tree_dir or "").resolve(strict=True)
    with sqlite3.connect(database) as connection:
        project = connection.execute(
            "INSERT INTO projects (name) VALUES (?) RETURNING project_id",
            ("synthetic repository project",),
        ).fetchone()
        assert project is not None
        project_id = int(project[0])
        binding = connection.execute(
            "INSERT INTO repository_bindings "
            "(project_id, worktree_path, common_git_dir, head_sha, branch_name, "
            "detached_head, dirty, status_fingerprint, status_entries_json, "
            "remotes_json, warnings_json, probe_version, inspected_at, recorded_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "RETURNING repository_binding_id",
            (
                project_id,
                str(worktree),
                str(Path(repository.common_dir).resolve(strict=True)),
                repository.head.commit.hexsha,
                repository.active_branch.name,
                False,
                False,
                "synthetic-status",
                "[]",
                "[]",
                "[]",
                "synthetic.v1",
                "2026-09-16T00:00:00+00:00",
                "synthetic-test",
            ),
        ).fetchone()
        assert binding is not None
        connection.execute(
            "UPDATE projects SET active_repository_binding_id = ? WHERE project_id = ?",
            (int(binding[0]), project_id),
        )
        return project_id


def _record_relocated_binding(
    database: Path,
    *,
    project_id: int,
    repository: Repo,
) -> None:
    worktree = Path(repository.working_tree_dir or "").resolve(strict=True)
    with sqlite3.connect(database) as connection:
        active = connection.execute(
            "SELECT active_repository_binding_id FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        assert active is not None
        assert active[0] is not None
        binding = connection.execute(
            "INSERT INTO repository_bindings "
            "(project_id, worktree_path, common_git_dir, head_sha, branch_name, "
            "detached_head, dirty, status_fingerprint, status_entries_json, "
            "remotes_json, warnings_json, probe_version, inspected_at, "
            "supersedes_repository_binding_id, recorded_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "RETURNING repository_binding_id",
            (
                project_id,
                str(worktree),
                str(Path(repository.common_dir).resolve(strict=True)),
                repository.head.commit.hexsha,
                repository.active_branch.name,
                False,
                False,
                "synthetic-relocated-status",
                "[]",
                "[]",
                "[]",
                "synthetic.v1",
                "2026-09-16T00:00:01+00:00",
                int(active[0]),
                "synthetic-guarded-attach",
            ),
        ).fetchone()
        assert binding is not None
        connection.execute(
            "UPDATE projects SET active_repository_binding_id = ? WHERE project_id = ?",
            (int(binding[0]), project_id),
        )


@pytest.mark.parametrize(
    ("argv", "command"),
    [
        (["init", "--profile", "default"], "init"),
        (["info", "--profile", "default"], "info"),
        (["serve", "--profile", "default"], "serve"),
        (["cli", "--profile", "default", "--", "status"], "cli"),
        (
            ["backup", "--profile", "default", "--destination", "/backup"],
            "backup",
        ),
        (
            ["restore", "--profile", "default", "--bundle", "/backup"],
            "restore",
        ),
    ],
)
def test_parser_exposes_only_explicit_lifecycle_commands(
    argv: list[str],
    command: str,
) -> None:
    """Every installed-state mutation or use must name its lifecycle operation."""
    parser = build_parser()

    arguments = parser.parse_args(argv)

    assert arguments.command == command
    assert arguments.profile == "default"


def test_parser_refuses_implicit_default_service() -> None:
    """An empty invocation must not silently initialize or serve state."""
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_production_environment_does_not_inherit_ambient_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host credentials must not cross the installed child boundary."""
    monkeypatch.setenv("OPEN_ROUTER_API_KEY", "ambient-secret-canary")
    monkeypatch.setenv("UNRELATED_SECRET", "another-canary")
    root = tmp_path / "default"

    environment = production_environment(_state(root), _build(), secrets={})

    assert "ambient-secret-canary" not in repr(environment)
    assert "another-canary" not in repr(environment)
    assert "AGILEFORGE_BUILD_REVISION" not in environment
    assert "AGILEFORGE_STATE_ID" not in environment
    assert environment["AGILEFORGE_PRODUCTION_PROFILE"] == "default"
    assert environment["AGILEFORGE_LAUNCHER_CHILD"] == "1"
    assert Path(environment["GIT_PYTHON_GIT_EXECUTABLE"]).is_absolute()
    assert environment["MODEL_CONFIG_PATH"] == str(root / "config" / "models.yaml")


def test_secret_file_requires_private_runtime_user_ownership(tmp_path: Path) -> None:
    """Group-readable runtime credentials must fail before a child starts."""
    secret_file = tmp_path / "agileforge"
    secret_file.write_text(
        "OPEN_ROUTER_API_KEY=runtime-secret-canary\n",
        encoding="utf-8",
    )
    secret_file.chmod(0o640)

    with pytest.raises(ContainerRuntimeError, match="private"):
        load_runtime_secrets(secret_file, expected_owner_uid=_current_uid())


def test_secret_file_accepts_only_supported_credentials(tmp_path: Path) -> None:
    """Unexpected dotenv keys must not become an accidental environment channel."""
    secret_file = tmp_path / "agileforge"
    secret_file.write_text(
        "DATABASE_PASSWORD=runtime-secret-canary\n",
        encoding="utf-8",
    )
    secret_file.chmod(0o600)

    with pytest.raises(ContainerRuntimeError, match="unsupported credential"):
        load_runtime_secrets(secret_file, expected_owner_uid=_current_uid())


class _FinishedProcess:
    pid: int = 4242
    returncode: int | None = 0

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9


def test_serve_uses_container_listener_owned_group_and_full_identity(
    tmp_path: Path,
) -> None:
    """Production readiness must bind externally but probe exact local identity."""
    root = tmp_path / "default"
    state = _state(root)
    captured: dict[str, Any] = {}

    def start(**kwargs: object) -> UIChild:
        captured["start"] = kwargs
        return UIChild(process=_FinishedProcess(), port=8765)

    def ready(child: UIChild, *, expected: object, timeout: float) -> None:
        captured["child"] = child
        captured["expected"] = expected
        captured["timeout"] = timeout

    exit_code = serve_production(
        state,
        _build(),
        secrets={"OPEN_ROUTER_API_KEY": "serve-secret-canary"},
        port=8765,
        ready_timeout=3.0,
        start=start,
        wait_ready=ready,
        stop=_stop_ui,
    )

    assert exit_code == 0
    assert captured["start"]["host"] == CONTAINER_HOST
    assert captured["start"]["owned_process_group"] is True
    assert captured["start"]["reload"] is False
    assert captured["start"]["redact_values"] == ("serve-secret-canary",)
    expected = captured["expected"]
    assert expected.commit == "1" * 40
    assert expected.state_id == str(state.state_id)
    assert expected.business_database == state.business_database
    assert (
        expected.launch_nonce
        == captured["start"]["environment"]["AGILEFORGE_UI_LAUNCH_NONCE"]
    )


def test_foreign_readiness_cleans_owned_process(tmp_path: Path) -> None:
    """A foreign response must stop the complete owned child group."""
    child = UIChild(process=_FinishedProcess(), port=8765)
    stopped: list[UIChild] = []

    def reject(*_args: object, **_kwargs: object) -> None:
        message = "foreign readiness"
        raise UIRuntimeMismatchError(message)

    def stop(child: UIChild) -> None:
        stopped.append(child)

    with pytest.raises(UIRuntimeMismatchError, match="foreign readiness"):
        serve_production(
            _state(tmp_path / "default"),
            _build(),
            secrets={},
            port=8765,
            ready_timeout=3.0,
            start=lambda **_kwargs: child,
            wait_ready=reject,
            stop=stop,
        )

    assert stopped == [child]


def test_cli_redacts_runtime_secret_from_child_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A product error must not echo the runtime-only credential canary."""
    credential_canary = "runtime-secret-canary"
    root = tmp_path / "default"
    root.mkdir()
    script = (
        "import os, sys; "
        "value = os.environ['OPEN_ROUTER_API_KEY']; "
        "print('failure ' + value); "
        "sys.stderr.write('detail ' + value + '\\n'); "
        "raise SystemExit(2)"
    )

    exit_code = run_product_cli(
        _state(root),
        _build(),
        forwarded=("--", "status"),
        secrets={"OPEN_ROUTER_API_KEY": credential_canary},
        child_arguments=(sys.executable, "-c", script),
    )

    captured = capsys.readouterr()
    assert exit_code == _ERROR_EXIT
    assert credential_canary not in captured.out
    assert credential_canary not in captured.err
    assert "[REDACTED]" in captured.out
    assert "[REDACTED]" in captured.err


def test_cli_cleans_descendant_after_leader_exit(tmp_path: Path) -> None:
    """A CLI leader exiting early must not leave its owned descendant alive."""
    root = tmp_path / "default"
    root.mkdir()
    child_pid_file = tmp_path / "child.pid"
    script = (
        "import pathlib, subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(60)']); "
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid)); "
        "raise SystemExit(0)"
    )

    exit_code = run_product_cli(
        _state(root),
        _build(),
        forwarded=("--", "status"),
        secrets={},
        child_arguments=(sys.executable, "-c", script),
    )

    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        os.kill(child_pid, int(getattr(signal, "SIGKILL", 9)))
        message = "owned CLI descendant survived leader cleanup"
        raise AssertionError(message)
    assert exit_code == 0


def test_info_refuses_missing_state_without_initializing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A read command against a missing profile must leave the root untouched."""
    build_path = tmp_path / "build.json"
    build_path.write_text(_build().model_dump_json(), encoding="utf-8")
    build_path.chmod(0o444)

    exit_code = main(
        ["info", "--profile", "default", "--json"],
        deployment_root=tmp_path,
        build_path=build_path,
        expected_build_owner_uid=_current_uid(),
        expected_state_owner_uid=_current_uid(),
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == _ERROR_EXIT
    assert payload["ok"] is False
    assert not (tmp_path / "profiles" / "default").exists()


def test_init_requires_exclusive_maintenance_fence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Initialization must not race a live service or state reader."""
    build_path = tmp_path / "build.json"
    build_path.write_text(_build().model_dump_json(), encoding="utf-8")
    build_path.chmod(0o444)
    model_config = tmp_path / "models.yaml"
    model_config.write_text("models:\n  default: test/model\n", encoding="utf-8")
    model_config.chmod(0o600)

    with runtime_fence(tmp_path):
        exit_code = main(
            [
                "init",
                "--profile",
                "default",
                "--model-config",
                str(model_config),
                "--json",
            ],
            deployment_root=tmp_path,
            build_path=build_path,
            expected_build_owner_uid=_current_uid(),
            expected_state_owner_uid=_current_uid(),
        )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == _ERROR_EXIT
    assert payload["ok"] is False
    assert not (tmp_path / "profiles" / "default").exists()


def test_init_then_info_preserves_durable_state_id(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Explicit initialization publishes one identity reused by later reads."""
    build_path = tmp_path / "build.json"
    build_path.write_text(_build().model_dump_json(), encoding="utf-8")
    build_path.chmod(0o444)
    model_config = tmp_path / "models.yaml"
    model_config.write_text("models:\n  default: test/model\n", encoding="utf-8")
    model_config.chmod(0o600)
    common = {
        "deployment_root": tmp_path,
        "build_path": build_path,
        "expected_build_owner_uid": _current_uid(),
        "expected_state_owner_uid": _current_uid(),
    }

    init_exit = main(
        [
            "init",
            "--profile",
            "default",
            "--model-config",
            str(model_config),
            "--json",
        ],
        **common,
    )
    initialized = json.loads(capsys.readouterr().out)
    info_exit = main(["info", "--profile", "default", "--json"], **common)
    observed = json.loads(capsys.readouterr().out)

    assert init_exit == 0
    assert info_exit == 0
    assert observed["state"]["state_id"] == initialized["state"]["state_id"]
    assert not (tmp_path / "profiles" / "default" / "adk-trace.sqlite3").exists()


def test_backup_restore_preserves_state_identity_and_absent_trace(
    tmp_path: Path,
) -> None:
    """Transfer rebases owned paths while keeping durable state identity."""
    build = _build()
    model_config = tmp_path / "models.yaml"
    model_config.write_text("models:\n  default: test/model\n", encoding="utf-8")
    model_config.chmod(0o600)
    source_root = tmp_path / "profiles" / "default"
    source = initialize_production_state(
        source_root,
        build=build,
        model_config_source=model_config,
        expected_owner_uid=_current_uid(),
    )
    (source.artifacts / "synthetic.txt").write_text("synthetic-only", encoding="utf-8")

    bundle = backup_production_state(
        source,
        tmp_path / "backup",
        deployment_root=tmp_path,
    )
    restored = restore_production_state(
        bundle,
        tmp_path / "profiles" / "restored",
        build=build,
        deployment_root=tmp_path,
        expected_owner_uid=_current_uid(),
    )

    assert restored.state_id == source.state_id
    assert restored.profile_name == "restored"
    assert restored.provenance == "restored"
    assert (restored.artifacts / "synthetic.txt").read_text(encoding="utf-8") == (
        "synthetic-only"
    )
    assert not restored.trace_database.exists()
    assert (
        load_production_state(
            restored.profile_root,
            build=build,
            expected_owner_uid=_current_uid(),
        ).state_id
        == source.state_id
    )


def test_restore_rejects_unknown_schema_before_destination_publication(
    tmp_path: Path,
) -> None:
    """A self-consistent transfer cannot introduce an unsupported DB schema."""
    build = _build()
    model_config = tmp_path / "models.yaml"
    model_config.write_text("models:\n  default: test/model\n", encoding="utf-8")
    model_config.chmod(0o600)
    source = initialize_production_state(
        tmp_path / "profiles" / "default",
        build=build,
        model_config_source=model_config,
        expected_owner_uid=_current_uid(),
    )
    with sqlite3.connect(source.business_database) as connection:
        connection.execute("CREATE TABLE unsupported_payload (value TEXT)")

    bundle = backup_production_state(
        source,
        tmp_path / "backup",
        deployment_root=tmp_path,
    )
    destination = tmp_path / "profiles" / "rejected"

    with pytest.raises(TransferError, match="unsupported current business schema"):
        restore_production_state(
            bundle,
            destination,
            build=build,
            deployment_root=tmp_path,
            expected_owner_uid=_current_uid(),
        )

    assert not destination.exists()


def test_restore_rejects_model_hash_mismatch_before_destination_publication(
    tmp_path: Path,
) -> None:
    """Transfer integrity cannot replace the configured model provenance hash."""
    build = _build()
    model_config = tmp_path / "models.yaml"
    model_config.write_text("models:\n  default: test/model\n", encoding="utf-8")
    model_config.chmod(0o600)
    source = initialize_production_state(
        tmp_path / "profiles" / "default",
        build=build,
        model_config_source=model_config,
        expected_owner_uid=_current_uid(),
    )
    source.model_config_path.write_text(
        "models:\n  default: changed/model\n",
        encoding="utf-8",
    )

    bundle = backup_production_state(
        source,
        tmp_path / "backup",
        deployment_root=tmp_path,
    )
    destination = tmp_path / "profiles" / "rejected"

    with pytest.raises(ContainerRuntimeError, match="model configuration hash"):
        restore_production_state(
            bundle,
            destination,
            build=build,
            deployment_root=tmp_path,
            expected_owner_uid=_current_uid(),
        )

    assert not destination.exists()


def test_registered_repository_restore_requires_exact_guarded_reattachment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registered Git bytes move safely while product use waits for attachment."""
    build = _build()
    model_config = tmp_path / "models.yaml"
    model_config.write_text("models:\n  default: test/model\n", encoding="utf-8")
    model_config.chmod(0o600)
    source = initialize_production_state(
        tmp_path / "profiles" / "default",
        build=build,
        model_config_source=model_config,
        expected_owner_uid=_current_uid(),
    )
    (tmp_path / "workspace").mkdir()
    repository = _committed_repository(tmp_path / "workspace" / "registered")
    project_id = _register_active_repository(source.business_database, repository)

    bundle = backup_production_state(
        source,
        tmp_path / "backup",
        deployment_root=tmp_path,
    )
    transfer = verify_backup(bundle)
    assert len(transfer.repositories) == 1
    assert transfer.repositories[0]["source_path"] == str(
        Path(repository.working_tree_dir or "").resolve(strict=True)
    )
    restored = restore_production_state(
        bundle,
        tmp_path / "profiles" / "restored",
        build=build,
        deployment_root=tmp_path,
        expected_owner_uid=_current_uid(),
    )
    pending = pending_relocations(restored.business_database, restored.profile_root)
    assert len(pending) == 1
    assert pending[0].project_id == project_id
    restored_repository = Path(pending[0].restored_path)
    assert (restored_repository / "tracked.txt").read_text(encoding="utf-8") == (
        "committed\n"
    )
    assert restored.repository_relocations_sha256 is not None

    def should_not_start(**_kwargs: object) -> UIChild:
        message = "blocked serve started a child"
        raise AssertionError(message)

    with pytest.raises(ContainerRuntimeError, match="pending repository relocation"):
        serve_production(
            restored,
            build,
            secrets={},
            port=8765,
            ready_timeout=1.0,
            start=should_not_start,
        )
    with pytest.raises(ContainerRuntimeError, match="guarded attach"):
        run_product_cli(
            restored,
            build,
            forwarded=("--", "project", "list"),
            secrets={},
            child_arguments=(sys.executable, "-c", "raise SystemExit(0)"),
        )

    with monkeypatch.context() as patch:
        observations = iter((pending, ()))
        patch.setattr(
            container_runtime,
            "pending_relocations",
            lambda *_args: next(observations),
        )
        patch.setattr(
            container_runtime,
            "_run_owned_cli_child",
            lambda *_args, **_kwargs: 0,
        )
        assert (
            run_product_cli(
                restored,
                build,
                forwarded=(
                    "--",
                    "repository",
                    "attach",
                    "--project-id",
                    str(project_id),
                    "--path",
                    str(restored_repository),
                    "--idempotency-key",
                    "synthetic-relocation",
                    "--actor",
                    "synthetic-operator",
                ),
                secrets={},
            )
            == 0
        )

    _record_relocated_binding(
        restored.business_database,
        project_id=project_id,
        repository=Repo(restored_repository),
    )
    assert pending_relocations(restored.business_database, restored.profile_root) == ()

    child = UIChild(process=_FinishedProcess(), port=8765)
    assert (
        serve_production(
            restored,
            build,
            secrets={},
            port=8765,
            ready_timeout=1.0,
            start=lambda **_kwargs: child,
            wait_ready=lambda *_args, **_kwargs: None,
            stop=_stop_ui,
        )
        == 0
    )
    relocation_record = restored.profile_root / "repository-relocations.json"
    relocation_record.write_bytes(relocation_record.read_bytes() + b" ")
    with pytest.raises(ProductionStateError, match="relocation record drift"):
        load_production_state(
            restored.profile_root,
            build=build,
            expected_owner_uid=_current_uid(),
        )
