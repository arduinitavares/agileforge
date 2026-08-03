"""Regression coverage for the current high-signal ty cleanup slice."""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404
from pathlib import Path

# Regression test runs fixed tool argv in a subprocess.


def test_ty_query_slice_is_clean() -> None:
    """Verify ty query slice is clean."""
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    uv_path = shutil.which("uv")
    if uv_path is None:
        msg = "uv executable not found"
        raise RuntimeError(msg)

    result = subprocess.run(  # noqa: S603  # nosec B603
        [
            uv_path,
            "run",
            "ty",
            "check",
            "tools/story_query_tools.py",
            "tools/db_tools.py",
            "repositories/story.py",
            "services/read_projections.py",
            "--output-format",
            "concise",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
