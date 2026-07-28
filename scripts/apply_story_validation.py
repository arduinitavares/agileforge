#!/usr/bin/env python3
"""Apply spec-authority validation to refined stories for a product."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from sqlmodel import Session, col, select

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.append(str(project_root))

from agile_sqlmodel import (  # noqa: E402
    Product,
    SpecRegistry,
    UserStory,
    get_engine,
)
from services.specs.authority_selection import accepted_compiled_authority  # noqa: E402
from services.specs.compiler_service import load_compiled_artifact  # noqa: E402
from tools.spec_tools import validate_story_with_spec_authority  # noqa: E402
from utils.logging_config import configure_logging  # noqa: E402

LOGGER_NAME = "scripts.apply_story_validation"
logger = logging.getLogger(LOGGER_NAME)
engine = get_engine()


@dataclass
class StoryValidationOutcome:
    """Structured result for a single story validation run."""

    story_id: int
    passed: bool = False
    detail_messages: list[str] = field(default_factory=list)
    error_message: str | None = None


@dataclass
class ValidationRunResult:
    """Aggregate result for a script invocation."""

    product_id: int
    product_name: str = ""
    mode: str = "deterministic"
    status: Literal["success", "noop", "error"] = "success"
    spec_version_id: int | None = None
    eligible_story_count: int = 0
    outcomes: list[StoryValidationOutcome] = field(default_factory=list)
    message: str | None = None

    @property
    def passed_count(self) -> int:
        """Return passed count."""
        return sum(1 for outcome in self.outcomes if outcome.passed)

    @property
    def failed_count(self) -> int:
        """Return failed count."""
        return sum(
            1
            for outcome in self.outcomes
            if not outcome.passed and not outcome.error_message
        )

    @property
    def error_count(self) -> int:
        """Return error count."""
        return sum(1 for outcome in self.outcomes if outcome.error_message)


def _log_info(message: str, *, console_visible: bool = True) -> None:
    logger.info(message, extra={"console_visible": console_visible})


def _effective_mode(explicit_mode: str | None) -> str:
    if explicit_mode:
        return explicit_mode
    return (
        os.getenv("SPEC_VALIDATION_DEFAULT_MODE", "deterministic").strip().lower()
        or "deterministic"
    )


def _load_invariant_map(product_id: int, spec_version_id: int) -> dict[str, dict]:
    with Session(engine) as session:
        authority = accepted_compiled_authority(
            session,
            product_id=product_id,
            spec_version_id=spec_version_id,
        )
        loaded = load_compiled_artifact(authority) if authority is not None else None
    if loaded is None or not loaded.ok or loaded.artifact is None:
        return {}
    return {
        invariant.id: invariant.model_dump(mode="json")
        for invariant in loaded.artifact.invariants
    }


def _extract_invariant_ids(*texts: str) -> list[str]:
    ids: list[str] = []
    for txt in texts:
        for match in re.findall(r"INV-[a-f0-9]{16}", txt or "", flags=re.IGNORECASE):
            ids.append("INV-" + match[4:].lower())  # noqa: PERF401

    seen = set()
    ordered: list[str] = []
    for invariant_id in ids:
        if invariant_id in seen:
            continue
        seen.add(invariant_id)
        ordered.append(invariant_id)
    return ordered


def _summarize_response(  # noqa: C901, PLR0912
    response: dict,
    *,
    invariant_map: dict[str, dict],
) -> list[str]:
    messages: list[str] = []
    failures = response.get("failures", []) or []
    alignment_failures = response.get("alignment_failures", []) or []

    if failures:
        for failure in failures[:3]:
            rule = failure.get("rule", "UNKNOWN_RULE")
            actual = failure.get("actual", "")
            messages.append(f"{rule}: {actual}")
            inv_ids = _extract_invariant_ids(actual, failure.get("message", ""))
            for inv_id in inv_ids:
                invariant = invariant_map.get(inv_id)
                if not invariant:
                    continue
                inv_type = invariant.get("type", "UNKNOWN")
                params = (
                    invariant.get("parameters", {})
                    if isinstance(invariant.get("parameters"), dict)
                    else {}
                )
                field_name = params.get("field_name")
                capability = params.get("capability")
                if field_name:
                    messages.append(f"{inv_id} [{inv_type}] field_name={field_name}")
                elif capability:
                    messages.append(f"{inv_id} [{inv_type}] capability={capability}")
                else:
                    messages.append(f"{inv_id} [{inv_type}]")
        if len(failures) > 3:  # noqa: PLR2004
            messages.append(f"... and {len(failures) - 3} more failure(s)")

    if alignment_failures:
        for finding in alignment_failures[:3]:
            code = finding.get("code", "ALIGNMENT_FAILURE")
            message = finding.get("message", "")
            invariant = finding.get("invariant")
            if invariant:
                messages.append(f"{code} ({invariant}): {message}")
            else:
                messages.append(f"{code}: {message}")
        if len(alignment_failures) > 3:  # noqa: PLR2004
            messages.append(
                f"... and {len(alignment_failures) - 3} more alignment failure(s)"
            )

    if not messages:
        fallback_message = response.get("message", "Validation failed without details")
        messages.append(fallback_message)
    return messages


def _require_spec_version_id(result: ValidationRunResult) -> int:
    spec_version_id = result.spec_version_id
    if spec_version_id is None:
        msg = "Approved spec version id was lost before validation."
        raise RuntimeError(msg)
    return spec_version_id


def apply_validation(product_id: int, mode: str | None = None) -> ValidationRunResult:
    """Validate all canonical refined stories for a product and return structured results."""  # noqa: E501
    active_mode = _effective_mode(mode)
    result = ValidationRunResult(product_id=product_id, mode=active_mode)

    with Session(engine) as session:
        product = session.get(Product, product_id)
        if not product:
            result.status = "error"
            result.message = f"Product {product_id} not found."
            return result

        result.product_name = product.name
        spec = session.exec(
            select(SpecRegistry)
            .where(
                SpecRegistry.product_id == product_id,
                SpecRegistry.status == "approved",
            )
            .order_by(col(SpecRegistry.spec_version_id).desc())
        ).first()

        if not spec:
            result.status = "error"
            result.message = f"No approved spec found for product {product_id}."
            return result

        spec_version_id = spec.spec_version_id
        if spec_version_id is None:
            result.status = "error"
            result.message = f"Approved spec for product {product_id} has no ID."
            return result

        result.spec_version_id = spec_version_id
        invariant_map = _load_invariant_map(product_id, spec_version_id)
        stories = session.exec(
            select(UserStory)
            .where(UserStory.product_id == product_id)
            .where(UserStory.is_refined == True)  # noqa: E712
            .where(UserStory.is_superseded == False)  # noqa: E712
            .order_by(col(UserStory.story_id).asc())
        ).all()

    result.eligible_story_count = len(stories)
    if not stories:
        result.status = "noop"
        result.message = (
            f"No refined stories found for product {product_id}. Nothing to validate."
        )
        return result

    spec_version_id = _require_spec_version_id(result)
    for story in stories:
        if story.story_id is None:
            continue
        try:
            response = validate_story_with_spec_authority(
                {
                    "story_id": story.story_id,
                    "spec_version_id": spec_version_id,
                    "mode": active_mode,
                }
            )
        except Exception as exc:  # pragma: no cover - defensive runtime guard  # noqa: BLE001, E501
            result.outcomes.append(
                StoryValidationOutcome(
                    story_id=story.story_id,
                    error_message=str(exc),
                )
            )
            continue

        if not response.get("success", True):
            result.outcomes.append(
                StoryValidationOutcome(
                    story_id=story.story_id,
                    error_message=(
                        response.get("error")
                        or response.get("message")
                        or "Validation execution failed"
                    ),
                )
            )
            continue

        passed = bool(response.get("passed", False))
        result.outcomes.append(
            StoryValidationOutcome(
                story_id=story.story_id,
                passed=passed,
                detail_messages=(
                    []
                    if passed
                    else _summarize_response(response, invariant_map=invariant_map)
                ),
            )
        )

    if result.error_count:
        result.status = "error"
        result.message = (
            f"Validation encountered {result.error_count} execution error(s)."
        )
        return result

    result.status = "success"
    result.message = (
        f"Validated {result.eligible_story_count} stories: "
        f"{result.passed_count} passed, {result.failed_count} failed"
    )
    return result


def _emit_run_logs(  # noqa: C901
    result: ValidationRunResult,
    *,
    verbose: bool,
    quiet: bool,
    selected_latest_message: str | None = None,
) -> None:
    if selected_latest_message and not quiet:
        _log_info(selected_latest_message)

    if not quiet:
        label = f"Product {result.product_id}"
        if result.product_name:
            label += f" '{result.product_name}'"
        _log_info(f"Applying validation to {label}.")
        _log_info(f"Validation mode: {result.mode}")
        if result.spec_version_id is not None:
            _log_info(f"Using Spec Version {result.spec_version_id}")
        if result.eligible_story_count:
            _log_info(f"Found {result.eligible_story_count} eligible refined stories.")

    for outcome in result.outcomes:
        if outcome.error_message:
            logger.error(
                "Story %s validation error: %s",
                outcome.story_id,
                outcome.error_message,
            )
            continue

        status_label = "PASS" if outcome.passed else "FAIL"
        _log_info(
            f"Story {outcome.story_id}: {status_label}",
            console_visible=verbose,
        )
        for detail in outcome.detail_messages:
            _log_info(f"  - {detail}", console_visible=verbose)

    if result.status == "error":
        logger.error(result.message or "Validation failed.")
        return

    if result.message:
        _log_info(result.message)


def main(argv: list[str] | None = None) -> int:
    """Return main."""
    parser = argparse.ArgumentParser(
        description="Apply spec-authority validation to all stories for a product."
    )
    parser.add_argument(
        "product_id",
        nargs="?",
        type=int,
        default=None,
        help="Product ID to validate. Defaults to the most recently created product.",
    )
    parser.add_argument(
        "--mode",
        choices=["deterministic", "llm", "hybrid"],
        default=None,
        help=(
            "Validation mode override. If omitted, uses "
            "SPEC_VALIDATION_DEFAULT_MODE from environment (fallback: deterministic)."
        ),
    )
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "--verbose",
        action="store_true",
        help="Show per-story PASS/FAIL results and top validation reasons in console output.",  # noqa: E501
    )
    output_group.add_argument(
        "--quiet",
        action="store_true",
        help="Show only warnings, errors, and the final summary in console output.",
    )
    args = parser.parse_args(argv)

    configure_logging(console=True, console_logger_names=(LOGGER_NAME,))

    selected_latest_message = None
    if args.product_id is None:
        with Session(engine) as session:
            latest = session.exec(
                select(Product).order_by(col(Product.product_id).desc())
            ).first()
        if not latest or latest.product_id is None:
            logger.error("No products found in DB.")
            return 1
        product_id = latest.product_id
        selected_latest_message = (
            f"(No product_id given - using latest: {product_id} '{latest.name}')"
        )
    else:
        product_id = args.product_id

    result = apply_validation(product_id, mode=args.mode)
    _emit_run_logs(
        result,
        verbose=args.verbose,
        quiet=args.quiet,
        selected_latest_message=selected_latest_message,
    )
    return 0 if result.status in {"success", "noop"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
