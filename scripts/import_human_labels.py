#!/usr/bin/env python3
"""Import human labels from JSONL/CSV and merge into benchmark cases JSONL."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from utils.cli_output import emit

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

KNOWN_REASON_CODES = {
    "SPEC_VERSION_EXISTS",
    "SPEC_PROJECT_MATCH",
    "SPEC_VERSION_COMPILED",
    "RULE_TITLE_REQUIRED",
    "RULE_ACCEPTANCE_CRITERIA_REQUIRED",
    "RULE_LLM_SPEC_VALIDATION",
    "FORBIDDEN_CAPABILITY",
    "LLM_SPEC_VALIDATION",
    "LLM_SPEC_VALIDATION_ERROR",
}


def _read_cases(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()  # noqa: PLW2901
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _write_cases(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True) + "\n")


def _as_text(raw: Any) -> str:  # noqa: ANN401
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return str(raw)


def _clean_optional_text(raw: Any) -> str | None:  # noqa: ANN401
    text = _as_text(raw).strip()
    return text or None


def _parse_bool(raw: Any) -> bool | None:  # noqa: ANN401
    if isinstance(raw, bool):
        return raw
    text = _as_text(raw).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    if text == "":
        return None
    msg = f"Invalid boolean value: {raw!r}"
    raise ValueError(msg)


def _parse_reasons(raw: Any) -> list[str]:  # noqa: ANN401
    if isinstance(raw, list):
        reasons = [str(part).strip() for part in raw if str(part).strip()]
        return sorted(set(reasons))
    text = _as_text(raw).strip()
    if not text:
        return []
    normalized = text.replace(";", ",").replace("|", ",")
    reasons = [part.strip() for part in normalized.split(",") if part.strip()]
    return sorted(set(reasons))


def _validate_reason_codes(
    reasons: Sequence[str], *, allow_unknown_reasons: bool
) -> None:
    if allow_unknown_reasons:
        return
    unknown = [reason for reason in reasons if reason not in KNOWN_REASON_CODES]
    if unknown:
        msg = f"Unknown reason codes: {unknown}. Use --allow-unknown-reasons to bypass."
        raise ValueError(
            msg
        )


def _validate_label_columns(columns: Iterable[str]) -> None:
    required = {"case_id", "rater_pass", "rater_fail_reasons", "rater_id"}
    missing = required.difference(set(columns))
    if missing:
        msg = f"Label file missing required column(s): {sorted(missing)}"
        raise ValueError(msg)


def _read_label_rows_csv(path: Path) -> dict[str, dict[str, Any]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        _validate_label_columns(reader.fieldnames or [])

        labels: dict[str, dict[str, Any]] = {}
        for row in reader:
            case_id = (row.get("case_id") or "").strip()
            if not case_id:
                continue
            labels[case_id] = row
        return labels


def _read_label_rows_jsonl(path: Path) -> dict[str, dict[str, Any]]:
    labels: dict[str, dict[str, Any]] = {}
    seen_columns: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for idx, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError as exc:
                msg = f"Invalid JSON on line {idx} in labels file: {path}"
                raise ValueError(
                    msg
                ) from exc
            if not isinstance(row, dict):
                msg = f"Labels JSONL line {idx} is not an object"
                raise ValueError(msg)  # noqa: TRY004
            seen_columns.update(row.keys())
            case_id = str(row.get("case_id", "")).strip()
            if not case_id:
                continue
            labels[case_id] = row
    _validate_label_columns(seen_columns)
    return labels


def _read_label_rows(path: Path) -> dict[str, dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _read_label_rows_csv(path)
    return _read_label_rows_jsonl(path)


def merge_human_labels(
    cases: Sequence[dict[str, Any]],
    labels_by_case: dict[str, dict[str, Any]],
    *,
    allow_unknown_reasons: bool,
) -> tuple[list[dict[str, Any]], int]:
    """Merge labeled rows into benchmark cases."""
    merged: list[dict[str, Any]] = []
    updated = 0
    now_iso = datetime.now(UTC).isoformat()

    for case in cases:
        updated_case = dict(case)
        label_row = labels_by_case.get(str(case.get("case_id")))
        if label_row is None:
            merged.append(updated_case)
            continue

        parsed_pass = _parse_bool(label_row.get("rater_pass", ""))
        if parsed_pass is None:
            merged.append(updated_case)
            continue

        reasons = _parse_reasons(label_row.get("rater_fail_reasons", ""))
        _validate_reason_codes(reasons, allow_unknown_reasons=allow_unknown_reasons)
        if parsed_pass and reasons:
            msg = f"Case {case.get('case_id')}: pass=true but fail reasons are not empty."  # noqa: E501
            raise ValueError(
                msg
            )

        updated_case["expected_pass"] = parsed_pass
        updated_case["expected_fail_reasons"] = reasons
        updated_case["label_source"] = "human_review"
        updated_case["rater_id"] = _clean_optional_text(label_row.get("rater_id"))
        updated_case["rater_confidence"] = _clean_optional_text(
            label_row.get("rater_confidence")
        )
        updated_case["notes"] = _clean_optional_text(label_row.get("rater_notes"))
        updated_case["labeled_at"] = now_iso
        updated += 1
        merged.append(updated_case)

    return merged, updated


def main() -> None:
    """Return main."""
    parser = argparse.ArgumentParser(
        description="Import human labels (JSONL/CSV) and merge into benchmark cases JSONL"  # noqa: E501
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path("artifacts") / "validation_benchmark" / "cases.jsonl",
        help="Input benchmark JSONL path",
    )
    parser.add_argument(
        "--labels",
        type=Path,
        required=True,
        help="Input labels file (JSONL or CSV) generated by export_benchmark_for_labeling.py",  # noqa: E501
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output JSONL path (defaults to overwrite --cases)",
    )
    parser.add_argument(
        "--allow-unknown-reasons",
        action="store_true",
        help="Allow reason codes outside KNOWN_REASON_CODES",
    )
    args = parser.parse_args()

    output_path = args.output or args.cases
    cases = _read_cases(args.cases)
    labels = _read_label_rows(args.labels)
    merged, updated = merge_human_labels(
        cases,
        labels,
        allow_unknown_reasons=args.allow_unknown_reasons,
    )
    _write_cases(output_path, merged)
    emit(f"Merged labels into {updated} case(s). Output: {output_path}")


if __name__ == "__main__":
    main()
