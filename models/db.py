"""Database engine helpers shared by the business model layer."""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    UniqueConstraint,
    event,
    inspect,
)
from sqlalchemy.engine import Engine
from sqlmodel import SQLModel, create_engine

from models import (
    core,
    events,
    product_definition,
    repository,
    specs,
    workflow,
)
from utils.runtime_config import get_business_db_target, get_database_echo

if TYPE_CHECKING:
    import sqlite3
    from types import ModuleType

logger: logging.Logger = logging.getLogger(name=__name__)

_CURRENT_MODEL_MODULES: tuple[ModuleType, ...] = (
    core,
    specs,
    events,
    product_definition,
    repository,
    workflow,
)


@dataclass(frozen=True)
class TableStructure:
    """Name-independent normalized structure for one business-critical table."""

    columns: tuple[tuple[str, bool], ...]
    uniques: frozenset[tuple[tuple[str, ...], str | None]]
    foreign_keys: frozenset[tuple[tuple[str, ...], str, tuple[str, ...]]]
    checks: frozenset[str]


@dataclass(frozen=True)
class BusinessSchemaManifest:
    """Reviewed fresh-schema table set and complete critical structures."""

    table_names: frozenset[str]
    structures: dict[str, TableStructure]


def _normalize_sql_expression(expression: object) -> str:
    """Normalize inspector/metadata SQL without depending on identifier names."""
    normalized = re.sub(r"\s+", " ", str(expression).strip()).casefold()
    while normalized.startswith("(") and normalized.endswith(")"):
        depth = 0
        encloses_expression = True
        for index, character in enumerate(normalized):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
            if depth == 0 and index != len(normalized) - 1:
                encloses_expression = False
                break
        if not encloses_expression:
            break
        normalized = normalized[1:-1].strip()
    return normalized


def _structure(
    *,
    columns: tuple[tuple[str, bool], ...],
    uniques: tuple[tuple[tuple[str, ...], str | None], ...] = (),
    foreign_keys: tuple[tuple[tuple[str, ...], str, tuple[str, ...]], ...] = (),
    checks: tuple[str, ...] = (),
) -> TableStructure:
    return TableStructure(
        columns=columns,
        uniques=frozenset(
            (column_names, _normalize_sql_expression(predicate) if predicate else None)
            for column_names, predicate in uniques
        ),
        foreign_keys=frozenset(foreign_keys),
        checks=frozenset(_normalize_sql_expression(check) for check in checks),
    )


_CURRENT_TABLE_NAMES = frozenset(
    {
        "backlog_artifact_decisions",
        "backlog_artifacts",
        "epics",
        "features",
        "post_sprint_triage",
        "product_goal_artifact_decisions",
        "product_goal_artifacts",
        "product_goal_interview_turns",
        "product_goal_outcomes",
        "project_personas",
        "project_teams",
        "projects",
        "repository_bindings",
        "roadmap_artifact_decisions",
        "roadmap_artifacts",
        "spec_registry",
        "specification_candidates",
        "specification_decisions",
        "specification_sources",
        "sprint_closures",
        "sprint_plan_artifact_decisions",
        "sprint_plan_artifacts",
        "sprint_reviews",
        "sprint_starts",
        "sprint_stories",
        "sprints",
        "story_artifact_decisions",
        "story_artifacts",
        "story_closures",
        "story_completion_logs",
        "story_dependency_reviews",
        "task_completion_evidence",
        "task_execution_logs",
        "tasks",
        "team_members",
        "team_memberships",
        "teams",
        "themes",
        "user_stories",
        "user_story_dependencies",
        "vision_artifact_decisions",
        "vision_artifacts",
        "vision_evidence_snapshots",
        "vision_interview_turns",
        "vision_revision_intents",
        "workflow_events",
        "workflow_node_attempt_outcomes",
        "workflow_node_attempts",
        "workflow_transition_receipts",
    }
)

CURRENT_BUSINESS_SCHEMA_MANIFEST = BusinessSchemaManifest(
    table_names=_CURRENT_TABLE_NAMES,
    structures={
        "backlog_artifact_decisions": _structure(
            columns=(
                ("backlog_artifact_decision_id", False),
                ("project_id", False),
                ("backlog_artifact_id", False),
                ("artifact_fingerprint", False),
                ("decision", False),
                ("rationale", False),
                ("reviewer", False),
                ("idempotency_key", False),
                ("decided_at", False),
            ),
            uniques=((("project_id", "backlog_artifact_id"), None),),
            foreign_keys=(
                (
                    ("project_id", "backlog_artifact_id", "artifact_fingerprint"),
                    "backlog_artifacts",
                    ("project_id", "backlog_artifact_id", "content_fingerprint"),
                ),
            ),
            checks=("decision in ('accepted', 'rejected', 'feedback')",),
        ),
        "epics": _structure(
            columns=(
                ("epic_id", False),
                ("title", False),
                ("summary", True),
                ("created_at", False),
                ("updated_at", False),
                ("theme_id", False),
            ),
            foreign_keys=((("theme_id",), "themes", ("theme_id",)),),
        ),
        "features": _structure(
            columns=(
                ("feature_id", False),
                ("title", False),
                ("description", True),
                ("created_at", False),
                ("updated_at", False),
                ("epic_id", False),
            ),
            foreign_keys=((("epic_id",), "epics", ("epic_id",)),),
        ),
        "post_sprint_triage": _structure(
            columns=(
                ("triage_id", False),
                ("project_id", False),
                ("sprint_id", False),
                ("impact", False),
                ("canonical_payload_json", False),
                ("payload_fingerprint", False),
                ("supersedes_triage_id", True),
                ("recorded_by", False),
                ("recorded_at", False),
            ),
            uniques=((("project_id", "sprint_id", "supersedes_triage_id"), None),),
            foreign_keys=(
                (("project_id",), "projects", ("project_id",)),
                (("sprint_id",), "sprints", ("sprint_id",)),
                (("supersedes_triage_id",), "post_sprint_triage", ("triage_id",)),
            ),
            checks=("impact in ('none', 'backlog', 'specification')",),
        ),
        "product_goal_artifact_decisions": _structure(
            columns=(
                ("product_goal_artifact_decision_id", False),
                ("project_id", False),
                ("product_goal_artifact_id", False),
                ("artifact_fingerprint", False),
                ("decision", False),
                ("rationale", False),
                ("reviewer", False),
                ("idempotency_key", False),
                ("decided_at", False),
            ),
            uniques=((("project_id", "idempotency_key"), None),),
            foreign_keys=(
                (("project_id",), "projects", ("project_id",)),
                (
                    ("project_id", "product_goal_artifact_id", "artifact_fingerprint"),
                    "product_goal_artifacts",
                    ("project_id", "product_goal_artifact_id", "content_fingerprint"),
                ),
            ),
            checks=("decision in ('accepted', 'rejected', 'feedback')",),
        ),
        "product_goal_artifacts": _structure(
            columns=(
                ("product_goal_artifact_id", False),
                ("project_id", False),
                ("vision_artifact_id", False),
                ("vision_fingerprint", False),
                ("goal_number", False),
                ("revision_number", False),
                ("statement", False),
                ("content_fingerprint", False),
                ("supersedes_product_goal_artifact_id", True),
                ("source_interview_turn_id", False),
                ("created_by", False),
                ("created_at", False),
            ),
            uniques=(
                (("project_id", "goal_number", "revision_number"), None),
                (("project_id", "product_goal_artifact_id"), None),
                (
                    ("project_id", "product_goal_artifact_id", "content_fingerprint"),
                    None,
                ),
            ),
            foreign_keys=(
                (("project_id",), "projects", ("project_id",)),
                (
                    ("project_id", "source_interview_turn_id"),
                    "product_goal_interview_turns",
                    ("project_id", "product_goal_interview_turn_id"),
                ),
                (
                    ("project_id", "supersedes_product_goal_artifact_id"),
                    "product_goal_artifacts",
                    ("project_id", "product_goal_artifact_id"),
                ),
                (
                    ("project_id", "vision_artifact_id", "vision_fingerprint"),
                    "vision_artifacts",
                    ("project_id", "vision_artifact_id", "content_fingerprint"),
                ),
            ),
        ),
        "product_goal_interview_turns": _structure(
            columns=(
                ("product_goal_interview_turn_id", False),
                ("project_id", False),
                ("vision_artifact_id", False),
                ("vision_fingerprint", False),
                ("goal_number", False),
                ("revision_number", False),
                ("prior_turn_id", True),
                ("user_text", False),
                ("components_json", False),
                ("goal_statement", False),
                ("is_complete", False),
                ("clarifying_questions_json", False),
                ("output_fingerprint", False),
                ("workflow_node_attempt_id", False),
                ("attempt_fingerprint", False),
                ("recorded_at", False),
            ),
            uniques=(
                (
                    (
                        "project_id",
                        "goal_number",
                        "revision_number",
                        "product_goal_interview_turn_id",
                    ),
                    None,
                ),
                (("project_id", "product_goal_interview_turn_id"), None),
            ),
            foreign_keys=(
                (("project_id",), "projects", ("project_id",)),
                (
                    ("project_id", "prior_turn_id"),
                    "product_goal_interview_turns",
                    ("project_id", "product_goal_interview_turn_id"),
                ),
                (
                    ("project_id", "vision_artifact_id", "vision_fingerprint"),
                    "vision_artifacts",
                    ("project_id", "vision_artifact_id", "content_fingerprint"),
                ),
                (
                    ("project_id", "workflow_node_attempt_id"),
                    "workflow_node_attempts",
                    ("project_id", "workflow_node_attempt_id"),
                ),
            ),
        ),
        "product_goal_outcomes": _structure(
            columns=(
                ("product_goal_outcome_id", False),
                ("project_id", False),
                ("product_goal_artifact_id", False),
                ("artifact_fingerprint", False),
                ("outcome", False),
                ("rationale", False),
                ("decided_by", False),
                ("idempotency_key", False),
                ("decided_at", False),
            ),
            uniques=(
                (("project_id", "idempotency_key"), None),
                (("project_id", "product_goal_artifact_id"), None),
            ),
            foreign_keys=(
                (("project_id",), "projects", ("project_id",)),
                (
                    ("project_id", "product_goal_artifact_id", "artifact_fingerprint"),
                    "product_goal_artifacts",
                    ("project_id", "product_goal_artifact_id", "content_fingerprint"),
                ),
            ),
            checks=("outcome in ('fulfilled', 'abandoned')",),
        ),
        "project_personas": _structure(
            columns=(
                ("persona_id", False),
                ("project_id", False),
                ("persona_name", False),
                ("is_default", False),
                ("category", False),
                ("description", True),
                ("created_at", False),
            ),
            uniques=((("project_id", "persona_name"), None),),
            foreign_keys=((("project_id",), "projects", ("project_id",)),),
        ),
        "project_teams": _structure(
            columns=(("project_id", False), ("team_id", False)),
            foreign_keys=(
                (("project_id",), "projects", ("project_id",)),
                (("team_id",), "teams", ("team_id",)),
            ),
        ),
        "projects": _structure(
            columns=(
                ("project_id", False),
                ("name", False),
                ("description", True),
                ("active_repository_binding_id", True),
                ("created_at", False),
                ("updated_at", False),
            ),
            uniques=((("name",), None),),
            foreign_keys=(
                (
                    ("active_repository_binding_id",),
                    "repository_bindings",
                    ("repository_binding_id",),
                ),
            ),
        ),
        "repository_bindings": _structure(
            columns=(
                ("repository_binding_id", False),
                ("project_id", False),
                ("worktree_path", False),
                ("common_git_dir", False),
                ("head_sha", False),
                ("branch_name", True),
                ("detached_head", False),
                ("dirty", False),
                ("status_fingerprint", False),
                ("status_entries_json", False),
                ("remotes_json", False),
                ("warnings_json", False),
                ("probe_version", False),
                ("inspected_at", False),
                ("supersedes_repository_binding_id", True),
                ("recorded_by", False),
            ),
            uniques=(
                (("project_id", "repository_binding_id"), None),
                (("project_id", "status_fingerprint", "inspected_at"), None),
            ),
            foreign_keys=(
                (("project_id",), "projects", ("project_id",)),
                (
                    ("project_id", "supersedes_repository_binding_id"),
                    "repository_bindings",
                    ("project_id", "repository_binding_id"),
                ),
            ),
        ),
        "roadmap_artifact_decisions": _structure(
            columns=(
                ("roadmap_artifact_decision_id", False),
                ("project_id", False),
                ("roadmap_artifact_id", False),
                ("artifact_fingerprint", False),
                ("decision", False),
                ("rationale", False),
                ("reviewer", False),
                ("idempotency_key", False),
                ("decided_at", False),
            ),
            uniques=((("project_id", "roadmap_artifact_id"), None),),
            foreign_keys=(
                (
                    ("project_id", "roadmap_artifact_id", "artifact_fingerprint"),
                    "roadmap_artifacts",
                    ("project_id", "roadmap_artifact_id", "content_fingerprint"),
                ),
            ),
            checks=("decision in ('accepted', 'rejected', 'feedback')",),
        ),
        "specification_candidates": _structure(
            columns=(
                ("specification_candidate_id", False),
                ("project_id", False),
                ("candidate_kind", False),
                ("specification_source_id", False),
                ("specification_source_fingerprint", False),
                ("vision_artifact_id", False),
                ("vision_fingerprint", False),
                ("product_goal_artifact_id", False),
                ("product_goal_fingerprint", False),
                ("base_spec_version_id", True),
                ("base_spec_hash", True),
                ("canonical_envelope_json", False),
                ("payload_fingerprint", False),
                ("source_manifest_fingerprint", False),
                ("producer_input_fingerprint", False),
                ("rendered_view_fingerprint", False),
                ("candidate_fingerprint", False),
                ("workflow_node_attempt_id", False),
                ("attempt_fingerprint", False),
                ("supersedes_specification_candidate_id", True),
                ("supersedes_candidate_fingerprint", True),
                ("recorded_by", False),
                ("recorded_at", False),
            ),
            uniques=(
                (
                    (
                        "project_id",
                        "specification_candidate_id",
                        "candidate_fingerprint",
                    ),
                    None,
                ),
                (
                    (
                        "project_id",
                        "specification_candidate_id",
                        "candidate_fingerprint",
                        "payload_fingerprint",
                    ),
                    None,
                ),
                (("project_id", "supersedes_specification_candidate_id"), None),
                (("project_id", "workflow_node_attempt_id"), None),
            ),
            foreign_keys=(
                (("project_id",), "projects", ("project_id",)),
                (
                    ("project_id", "base_spec_version_id", "base_spec_hash"),
                    "spec_registry",
                    ("project_id", "spec_version_id", "spec_hash"),
                ),
                (
                    (
                        "project_id",
                        "product_goal_artifact_id",
                        "product_goal_fingerprint",
                    ),
                    "product_goal_artifacts",
                    ("project_id", "product_goal_artifact_id", "content_fingerprint"),
                ),
                (
                    (
                        "project_id",
                        "specification_source_id",
                        "specification_source_fingerprint",
                    ),
                    "specification_sources",
                    ("project_id", "specification_source_id", "source_fingerprint"),
                ),
                (
                    (
                        "project_id",
                        "supersedes_specification_candidate_id",
                        "supersedes_candidate_fingerprint",
                    ),
                    "specification_candidates",
                    (
                        "project_id",
                        "specification_candidate_id",
                        "candidate_fingerprint",
                    ),
                ),
                (
                    ("project_id", "vision_artifact_id", "vision_fingerprint"),
                    "vision_artifacts",
                    ("project_id", "vision_artifact_id", "content_fingerprint"),
                ),
                (
                    ("project_id", "workflow_node_attempt_id", "attempt_fingerprint"),
                    "workflow_node_attempts",
                    ("project_id", "workflow_node_attempt_id", "attempt_fingerprint"),
                ),
            ),
            checks=(
                "(candidate_kind = 'initial' and base_spec_version_id is null "
                "and base_spec_hash is null) or (candidate_kind = 'amendment' "
                "and base_spec_version_id is not null and base_spec_hash is not null)",
                "candidate_kind in ('initial', 'amendment')",
                "(supersedes_specification_candidate_id is null and "
                "supersedes_candidate_fingerprint is null) or "
                "(supersedes_specification_candidate_id is not null and "
                "supersedes_candidate_fingerprint is not null)",
            ),
        ),
        "specification_sources": _structure(
            columns=(
                ("specification_source_id", False),
                ("project_id", False),
                ("source_bundle_json", False),
                ("source_fingerprint", False),
                ("repository_binding_id", False),
                ("repository_head_sha", False),
                ("repository_dirty", False),
                ("repository_status_fingerprint", False),
                ("vision_artifact_id", False),
                ("vision_fingerprint", False),
                ("product_goal_artifact_id", False),
                ("product_goal_fingerprint", False),
                ("supersedes_specification_source_id", True),
                ("supersedes_source_fingerprint", True),
                ("registered_by", False),
                ("registered_at", False),
            ),
            uniques=(
                (("project_id", "specification_source_id", "source_fingerprint"), None),
                (("project_id", "supersedes_specification_source_id"), None),
            ),
            foreign_keys=(
                (("project_id",), "projects", ("project_id",)),
                (
                    (
                        "project_id",
                        "product_goal_artifact_id",
                        "product_goal_fingerprint",
                    ),
                    "product_goal_artifacts",
                    ("project_id", "product_goal_artifact_id", "content_fingerprint"),
                ),
                (
                    ("project_id", "repository_binding_id"),
                    "repository_bindings",
                    ("project_id", "repository_binding_id"),
                ),
                (
                    (
                        "project_id",
                        "supersedes_specification_source_id",
                        "supersedes_source_fingerprint",
                    ),
                    "specification_sources",
                    ("project_id", "specification_source_id", "source_fingerprint"),
                ),
                (
                    ("project_id", "vision_artifact_id", "vision_fingerprint"),
                    "vision_artifacts",
                    ("project_id", "vision_artifact_id", "content_fingerprint"),
                ),
            ),
            checks=(
                "(supersedes_specification_source_id is null and "
                "supersedes_source_fingerprint is null) or "
                "(supersedes_specification_source_id is not null and "
                "supersedes_source_fingerprint is not null)",
            ),
        ),
        "sprint_closures": _structure(
            columns=(
                ("sprint_closure_id", False),
                ("project_id", False),
                ("sprint_id", False),
                ("review_fingerprint", False),
                ("close_fingerprint", False),
                ("closed_by", False),
                ("closed_at", False),
            ),
            uniques=((("sprint_id",), None),),
            foreign_keys=(
                (("project_id",), "projects", ("project_id",)),
                (("sprint_id",), "sprints", ("sprint_id",)),
            ),
        ),
        "sprint_reviews": _structure(
            columns=(
                ("sprint_review_id", False),
                ("project_id", False),
                ("sprint_id", False),
                ("review_fingerprint", False),
                ("reviewed_by", False),
                ("reviewed_at", False),
            ),
            uniques=((("sprint_id",), None),),
            foreign_keys=(
                (("project_id",), "projects", ("project_id",)),
                (("sprint_id",), "sprints", ("sprint_id",)),
            ),
        ),
        "sprint_starts": _structure(
            columns=(
                ("sprint_start_id", False),
                ("project_id", False),
                ("sprint_id", False),
                ("sprint_plan_artifact_id", False),
                ("sprint_plan_artifact_decision_id", False),
                ("story_dependency_review_id", False),
                ("plan_fingerprint", False),
                ("candidate_set_fingerprint", False),
                ("selected_story_ids_json", False),
                ("task_content_fingerprint", False),
                ("dependency_source_fingerprint", False),
                ("dependency_fingerprint", False),
                ("dependency_rows_fingerprint", False),
                ("decision_fingerprint", False),
                ("audit_event_id", False),
                ("started_by", False),
                ("started_at", False),
            ),
            uniques=((("audit_event_id",), None), (("sprint_id",), None)),
            foreign_keys=(
                (("audit_event_id",), "workflow_events", ("event_id",)),
                (("project_id",), "projects", ("project_id",)),
                (
                    (
                        "project_id",
                        "sprint_plan_artifact_id",
                        "sprint_plan_artifact_decision_id",
                    ),
                    "sprint_plan_artifact_decisions",
                    (
                        "project_id",
                        "sprint_plan_artifact_id",
                        "sprint_plan_artifact_decision_id",
                    ),
                ),
                (("sprint_id",), "sprints", ("sprint_id",)),
                (
                    ("story_dependency_review_id",),
                    "story_dependency_reviews",
                    ("story_dependency_review_id",),
                ),
            ),
        ),
        "sprint_stories": _structure(
            columns=(("sprint_id", False), ("story_id", False), ("added_at", False)),
            foreign_keys=(
                (("sprint_id",), "sprints", ("sprint_id",)),
                (("story_id",), "user_stories", ("story_id",)),
            ),
        ),
        "sprints": _structure(
            columns=(
                ("sprint_id", False),
                ("goal", True),
                ("start_date", True),
                ("end_date", True),
                ("status", False),
                ("started_at", True),
                ("completed_at", True),
                ("close_snapshot_json", True),
                ("created_at", False),
                ("updated_at", False),
                ("project_id", False),
                ("team_id", False),
            ),
            foreign_keys=(
                (("project_id",), "projects", ("project_id",)),
                (("team_id",), "teams", ("team_id",)),
            ),
        ),
        "story_artifact_decisions": _structure(
            columns=(
                ("story_artifact_decision_id", False),
                ("project_id", False),
                ("story_artifact_id", False),
                ("artifact_fingerprint", False),
                ("decision", False),
                ("rationale", False),
                ("reviewer", False),
                ("idempotency_key", False),
                ("decided_at", False),
            ),
            uniques=((("project_id", "story_artifact_id"), None),),
            foreign_keys=(
                (
                    ("project_id", "story_artifact_id", "artifact_fingerprint"),
                    "story_artifacts",
                    ("project_id", "story_artifact_id", "content_fingerprint"),
                ),
            ),
            checks=("decision in ('accepted', 'rejected', 'feedback')",),
        ),
        "story_closures": _structure(
            columns=(
                ("story_closure_id", False),
                ("project_id", False),
                ("sprint_id", False),
                ("story_id", False),
                ("completion_fingerprint", False),
                ("resolution", False),
                ("delivered", False),
                ("evidence", False),
                ("known_gaps", False),
                ("closed_by", False),
                ("closed_at", False),
            ),
            uniques=((("story_id", "sprint_id"), None),),
            foreign_keys=(
                (("project_id",), "projects", ("project_id",)),
                (("sprint_id",), "sprints", ("sprint_id",)),
                (("story_id",), "user_stories", ("story_id",)),
            ),
        ),
        "story_completion_logs": _structure(
            columns=(
                ("log_id", False),
                ("story_id", False),
                ("old_status", False),
                ("new_status", False),
                ("resolution", True),
                ("delivered", True),
                ("evidence", True),
                ("known_gaps", True),
                ("follow_ups_created", True),
                ("changed_by", True),
                ("changed_at", False),
            ),
            foreign_keys=((("story_id",), "user_stories", ("story_id",)),),
        ),
        "story_dependency_reviews": _structure(
            columns=(
                ("story_dependency_review_id", False),
                ("project_id", False),
                ("selected_story_ids_json", False),
                ("reviewed_edges_json", False),
                ("source_fingerprint", False),
                ("dependency_fingerprint", False),
                ("reviewed_by", False),
                ("reviewed_at", False),
            ),
            uniques=((("project_id", "source_fingerprint"), None),),
            foreign_keys=((("project_id",), "projects", ("project_id",)),),
        ),
        "task_completion_evidence": _structure(
            columns=(
                ("task_completion_evidence_id", False),
                ("project_id", False),
                ("sprint_id", False),
                ("task_id", False),
                ("outcome_summary", False),
                ("artifact_refs_json", False),
                ("acceptance_result", False),
                ("checklist_result_json", False),
                ("evidence_fingerprint", False),
                ("completed_by", False),
                ("completed_at", False),
            ),
            uniques=((("task_id", "sprint_id"), None),),
            foreign_keys=(
                (("project_id",), "projects", ("project_id",)),
                (("sprint_id",), "sprints", ("sprint_id",)),
                (("task_id",), "tasks", ("task_id",)),
            ),
            checks=("acceptance_result in ('partially_met', 'fully_met')",),
        ),
        "task_execution_logs": _structure(
            columns=(
                ("log_id", False),
                ("old_status", True),
                ("new_status", False),
                ("outcome_summary", True),
                ("artifact_refs_json", True),
                ("acceptance_result", False),
                ("notes", True),
                ("changed_by", False),
                ("changed_at", False),
                ("task_id", False),
                ("sprint_id", False),
            ),
            foreign_keys=(
                (("sprint_id",), "sprints", ("sprint_id",)),
                (("task_id",), "tasks", ("task_id",)),
            ),
        ),
        "team_members": _structure(
            columns=(
                ("member_id", False),
                ("name", False),
                ("email", False),
                ("created_at", False),
                ("updated_at", False),
            ),
            uniques=((("email",), None),),
        ),
        "team_memberships": _structure(
            columns=(("team_id", False), ("member_id", False), ("role", False)),
            foreign_keys=(
                (("member_id",), "team_members", ("member_id",)),
                (("team_id",), "teams", ("team_id",)),
            ),
        ),
        "teams": _structure(
            columns=(
                ("team_id", False),
                ("name", False),
                ("created_at", False),
                ("updated_at", False),
            ),
            uniques=((("name",), None),),
        ),
        "themes": _structure(
            columns=(
                ("theme_id", False),
                ("title", False),
                ("description", True),
                ("time_frame", True),
                ("created_at", False),
                ("updated_at", False),
                ("project_id", False),
            ),
            uniques=((("project_id", "title"), None),),
            foreign_keys=((("project_id",), "projects", ("project_id",)),),
        ),
        "user_story_dependencies": _structure(
            columns=(
                ("dependency_id", False),
                ("project_id", False),
                ("dependent_story_id", False),
                ("prerequisite_story_id", False),
                ("status", False),
                ("source", False),
                ("confidence", False),
                ("reason", True),
                ("created_at", False),
                ("updated_at", False),
            ),
            uniques=(
                (("project_id", "dependent_story_id", "prerequisite_story_id"), None),
            ),
            foreign_keys=(
                (("dependent_story_id",), "user_stories", ("story_id",)),
                (("prerequisite_story_id",), "user_stories", ("story_id",)),
                (("project_id",), "projects", ("project_id",)),
            ),
            checks=(
                "confidence in ('explicit', 'inferred', 'reviewed')",
                "dependent_story_id <> prerequisite_story_id",
                "source in ('story_writer', 'dependency_repair', 'manual_review')",
                "status in ('proposed', 'active', 'rejected')",
            ),
        ),
        "vision_artifact_decisions": _structure(
            columns=(
                ("vision_artifact_decision_id", False),
                ("project_id", False),
                ("vision_artifact_id", False),
                ("artifact_fingerprint", False),
                ("decision", False),
                ("rationale", False),
                ("reviewer", False),
                ("idempotency_key", False),
                ("decided_at", False),
            ),
            uniques=(
                (("project_id", "idempotency_key"), None),
                (("project_id", "vision_artifact_id"), None),
            ),
            foreign_keys=(
                (
                    ("project_id", "vision_artifact_id", "artifact_fingerprint"),
                    "vision_artifacts",
                    ("project_id", "vision_artifact_id", "content_fingerprint"),
                ),
            ),
            checks=("decision in ('accepted', 'rejected', 'feedback')",),
        ),
        "vision_artifacts": _structure(
            columns=(
                ("vision_artifact_id", False),
                ("project_id", False),
                ("version_number", False),
                ("components_json", False),
                ("statement", False),
                ("content_fingerprint", False),
                ("vision_evidence_snapshot_id", False),
                ("component_basis_json", False),
                ("assumptions_json", False),
                ("conflicts_json", False),
                ("supersedes_vision_artifact_id", True),
                ("source_interview_turn_id", False),
                ("created_by", False),
                ("created_at", False),
            ),
            uniques=(
                (("project_id", "content_fingerprint"), None),
                (("project_id", "version_number"), None),
                (("project_id", "vision_artifact_id"), None),
                (("project_id", "vision_artifact_id", "content_fingerprint"), None),
            ),
            foreign_keys=(
                (("project_id",), "projects", ("project_id",)),
                (
                    ("project_id", "source_interview_turn_id"),
                    "vision_interview_turns",
                    ("project_id", "vision_interview_turn_id"),
                ),
                (
                    ("project_id", "supersedes_vision_artifact_id"),
                    "vision_artifacts",
                    ("project_id", "vision_artifact_id"),
                ),
                (
                    ("project_id", "vision_evidence_snapshot_id"),
                    "vision_evidence_snapshots",
                    ("project_id", "vision_evidence_snapshot_id"),
                ),
            ),
        ),
        "vision_evidence_snapshots": _structure(
            columns=(
                ("vision_evidence_snapshot_id", False),
                ("project_id", False),
                ("repository_binding_id", True),
                ("supersedes_vision_evidence_snapshot_id", True),
                ("workflow_node_attempt_id", False),
                ("evidence_json", False),
                ("evidence_fingerprint", False),
                ("warnings_json", False),
                ("created_at", False),
            ),
            uniques=((("project_id", "vision_evidence_snapshot_id"), None),),
            foreign_keys=(
                (("project_id",), "projects", ("project_id",)),
                (
                    ("project_id", "repository_binding_id"),
                    "repository_bindings",
                    ("project_id", "repository_binding_id"),
                ),
                (
                    ("project_id", "supersedes_vision_evidence_snapshot_id"),
                    "vision_evidence_snapshots",
                    ("project_id", "vision_evidence_snapshot_id"),
                ),
                (
                    ("project_id", "workflow_node_attempt_id"),
                    "workflow_node_attempts",
                    ("project_id", "workflow_node_attempt_id"),
                ),
            ),
        ),
        "vision_interview_turns": _structure(
            columns=(
                ("vision_interview_turn_id", False),
                ("project_id", False),
                ("operation", False),
                ("turn_number", False),
                ("revision_intent_id", True),
                ("vision_evidence_snapshot_id", False),
                ("prior_turn_id", True),
                ("user_text", True),
                ("components_json", False),
                ("vision_statement", False),
                ("is_complete", False),
                ("clarifying_questions_json", False),
                ("component_basis_json", False),
                ("assumptions_json", False),
                ("conflicts_json", False),
                ("output_fingerprint", False),
                ("workflow_node_attempt_id", False),
                ("attempt_fingerprint", False),
                ("recorded_at", False),
            ),
            uniques=(
                (("project_id", "vision_evidence_snapshot_id", "turn_number"), None),
                (("project_id", "vision_interview_turn_id"), None),
            ),
            foreign_keys=(
                (("project_id",), "projects", ("project_id",)),
                (
                    ("project_id", "prior_turn_id"),
                    "vision_interview_turns",
                    ("project_id", "vision_interview_turn_id"),
                ),
                (
                    ("project_id", "revision_intent_id"),
                    "vision_revision_intents",
                    ("project_id", "vision_revision_intent_id"),
                ),
                (
                    ("project_id", "vision_evidence_snapshot_id"),
                    "vision_evidence_snapshots",
                    ("project_id", "vision_evidence_snapshot_id"),
                ),
                (
                    ("project_id", "workflow_node_attempt_id"),
                    "workflow_node_attempts",
                    ("project_id", "workflow_node_attempt_id"),
                ),
            ),
            checks=(
                "(operation = 'bootstrap' and user_text is null) or "
                "(operation in ('clarification', 'revision') and "
                "user_text is not null)",
                "operation in ('bootstrap', 'clarification', 'revision')",
            ),
        ),
        "vision_revision_intents": _structure(
            columns=(
                ("vision_revision_intent_id", False),
                ("project_id", False),
                ("source_vision_artifact_id", False),
                ("source_vision_fingerprint", False),
                ("reason", False),
                ("initiated_by", False),
                ("initiated_at", False),
            ),
            uniques=((("project_id", "vision_revision_intent_id"), None),),
            foreign_keys=(
                (("project_id",), "projects", ("project_id",)),
                (
                    (
                        "project_id",
                        "source_vision_artifact_id",
                        "source_vision_fingerprint",
                    ),
                    "vision_artifacts",
                    ("project_id", "vision_artifact_id", "content_fingerprint"),
                ),
            ),
        ),
        "workflow_events": _structure(
            columns=(
                ("event_id", False),
                ("event_type", False),
                ("timestamp", False),
                ("duration_seconds", True),
                ("turn_count", True),
                ("project_id", True),
                ("sprint_id", True),
                ("event_metadata", True),
            ),
            foreign_keys=(
                (("project_id",), "projects", ("project_id",)),
                (("sprint_id",), "sprints", ("sprint_id",)),
            ),
        ),
        "workflow_node_attempt_outcomes": _structure(
            columns=(
                ("workflow_node_attempt_outcome_id", False),
                ("project_id", False),
                ("workflow_node_attempt_id", False),
                ("status", False),
                ("output_fingerprint", True),
                ("output_json", True),
                ("failure_code", True),
                ("failure_message", True),
                ("recorded_at", False),
            ),
            uniques=((("workflow_node_attempt_id",), None),),
            foreign_keys=(
                (
                    ("project_id", "workflow_node_attempt_id"),
                    "workflow_node_attempts",
                    ("project_id", "workflow_node_attempt_id"),
                ),
            ),
            checks=(
                "(status = 'success' and output_fingerprint is not null and "
                "output_json is not null and failure_code is null and "
                "failure_message is null) or (status = 'failure' and "
                "output_fingerprint is null and output_json is null and "
                "failure_code is not null and failure_message is not null) or "
                "(status = 'obsolete' and output_fingerprint is null and "
                "output_json is null and failure_code is null and "
                "failure_message is null)",
                "status in ('success', 'failure', 'obsolete')",
            ),
        ),
        "workflow_node_attempts": _structure(
            columns=(
                ("workflow_node_attempt_id", False),
                ("project_id", False),
                ("node_id", False),
                ("instance_key", True),
                ("graph_version", False),
                ("fact_fingerprint", False),
                ("business_fact_fingerprint", False),
                ("decision_fingerprint", False),
                ("normalized_input_json", False),
                ("input_fingerprint", False),
                ("model_id", False),
                ("execution_settings_json", False),
                ("idempotency_key", False),
                ("actor", False),
                ("correlation_id", True),
                ("started_at", False),
                ("lease_expires_at", False),
                ("attempt_fingerprint", False),
            ),
            uniques=(
                (("project_id", "workflow_node_attempt_id"), None),
                (
                    ("project_id", "workflow_node_attempt_id", "attempt_fingerprint"),
                    None,
                ),
            ),
            foreign_keys=((("project_id",), "projects", ("project_id",)),),
            checks=("lease_expires_at > started_at",),
        ),
        "workflow_transition_receipts": _structure(
            columns=(
                ("workflow_transition_receipt_id", False),
                ("request_kind", False),
                ("idempotency_key", False),
                ("request_fingerprint", False),
                ("request_json", False),
                ("result_json", True),
                ("started_at", False),
                ("completed_at", True),
            ),
            uniques=((("request_kind", "idempotency_key"), None),),
        ),
        "specification_decisions": _structure(
            columns=(
                ("specification_decision_id", False),
                ("project_id", False),
                ("specification_candidate_id", False),
                ("candidate_fingerprint", False),
                ("decision", False),
                ("rationale", False),
                ("reviewer", False),
                ("idempotency_key", False),
                ("decided_at", False),
            ),
            uniques=(
                (("project_id", "idempotency_key"), None),
                (("project_id", "specification_candidate_id"), None),
                (
                    (
                        "project_id",
                        "specification_decision_id",
                        "specification_candidate_id",
                        "candidate_fingerprint",
                    ),
                    None,
                ),
            ),
            foreign_keys=(
                (("project_id",), "projects", ("project_id",)),
                (
                    (
                        "project_id",
                        "specification_candidate_id",
                        "candidate_fingerprint",
                    ),
                    "specification_candidates",
                    (
                        "project_id",
                        "specification_candidate_id",
                        "candidate_fingerprint",
                    ),
                ),
            ),
            checks=("decision IN ('accepted', 'rejected', 'feedback')",),
        ),
        "spec_registry": _structure(
            columns=(
                ("spec_version_id", False),
                ("project_id", False),
                ("spec_hash", False),
                ("status", False),
                ("created_at", False),
                ("source_specification_decision_id", False),
                ("source_specification_candidate_id", False),
                ("source_specification_candidate_fingerprint", False),
                ("source_vision_artifact_id", False),
                ("source_vision_fingerprint", False),
                ("source_product_goal_artifact_id", False),
                ("source_product_goal_fingerprint", False),
                ("supersedes_spec_version_id", True),
            ),
            uniques=(
                (("project_id", "spec_version_id"), None),
                (("project_id", "spec_version_id", "spec_hash"), None),
                (("project_id", "source_specification_candidate_id"), None),
                (
                    (
                        "project_id",
                        "source_specification_candidate_id",
                        "source_specification_candidate_fingerprint",
                        "spec_hash",
                    ),
                    None,
                ),
                (("project_id",), "status = 'approved'"),
            ),
            foreign_keys=(
                (("project_id",), "projects", ("project_id",)),
                (
                    ("supersedes_spec_version_id",),
                    "spec_registry",
                    ("spec_version_id",),
                ),
                (
                    (
                        "project_id",
                        "source_specification_candidate_id",
                        "source_specification_candidate_fingerprint",
                        "spec_hash",
                    ),
                    "specification_candidates",
                    (
                        "project_id",
                        "specification_candidate_id",
                        "candidate_fingerprint",
                        "payload_fingerprint",
                    ),
                ),
                (
                    (
                        "project_id",
                        "source_specification_decision_id",
                        "source_specification_candidate_id",
                        "source_specification_candidate_fingerprint",
                    ),
                    "specification_decisions",
                    (
                        "project_id",
                        "specification_decision_id",
                        "specification_candidate_id",
                        "candidate_fingerprint",
                    ),
                ),
                (
                    (
                        "project_id",
                        "source_vision_artifact_id",
                        "source_vision_fingerprint",
                    ),
                    "vision_artifacts",
                    ("project_id", "vision_artifact_id", "content_fingerprint"),
                ),
                (
                    (
                        "project_id",
                        "source_product_goal_artifact_id",
                        "source_product_goal_fingerprint",
                    ),
                    "product_goal_artifacts",
                    ("project_id", "product_goal_artifact_id", "content_fingerprint"),
                ),
            ),
            checks=("status IN ('approved', 'superseded')",),
        ),
        "backlog_artifacts": _structure(
            columns=(
                ("backlog_artifact_id", False),
                ("project_id", False),
                ("spec_version_id", False),
                ("spec_hash", False),
                ("product_goal_artifact_id", False),
                ("product_goal_fingerprint", False),
                ("version_number", False),
                ("canonical_content_json", False),
                ("content_fingerprint", False),
                ("supersedes_backlog_artifact_id", True),
                ("created_by", False),
                ("created_at", False),
            ),
            uniques=(
                (("project_id", "backlog_artifact_id"), None),
                (("project_id", "backlog_artifact_id", "content_fingerprint"), None),
                (
                    (
                        "project_id",
                        "product_goal_artifact_id",
                        "product_goal_fingerprint",
                        "spec_version_id",
                        "spec_hash",
                        "version_number",
                    ),
                    None,
                ),
                (
                    (
                        "project_id",
                        "product_goal_artifact_id",
                        "product_goal_fingerprint",
                        "spec_version_id",
                        "spec_hash",
                        "content_fingerprint",
                    ),
                    None,
                ),
            ),
            foreign_keys=(
                (("project_id",), "projects", ("project_id",)),
                (
                    ("project_id", "spec_version_id", "spec_hash"),
                    "spec_registry",
                    ("project_id", "spec_version_id", "spec_hash"),
                ),
                (
                    (
                        "project_id",
                        "product_goal_artifact_id",
                        "product_goal_fingerprint",
                    ),
                    "product_goal_artifacts",
                    ("project_id", "product_goal_artifact_id", "content_fingerprint"),
                ),
                (
                    ("project_id", "supersedes_backlog_artifact_id"),
                    "backlog_artifacts",
                    ("project_id", "backlog_artifact_id"),
                ),
            ),
        ),
        "roadmap_artifacts": _structure(
            columns=(
                ("roadmap_artifact_id", False),
                ("project_id", False),
                ("backlog_artifact_id", False),
                ("backlog_artifact_fingerprint", False),
                ("version_number", False),
                ("canonical_content_json", False),
                ("content_fingerprint", False),
                ("supersedes_roadmap_artifact_id", True),
                ("created_by", False),
                ("created_at", False),
            ),
            uniques=(
                (("project_id", "roadmap_artifact_id"), None),
                (("project_id", "roadmap_artifact_id", "content_fingerprint"), None),
                (
                    (
                        "project_id",
                        "backlog_artifact_id",
                        "backlog_artifact_fingerprint",
                        "version_number",
                    ),
                    None,
                ),
                (
                    (
                        "project_id",
                        "backlog_artifact_id",
                        "backlog_artifact_fingerprint",
                        "content_fingerprint",
                    ),
                    None,
                ),
            ),
            foreign_keys=(
                (("project_id",), "projects", ("project_id",)),
                (
                    (
                        "project_id",
                        "backlog_artifact_id",
                        "backlog_artifact_fingerprint",
                    ),
                    "backlog_artifacts",
                    ("project_id", "backlog_artifact_id", "content_fingerprint"),
                ),
                (
                    ("project_id", "supersedes_roadmap_artifact_id"),
                    "roadmap_artifacts",
                    ("project_id", "roadmap_artifact_id"),
                ),
            ),
        ),
        "story_artifacts": _structure(
            columns=(
                ("story_artifact_id", False),
                ("project_id", False),
                ("source_backlog_artifact_id", False),
                ("source_backlog_artifact_fingerprint", False),
                ("backlog_item_id", False),
                ("roadmap_artifact_id", False),
                ("roadmap_artifact_fingerprint", False),
                ("version_number", False),
                ("canonical_content_json", False),
                ("content_fingerprint", False),
                ("story_item_ids_json", False),
                ("supersedes_story_artifact_id", True),
                ("created_by", False),
                ("created_at", False),
            ),
            uniques=(
                (("project_id", "story_artifact_id"), None),
                (("project_id", "story_artifact_id", "content_fingerprint"), None),
                (
                    (
                        "project_id",
                        "source_backlog_artifact_id",
                        "backlog_item_id",
                        "version_number",
                    ),
                    None,
                ),
                (
                    (
                        "project_id",
                        "source_backlog_artifact_id",
                        "backlog_item_id",
                        "content_fingerprint",
                    ),
                    None,
                ),
            ),
            foreign_keys=(
                (("project_id",), "projects", ("project_id",)),
                (
                    (
                        "project_id",
                        "source_backlog_artifact_id",
                        "source_backlog_artifact_fingerprint",
                    ),
                    "backlog_artifacts",
                    ("project_id", "backlog_artifact_id", "content_fingerprint"),
                ),
                (
                    (
                        "project_id",
                        "roadmap_artifact_id",
                        "roadmap_artifact_fingerprint",
                    ),
                    "roadmap_artifacts",
                    ("project_id", "roadmap_artifact_id", "content_fingerprint"),
                ),
                (
                    ("project_id", "supersedes_story_artifact_id"),
                    "story_artifacts",
                    ("project_id", "story_artifact_id"),
                ),
            ),
        ),
        "user_stories": _structure(
            columns=(
                ("story_id", False),
                ("project_id", False),
                ("source_story_artifact_id", False),
                ("source_story_artifact_fingerprint", False),
                ("source_story_item_id", False),
                ("source_story_item_fingerprint", False),
                ("accepted_spec_version_id", False),
                ("accepted_spec_hash", False),
                ("spec_item_ids_json", False),
                ("title", False),
                ("story_description", False),
                ("acceptance_criteria_json", False),
                ("persona", False),
                ("status", False),
                ("story_points", True),
                ("rank", True),
                ("is_superseded", False),
                ("resolution", True),
                ("completion_notes", True),
                ("evidence_links", True),
                ("completed_at", True),
                ("validation_evidence", True),
                ("created_at", False),
                ("updated_at", False),
            ),
            uniques=(
                (
                    ("project_id", "source_story_artifact_id", "source_story_item_id"),
                    None,
                ),
            ),
            foreign_keys=(
                (("project_id",), "projects", ("project_id",)),
                (
                    ("project_id", "accepted_spec_version_id", "accepted_spec_hash"),
                    "spec_registry",
                    ("project_id", "spec_version_id", "spec_hash"),
                ),
                (
                    (
                        "project_id",
                        "source_story_artifact_id",
                        "source_story_artifact_fingerprint",
                    ),
                    "story_artifacts",
                    ("project_id", "story_artifact_id", "content_fingerprint"),
                ),
            ),
        ),
        "sprint_plan_artifacts": _structure(
            columns=(
                ("sprint_plan_artifact_id", False),
                ("project_id", False),
                ("spec_version_id", False),
                ("spec_hash", False),
                ("sprint_plan_stream_id", False),
                ("version_number", False),
                ("selected_story_ids_json", False),
                ("canonical_task_plan_json", False),
                ("plan_fingerprint", False),
                ("candidate_set_fingerprint", False),
                ("supersedes_sprint_plan_artifact_id", True),
                ("created_by", False),
                ("created_at", False),
            ),
            uniques=(
                (("project_id", "sprint_plan_artifact_id"), None),
                (("project_id", "sprint_plan_artifact_id", "plan_fingerprint"), None),
                (
                    (
                        "project_id",
                        "spec_version_id",
                        "spec_hash",
                        "sprint_plan_stream_id",
                        "version_number",
                    ),
                    None,
                ),
                (
                    (
                        "project_id",
                        "spec_version_id",
                        "spec_hash",
                        "sprint_plan_stream_id",
                        "plan_fingerprint",
                    ),
                    None,
                ),
            ),
            foreign_keys=(
                (("project_id",), "projects", ("project_id",)),
                (
                    ("project_id", "spec_version_id", "spec_hash"),
                    "spec_registry",
                    ("project_id", "spec_version_id", "spec_hash"),
                ),
                (
                    ("project_id", "supersedes_sprint_plan_artifact_id"),
                    "sprint_plan_artifacts",
                    ("project_id", "sprint_plan_artifact_id"),
                ),
            ),
        ),
        "sprint_plan_artifact_decisions": _structure(
            columns=(
                ("sprint_plan_artifact_decision_id", False),
                ("project_id", False),
                ("sprint_plan_artifact_id", False),
                ("plan_fingerprint", False),
                ("decision", False),
                ("activated_sprint_id", True),
                ("rationale", False),
                ("reviewer", False),
                ("idempotency_key", False),
                ("decided_at", False),
            ),
            uniques=(
                (("project_id", "sprint_plan_artifact_id"), None),
                (
                    (
                        "project_id",
                        "sprint_plan_artifact_id",
                        "sprint_plan_artifact_decision_id",
                    ),
                    None,
                ),
            ),
            foreign_keys=(
                (("activated_sprint_id",), "sprints", ("sprint_id",)),
                (
                    ("project_id", "sprint_plan_artifact_id", "plan_fingerprint"),
                    "sprint_plan_artifacts",
                    ("project_id", "sprint_plan_artifact_id", "plan_fingerprint"),
                ),
            ),
            checks=(
                "decision IN ('accepted', 'rejected', 'feedback')",
                "(decision = 'accepted' AND activated_sprint_id IS NOT NULL) OR "
                "(decision IN ('feedback', 'rejected') AND "
                "activated_sprint_id IS NULL)",
            ),
        ),
        "tasks": _structure(
            columns=(
                ("task_id", False),
                ("description", False),
                ("metadata_json", False),
                ("status", False),
                ("created_at", False),
                ("updated_at", False),
                ("story_id", False),
                ("assigned_to_member_id", True),
            ),
            foreign_keys=(
                (("story_id",), "user_stories", ("story_id",)),
                (("assigned_to_member_id",), "team_members", ("member_id",)),
            ),
        ),
    },
)

_RETIRED_TABLES = frozenset(
    {
        "discovery_artifacts",
        "compiled_spec_authority",
        "spec_authority_acceptance",
        "authority_feedback_attempts",
        "authority_curation_attempts",
    }
)
_RETIRED_COLUMNS = {
    "backlog_artifacts": frozenset({"authority_id", "authority_fingerprint"}),
    "spec_registry": frozenset({"approved_at", "approved_by", "approval_notes"}),
    "story_artifacts": frozenset({"requirement_id", "story_ids_json"}),
    "sprint_plan_artifacts": frozenset({"sprint_id"}),
    "user_stories": frozenset(
        {
            "acceptance_criteria",
            "source_requirement",
            "refinement_slot",
            "story_origin",
            "is_refined",
            "archived_reason",
            "archived_at",
            "archived_by",
            "archive_reset_attempt_id",
            "archive_previous_status",
            "original_acceptance_criteria",
            "ac_updated_at",
            "ac_update_reason",
            "superseded_by_story_id",
        }
    ),
}


class UnsupportedBusinessSchemaError(RuntimeError):
    """Raised when a hard-break database predates the current model contract."""


def _metadata_table_structure(table_name: str) -> TableStructure:
    table = SQLModel.metadata.tables[table_name]
    uniques = {
        (tuple(constraint.columns.keys()), None)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    uniques.update(
        (
            tuple(index.columns.keys()),
            _normalize_sql_expression(index.dialect_options["sqlite"].get("where"))
            if index.dialect_options["sqlite"].get("where") is not None
            else None,
        )
        for index in table.indexes
        if index.unique
    )
    return TableStructure(
        columns=tuple((column.name, bool(column.nullable)) for column in table.columns),
        uniques=frozenset(uniques),
        foreign_keys=frozenset(
            (
                tuple(constraint.column_keys),
                constraint.referred_table.name,
                tuple(element.column.name for element in constraint.elements),
            )
            for constraint in table.constraints
            if isinstance(constraint, ForeignKeyConstraint)
        ),
        checks=frozenset(
            _normalize_sql_expression(constraint.sqltext)
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        ),
    )


def _sqlmodel_business_schema_manifest() -> BusinessSchemaManifest:
    """Normalize fresh SQLModel metadata for comparison to the static manifest."""
    return BusinessSchemaManifest(
        table_names=frozenset(SQLModel.metadata.tables),
        structures={
            table_name: _metadata_table_structure(table_name)
            for table_name in CURRENT_BUSINESS_SCHEMA_MANIFEST.structures
        },
    )


def _inspected_table_structure(
    target_engine: Engine, table_name: str
) -> TableStructure:
    inspector = inspect(target_engine)
    uniques: set[tuple[tuple[str, ...], str | None]] = {
        (tuple(str(column) for column in item["column_names"]), None)
        for item in inspector.get_unique_constraints(table_name)
    }
    for item in inspector.get_indexes(table_name):
        if not item["unique"]:
            continue
        dialect_options = item.get("dialect_options") or {}
        predicate = dialect_options.get("sqlite_where")
        uniques.add(
            (
                tuple(str(column) for column in item["column_names"]),
                _normalize_sql_expression(predicate) if predicate is not None else None,
            )
        )
    return TableStructure(
        columns=tuple(
            (column["name"], bool(column["nullable"]))
            for column in inspector.get_columns(table_name)
        ),
        uniques=frozenset(uniques),
        foreign_keys=frozenset(
            (
                tuple(item["constrained_columns"]),
                item["referred_table"],
                tuple(item["referred_columns"]),
            )
            for item in inspector.get_foreign_keys(table_name)
        ),
        checks=frozenset(
            _normalize_sql_expression(item["sqltext"])
            for item in inspector.get_check_constraints(table_name)
        ),
    )


def _inspect_business_schema_manifest(target_engine: Engine) -> BusinessSchemaManifest:
    """Inspect an existing database using the same normalized manifest shape."""
    table_names = frozenset(inspect(target_engine).get_table_names())
    return BusinessSchemaManifest(
        table_names=table_names,
        structures={
            table_name: _inspected_table_structure(target_engine, table_name)
            for table_name in CURRENT_BUSINESS_SCHEMA_MANIFEST.structures
            if table_name in table_names
        },
    )


def _retired_schema_references(target_engine: Engine) -> tuple[str, ...]:
    """Find retired names in tables, columns, and structural SQL expressions."""
    inspector = inspect(target_engine)
    table_names = frozenset(inspector.get_table_names())
    incompatible = [
        f"retired table {table_name}"
        for table_name in sorted(table_names & _RETIRED_TABLES)
    ]
    for table_name in sorted(table_names):
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        incompatible.extend(
            f"retired column {table_name}.{column_name}"
            for column_name in sorted(
                columns & _RETIRED_COLUMNS.get(table_name, frozenset())
            )
        )
        incompatible.extend(
            f"retired foreign key target {table_name}->{foreign_key['referred_table']}"
            for foreign_key in inspector.get_foreign_keys(table_name)
            if foreign_key["referred_table"] in _RETIRED_TABLES
        )
    return tuple(incompatible)


def _assert_current_business_schema(target_engine: Engine) -> None:
    """Allow only an empty database or the exact reviewed fresh schema."""
    if _sqlmodel_business_schema_manifest() != CURRENT_BUSINESS_SCHEMA_MANIFEST:
        message = (
            "UNSUPPORTED_BUSINESS_SCHEMA: SQLModel metadata does not match the "
            "reviewed issue #210 fresh-schema manifest."
        )
        raise UnsupportedBusinessSchemaError(message)
    table_names = frozenset(inspect(target_engine).get_table_names())
    if not table_names:
        return

    observed = _inspect_business_schema_manifest(target_engine)
    incompatible = list(_retired_schema_references(target_engine))
    missing_tables = sorted(CURRENT_BUSINESS_SCHEMA_MANIFEST.table_names - table_names)
    extra_tables = sorted(table_names - CURRENT_BUSINESS_SCHEMA_MANIFEST.table_names)
    if missing_tables:
        incompatible.append(f"missing tables {', '.join(missing_tables)}")
    if extra_tables:
        incompatible.append(f"unexpected tables {', '.join(extra_tables)}")
    for table_name, expected in CURRENT_BUSINESS_SCHEMA_MANIFEST.structures.items():
        actual = observed.structures.get(table_name)
        if actual != expected:
            incompatible.append(f"structural mismatch {table_name}")

    if incompatible:
        detail = "; ".join(dict.fromkeys(incompatible))
        message = (
            "UNSUPPORTED_BUSINESS_SCHEMA: the database does not match the "
            f"issue #210 fresh schema ({detail}). Create a fresh AgileForge "
            "profile/database; automatic migration is intentionally unsupported."
        )
        raise UnsupportedBusinessSchemaError(message)


def _is_pytest_running() -> bool:
    """Detect if code is running under pytest."""
    return "pytest" in sys.modules or "py.test" in sys.modules


def get_database_url() -> str:
    """Return the configured business database URL."""
    return get_business_db_target().sqlite_url


class _PytestEngineGuardError(RuntimeError):
    """Raised when production DB access is attempted during pytest."""

    def __init__(self) -> None:
        super().__init__(
            "get_engine() called during pytest without ALLOW_PROD_DB_IN_TEST=1. "
            "Tests should use the 'engine' fixture and monkey-patch the module. "
            "Example: monkeypatch.setattr(save_mod, 'engine', test_engine)"
        )


@cache
def _create_production_engine() -> Engine:
    """Create the production database engine."""
    return create_engine(
        get_database_url(),
        echo=get_database_echo(),
        connect_args={"check_same_thread": False},
    )


def get_engine() -> Engine:
    """Return the database engine with test safety guard."""
    if _is_pytest_running() and not os.environ.get("ALLOW_PROD_DB_IN_TEST"):
        raise _PytestEngineGuardError()

    return _create_production_engine()


DB_URL = get_database_url()
engine = create_engine(
    DB_URL,
    echo=get_database_echo(),
    connect_args={"check_same_thread": False},
)


@event.listens_for(Engine, "connect")
def set_sqlite_pragma(
    dbapi_connection: sqlite3.Connection,
    _connection_record: object,
) -> None:
    """Enforce foreign key constraints on SQLite."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def create_db_and_tables() -> None:
    """Create the current database schema."""
    logger.info("Creating tables.")
    ensure_business_db_ready()
    logger.info("Tables created successfully.")


def ensure_business_db_ready(engine_override: Engine | None = None) -> None:
    """Create all current business tables from SQLModel metadata."""
    target_engine = engine_override or engine
    _assert_current_business_schema(target_engine)
    SQLModel.metadata.create_all(target_engine)
