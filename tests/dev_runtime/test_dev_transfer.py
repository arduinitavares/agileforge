"""Checkout restore preserves source ownership and tracked configuration."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from git import Repo
from sqlalchemy import create_engine
from sqlmodel import SQLModel

from cli import dev_transfer
from cli.dev_main import build_parser
from cli.dev_profiles import RuntimeProfile, load_profile, profile_paths
from cli.dev_transfer import restore_development_profile
from cli.state_transfer import StateLayout, TransferError, backup_state
from models import db  # noqa: F401 - register all current SQLModel tables
from utils.runtime_fence import FenceError, runtime_fence

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX maintenance fencing")


@pytest.fixture
def transfer_checkout(tmp_path: Path) -> Path:
    """Create a small real checkout with the fingerprint inputs."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    repository = Repo.init(checkout)
    with repository.config_writer() as config:
        config.set_value("user", "name", "Synthetic Transfer")
        config.set_value("user", "email", "transfer@example.invalid")
    (checkout / ".gitignore").write_text(".agileforge/\n", encoding="utf-8")
    (checkout / "config").mkdir()
    (checkout / "config/models.yaml").write_text("models: {}\n", encoding="utf-8")
    (checkout / "models").mkdir()
    (checkout / "models/core.py").write_text("# schema source\n", encoding="utf-8")
    (checkout / "agile_sqlmodel.py").write_text(
        "# bootstrap source\n", encoding="utf-8"
    )
    repository.index.add(
        [".gitignore", "config/models.yaml", "models/core.py", "agile_sqlmodel.py"]
    )
    repository.index.commit("synthetic checkout")
    return checkout


@pytest.fixture
def development_bundle(tmp_path: Path) -> Path:
    """Build current-schema synthetic state with an intentionally absent trace."""
    root = tmp_path / "source"
    root.mkdir()
    business = root / "business.sqlite3"
    engine = create_engine(f"sqlite:///{business}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()
    artifacts = root / "artifacts"
    artifacts.mkdir()
    (artifacts / "accepted.txt").write_bytes(b"immutable artifact\x00\n")
    config = root / "models.yaml"
    config.write_text("models: {}\n", encoding="utf-8")
    return backup_state(
        StateLayout(root, business, root / "absent-trace.sqlite3", artifacts, config),
        tmp_path / "bundle",
    )


def test_restore_refuses_different_tracked_model_configuration(
    transfer_checkout: Path, development_bundle: Path
) -> None:
    """A transfer cannot overwrite the checkout's tracked model decisions."""
    config = transfer_checkout / "config/models.yaml"
    config.write_bytes(b"models: different\n")
    before = config.read_bytes()
    with pytest.raises(TransferError, match="model configuration"):
        restore_development_profile(transfer_checkout, "restored", development_bundle)
    assert config.read_bytes() == before
    assert not profile_paths(transfer_checkout, "restored").root.exists()


def test_restore_publishes_owned_profile_after_verified_payload(
    transfer_checkout: Path, development_bundle: Path
) -> None:
    """Restored state uses new checkout identity and preserves absent traces."""
    before = (transfer_checkout / "config/models.yaml").read_bytes()
    result = restore_development_profile(
        transfer_checkout, "restored", development_bundle
    )
    profile = load_profile(transfer_checkout, "restored")
    paths = profile_paths(transfer_checkout, "restored")
    assert result == profile
    assert profile.checkout.root == transfer_checkout
    assert paths.business_database.is_file()
    assert not paths.trace_database.exists()
    assert (
        paths.artifacts / "accepted.txt"
    ).read_bytes() == b"immutable artifact\x00\n"
    assert (transfer_checkout / "config/models.yaml").read_bytes() == before
    with pytest.raises(FileExistsError):
        restore_development_profile(transfer_checkout, "restored", development_bundle)


def test_restore_corrupt_bundle_never_reserves_profile(
    transfer_checkout: Path, development_bundle: Path
) -> None:
    """Verification failure cannot leave an apparently initialized profile."""
    (development_bundle / "artifacts/accepted.txt").write_bytes(b"corrupt")
    with pytest.raises(TransferError):
        restore_development_profile(transfer_checkout, "restored", development_bundle)
    assert not profile_paths(transfer_checkout, "restored").root.exists()


def test_backup_takes_exclusive_ownership_before_loading_profile(
    transfer_checkout: Path,
    development_bundle: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Profile validation reads mutable state and belongs inside maintenance."""
    restore_development_profile(transfer_checkout, "source", development_bundle)
    original_load = dev_transfer.load_profile
    roots = dev_transfer._maintenance_roots(transfer_checkout)

    def checked_load(checkout: Path, name: str) -> RuntimeProfile:
        with pytest.raises(FenceError), runtime_fence(roots[0]):
            pytest.fail("profile read escaped maintenance")  # ty: ignore[invalid-argument-type]
        return original_load(checkout, name)

    monkeypatch.setattr(dev_transfer, "load_profile", checked_load)
    result = dev_transfer.backup_development_profile(
        transfer_checkout, "source", tmp_path / "second-bundle"
    )
    assert result.is_dir()


def test_restored_profile_rejects_missing_relocation_record(
    transfer_checkout: Path,
    development_bundle: Path,
) -> None:
    """Removing relocation metadata cannot silently make a restore usable."""
    restore_development_profile(transfer_checkout, "restored", development_bundle)
    (
        profile_paths(transfer_checkout, "restored").root
        / "repository-relocations.json"
    ).unlink()
    with pytest.raises(FileNotFoundError):
        load_profile(transfer_checkout, "restored")


@pytest.mark.parametrize(
    "argv",
    [
        ["backup", "--profile", "source", "--destination", "/backup"],
        ["verify-backup", "--bundle", "/backup"],
        ["restore", "--profile", "target", "--bundle", "/backup"],
    ],
)
def test_transfer_commands_are_explicit(argv: list[str]) -> None:
    """No transfer command silently chooses a profile or backup destination."""
    assert build_parser().parse_args(argv).command == argv[0]
