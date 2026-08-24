"""CLI for generating project snapshot HTML exports."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from utils.cli_output import emit

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))

from typing import TYPE_CHECKING  # noqa: E402

from tools.export_snapshot import export_project_snapshot_html  # noqa: E402

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def export_snapshot_command(
    *,
    project_id: int,
    output_dir: Path,
    engine_override: Engine | None = None,
) -> Path:
    """Generate a snapshot HTML file for a Project.

    Args:
        project_id: Project identifier.
        output_dir: Destination folder for export.
        engine_override: Optional SQLAlchemy engine for testing.

    Returns:
        Path to the generated HTML file.
    """
    return export_project_snapshot_html(
        project_id=project_id,
        output_dir=output_dir,
        engine_override=engine_override,
    )


def main(argv: list[str] | None = None) -> int:
    """Return main."""
    parser = argparse.ArgumentParser(description="Export project snapshot HTML")
    parser.add_argument("--project-id", type=int, required=True, help="Project ID")
    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(Path("artifacts") / "exports"),
        help="Output folder for snapshot HTML",
    )
    args = parser.parse_args(argv)

    output_path = export_snapshot_command(
        project_id=args.project_id,
        output_dir=Path(args.output_dir),
    )
    emit(f"Snapshot written: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
