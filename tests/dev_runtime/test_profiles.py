"""Tests for worktree-local runtime profile safety and provenance."""

from __future__ import annotations

import hashlib
import json
import stat
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import pytest
from git import Git
from pydantic import ValidationError

from cli.dev_profiles import (
    CheckoutProvenance,
    ProfileMode,
    RuntimeProfile,
    initialize_profile_record,
    load_profile,
    profile_environment,
    profile_paths,
    reset_profile,
    resolve_checkout_root,
    touch_profile_last_used,
)

if TYPE_CHECKING:
    from pathlib import Path

_MANIFEST_MODE = 0o600


def _git(checkout: Path, *arguments: str) -> str:
    output = Git().execute(
        command=["git", "-C", str(checkout), *arguments],
    )
    return cast("str", output).strip()


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """Create a minimal tracked checkout with runtime fingerprint inputs."""
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-b", "feature/profile-core")
    _git(root, "config", "user.name", "Profile Tests")
    _git(root, "config", "user.email", "profiles@example.invalid")
    (root / "config").mkdir()
    (root / "models").mkdir()
    (root / "README.md").write_text("profile fixture\n", encoding="utf-8")
    (root / "config" / "models.yaml").write_text(
        "models:\n  default: test-model\n",
        encoding="utf-8",
    )
    (root / "agile_sqlmodel.py").write_text("SCHEMA = 1\n", encoding="utf-8")
    (root / "models" / "__init__.py").write_text("", encoding="utf-8")
    (root / "models" / "core.py").write_text("MODEL = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    return root


@pytest.mark.parametrize(
    "profile_name",
    ["a", "local", "ci.profile_1-2", "a" * 64],
)
def test_profile_paths_accept_valid_names(tmp_path: Path, profile_name: str) -> None:
    """Accept every profile name allowed by the bounded grammar."""
    paths = profile_paths(checkout_root=tmp_path, profile_name=profile_name)

    assert paths.root.name == profile_name
    assert paths.manifest == paths.root / "profile.json"
    assert paths.business_database == paths.root / "business.sqlite3"
    assert paths.trace_database == paths.root / "adk-trace.sqlite3"


@pytest.mark.parametrize(
    "profile_name",
    ["", ".", "..", "../local", "local/name", "Local", "-local", "a" * 65, "x\n"],
)
def test_profile_paths_reject_unsafe_names(
    tmp_path: Path,
    profile_name: str,
) -> None:
    """Reject traversal, control characters, and out-of-grammar names."""
    with pytest.raises(ValueError, match="profile name"):
        profile_paths(checkout_root=tmp_path, profile_name=profile_name)


def test_resolve_checkout_root_from_nested_path(checkout: Path) -> None:
    """Resolve nested anchors to the canonical Git checkout root."""
    nested = checkout / "nested" / "directory"
    nested.mkdir(parents=True)

    assert resolve_checkout_root(nested) == checkout.resolve()


def test_checkout_provenance_records_branch_and_commit(checkout: Path) -> None:
    """Record branch and full commit provenance."""
    profile = initialize_profile_record(checkout, "branch")
    provenance = profile.checkout

    assert provenance.root == checkout.resolve()
    assert provenance.branch == "feature/profile-core"
    assert provenance.commit == _git(checkout, "rev-parse", "HEAD")


def test_checkout_provenance_supports_detached_head(checkout: Path) -> None:
    """Represent detached HEAD without inventing a branch name."""
    commit = _git(checkout, "rev-parse", "HEAD")
    _git(checkout, "checkout", "--detach", commit)

    provenance = initialize_profile_record(checkout, "detached").checkout

    assert provenance.branch is None
    assert provenance.commit == commit


def test_same_profile_name_is_isolated_by_checkout(tmp_path: Path) -> None:
    """Keep identical profile names isolated by checkout root."""
    first = profile_paths(checkout_root=tmp_path / "one", profile_name="local")
    second = profile_paths(checkout_root=tmp_path / "two", profile_name="local")

    assert first.root != second.root
    assert first.business_database != second.business_database
    assert first.trace_database != second.trace_database


def test_profile_contracts_are_frozen_and_forbid_unknown_fields(
    checkout: Path,
) -> None:
    """Freeze profile records and reject undeclared data."""
    profile = initialize_profile_record(checkout, "frozen")

    with pytest.raises(ValidationError, match="frozen_instance"):
        profile.name = "changed"

    with pytest.raises(ValidationError, match="extra_forbidden"):
        CheckoutProvenance.model_validate(
            {**profile.checkout.model_dump(mode="json"), "token": "secret"}
        )


def test_mode_contracts_reject_invalid_expected_commits(checkout: Path) -> None:
    """Enforce mode-specific expected commit contracts in the model."""
    development = initialize_profile_record(checkout, "development")
    payload = development.model_dump(mode="json")

    with pytest.raises(ValidationError, match="expected_commit"):
        RuntimeProfile.model_validate({**payload, "expected_commit": "a" * 40})

    with pytest.raises(ValidationError, match="expected_commit"):
        RuntimeProfile.model_validate(
            {
                **payload,
                "mode": "acceptance",
                "expected_commit": "ABC",
            }
        )


def test_initialize_persists_private_atomic_manifest(checkout: Path) -> None:
    """Persist a private manifest without leaving a temporary file."""
    profile = initialize_profile_record(checkout, "atomic")
    paths = profile_paths(checkout, "atomic")

    assert load_profile(checkout, "atomic") == profile
    assert stat.S_IMODE(paths.manifest.stat().st_mode) == _MANIFEST_MODE
    assert list(paths.root.glob(".profile.*.tmp")) == []


def test_initialize_refuses_preexisting_root_without_modifying_databases(
    checkout: Path,
) -> None:
    """Refuse stale database adoption from a manifest-free profile root."""
    paths = profile_paths(checkout, "stale-databases")
    paths.root.mkdir(parents=True)
    business_bytes = b"stale business database\x00\x01"
    trace_bytes = b"stale trace database\x02\x03"
    paths.business_database.write_bytes(business_bytes)
    paths.trace_database.write_bytes(trace_bytes)

    with pytest.raises(FileExistsError, match="profile root already exists"):
        initialize_profile_record(checkout, "stale-databases")

    assert paths.business_database.read_bytes() == business_bytes
    assert paths.trace_database.read_bytes() == trace_bytes
    assert set(paths.root.iterdir()) == {
        paths.business_database,
        paths.trace_database,
    }


def test_profile_fingerprints_are_deterministic(checkout: Path) -> None:
    """Hash model configuration and sorted tracked schema sources canonically."""
    profile = initialize_profile_record(checkout, "fingerprint")
    expected_schema = hashlib.sha256()
    for relative in (
        "agile_sqlmodel.py",
        "models/__init__.py",
        "models/core.py",
    ):
        expected_schema.update(relative.encode("utf-8"))
        expected_schema.update(b"\0")
        expected_schema.update((checkout / relative).read_bytes())
        expected_schema.update(b"\0")

    assert (
        profile.model_config_sha256
        == hashlib.sha256(
            (checkout / "config" / "models.yaml").read_bytes()
        ).hexdigest()
    )
    assert profile.schema_source_sha256 == expected_schema.hexdigest()


@pytest.mark.parametrize("secret_field", ["api_key", "password", "token"])
def test_manifest_rejects_secret_shaped_fields(
    checkout: Path,
    secret_field: str,
) -> None:
    """Reject likely credential fields instead of persisting them."""
    initialize_profile_record(checkout, "secret-free")
    paths = profile_paths(checkout, "secret-free")
    payload = json.loads(paths.manifest.read_text(encoding="utf-8"))
    payload[secret_field] = "must-not-persist"
    paths.manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValidationError, match="extra_forbidden"):
        load_profile(checkout, "secret-free")


def test_load_rejects_unknown_nested_manifest_field(checkout: Path) -> None:
    """Reject unknown fields in nested provenance records."""
    initialize_profile_record(checkout, "unknown")
    paths = profile_paths(checkout, "unknown")
    payload = json.loads(paths.manifest.read_text(encoding="utf-8"))
    payload["checkout"]["unexpected"] = True
    paths.manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValidationError, match="extra_forbidden"):
        load_profile(checkout, "unknown")


def test_load_rejects_model_config_drift(checkout: Path) -> None:
    """Fail closed when the selected model configuration changes."""
    initialize_profile_record(checkout, "model-drift")
    (checkout / "config" / "models.yaml").write_text(
        "models:\n  default: changed\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="model configuration drift"):
        load_profile(checkout, "model-drift")


def test_load_rejects_schema_source_drift(checkout: Path) -> None:
    """Fail closed when a tracked model source changes."""
    initialize_profile_record(checkout, "schema-drift")
    (checkout / "models" / "core.py").write_text("MODEL = 2\n", encoding="utf-8")

    with pytest.raises(ValueError, match="schema source drift"):
        load_profile(checkout, "schema-drift")


def test_untracked_model_file_does_not_change_schema_fingerprint(
    checkout: Path,
) -> None:
    """Exclude untracked model files from the canonical schema hash."""
    initialize_profile_record(checkout, "untracked")
    (checkout / "models" / "scratch.py").write_text("SCRATCH = 1\n", encoding="utf-8")

    load_profile(checkout, "untracked")


def test_acceptance_initialization_requires_current_full_commit(checkout: Path) -> None:
    """Require acceptance initialization to pin the current full commit."""
    commit = _git(checkout, "rev-parse", "HEAD")

    with pytest.raises(ValueError, match="expected_commit"):
        initialize_profile_record(
            checkout,
            "missing-sha",
            mode=ProfileMode.ACCEPTANCE,
        )
    with pytest.raises(ValueError, match="current commit"):
        initialize_profile_record(
            checkout,
            "wrong-sha",
            mode=ProfileMode.ACCEPTANCE,
            expected_commit="0" * 40,
        )

    profile = initialize_profile_record(
        checkout,
        "acceptance",
        mode=ProfileMode.ACCEPTANCE,
        expected_commit=commit,
    )
    assert profile.expected_commit == commit


def test_acceptance_profile_rejects_commit_advancement(checkout: Path) -> None:
    """Reject acceptance profile use after checkout commit advancement."""
    commit = _git(checkout, "rev-parse", "HEAD")
    initialize_profile_record(
        checkout,
        "acceptance",
        mode=ProfileMode.ACCEPTANCE,
        expected_commit=commit,
    )
    (checkout / "README.md").write_text("advanced\n", encoding="utf-8")
    _git(checkout, "add", "README.md")
    _git(checkout, "commit", "-m", "advance")

    with pytest.raises(ValueError, match="acceptance commit mismatch"):
        load_profile(checkout, "acceptance")


def test_development_profile_allows_commit_advancement(checkout: Path) -> None:
    """Allow development commit advancement when schema inputs stay stable."""
    initialize_profile_record(checkout, "development")
    (checkout / "README.md").write_text("advanced\n", encoding="utf-8")
    _git(checkout, "add", "README.md")
    _git(checkout, "commit", "-m", "advance")

    load_profile(checkout, "development")


def test_load_enforces_distinct_owned_database_paths(checkout: Path) -> None:
    """Reject database aliases and manifest path substitution."""
    initialize_profile_record(checkout, "database-paths")
    paths = profile_paths(checkout, "database-paths")
    payload = json.loads(paths.manifest.read_text(encoding="utf-8"))
    payload["trace_database"] = payload["business_database"]
    paths.manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="database paths"):
        load_profile(checkout, "database-paths")


def test_load_rejects_symlinked_database(checkout: Path, tmp_path: Path) -> None:
    """Reject symlink substitution at a reserved database path."""
    initialize_profile_record(checkout, "linked-database")
    paths = profile_paths(checkout, "linked-database")
    target = tmp_path / "outside.sqlite3"
    target.touch()
    paths.business_database.symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        load_profile(checkout, "linked-database")


def test_profile_paths_reject_symlinked_state_ancestor(
    checkout: Path,
    tmp_path: Path,
) -> None:
    """Reject symlinked checkout-local state ancestors."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (checkout / ".agileforge").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        profile_paths(checkout, "linked-state")


def test_profile_environment_contains_only_runtime_controls(checkout: Path) -> None:
    """Return exactly the three non-secret runtime controls."""
    profile = initialize_profile_record(checkout, "environment")

    assert profile_environment(profile) == {
        "AGILEFORGE_DB_URL": (f"sqlite:///{profile.business_database.as_posix()}"),
        "AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL": (
            f"sqlite:///{profile.trace_database.as_posix()}"
        ),
        "MODEL_CONFIG_PATH": str(profile.model_config_path),
    }


def test_touch_advances_only_last_used_after_validation(checkout: Path) -> None:
    """Advance only last-use time after complete profile validation."""
    created_at = datetime(2026, 8, 3, 10, tzinfo=UTC)
    profile = initialize_profile_record(checkout, "touch", now=created_at)
    touched_at = created_at + timedelta(minutes=5)

    touched = touch_profile_last_used(checkout, "touch", now=touched_at)

    assert touched.last_used_at == touched_at
    assert touched.created_at == profile.created_at
    assert touched.model_copy(update={"last_used_at": profile.last_used_at}) == profile


def test_touch_failure_keeps_original_manifest(
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the original manifest when atomic replacement fails."""
    initialize_profile_record(checkout, "atomic-touch")
    paths = profile_paths(checkout, "atomic-touch")
    original = paths.manifest.read_bytes()

    def fail_replace(source: Path, destination: Path) -> Path:
        del source, destination
        message = "replace failed"
        raise OSError(message)

    monkeypatch.setattr("cli.dev_profiles.Path.replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        touch_profile_last_used(checkout, "atomic-touch")

    assert paths.manifest.read_bytes() == original
    assert list(paths.root.glob(".profile.*.tmp")) == []


def test_reset_requires_exact_confirmation(checkout: Path) -> None:
    """Require exact profile-name confirmation before reset."""
    initialize_profile_record(checkout, "reset-me")

    with pytest.raises(ValueError, match="confirmation"):
        reset_profile(checkout, "reset-me", "RESET-ME")

    assert profile_paths(checkout, "reset-me").root.is_dir()


def test_reset_refuses_symlinked_profile_content(
    checkout: Path,
    tmp_path: Path,
) -> None:
    """Refuse reset when any owned content is a symlink."""
    initialize_profile_record(checkout, "linked-reset")
    paths = profile_paths(checkout, "linked-reset")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    (paths.root / "linked.txt").symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        reset_profile(checkout, "linked-reset", "linked-reset")

    assert outside.read_text(encoding="utf-8") == "keep\n"
    assert paths.root.is_dir()


def test_reset_removes_only_owned_profile_and_reports_paths(checkout: Path) -> None:
    """Remove and report one owned profile without touching its sibling."""
    initialize_profile_record(checkout, "remove-me")
    initialize_profile_record(checkout, "keep-me")
    paths = profile_paths(checkout, "remove-me")

    removed = reset_profile(checkout, "remove-me", "remove-me")

    assert paths.manifest in removed
    assert paths.root in removed
    assert not paths.root.exists()
    assert profile_paths(checkout, "keep-me").root.is_dir()
