from __future__ import annotations

from typing import Any, cast

from sqlalchemy import delete
from sqlmodel import Session, select

from models.core import (
    SprintStory,
    Task,
    UserStory,
)
from models.db import get_engine
from models.events import StoryCompletionLog, TaskExecutionLog


class StoryRepository:
    """Repository handling database operations for story aggregates."""

    def __init__(self, session: Session | None = None):
        self._session = session

    def _get_session(self) -> Session:
        return self._session if self._session else Session(get_engine())

    def delete_by_requirement(
        self,
        *,
        project_id: int,
        normalized_requirement: str,
        chunk_size: int = 500,
    ) -> int:
        """Delete stories and dependent records for one requirement."""
        session = self._get_session()
        try:
            story_ids = [
                story_id
                for story_id in session.exec(
                    select(UserStory.story_id).where(
                        UserStory.project_id == project_id,
                        UserStory.source_requirement == normalized_requirement,
                    )
                ).all()
                if story_id is not None
            ]

            deleted_count = len(story_ids)
            if story_ids:
                for i in range(0, len(story_ids), chunk_size):
                    chunk_ids = story_ids[i : i + chunk_size]

                    session.exec(
                        delete(SprintStory).where(
                            cast("Any", SprintStory.story_id).in_(chunk_ids)
                        )
                    )
                    session.exec(
                        delete(StoryCompletionLog).where(
                            cast("Any", StoryCompletionLog.story_id).in_(chunk_ids)
                        )
                    )

                    task_ids = [
                        task_id
                        for task_id in session.exec(
                            select(Task.task_id).where(
                                cast("Any", Task.story_id).in_(chunk_ids)
                            )
                        ).all()
                        if task_id is not None
                    ]
                    if task_ids:
                        for j in range(0, len(task_ids), chunk_size):
                            task_chunk = task_ids[j : j + chunk_size]
                            session.exec(
                                delete(TaskExecutionLog).where(
                                    cast("Any", TaskExecutionLog.task_id).in_(
                                        task_chunk
                                    )
                                )
                            )

                    session.exec(
                        delete(Task).where(cast("Any", Task.story_id).in_(chunk_ids))
                    )
                    session.exec(
                        delete(UserStory).where(
                            cast("Any", UserStory.story_id).in_(chunk_ids)
                        )
                    )

            session.commit()
            return deleted_count
        finally:
            if not self._session:
                session.close()
