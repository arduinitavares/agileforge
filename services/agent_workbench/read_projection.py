"""Read-only project projections for the agent workbench."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from json import JSONDecodeError
from typing import TYPE_CHECKING, Any, Final, cast

from pydantic import ValidationError
from sqlalchemy import func
from sqlmodel import Session, select

from models import db as model_db
from models.core import Epic, Feature, Product, Sprint, SprintStory, Theme, UserStory
from models.enums import SprintStatus, StoryStatus
from models.specs import CompiledSpecAuthority, SpecRegistry
from services.agent_workbench.envelope import error_envelope
from services.agent_workbench.error_codes import ErrorCode, workbench_error
from services.agent_workbench.fingerprints import canonical_hash
from services.agent_workbench.schema_readiness import (
    SchemaReadiness,
    SchemaRequirement,
    check_schema_readiness,
)
from services.agent_workbench.session_reader import ReadOnlySessionReader
from services.orchestrator_query_service import fetch_sprint_candidates_from_session
from services.specs.compiler_service import (
    compiled_authority_schema_unsupported_details,
    compiled_authority_schema_unsupported_remediation,
    load_compiled_artifact,
)
from services.sprint_input import apply_story_completion_scope_to_candidate_result
from utils.spec_schemas import ValidationEvidence

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm.attributes import InstrumentedAttribute

JsonDict = dict[str, Any]

PROJECT_LIST_COMMAND: Final[str] = "agileforge project list"
PROJECT_SHOW_COMMAND: Final[str] = "agileforge project show"
WORKFLOW_STATE_COMMAND: Final[str] = "agileforge workflow state"
STORY_SHOW_COMMAND: Final[str] = "agileforge story show"
SPRINT_CANDIDATES_COMMAND: Final[str] = "agileforge sprint candidates"

_PRODUCT_REQUIREMENT: Final[SchemaRequirement] = SchemaRequirement(
    "products",
    (
        "product_id",
        "name",
        "description",
        "vision",
        "roadmap",
        "spec_file_path",
        "updated_at",
    ),
)
_USER_STORY_REQUIREMENT: Final[SchemaRequirement] = SchemaRequirement(
    "user_stories",
    (
        "story_id",
        "product_id",
        "feature_id",
        "title",
        "story_description",
        "acceptance_criteria",
        "status",
        "story_points",
        "rank",
        "source_requirement",
        "story_origin",
        "is_refined",
        "is_superseded",
        "archived_reason",
        "archived_at",
        "archived_by",
        "archive_reset_attempt_id",
        "archive_previous_status",
        "persona",
        "accepted_spec_version_id",
        "validation_evidence",
        "updated_at",
    ),
)
_SPRINT_REQUIREMENT: Final[SchemaRequirement] = SchemaRequirement(
    "sprints",
    ("sprint_id", "product_id", "status", "updated_at"),
)
_SPRINT_STORY_REQUIREMENT: Final[SchemaRequirement] = SchemaRequirement(
    "sprint_stories",
    ("sprint_id", "story_id"),
)
_THEME_REQUIREMENT: Final[SchemaRequirement] = SchemaRequirement(
    "themes",
    ("theme_id", "product_id", "title", "updated_at"),
)
_EPIC_REQUIREMENT: Final[SchemaRequirement] = SchemaRequirement(
    "epics",
    ("epic_id", "theme_id", "title", "updated_at"),
)
_FEATURE_REQUIREMENT: Final[SchemaRequirement] = SchemaRequirement(
    "features",
    ("feature_id", "epic_id", "title", "updated_at"),
)
_SPEC_REQUIREMENT: Final[SchemaRequirement] = SchemaRequirement(
    "spec_registry",
    (
        "spec_version_id",
        "product_id",
        "spec_hash",
        "status",
        "created_at",
        "approved_at",
    ),
)

_PROJECT_LIST_REQUIREMENTS: Final[tuple[SchemaRequirement, ...]] = (
    _PRODUCT_REQUIREMENT,
    _USER_STORY_REQUIREMENT,
    _SPRINT_REQUIREMENT,
)
_PROJECT_SHOW_REQUIREMENTS: Final[tuple[SchemaRequirement, ...]] = (
    _PRODUCT_REQUIREMENT,
    _THEME_REQUIREMENT,
    _EPIC_REQUIREMENT,
    _FEATURE_REQUIREMENT,
    _USER_STORY_REQUIREMENT,
    _SPRINT_REQUIREMENT,
    _SPEC_REQUIREMENT,
)
_WORKFLOW_STATE_REQUIREMENTS: Final[tuple[SchemaRequirement, ...]] = (
    _PRODUCT_REQUIREMENT,
)
_STORY_SHOW_REQUIREMENTS: Final[tuple[SchemaRequirement, ...]] = (
    _PRODUCT_REQUIREMENT,
    _THEME_REQUIREMENT,
    _EPIC_REQUIREMENT,
    _FEATURE_REQUIREMENT,
    _USER_STORY_REQUIREMENT,
)
_SPRINT_CANDIDATE_REQUIREMENTS: Final[tuple[SchemaRequirement, ...]] = (
    _PRODUCT_REQUIREMENT,
    _USER_STORY_REQUIREMENT,
    _SPRINT_REQUIREMENT,
    _SPRINT_STORY_REQUIREMENT,
)


@dataclass(frozen=True)
class _StoryHierarchy:
    """Optional parent rows for a story."""

    product: Product | None
    feature: Feature | None
    epic: Epic | None
    theme: Theme | None


def _success(data: JsonDict) -> JsonDict:
    """Return a successful projection envelope-like payload."""
    return {"ok": True, "data": data, "warnings": [], "errors": []}


def _schema_error(command: str, readiness: SchemaReadiness) -> JsonDict:
    """Return a stable schema-not-ready error envelope."""
    return error_envelope(
        command=command,
        error=workbench_error(
            ErrorCode.SCHEMA_NOT_READY,
            message=(
                "Database schema is missing required tables or columns for this "
                "read-only command."
            ),
            details={"missing": readiness.missing},
            remediation=[
                "Run the application startup or migration command before using the CLI."
            ],
        ),
    )


def _int_column(value: object) -> InstrumentedAttribute[int | None]:
    """Return a SQLModel integer column expression for typed query helpers."""
    return cast("InstrumentedAttribute[int | None]", value)


def _project_not_found_error(command: str, project_id: int) -> JsonDict:
    """Return a structured project lookup error."""
    return error_envelope(
        command=command,
        error=workbench_error(
            ErrorCode.PROJECT_NOT_FOUND,
            message=f"Project {project_id} was not found.",
            details={"project_id": project_id},
            remediation=["agileforge project list"],
        ),
    )


def _authority_regenerate_next_action(
    *,
    project_id: int,
    spec_version_id: int | None,
) -> JsonDict:
    """Return the standard next action for unsupported authority artifacts."""
    return {
        "command": "agileforge authority regenerate",
        "args": {
            "project_id": project_id,
            "spec_version_id": spec_version_id,
            "idempotency_key": "<new-key>",
        },
        "reason": "regenerate unsupported compiled authority before continuing.",
    }


def _unsupported_authority_error(
    *,
    command: str,
    project_id: int,
    spec_version_id: int | None,
    observed_schema_version: str | None,
) -> JsonDict:
    """Return an error envelope for unsupported compiled-authority artifacts."""
    result = error_envelope(
        command=command,
        error=workbench_error(
            ErrorCode.COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED,
            message="Compiled authority artifact schema is unsupported.",
            details=compiled_authority_schema_unsupported_details(
                project_id=project_id,
                spec_version_id=spec_version_id,
                observed_schema_version=observed_schema_version,
            ),
            remediation=compiled_authority_schema_unsupported_remediation(
                project_id=project_id,
                spec_version_id=spec_version_id,
            ),
        ),
    )
    result["data"] = {
        "project_id": project_id,
        "authority_status": "unsupported_schema",
        "next_actions": [
            _authority_regenerate_next_action(
                project_id=project_id,
                spec_version_id=spec_version_id,
            )
        ],
    }
    return result


def _story_not_found_error(story_id: int) -> JsonDict:
    """Return a structured story lookup error."""
    return error_envelope(
        command=STORY_SHOW_COMMAND,
        error=workbench_error(
            ErrorCode.STORY_NOT_FOUND,
            message=f"Story {story_id} was not found.",
            details={"story_id": story_id},
            remediation=["agileforge sprint candidates --project-id <project_id>"],
        ),
    )


def _iso_z(value: datetime | None) -> str | None:
    """Serialize datetimes as UTC ISO-8601 strings with a Z suffix."""
    if value is None:
        return None
    normalized = value if value.tzinfo else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _enum_value(value: object) -> object:
    """Return enum values without assuming a specific enum base class."""
    return value.value if hasattr(value, "value") else value


def _validation_evidence(raw: str | None) -> ValidationEvidence | None:
    """Parse persisted validation evidence without running validation."""
    if not raw:
        return None
    try:
        return ValidationEvidence.model_validate_json(raw)
    except (TypeError, ValueError, ValidationError, JSONDecodeError):
        return None


def _validation_summary(story: UserStory) -> JsonDict:
    """Return stable validation metadata for a story."""
    evidence = _validation_evidence(story.validation_evidence)
    return {
        "present": bool(story.validation_evidence),
        "passed": evidence.passed if evidence is not None else None,
        "spec_version_id": evidence.spec_version_id if evidence is not None else None,
        "evaluated_invariant_ids": (
            list(evidence.evaluated_invariant_ids) if evidence is not None else []
        ),
        "source_hash": (
            canonical_hash({"validation_evidence": story.validation_evidence})
            if story.validation_evidence
            else None
        ),
    }


def _latest_approved_spec(session: Session, project_id: int) -> SpecRegistry | None:
    """Return the latest approved spec for a project."""
    return session.exec(
        select(SpecRegistry)
        .where(
            SpecRegistry.product_id == project_id,
            SpecRegistry.status == "approved",
        )
        .order_by(
            cast("Any", SpecRegistry.approved_at).desc(),
            cast("Any", SpecRegistry.created_at).desc(),
            cast("Any", SpecRegistry.spec_version_id).desc(),
        )
    ).first()


def _latest_approved_spec_payload(spec: SpecRegistry | None) -> JsonDict | None:
    """Return JSON-safe latest spec metadata."""
    if spec is None:
        return None
    return {
        "spec_version_id": spec.spec_version_id,
        "spec_hash": spec.spec_hash,
        "status": spec.status,
        "created_at": _iso_z(spec.created_at),
        "approved_at": _iso_z(spec.approved_at),
    }


def _count(session: Session, statement: object) -> int:
    """Return an integer count from a SQLModel scalar query."""
    value = session.exec(cast("Any", statement)).one()
    return int(value or 0)


def _sprint_candidate_story_sources(
    session: Session,
    project_id: int,
) -> list[JsonDict]:
    """Return private row-state inputs for sprint candidate fingerprinting."""
    stories = session.exec(
        select(UserStory)
        .where(
            UserStory.product_id == project_id,
            UserStory.status == StoryStatus.TO_DO,
        )
        .order_by(
            cast("Any", UserStory.rank),
            cast("Any", UserStory.story_id),
        )
    ).all()
    return [
        {
            "story_id": story.story_id,
            "updated_at": _iso_z(story.updated_at),
            "status": _enum_value(story.status),
            "rank": story.rank,
            "is_refined": story.is_refined,
            "is_superseded": story.is_superseded,
            "archived_reason": story.archived_reason,
            "archived_at": _iso_z(story.archived_at),
            "archived_by": story.archived_by,
            "archive_reset_attempt_id": story.archive_reset_attempt_id,
            "archive_previous_status": story.archive_previous_status,
            "story_points": story.story_points,
            "accepted_spec_version_id": story.accepted_spec_version_id,
        }
        for story in stories
    ]


def _story_hierarchy(session: Session, story: UserStory) -> _StoryHierarchy:
    """Load optional parent rows for story detail output."""
    product = session.get(Product, story.product_id)
    feature = session.get(Feature, story.feature_id) if story.feature_id else None
    epic = session.get(Epic, feature.epic_id) if feature is not None else None
    theme = session.get(Theme, epic.theme_id) if epic is not None else None
    return _StoryHierarchy(
        product=product,
        feature=feature,
        epic=epic,
        theme=theme,
    )


def _hierarchy_payload(hierarchy: _StoryHierarchy) -> JsonDict:
    """Return optional story hierarchy metadata."""
    return {
        "product": (
            {
                "product_id": hierarchy.product.product_id,
                "name": hierarchy.product.name,
                "updated_at": _iso_z(hierarchy.product.updated_at),
            }
            if hierarchy.product is not None
            else None
        ),
        "theme": (
            {
                "theme_id": hierarchy.theme.theme_id,
                "title": hierarchy.theme.title,
                "updated_at": _iso_z(hierarchy.theme.updated_at),
            }
            if hierarchy.theme is not None
            else None
        ),
        "epic": (
            {
                "epic_id": hierarchy.epic.epic_id,
                "title": hierarchy.epic.title,
                "updated_at": _iso_z(hierarchy.epic.updated_at),
            }
            if hierarchy.epic is not None
            else None
        ),
        "feature": (
            {
                "feature_id": hierarchy.feature.feature_id,
                "title": hierarchy.feature.title,
                "updated_at": _iso_z(hierarchy.feature.updated_at),
            }
            if hierarchy.feature is not None
            else None
        ),
    }


def _int_or_none(value: object) -> int | None:
    """Return an integer for int-shaped values."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdecimal():
            return int(stripped)
    return None


def _reconcile_completed_sprint_projection(
    *,
    session: Session,
    project_id: int,
    state: JsonDict,
) -> JsonDict:
    """Project stale active-sprint state as complete when persisted Sprint is done."""
    if state.get("fsm_state") not in {"SPRINT_PERSISTENCE", "SPRINT_VIEW"}:
        return state

    active_sprint_id = _int_or_none(state.get("active_sprint_id"))
    if active_sprint_id is None:
        return state

    sprint = session.get(Sprint, active_sprint_id)
    if (
        sprint is None
        or sprint.product_id != project_id
        or sprint.status != SprintStatus.COMPLETED
    ):
        return state

    reconciled = dict(state)
    completed_at = _iso_z(sprint.completed_at or sprint.updated_at or sprint.created_at)
    reconciled["fsm_state"] = "SPRINT_COMPLETE"
    reconciled["active_sprint_id"] = None
    reconciled["latest_completed_sprint_id"] = active_sprint_id
    reconciled["sprint_completed_at"] = completed_at
    reconciled["sprint_state_reconciled_reason"] = "active_sprint_completed"
    return reconciled


class ReadProjectionService:
    """Read-only projections for CLI orientation commands."""

    def __init__(
        self,
        *,
        engine: Engine | None = None,
        session_reader: ReadOnlySessionReader | None = None,
    ) -> None:
        """Initialize the projection with read-only dependencies."""
        self._engine = engine or model_db.get_engine()
        self._session_reader = session_reader or ReadOnlySessionReader()

    def project_list(self) -> JsonDict:
        """Return projects with story and sprint counts."""
        schema_error = self._check_schema(
            PROJECT_LIST_COMMAND,
            _PROJECT_LIST_REQUIREMENTS,
        )
        if schema_error is not None:
            return schema_error

        with Session(self._engine) as session:
            products = list(
                session.exec(
                    select(Product).order_by(cast("Any", Product.product_id))
                ).all()
            )
            product_ids = [
                product.product_id
                for product in products
                if product.product_id is not None
            ]
            story_counts = self._count_by_product(
                session,
                _int_column(UserStory.product_id),
                _int_column(UserStory.story_id),
                product_ids,
                active_user_stories_only=True,
            )
            sprint_counts = self._count_by_product(
                session,
                _int_column(Sprint.product_id),
                _int_column(Sprint.sprint_id),
                product_ids,
            )
            items: list[JsonDict] = []
            for product in products:
                product_id = product.product_id
                if product_id is None:
                    continue
                items.append(
                    {
                        "product_id": product_id,
                        "name": product.name,
                        "description": product.description,
                        "user_stories_count": story_counts.get(product_id, 0),
                        "sprint_count": sprint_counts.get(product_id, 0),
                        "updated_at": _iso_z(product.updated_at),
                    }
                )

        data = {
            "items": items,
            "count": len(items),
            "source_fingerprint": canonical_hash(
                {"command": PROJECT_LIST_COMMAND, "items": items}
            ),
        }
        return _success(data)

    def project_show(self, *, project_id: int) -> JsonDict:
        """Return project detail counts without active-project hydration."""
        schema_error = self._check_schema(
            PROJECT_SHOW_COMMAND,
            _PROJECT_SHOW_REQUIREMENTS,
        )
        if schema_error is not None:
            return schema_error

        with Session(self._engine) as session:
            product = session.get(Product, project_id)
            if product is None:
                return _project_not_found_error(PROJECT_SHOW_COMMAND, project_id)

            counts = {
                "themes": _count(
                    session,
                    select(func.count(cast("Any", Theme.theme_id))).where(
                        Theme.product_id == project_id
                    ),
                ),
                "epics": _count(
                    session,
                    select(func.count(cast("Any", Epic.epic_id)))
                    .join(Theme)
                    .where(Theme.product_id == project_id),
                ),
                "features": _count(
                    session,
                    select(func.count(cast("Any", Feature.feature_id)))
                    .join(Epic)
                    .join(Theme)
                    .where(Theme.product_id == project_id),
                ),
                "user_stories": _count(
                    session,
                    select(func.count(cast("Any", UserStory.story_id)))
                    .where(UserStory.product_id == project_id)
                    .where(UserStory.is_superseded == False),  # noqa: E712
                ),
                "sprints": _count(
                    session,
                    select(func.count(cast("Any", Sprint.sprint_id))).where(
                        Sprint.product_id == project_id
                    ),
                ),
            }
            latest_spec = _latest_approved_spec(session, project_id)
            latest_spec_payload = _latest_approved_spec_payload(latest_spec)
            product_payload = {
                "product_id": product.product_id,
                "name": product.name,
                "description": product.description,
                "vision_present": bool(product.vision),
                "roadmap_present": bool(product.roadmap),
                "spec_file_path": product.spec_file_path,
                "updated_at": _iso_z(product.updated_at),
            }

        data = {
            **product_payload,
            "structure_counts": counts,
            "latest_approved_spec": latest_spec_payload,
            "source_fingerprint": canonical_hash(
                {
                    "command": PROJECT_SHOW_COMMAND,
                    "project_id": project_id,
                    "product": product_payload,
                    "structure_counts": counts,
                    "latest_approved_spec": latest_spec_payload,
                }
            ),
        }
        return _success(data)

    def workflow_state(self, *, project_id: int) -> JsonDict:
        """Return workflow session state without creating or updating sessions."""
        schema_error = self._check_schema(
            WORKFLOW_STATE_COMMAND,
            _WORKFLOW_STATE_REQUIREMENTS,
        )
        if schema_error is not None:
            return schema_error

        state = self._session_reader.get_project_state(project_id)

        with Session(self._engine) as session:
            product = session.get(Product, project_id)
            if product is None:
                return _project_not_found_error(WORKFLOW_STATE_COMMAND, project_id)
            product_payload = {
                "product_id": product.product_id,
                "updated_at": _iso_z(product.updated_at),
            }
            state = _reconcile_completed_sprint_projection(
                session=session,
                project_id=project_id,
                state=state,
            )
            spec_version_id = _int_or_none(state.get("latest_spec_version_id"))
            if spec_version_id is None:
                latest_spec = _latest_approved_spec(session, project_id)
                spec_version_id = (
                    latest_spec.spec_version_id if latest_spec is not None else None
                )
            if spec_version_id is not None:
                authority = session.exec(
                    select(CompiledSpecAuthority).where(
                        CompiledSpecAuthority.spec_version_id == spec_version_id
                    )
                ).first()
                if authority is not None:
                    load_result = load_compiled_artifact(authority)
                    if load_result.unsupported:
                        return _unsupported_authority_error(
                            command=WORKFLOW_STATE_COMMAND,
                            project_id=project_id,
                            spec_version_id=spec_version_id,
                            observed_schema_version=(
                                load_result.observed_schema_version
                            ),
                        )

        data = {
            "project_id": project_id,
            "state": state,
            "source_fingerprint": canonical_hash(
                {
                    "command": WORKFLOW_STATE_COMMAND,
                    "project_id": project_id,
                    "product": product_payload,
                    "state": state,
                }
            ),
        }
        return _success(data)

    def story_show(self, *, story_id: int) -> JsonDict:
        """Return story details and stored validation evidence metadata."""
        schema_error = self._check_schema(
            STORY_SHOW_COMMAND,
            _STORY_SHOW_REQUIREMENTS,
        )
        if schema_error is not None:
            return schema_error

        with Session(self._engine) as session:
            story = session.get(UserStory, story_id)
            if story is None:
                return _story_not_found_error(story_id)

            hierarchy = _story_hierarchy(session, story)
            hierarchy_payload = _hierarchy_payload(hierarchy)
            validation = _validation_summary(story)
            story_payload = {
                "story_id": story.story_id,
                "product_id": story.product_id,
                "feature_id": story.feature_id,
                "title": story.title,
                "description": story.story_description,
                "acceptance_criteria": story.acceptance_criteria,
                "status": _enum_value(story.status),
                "story_points": story.story_points,
                "rank": story.rank,
                "is_refined": story.is_refined,
                "is_superseded": story.is_superseded,
                "archived_reason": story.archived_reason,
                "archived_at": _iso_z(story.archived_at),
                "archived_by": story.archived_by,
                "archive_reset_attempt_id": story.archive_reset_attempt_id,
                "archive_previous_status": story.archive_previous_status,
                "accepted_spec_version_id": story.accepted_spec_version_id,
                "updated_at": _iso_z(story.updated_at),
            }

        data = {
            **story_payload,
            "hierarchy": hierarchy_payload,
            "validation": validation,
            "source_fingerprint": canonical_hash(
                {
                    "command": STORY_SHOW_COMMAND,
                    "story": story_payload,
                    "hierarchy": hierarchy_payload,
                    "validation": validation,
                }
            ),
        }
        return _success(data)

    def sprint_candidates(self, *, project_id: int) -> JsonDict:
        """Return sprint candidates using existing eligibility semantics."""
        schema_error = self._check_schema(
            SPRINT_CANDIDATES_COMMAND,
            _SPRINT_CANDIDATE_REQUIREMENTS,
        )
        if schema_error is not None:
            return schema_error

        with Session(self._engine) as session:
            product = session.get(Product, project_id)
            if product is None:
                return _project_not_found_error(SPRINT_CANDIDATES_COMMAND, project_id)

            open_sprint_story_ids, open_sprints = self._open_sprint_story_ids(
                session=session,
                project_id=project_id,
            )
            story_sources = _sprint_candidate_story_sources(session, project_id)
            raw = fetch_sprint_candidates_from_session(session, project_id)

        state = self._session_reader.get_project_state(project_id)
        raw = apply_story_completion_scope_to_candidate_result(
            raw,
            state.get("story_completion_scope"),
        )
        items = raw.get("stories", [])
        excluded_counts = raw.get("excluded_counts", {})
        readiness = raw.get("readiness") or {
            "status": "ready",
            "unsized_count": 0,
            "default_priority_count": 0,
            "blocking_codes": [],
            "blocking_story_ids": [],
        }
        source_payload = {
            "command": SPRINT_CANDIDATES_COMMAND,
            "project_id": project_id,
            "open_sprint_story_ids": sorted(open_sprint_story_ids),
            "open_sprints": open_sprints,
            "story_sources": story_sources,
            "candidate_story_ids": [item.get("story_id") for item in items],
            "candidate_items": items,
            "count": raw.get("count", 0),
            "excluded_counts": excluded_counts,
            "readiness": readiness,
            "message": raw.get("message"),
            "story_completion_scope": raw.get("story_completion_scope"),
        }
        data = {
            "items": items,
            "count": raw.get("count", 0),
            "excluded_counts": excluded_counts,
            "readiness": readiness,
            "message": raw.get("message"),
            "story_completion_scope": raw.get("story_completion_scope"),
            "source_fingerprint": canonical_hash(source_payload),
        }
        return _success(data)

    def _check_schema(
        self,
        command: str,
        requirements: tuple[SchemaRequirement, ...],
    ) -> JsonDict | None:
        """Return a schema error envelope when required schema is absent."""
        readiness = check_schema_readiness(self._engine, requirements)
        if readiness.ok:
            return None
        return _schema_error(command, readiness)

    def _count_by_product(
        self,
        session: Session,
        product_column: InstrumentedAttribute[int | None],
        counted_column: InstrumentedAttribute[int | None],
        product_ids: list[int],
        *,
        active_user_stories_only: bool = False,
    ) -> dict[int, int]:
        """Return grouped row counts by product id."""
        if not product_ids:
            return {}
        statement = select(product_column, func.count(counted_column)).where(
            product_column.in_(product_ids)
        )
        if active_user_stories_only:
            statement = statement.where(UserStory.is_superseded == False)  # noqa: E712
        rows = session.exec(statement.group_by(product_column)).all()
        return {int(product_id): int(count) for product_id, count in rows}

    def _open_sprint_story_ids(
        self,
        *,
        session: Session,
        project_id: int,
    ) -> tuple[set[int], list[JsonDict]]:
        """Return stories already attached to planned or active project sprints."""
        open_sprints = list(
            session.exec(
                select(Sprint)
                .where(
                    Sprint.product_id == project_id,
                    cast("Any", Sprint.status).in_(
                        [SprintStatus.PLANNED, SprintStatus.ACTIVE]
                    ),
                )
                .order_by(cast("Any", Sprint.sprint_id))
            ).all()
        )
        open_sprint_ids = [
            sprint.sprint_id for sprint in open_sprints if sprint.sprint_id is not None
        ]
        if not open_sprint_ids:
            return set(), []

        story_ids = {
            int(story_id)
            for story_id in session.exec(
                select(SprintStory.story_id).where(
                    cast("Any", SprintStory.sprint_id).in_(open_sprint_ids)
                )
            ).all()
            if story_id is not None
        }
        sprint_payloads = [
            {
                "sprint_id": sprint.sprint_id,
                "status": _enum_value(sprint.status),
                "updated_at": _iso_z(sprint.updated_at),
            }
            for sprint in open_sprints
        ]
        return story_ids, sprint_payloads
