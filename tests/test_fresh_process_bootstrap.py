"""Fresh-process regressions for the production SQLModel bootstrap."""

from __future__ import annotations

import asyncio
import importlib
import json
import multiprocessing
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from sqlalchemy import inspect
from sqlmodel import SQLModel

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager

    from fastapi import FastAPI
    from sqlalchemy.engine import Engine


class _ApiModule(Protocol):
    app: FastAPI
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]]


class _DatabaseModule(Protocol):
    engine: Engine


def _bootstrap_worker(
    repository_root_value: str,
    database_path_value: str,
    result_path_value: str,
) -> None:
    """Run production bootstrap in a spawned interpreter with fresh metadata."""
    assert "models.core" not in sys.modules
    sys.path.insert(0, repository_root_value)
    os.environ.update(
        {
            "AGILEFORGE_DB_URL": f"sqlite:///{database_path_value}",
            "MODEL_CONFIG_PATH": str(
                Path(repository_root_value) / "config/models.test.yaml"
            ),
            "RELAX_ZDR_FOR_TESTS": "true",
        }
    )

    api = cast("_ApiModule", importlib.import_module("api"))

    async def verify() -> dict[str, object]:
        async with api.lifespan(api.app):
            db = cast("_DatabaseModule", importlib.import_module("models.db"))

            resolved_foreign_keys = sorted(
                (
                    foreign_key.parent.table.name,
                    foreign_key.parent.name,
                    foreign_key.column.table.name,
                    foreign_key.column.name,
                )
                for table in SQLModel.metadata.sorted_tables
                for foreign_key in table.foreign_keys
            )
            inspector = inspect(db.engine)
            actual_foreign_keys = {
                table: sorted(
                    (
                        tuple(item.get("constrained_columns") or ()),
                        item.get("referred_table"),
                        tuple(item.get("referred_columns") or ()),
                    )
                    for item in inspector.get_foreign_keys(table)
                )
                for table in (
                    "project_personas",
                    "spec_registry",
                    "sprints",
                    "workflow_events",
                )
            }
            return {
                "tables": sorted(inspector.get_table_names()),
                "resolved_foreign_keys": resolved_foreign_keys,
                "actual_foreign_keys": actual_foreign_keys,
            }

    payload = asyncio.run(verify())
    Path(result_path_value).write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )


def test_production_api_bootstrap_creates_complete_fresh_schema(
    tmp_path: Path,
) -> None:
    """Create and inspect the current schema without pytest model pre-imports."""
    repository_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "fresh-bootstrap.db"
    result_path = tmp_path / "bootstrap-result.json"
    process = multiprocessing.get_context("spawn").Process(
        target=_bootstrap_worker,
        args=(str(repository_root), str(database_path), str(result_path)),
    )
    process.start()
    process.join(timeout=60)
    if process.is_alive():
        process.terminate()
        process.join()
    assert process.exitcode == 0
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    tables = set(payload["tables"])
    assert {"projects", "spec_registry", "workflow_events"} <= tables
    assert {"products", "sessions"}.isdisjoint(tables)
    assert payload["resolved_foreign_keys"]
    for table_name in (
        "project_personas",
        "spec_registry",
        "sprints",
        "workflow_events",
    ):
        assert (["project_id"], "projects", ["project_id"]) in [
            (columns, referred_table, referred_columns)
            for columns, referred_table, referred_columns in payload[
                "actual_foreign_keys"
            ][table_name]
        ]
