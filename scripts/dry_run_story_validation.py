#!/usr/bin/env python3
"""Validate stories against spec authority without persisting changes."""

import argparse
from collections.abc import Mapping, Sequence
from typing import TypeGuard

from sqlmodel import Session, col, create_engine, select

from agile_sqlmodel import SpecRegistry, UserStory
from services.specs.authority_selection import accepted_compiled_authority
from services.specs.compiler_service import load_compiled_artifact
from utils.cli_output import emit
from utils.runtime_config import DatabaseTarget, resolve_database_target


def resolve_db_target(explicit_db: str | None = None) -> DatabaseTarget:
    """Resolve the business DB target for dry-run validation."""
    return resolve_database_target(explicit_db, env_name="AGILEFORGE_DB_URL")


def _load_accepted_invariants(
    session: Session,
    *,
    project_id: int,
    spec_version_id: int,
) -> list[dict[str, object]] | None:
    """Load invariants only from the exact accepted valid authority."""
    authority = accepted_compiled_authority(
        session,
        project_id=project_id,
        spec_version_id=spec_version_id,
    )
    if authority is None:
        return None
    loaded = load_compiled_artifact(authority)
    if not loaded.ok or loaded.artifact is None:
        return None
    return [
        invariant.model_dump(mode="json") for invariant in loaded.artifact.invariants
    ]


def _story_validation_failures(
    story: UserStory,
    invariants: Sequence[Mapping[str, object]],
) -> list[str]:
    failures: list[str] = []
    if not story.title:
        failures.append("Missing title")
    if not story.acceptance_criteria:
        failures.append("Missing acceptance_criteria")

    for invariant in invariants:
        if invariant.get("type") != "FORBIDDEN_CAPABILITY":
            continue
        parameters = invariant.get("parameters")
        if not _is_string_object_mapping(parameters):
            continue
        term = parameters.get("capability")
        if (
            isinstance(term, str)
            and term
            and term in (story.story_description or "")
        ):
            failures.append(f"Forbidden term found: {term}")
    return failures


def _is_string_object_mapping(
    value: object,
) -> TypeGuard[Mapping[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _emit_story_results(
    stories: Sequence[UserStory],
    invariants: Sequence[Mapping[str, object]],
) -> tuple[int, int]:
    passed_count = 0
    failed_count = 0
    for story in stories:
        failures = _story_validation_failures(story, invariants)
        status = "FAIL" if failures else "PASS"
        if failures:
            failed_count += 1
        else:
            passed_count += 1

        emit(f"Story {story.story_id}: {status}")
        emit(f"  Title: {story.title}")
        if failures:
            emit(f"  Desc: {(story.story_description or '')[:100]}...")
            for failure in failures:
                emit(f"  - {failure}")
    return passed_count, failed_count


def dry_run_validation(project_id: int, db: str | None = None) -> None:
    """Return dry run validation."""
    db_target = resolve_db_target(db)
    if db_target.sqlite_path is None or not db_target.sqlite_path.exists():
        msg = f"Database file not found: {db_target.sqlite_connect_target}"
        raise FileNotFoundError(
            msg
        )
    engine = create_engine(
        db_target.sqlite_url,
        connect_args={"check_same_thread": False},
    )

    emit(f"Connecting to DB: {db_target.sqlite_connect_target}")

    with Session(engine) as session:
        # 1. Get the APPROVED spec version for this Project
        statement = (
            select(SpecRegistry)
            .where(
                SpecRegistry.project_id == project_id, SpecRegistry.status == "approved"
            )
            .order_by(col(SpecRegistry.spec_version_id).desc())
        )

        spec = session.exec(statement).first()

        if not spec:
            emit(f"ERROR: No approved spec found for Project {project_id}")
            return
        if spec.spec_version_id is None:
            emit(f"ERROR: Approved spec for Project {project_id} has no ID.")
            return

        emit(f"Using Spec Version {spec.spec_version_id} (ID: {spec.spec_version_id})")

        # 2. Get all stories for the Project
        stories = session.exec(
            select(UserStory).where(UserStory.project_id == project_id)
        ).all()

        emit(f"Found {len(stories)} stories to validate.")
        emit("-" * 60)

        invariants = _load_accepted_invariants(
            session,
            project_id=project_id,
            spec_version_id=spec.spec_version_id,
        )
        if invariants is None:
            emit(
                "ERROR: No exact accepted valid authority found for this spec version."
            )
            return

        emit(f"Loaded {len(invariants)} invariants from authority.")
        passed_count, failed_count = _emit_story_results(stories, invariants)

        emit("-" * 60)
        emit(f"Validation Summary: {passed_count} PASSED, {failed_count} FAILED")
        emit("NOTE: This dry run checked basic fields and forbidden terms only.")
        emit("Real validation would be stricter but this confirms data shape.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Dry-run story validation against compiled spec authority.",
    )
    parser.add_argument("project_id", type=int, help="Project ID to inspect.")
    parser.add_argument(
        "--db",
        help=(
            "Optional SQLite database path or sqlite:/// URL. "
            "Defaults to AGILEFORGE_DB_URL."
        ),
    )
    args = parser.parse_args()
    dry_run_validation(args.project_id, db=args.db)
