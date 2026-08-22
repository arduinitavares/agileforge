"""Tests for specs engine resolution."""

from __future__ import annotations

import importlib

import pytest


@pytest.mark.parametrize(
    "module_name",
    [
        "services.specs.story_validation_service",
    ],
)
def test_resolve_engine_prefers_live_models_db_get_engine_over_stale_default_binding(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
) -> None:
    """Verify resolve engine prefers live models db get engine over stale default binding."""  # noqa: E501
    from models import db as model_db  # noqa: PLC0415
    from services.specs._engine_resolution import resolve_spec_engine  # noqa: PLC0415

    service_module = importlib.import_module(module_name)
    preferred_engine = object()

    monkeypatch.setattr(model_db, "get_engine", lambda: preferred_engine)

    resolved = resolve_spec_engine(
        service_get_engine=service_module.get_engine,
        default_service_get_engine=service_module.get_engine,
    )

    assert resolved is preferred_engine


@pytest.mark.parametrize(
    "module_name",
    [
        "services.specs.story_validation_service",
    ],
)
def test_resolve_engine_preserves_explicit_service_local_get_engine_override(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
) -> None:
    """Verify resolve engine preserves explicit service local get engine override."""
    from services.specs._engine_resolution import resolve_spec_engine  # noqa: PLC0415

    service_module = importlib.import_module(module_name)
    default_service_get_engine = service_module.get_engine
    local_engine = object()

    def local_get_engine() -> object:
        return local_engine

    monkeypatch.setattr(service_module, "get_engine", local_get_engine, raising=False)

    resolved = resolve_spec_engine(
        service_get_engine=service_module.get_engine,
        default_service_get_engine=default_service_get_engine,
    )

    assert resolved is local_engine
