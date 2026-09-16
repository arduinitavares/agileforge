"""Synthetic credentials never reach supervised output or persistent logs."""

from __future__ import annotations

import logging
import os
import subprocess  # nosec B404
import sys
from typing import TYPE_CHECKING

import pytest

from cli import dev_server
from utils import logging_config

if TYPE_CHECKING:
    from pathlib import Path

CANARY = "synthetic-secret-redaction-canary"


def test_file_log_redacts_message_arguments_and_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The final formatted record must redact exception text as well as messages."""
    monkeypatch.setenv("OPEN_ROUTER_API_KEY", CANARY)
    target = tmp_path / "app.log"
    handler = logging_config._build_handler(target, level=logging.INFO)
    logger = logging.getLogger("synthetic-redaction")
    logger.propagate = False
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("provider key: %s", CANARY)
        try:
            raise ValueError(CANARY)  # noqa: TRY301 - simulate a provider traceback
        except ValueError:
            logger.exception("synthetic provider failure")
    finally:
        handler.close()
        logger.removeHandler(handler)
    payload = target.read_text()
    assert CANARY not in payload
    assert "[REDACTED]" in payload
    assert "synthetic provider failure" in payload


@pytest.mark.skipif(os.name != "posix", reason="POSIX process supervision")
def test_supervised_stdout_and_stderr_redact_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """Raw third-party prints are redacted even when they bypass Python logging."""
    real_spawn = subprocess.Popen
    script = f"import sys; print({CANARY!r}); print({CANARY!r}, file=sys.stderr)"

    def spawn(
        _arguments: object,
        *,
        cwd: Path,
        env: dict[str, str],
        stdout: int,
        stderr: int,
        start_new_session: bool,
    ) -> subprocess.Popen[bytes]:
        return real_spawn(
            (sys.executable, "-c", script),
            cwd=cwd,
            env=env,
            stdout=stdout,
            stderr=stderr,
            start_new_session=start_new_session,
        )

    monkeypatch.setattr(dev_server.subprocess, "Popen", spawn)
    child = dev_server.start_ui(
        checkout_root=tmp_path,
        environment={},
        port=0,
        reload=False,
        owned_process_group=True,
        redact_values=(CANARY,),
    )
    child.process.wait(timeout=5)
    dev_server.stop_ui(child)
    output = capfd.readouterr().err
    assert CANARY not in output
    assert output.count("[REDACTED]") == 2  # noqa: PLR2004


def test_stream_redaction_handles_a_secret_split_between_chunks() -> None:
    """A read boundary cannot expose either part of a credential."""
    from utils.secret_redaction import SecretRedactor  # noqa: PLC0415

    redactor = SecretRedactor((CANARY,))
    pieces = [
        redactor.feed(b"prefix: " + CANARY[:15].encode()),
        redactor.feed(CANARY[15:].encode() + b" suffix\n"),
        redactor.finish(),
    ]
    assert b"".join(pieces) == b"prefix: [REDACTED] suffix\n"
