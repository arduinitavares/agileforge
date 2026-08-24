#!/usr/bin/env python3
"""
Generate Synthetic Hard Negative Benchmark Cases.

Derives new failure cases from existing passing cases by injecting specific faults.
Targets: p7, p9, p10.
"""

import argparse
import json
import random

# Add repo root to path
import sys
from pathlib import Path
from typing import Any

from utils.cli_output import emit

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))

# Configuration for mutations
MUTATIONS = {
    "FORBIDDEN_CAPABILITY": [
        "The system shall provide real-time collaboration features.",
        "Users can access the public API for integration.",
        "The platform supports cryptocurrency payments.",
        "Include a cloud dashboard for remote management.",
        "Enable voice assistant integration.",
    ],
    "REQUIRED_FIELD_MISSING": [
        # Strategy: Wipe the Acceptance Criteria
        "WIPE_AC"
    ],
    "SCOPE_CREEP": [
        "As a user, I want to manage physical inventory in the warehouse.",
        "As a driver, I want to optimize shipping routes.",
        "As a user, I want to stream video from my security cameras.",
        "As a user, I want to auction my items to the highest bidder.",
    ],
    "CONTRADICTION": [
        "The story points for this task are 100.",  # Violates MAX_VALUE
        "Latency must be under 0ms (impossible).",
        "The system must work offline but requires cloud sync.",
    ],
}
EXPECTED_OUTCOME_FIELD = "expected_pass"
REVIEW_OUTCOME_FIELD = "rater_pass"
# Synthetic case generation is intentionally deterministic, not security-sensitive.
_SYNTHETIC_RANDOM = random.Random()  # noqa: S311  # nosec B311


def load_cases(path: Path) -> list[dict[str, Any]]:
    """Return load cases."""
    cases = []
    with open(path) as f:  # noqa: PTH123
        for line in f:
            if line.strip():
                cases.append(json.loads(line))  # noqa: PERF401
    return cases


def mutate_case(
    case: dict[str, Any], mutation_type: str, mutation_payload: str
) -> dict[str, Any]:
    """Return mutate case."""
    new_case = case.copy()
    # Generate new ID
    new_case["case_id"] = f"syn-{case.get('case_id', 'nocase')}-{mutation_type[:3]}"
    new_case["tags"] = ["synthetic", mutation_type]
    new_case[EXPECTED_OUTCOME_FIELD] = False
    new_case["label_source"] = "synthetic_mutation"
    new_case["enabled"] = True
    new_case["expected_fail_reasons"] = []  # Reset

    # Apply Mutation
    if mutation_type == "FORBIDDEN_CAPABILITY":
        new_case["story_description"] = (
            new_case.get("story_description", "") + " " + mutation_payload
        ).strip()
        new_case["expected_fail_reasons"] = ["FORBIDDEN_CAPABILITY"]

    elif mutation_type == "REQUIRED_FIELD_MISSING":
        if mutation_payload == "WIPE_AC":
            new_case["acceptance_criteria"] = ""  # Empty AC
            new_case["expected_fail_reasons"] = ["RULE_ACCEPTANCE_CRITERIA_REQUIRED"]

    elif mutation_type == "SCOPE_CREEP":
        new_case["story_title"] = mutation_payload
        new_case["story_description"] = "Out of scope feature request."
        new_case["expected_fail_reasons"] = ["RULE_LLM_SPEC_VALIDATION"]

    elif mutation_type == "CONTRADICTION":
        new_case["acceptance_criteria"] = (
            new_case.get("acceptance_criteria", "") + "\n" + mutation_payload
        ).strip()
        new_case["expected_fail_reasons"] = (
            ["MAX_VALUE"]
            if "points" in mutation_payload
            else ["RULE_LLM_SPEC_VALIDATION"]
        )

    return new_case


def generate_synthetic_cases(
    source_cases: list[dict[str, Any]], count: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # Filter for passing cases to mutate (from cases_for_labeling, we check rater_pass or expected_pass)  # noqa: E501
    """Return generate synthetic cases."""
    passing_cases = [
        c
        for c in source_cases
        if (
            c.get(REVIEW_OUTCOME_FIELD) is True or c.get(EXPECTED_OUTCOME_FIELD) is True
        )
    ]

    if not passing_cases:
        emit("No passing cases found to mutate. Using all cases.")
        passing_cases = source_cases

    generated_cases = []  # For cases.jsonl format
    synthetic_stories = []  # For DB hydration

    emit(f"Found {len(passing_cases)} source cases.")

    # P7 Mutations
    # Shift synthetic ID range to 9100+ to avoid collision with base 9001
    for i in range(count):
        source = _SYNTHETIC_RANDOM.choice(passing_cases)
        m_type = _SYNTHETIC_RANDOM.choice(list(MUTATIONS.keys()))
        m_payload = _SYNTHETIC_RANDOM.choice(MUTATIONS[m_type])

        syn_data = mutate_case(source, m_type, m_payload)
        syn_id = 9100 + i  # Changed from 7000 to be safe
        project_id = source.get("project_id", 7)

        syn_case_record = {
            "case_id": f"p7-syn-{i}",
            "story_id": syn_id,
            "spec_version_id": source["spec_version_id"],
            EXPECTED_OUTCOME_FIELD: False,
            "expected_fail_reasons": syn_data["expected_fail_reasons"],
            "notes": f"Mutation: {m_type}",
            "tags": ["synthetic", m_type],
            "enabled": True,
            "project_id": project_id,
            "story_title": syn_data["story_title"],
            "spec_source": "synthetic",
            "label_source": "synthetic",
        }

        syn_story_record = {
            "story_id": syn_id,
            "project_id": project_id,
            "title": syn_data["story_title"],
            "description": syn_data.get("story_description", ""),
            "acceptance_criteria": syn_data.get("acceptance_criteria", ""),
        }

        generated_cases.append(syn_case_record)
        synthetic_stories.append(syn_story_record)

    # P9/P10 Setup
    p9_base = {
        "project_id": 9,
        "spec_version_id": 901,
        "story_title": "Add item to cart",
        "story_description": "As a user I want to add items.",
        "acceptance_criteria": "Given item, When add, Then in cart.",
        REVIEW_OUTCOME_FIELD: True,
    }

    p10_base = {
        "project_id": 10,
        "spec_version_id": 1001,
        "story_title": "Turn on light",
        "story_description": "As a user I want to turn on lights.",
        "acceptance_criteria": "Given light off, When toggle, Then on.",
        REVIEW_OUTCOME_FIELD: True,
    }

    # Generate P9 Failures (ID range 9200+)
    for i in range(5):
        m_type = _SYNTHETIC_RANDOM.choice(list(MUTATIONS.keys()))
        m_payload = _SYNTHETIC_RANDOM.choice(MUTATIONS[m_type])
        syn_data = mutate_case(p9_base, m_type, m_payload)
        syn_id = 9200 + i

        syn_case_record = {
            "case_id": f"p9-syn-{i}",
            "story_id": syn_id,
            "spec_version_id": 901,
            EXPECTED_OUTCOME_FIELD: False,
            "expected_fail_reasons": syn_data["expected_fail_reasons"],
            "notes": f"Mutation: {m_type}",
            "tags": ["synthetic", m_type],
            "enabled": True,
            "project_id": 9,
            "story_title": syn_data["story_title"],
            "spec_source": "synthetic",
            "label_source": "synthetic",
        }
        syn_story_record = {
            "story_id": syn_id,
            "project_id": 9,
            "title": syn_data["story_title"],
            "description": syn_data.get("story_description", ""),
            "acceptance_criteria": syn_data.get("acceptance_criteria", ""),
        }
        generated_cases.append(syn_case_record)
        synthetic_stories.append(syn_story_record)

    # Generate P10 Failures (ID range 10200+)
    for i in range(5):
        m_type = _SYNTHETIC_RANDOM.choice(list(MUTATIONS.keys()))
        m_payload = _SYNTHETIC_RANDOM.choice(MUTATIONS[m_type])
        syn_data = mutate_case(p10_base, m_type, m_payload)
        syn_id = 10200 + i

        syn_case_record = {
            "case_id": f"p10-syn-{i}",
            "story_id": syn_id,
            "spec_version_id": 1001,
            EXPECTED_OUTCOME_FIELD: False,
            "expected_fail_reasons": syn_data["expected_fail_reasons"],
            "notes": f"Mutation: {m_type}",
            "tags": ["synthetic", m_type],
            "enabled": True,
            "project_id": 10,
            "story_title": syn_data["story_title"],
            "spec_source": "synthetic",
            "label_source": "synthetic",
        }
        syn_story_record = {
            "story_id": syn_id,
            "project_id": 10,
            "title": syn_data["story_title"],
            "description": syn_data.get("story_description", ""),
            "acceptance_criteria": syn_data.get("acceptance_criteria", ""),
        }
        generated_cases.append(syn_case_record)
        synthetic_stories.append(syn_story_record)

    return generated_cases, synthetic_stories


def main() -> None:
    """Return main."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        type=Path,
        default="artifacts/validation_benchmark/cases_for_labeling.jsonl",
    )
    parser.add_argument(
        "--output-cases",
        type=Path,
        default="artifacts/validation_benchmark/cases.expanded.jsonl",
    )
    parser.add_argument(
        "--output-stories",
        type=Path,
        default="artifacts/validation_benchmark/synthetic_stories.jsonl",
    )
    parser.add_argument("--count", type=int, default=20)
    args = parser.parse_args()

    _SYNTHETIC_RANDOM.seed(42)  # Deterministic generation

    source_cases = load_cases(args.input)
    synthetic_cases, synthetic_stories = generate_synthetic_cases(
        source_cases, args.count
    )

    final_cases = []
    source_story_records = []

    for sc in source_cases:
        pass_val = sc.get(REVIEW_OUTCOME_FIELD)
        fail_reasons = sc.get("rater_fail_reasons", "")
        if isinstance(fail_reasons, str):
            fail_reasons = [r.strip() for r in fail_reasons.split(",") if r.strip()]

        # FIX: Ensure project_id is correctly mapped for P8 cases
        # cases_for_labeling has no project_id, so default to 7 (Quadra).
        # But if case_id starts with 'p8', it's Project 8.
        project_id = sc.get("project_id", 7)
        if str(sc.get("case_id", "")).startswith("p8-"):
            project_id = 8

        final_cases.append(
            {
                "case_id": sc["case_id"],
                "story_id": sc["story_id"],
                "spec_version_id": sc["spec_version_id"],
                EXPECTED_OUTCOME_FIELD: pass_val,
                "expected_fail_reasons": fail_reasons,
                "notes": sc.get("rater_notes"),
                "tags": ["real-data"],
                "enabled": True,
                "project_id": project_id,
                "story_title": sc["story_title"],
                "spec_source": "real",
                "label_source": "human_review",
            }
        )

        source_story_records.append(
            {
                "story_id": sc["story_id"],
                "project_id": project_id,
                "title": sc["story_title"],
                "description": sc.get("story_description", ""),
                "acceptance_criteria": sc.get("acceptance_criteria", ""),
            }
        )

    final_cases.extend(synthetic_cases)
    final_story_records = source_story_records + synthetic_stories

    # Add base P9/P10 passing cases (IDs 9001, 10001)
    final_cases.append(
        {
            "case_id": "p9-base",
            "story_id": 9001,
            "spec_version_id": 901,
            EXPECTED_OUTCOME_FIELD: True,
            "project_id": 9,
            "story_title": "Base P9",
            "expected_fail_reasons": [],
            "tags": ["synthetic-base"],
            "enabled": True,
            "spec_source": "synthetic",
            "label_source": "synthetic",
        }
    )
    final_story_records.append(
        {
            "story_id": 9001,
            "project_id": 9,
            "title": "Base P9",
            "description": "As a user I want to add items.",
            "acceptance_criteria": "Given item, When add, Then in cart.",
        }
    )

    final_cases.append(
        {
            "case_id": "p10-base",
            "story_id": 10001,
            "spec_version_id": 1001,
            EXPECTED_OUTCOME_FIELD: True,
            "project_id": 10,
            "story_title": "Base P10",
            "expected_fail_reasons": [],
            "tags": ["synthetic-base"],
            "enabled": True,
            "spec_source": "synthetic",
            "label_source": "synthetic",
        }
    )
    final_story_records.append(
        {
            "story_id": 10001,
            "project_id": 10,
            "title": "Base P10",
            "description": "As a user I want to turn on lights.",
            "acceptance_criteria": "Given light off, When toggle, Then on.",
        }
    )

    with open(args.output_cases, "w") as f:  # noqa: PTH123
        for c in final_cases:
            f.write(json.dumps(c) + "\n")

    with open(args.output_stories, "w") as f:  # noqa: PTH123
        for s in final_story_records:
            f.write(json.dumps(s) + "\n")

    emit(f"Generated {len(synthetic_cases)} synthetic cases.")
    emit(f"Total benchmark size: {len(final_cases)}")
    emit(f"Total stories to hydrate: {len(final_story_records)}")


if __name__ == "__main__":
    main()
