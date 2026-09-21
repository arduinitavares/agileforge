# tests/utils/test_platform_support.py
"""Native execution outside Linux is refused before any state is touched."""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path

import pytest

from utils import platform_support
from utils.platform_support import (
    UNSUPPORTED_PLATFORM_EXIT_CODE,
    UnsupportedPlatformError,
    require_linux,
    unsupported_platform_message,
)

_LAUNCHER = Path(__file__).resolve().parents[2] / "agileforge-dev"
_USAGE_ERROR = 2

pytestmark = pytest.mark.usefixtures("_real_platform_guard")


@pytest.mark.parametrize("platform", ["linux", "linux2"])
def test_require_linux_accepts_linux(platform: str) -> None:
    """Linux interpreter tags pass the guard."""
    require_linux(platform=platform)


@pytest.mark.parametrize("platform", ["darwin", "win32", "cygwin", "freebsd14"])
def test_require_linux_refuses_other_platforms(platform: str) -> None:
    """Every non-Linux tag is refused with an actionable message."""
    with pytest.raises(UnsupportedPlatformError) as info:
        require_linux(platform=platform)
    message = str(info.value)
    assert platform in message
    assert "Linux" in message
    assert "docs/linux-containers.md" in message


def test_require_linux_reads_current_platform_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default platform comes from the patchable seam."""
    monkeypatch.setattr(platform_support, "current_platform", lambda: "darwin")
    with pytest.raises(UnsupportedPlatformError):
        require_linux()
    monkeypatch.setattr(platform_support, "current_platform", lambda: "linux")
    require_linux()


def test_unsupported_platform_message_names_compose_runtime() -> None:
    """The refusal points at the supported Compose runtime."""
    message = unsupported_platform_message("win32")
    assert message.startswith("AgileForge runs only on Linux")
    assert "docker compose" in message


def test_exit_code_is_usage_error() -> None:
    """Refusal shares the usage-error status of the CLIs."""
    assert UNSUPPORTED_PLATFORM_EXIT_CODE == _USAGE_ERROR


def _fake_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_support, "current_platform", lambda: "darwin")


def test_dev_main_refuses_before_touching_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The developer launcher exits before creating any checkout state."""
    from cli import dev_main  # noqa: PLC0415

    _fake_darwin(monkeypatch)
    before = sorted(tmp_path.rglob("*"))

    code = dev_main.main(["info", "--json"], checkout_root=tmp_path)

    assert code == UNSUPPORTED_PLATFORM_EXIT_CODE
    assert sorted(tmp_path.rglob("*")) == before
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "AgileForge runs only on Linux" in captured.err
    assert "darwin" in captured.err


def test_product_cli_refuses_with_json_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The product CLI reports the refusal as its JSON error envelope."""
    from cli import main as product_main  # noqa: PLC0415

    _fake_darwin(monkeypatch)

    code = product_main.main(["project", "list"])

    assert code == UNSUPPORTED_PLATFORM_EXIT_CODE
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "AgileForge runs only on Linux" in payload["error"]


def test_container_runtime_refuses_before_reading_build_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The container runtime refuses before reading build identity or profiles."""
    from cli import container_runtime  # noqa: PLC0415

    _fake_darwin(monkeypatch)
    missing_build = tmp_path / "missing-build.json"

    code = container_runtime.main(
        ["info", "--profile", "default", "--json"],
        deployment_root=tmp_path,
        build_path=missing_build,
    )

    assert code == UNSUPPORTED_PLATFORM_EXIT_CODE
    assert not (tmp_path / "profiles").exists()
    assert "AgileForge runs only on Linux" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_api_lifespan_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The API lifespan refuses before opening the business database."""
    import api  # noqa: PLC0415

    _fake_darwin(monkeypatch)
    with pytest.raises(UnsupportedPlatformError):
        async with api.lifespan(api.app):
            pass


@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("sh") is None,
    reason="shell launcher guard is exercised on Linux only",
)
def test_shell_launcher_refuses_faked_darwin_uname(tmp_path: Path) -> None:
    """The shell launcher refuses a non-Linux kernel before invoking uv."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uname = fake_bin / "uname"
    uname.write_text("#!/bin/sh\nprintf '%s\\n' Darwin\n", encoding="utf-8")
    uname.chmod(0o755)
    uv = fake_bin / "uv"
    uv.write_text("#!/bin/sh\nprintf '%s\\n' 'uv must not run' >&2\nexit 99\n")
    uv.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"

    completed = subprocess.run(  # noqa: S603  # nosec B603
        [str(_LAUNCHER), "info", "--json"],
        cwd=_LAUNCHER.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == UNSUPPORTED_PLATFORM_EXIT_CODE
    assert "AgileForge runs only on Linux" in completed.stderr
    assert "uv must not run" not in completed.stderr
    assert completed.stdout == ""
