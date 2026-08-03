"""
Automated persona correction for existing user stories.

This script identifies stories with incorrect/generic personas and replaces them
with domain-specific personas. Safe to run with dry_run=True for preview.

Usage:
    # Preview changes
    python scripts/fix_persona_drift.py --project-id 1 --dry-run

    # Apply changes
    python scripts/fix_persona_drift.py --project-id 1

    # Generate review report
    python scripts/fix_persona_drift.py --project-id 1 --report-only
"""

import argparse
import csv
import re

from sqlmodel import Session, select

from agile_sqlmodel import Project, UserStory, get_engine
from models.core import Feature
from utils.cli_output import emit

_MODEL_IMPORT_BOUNDARY = (Feature,)

# --- Configuration ---

# Persona substitution rules for automated fixes
PERSONA_FIXES = {
    "data annotator": "automation engineer",
    "software engineer": "automation engineer",
    "frontend developer": "automation engineer",
    "backend developer": "automation engineer",
    "developer": "automation engineer",
    "qa engineer": "engineering QA reviewer",
    "data scientist": "automation engineer",  # Unless ML context
    "user": "automation engineer",  # Default - may need manual review
}

_PERSONA_PATTERN = re.compile(r"\bAs an? (?P<persona>[^,]+),", re.IGNORECASE)


def extract_persona_from_story(description: str | None) -> str | None:
    """Extract the persona from a canonical user-story statement."""
    if not description:
        return None
    match = _PERSONA_PATTERN.search(description)
    if match is None:
        return None
    return match.group("persona").strip()


def detect_generic_personas(description: str | None) -> bool:
    """Return whether a story uses one of the generic personas this script fixes."""
    persona = extract_persona_from_story(description)
    return bool(persona and persona.lower().strip() in PERSONA_FIXES)


def _persona_article(persona: str) -> str:
    first = persona.strip()[:1].lower()
    return "an" if first in {"a", "e", "i", "o", "u"} else "a"


def auto_correct_persona(
    story_dict: dict[str, str],
    new_persona: str,
) -> dict[str, str]:
    """Replace the persona phrase in a story description."""
    description = story_dict.get("description", "")
    replacement = f"As {_persona_article(new_persona)} {new_persona},"
    corrected = _PERSONA_PATTERN.sub(replacement, description, count=1)
    return {**story_dict, "description": corrected}


def suggest_persona_replacement(current_persona: str, feature_title: str) -> str:
    """Suggest a domain persona from the current persona and feature context."""
    normalized = current_persona.lower().strip()
    if normalized in PERSONA_FIXES:
        return PERSONA_FIXES[normalized]
    lowered_feature = feature_title.lower()
    if "train" in lowered_feature or "model" in lowered_feature:
        return "ML engineer"
    return "automation engineer"


def analyze_project_personas(project_id: int) -> dict:
    """
    Analyze persona distribution in a project's stories.

    Args:
        project_id: Project ID to analyze

    Returns:
        Dict with persona statistics
    """
    with Session(get_engine()) as session:
        project = session.get(Project, project_id)
        if not project:
            msg = f"Project {project_id} not found"
            raise ValueError(msg)

        stories = session.exec(
            select(UserStory).where(UserStory.project_id == project_id)
        ).all()

        persona_counts = {}
        generic_count = 0
        no_persona_count = 0

        for story in stories:
            persona = extract_persona_from_story(story.story_description)

            if not persona:
                no_persona_count += 1
            else:
                persona_lower = persona.lower().strip()
                persona_counts[persona_lower] = persona_counts.get(persona_lower, 0) + 1

                if detect_generic_personas(story.story_description):
                    generic_count += 1

        return {
            "project_name": project.name,
            "total_stories": len(stories),
            "persona_distribution": persona_counts,
            "generic_persona_count": generic_count,
            "no_persona_count": no_persona_count,
        }


def fix_story_personas(  # noqa: C901, PLR0912
    project_id: int,
    target_persona: str = "automation engineer",
    dry_run: bool = True,
    verbose: bool = True,
) -> dict:
    """
    Fix personas in all stories for a project.

    Args:
        project_id: Project ID to fix
        target_persona: Default persona to use for generic replacements
        dry_run: If True, preview changes without saving
        verbose: Print detailed output

    Returns:
        Dict with fix statistics
    """
    with Session(get_engine()) as session:
        project = session.get(Project, project_id)
        if not project:
            msg = f"Project {project_id} not found"
            raise ValueError(msg)

        stories = session.exec(
            select(UserStory).where(UserStory.project_id == project_id)
        ).all()

        fixed_count = 0
        skipped_count = 0
        changes = []

        for story in stories:
            # Extract current persona
            current_persona = extract_persona_from_story(story.story_description)

            if not current_persona:
                skipped_count += 1
                if verbose:
                    emit(
                        f"⚠️  Story {story.story_id}: No persona found - SKIPPED (manual review needed)"  # noqa: E501
                    )
                continue

            # Check if persona needs fixing
            persona_lower = current_persona.lower().strip()

            # Determine correct persona
            if persona_lower in PERSONA_FIXES:
                # Use mapping for known generic personas
                new_persona = PERSONA_FIXES[persona_lower]

                # Check for ML context override
                feature_title = story.feature.title if story.feature else ""
                if "train" in feature_title.lower() or "model" in feature_title.lower():
                    new_persona = "ML engineer"

            elif persona_lower == target_persona.lower():
                # Already correct
                continue

            else:
                # Unknown persona - skip for manual review
                skipped_count += 1
                if verbose:
                    emit(
                        f"⚠️  Story {story.story_id}: Unknown persona '{current_persona}' - SKIPPED (manual review needed)"  # noqa: E501
                    )
                continue

            # Apply correction
            old_desc = story.story_description or ""
            story_dict = {"description": old_desc}
            corrected_dict = auto_correct_persona(story_dict, new_persona)
            new_desc = corrected_dict["description"]

            if old_desc != new_desc:
                change_record = {
                    "story_id": story.story_id,
                    "feature": story.feature.title if story.feature else "N/A",
                    "old_persona": current_persona,
                    "new_persona": new_persona,
                    "old_description": old_desc[:80] + "...",
                    "new_description": new_desc[:80] + "...",
                }
                changes.append(change_record)

                if verbose:
                    emit(
                        f"✅ Story {story.story_id}: '{current_persona}' → '{new_persona}'"  # noqa: E501
                    )

                if not dry_run:
                    story.story_description = new_desc
                    session.add(story)

                fixed_count += 1

        if not dry_run and fixed_count > 0:
            session.commit()
            if verbose:
                emit(f"\n💾 Committed {fixed_count} changes to database")

        return {
            "total_stories": len(stories),
            "fixed_count": fixed_count,
            "skipped_count": skipped_count,
            "changes": changes,
        }


def generate_review_report(
    project_id: int, output_file: str = "persona_review.csv"
) -> None:
    """
    Generate CSV report for manual persona review.

    Args:
        project_id: Project ID to report on
        output_file: Output CSV filename
    """
    with Session(get_engine()) as session:
        project = session.get(Project, project_id)
        if not project:
            msg = f"Project {project_id} not found"
            raise ValueError(msg)

        stories = session.exec(
            select(UserStory).where(UserStory.project_id == project_id)
        ).all()

        with open(output_file, "w", newline="", encoding="utf-8") as f:  # noqa: PTH123
            writer = csv.writer(f)
            writer.writerow(
                [
                    "Story ID",
                    "Feature",
                    "Current Persona",
                    "Is Generic?",
                    "Suggested Fix",
                    "Description",
                    "Acceptance Criteria",
                    "Review Status",
                ]
            )

            for story in stories:
                current_persona = extract_persona_from_story(story.story_description)
                is_generic = detect_generic_personas(story.story_description)

                # Suggest fix
                if current_persona and current_persona.lower() in PERSONA_FIXES:
                    suggested = PERSONA_FIXES[current_persona.lower()]
                elif is_generic:
                    feature_title = story.feature.title if story.feature else ""
                    suggested = suggest_persona_replacement(
                        current_persona or "", feature_title
                    )
                else:
                    suggested = "OK" if current_persona else "MISSING"

                writer.writerow(
                    [
                        story.story_id,
                        story.feature.title if story.feature else "N/A",
                        current_persona or "MISSING",
                        "YES" if is_generic else "NO",
                        suggested or "MANUAL_REVIEW",
                        (story.story_description or "")[:100],
                        story.acceptance_criteria[:100]
                        if story.acceptance_criteria
                        else "",
                        "PENDING",
                    ]
                )

    emit(f"📊 Review report saved to {output_file}")
    emit("   Open in Excel/Sheets for manual review")


def main() -> None:
    """Return main."""
    parser = argparse.ArgumentParser(
        description="Fix persona drift in user stories",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Preview changes
  python scripts/fix_persona_drift.py --project-id 1 --dry-run

  # Apply fixes
  python scripts/fix_persona_drift.py --project-id 1

  # Use custom default persona
  python scripts/fix_persona_drift.py --project-id 1 --persona "engineering QA reviewer"

  # Generate manual review report
  python scripts/fix_persona_drift.py --project-id 1 --report-only
        """,
    )

    parser.add_argument(
        "--project-id", type=int, required=True, help="Project ID to process"
    )
    parser.add_argument(
        "--persona",
        type=str,
        default="automation engineer",
        help="Default persona for replacements (default: automation engineer)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview changes without saving"
    )
    parser.add_argument(
        "--report-only", action="store_true", help="Generate CSV report only (no fixes)"
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress detailed output")

    args = parser.parse_args()

    try:
        if args.report_only:
            emit(f"Generating persona review report for project {args.project_id}...")
            generate_review_report(args.project_id)

        else:
            # Analyze first
            emit(f"Analyzing project {args.project_id}...\n")
            analysis = analyze_project_personas(args.project_id)

            emit(f"Project: {analysis['project_name']}")
            emit(f"Total Stories: {analysis['total_stories']}")
            emit(f"Generic Personas: {analysis['generic_persona_count']}")
            emit(f"Missing Personas: {analysis['no_persona_count']}")
            emit("\nPersona Distribution:")
            for persona, count in sorted(
                analysis["persona_distribution"].items(),
                key=lambda x: x[1],
                reverse=True,
            ):
                marker = "⚠️ " if persona in PERSONA_FIXES else "✅ "
                emit(f"  {marker}{persona}: {count}")

            emit(
                f"\n{'=' * 60}\n{'PREVIEW MODE - No changes will be saved' if args.dry_run else 'APPLYING FIXES'}\n{'=' * 60}\n"  # noqa: E501
            )

            # Fix personas
            stats = fix_story_personas(
                args.project_id,
                target_persona=args.persona,
                dry_run=args.dry_run,
                verbose=not args.quiet,
            )

            # Summary
            emit(f"\n{'=' * 60}")
            emit("SUMMARY")
            emit(f"{'=' * 60}")
            emit(f"Total Stories: {stats['total_stories']}")
            emit(f"Fixed: {stats['fixed_count']}")
            emit(f"Skipped (manual review): {stats['skipped_count']}")

            if args.dry_run and stats["fixed_count"] > 0:
                emit("\n💡 Run without --dry-run to apply these changes")

            if stats["skipped_count"] > 0:
                emit(f"\n⚠️  {stats['skipped_count']} stories require manual review")
                emit("   Run with --report-only to generate a review CSV")

    except Exception as e:
        emit(f"❌ Error: {e}")
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
