#!/usr/bin/env python3
"""Build candidate validation benchmark cases from real DB records."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from sqlmodel import Session, col, select

from utils.cli_output import emit

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))

from agile_sqlmodel import (  # pylint: disable=wrong-import-position  # noqa: E402
    SpecRegistry,
    UserStory,
    get_engine,
)
from services.specs.authority_selection import accepted_compiled_authority  # noqa: E402
from services.specs.compiler_service import load_compiled_artifact  # noqa: E402


def _compute_content_hash(story: UserStory) -> str:
    """Compute stable hash for label invalidation when story text changes."""
    payload = json.dumps(
        {
            "title": story.title or "",
            "description": story.story_description or "",
            "acceptance_criteria": story.acceptance_criteria or "",
        },
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _extract_existing_labels(
    story: UserStory,
) -> tuple[bool | None, list[str], str | None]:
    """Extract optional labels from story.validation_evidence."""
    if not story.validation_evidence:
        return None, [], None

    try:
        evidence = json.loads(story.validation_evidence)
    except json.JSONDecodeError:
        return None, [], "validation_evidence_invalid_json"

    expected_pass = evidence.get("passed")
    if not isinstance(expected_pass, bool):
        expected_pass = None

    reason_codes: set[str] = set()
    for failure in evidence.get("failures", []):
        if isinstance(failure, dict):
            rule = failure.get("rule")
            if isinstance(rule, str) and rule.strip():
                reason_codes.add(rule.strip())

    for finding in evidence.get("alignment_failures", []):
        if isinstance(finding, dict):
            code = finding.get("code")
            if isinstance(code, str) and code.strip():
                reason_codes.add(code.strip())

    return expected_pass, sorted(reason_codes), "validation_evidence"


def _apply_no_evidence_labels(
    expected_pass: bool | None,
    expected_fail_reasons: list[str],
    label_source: str | None,
    *,
    no_evidence_labels: bool,
) -> tuple[bool | None, list[str]]:
    """Optionally clear labels that come from validator-written evidence."""
    if no_evidence_labels and label_source == "validation_evidence":
        return None, []
    return expected_pass, expected_fail_reasons


def _maybe_warn_evidence_only(cases: list[dict[str, Any]]) -> None:
    """Warn when all labels in output are sourced from validation_evidence."""
    if not cases:
        return
    evidence_labels = [
        case for case in cases if case.get("label_source") == "validation_evidence"
    ]
    if len(evidence_labels) == len(cases):
        emit(
            (
                "WARNING: All labels derive from validation_evidence. "
                "This creates circular evaluation risk. Relabel with human_review "
                "before benchmarking (see docs/VALIDATION_BENCHMARK_FORMAT.md)."
            ),
            file=sys.stderr,
        )


def _resolve_spec_version_id(
    session: Session,
    story: UserStory,
    *,
    require_compiled: bool,
) -> tuple[int | None, str]:
    """Resolve spec version for a story using accepted pin or latest approved spec."""
    if story.accepted_spec_version_id:
        spec_id = int(story.accepted_spec_version_id)
        if require_compiled:
            authority = accepted_compiled_authority(
                session,
                product_id=story.product_id,
                spec_version_id=spec_id,
            )
            loaded = (
                load_compiled_artifact(authority) if authority is not None else None
            )
            if loaded is None or not loaded.ok or loaded.artifact is None:
                return None, "accepted_spec_invalid"
        return spec_id, "accepted_spec_version_id"

    spec = session.exec(
        select(SpecRegistry)
        .where(
            SpecRegistry.product_id == story.product_id,
            SpecRegistry.status == "approved",
        )
        .order_by(col(SpecRegistry.spec_version_id).desc())
    ).first()
    if not spec:
        return None, "no_approved_spec"

    spec_id = spec.spec_version_id
    if spec_id is None:
        return None, "latest_approved_spec_missing_id"
    if require_compiled:
        authority = accepted_compiled_authority(
            session,
            product_id=story.product_id,
            spec_version_id=spec_id,
        )
        loaded = load_compiled_artifact(authority) if authority is not None else None
        if loaded is None or not loaded.ok or loaded.artifact is None:
            return None, "approved_spec_invalid"

    return spec_id, "latest_approved_spec"


def build_cases(
    *,
    product_id: int | None,
    limit: int,
    labeled_only: bool,
    require_compiled: bool,
    no_evidence_labels: bool = False,
) -> list[dict[str, Any]]:
    """Build benchmark case records from DB stories."""
    rows: list[dict[str, Any]] = []

    with Session(get_engine()) as session:
        statement = select(UserStory).order_by(col(UserStory.story_id).asc())
        if product_id is not None:
            statement = statement.where(UserStory.product_id == product_id)

        stories = session.exec(statement).all()
        for story in stories:
            spec_id, spec_source = _resolve_spec_version_id(
                session,
                story,
                require_compiled=require_compiled,
            )
            if spec_id is None:
                continue

            expected_pass, expected_fail_reasons, label_source = (
                _extract_existing_labels(story)
            )
            expected_pass, expected_fail_reasons = _apply_no_evidence_labels(
                expected_pass,
                expected_fail_reasons,
                label_source,
                no_evidence_labels=no_evidence_labels,
            )
            if labeled_only and expected_pass is None:
                continue

            story_id = story.story_id
            if story_id is None:
                continue

            rows.append(
                {
                    "case_id": f"p{story.product_id}-s{story_id}-v{spec_id}",
                    "story_id": story_id,
                    "spec_version_id": spec_id,
                    "expected_pass": expected_pass,
                    "expected_fail_reasons": expected_fail_reasons,
                    "notes": None,
                    "tags": ["real-data"],
                    "enabled": True,
                    "product_id": int(story.product_id),
                    "story_title": story.title or "",
                    "spec_source": spec_source,
                    "label_source": label_source,
                    "content_hash": _compute_content_hash(story),
                }
            )
            if len(rows) >= limit:
                break

    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write rows as JSONL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True) + "\n")


def main() -> None:
    """Return main."""
    parser = argparse.ArgumentParser(
        description="Build validation benchmark candidate cases from DB stories"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts") / "validation_benchmark" / "cases.jsonl",
        help="Output JSONL path",
    )
    parser.add_argument(
        "--product-id",
        type=int,
        default=None,
        help="Optional product id filter",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=120,
        help="Maximum number of cases",
    )
    parser.add_argument(
        "--labeled-only",
        action="store_true",
        help="Only include cases with existing expected_pass from validation_evidence",
    )
    parser.add_argument(
        "--allow-uncompiled",
        action="store_true",
        help=(
            "Permissive mode: allow cases without an exact accepted valid "
            "compiled authority"
        ),
    )
    parser.add_argument(
        "--no-evidence-labels",
        action="store_true",
        help=(
            "Clear expected_pass/expected_fail_reasons when label source is "
            "validation_evidence to avoid circular labeling."
        ),
    )
    args = parser.parse_args()

    cases = build_cases(
        product_id=args.product_id,
        limit=args.limit,
        labeled_only=args.labeled_only,
        require_compiled=not args.allow_uncompiled,
        no_evidence_labels=args.no_evidence_labels,
    )
    _maybe_warn_evidence_only(cases)
    write_jsonl(args.output, cases)
    emit(f"Wrote {len(cases)} case(s) to: {args.output}")


if __name__ == "__main__":
    main()
