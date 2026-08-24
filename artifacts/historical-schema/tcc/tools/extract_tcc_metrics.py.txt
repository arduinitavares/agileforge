#!/usr/bin/env python3
# ruff: noqa: E501, S608
"""
TCC Metrics Extraction Script.

Extracts evaluation metrics from the project's SQLite database(s) for
Chapter 6 (Resultados) of the thesis. Produces reproducible, auditable outputs.

Usage:
    python -m scripts.extract_tcc_metrics <db_path>
    python -m scripts.extract_tcc_metrics db/spec_authority_dev.db
    python -m scripts.extract_tcc_metrics  # uses AGILEFORGE_DB_URL

Outputs:
    - artifacts/metrics_summary.csv
    - artifacts/metrics_summary.json
    - artifacts/query_results/*.csv (one per query)
    - stdout: Markdown-ready summary

Author: TCC Evaluation Script
Date: 2026-02-01
"""

import argparse
import csv
import json
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from utils.cli_output import emit
from utils.runtime_config import resolve_database_target

_SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REFINED_STORY_COUNT_QUERIES: dict[tuple[bool, bool, bool], str] = {
    (True, True, True): (
        "SELECT COUNT(*) FROM user_stories WHERE project_id = ? "
        "AND is_refined = 1 AND COALESCE(is_superseded, 0) = 0 "
        "AND accepted_spec_version_id IS NOT NULL"
    ),
    (True, True, False): (
        "SELECT COUNT(*) FROM user_stories WHERE project_id = ? "
        "AND is_refined = 1 AND COALESCE(is_superseded, 0) = 0"
    ),
    (True, False, True): (
        "SELECT COUNT(*) FROM user_stories WHERE project_id = ? "
        "AND is_refined = 1 AND accepted_spec_version_id IS NOT NULL"
    ),
    (True, False, False): (
        "SELECT COUNT(*) FROM user_stories WHERE project_id = ? AND is_refined = 1"
    ),
    (False, True, True): (
        "SELECT COUNT(*) FROM user_stories WHERE project_id = ? "
        "AND COALESCE(is_superseded, 0) = 0 "
        "AND accepted_spec_version_id IS NOT NULL"
    ),
    (False, True, False): (
        "SELECT COUNT(*) FROM user_stories WHERE project_id = ? "
        "AND COALESCE(is_superseded, 0) = 0"
    ),
    (False, False, True): (
        "SELECT COUNT(*) FROM user_stories WHERE project_id = ? "
        "AND accepted_spec_version_id IS NOT NULL"
    ),
    (False, False, False): (
        "SELECT COUNT(*) FROM user_stories WHERE project_id = ?"
    ),
}

# ============================================================================
# Data Classes for Metrics
# ============================================================================


@dataclass
class ProjectMetrics:
    """Metrics for a single product/project."""

    project_id: int
    product_name: str
    created_at: str

    # Story counts
    total_stories: int = 0
    stories_with_spec_version_id: int = 0
    refined_stories_with_spec_version_id: int = 0
    stories_with_validation_evidence: int = 0
    stories_approved_first_pass: int = 0
    stories_refined: int = 0

    # Sprint metrics
    sprint_count: int = 0
    sprint_plan_drafts: int = 0
    sprint_plan_saves: int = 0
    vision_saves: int = 0
    backlog_saves: int = 0
    roadmap_saves: int = 0
    stories_saves: int = 0
    spec_compiles: int = 0
    avg_vision_duration_sec: float = 0.0
    avg_spec_compile_duration_sec: float = 0.0
    avg_backlog_duration_sec: float = 0.0
    avg_roadmap_duration_sec: float = 0.0
    avg_stories_duration_sec: float = 0.0

    # Workflow timing (from WorkflowEvent)
    total_sprint_planning_duration_sec: float = 0.0
    avg_sprint_planning_duration_sec: float = 0.0

    # Flow Efficiency & Execution (Dev Support Agent)
    avg_story_cycle_time_hours: float = 0.0
    stories_with_evidence_links: int = 0
    total_tasks: int = 0
    completed_tasks: int = 0

    # Spec Authority metrics
    spec_versions_count: int = 0
    spec_versions_approved: int = 0
    compiled_authorities_count: int = 0
    spec_accepted_count: int = 0
    spec_rejected_count: int = 0


@dataclass
class SmokeRunMetrics:
    """Aggregated metrics from smoke_runs.jsonl."""

    total_runs: int = 0
    pipeline_ran_count: int = 0
    alignment_rejected_count: int = 0
    acceptance_blocked_count: int = 0
    contract_passed_count: int = 0

    # Timing aggregates (milliseconds)
    avg_total_ms: float = 0.0
    avg_compile_ms: float = 0.0
    avg_pipeline_ms: float = 0.0
    avg_validation_ms: float = 0.0

    # Spec version pinning
    spec_version_id_match_count: int = 0
    spec_version_id_mismatch_count: int = 0


@dataclass
class ExtractionResult:
    """Complete extraction result."""

    extraction_timestamp: str
    db_path: str
    db_filename: str
    commit_hash: str | None

    # Schema info
    tables: list = field(default_factory=list)

    # Per-product metrics
    projects: list = field(default_factory=list)

    # Smoke run metrics (if available)
    smoke_runs: SmokeRunMetrics | None = None

    # Workflow events summary
    workflow_events_by_type: dict = field(default_factory=dict)
    state_dwell_by_state: dict = field(default_factory=dict)

    # Task mapping notes
    task_mapping_notes: str = ""


# ============================================================================
# Database Extraction Functions
# ============================================================================


def _quote_sql_identifier(identifier: str) -> str:
    if not _SQL_IDENTIFIER_RE.fullmatch(identifier):
        msg = f"Unsafe SQLite identifier: {identifier!r}"
        raise ValueError(msg)
    return f'"{identifier}"'


def _resolve_git_dir(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        marker = directory / ".git"
        if marker.is_dir():
            return marker
        if marker.is_file():
            content = marker.read_text(encoding="utf-8").strip()
            prefix = "gitdir:"
            if not content.startswith(prefix):
                return None
            git_dir = Path(content.removeprefix(prefix).strip())
            return git_dir if git_dir.is_absolute() else directory / git_dir
    return None


def _read_git_ref(git_dir: Path, ref_name: str) -> str | None:
    ref_path = git_dir / ref_name
    if ref_path.is_file():
        return ref_path.read_text(encoding="utf-8").strip()[:12]

    packed_refs = git_dir / "packed-refs"
    if not packed_refs.is_file():
        return None
    for line in packed_refs.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith(("#", "^")):
            continue
        commit_hash, _, packed_ref = line.partition(" ")
        if packed_ref == ref_name:
            return commit_hash[:12]
    return None


def get_git_commit_hash() -> str | None:
    """Get current git commit hash, or None if not in a git repo."""
    git_dir = _resolve_git_dir(Path.cwd())
    if git_dir is None:
        return None

    head_path = git_dir / "HEAD"
    if not head_path.is_file():
        return None
    head = head_path.read_text(encoding="utf-8").strip()
    if head.startswith("ref: "):
        return _read_git_ref(git_dir, head.removeprefix("ref: ").strip())
    return head[:12] if head else None


def _refined_story_count_query(
    *,
    has_is_refined: bool,
    has_is_superseded: bool,
    require_spec_version: bool,
) -> str:
    return _REFINED_STORY_COUNT_QUERIES[
        (has_is_refined, has_is_superseded, require_spec_version)
    ]



def get_schema_summary(cursor: sqlite3.Cursor) -> list[dict]:
    """Extract schema summary for all tables."""
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = []

    for (table_name,) in cursor.fetchall():
        quoted_table_name = _quote_sql_identifier(str(table_name))
        cursor.execute("PRAGMA table_info(" + quoted_table_name + ")")
        columns = [
            {
                "name": col[1],
                "type": col[2],
                "notnull": bool(col[3]),
                "pk": bool(col[5]),
            }
            for col in cursor.fetchall()
        ]

        # Table names come from sqlite_master and are allowlisted as identifiers.
        cursor.execute("SELECT COUNT(*) FROM " + quoted_table_name)  # nosec B608
        row_count = cursor.fetchone()[0]

        tables.append({"name": table_name, "columns": columns, "row_count": row_count})

    return tables


def extract_product_metrics(cursor: sqlite3.Cursor) -> list[ProjectMetrics]:  # noqa: C901, PLR0912, PLR0915
    """Extract metrics for each product in the database."""
    projects = []

    # Get all projects
    cursor.execute("""
        SELECT project_id, name, created_at
        FROM projects
        ORDER BY project_id
    """)
    product_rows = cursor.fetchall()

    for project_id, name, created_at in product_rows:
        metrics = ProjectMetrics(
            project_id=project_id, product_name=name, created_at=str(created_at)
        )

        # Story counts
        cursor.execute(
            """
            SELECT COUNT(*) FROM user_stories WHERE project_id = ?
        """,
            (project_id,),
        )
        metrics.total_stories = cursor.fetchone()[0]

        # Check if accepted_spec_version_id column exists
        cursor.execute("PRAGMA table_info(user_stories)")
        columns = [col[1] for col in cursor.fetchall()]

        has_is_refined = "is_refined" in columns
        has_is_superseded = "is_superseded" in columns

        cursor.execute(
            _refined_story_count_query(
                has_is_refined=has_is_refined,
                has_is_superseded=has_is_superseded,
                require_spec_version=False,
            ),
            (project_id,),
        )
        metrics.stories_refined = cursor.fetchone()[0]

        if "accepted_spec_version_id" in columns:
            cursor.execute(
                """
                SELECT COUNT(*) FROM user_stories
                WHERE project_id = ? AND accepted_spec_version_id IS NOT NULL
            """,
                (project_id,),
            )
            metrics.stories_with_spec_version_id = cursor.fetchone()[0]

            query = _refined_story_count_query(
                has_is_refined=has_is_refined,
                has_is_superseded=has_is_superseded,
                require_spec_version=True,
            )
            cursor.execute(query, (project_id,))
            metrics.refined_stories_with_spec_version_id = cursor.fetchone()[0]

        if "validation_evidence" in columns:
            cursor.execute(
                """
                SELECT COUNT(*) FROM user_stories
                WHERE project_id = ? AND validation_evidence IS NOT NULL
            """,
                (project_id,),
            )
            metrics.stories_with_validation_evidence = cursor.fetchone()[0]

        # Sprint counts
        cursor.execute(
            """
            SELECT COUNT(*) FROM sprints WHERE project_id = ?
        """,
            (project_id,),
        )
        metrics.sprint_count = cursor.fetchone()[0]

        # Workflow events
        cursor.execute(
            """
            SELECT event_type, COUNT(*)
            FROM workflow_events
            WHERE project_id = ?
            GROUP BY event_type
        """,
            (project_id,),
        )
        for event_type, count in cursor.fetchall():
            if event_type == "VISION_SAVED":
                metrics.vision_saves = count
            elif event_type == "SPEC_COMPILED":
                metrics.spec_compiles = count
            elif event_type == "BACKLOG_SAVED":
                metrics.backlog_saves = count
            elif event_type == "ROADMAP_SAVED":
                metrics.roadmap_saves = count
            elif event_type == "STORIES_SAVED":
                metrics.stories_saves = count
            if event_type == "SPRINT_PLAN_DRAFT":
                metrics.sprint_plan_drafts = count
            elif event_type == "SPRINT_PLAN_SAVED":
                metrics.sprint_plan_saves = count

        # Sprint planning timing
        cursor.execute(
            """
            SELECT SUM(duration_seconds), AVG(duration_seconds)
            FROM workflow_events
            WHERE project_id = ? AND event_type = 'SPRINT_PLAN_SAVED' AND duration_seconds IS NOT NULL
        """,
            (project_id,),
        )
        result = cursor.fetchone()
        if result[0]:
            metrics.total_sprint_planning_duration_sec = round(result[0], 2)
            metrics.avg_sprint_planning_duration_sec = round(result[1], 2)

        # Artifact creation timings
        duration_queries = {
            "VISION_SAVED": "avg_vision_duration_sec",
            "SPEC_COMPILED": "avg_spec_compile_duration_sec",
            "BACKLOG_SAVED": "avg_backlog_duration_sec",
            "ROADMAP_SAVED": "avg_roadmap_duration_sec",
            "STORIES_SAVED": "avg_stories_duration_sec",
        }
        for event_name, attr in duration_queries.items():
            cursor.execute(
                """
                SELECT AVG(duration_seconds)
                FROM workflow_events
                WHERE project_id = ?
                  AND event_type = ?
                  AND duration_seconds IS NOT NULL
                """,
                (project_id, event_name),
            )
            avg_val = cursor.fetchone()[0]
            if avg_val:
                setattr(metrics, attr, round(avg_val, 2))

        # Flow Efficiency (Cycle Time)
        if "completed_at" in columns:
            cursor.execute(
                """
                SELECT AVG((julianday(completed_at) - julianday(created_at)) * 24)
                FROM user_stories
                WHERE project_id = ? AND completed_at IS NOT NULL AND created_at IS NOT NULL
            """,
                (project_id,),
            )
            cycle_time = cursor.fetchone()[0]
            if cycle_time:
                metrics.avg_story_cycle_time_hours = round(cycle_time, 2)

        # Execution Evidence (DoD)
        if "evidence_links" in columns:
            cursor.execute(
                """
                SELECT COUNT(*) FROM user_stories
                WHERE project_id = ? AND evidence_links IS NOT NULL AND evidence_links != '[]'
            """,
                (project_id,),
            )
            metrics.stories_with_evidence_links = cursor.fetchone()[0]

        # Task Execution metrics (Dev Support Agent)
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
        )
        if cursor.fetchone():
            cursor.execute(
                """
                SELECT COUNT(*), SUM(CASE WHEN t.status = 'DONE' THEN 1 ELSE 0 END)
                FROM tasks t
                JOIN user_stories us ON t.story_id = us.story_id
                WHERE us.project_id = ?
            """,
                (project_id,),
            )
            task_res = cursor.fetchone()
            if task_res:
                metrics.total_tasks = task_res[0] or 0
                metrics.completed_tasks = task_res[1] or 0

        # Spec Authority metrics (if tables exist)
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='spec_registry'"
        )
        if cursor.fetchone():
            cursor.execute(
                """
                SELECT COUNT(*) FROM spec_registry WHERE project_id = ?
            """,
                (project_id,),
            )
            metrics.spec_versions_count = cursor.fetchone()[0]

            cursor.execute(
                """
                SELECT COUNT(*) FROM spec_registry
                WHERE project_id = ? AND status = 'approved'
            """,
                (project_id,),
            )
            metrics.spec_versions_approved = cursor.fetchone()[0]

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='compiled_spec_authority'"
        )
        if cursor.fetchone():
            cursor.execute(
                """
                SELECT COUNT(*) FROM compiled_spec_authority csa
                JOIN spec_registry sr ON csa.spec_version_id = sr.spec_version_id
                WHERE sr.project_id = ?
            """,
                (project_id,),
            )
            metrics.compiled_authorities_count = cursor.fetchone()[0]

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='spec_authority_acceptance'"
        )
        if cursor.fetchone():
            cursor.execute(
                """
                SELECT status, COUNT(*)
                FROM spec_authority_acceptance
                WHERE project_id = ?
                GROUP BY status
            """,
                (project_id,),
            )
            for status, count in cursor.fetchall():
                if status == "accepted":
                    metrics.spec_accepted_count = count
                elif status == "rejected":
                    metrics.spec_rejected_count = count

        projects.append(metrics)

    return projects


def extract_workflow_events_summary(cursor: sqlite3.Cursor) -> dict:
    """Extract summary of all workflow events."""
    cursor.execute("""
        SELECT event_type, COUNT(*),
               AVG(duration_seconds),
               SUM(duration_seconds),
               MIN(timestamp),
               MAX(timestamp)
        FROM workflow_events
        GROUP BY event_type
        ORDER BY event_type
    """)

    summary = {}
    for row in cursor.fetchall():
        event_type, count, avg_duration, total_duration, min_ts, max_ts = row
        summary[event_type] = {
            "count": count,
            "avg_duration_sec": round(avg_duration, 2) if avg_duration else None,
            "total_duration_sec": round(total_duration, 2) if total_duration else None,
            "first_event": min_ts,
            "last_event": max_ts,
        }

    return summary


def extract_state_dwell_summary(cursor: sqlite3.Cursor) -> dict:
    """Extract per-FSM-state dwell duration summary from workflow events."""
    cursor.execute(
        """
        SELECT event_metadata, duration_seconds
        FROM workflow_events
        WHERE event_type = 'FSM_STATE_DWELL'
          AND duration_seconds IS NOT NULL
        """
    )

    buckets: dict[str, list[float]] = {}
    for metadata_raw, duration in cursor.fetchall():
        if duration is None:
            continue
        payload = json.loads(metadata_raw) if metadata_raw else {}
        from_state = payload.get("from_state")
        if not isinstance(from_state, str) or not from_state:
            from_state = "UNKNOWN"
        buckets.setdefault(from_state, []).append(float(duration))

    summary: dict[str, dict[str, Any]] = {}
    for state_name in sorted(buckets.keys()):
        durations = buckets[state_name]
        summary[state_name] = {
            "count": len(durations),
            "avg_duration_sec": round(sum(durations) / len(durations), 2),
            "total_duration_sec": round(sum(durations), 2),
        }
    return summary


def extract_smoke_run_metrics(artifacts_dir: Path) -> SmokeRunMetrics | None:  # noqa: C901, PLR0912
    """Extract metrics from smoke_runs.jsonl if available."""
    smoke_file = artifacts_dir / "smoke_runs.jsonl"
    if not smoke_file.exists():
        return None

    metrics = SmokeRunMetrics()
    total_ms_list = []
    compile_ms_list = []
    pipeline_ms_list = []
    validation_ms_list = []

    with open(smoke_file, encoding="utf-8") as f:  # noqa: PTH123
        for line in f:
            if not line.strip():
                continue
            run = json.loads(line)
            metrics.total_runs += 1

            # Stage counts
            stage = run.get("METRICS", {}).get("stage", "")
            if stage == "pipeline_ran":
                metrics.pipeline_ran_count += 1
            elif stage == "alignment_rejected":
                metrics.alignment_rejected_count += 1
            elif stage == "acceptance_blocked":
                metrics.acceptance_blocked_count += 1

            # Contract passed
            if run.get("METRICS", {}).get("contract_passed") is True:
                metrics.contract_passed_count += 1

            # Spec version ID match
            if run.get("SPEC_VERSION_ID_MATCH") is True:
                metrics.spec_version_id_match_count += 1
            elif run.get("SPEC_VERSION_ID_MATCH") is False:
                metrics.spec_version_id_mismatch_count += 1

            # Timing
            timing = run.get("TIMING_MS", {})
            if timing.get("total_ms") is not None:
                total_ms_list.append(timing["total_ms"])
            if timing.get("compile_ms") is not None:
                compile_ms_list.append(timing["compile_ms"])
            if timing.get("pipeline_ms") is not None:
                pipeline_ms_list.append(timing["pipeline_ms"])
            if timing.get("validation_ms") is not None:
                validation_ms_list.append(timing["validation_ms"])

    # Calculate averages
    if total_ms_list:
        metrics.avg_total_ms = round(sum(total_ms_list) / len(total_ms_list), 2)
    if compile_ms_list:
        metrics.avg_compile_ms = round(sum(compile_ms_list) / len(compile_ms_list), 2)
    if pipeline_ms_list:
        metrics.avg_pipeline_ms = round(
            sum(pipeline_ms_list) / len(pipeline_ms_list), 2
        )
    if validation_ms_list:
        metrics.avg_validation_ms = round(
            sum(validation_ms_list) / len(validation_ms_list), 2
        )

    return metrics


# ============================================================================
# Task Mapping (T1-T5)
# ============================================================================

TASK_MAPPING_NOTES = """
## Task-to-Event Mapping (T1-T5)

The database does NOT encode T1-T5 directly. Proposed mapping:

| Task | Description | DB Representation | Notes |
|------|-------------|-------------------|-------|
| T1 | Definição de Visão | projects.vision, projects.created_at | No explicit timing event |
| T2 | Especificação Técnica | projects.technical_spec, spec_registry | No explicit timing event |
| T3 | Compilação de Autoridade | compiled_spec_authority.compiled_at, spec_authority_acceptance | Timing available via compiled_at |
| T4 | Geração de Backlog | user_stories.created_at, validation_evidence | Aggregate by product |
| T5 | Planejamento de Sprint | WorkflowEvent.SPRINT_PLAN_SAVED.duration_seconds | Explicit timing available |
| State Dwell | Tempo por estado FSM (inclui tempo de interação/humano) | WorkflowEvent.FSM_STATE_DWELL.duration_seconds + event_metadata.from_state | Explicit timing available |

**Caveats:**
- T1/T2/T4 cycle times must be computed as wall-clock differences between timestamps
- Baseline manual times are NOT in the database (collected externally)
- NASA-TLX scores are NOT stored in the database (collected via questionnaire)

**Recommended approach:**
- Use WorkflowEvent timestamps for T5 (sprint planning) cycle time
- Use WorkflowEvent.FSM_STATE_DWELL for true per-state platform dwell time
- Use projects.created_at and first story created_at for T4 rough estimate
- Use spec_registry/compiled_spec_authority timestamps for T3
- T1/T2 timing requires external baseline data or session logs
"""


# ============================================================================
# Output Functions
# ============================================================================


def write_csv_results(output_dir: Path, result: ExtractionResult) -> None:
    """Write per-query CSV files."""
    query_dir = output_dir / "query_results"
    query_dir.mkdir(parents=True, exist_ok=True)

    # Projects summary
    if result.projects:
        with open(  # noqa: PTH123
            query_dir / "products_metrics.csv", "w", newline="", encoding="utf-8"
        ) as f:
            writer = csv.DictWriter(f, fieldnames=asdict(result.projects[0]).keys())
            writer.writeheader()
            for p in result.projects:
                writer.writerow(asdict(p))

    # Workflow events
    if result.workflow_events_by_type:
        with open(  # noqa: PTH123
            query_dir / "workflow_events_summary.csv", "w", newline="", encoding="utf-8"
        ) as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "event_type",
                    "count",
                    "avg_duration_sec",
                    "total_duration_sec",
                    "first_event",
                    "last_event",
                ]
            )
            for event_type, data in result.workflow_events_by_type.items():
                writer.writerow(
                    [
                        event_type,
                        data["count"],
                        data["avg_duration_sec"],
                        data["total_duration_sec"],
                        data["first_event"],
                        data["last_event"],
                    ]
                )

    if result.state_dwell_by_state:
        with open(  # noqa: PTH123
            query_dir / "state_dwell_summary.csv", "w", newline="", encoding="utf-8"
        ) as f:
            writer = csv.writer(f)
            writer.writerow(
                ["from_state", "count", "avg_duration_sec", "total_duration_sec"]
            )
            for state_name, data in result.state_dwell_by_state.items():
                writer.writerow(
                    [
                        state_name,
                        data["count"],
                        data["avg_duration_sec"],
                        data["total_duration_sec"],
                    ]
                )

    # Smoke runs
    if result.smoke_runs:
        with open(  # noqa: PTH123
            query_dir / "smoke_runs_metrics.csv", "w", newline="", encoding="utf-8"
        ) as f:
            writer = csv.DictWriter(f, fieldnames=asdict(result.smoke_runs).keys())
            writer.writeheader()
            writer.writerow(asdict(result.smoke_runs))


def write_summary_csv(output_file: Path, result: ExtractionResult) -> None:
    """Write main metrics summary CSV."""
    with open(output_file, "w", newline="", encoding="utf-8") as f:  # noqa: PTH123
        writer = csv.writer(f)

        # Header section
        writer.writerow(["=== EXTRACTION METADATA ==="])
        writer.writerow(["timestamp", result.extraction_timestamp])
        writer.writerow(["db_path", result.db_path])
        writer.writerow(["db_filename", result.db_filename])
        writer.writerow(["commit_hash", result.commit_hash or "N/A"])
        writer.writerow([])

        # Per-product metrics
        writer.writerow(["=== PRODUCT METRICS ==="])
        if result.projects:
            headers = list(asdict(result.projects[0]).keys())
            writer.writerow(headers)
            for p in result.projects:
                writer.writerow(list(asdict(p).values()))
        writer.writerow([])

        # Workflow events
        writer.writerow(["=== WORKFLOW EVENTS ==="])
        writer.writerow(
            ["event_type", "count", "avg_duration_sec", "total_duration_sec"]
        )
        for event_type, data in result.workflow_events_by_type.items():
            writer.writerow(
                [
                    event_type,
                    data["count"],
                    data["avg_duration_sec"],
                    data["total_duration_sec"],
                ]
            )


def write_summary_json(output_file: Path, result: ExtractionResult) -> None:
    """Write main metrics summary JSON."""
    output_dict = {
        "extraction_metadata": {
            "timestamp": result.extraction_timestamp,
            "db_path": result.db_path,
            "db_filename": result.db_filename,
            "commit_hash": result.commit_hash,
        },
        "schema": {"tables": result.tables},
        "projects": [asdict(p) for p in result.projects],
        "workflow_events": result.workflow_events_by_type,
        "state_dwell_by_state": result.state_dwell_by_state,
        "smoke_runs": asdict(result.smoke_runs) if result.smoke_runs else None,
        "task_mapping_notes": result.task_mapping_notes,
    }

    with open(output_file, "w", encoding="utf-8") as f:  # noqa: PTH123
        json.dump(output_dict, f, indent=2, ensure_ascii=False)


def print_markdown_summary(result: ExtractionResult) -> None:  # noqa: PLR0915
    """Print markdown-ready summary to stdout."""
    emit("=" * 80)
    emit("# TCC Metrics Extraction Summary")
    emit("=" * 80)
    emit()
    emit(f"**Database:** `{result.db_filename}`")
    emit(f"**Extraction Time:** {result.extraction_timestamp}")
    emit(f"**Commit Hash:** {result.commit_hash or 'N/A'}")
    emit()

    # Projects table
    emit("## Projects Summary")
    emit()
    emit(
        "| Project ID | Name | Stories | Refined | Refined w/ Spec Version | Sprints | Sprint Plan Events |"
    )
    emit(
        "|------------|------|---------|---------|--------------------------|---------|-------------------|"
    )
    for p in result.projects:
        name_short = (
            p.product_name[:40] + "..." if len(p.product_name) > 40 else p.product_name  # noqa: PLR2004
        )
        emit(
            f"| {p.project_id} | {name_short} | {p.total_stories} | {p.stories_refined} | "
            f"{p.refined_stories_with_spec_version_id} | {p.sprint_count} | {p.sprint_plan_saves} |"
        )
    emit()

    # Workflow events
    emit("## Workflow Events Summary")
    emit()
    emit("| Event Type | Count | Avg Duration (s) | Total Duration (s) |")
    emit("|------------|-------|------------------|-------------------|")
    for event_type, data in result.workflow_events_by_type.items():
        avg_dur = data["avg_duration_sec"] if data["avg_duration_sec"] else "N/A"
        total_dur = data["total_duration_sec"] if data["total_duration_sec"] else "N/A"
        emit(f"| {event_type} | {data['count']} | {avg_dur} | {total_dur} |")
    emit()

    if result.state_dwell_by_state:
        emit("## FSM State Dwell Time (Includes User Thinking/Interaction)")
        emit()
        emit("| FSM State | Exits | Avg Dwell (s) | Total Dwell (s) |")
        emit("|-----------|-------|---------------|-----------------|")
        for state_name, data in result.state_dwell_by_state.items():
            emit(
                f"| {state_name} | {data['count']} | {data['avg_duration_sec']} | {data['total_duration_sec']} |"
            )
        emit()

    # Spec Authority Pinning Coverage
    emit("## Spec Authority Pinning Coverage")
    emit()
    total_refined = sum(p.stories_refined for p in result.projects)
    refined_with_spec = sum(
        p.refined_stories_with_spec_version_id for p in result.projects
    )
    pct = (refined_with_spec / total_refined * 100) if total_refined > 0 else 0
    emit(f"- Total canonical refined stories: {total_refined}")
    emit(
        f"- Refined stories with valid `accepted_spec_version_id`: {refined_with_spec} ({pct:.1f}%)"
    )
    emit(f"- Refined stories without spec version: {total_refined - refined_with_spec}")
    emit()

    # Flow Efficiency & Execution Metrics (Added for Proposal alignment)
    emit("## Flow Efficiency & Execution Metrics")
    emit()
    emit(
        "| Project | Avg Story Cycle Time (h) | Stories w/ Evidence | Tasks (Done/Total) |"
    )
    emit(
        "|---------|--------------------------|---------------------|--------------------|"
    )
    for p in result.projects:
        name_short = p.product_name[:20]
        task_info = f"{p.completed_tasks}/{p.total_tasks}"
        emit(
            f"| {name_short} | {p.avg_story_cycle_time_hours} | {p.stories_with_evidence_links} | {task_info} |"
        )
    emit()

    # Smoke runs (if available)
    if result.smoke_runs:
        sr = result.smoke_runs
        emit("## Smoke Run Metrics (from smoke_runs.jsonl)")
        emit()
        emit(f"- Total runs: {sr.total_runs}")
        emit(f"- Pipeline ran: {sr.pipeline_ran_count}")
        emit(f"- Alignment rejected: {sr.alignment_rejected_count}")
        emit(f"- Acceptance blocked: {sr.acceptance_blocked_count}")
        emit(f"- Contract passed: {sr.contract_passed_count}")
        emit(f"- Spec version ID match: {sr.spec_version_id_match_count}")
        emit()
        emit("### Timing (avg ms)")
        emit(f"- Total: {sr.avg_total_ms}")
        emit(f"- Compile: {sr.avg_compile_ms}")
        emit(f"- Pipeline: {sr.avg_pipeline_ms}")
        emit(f"- Validation: {sr.avg_validation_ms}")
        emit()

    # Task mapping
    emit("## Task-to-Event Mapping (T1-T5)")
    emit()
    emit("| Task | DB Event/Table | Timing Available |")
    emit("|------|----------------|------------------|")
    emit("| T1 - Vision | WorkflowEvent.VISION_SAVED | Yes (duration_seconds) |")
    emit("| T2 - Tech Spec | WorkflowEvent.SPEC_COMPILED | Yes (duration_seconds) |")
    emit(
        "| T3 - Compile Authority | WorkflowEvent.SPEC_COMPILED | Yes (duration_seconds) |"
    )
    emit(
        "| T4 - Backlog Gen/Refine | WorkflowEvent.BACKLOG_SAVED + STORIES_SAVED + ROADMAP_SAVED | Yes (duration_seconds) |"
    )
    emit(
        "| T5 - Sprint Plan | WorkflowEvent.SPRINT_PLAN_SAVED | Yes (duration_seconds) |"
    )
    emit()
    emit("**Note:** Baseline manual times and NASA-TLX scores are NOT in the database.")
    emit("[PLACEHOLDER: Baseline timing data collected externally via stopwatch]")
    emit("[PLACEHOLDER: NASA-TLX scores collected via self-assessment questionnaire]")
    emit()

    # Artifact timing (T1-T5 proxies)
    emit("## Artifact Timing (T1-T5)")
    emit()
    emit(
        "| Project ID | T1 Vision (avg s) | T2 Spec/Compile (avg s) | T4 Backlog (avg s) | T4 Roadmap (avg s) | T4 Stories (avg s) | T5 Sprint (avg s) |"
    )
    emit(
        "|------------|-------------------|--------------------------|--------------------|--------------------|--------------------|-------------------|"
    )
    for p in result.projects:

        def _fmt(v: float) -> str:
            return str(v) if v and v > 0 else "N/A"

        emit(
            f"| {p.project_id} | {_fmt(p.avg_vision_duration_sec)} | "
            f"{_fmt(p.avg_spec_compile_duration_sec)} | {_fmt(p.avg_backlog_duration_sec)} | "
            f"{_fmt(p.avg_roadmap_duration_sec)} | {_fmt(p.avg_stories_duration_sec)} | "
            f"{_fmt(p.avg_sprint_planning_duration_sec)} |"
        )
    emit()


# ============================================================================
# Main Entry Point
# ============================================================================


def main() -> None:
    """Return main."""
    parser = argparse.ArgumentParser(
        description="Extract TCC evaluation metrics from SQLite database"
    )
    parser.add_argument(
        "db_path",
        nargs="?",
        help="Optional SQLite database path or sqlite:/// URL. Defaults to AGILEFORGE_DB_URL.",
    )
    parser.add_argument(
        "--output-dir",
        default="artifacts",
        help="Output directory for extracted files (default: artifacts)",
    )
    args = parser.parse_args()

    db_target = resolve_database_target(args.db_path, env_name="AGILEFORGE_DB_URL")
    if db_target.sqlite_path is None:
        emit(
            "ERROR: Metrics extraction requires a file-backed SQLite database.",
            file=sys.stderr,
        )
        sys.exit(1)

    db_path = db_target.sqlite_path
    if not db_path.exists():
        emit(
            f"ERROR: Database file not found: {db_target.sqlite_connect_target}",
            file=sys.stderr,
        )
        sys.exit(1)

    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = Path(__file__).parent.parent / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    # Initialize result
    result = ExtractionResult(
        extraction_timestamp=datetime.now(UTC).isoformat(),
        db_path=str(db_path),
        db_filename=db_path.name,
        commit_hash=get_git_commit_hash(),
        task_mapping_notes=TASK_MAPPING_NOTES,
    )

    # Connect to database
    emit(f"Connecting to database: {db_path}", file=sys.stderr)
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # Extract data
    emit("Extracting schema summary...", file=sys.stderr)
    result.tables = get_schema_summary(cursor)

    emit("Extracting product metrics...", file=sys.stderr)
    result.projects = extract_product_metrics(cursor)

    emit("Extracting workflow events...", file=sys.stderr)
    result.workflow_events_by_type = extract_workflow_events_summary(cursor)
    result.state_dwell_by_state = extract_state_dwell_summary(cursor)

    conn.close()

    # Extract smoke run metrics (from artifacts dir)
    artifacts_path = Path(__file__).parent.parent / "artifacts"
    emit("Checking for smoke_runs.jsonl...", file=sys.stderr)
    result.smoke_runs = extract_smoke_run_metrics(artifacts_path)

    # Write outputs
    emit(f"Writing outputs to {output_dir}/...", file=sys.stderr)
    write_summary_csv(output_dir / "metrics_summary.csv", result)
    write_summary_json(output_dir / "metrics_summary.json", result)
    write_csv_results(output_dir, result)

    # Print markdown summary to stdout
    print_markdown_summary(result)

    emit("\nOutputs written to:", file=sys.stderr)
    emit(f"  - {output_dir}/metrics_summary.csv", file=sys.stderr)
    emit(f"  - {output_dir}/metrics_summary.json", file=sys.stderr)
    emit(f"  - {output_dir}/query_results/*.csv", file=sys.stderr)


if __name__ == "__main__":
    main()
