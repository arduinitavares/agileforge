"""Installed model maintenance preserves profile identity and recovery bytes."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from cli import production_model_config
from cli.container_runtime import main
from cli.production_state import initialize_production_state, load_production_state
from utils.build_identity import BuildIdentity
from utils.runtime_fence import runtime_fence

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Linux installed runtime")
_ERROR_EXIT = 2
_FINAL_PUBLICATION = 4


def _uid() -> int:
    return cast("Callable[[], int]", os.getuid)()


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


def _profile(tmp_path: Path) -> tuple[Path, Path, Path]:
    deployment = tmp_path / "deployment"
    deployment.mkdir(mode=0o700)
    profile = deployment / "profiles" / "default"
    source = Path("config/models.yaml").resolve()
    initialize_production_state(profile, build=_build(), model_config_source=source)
    build_file = tmp_path / "build.json"
    build_file.write_text(_build().model_dump_json(), encoding="utf-8")
    build_file.chmod(0o444)
    return deployment, profile, build_file


def _call(
    argv: list[str],
    deployment: Path,
    build_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, dict[str, object]]:
    result = main(
        argv,
        deployment_root=deployment,
        build_path=build_file,
        expected_build_owner_uid=_uid(),
        expected_state_owner_uid=_uid(),
    )
    return result, json.loads(capsys.readouterr().out)


def _candidate(tmp_path: Path, profile: Path) -> Path:
    source = (profile / "config" / "models.yaml").read_bytes()
    candidate = tmp_path / "candidate.yaml"
    candidate.write_bytes(source.replace(b"gpt-5.6-luna", b"gpt-6-sol", 1))
    candidate.chmod(0o600)
    return candidate


def _update_arguments(candidate: Path, recovery: Path) -> list[str]:
    return [
        "configure-models",
        "--profile",
        "default",
        "--model-config",
        str(candidate),
        "--backup-directory",
        str(recovery),
        "--json",
    ]


def _recover_arguments(recovery: Path) -> list[str]:
    return [
        "recover-models",
        "--profile",
        "default",
        "--backup-directory",
        str(recovery),
        "--json",
    ]


def test_update_and_recovery_preserve_literal_previous_pair(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A configuration-only update can roll back without changing provenance."""
    deployment, profile, build_file = _profile(tmp_path)
    model_file = profile / "config" / "models.yaml"
    manifest_file = profile / "runtime.json"
    old_model, old_manifest = model_file.read_bytes(), manifest_file.read_bytes()
    old_state = load_production_state(profile, build=_build())
    artifact = profile / "artifacts" / "retained.txt"
    artifact.write_bytes(b"accepted artifact\n")
    artifact.chmod(0o600)
    business = (profile / "business.sqlite3").read_bytes()
    candidate = _candidate(tmp_path, profile)
    recovery = deployment / "recovery"

    code, payload = _call(
        _update_arguments(candidate, recovery),
        deployment,
        build_file,
        capsys,
    )

    assert code == 0
    assert payload["old_model_config_sha256"] == hashlib.sha256(old_model).hexdigest()
    assert (
        payload["new_model_config_sha256"]
        == hashlib.sha256(candidate.read_bytes()).hexdigest()
    )
    assert payload["backup_directory"] == str(recovery)
    new_state = load_production_state(profile, build=_build())
    assert (
        new_state.model_copy(
            update={"model_config_sha256": old_state.model_config_sha256}
        )
        == old_state
    )
    assert (profile / "business.sqlite3").read_bytes() == business
    assert artifact.read_bytes() == b"accepted artifact\n"
    assert not (profile / "adk-trace.sqlite3").exists()

    code, payload = _call(
        _recover_arguments(recovery),
        deployment,
        build_file,
        capsys,
    )
    assert code == 0
    assert payload["ok"] is True
    assert model_file.read_bytes() == old_model
    assert manifest_file.read_bytes() == old_manifest
    assert (profile / "business.sqlite3").read_bytes() == business
    assert artifact.read_bytes() == b"accepted artifact\n"


@pytest.mark.parametrize("boundary", [1, 2, 3, 4])
def test_interrupted_apply_blocks_startup_and_recovers(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    boundary: int,
) -> None:
    """Every durable apply boundary leaves only recorded bytes recoverable."""
    deployment, profile, build_file = _profile(tmp_path)
    candidate = _candidate(tmp_path, profile)
    recovery = deployment / "recovery"
    old_model = (profile / "config" / "models.yaml").read_bytes()
    old_manifest = (profile / "runtime.json").read_bytes()
    publish = production_model_config._publish_bytes
    calls = 0

    def interrupt(path: Path, payload: bytes) -> None:
        nonlocal calls
        publish(path, payload)
        calls += 1
        if calls == boundary:
            message = "simulated process interruption"
            raise OSError(message)

    monkeypatch.setattr(production_model_config, "_publish_bytes", interrupt)
    code, payload = _call(
        _update_arguments(candidate, recovery), deployment, build_file, capsys
    )
    assert code == _ERROR_EXIT
    assert payload["ok"] is False
    assert calls == boundary
    code, payload = _call(
        ["info", "--profile", "default", "--json"], deployment, build_file, capsys
    )
    assert code == (0 if boundary == _FINAL_PUBLICATION else _ERROR_EXIT)
    if boundary != _FINAL_PUBLICATION:
        assert "explicit recovery" in str(payload["error"])

    monkeypatch.setattr(production_model_config, "_publish_bytes", publish)
    code, _ = _call(_recover_arguments(recovery), deployment, build_file, capsys)
    assert code == 0
    assert (profile / "config" / "models.yaml").read_bytes() == old_model
    assert (profile / "runtime.json").read_bytes() == old_manifest
    assert (
        _call(
            ["info", "--profile", "default", "--json"], deployment, build_file, capsys
        )[0]
        == 0
    )


@pytest.mark.parametrize("boundary", [1, 2, 3, 4])
def test_interrupted_recovery_is_repeatable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    boundary: int,
) -> None:
    """A second recovery finishes after any fsynced rollback boundary."""
    deployment, profile, build_file = _profile(tmp_path)
    candidate = _candidate(tmp_path, profile)
    recovery = deployment / "recovery"
    old_model = (profile / "config" / "models.yaml").read_bytes()
    old_manifest = (profile / "runtime.json").read_bytes()
    assert (
        _call(_update_arguments(candidate, recovery), deployment, build_file, capsys)[0]
        == 0
    )
    publish = production_model_config._publish_bytes
    calls = 0

    def interrupt(path: Path, payload: bytes) -> None:
        nonlocal calls
        publish(path, payload)
        calls += 1
        if calls == boundary:
            message = "simulated recovery interruption"
            raise OSError(message)

    monkeypatch.setattr(production_model_config, "_publish_bytes", interrupt)
    code, _ = _call(_recover_arguments(recovery), deployment, build_file, capsys)
    assert code == _ERROR_EXIT
    assert calls == boundary
    assert _call(
        ["info", "--profile", "default", "--json"], deployment, build_file, capsys
    )[0] == (0 if boundary == _FINAL_PUBLICATION else _ERROR_EXIT)
    monkeypatch.setattr(production_model_config, "_publish_bytes", publish)
    assert _call(_recover_arguments(recovery), deployment, build_file, capsys)[0] == 0
    assert _call(_recover_arguments(recovery), deployment, build_file, capsys)[0] == 0
    assert (profile / "config" / "models.yaml").read_bytes() == old_model
    assert (profile / "runtime.json").read_bytes() == old_manifest


def test_invalid_candidate_and_existing_drift_never_publish_marker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Validation refuses incomplete YAML and an already modified live config."""
    deployment, profile, build_file = _profile(tmp_path)
    candidate = tmp_path / "candidate.yaml"
    candidate.write_text("models:\n  product_vision: test/model\n", encoding="utf-8")
    recovery = deployment / "recovery"
    code, payload = _call(
        _update_arguments(candidate, recovery), deployment, build_file, capsys
    )
    assert code == _ERROR_EXIT
    assert "candidate" in str(payload["error"])
    assert not recovery.exists()
    assert not (profile / "model-config-update.json").exists()

    candidate.write_text("models: [unclosed\n", encoding="utf-8")
    code, payload = _call(
        _update_arguments(candidate, recovery), deployment, build_file, capsys
    )
    assert code == _ERROR_EXIT
    assert "candidate" in str(payload["error"])
    assert not recovery.exists()

    candidate = _candidate(tmp_path, profile)
    (profile / "config" / "models.yaml").write_bytes(b"models: {}\n")
    code, payload = _call(
        _update_arguments(candidate, recovery), deployment, build_file, capsys
    )
    assert code == _ERROR_EXIT
    assert "drift" in str(payload["error"])
    assert not recovery.exists()


def test_recovery_rejects_altered_record_but_completed_startup_is_local(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """External recovery evidence is checked only when rollback is requested."""
    deployment, profile, build_file = _profile(tmp_path)
    candidate = _candidate(tmp_path, profile)
    recovery = deployment / "recovery"
    assert (
        _call(_update_arguments(candidate, recovery), deployment, build_file, capsys)[0]
        == 0
    )
    (recovery / "old-runtime.json").write_bytes(b"tampered")
    assert (
        _call(
            ["info", "--profile", "default", "--json"], deployment, build_file, capsys
        )[0]
        == 0
    )
    code, payload = _call(_recover_arguments(recovery), deployment, build_file, capsys)
    assert code == _ERROR_EXIT
    assert "saved model update file" in str(payload["error"])
    assert (
        _call(
            ["info", "--profile", "default", "--json"], deployment, build_file, capsys
        )[0]
        == 0
    )


def test_recovery_rejects_third_hash_and_wrong_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rollback must not bless an unrelated manual configuration edit."""
    deployment, profile, build_file = _profile(tmp_path)
    candidate = _candidate(tmp_path, profile)
    recovery = deployment / "recovery"
    assert (
        _call(_update_arguments(candidate, recovery), deployment, build_file, capsys)[0]
        == 0
    )
    wrong = deployment / "another"
    wrong.mkdir(mode=0o700)
    code, payload = _call(_recover_arguments(wrong), deployment, build_file, capsys)
    assert code == _ERROR_EXIT
    assert "does not match marker" in str(payload["error"])
    (profile / "config" / "models.yaml").write_bytes(b"unrelated edit")
    code, payload = _call(_recover_arguments(recovery), deployment, build_file, capsys)
    assert code == _ERROR_EXIT
    assert "both recorded sides" in str(payload["error"])


def test_marker_identity_is_required_for_recovery(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A marker naming another state cannot authorize rollback or startup."""
    deployment, profile, build_file = _profile(tmp_path)
    candidate = _candidate(tmp_path, profile)
    recovery = deployment / "recovery"
    code, _ = _call(
        _update_arguments(candidate, recovery), deployment, build_file, capsys
    )
    assert code == 0
    marker_path = profile / "model-config-update.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["state_id"] = "11111111-1111-4111-8111-111111111111"
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    assert (
        _call(
            ["info", "--profile", "default", "--json"], deployment, build_file, capsys
        )[0]
        == _ERROR_EXIT
    )
    code, payload = _call(_recover_arguments(recovery), deployment, build_file, capsys)
    assert code == _ERROR_EXIT
    assert "receipt does not match marker" in str(payload["error"])


def test_new_update_supersedes_old_recovery_authorization(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only the most recent verified update may be rolled back."""
    deployment, profile, build_file = _profile(tmp_path)
    first = _candidate(tmp_path, profile)
    first_recovery = deployment / "first-recovery"
    code, _ = _call(
        _update_arguments(first, first_recovery), deployment, build_file, capsys
    )
    assert code == 0
    second = tmp_path / "second.yaml"
    second.write_bytes(first.read_bytes().replace(b"gpt-5.6-luna", b"gpt-6-sol", 1))
    second_recovery = deployment / "second-recovery"
    code, _ = _call(
        _update_arguments(second, second_recovery), deployment, build_file, capsys
    )
    assert code == 0
    code, payload = _call(
        _recover_arguments(first_recovery), deployment, build_file, capsys
    )
    assert code == _ERROR_EXIT
    assert "does not match marker" in str(payload["error"])
    code, _ = _call(_recover_arguments(second_recovery), deployment, build_file, capsys)
    assert code == 0
    assert (profile / "config" / "models.yaml").read_bytes() == first.read_bytes()


def test_manifest_edit_during_preparation_cannot_become_recovery_baseline(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-owner unfenced edit after validation cannot enter the receipt."""
    deployment, profile, build_file = _profile(tmp_path)
    candidate = _candidate(tmp_path, profile)
    recovery = deployment / "recovery"
    original = (profile / "runtime.json").read_bytes()
    safe_destination = production_model_config._safe_destination

    def edit_manifest(*args: object) -> Path:
        target = safe_destination(*args)
        manifest = profile / "runtime.json"
        altered = json.loads(manifest.read_text(encoding="utf-8"))
        altered["created_at"] = "2026-09-17T00:00:00Z"
        manifest.write_text(json.dumps(altered), encoding="utf-8")
        return target

    monkeypatch.setattr(production_model_config, "_safe_destination", edit_manifest)
    code, payload = _call(
        _update_arguments(candidate, recovery), deployment, build_file, capsys
    )
    assert code == _ERROR_EXIT
    assert "changed" in str(payload["error"])
    assert not recovery.exists()
    assert not (profile / "model-config-update.json").exists()
    assert (profile / "runtime.json").read_bytes() != original


def test_recovery_directory_cannot_overlap_repository_or_external_git_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A captured repository and its linked admin path stay outside recovery."""
    deployment, profile, build_file = _profile(tmp_path)
    candidate = _candidate(tmp_path, profile)
    repository = deployment / "repository"
    repository.mkdir(mode=0o700)
    git_admin = deployment / "git-admin"
    git_admin.mkdir(mode=0o700)
    (repository / ".git").write_text(f"gitdir: {git_admin}\n", encoding="utf-8")
    monkeypatch.setattr(
        production_model_config,
        "discover_registered_repositories",
        lambda _database: (repository,),
    )
    for target in (repository / "recovery", git_admin / "recovery"):
        code, payload = _call(
            _update_arguments(candidate, target), deployment, build_file, capsys
        )
        assert code == _ERROR_EXIT
        assert "overlaps captured state" in str(payload["error"])
        assert not target.exists()


def test_update_refuses_symlink_and_profile_tree_destination(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Candidate and recovery paths cannot escape the owned path boundary."""
    deployment, profile, build_file = _profile(tmp_path)
    candidate = _candidate(tmp_path, profile)
    link = tmp_path / "linked.yaml"
    link.symlink_to(candidate)
    recovery = deployment / "recovery"
    code, _ = _call(_update_arguments(link, recovery), deployment, build_file, capsys)
    assert code == _ERROR_EXIT
    assert not recovery.exists()
    nested = profile / "artifacts" / "recovery"
    code, payload = _call(
        _update_arguments(candidate, nested), deployment, build_file, capsys
    )
    assert code == _ERROR_EXIT
    assert "overlaps production profiles" in str(payload["error"])


def test_update_refuses_wrong_owner_and_held_fence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Neither an unauthorized owner nor a live reader can start publication."""
    deployment, profile, build_file = _profile(tmp_path)
    candidate = _candidate(tmp_path, profile)
    recovery = deployment / "recovery"
    code = main(
        _update_arguments(candidate, recovery),
        deployment_root=deployment,
        build_path=build_file,
        expected_build_owner_uid=_uid(),
        expected_state_owner_uid=_uid() + 1,
    )
    capsys.readouterr()
    assert code == _ERROR_EXIT
    assert not recovery.exists()
    with runtime_fence(deployment):
        code, payload = _call(
            _update_arguments(candidate, recovery), deployment, build_file, capsys
        )
    assert code == _ERROR_EXIT
    assert "fence" in str(payload["error"])
    assert not recovery.exists()


@pytest.mark.parametrize("saved_boundary", [1, 2, 3, 4, 5])
def test_interrupted_private_staging_leaves_live_pair_unchanged(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    saved_boundary: int,
) -> None:
    """Before marker publication, partial private evidence cannot affect startup."""
    deployment, profile, build_file = _profile(tmp_path)
    candidate = _candidate(tmp_path, profile)
    recovery = deployment / "recovery"
    old_model = (profile / "config" / "models.yaml").read_bytes()
    old_manifest = (profile / "runtime.json").read_bytes()
    write = production_model_config._write_private_file
    calls = 0

    def interrupt(path: Path, payload: bytes) -> None:
        nonlocal calls
        write(path, payload)
        calls += 1
        if calls == saved_boundary:
            message = "simulated staging interruption"
            raise OSError(message)

    monkeypatch.setattr(production_model_config, "_write_private_file", interrupt)
    assert (
        _call(_update_arguments(candidate, recovery), deployment, build_file, capsys)[0]
        == _ERROR_EXIT
    )
    assert calls == saved_boundary
    assert not (profile / "model-config-update.json").exists()
    assert (profile / "config" / "models.yaml").read_bytes() == old_model
    assert (profile / "runtime.json").read_bytes() == old_manifest
    assert (
        _call(
            ["info", "--profile", "default", "--json"], deployment, build_file, capsys
        )[0]
        == 0
    )
