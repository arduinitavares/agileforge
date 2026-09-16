"""Lifetime ownership shared by supported application entrypoints."""

from __future__ import annotations

import os
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from cli.production_state import (
    ProductionStateManifest,
    load_production_state,
    production_profile_root,
)
from utils.build_identity import (
    BUILD_IDENTITY_PATH,
    BuildIdentity,
    runtime_build_identity,
)
from utils.runtime_fence import FenceError, runtime_fence

if TYPE_CHECKING:
    from collections.abc import Iterator

WORKSPACE_ROOT = Path("/workspace")
PRODUCTION_ROOT = Path("/var/lib/agileforge")
INSTALLED_ROOT = Path("/opt/agileforge")


def _production_selected() -> bool:
    return BUILD_IDENTITY_PATH.exists() or "AGILEFORGE_PRODUCTION_PROFILE" in os.environ


def runtime_output_root() -> Path | None:
    """Select owned diagnostic paths without creating files during import.

    Runtime entrypoints validate the complete profile and environment under a
    lifetime fence before any writer uses these paths.
    """
    if _production_selected():
        return production_profile_root(
            os.environ.get("AGILEFORGE_PRODUCTION_PROFILE", "")
        )
    if os.environ.get("AGILEFORGE_LAUNCHER_CHILD") != "1":
        return None
    value = os.environ.get("AGILEFORGE_DB_URL", "")
    prefix = "sqlite:///"
    database = Path(value.removeprefix(prefix))
    if not value.startswith(prefix) or not database.is_absolute():
        message = "launcher child requires an absolute SQLite database URL"
        raise ValueError(message)
    return database.parent


def production_runtime_identity() -> (
    tuple[BuildIdentity, ProductionStateManifest] | None
):
    """Validate production identity and the exact environment used by the app."""
    if not _production_selected():
        return None
    build = runtime_build_identity()
    profile_name = os.environ.get("AGILEFORGE_PRODUCTION_PROFILE", "")
    state = load_production_state(production_profile_root(profile_name), build=build)
    expected = {
        "AGILEFORGE_DB_URL": f"sqlite:///{state.business_database.as_posix()}",
        "AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL": (
            f"sqlite:///{state.trace_database.as_posix()}"
        ),
        "MODEL_CONFIG_PATH": str(state.model_config_path),
    }
    for name, value in expected.items():
        if os.environ.get(name) != value:
            message = f"{name} does not match the production state manifest"
            raise ValueError(message)
    return build, state


def development_roots(checkout_root: Path) -> tuple[Path, ...]:
    """Use the common Linux volume fence, or the pre-cutover POSIX state root."""
    root = checkout_root.resolve(strict=True)
    if root.is_relative_to(WORKSPACE_ROOT):
        return (WORKSPACE_ROOT,)
    if os.name != "posix":
        # Native Windows stays supported until the approved live cutover.
        # Explicit backup/restore still rejects platforms without safe fencing.
        return ()
    home_root = Path.home().resolve(strict=True)
    if root.is_relative_to(home_root):
        return (home_root,)
    state_root = root / ".agileforge"
    state_root.mkdir(mode=0o700, exist_ok=True)
    return (state_root,)


def runtime_roots() -> tuple[Path, ...]:
    """Return the common maintenance domains of this supported runtime."""
    if _production_selected():
        roots = [PRODUCTION_ROOT]
        if WORKSPACE_ROOT.exists():
            roots.append(WORKSPACE_ROOT)
        return tuple(sorted(roots))
    source_root = Path(__file__).resolve().parent.parent
    if (source_root / "agileforge-dev").is_file():
        roots = development_roots(source_root)
        if os.name == "posix" and not source_root.is_relative_to(WORKSPACE_ROOT):
            return tuple(sorted(set(roots) | set(_native_database_roots())))
        return roots
    if os.name != "posix":
        # Native Windows maintenance is explicitly unsupported until cutover.
        return ()
    return _native_database_roots()


def _native_database_roots() -> tuple[Path, ...]:
    from utils.runtime_config import (  # noqa: PLC0415
        get_adk_execution_trace_db_target,
        get_business_db_target,
    )

    targets = [get_business_db_target()]
    if os.environ.get("AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL"):
        targets.append(get_adk_execution_trace_db_target())
    roots: set[Path] = set()
    for target in targets:
        database = target.sqlite_path
        if database is None:
            continue
        root = database.parent
        for parent in database.parents:
            if parent.name == ".agileforge" and database.is_relative_to(
                parent / "dev" / "profiles"
            ):
                root = parent
                break
        home_root = Path.home().resolve(strict=True)
        roots.add(home_root if root.is_relative_to(home_root) else root)
    return tuple(sorted(roots))


@contextmanager
def development_access(checkout_root: Path) -> Iterator[None]:
    """Fence a complete checkout launcher operation and its child lifetime."""
    with ExitStack() as stack:
        for root in development_roots(checkout_root):
            stack.enter_context(runtime_fence(root))
        yield


def _require_relocated_repositories(
    repository_attachment: tuple[int, Path] | None,
) -> None:
    from cli.repository_transfer import pending_relocations  # noqa: PLC0415
    from utils.runtime_config import get_business_db_target  # noqa: PLC0415

    root = runtime_output_root()
    if root is None:
        database = get_business_db_target().sqlite_path
        if database is None:
            return
        root = database.parent
    else:
        database = root / "business.sqlite3"
    record = root / "repository-relocations.json"
    pending = (
        pending_relocations(database, root)
        if record.exists() or record.is_symlink()
        else ()
    )
    permitted = repository_attachment is not None and any(
        repository_attachment == (item.project_id, Path(item.restored_path))
        for item in pending
    )
    if pending and not permitted:
        message = (
            "repository relocation requires guarded repository attach "
            "before runtime use"
        )
        raise FenceError(message)


@contextmanager
def runtime_access(
    *,
    repository_attachment: tuple[int, Path] | None = None,
) -> Iterator[None]:
    """Acquire ownership and validate identity before any state initialization."""
    with ExitStack() as stack:
        for root in runtime_roots():
            stack.enter_context(runtime_fence(root))
        production_runtime_identity()
        _require_relocated_repositories(repository_attachment)
        yield
