"""Script to delete a project and all its related data from the database.

Usage: python -m scripts.delete_project <product_id> [db_path].
"""

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

from utils.cli_output import emit
from utils.runtime_config import resolve_database_target


def _fetch_child_ids(
    cur: sqlite3.Cursor,
    query: str,
    parent_ids: list[int],
) -> list[int]:
    ids: list[int] = []
    for parent_id in parent_ids:
        cur.execute(query, (parent_id,))
        ids.extend(row[0] for row in cur.fetchall())
    return ids


def _delete_by_ids(cur: sqlite3.Cursor, query: str, ids: list[int]) -> None:
    cur.executemany(query, ((record_id,) for record_id in ids))


def _table_exists(cur: sqlite3.Cursor, table_name: str) -> bool:
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    return cur.fetchone() is not None


def _delete_project_rows_if_table_exists(
    cur: sqlite3.Cursor,
    table_name: str,
    delete_query: str,
    product_id: int,
) -> None:
    if not _table_exists(cur, table_name):
        return
    emit(f"  - Deleting {table_name}...")
    cur.execute(delete_query, (product_id,))


def resolve_db_path(explicit_path: str | None = None) -> str:
    """Resolve a database path from CLI input or required runtime config."""
    return resolve_database_target(
        explicit_path,
        env_name="AGILEFORGE_DB_URL",
    ).sqlite_connect_target


def delete_project(product_id: int, db_path: str) -> None:  # noqa: C901, PLR0912, PLR0915
    """Return delete project."""
    emit(f"Connecting to database at: {db_path}")
    if db_path != ":memory:" and not Path(db_path).exists():
        msg = f"Database file not found: {db_path}"
        raise FileNotFoundError(msg)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        cur = conn.cursor()
        # Verify project exists
        cur.execute("SELECT name FROM products WHERE product_id=?", (product_id,))
        res = cur.fetchone()
        if not res:
            emit(f"Product ID {product_id} not found.")
            return

        emit(f"Found product: {res[0]} (ID: {product_id}). preparing to delete...")

        # 1. Collect hierarchical IDs to delete
        # Themes
        cur.execute("SELECT theme_id FROM themes WHERE product_id=?", (product_id,))
        theme_ids = [r[0] for r in cur.fetchall()]

        # Epics
        epic_ids = []
        if theme_ids:
            epic_ids = _fetch_child_ids(
                cur,
                "SELECT epic_id FROM epics WHERE theme_id=?",
                theme_ids,
            )

        # Features
        feature_ids = []
        if epic_ids:
            feature_ids = _fetch_child_ids(
                cur,
                "SELECT feature_id FROM features WHERE epic_id=?",
                epic_ids,
            )

        # Sprints
        cur.execute("SELECT sprint_id FROM sprints WHERE product_id=?", (product_id,))
        sprint_ids = [r[0] for r in cur.fetchall()]

        # Spec versions
        cur.execute(
            "SELECT spec_version_id FROM spec_registry WHERE product_id=?",
            (product_id,),
        )
        spec_version_ids = [r[0] for r in cur.fetchall()]

        emit(
            "  - Associated records found: "
            f"{len(theme_ids)} themes, "
            f"{len(epic_ids)} epics, "
            f"{len(feature_ids)} features, "
            f"{len(sprint_ids)} sprints, "
            f"{len(spec_version_ids)} spec versions"
        )

        # 2. Delete Dependent Records (Order matters if FKs are restricted, though we set ON)  # noqa: E501
        # However, manual deletion ensures we don't hit constraints if cascade isn't perfect.  # noqa: E501

        emit(
            "  - Deleting task_execution_logs linked to this product's sprints..."
        )
        cur.execute(
            "DELETE FROM task_execution_logs "
            "WHERE sprint_id IN (SELECT sprint_id FROM sprints WHERE product_id=?)",
            (product_id,),
        )

        emit(
            "  - Deleting story_completion_logs linked to user stories of this product..."  # noqa: E501
        )
        cur.execute(
            "DELETE FROM story_completion_logs "
            "WHERE story_id IN (SELECT story_id FROM user_stories WHERE product_id=?)",
            (product_id,),
        )

        emit("  - Deleting tasks linked to user stories of this product...")
        cur.execute(
            "DELETE FROM tasks WHERE story_id IN (SELECT story_id FROM user_stories WHERE product_id=?)",  # noqa: E501
            (product_id,),
        )

        emit("  - Deleting sprint_stories linked to user stories of this product...")
        cur.execute(
            "DELETE FROM sprint_stories WHERE story_id IN (SELECT story_id FROM user_stories WHERE product_id=?)",  # noqa: E501
            (product_id,),
        )
        if sprint_ids:
            _delete_by_ids(
                cur,
                "DELETE FROM sprint_stories WHERE sprint_id=?",
                sprint_ids,
            )

        emit("  - Deleting user_stories...")
        cur.execute("DELETE FROM user_stories WHERE product_id=?", (product_id,))

        emit("  - Deleting workflow_events linked to this product's sprints...")
        if sprint_ids:
            _delete_by_ids(
                cur,
                "DELETE FROM workflow_events WHERE sprint_id=?",
                sprint_ids,
            )

        if feature_ids:
            emit(f"  - Deleting {len(feature_ids)} features...")
            _delete_by_ids(
                cur,
                "DELETE FROM features WHERE feature_id=?",
                feature_ids,
            )

        if epic_ids:
            emit(f"  - Deleting {len(epic_ids)} epics...")
            _delete_by_ids(cur, "DELETE FROM epics WHERE epic_id=?", epic_ids)

        if theme_ids:
            emit(f"  - Deleting {len(theme_ids)} themes...")
            _delete_by_ids(cur, "DELETE FROM themes WHERE theme_id=?", theme_ids)

        if sprint_ids:
            emit(f"  - Deleting {len(sprint_ids)} sprints...")
            _delete_by_ids(cur, "DELETE FROM sprints WHERE sprint_id=?", sprint_ids)

        emit("  - Deleting workflow_events...")
        cur.execute("DELETE FROM workflow_events WHERE product_id=?", (product_id,))

        emit("  - Deleting product_teams...")
        cur.execute("DELETE FROM product_teams WHERE product_id=?", (product_id,))

        emit("  - Deleting product_personas...")
        cur.execute("DELETE FROM product_personas WHERE product_id=?", (product_id,))

        emit("  - Deleting spec_authority_acceptance...")
        cur.execute(
            "DELETE FROM spec_authority_acceptance WHERE product_id=?", (product_id,)
        )

        if spec_version_ids:
            emit("  - Deleting compiled_spec_authority...")
            _delete_by_ids(
                cur,
                "DELETE FROM compiled_spec_authority WHERE spec_version_id=?",
                spec_version_ids,
            )

        _delete_project_rows_if_table_exists(
            cur,
            "brownfield_spec_approvals",
            "DELETE FROM brownfield_spec_approvals WHERE project_id=?",
            product_id,
        )

        _delete_project_rows_if_table_exists(
            cur,
            "brownfield_spec_draft_attempts",
            "DELETE FROM brownfield_spec_draft_attempts WHERE project_id=?",
            product_id,
        )

        _delete_project_rows_if_table_exists(
            cur,
            "brownfield_scan_attempts",
            "DELETE FROM brownfield_scan_attempts WHERE project_id=?",
            product_id,
        )

        _delete_project_rows_if_table_exists(
            cur,
            "brownfield_source_artifacts",
            "DELETE FROM brownfield_source_artifacts WHERE project_id=?",
            product_id,
        )

        emit("  - Deleting spec_registry...")
        cur.execute("DELETE FROM spec_registry WHERE product_id=?", (product_id,))

        emit("  - Deleting product root record...")
        cur.execute("DELETE FROM products WHERE product_id=?", (product_id,))

        conn.commit()
        emit("Deletion complete.")

        # Verify
        cur.execute("SELECT count(*) FROM products WHERE product_id=?", (product_id,))
        count = cur.fetchone()[0]
        if count == 0:
            emit(f"SUCCESS: Product {product_id} successfully deleted.")
        else:
            emit(f"WARNING: Product {product_id} still exists.")


def main() -> None:
    """Return main."""
    parser = argparse.ArgumentParser(
        description="Delete a project and all related records from the configured business database.",  # noqa: E501
    )
    parser.add_argument("product_id", type=int, help="Product ID to delete.")
    parser.add_argument(
        "db",
        nargs="?",
        help="Optional SQLite database path or sqlite:/// URL. Defaults to AGILEFORGE_DB_URL.",  # noqa: E501
    )
    args = parser.parse_args()
    delete_project(args.product_id, resolve_db_path(args.db))


if __name__ == "__main__":
    main()
