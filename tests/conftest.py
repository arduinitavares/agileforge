"""Pytest configuration and fixtures."""

import importlib
import os
import socket
import sys
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path
from sqlite3 import Connection
from threading import RLock
from typing import cast

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

_ORIGINAL_SOCKET: type[socket.socket] = socket.socket
_WIN_ERROR_PRIVILEGE_NOT_HELD: int = 1314


def _windows_testclient_socketpair() -> tuple[socket.socket, socket.socket]:
    """Create only the loopback pair required by Windows ProactorEventLoop."""
    with _ORIGINAL_SOCKET(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        address = listener.getsockname()
        with ExitStack() as sockets:
            client = _ORIGINAL_SOCKET(socket.AF_INET, socket.SOCK_STREAM)
            sockets.callback(client.close)
            client.setblocking(False)
            with suppress(BlockingIOError, InterruptedError):
                client.connect(address)
            client.setblocking(True)
            accept_fn = getattr(listener, "_accept")  # noqa: B009
            accepted_fd, _ = accept_fn()
            server = _ORIGINAL_SOCKET(
                listener.family,
                listener.type,
                listener.proto,
                fileno=accepted_fd,
            )
            sockets.callback(server.close)
            if (
                server.getsockname() != client.getpeername()
                or client.getsockname() != server.getpeername()
            ):
                msg = "socketpair endpoints did not match"
                raise RuntimeError(msg)
            sockets.pop_all()
            return server, client


@pytest.fixture(autouse=True)
def _permit_windows_testclient_socketpair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Permit only Proactor's internal pair while pytest-socket blocks sockets."""
    if sys.platform == "win32":
        monkeypatch.setattr(socket, "socketpair", _windows_testclient_socketpair)


_TEST_MODEL_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "models.test.yaml"
)
os.environ.setdefault("MODEL_CONFIG_PATH", str(_TEST_MODEL_CONFIG_PATH))
os.environ.setdefault("RELAX_ZDR_FOR_TESTS", "true")
os.environ.setdefault("AGILEFORGE_DB_URL", "sqlite:///:memory:")
from models.core import Team, TeamMember  # noqa: E402

model_config = importlib.import_module("utils.model_config")
runtime_config = importlib.import_module("utils.runtime_config")
core_models = importlib.import_module("models.core")
agile_sqlmodel = importlib.import_module("agile_sqlmodel")
model_db = importlib.import_module("models.db")

clear_runtime_config_cache = runtime_config.clear_runtime_config_cache

_TEST_TEAM_MODELS = (Team, TeamMember)

model_config.clear_config_cache()
clear_runtime_config_cache()


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Require external socket access to be excluded from the default suite."""
    invalid_nodeids = sorted(
        item.nodeid
        for item in items
        if item.get_closest_marker("enable_socket") is not None
        and item.get_closest_marker("integration") is None
    )
    if invalid_nodeids:
        nodeids = ", ".join(invalid_nodeids)
        message = f"enable_socket requires integration: {nodeids}"
        raise pytest.UsageError(message)


@pytest.fixture(scope="session")
def test_db_url() -> str:
    """Return test database URL."""
    return "sqlite:///:memory:"


@pytest.fixture
def engine(test_db_url: str) -> Iterator[Engine]:  # pylint: disable=redefined-outer-name
    """Create a fresh in-memory database for each test."""
    with fresh_test_engine(test_db_url) as test_engine:
        yield test_engine


@contextmanager
def fresh_test_engine(database_url: str) -> Iterator[Engine]:
    """Own one in-memory database, including cleanup after failed setup."""
    _engine = create_engine(
        database_url,
        echo=False,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(_engine, "connect")
    def set_sqlite_pragma(
        dbapi_connection: Connection,
        _connection_record: object,
    ) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    try:
        SQLModel.metadata.create_all(_engine)
        yield _engine
    finally:
        # Closing the owned in-memory pool discards its schema and rows without
        # issuing DROP statements or disabling foreign key enforcement.
        _engine.dispose()


@pytest.fixture(autouse=True)
def patch_get_engine_globally(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Automatically patch get_engine() in all modules to return the test engine.

    This ensures tests never accidentally hit the production database.
    Install the guard for every test, constructing its function-scoped database
    only when it explicitly requests engine/session or calls get_engine().
    """
    engine_lock = RLock()

    def get_test_engine() -> Engine:
        # Pytest does not synchronize two first-time dynamic fixture requests.
        with engine_lock:
            return cast("Engine", request.getfixturevalue("engine"))

    # Patch the agile_sqlmodel module's get_engine function
    monkeypatch.setattr(agile_sqlmodel, "get_engine", get_test_engine)
    monkeypatch.setattr(model_db, "get_engine", get_test_engine)

    # Also patch in all modules that import get_engine
    # These need explicit patching because they import at module load time
    modules_to_patch = [
        "api",
        "repositories.project",
        "repositories.story",
        "tools.db_tools",
        "services.read_projections",
    ]

    for module_path in modules_to_patch:
        try:
            module = importlib.import_module(module_path)
            if hasattr(module, "get_engine"):
                monkeypatch.setattr(module, "get_engine", get_test_engine)
        except ImportError:
            pass  # Module not imported in this test, skip


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:  # pylint: disable=redefined-outer-name
    """Create a new database session for each test."""
    # Use _session to avoid redefining the 'session' fixture name
    with Session(engine) as _session:
        yield _session


@pytest.fixture
def create_symlink() -> Callable[[Path, Path | str], None]:
    """Create a symlink, skipping or failing on Windows if unprivileged."""

    def _create_symlink(link: Path, target: Path | str) -> None:
        try:
            link.symlink_to(target)
        except OSError as error:
            if (
                os.name == "nt"
                and getattr(error, "winerror", None) == _WIN_ERROR_PRIVILEGE_NOT_HELD
            ):
                if os.environ.get("AGILEFORGE_REQUIRE_WINDOWS_SYMLINK_TESTS") == "1":
                    pytest.fail(
                        "Windows SeCreateSymbolicLinkPrivilege not held and "
                        "AGILEFORGE_REQUIRE_WINDOWS_SYMLINK_TESTS is set.",  # ty: ignore[invalid-argument-type]
                    )
                pytest.skip(
                    "Windows SeCreateSymbolicLinkPrivilege not held.",  # ty: ignore[too-many-positional-arguments]
                )
            raise

    return _create_symlink
