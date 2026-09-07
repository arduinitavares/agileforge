"""Real Windows installed-venv ownership regressions."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import psutil
import pytest

import scripts.verify_distribution as distribution_verifier
from scripts.verify_distribution import IsolationLayout

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

pytestmark = [
    pytest.mark.skipif(
        sys.platform != "win32", reason="Windows installed interpreter ownership"
    ),
    pytest.mark.allow_hosts(["127.0.0.1"]),
]

_RUNTIME_PROBE = """
import json
import os
import sys

print(json.dumps({
    "pid": os.getpid(),
    "ppid": os.getppid(),
    "executable": sys.executable,
    "base_executable": getattr(sys, "_base_executable", None),
    "prefix": sys.prefix,
    "base_prefix": sys.base_prefix,
    "launcher_present": "__PYVENV_LAUNCHER__" in os.environ,
}), flush=True)
sys.stdin.read(1)
"""


def _controlled_environment(tmp_path: Path) -> dict[str, str]:
    """Retain only Windows process essentials in the real-venv probe."""
    environment = {
        name: value
        for name in ("PATH", "SystemRoot", "TEMP", "TMP")
        if (value := os.environ.get(name))
    }
    environment.update(
        {
            "HOME": str(tmp_path / "home"),
            "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
            "UV_LINK_MODE": "copy",
            "UV_NO_PROGRESS": "1",
        }
    )
    return environment


def _create_installed_venv(
    layout: IsolationLayout, *, environment: Mapping[str, str]
) -> Path:
    """Create the same venv path shape used by an installed uv tool."""
    uv_executable = shutil.which("uv")
    assert uv_executable is not None
    venv_root = layout.tool_dir / "agileforge"
    distribution_verifier._run_checked(
        (
            uv_executable,
            "venv",
            "--python",
            sys.executable,
            str(venv_root),
        ),
        cwd=layout.cwd,
        environment=environment,
        timeout=30,
    )
    return venv_root / "Scripts" / "python.exe"


def _run_runtime_probe(
    executable: Path, *, environment: Mapping[str, str]
) -> tuple[int, dict[str, object]]:
    """Return Popen and serving identities after a bounded graceful exit."""
    process = psutil.Popen(
        (str(executable), "-I", "-c", _RUNTIME_PROBE),
        env=dict(environment),
        stdin=-1,
        stdout=-1,
        stderr=-1,
        text=True,
    )
    stdout, stderr = process.communicate(input="x", timeout=15)
    assert process.returncode == 0, stderr
    payload = json.loads(stdout)
    assert isinstance(payload, dict)
    return process.pid, cast("dict[str, object]", payload)


def test_real_windows_distribution_owns_the_installed_serving_interpreter(
    tmp_path: Path,
) -> None:
    """Bypass the redirector while preserving the installed venv identity."""
    layout = IsolationLayout.create(tmp_path / "installation")
    environment = _controlled_environment(tmp_path)
    installed_python = _create_installed_venv(layout, environment=environment)

    redirector_pid, redirector_runtime = _run_runtime_probe(
        installed_python,
        environment=environment,
    )
    serving_pid = redirector_runtime["pid"]
    assert isinstance(serving_pid, int)
    assert redirector_pid != serving_pid
    assert redirector_runtime["ppid"] == redirector_pid
    assert not psutil.pid_exists(redirector_pid)
    assert not psutil.pid_exists(serving_pid)

    selector = getattr(distribution_verifier, "_installed_api_python", None)
    assert selector is not None, (
        "distribution launch still owns the Windows venv redirector: "
        f"Popen.pid={redirector_pid}, serving pid={serving_pid}"
    )
    typed_selector = cast(
        "Callable[..., tuple[Path, dict[str, str]]]",
        selector,
    )
    ambient = dict(environment, __PYVENV_LAUNCHER__="ambient-must-not-leak")
    selected, child_environment = typed_selector(layout, environment=ambient)

    assert child_environment["__PYVENV_LAUNCHER__"] == str(installed_python)
    owned_pid, owned_runtime = _run_runtime_probe(
        selected,
        environment=child_environment,
    )
    assert owned_runtime["pid"] == owned_pid
    assert Path(cast("str", owned_runtime["executable"])).resolve() == (
        installed_python.resolve()
    )
    assert Path(cast("str", owned_runtime["prefix"])).resolve() == (
        installed_python.parents[1].resolve()
    )
    assert owned_runtime["launcher_present"] is False
    assert not psutil.pid_exists(owned_pid)
