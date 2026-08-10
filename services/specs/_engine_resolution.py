"""Shared engine-resolution helper for spec services."""

from __future__ import annotations

from typing import TYPE_CHECKING

from models import db as model_db

if TYPE_CHECKING:
    from collections.abc import Callable


def resolve_spec_engine(
    *,
    service_get_engine: Callable[[], object],
    default_service_get_engine: Callable[[], object],
) -> object:
    """Resolve a service-local override or the live database binding.

    Resolution order:
    1. An explicit service-local ``get_engine`` override.
    2. The live ``models.db.get_engine`` binding.
    """
    if service_get_engine is not default_service_get_engine:
        return service_get_engine()
    return model_db.get_engine()
