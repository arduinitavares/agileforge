"""Regression tests for business DB bootstrap on API startup."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest  # noqa: TC002
from fastapi.testclient import TestClient
from sqlmodel import create_engine

import api as api_module
from agile_sqlmodel import ensure_business_db_ready

if TYPE_CHECKING:
    from pathlib import Path


def test_ensure_business_db_ready_creates_core_tables(tmp_path: Path) -> None:
    """Verify ensure business db ready creates core tables."""
    db_path = tmp_path / "business_bootstrap.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    ensure_business_db_ready(engine_override=engine)

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='products'"
        )
        products = cursor.fetchone()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='spec_registry'"
        )
        spec_registry = cursor.fetchone()

    assert products is not None
    assert spec_registry is not None


def test_ensure_business_db_ready_creates_brownfield_tables(tmp_path: Path) -> None:
    """Verify ensure business db ready creates brownfield artifact tables."""
    db_path = tmp_path / "business_bootstrap.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    ensure_business_db_ready(engine_override=engine)

    expected_tables = {
        "brownfield_source_artifacts",
        "brownfield_scan_attempts",
        "brownfield_spec_draft_attempts",
        "brownfield_spec_approvals",
    }
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        table_names = {row[0] for row in cursor.fetchall()}

    assert expected_tables <= table_names


def test_api_startup_bootstraps_business_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify api startup bootstraps business db."""
    called = {"value": False}

    def _fake_bootstrap() -> None:
        called["value"] = True

    monkeypatch.setattr(api_module, "ensure_business_db_ready", _fake_bootstrap)

    with TestClient(api_module.app):
        pass

    assert called["value"] is True
