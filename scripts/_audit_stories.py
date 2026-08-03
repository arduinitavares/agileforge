#!/usr/bin/env python3
"""Quick audit: show acceptance_criteria status for project 8 stories."""

import sys
from pathlib import Path

from utils.cli_output import emit

sys.path.append(str(Path(__file__).parent.parent))

from sqlmodel import Session, col, select

from agile_sqlmodel import UserStory, get_engine

PROJECT_ID = 8

with Session(get_engine()) as s:
    stories = s.exec(
        select(UserStory)
        .where(UserStory.project_id == PROJECT_ID)
        .order_by(col(UserStory.story_id))
    ).all()
    has_ac = 0
    no_ac = 0
    for st in stories:
        ac = (st.acceptance_criteria or "").strip()
        has = bool(ac)
        if has:
            has_ac += 1
        else:
            no_ac += 1
        persona_ok = (st.story_description or "").strip().startswith("As a")
        emit(
            f"  {st.story_id}: ac={has!s:<6} persona={persona_ok!s:<6} {st.title[:55]}"
        )
    emit(f"\nTotal: {len(stories)} | With AC: {has_ac} | Without AC: {no_ac}")
