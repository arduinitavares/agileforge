# scripts/rehearse_windows_to_linux.py
# ruff: noqa: T201, PLR0915, D103, TRY003, EM102, E402
"""End-to-end Windows-to-Linux migration rehearsal with disposable synthetic data."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from git import Repo
from google.adk.sessions import DatabaseSessionService
from sqlmodel import create_engine

from cli.state_transfer import verify_backup
from models.db import ensure_business_db_ready
from scripts.migrate_windows_profile import export_windows_profile

logger: logging.Logger = logging.getLogger(name=__name__)


def _to_wsl_path(path: Path) -> str:
    """Convert Windows path to WSL /mnt/... path."""
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").lower()
    path_without_drive = resolved.as_posix().removeprefix(f"{resolved.drive}")
    return f"/mnt/{drive}{path_without_drive}"


def _run_wsl(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    cmd = ["wsl", "-d", "Ubuntu", *arguments]
    print(f"WSL: {' '.join(cmd)}")
    try:
        return subprocess.run(  # noqa: S603 # nosec B603
            cmd,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as err:
        print(f"WSL COMMAND FAILED:\nSTDOUT:\n{err.stdout}\nSTDERR:\n{err.stderr}")
        raise


async def _init_adk_trace(db_path: Path) -> None:
    service = DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{db_path.as_posix()}")
    try:
        await service.create_session(
            app_name="rehearsal-app",
            user_id="rehearsal-user",
            session_id="rehearsal-session-1",
            state={"status": "initial"},
        )
    finally:
        await service.close()


def run_rehearsal() -> dict[str, object]:
    tmp_dir = Path(tempfile.mkdtemp(prefix="agileforge-rehearsal-"))
    project_name = "rehearsal-pidextract-20260919"
    try:
        print(f"1. Setting up synthetic environment at {tmp_dir}")
        repo_dir = tmp_dir / "synthetic_repo"
        repo_dir.mkdir()
        repo = Repo.init(repo_dir)
        tracked_file = repo_dir / "tracked.txt"
        tracked_file.write_text("initial tracked content\n", encoding="utf-8")
        repo.index.add([str(tracked_file)])
        commit = repo.index.commit("Initial synthetic commit")

        # Dirty change and untracked file
        tracked_file.write_text("dirty tracked content\n", encoding="utf-8")
        (repo_dir / "untracked.txt").write_text("untracked work\n", encoding="utf-8")
        (repo_dir / ".env").write_text("SECRET_KEY=do-not-leak\n", encoding="utf-8")

        # Profile directory
        profile_root = tmp_dir / "synthetic_profile"
        profile_root.mkdir()
        artifacts_dir = profile_root / "artifacts"
        artifacts_dir.mkdir()
        (artifacts_dir / "artifact_output.txt").write_text(
            "synthetic artifact output\n", encoding="utf-8"
        )

        # Business DB
        business_db = profile_root / "business.sqlite3"
        engine = create_engine(f"sqlite:///{business_db}")
        ensure_business_db_ready(engine)
        engine.dispose()

        # ADK Trace DB
        trace_db = profile_root / "adk-trace.sqlite3"
        asyncio.run(_init_adk_trace(trace_db))

        # Model config
        model_config = Path("config/models.yaml").resolve()
        model_hash = hashlib.sha256(model_config.read_bytes()).hexdigest()

        # profile.json
        profile_json = profile_root / "profile.json"
        metadata = {
            "name": "rehearsal-synthetic",
            "created_at": "2026-09-19T10:00:00Z",
            "checkout": {
                "root": str(repo_dir),
                "branch": "master",
                "commit": commit.hexsha,
            },
            "model_config_path": str(model_config),
            "model_config_sha256": model_hash,
        }
        profile_json.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        # Step 2: Export profile
        bundle_dest = tmp_dir / "migration_bundle"
        print("2. Exporting Windows profile bundle...")
        exported = export_windows_profile(
            profile_root=profile_root,
            destination=bundle_dest,
            repositories=[repo_dir],
            model_config=model_config,
        )
        print(f"Exported to: {exported}")
        manifest = verify_backup(bundle_dest)
        print(
            f"Manifest verified: format={manifest.format}, "
            f"repos={len(manifest.repositories)}"
        )

        # Step 3: Clean up any stale WSL volumes
        print("3. Preparing WSL Docker volumes...")
        _run_wsl(
            [
                "docker",
                "volume",
                "rm",
                "-f",
                f"{project_name}-workspace",
                f"{project_name}-production-state",
            ]
        )

        # Step 4: Populate workspace volume with the migration bundle
        print("4. Copying migration bundle into WSL workspace volume...")
        wsl_bundle = _to_wsl_path(bundle_dest)
        _run_wsl(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "/bin/bash",
                "-u",
                "0",
                "-v",
                f"{project_name}-workspace:/workspace",
                "-v",
                f"{wsl_bundle}:/source:ro",
                "agileforge-production:local",
                "-c",
                "cp -a /source /workspace/synthetic-bundle && "
                "find /workspace/synthetic-bundle -type f -exec chmod 666 {} + && "
                "find /workspace/synthetic-bundle -type d -exec chmod 777 {} + && "
                "chown -R 10001:10001 /workspace/synthetic-bundle",
            ]
        )

        # Step 5: Run production-maintenance restore
        print("5. Running production restore in container...")
        restore_result = _run_wsl(
            [
                "python3",
                "scripts/container.py",
                "--project-name",
                project_name,
                "production-maintenance",
                "--image",
                "agileforge-production:local",
                "--",
                "restore",
                "--profile",
                "rehearsal-synthetic",
                "--bundle",
                "/workspace/synthetic-bundle",
                "--json",
            ]
        )
        print(f"Restore output:\n{restore_result.stdout}")
        restore_payload = json.loads(restore_result.stdout)
        if not restore_payload.get("ok"):
            raise RuntimeError(f"Restore failed: {restore_result.stdout}")

        # Step 6: Validate restored profile with info --json
        print("6. Inspecting restored profile runtime with info...")
        info_result = _run_wsl(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{project_name}-production-state:/var/lib/agileforge",
                "agileforge-production:local",
                "info",
                "--profile",
                "rehearsal-synthetic",
                "--json",
            ]
        )
        print(f"Info output:\n{info_result.stdout}")
        info_payload = json.loads(info_result.stdout)
        if not info_payload.get("ok"):
            raise RuntimeError(f"Info failed: {info_result.stdout}")

        # Step 7: Clean up WSL volumes
        print("7. Cleaning up WSL rehearsal volumes...")
        _run_wsl(
            [
                "docker",
                "volume",
                "rm",
                f"{project_name}-workspace",
                f"{project_name}-production-state",
            ]
        )

        print("REHEARSAL COMPLETED SUCCESSFULLY!")
        return {
            "rehearsal_passed": True,
            "restore_payload": restore_payload,
            "info_payload": info_payload,
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    result = run_rehearsal()
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
