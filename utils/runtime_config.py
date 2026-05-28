"""Centralized runtime configuration and identities."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ENV_PATH = _REPO_ROOT / ".env"
_LEGACY_DB_FILENAMES = frozenset({"agile_simple.db", "agile_sqlmodel.db"})
_TRUE_VALUES = {"1", "true", "yes", "on"}
_DEFAULT_API_HOST = "127.0.0.1"


class RuntimeConfigError(RuntimeError):
    """Raised when required runtime configuration is missing or invalid."""

    @classmethod
    def missing_required_env(cls, name: str) -> RuntimeConfigError:
        """Build an error for a missing required environment variable."""
        return cls(
            f"Missing required environment variable: {name}. "
            f"Add it to {_ENV_PATH.name} or export it before running the app."
        )

    @classmethod
    def legacy_db_filename(cls, source: str, path: Path) -> RuntimeConfigError:
        """Build an error for rejected legacy database filenames."""
        return cls(
            f"{source} resolves to legacy database filename {path.name!r}. "
            "Legacy root database files are no longer supported."
        )

    @classmethod
    def empty_database_target(cls, source: str) -> RuntimeConfigError:
        """Build an error for empty database target values."""
        return cls(f"{source} must not be empty.")

    @classmethod
    def invalid_sqlite_file_url(cls, source: str, value: str) -> RuntimeConfigError:
        """Build an error for malformed SQLite URLs."""
        return cls(
            f"{source} must be a SQLite file URL of the form "
            f"sqlite:///path/to/db.sqlite3 or a filesystem path, got {value!r}."
        )

    @classmethod
    def unsupported_database_url(cls, source: str, value: str) -> RuntimeConfigError:
        """Build an error for unsupported non-SQLite URLs."""
        return cls(
            f"{source} must point to a SQLite database, got unsupported URL {value!r}."
        )

    @classmethod
    def shared_session_database(cls) -> RuntimeConfigError:
        """Build an error when business and session DBs point to the same file."""
        return cls(
            "AGILEFORGE_SESSION_DB_URL must point to a different SQLite file than "
            "AGILEFORGE_DB_URL."
        )


@dataclass(frozen=True)
class DatabaseTarget:
    """Resolved database target for both SQLAlchemy and sqlite3 callers."""

    source: str
    sqlite_url: str
    sqlite_path: Path | None

    @property
    def sqlite_connect_target(self) -> str:
        """Return the sqlite3-compatible connection target."""
        if self.sqlite_path is None:
            return ":memory:"
        return str(self.sqlite_path)


@dataclass(frozen=True)
class RunnerIdentity:
    """Stable app/user namespace for an ADK runner."""

    app_name: str
    user_id: str


WORKFLOW_RUNNER_IDENTITY = RunnerIdentity(
    app_name="agile_orchestrator",
    user_id="local_developer",
)
VISION_RUNNER_IDENTITY = RunnerIdentity(
    app_name="product_vision_tool",
    user_id="dashboard_vision",
)
BACKLOG_RUNNER_IDENTITY = RunnerIdentity(
    app_name="backlog_primer",
    user_id="dashboard_backlog",
)
ROADMAP_RUNNER_IDENTITY = RunnerIdentity(
    app_name="roadmap_builder",
    user_id="dashboard_roadmap",
)
STORY_RUNNER_IDENTITY = RunnerIdentity(
    app_name="user_story_writer",
    user_id="dashboard_story",
)
SPRINT_RUNNER_IDENTITY = RunnerIdentity(
    app_name="sprint_planner",
    user_id="dashboard_sprint",
)
AS_BUILT_RUNNER_IDENTITY = RunnerIdentity(
    app_name="as_built_assessor",
    user_id="dashboard_as_built",
)
SPEC_AUTHORITY_COMPILER_IDENTITY = RunnerIdentity(
    app_name="spec_authority_compiler",
    user_id="spec_compiler",
)
SPEC_VALIDATOR_IDENTITY = RunnerIdentity(
    app_name="spec_validator_agent",
    user_id="spec_validator",
)


def load_runtime_env() -> None:
    """Load the repository .env file once for all runtime consumers."""
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH, override=False)


load_runtime_env()


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeConfigError.missing_required_env(name)
    return value


def get_optional_env(name: str, default: str | None = None) -> str | None:
    """Read an optional environment variable after loading .env once."""
    value = os.environ.get(name)
    if value is None:
        return default
    stripped = value.strip()
    if not stripped and default is not None:
        return default
    return stripped


def get_bool_env(name: str, default: bool = False) -> bool:
    """Read a boolean environment variable."""
    value = get_optional_env(name)
    if value is None:
        return default
    return value.lower() in _TRUE_VALUES


def get_int_env(name: str, default: int) -> int:
    """Read an integer environment variable with a validated default."""
    value = get_optional_env(name)
    if value is None:
        return default
    return int(value)


def _reject_legacy_db_name(path: Path, source: str) -> None:
    if path.name in _LEGACY_DB_FILENAMES:
        raise RuntimeConfigError.legacy_db_filename(source, path)


def _normalize_sqlite_target(raw_value: str, *, source: str) -> DatabaseTarget:
    value = raw_value.strip()
    if not value:
        raise RuntimeConfigError.empty_database_target(source)

    if value in {":memory:", "sqlite:///:memory:"}:
        return DatabaseTarget(
            source=source, sqlite_url="sqlite:///:memory:", sqlite_path=None
        )

    if value.startswith("sqlite:///"):
        raw_path = value.replace("sqlite:///", "", 1)
    elif value.startswith("sqlite://"):
        raise RuntimeConfigError.invalid_sqlite_file_url(source, value)
    elif "://" in value:
        raise RuntimeConfigError.unsupported_database_url(source, value)
    else:
        raw_path = value

    path = Path(raw_path)
    path = (_REPO_ROOT / path).resolve() if not path.is_absolute() else path.resolve()

    _reject_legacy_db_name(path, source)
    return DatabaseTarget(
        source=source,
        sqlite_url=f"sqlite:///{path.as_posix()}",
        sqlite_path=path,
    )


def resolve_database_target(
    explicit_value: str | None,
    *,
    env_name: str,
) -> DatabaseTarget:
    """Resolve an explicit DB argument or a required environment variable."""
    if explicit_value is not None and explicit_value.strip():
        return _normalize_sqlite_target(
            explicit_value, source="explicit database argument"
        )
    return _normalize_sqlite_target(_require_env(env_name), source=env_name)


@lru_cache(maxsize=1)
def get_business_db_target() -> DatabaseTarget:
    """Return the configured business database target."""
    return resolve_database_target(None, env_name="AGILEFORGE_DB_URL")


@lru_cache(maxsize=1)
def get_session_db_target() -> DatabaseTarget:
    """Return the configured session database target."""
    target = resolve_database_target(None, env_name="AGILEFORGE_SESSION_DB_URL")
    business_target = get_business_db_target()
    if (
        target.sqlite_path is not None
        and target.sqlite_path == business_target.sqlite_path
    ):
        raise RuntimeConfigError.shared_session_database()
    return target


def get_openrouter_api_key() -> str | None:
    """Return the OpenRouter API key, if configured."""
    return get_optional_env("OPEN_ROUTER_API_KEY")


@lru_cache(maxsize=1)
def get_database_echo() -> bool:
    """Return whether SQLAlchemy echo logging is enabled."""
    return get_bool_env("AGILEFORGE_DB_ECHO", default=False)


def get_spec_validator_max_tokens(default: int = 4096) -> int:
    """Return the max token budget for the spec validator."""
    return get_int_env("SPEC_VALIDATOR_MAX_TOKENS", default)


def get_vision_interviewer_max_tokens(default: int = 4096) -> int:
    """Return the max token budget for the vision interviewer."""
    return get_int_env("VISION_INTERVIEWER_MAX_TOKENS", default)


def get_backlog_primer_max_tokens(default: int = 8192) -> int:
    """Return the max token budget for the backlog primer."""
    return get_int_env("BACKLOG_PRIMER_MAX_TOKENS", default)


def get_roadmap_builder_max_tokens(default: int = 8192) -> int:
    """Return the max token budget for the roadmap builder."""
    return get_int_env("ROADMAP_BUILDER_MAX_TOKENS", default)


def get_story_writer_max_tokens(default: int = 16384) -> int:
    """Return the max token budget for the user story writer."""
    return get_int_env("STORY_WRITER_MAX_TOKENS", default)


def get_sprint_planner_max_tokens(default: int = 8192) -> int:
    """Return the max token budget for the sprint planner."""
    return get_int_env("SPRINT_PLANNER_MAX_TOKENS", default)


def get_as_built_assessor_max_tokens(default: int = 8192) -> int:
    """Return the max token budget for the as-built assessor."""
    return get_int_env("AS_BUILT_ASSESSOR_MAX_TOKENS", default)


def get_as_built_assessor_timeout_seconds(default: float = 120.0) -> float:
    """Return the bounded runtime for the as-built assessor model call."""
    value = get_optional_env("AS_BUILT_ASSESSOR_TIMEOUT_SECONDS")
    if value is None:
        return default
    return float(value)


def get_as_built_assessor_batch_size(default: int = 10) -> int:
    """Return the maximum authority targets per as-built assessor batch."""
    value = get_int_env("AS_BUILT_ASSESSOR_BATCH_SIZE", default)
    if value < 1:
        msg = "AS_BUILT_ASSESSOR_BATCH_SIZE must be at least 1."
        raise RuntimeConfigError(msg)
    if value > 50:
        msg = "AS_BUILT_ASSESSOR_BATCH_SIZE must be at most 50."
        raise RuntimeConfigError(msg)
    return value


def is_spec_compiler_schema_disabled() -> bool:
    """Return whether the spec compiler should skip output schema enforcement."""
    return get_bool_env("SPEC_COMPILER_DISABLE_SCHEMA", default=True)


def get_default_validation_mode(default: str = "deterministic") -> str:
    """Return the default spec validation mode."""
    return get_optional_env("SPEC_VALIDATION_DEFAULT_MODE", default) or default


def get_api_host(default: str = _DEFAULT_API_HOST) -> str:
    """Return the API host for local runs.

    Defaults to loopback for safer local development. Set
    AGILEFORGE_API_HOST explicitly when you want broader network exposure.
    """
    return get_optional_env("AGILEFORGE_API_HOST", default) or default


def get_api_port(default: int = 8000) -> int:
    """Return the API port for local runs."""
    return get_int_env("AGILEFORGE_API_PORT", default)


def get_api_reload(default: bool = True) -> bool:
    """Return whether api.py should launch uvicorn in reload mode."""
    return get_bool_env("AGILEFORGE_API_RELOAD", default)


def clear_runtime_config_cache() -> None:
    """Clear cached runtime settings for tests."""
    get_business_db_target.cache_clear()
    get_session_db_target.cache_clear()
    get_database_echo.cache_clear()
