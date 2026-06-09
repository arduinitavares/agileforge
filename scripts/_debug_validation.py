#!/usr/bin/env python3
"""Temporary debug script to investigate validation failures for product 8."""

import json
import sys
from pathlib import Path

from utils.cli_output import emit

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlmodel import Session, select

from agile_sqlmodel import CompiledSpecAuthority, SpecRegistry, UserStory, get_engine
from tools.spec_tools import _load_compiled_artifact, validate_story_with_spec_authority

engine = get_engine()

with Session(engine) as s:
    # Check spec version 9
    spec = s.get(SpecRegistry, 9)
    if spec:
        emit(f"Spec 9: product_id={spec.product_id}, status={spec.status}")
    else:
        emit("Spec 9: NOT FOUND")

    # Check authority for spec 9
    auth = s.exec(
        select(CompiledSpecAuthority).where(CompiledSpecAuthority.spec_version_id == 9)  # noqa: PLR2004
    ).first()
    if auth:
        emit(f"Authority for spec 9: authority_id={auth.authority_id}")
        emit(f"  compiled_artifact_json present: {bool(auth.compiled_artifact_json)}")
        load_result = _load_compiled_artifact(auth)
        artifact = load_result.artifact if load_result.ok else None
        if artifact:
            emit(f"  scope_themes: {artifact.scope_themes}")
            emit(f"  invariants count: {len(artifact.invariants)}")
            for inv in artifact.invariants:
                emit(f"    {inv.id}: {inv.type}")
        else:
            emit("  Could not load artifact")
    else:
        emit("Authority for spec 9: NOT FOUND")

    # Check stories
    stories = s.exec(select(UserStory).where(UserStory.product_id == 8)).all()  # noqa: PLR2004
    emit(f"\nStories for product 8: {len(stories)}")

    # Validate first story with verbose output
    if stories:
        st = stories[0]
        emit(f"\n--- Sample Story {st.story_id} ---")
        emit(f"  title: {st.title}")
        emit(f"  product_id: {st.product_id}")
        has_ac = bool(st.acceptance_criteria and st.acceptance_criteria.strip())
        emit(f"  acceptance_criteria present: {has_ac}")
        if st.acceptance_criteria:
            emit(f"  acceptance_criteria (first 200): {st.acceptance_criteria[:200]}")
        desc = st.story_description or ""
        emit(f"  description (first 200): {desc[:200]}")
        title_lower = (st.title or "").lower()
        desc_lower = desc.lower()
        has_persona = (
            "as a " in title_lower
            or "as a " in desc_lower
            or "as an " in title_lower
            or "as an " in desc_lower
        )
        emit(f"  has persona format: {has_persona}")

        # Run actual validation
        emit("\n--- Validation Result ---")
        res = validate_story_with_spec_authority(
            {"story_id": st.story_id, "spec_version_id": 9}
        )
        emit(json.dumps(res, indent=2, default=str))
