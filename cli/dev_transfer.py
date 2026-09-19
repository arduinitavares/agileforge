"""Explicit backup and verified restore for strictly owned checkout profiles."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from contextlib import ExitStack
from pathlib import Path

from cli.dev_profiles import (
    RuntimeProfile,
    finalize_profile_record,
    load_profile,
    prepare_profile_record,
    profile_paths,
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
from utils.runtime_fence import FenceError, runtime_fence
from utils.runtime_ownership import development_roots


def _maintenance_roots(checkout_root: Path) -> tuple[Path, ...]:
    roots = development_roots(checkout_root)
    if not roots:
        message = "profile transfer requires safe POSIX maintenance fencing"
        raise FenceError(message)
    return roots


def backup_development_profile(
    checkout_root: Path,
    profile_name: str,
    destination: Path,
    *,
    repositories: tuple[Path, ...] | None = None,
    state_only: bool = False,
) -> Path:
    """Capture explicit owned state and repositories under exclusive ownership."""
    roots = _maintenance_roots(checkout_root)
    with ExitStack() as stack:
        for root in roots:
            stack.enter_context(runtime_fence(root, exclusive=True))
        profile = load_profile(checkout_root, profile_name)
        paths = profile_paths(checkout_root, profile_name)
        verify_current_business_schema(profile.business_database)
        if profile.trace_database.exists():
            verify_current_trace_schema(profile.trace_database)
        return backup_state(
            StateLayout(
                root=checkout_root,
                business_database=profile.business_database,
                trace_database=profile.trace_database,
                artifacts=paths.artifacts,
                model_config=profile.model_config_path,
                repositories=repositories,
                include_registered_repositories=not state_only,
                maintenance_roots=roots,
                provenance_files=(paths.manifest,),
            ),
            destination,
            maintenance_fences_held=True,
        )


def restore_development_profile(
    checkout_root: Path, profile_name: str, bundle: Path
) -> RuntimeProfile:
    """Verify first, reserve a new profile, then publish its ownership record last.

    A failure after reservation leaves an unfinalized profile for inspection.
    Existing state is never overwritten or automatically deleted. Git repair
    completes before publication; Project reattachment remains operator-owned.
    """
    with ExitStack() as stack:
        for root in _maintenance_roots(checkout_root):
            stack.enter_context(runtime_fence(root, exclusive=True))
        manifest = verify_backup(bundle)
        config = checkout_root / "config" / "models.yaml"
        if (
            hashlib.sha256(config.read_bytes()).hexdigest()
            != manifest.model_config_sha256
        ):
            message = "backup model configuration does not match the tracked checkout"
            raise TransferError(message)
        paths = profile_paths(checkout_root, profile_name)
        if paths.root.exists() or paths.root.is_symlink():
            message = f"profile root already exists: {paths.root}"
            raise FileExistsError(message)
        staging_parent = checkout_root / ".agileforge"
        staging_parent.mkdir(mode=0o700, exist_ok=True)
        temporary = stack.enter_context(
            tempfile.TemporaryDirectory(prefix="restore-", dir=staging_parent)
        )
        payload = Path(temporary) / "payload"
        restore_payload(bundle, payload)
        verify_current_business_schema(payload / "business.sqlite3")
        if (payload / "trace.sqlite3").exists():
            verify_current_trace_schema(payload / "trace.sqlite3")
        profile = prepare_profile_record(checkout_root, profile_name)
        (payload / "business.sqlite3").replace(paths.business_database)
        trace = payload / "trace.sqlite3"
        if trace.exists():
            trace.replace(paths.trace_database)
        paths.artifacts.rmdir()
        (payload / "artifacts").replace(paths.artifacts)
        for name in ("repositories", "repository-admin"):
            source = payload / name
            if source.exists():
                shutil.move(source, paths.root / name)
        from cli.repository_transfer import (  # noqa: PLC0415
            finalize_restored_repositories,
            write_relocation_record,
        )

        finalize_restored_repositories(manifest, paths.root)
        record = write_relocation_record(manifest, paths.root, paths.root)
        profile = profile.model_copy(
            update={
                "repository_relocations_sha256": hashlib.sha256(
                    record.read_bytes()
                ).hexdigest()
            }
        )
        return finalize_profile_record(profile)
