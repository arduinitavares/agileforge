"""Synthetic credentials never reach supervised output or persistent logs."""

from __future__ import annotations

import json
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

CANARY = 'synthetic-secret-"quote"\\slash\ncaf\u00e9'


def _serialized_canary_forms() -> tuple[str, ...]:
    """Return the raw and escaped spellings that must never reach output."""
    return (
        CANARY,
        json.dumps(CANARY, ensure_ascii=True)[1:-1],
        json.dumps(CANARY, ensure_ascii=False)[1:-1],
        repr(CANARY)[1:-1],
    )


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
        logger.info("provider key repr: %r", CANARY)
        logger.info("provider key json: %s", json.dumps(CANARY, ensure_ascii=True))
        try:
            raise ValueError(CANARY)  # noqa: TRY301 - simulate a provider traceback
        except ValueError:
            logger.exception("synthetic provider failure")
    finally:
        handler.close()
        logger.removeHandler(handler)
    payload = target.read_text()
    assert all(value not in payload for value in _serialized_canary_forms())
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
    """A read boundary cannot expose raw or serialized credential spellings."""
    from utils.secret_redaction import SecretRedactor  # noqa: PLC0415

    redactor = SecretRedactor((CANARY,))
    json_canary = json.dumps(CANARY, ensure_ascii=True)[1:-1].encode()
    split = len(json_canary) // 2
    prefix = b"prefix: " + CANARY.encode() + b"; escaped: "
    pieces = [
        redactor.feed(prefix + json_canary[:split]),
        redactor.feed(json_canary[split:] + b" suffix\n"),
        redactor.finish(),
    ]
    output = b"".join(pieces).decode()
    assert all(value not in output for value in _serialized_canary_forms())
    assert output == b"prefix: [REDACTED]; escaped: [REDACTED] suffix\n".decode()
