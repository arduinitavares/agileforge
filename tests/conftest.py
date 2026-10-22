"""Pytest configuration and fixtures."""

import importlib
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Literal, cast

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine


@pytest.fixture
def _real_platform_guard() -> None:
    """Opt a test out of the suite-wide Linux guard bypass."""


@pytest.fixture(autouse=True)
def _bypass_platform_guard_off_linux(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Let the unit suite run on developer macOS while the product refuses it."""
    if sys.platform == "linux" or "_real_platform_guard" in request.fixturenames:
        return
    from utils import platform_support  # noqa: PLC0415

    monkeypatch.setattr(platform_support, "current_platform", lambda: "linux")


@pytest.fixture
def _real_platform_guard() -> None:
    """Opt a test out of the suite-wide Linux guard bypass."""


@pytest.fixture(autouse=True)
def _bypass_platform_guard_off_linux(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Let the unit suite run on developer macOS while the product refuses it."""
    if sys.platform == "linux" or "_real_platform_guard" in request.fixturenames:
        return
    from utils import platform_support  # noqa: PLC0415

    monkeypatch.setattr(platform_support, "current_platform", lambda: "linux")


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
    """Create a symlink at ``link`` pointing to ``target``."""

    def _create_symlink(link: Path, target: Path | str) -> None:
        link.symlink_to(target)

    return _create_symlink


@pytest.fixture(autouse=True)
def _published_capacity_for_offline_tests(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep unit inference seams offline; transport tests override with real resolution.

    Resolver unit tests call the real module directly. Public-network integration
    tests retain real lookup. This patches only the retry client's imported seam.
    """
    if request.node.get_closest_marker("integration") is not None:
        return
    from datetime import UTC, datetime  # noqa: PLC0415

    from adapters.adk import provider_retry  # noqa: PLC0415
    from services.contracts.generation_policy import (  # noqa: PLC0415
        GenerationAllowance,
    )
    from utils.model_config import get_model_token_limit_args  # noqa: PLC0415

    async def published(model: str) -> GenerationAllowance:
        return GenerationAllowance(
            model_id=model,
            completion_tokens=128_000,
            maximum_published_context=1_050_000,
            known_capacity_endpoints=1,
            unknown_capacity_endpoints=0,
            request_parameter=cast(
                'Literal["max_tokens", "max_completion_tokens"]',
                next(iter(get_model_token_limit_args(model, 128_000))),
            ),
            source_url="https://openrouter.ai/api/v1/models/test/offline/endpoints",
            snapshot_sha256="sha256:" + "a" * 64,
            retrieved_at=datetime(2026, 10, 21, tzinfo=UTC),
        )

    monkeypatch.setattr(provider_retry, "resolve_generation_allowance", published)
