#!/usr/bin/env python3
"""One-time reconciliation for mixed backlog/refinement rows."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from utils.cli_output import emit

project_root = Path(__file__).parent.parent
sys.path.append(str(project_root))

from sqlmodel import Session, col, select  # noqa: E402

from agile_sqlmodel import UserStory, get_engine  # noqa: E402
from db.migrations import (  # noqa: E402
    migrate_performance_indexes,
    migrate_user_story_refinement_linkage,
)
from services.story_linkage import (  # noqa: E402
    normalize_requirement_key,
)


@dataclass
class ReconcileSummary:
    """Test helper for reconcile summary."""

    product_id: int
    migrated_actions: list[str]
    canonical_story_ids: list[int]
    superseded_story_ids: list[int]
    placeholder_story_ids: list[int]
    unresolved_story_ids: list[int]

    def as_dict(self) -> dict[str, object]:
        """Return as dict."""
        return {
            "product_id": self.product_id,
            "migrated_actions": self.migrated_actions,
            "canonical_story_ids": self.canonical_story_ids,
            "superseded_story_ids": self.superseded_story_ids,
            "placeholder_story_ids": self.placeholder_story_ids,
            "unresolved_story_ids": self.unresolved_story_ids,
            "canonical_count": len(self.canonical_story_ids),
            "superseded_count": len(self.superseded_story_ids),
            "placeholder_count": len(self.placeholder_story_ids),
            "unresolved_count": len(self.unresolved_story_ids),
        }


def _story_fingerprint(story: UserStory) -> tuple[str, str, str]:
    return (
        normalize_requirement_key(story.title or ""),
        normalize_requirement_key(story.story_description or ""),
        normalize_requirement_key(story.acceptance_criteria or ""),
    )


def _require_id(value: int | None, name: str) -> int:
    if value is None:
        msg = f"{name} was not generated"
        raise RuntimeError(msg)
    return value


def reconcile_product(product_id: int) -> ReconcileSummary:  # noqa: C901, PLR0912
    """Return reconcile product."""
    engine = get_engine()
    actions: list[str] = []
    actions.extend(migrate_user_story_refinement_linkage(engine))
    actions.extend(migrate_performance_indexes(engine))

    canonical_ids: list[int] = []
    superseded_ids: list[int] = []
    placeholder_ids: list[int] = []
    unresolved_ids: list[int] = []

    with Session(engine) as session:
        stories = session.exec(
            select(UserStory)
            .where(UserStory.product_id == product_id)
            .where(UserStory.is_superseded == False)  # noqa: E712
            .order_by(col(UserStory.story_id).asc())
        ).all()

        # Backfill linkage defaults for legacy rows where possible.
        for idx, story in enumerate(stories, start=1):
            if not story.source_requirement:
                story.source_requirement = normalize_requirement_key(story.title or "")
            if story.refinement_slot is None:
                story.refinement_slot = idx
            has_ac = bool((story.acceptance_criteria or "").strip())
            if story.story_origin is None:
                story.story_origin = "refined" if has_ac else "backlog_seed"
            if has_ac and not story.is_refined:
                story.is_refined = True
            if not has_ac and story.is_refined is None:
                story.is_refined = False
            session.add(story)

        # Deduplicate refined rows by strict fingerprint, keep earliest story_id canonical.  # noqa: E501
        refreshed = session.exec(
            select(UserStory)
            .where(UserStory.product_id == product_id)
            .where(UserStory.is_superseded == False)  # noqa: E712
            .order_by(col(UserStory.story_id).asc())
        ).all()

        by_fp: dict[tuple[str, str, str], list[UserStory]] = {}
        for story in refreshed:
            fp = _story_fingerprint(story)
            by_fp.setdefault(fp, []).append(story)

        for fp, group in by_fp.items():
            if not fp[2]:  # empty AC -> placeholder style
                for st in group:
                    placeholder_ids.append(_require_id(st.story_id, "Story ID"))  # noqa: PERF401
                continue

            canonical = group[0]
            canonical_story_id = _require_id(canonical.story_id, "Canonical story ID")
            canonical_ids.append(canonical_story_id)
            for duplicate in group[1:]:
                duplicate.is_superseded = True
                duplicate.superseded_by_story_id = canonical_story_id
                session.add(duplicate)
                superseded_ids.append(
                    _require_id(duplicate.story_id, "Duplicate story ID")
                )

        # Legacy placeholders remain unresolved for manual review if no direct mapping.
        for st in refreshed:
            if not (st.acceptance_criteria or "").strip() and not st.is_superseded:
                unresolved_ids.append(_require_id(st.story_id, "Story ID"))  # noqa: PERF401

        session.commit()

    return ReconcileSummary(
        product_id=product_id,
        migrated_actions=actions,
        canonical_story_ids=sorted(set(canonical_ids)),
        superseded_story_ids=sorted(set(superseded_ids)),
        placeholder_story_ids=sorted(set(placeholder_ids)),
        unresolved_story_ids=sorted(set(unresolved_ids)),
    )


def main() -> None:
    """Return main."""
    parser = argparse.ArgumentParser(
        description="Reconcile mixed backlog/refinement stories."
    )
    parser.add_argument(
        "--product-id", type=int, required=True, help="Product ID to reconcile."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional JSON output path (default artifacts/query_results/reconcile_product_<id>.json)",  # noqa: E501
    )
    args = parser.parse_args()

    summary = reconcile_product(args.product_id)

    output = (
        args.output
        or Path("artifacts/query_results") / f"reconcile_product_{args.product_id}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(summary.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )

    emit(json.dumps(summary.as_dict(), indent=2, ensure_ascii=False))
    emit(f"Wrote reconciliation report: {output}")


if __name__ == "__main__":
    main()
