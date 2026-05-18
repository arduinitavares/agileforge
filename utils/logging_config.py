"""Logging helpers for file and optional console output."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

from utils.failure_artifacts import LOGS_DIR
from utils.runtime_config import get_database_echo

if TYPE_CHECKING:
    from collections.abc import Iterable

APP_LOG_PATH = LOGS_DIR / "app.log"
ERROR_LOG_PATH = LOGS_DIR / "error.log"
_MAX_LOG_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3


class _ConsoleVisibilityFilter(logging.Filter):
    """Restrict CLI console output to selected loggers and debug-only SQL traces."""

    def __init__(
        self,
        *,
        console_logger_names: tuple[str, ...],
        allow_sql_echo: bool,
    ) -> None:
        super().__init__()
        self.console_logger_names = console_logger_names
        self.allow_sql_echo = allow_sql_echo

    def _matches_console_logger(self, logger_name: str) -> bool:
        if not self.console_logger_names:
            return True
        return any(
            logger_name == candidate or logger_name.startswith(f"{candidate}.")
            for candidate in self.console_logger_names
        )

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        if record.name.startswith("sqlalchemy"):
            return self.allow_sql_echo
        if not self._matches_console_logger(record.name):
            return False
        return bool(getattr(record, "console_visible", True))


def _ensure_handler(
    logger: logging.Logger,
    handler: logging.Handler,
    *,
    handler_id: str | None = None,
) -> None:
    for existing in list(logger.handlers):
        if handler_id and existing.get_name() == handler_id:
            logger.removeHandler(existing)
            existing.close()
            break
        if type(existing) is type(handler):
            existing_path = getattr(existing, "baseFilename", None)
            new_path = getattr(handler, "baseFilename", None)
            if existing_path and new_path and Path(existing_path) == Path(new_path):
                return
    if handler_id:
        handler.set_name(handler_id)
        handler.__dict__["_agileforge_handler_id"] = handler_id
    logger.addHandler(handler)


def _build_handler(path: Path, *, level: int) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        path,
        maxBytes=_MAX_LOG_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    return handler


def _build_console_handler(
    *,
    level: int,
    console_logger_names: Iterable[str],
    allow_sql_echo: bool,
) -> logging.StreamHandler:
    handler = logging.StreamHandler()
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(
        _ConsoleVisibilityFilter(
            console_logger_names=tuple(console_logger_names),
            allow_sql_echo=allow_sql_echo,
        )
    )
    return handler


def configure_logging(
    *,
    console: bool = False,
    console_level: int = logging.INFO,
    console_logger_names: tuple[str, ...] = (),
) -> None:
    """Configure rotating file logs and optional filtered console logging."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    app_handler = _build_handler(APP_LOG_PATH, level=logging.INFO)
    error_handler = _build_handler(ERROR_LOG_PATH, level=logging.WARNING)
    allow_sql_echo = get_database_echo()

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    _ensure_handler(root_logger, app_handler)
    _ensure_handler(root_logger, error_handler)

    if console:
        console_handler = _build_console_handler(
            level=console_level,
            console_logger_names=console_logger_names,
            allow_sql_echo=allow_sql_echo,
        )
        handler_scope = ",".join(console_logger_names) or "root"
        _ensure_handler(
            root_logger,
            console_handler,
            handler_id=f"console:{handler_scope}:{console_level}:{int(allow_sql_echo)}",
        )

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(logger_name)
        uvicorn_logger.setLevel(logging.INFO)
        _ensure_handler(uvicorn_logger, app_handler)
        _ensure_handler(uvicorn_logger, error_handler)

    sqlalchemy_level = logging.INFO if allow_sql_echo else logging.WARNING
    for logger_name in (
        "sqlalchemy",
        "sqlalchemy.engine",
        "sqlalchemy.engine.Engine",
        "sqlalchemy.pool",
    ):
        logging.getLogger(logger_name).setLevel(sqlalchemy_level)

    for logger_name in ("LiteLLM", "litellm"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
