"""Whole-tree absence regression for the retired project setup vocabulary."""

from __future__ import annotations

import os
from pathlib import Path

from git import Repo

RETIRED_LABELS = ("brown" + "field", "green" + "field")
_ROOT = Path(__file__).resolve().parents[1]


def test_retired_labels_absent_from_tracked_paths_and_content() -> None:
    """Reject retired labels in every tracked path and UTF-8 text file."""
    paths = tuple(
        Path(path) for (path, stage) in Repo(_ROOT).index.entries if stage == 0
    )
    offenders: list[str] = []
    for path in paths:
        folded_path = os.fsdecode(os.fsencode(path)).casefold()
        if any(label in folded_path for label in RETIRED_LABELS):
            offenders.append(str(path))
            continue
        try:
            content = (_ROOT / path).read_text(encoding="utf-8")
        except (FileNotFoundError, UnicodeDecodeError, IsADirectoryError):
            continue
        if any(label in content.casefold() for label in RETIRED_LABELS):
            offenders.append(str(path))
    assert offenders == [], "\n".join(offenders)
