"""Pytest configuration and fixtures."""

import importlib
import os
import socket
import sys
from collections.abc import Iterator
from contextlib import ExitStack, suppress
from pathlib import Path
from sqlite3 import Connection

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

_ORIGINAL_SOCKET: type[socket.socket] = socket.socket


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

    # Enable foreign key support for SQLite
    @event.listens_for(Engine, "connect")
    def set_sqlite_pragma(
        dbapi_connection: Connection,
        _connection_record: object,
    ) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    # Use _engine to avoid redefining the 'engine' fixture name
    # Use StaticPool to ensure in-memory DB persists across connections in the same test
    _engine = create_engine(
        test_db_url,
        echo=False,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # Import here to avoid circular imports.
    # We disable unused-import as these models are needed to populate
    # SQLModel.metadata before create_all() is called.

    # Create all tables
    SQLModel.metadata.create_all(_engine)

    yield _engine

    # Cleanup
    # SQLite cannot topologically drop the intentionally cyclic immutable
    # Specification candidate/registry lineage while FK checks are enabled.
    # Disable enforcement only for schema teardown; every test runs with it on.
    with _engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    SQLModel.metadata.drop_all(_engine)


@pytest.fixture(autouse=True)
def patch_get_engine_globally(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Automatically patch get_engine() in all modules to return the test engine.

    This ensures tests never accidentally hit the production database.
    The autouse=True means this runs for every test automatically.
    """
    # Patch the agile_sqlmodel module's get_engine function
    monkeypatch.setattr(agile_sqlmodel, "get_engine", lambda: engine)
    monkeypatch.setattr(model_db, "get_engine", lambda: engine)

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
                monkeypatch.setattr(module, "get_engine", lambda: engine)
        except ImportError:
            pass  # Module not imported in this test, skip


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:  # pylint: disable=redefined-outer-name
    """Create a new database session for each test."""
    # Use _session to avoid redefining the 'session' fixture name
    with Session(engine) as _session:
        yield _session
