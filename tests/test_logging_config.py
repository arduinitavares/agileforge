"""Tests for shared logging configuration helpers."""

from __future__ import annotations

import io
import logging
from contextlib import redirect_stderr
from typing import TYPE_CHECKING

import pytest

from utils.logging_config import configure_logging
from utils.runtime_config import clear_runtime_config_cache

if TYPE_CHECKING:
    from collections.abc import Iterator


def _remove_console_handlers() -> None:
    root_logger = logging.getLogger()
    kept_handlers = []
    for handler in root_logger.handlers:
        handler_id = getattr(handler, "_agileforge_handler_id", "")
        if str(handler_id).startswith("console:"):
            handler.close()
            continue
        kept_handlers.append(handler)
    root_logger.handlers = kept_handlers


@pytest.fixture(autouse=True)
def _reset_console_logging() -> Iterator[None]:
    _remove_console_handlers()
    clear_runtime_config_cache()
    yield
    _remove_console_handlers()
    clear_runtime_config_cache()


def test_console_logging_hides_file_only_messages_and_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify console logging hides file only messages and sql."""
    monkeypatch.delenv("AGILEFORGE_DB_ECHO", raising=False)

    stream = io.StringIO()
    with redirect_stderr(stream):
        configure_logging(
            console=True,
            console_logger_names=("scripts.apply_story_validation",),
        )
        logging.getLogger("scripts.apply_story_validation").info(
            "summary line",
            extra={"console_visible": True},
        )
        logging.getLogger("scripts.apply_story_validation").info(
            "file only detail",
            extra={"console_visible": False},
        )
        logging.getLogger("sqlalchemy.engine.Engine").info("SELECT 1")

    output = stream.getvalue()
    assert "summary line" in output
    assert "file only detail" not in output
    assert "SELECT 1" not in output


def test_console_logging_allows_sql_when_echo_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify console logging allows sql when echo is enabled."""
    monkeypatch.setenv("AGILEFORGE_DB_ECHO", "true")

    stream = io.StringIO()
    with redirect_stderr(stream):
        configure_logging(
            console=True,
            console_logger_names=("scripts.apply_story_validation",),
        )
        logging.getLogger("sqlalchemy.engine.Engine").info("SELECT 42")

    assert "SELECT 42" in stream.getvalue()


def test_configure_logging_suppresses_litellm_info_noise() -> None:
    """Third-party LLM info logs should not pollute CLI stderr."""
    logging.getLogger("LiteLLM").setLevel(logging.INFO)
    logging.getLogger("litellm").setLevel(logging.INFO)

    configure_logging()

    assert logging.getLogger("LiteLLM").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("litellm").getEffectiveLevel() >= logging.WARNING
