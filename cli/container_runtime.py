"""Explicit lifecycle entrypoint for installed Linux production runtimes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets as secure_random
import shutil
import signal
import stat
import subprocess  # nosec B404
import sys
import threading
import uuid
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol, cast

from dotenv import dotenv_values
from pydantic import ValidationError

from cli.dev_secrets import SecretsFileError, open_secrets_file
from cli.dev_server import (
    CONTAINER_HOST,
    ExpectedUIRuntime,
    PosixProcessGroup,
    UIChild,
    start_ui,
    stop_ui,
    wait_for_readiness,
)
from cli.production_model_config import configure_models, recover_models
from cli.production_state import (
    PRODUCTION_STATE_BASE,
    ProductionStateError,
    ProductionStateManifest,
    database_schema_sha256,
    finalize_restored_production_state,
    initialize_production_state,
    load_production_state,
    prepare_profile_parent,
    production_state_paths,
)
from cli.repository_transfer import (
    RepositoryRelocation,
    RepositoryTransferError,
    finalize_restored_repositories,
    pending_relocations,
    validate_relocation_targets,
    write_relocation_record,
)
from cli.state_transfer import (
    StateLayout,
    TransferError,
    backup_state,
    restore_payload,
    verify_backup,
    verify_current_business_schema,
    verify_current_trace_schema,
)
from utils.build_identity import (
    BUILD_IDENTITY_PATH,
    BuildIdentity,
    BuildIdentityError,
    load_build_identity,
)
from utils.cli_output import emit
from utils.platform_support import (
    UNSUPPORTED_PLATFORM_EXIT_CODE,
    UnsupportedPlatformError,
    require_linux,
)
from utils.runtime_controls import (
    LAUNCHER_CHILD_ENV,
    LAUNCHER_CHILD_VALUE,
    UI_LAUNCH_NONCE_ENV,
)
from utils.runtime_fence import FenceError, runtime_fence
from utils.secret_redaction import redact_text

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from typing import BinaryIO, Never

DEFAULT_SECRET_PATH = Path("/run/secrets/agileforge")
INSTALLED_ROOT = Path("/opt/agileforge")
PRODUCTION_DEPLOYMENT_ROOT = Path("/var/lib/agileforge")
PRODUCTION_WORKSPACE_ROOT = Path("/workspace")
_SUPPORTED_CREDENTIALS = frozenset({"OPEN_ROUTER_API_KEY"})
_MAX_PORT = 65_535
_MAX_CHILD_OUTPUT_BYTES = 8 * 1024 * 1024
_COMMIT_LENGTH = 40
_HASH_LENGTH = 64


class ContainerRuntimeError(RuntimeError):
    """An installed-runtime command failed its production safety contract."""


class _StartUI(Protocol):
    def __call__(  # noqa: PLR0913
        self,
        *,
        checkout_root: Path,
        environment: Mapping[str, str],
        port: int,
        reload: bool,
        host: str,
        owned_process_group: bool,
        redact_values: tuple[str, ...],
    ) -> UIChild: ...


class _WaitForReadiness(Protocol):
    def __call__(
        self,
        child: UIChild,
        *,
        expected: ExpectedUIRuntime,
        timeout: float,
    ) -> object: ...


class _StopUI(Protocol):
    def __call__(self, child: UIChild) -> None: ...


def _effective_uid() -> int:
    getter = cast("Callable[[], int] | None", getattr(os, "geteuid", None))
    if getter is None:
        message = "installed runtime requires POSIX user ownership"
        raise ContainerRuntimeError(message)
    return getter()


def _add_profile_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", required=True)


def build_parser() -> argparse.ArgumentParser:
    """Build the explicit installed-runtime command surface."""
    parser = argparse.ArgumentParser(prog="agileforge-container")
    commands = parser.add_subparsers(dest="command", required=True)

    init_parser = commands.add_parser("init", help="Initialize new durable state")
    _add_profile_argument(init_parser)
    init_parser.add_argument("--model-config", type=Path)
    init_parser.add_argument("--json", action="store_true")

    info_parser = commands.add_parser("info", help="Validate and show runtime state")
    _add_profile_argument(info_parser)
    info_parser.add_argument("--json", action="store_true")

    serve_parser = commands.add_parser(
        "serve",
        help="Serve the dashboard in foreground",
    )
    _add_profile_argument(serve_parser)
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--ready-timeout", type=float, default=15.0)
    serve_parser.add_argument("--secrets-file", type=Path)
    serve_parser.add_argument("--json", action="store_true")

    cli_parser = commands.add_parser("cli", help="Run one product CLI command")
    _add_profile_argument(cli_parser)
    cli_parser.add_argument("--secrets-file", type=Path)
    cli_parser.add_argument("--json", action="store_true")
    cli_parser.add_argument("agileforge_arguments", nargs=argparse.REMAINDER)

    backup_parser = commands.add_parser("backup", help="Export verified durable state")
    _add_profile_argument(backup_parser)
    backup_parser.add_argument("--destination", type=Path, required=True)
    backup_parser.add_argument("--repository", type=Path, action="append")
    backup_parser.add_argument("--json", action="store_true")

    configure_parser = commands.add_parser(
        "configure-models", help="Update a production profile's model configuration"
    )
    _add_profile_argument(configure_parser)
    configure_parser.add_argument("--model-config", type=Path, required=True)
    configure_parser.add_argument("--backup-directory", type=Path, required=True)
    configure_parser.add_argument("--json", action="store_true")

    recover_parser = commands.add_parser(
        "recover-models", help="Restore a recorded previous model configuration"
    )
    _add_profile_argument(recover_parser)
    recover_parser.add_argument("--backup-directory", type=Path, required=True)
    recover_parser.add_argument("--json", action="store_true")

    restore_parser = commands.add_parser(
        "restore",
        help="Restore verified durable state",
    )
    _add_profile_argument(restore_parser)
    restore_parser.add_argument("--bundle", type=Path, required=True)
    restore_parser.add_argument(
        "--from-development",
        action="store_true",
        help=(
            "Promote an agileforge-dev bundle (provenance/profile.json) into a "
            "production profile"
        ),
    )
    restore_parser.add_argument(
        "--bind-repository",
        action="append",
        metavar="PROJECT_ID=PATH",
        help=(
            "Absolute container path the named Project's repository will be "
            "attached at; required for every active binding when promoting"
        ),
    )
    restore_parser.add_argument("--json", action="store_true")
    return parser


def parse_relocation_targets(values: Sequence[str]) -> dict[int, str]:
    """Parse repeated ``PROJECT_ID=PATH`` options into one strict mapping."""
    targets: dict[int, str] = {}
    for value in values:
        project_text, separator, target = value.partition("=")
        if not separator or not project_text.isdecimal() or int(project_text) < 1:
            message = f"--bind-repository expects PROJECT_ID=PATH, got {value!r}"
            raise ContainerRuntimeError(message)
        project_id = int(project_text)
        if not PurePosixPath(target).is_absolute():
            message = f"--bind-repository path must be absolute, got {target!r}"
            raise ContainerRuntimeError(message)
        if project_id in targets:
            message = f"--bind-repository names duplicate project {project_id}"
            raise ContainerRuntimeError(message)
        targets[project_id] = target
    return targets


def _restore_relocation_targets(
    arguments: argparse.Namespace,
) -> dict[int, str] | None:
    if arguments.bind_repository is None:
        return None
    if not arguments.from_development:
        message = "--bind-repository requires --from-development"
        raise ContainerRuntimeError(message)
    return parse_relocation_targets(arguments.bind_repository)


def load_runtime_secrets(
    path: Path,
    *,
    expected_owner_uid: int | None = None,
) -> dict[str, str]:
    """Load only supported credentials from one private retained descriptor."""
    owner_uid = _effective_uid() if expected_owner_uid is None else expected_owner_uid
    absolute_path = path.absolute()
    try:
        metadata = absolute_path.lstat()
    except OSError as error:
        message = "runtime secrets file is unavailable"
        raise ContainerRuntimeError(message) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        message = "runtime secrets file must be a private regular file"
        raise ContainerRuntimeError(message)
    if metadata.st_uid != owner_uid or metadata.st_mode & 0o077:
        message = "runtime secrets file must have private runtime-user ownership"
        raise ContainerRuntimeError(message)
    if absolute_path.resolve(strict=True) != absolute_path:
        message = "runtime secrets file must be a private regular file"
        raise ContainerRuntimeError(message)
    try:
        with open_secrets_file(absolute_path) as stream:
            raw_values = dotenv_values(
                stream=stream,
                verbose=False,
                interpolate=False,
            )
    except (OSError, UnicodeError, SecretsFileError) as error:
        message = "runtime secrets file could not be read safely"
        raise ContainerRuntimeError(message) from error
    unsupported = sorted(
        key
        for key, value in raw_values.items()
        if key not in _SUPPORTED_CREDENTIALS and value is not None
    )
    if unsupported:
        message = "runtime secrets file contains an unsupported credential"
        raise ContainerRuntimeError(message)
    return {
        key: value
        for key, value in raw_values.items()
        if key in _SUPPORTED_CREDENTIALS and isinstance(value, str) and value
    }


def production_environment(
    state: ProductionStateManifest,
    build: BuildIdentity,
    *,
    secrets: Mapping[str, str],
) -> dict[str, str]:
    """Return a sanitized child environment containing no caller identity claims."""
    if build.schema_version != "agileforge.build.v1":
        message = "unsupported build identity"
        raise ContainerRuntimeError(message)
    unsupported = set(secrets) - _SUPPORTED_CREDENTIALS
    if unsupported:
        message = "unsupported runtime credential"
        raise ContainerRuntimeError(message)
    git_executable = shutil.which("git", path=os.defpath)
    if git_executable is None:
        message = "installed runtime could not resolve Git"
        raise ContainerRuntimeError(message)
    git_path = Path(git_executable).resolve(strict=True)
    if (
        not git_path.is_absolute()
        or not git_path.is_file()
        or not os.access(git_path, os.X_OK)
    ):
        message = "installed runtime resolved an unsafe Git executable"
        raise ContainerRuntimeError(message)
    environment = {
        "AGILEFORGE_DB_URL": f"sqlite:///{state.business_database.as_posix()}",
        "AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL": (
            f"sqlite:///{state.trace_database.as_posix()}"
        ),
        "AGILEFORGE_PRODUCTION_PROFILE": state.profile_name,
        LAUNCHER_CHILD_ENV: LAUNCHER_CHILD_VALUE,
        "GIT_PYTHON_GIT_EXECUTABLE": str(git_path),
        "HOME": str(state.profile_root),
        "LANG": "C.UTF-8",
        "MODEL_CONFIG_PATH": str(state.model_config_path),
        "PATH": os.defpath,
        "PYTHONNOUSERSITE": "1",
        "PYTHONUNBUFFERED": "1",
    }
    environment.update(secrets)
    return environment


def _redact_text(value: str, secret_values: tuple[str, ...]) -> str:
    return redact_text(value, secret_values)


def _capture_child_output(
    stream: BinaryIO,
    sink: bytearray,
    *,
    overflow: threading.Event,
    child: UIChild,
) -> None:
    try:
        while chunk := stream.read(64 * 1024):
            if len(sink) + len(chunk) > _MAX_CHILD_OUTPUT_BYTES:
                overflow.set()
                child.process.kill()
                return
            sink.extend(chunk)
    finally:
        stream.close()


def _run_owned_cli_child(
    arguments: tuple[str, ...],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    secret_values: tuple[str, ...],
) -> int:
    process = subprocess.Popen(  # noqa: S603  # nosec B603
        arguments,
        cwd=cwd,
        env=dict(environment),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    child = UIChild(process=PosixProcessGroup(process), port=0)
    if process.stdout is None or process.stderr is None:  # pragma: no cover
        message = "product CLI output pipes were not acquired"
        raise ContainerRuntimeError(message)
    stdout = bytearray()
    stderr = bytearray()
    overflow = threading.Event()
    readers = (
        threading.Thread(
            target=_capture_child_output,
            args=(process.stdout, stdout),
            kwargs={"overflow": overflow, "child": child},
            daemon=True,
        ),
        threading.Thread(
            target=_capture_child_output,
            args=(process.stderr, stderr),
            kwargs={"overflow": overflow, "child": child},
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()
    try:
        exit_code = process.wait()
    finally:
        stop_ui(child)
        for reader in readers:
            reader.join(timeout=5.0)
    if any(reader.is_alive() for reader in readers):
        message = "product CLI output reader did not terminate"
        raise ContainerRuntimeError(message)
    if overflow.is_set():
        message = "product CLI output exceeded the safe limit"
        raise ContainerRuntimeError(message)
    sys.stdout.write(
        _redact_text(stdout.decode("utf-8", errors="replace"), secret_values)
    )
    sys.stderr.write(
        _redact_text(stderr.decode("utf-8", errors="replace"), secret_values)
    )
    return exit_code


def run_product_cli(
    state: ProductionStateManifest,
    build: BuildIdentity,
    *,
    forwarded: tuple[str, ...],
    secrets: Mapping[str, str],
    child_arguments: tuple[str, ...] | None = None,
) -> int:
    """Run one product CLI command with bounded credential-safe output."""
    if not forwarded or forwarded[0] != "--" or len(forwarded) == 1:
        message = "product CLI arguments must follow --"
        raise ContainerRuntimeError(message)
    pending = _pending_relocations(state)
    if pending and not _is_allowed_relocation_attach(forwarded, pending):
        message = (
            "pending repository relocation permits only its guarded attach command"
        )
        raise ContainerRuntimeError(message)
    arguments = child_arguments or (
        sys.executable,
        "-m",
        "cli.main",
        *forwarded[1:],
    )
    exit_code = _run_owned_cli_child(
        arguments,
        cwd=state.profile_root,
        environment=production_environment(state, build, secrets=secrets),
        secret_values=tuple(secrets.values()),
    )
    if exit_code == 0 and pending:
        remaining = _pending_relocations(state)
        if any(
            item in remaining and _is_allowed_relocation_attach(forwarded, (item,))
            for item in pending
        ):
            message = "guarded repository attachment did not clear its relocation"
            raise ContainerRuntimeError(message)
    return exit_code


def _is_allowed_relocation_attach(
    forwarded: tuple[str, ...],
    pending: tuple[RepositoryRelocation, ...],
) -> bool:
    if forwarded[1:3] != ("repository", "attach"):
        return False
    values: dict[str, str] = {}
    arguments = forwarded[3:]
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        matched = False
        for option in ("--project-id", "--path"):
            if argument == option and index + 1 < len(arguments):
                if option in values:
                    return False
                values[option] = arguments[index + 1]
                index += 2
                matched = True
                break
            prefix = f"{option}="
            if argument.startswith(prefix):
                if option in values:
                    return False
                values[option] = argument.removeprefix(prefix)
                index += 1
                matched = True
                break
        if not matched:
            index += 1
    try:
        project_id = int(values["--project-id"])
        restored_path = Path(values["--path"])
    except (KeyError, ValueError):
        return False
    return restored_path.is_absolute() and any(
        relocation.project_id == project_id
        and Path(relocation.restored_path) == restored_path
        for relocation in pending
    )


def _pending_relocations(
    state: ProductionStateManifest,
) -> tuple[RepositoryRelocation, ...]:
    if state.repository_relocations_sha256 is None:
        return ()
    return pending_relocations(state.business_database, state.profile_root)


def serve_production(  # noqa: PLR0913
    state: ProductionStateManifest,
    build: BuildIdentity,
    *,
    secrets: Mapping[str, str],
    port: int,
    ready_timeout: float,
    start: _StartUI = start_ui,
    wait_ready: _WaitForReadiness = wait_for_readiness,
    stop: _StopUI = stop_ui,
) -> int:
    """Own one foreground dashboard group until exit or interrupted startup."""
    if not 1 <= port <= _MAX_PORT:
        message = "port must be from 1 through 65535"
        raise ContainerRuntimeError(message)
    if ready_timeout <= 0:
        message = "readiness timeout must be greater than zero"
        raise ContainerRuntimeError(message)
    if _pending_relocations(state):
        message = "dashboard start is blocked by pending repository relocation"
        raise ContainerRuntimeError(message)
    if not callable(start) or not callable(wait_ready) or not callable(stop):
        message = "dashboard lifecycle dependency is not callable"
        raise TypeError(message)
    launch_nonce = secure_random.token_hex(16)
    environment = production_environment(state, build, secrets=secrets)
    environment[UI_LAUNCH_NONCE_ENV] = launch_nonce
    child: UIChild = start(
        checkout_root=INSTALLED_ROOT,
        environment=environment,
        port=port,
        reload=False,
        host=CONTAINER_HOST,
        owned_process_group=True,
        redact_values=tuple(secrets.values()),
    )
    try:
        expected = ExpectedUIRuntime(
            checkout_root=INSTALLED_ROOT,
            commit=build.revision,
            business_database=state.business_database,
            trace_database=state.trace_database,
            process_id=child.process.pid,
            launch_nonce=launch_nonce,
            state_id=str(state.state_id),
        )
        wait_ready(child, expected=expected, timeout=ready_timeout)
        return child.process.wait()
    finally:
        stop(child)


def _production_profile_root(deployment_root: Path, profile_name: str) -> Path:
    root = deployment_root.absolute() / "profiles" / profile_name
    paths = production_state_paths(root)
    if paths.root.parent != deployment_root.absolute() / "profiles":
        message = "production profile escapes the deployment root"
        raise ContainerRuntimeError(message)
    return paths.root


def _maintenance_roots(deployment_root: Path) -> tuple[Path, ...]:
    roots = [deployment_root.absolute()]
    if (
        deployment_root.absolute() == PRODUCTION_DEPLOYMENT_ROOT
        and PRODUCTION_WORKSPACE_ROOT.is_dir()
    ):
        roots.append(PRODUCTION_WORKSPACE_ROOT)
    return tuple(sorted(roots, key=lambda item: str(item.resolve(strict=True))))


@contextmanager
def _runtime_fences(
    deployment_root: Path,
    *,
    exclusive: bool = False,
) -> Iterator[None]:
    with ExitStack() as stack:
        for root in _maintenance_roots(deployment_root):
            stack.enter_context(runtime_fence(root, exclusive=exclusive))
        yield


def _secrets_for_arguments(arguments: argparse.Namespace) -> dict[str, str]:
    secret_path = getattr(arguments, "secrets_file", None)
    if secret_path is None:
        return {}
    return load_runtime_secrets(secret_path)


def _runtime_payload(
    state: ProductionStateManifest,
    build: BuildIdentity,
) -> dict[str, object]:
    pending = _pending_relocations(state)
    return {
        "ok": True,
        "build": build.model_dump(mode="json"),
        "state": state.model_dump(mode="json"),
        "pending_repository_relocations": [item.to_dict() for item in pending],
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def backup_production_state(
    state: ProductionStateManifest,
    destination: Path,
    *,
    deployment_root: Path = PRODUCTION_DEPLOYMENT_ROOT,
    repositories: tuple[Path, ...] | None = None,
    maintenance_fences_held: bool = False,
) -> Path:
    """Capture complete state and every registered repository under one fence."""
    paths = production_state_paths(state.profile_root)
    layout = StateLayout(
        root=paths.root,
        business_database=paths.business_database,
        trace_database=paths.trace_database,
        artifacts=paths.artifacts,
        model_config=paths.model_config,
        repositories=repositories,
        include_registered_repositories=True,
        maintenance_roots=_maintenance_roots(deployment_root),
        provenance_files=(paths.manifest,),
    )
    return backup_state(
        layout,
        destination,
        maintenance_fences_held=maintenance_fences_held,
    )


def _check_bundle_mode(bundle: Path, *, from_development: bool) -> None:
    """Refuse bundle/flag mismatches before any provenance is parsed."""
    runtime_provenance = bundle / "provenance" / "runtime.json"
    profile_provenance = bundle / "provenance" / "profile.json"
    if runtime_provenance.is_file() and from_development:
        message = "production bundle must be restored without --from-development"
        raise ContainerRuntimeError(message)
    if (
        profile_provenance.is_file()
        and not runtime_provenance.is_file()
        and not from_development
    ):
        message = "development profile bundle requires --from-development"
        raise ContainerRuntimeError(message)


def _load_restored_provenance(  # noqa: C901
    bundle: Path,
    *,
    from_development: bool = False,
) -> ProductionStateManifest:
    _check_bundle_mode(bundle, from_development=from_development)
    runtime_provenance = bundle / "provenance" / "runtime.json"
    profile_provenance = bundle / "provenance" / "profile.json"
    if runtime_provenance.is_file():
        try:
            source_state = ProductionStateManifest.model_validate_json(
                runtime_provenance.read_bytes()
            )
        except (OSError, ValidationError) as error:
            message = "restored production provenance is invalid"
            raise ContainerRuntimeError(message) from error
        source_paths = production_state_paths(source_state.profile_root)
        expected = {
            "profile_root": source_paths.root,
            "business_database": source_paths.business_database,
            "trace_database": source_paths.trace_database,
            "artifacts": source_paths.artifacts,
            "model_config_path": source_paths.model_config,
        }
        if source_state.profile_name != source_paths.root.name or any(
            getattr(source_state, field_name) != expected_path
            for field_name, expected_path in expected.items()
        ):
            message = "restored production provenance has invalid reserved paths"
            raise ContainerRuntimeError(message)
        if _sha256_file(bundle / "model-config") != source_state.model_config_sha256:
            message = "restored model configuration hash does not match provenance"
            raise ContainerRuntimeError(message)
        if (
            database_schema_sha256(bundle / "business.sqlite3")
            != source_state.business_schema_sha256
        ):
            message = "restored business schema hash does not match provenance"
            raise ContainerRuntimeError(message)
        if (
            source_state.trace_database_present
            and not (bundle / "trace.sqlite3").is_file()
        ):
            message = "restored trace database is missing from its provenance"
            raise ContainerRuntimeError(message)
        return source_state

    if profile_provenance.is_file():
        (
            profile_name,
            created_at,
            commit,
            model_config_sha256,
            expected_schema_hash,
            expected_trace_present,
        ) = _parse_migration_profile(profile_provenance)

        if _sha256_file(bundle / "model-config") != model_config_sha256:
            message = "restored model configuration hash does not match provenance"
            raise ContainerRuntimeError(message)

        business_schema_hash = database_schema_sha256(bundle / "business.sqlite3")
        if (
            expected_schema_hash is not None
            and expected_schema_hash != business_schema_hash
        ):
            message = "restored business schema hash does not match provenance"
            raise ContainerRuntimeError(message)

        trace_present = (bundle / "trace.sqlite3").is_file()
        if expected_trace_present is True and not trace_present:
            message = "restored trace database is missing from its provenance"
            raise ContainerRuntimeError(message)

        state_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"urn:agileforge:migration:{profile_name}:{created_at.isoformat()}",
        )
        source_paths = production_state_paths(PRODUCTION_STATE_BASE / profile_name)
        try:
            return ProductionStateManifest(
                schema_version="agileforge.production-state.v1",
                state_id=state_id,
                profile_name=profile_name,
                profile_root=source_paths.root,
                business_database=source_paths.business_database,
                trace_database=source_paths.trace_database,
                trace_database_present=trace_present,
                artifacts=source_paths.artifacts,
                model_config_path=source_paths.model_config,
                model_config_sha256=model_config_sha256,
                business_schema_sha256=business_schema_hash,
                created_at=created_at,
                created_by_build_revision=commit,
                provenance="initialized",
                source_backup_sha256=None,
                repository_relocations_sha256=None,
            )
        except ValidationError as error:
            message = "restored production provenance is invalid"
            raise ContainerRuntimeError(message) from error

    message = "restored production provenance is invalid"
    raise ContainerRuntimeError(message)


def _parse_migration_profile(
    profile_provenance: Path,
) -> tuple[str, datetime, str, str, str | None, bool | None]:
    invalid_message = "restored production provenance is invalid"
    try:
        data = json.loads(profile_provenance.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ContainerRuntimeError(invalid_message)
        name = data.get("name")
        if not isinstance(name, str) or not name:
            raise ContainerRuntimeError(invalid_message)
        created_at_raw = data.get("created_at")
        if not isinstance(created_at_raw, str):
            raise ContainerRuntimeError(invalid_message)
        created_at = datetime.fromisoformat(created_at_raw)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        checkout = data.get("checkout")
        if not isinstance(checkout, dict):
            raise ContainerRuntimeError(invalid_message)
        commit = checkout.get("commit")
        if (
            not isinstance(commit, str)
            or len(commit) != _COMMIT_LENGTH
            or any(c not in "0123456789abcdef" for c in commit)
        ):
            raise ContainerRuntimeError(invalid_message)
        model_config_sha256 = data.get("model_config_sha256")
        if (
            not isinstance(model_config_sha256, str)
            or len(model_config_sha256) != _HASH_LENGTH
        ):
            raise ContainerRuntimeError(invalid_message)
        expected_schema = data.get("business_schema_sha256")
        trace_present = data.get("trace_database_present")
        return (
            name,
            created_at,
            commit,
            model_config_sha256,
            expected_schema if isinstance(expected_schema, str) else None,
            trace_present if isinstance(trace_present, bool) else None,
        )
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise ContainerRuntimeError(invalid_message) from error


def _install_restored_payload(profile_root: Path) -> None:
    paths = production_state_paths(profile_root)
    generic_model_config = paths.root / "model-config"
    generic_trace = paths.root / "trace.sqlite3"
    provenance_root = paths.root / "provenance"
    paths.root.chmod(0o700)
    paths.config_directory.mkdir(mode=0o700, exist_ok=True)
    paths.config_directory.chmod(0o700)
    generic_model_config.replace(paths.model_config)
    paths.model_config.chmod(0o600)
    if generic_trace.exists():
        generic_trace.replace(paths.trace_database)
        paths.trace_database.chmod(0o600)
    if paths.business_database.exists():
        paths.business_database.chmod(0o600)
    if paths.artifacts.is_dir():
        paths.artifacts.chmod(0o700)
        for root, dirs, files in os.walk(paths.artifacts):
            for d in dirs:
                (Path(root) / d).chmod(0o700)
            for f in files:
                (Path(root) / f).chmod(0o600)
    if provenance_root.is_dir():
        for item in sorted(provenance_root.iterdir()):
            if item.is_file():
                destination_path = paths.config_directory / f"source-{item.name}"
                shutil.copy2(item, destination_path)
                destination_path.chmod(0o600)
                item.unlink()
        provenance_root.rmdir()


def restore_production_state(  # noqa: PLR0913
    bundle: Path,
    profile_root: Path,
    *,
    build: BuildIdentity,
    deployment_root: Path = PRODUCTION_DEPLOYMENT_ROOT,
    expected_owner_uid: int | None = None,
    from_development: bool = False,
    relocation_targets: Mapping[int, str] | None = None,
) -> ProductionStateManifest:
    """Restore, validate, rebase, and publish one production profile.

    ``from_development`` promotes an ``agileforge-dev`` bundle. Promotion is
    strict about repositories: every active binding in the bundle must map to
    a ``relocation_targets`` entry, and the resulting record keeps the
    dashboard blocked until that exact guarded attach succeeds.
    """
    if relocation_targets is not None and not from_development:
        message = "relocation targets require --from-development"
        raise ContainerRuntimeError(message)
    targets = dict(relocation_targets or {}) if from_development else None
    with _runtime_fences(deployment_root, exclusive=True):
        transfer_manifest = verify_backup(bundle)
        verify_current_business_schema(bundle / "business.sqlite3")
        if (bundle / "trace.sqlite3").is_file():
            verify_current_trace_schema(bundle / "trace.sqlite3")
        source_state = _load_restored_provenance(
            bundle, from_development=from_development
        )
        if targets is not None:
            validate_relocation_targets(
                bundle / "business.sqlite3", transfer_manifest, targets
            )
        owner_uid = (
            _effective_uid() if expected_owner_uid is None else expected_owner_uid
        )
        prepare_profile_parent(profile_root.parent, expected_owner_uid=owner_uid)
        restored_manifest = restore_payload(bundle, profile_root)
        if restored_manifest != transfer_manifest:
            message = "restored transfer manifest changed during publication"
            raise ContainerRuntimeError(message)
        finalize_restored_repositories(transfer_manifest, profile_root)
        relocation_record = write_relocation_record(
            transfer_manifest,
            profile_root,
            profile_root,
            relocation_targets=targets,
        )
        relocation_sha256 = _sha256_file(relocation_record)
        _install_restored_payload(profile_root)
        return finalize_restored_production_state(
            profile_root,
            source_state=source_state,
            build=build,
            source_backup_sha256=_sha256_file(bundle / "manifest.json"),
            repository_relocations_sha256=relocation_sha256,
            expected_owner_uid=expected_owner_uid,
        )


def _emit_payload(payload: Mapping[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
    sys.stdout.write("\n")


def _sigterm_interrupt(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


@contextmanager
def _termination_as_interrupt() -> Iterator[None]:
    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, _sigterm_interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _unsupported_command() -> Never:
    message = "unsupported production command"
    raise ContainerRuntimeError(message)


def main(  # noqa: C901, PLR0911, PLR0912, PLR0915
    argv: Sequence[str] | None = None,
    *,
    deployment_root: Path = PRODUCTION_DEPLOYMENT_ROOT,
    build_path: Path = BUILD_IDENTITY_PATH,
    expected_build_owner_uid: int = 0,
    expected_state_owner_uid: int | None = None,
) -> int:
    """Run one explicit installed-runtime lifecycle command."""
    try:
        require_linux()
    except UnsupportedPlatformError as error:
        emit(str(error), file=sys.stderr)
        return UNSUPPORTED_PLATFORM_EXIT_CODE
    arguments = build_parser().parse_args(argv)
    json_output = bool(getattr(arguments, "json", False))
    try:
        build = load_build_identity(
            build_path,
            expected_owner_uid=expected_build_owner_uid,
        )
        profile_root = _production_profile_root(deployment_root, arguments.profile)
        if arguments.command == "init":
            model_source = arguments.model_config or Path(
                str(files("config").joinpath("models.yaml"))
            )
            with _runtime_fences(deployment_root, exclusive=True):
                state = initialize_production_state(
                    profile_root,
                    build=build,
                    model_config_source=model_source,
                    expected_owner_uid=expected_state_owner_uid,
                )
            _emit_payload(_runtime_payload(state, build))
            return 0
        if arguments.command == "backup":
            with _runtime_fences(deployment_root, exclusive=True):
                state = load_production_state(
                    profile_root,
                    build=build,
                    expected_owner_uid=expected_state_owner_uid,
                )
                explicit_repositories = (
                    None
                    if arguments.repository is None
                    else tuple(arguments.repository)
                )
                bundle = backup_production_state(
                    state,
                    arguments.destination,
                    deployment_root=deployment_root,
                    repositories=explicit_repositories,
                    maintenance_fences_held=True,
                )
            _emit_payload({"ok": True, "backup": str(bundle)})
            return 0
        if arguments.command in {"configure-models", "recover-models"}:
            owner_uid = (
                _effective_uid()
                if expected_state_owner_uid is None
                else expected_state_owner_uid
            )
            with _runtime_fences(deployment_root, exclusive=True):
                if arguments.command == "configure-models":
                    result = configure_models(
                        profile_root,
                        arguments.model_config,
                        arguments.backup_directory,
                        build=build,
                        deployment_root=deployment_root,
                        expected_owner_uid=owner_uid,
                    )
                else:
                    result = recover_models(
                        profile_root,
                        arguments.backup_directory,
                        build=build,
                        deployment_root=deployment_root,
                        expected_owner_uid=owner_uid,
                    )
            _emit_payload(result)
            return 0
        if arguments.command in {"info", "serve", "cli"}:
            with _runtime_fences(deployment_root):
                state = load_production_state(
                    profile_root,
                    build=build,
                    expected_owner_uid=expected_state_owner_uid,
                )
                if arguments.command == "info":
                    _emit_payload(_runtime_payload(state, build))
                    return 0
                secrets = _secrets_for_arguments(arguments)
                if arguments.command == "cli":
                    with _termination_as_interrupt():
                        try:
                            return run_product_cli(
                                state,
                                build,
                                forwarded=tuple(arguments.agileforge_arguments),
                                secrets=secrets,
                            )
                        except KeyboardInterrupt:
                            return 0
                with _termination_as_interrupt():
                    try:
                        return serve_production(
                            state,
                            build,
                            secrets=secrets,
                            port=arguments.port,
                            ready_timeout=arguments.ready_timeout,
                        )
                    except KeyboardInterrupt:
                        return 0
        if arguments.command == "restore":
            relocation_targets = _restore_relocation_targets(arguments)
            state = restore_production_state(
                arguments.bundle,
                profile_root,
                build=build,
                deployment_root=deployment_root,
                expected_owner_uid=expected_state_owner_uid,
                from_development=arguments.from_development,
                relocation_targets=relocation_targets,
            )
            _emit_payload(_runtime_payload(state, build))
            return 0
        _unsupported_command()
    except (
        BuildIdentityError,
        ContainerRuntimeError,
        FenceError,
        OSError,
        ProductionStateError,
        RepositoryTransferError,
        TransferError,
        ValueError,
    ) as error:
        payload = {"ok": False, "error": str(error)}
        if json_output:
            _emit_payload(payload)
        else:
            sys.stderr.write(f"Error: {error}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ContainerRuntimeError",
    "build_parser",
    "load_runtime_secrets",
    "main",
    "production_environment",
]
