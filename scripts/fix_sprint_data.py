"""Fix corrupted sprint data for product 2."""

import sys
from pathlib import Path

from utils.cli_output import emit

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import inspect
from sqlmodel import Session, select

from agile_sqlmodel import (
    Sprint,
    SprintStory,
    StoryStatus,
    Task,
    UserStory,
    WorkflowEvent,
    engine,
)


def main() -> None:  # noqa: C901
    """Return main."""
    with Session(engine) as s:
        # List all tables
        emit("=== DATABASE TABLES ===")
        tables = inspect(engine).get_table_names()
        emit(f"Tables: {tables}")

        # Check workflow events referencing sprint 1
        emit("\n=== WORKFLOW EVENTS REFERENCING SPRINT 1 ===")
        events = s.exec(select(WorkflowEvent).where(WorkflowEvent.sprint_id == 1)).all()
        emit(f"Events: {len(events)}")
        for e in events:
            emit(f"  Event {e.event_id}: {e.event_type}")

        # Delete workflow events first
        emit("\n=== DELETING WORKFLOW EVENTS ===")
        for e in events:
            s.delete(e)
            emit(f"  Deleted event {e.event_id}")

        # Check tasks linked to stories in sprint 1
        emit("\n=== TASKS LINKED TO STORIES ===")
        story_ids = [8, 9, 10, 11, 14]
        for sid in story_ids:
            tasks = s.exec(select(Task).where(Task.story_id == sid)).all()
            if tasks:
                emit(f"  Story {sid} has {len(tasks)} tasks")
                for t in tasks:
                    s.delete(t)
                    emit(f"    Deleted task {t.task_id}")

        # Delete SprintStory links
        emit("\n=== DELETING SPRINT-STORY LINKS ===")
        sprint_stories = s.exec(
            select(SprintStory).where(SprintStory.sprint_id == 1)
        ).all()
        for ss in sprint_stories:
            s.delete(ss)
            emit(f"  Deleted link: sprint_id={ss.sprint_id}, story_id={ss.story_id}")

        # Reset stories to TO_DO
        emit("\n=== RESETTING STORIES TO TO_DO ===")
        story_ids = [8, 9, 10, 11, 14]
        for sid in story_ids:
            story = s.get(UserStory, sid)
            if story:
                story.status = StoryStatus.TO_DO
                s.add(story)
                emit(f"  Story {sid}: reset to TO_DO")

        # Delete Sprint 1
        emit("\n=== DELETING SPRINT 1 ===")
        sprint = s.get(Sprint, 1)
        if sprint:
            s.delete(sprint)
            emit("  Sprint 1 deleted")

        # Commit all changes
        s.commit()
        emit("\n=== COMMITTED SUCCESSFULLY ===")

        # Verify
        emit("\n=== VERIFICATION ===")
        stories = s.exec(select(UserStory).where(UserStory.project_id == 2)).all()  # noqa: PLR2004
        for st in stories:
            emit(f"Story {st.story_id}: status={st.status.value}")

        sprints = s.exec(select(Sprint).where(Sprint.project_id == 2)).all()  # noqa: PLR2004
        emit(f"Sprints for product 2: {len(sprints)}")


if __name__ == "__main__":
    main()
