"""POSIX retained-file coverage; native execution belongs to Linux/macOS CI."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import pytest

from cli import dev_main

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="requires native POSIX file semantics"
)


@pytest.mark.parametrize("kind", ["fifo", "directory", "symlink"])
def test_posix_rejects_unsafe_leaf_types(tmp_path: Path, kind: str) -> None:
    """Reject unsafe objects before parsing; opening a FIFO must not block."""
    selected = tmp_path / "provider.env"
    if kind == "fifo":
        mkfifo = getattr(os, "mkfifo", None)
        assert callable(mkfifo)
        mkfifo(selected)
    elif kind == "directory":
        selected.mkdir()
    else:
        target = tmp_path / "target.env"
        target.write_text("OPEN_ROUTER_API_KEY=dummy-posix-sentinel", encoding="utf-8")
        selected.symlink_to(target)
    with pytest.raises(dev_main.DeveloperCommandError, match="regular file"):
        dev_main._provider_environment(selected)


def test_posix_trusts_parent_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reader promises leaf no-follow, not ancestor containment."""
    monkeypatch.delenv("OPEN_ROUTER_API_KEY", raising=False)
    parent = tmp_path / "real"
    parent.mkdir()
    selected = parent / "provider.env"
    selected.write_text("OPEN_ROUTER_API_KEY=dummy-posix-sentinel", encoding="utf-8")
    link = tmp_path / "parent-link"
    link.symlink_to(parent, target_is_directory=True)
    assert dev_main._provider_environment(link / selected.name) == {
        "OPEN_ROUTER_API_KEY": "dummy-posix-sentinel"
    }
