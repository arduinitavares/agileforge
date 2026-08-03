"""Script for convert proposal."""

import importlib
import sys
from pathlib import Path
from typing import Any

from utils.cli_output import emit


def _load_docx() -> Any:  # noqa: ANN401
    try:
        return importlib.import_module("docx")
    except ModuleNotFoundError as exc:
        msg = "python-docx is required to convert DOCX proposals."
        raise RuntimeError(msg) from exc


docx = _load_docx()


def convert_to_md(docx_file: Path) -> Path:  # noqa: C901, PLR0912
    """Return convert to md."""
    if not docx_file.exists():
        emit(f"Error: File {docx_file} not found.")
        sys.exit(1)

    emit(f"Reading {docx_file}...")
    try:
        doc = docx.Document(docx_file)
    except Exception as e:  # noqa: BLE001
        emit(f"Failed to open document: {e}")
        sys.exit(1)

    md_lines = []

    # Extract paragraphs
    for para in doc.paragraphs:
        # Simple style mapping could be added here
        text = para.text.strip()
        if not text:
            continue

        style_name = para.style.name.lower()
        if "heading 1" in style_name:
            md_lines.append(f"# {text}")
        elif "heading 2" in style_name:
            md_lines.append(f"## {text}")
        elif "heading 3" in style_name:
            md_lines.append(f"### {text}")
        elif "list bullet" in style_name:
            md_lines.append(f"- {text}")
        elif "list number" in style_name:
            md_lines.append(f"1. {text}")  # Simple numbering
        else:
            md_lines.append(text)

    # Extract tables (basic)
    if doc.tables:
        md_lines.append("\n\n--- TABLES ---\n")
        for table in doc.tables:
            for row in table.rows:
                row_text = [cell.text.strip() for cell in row.cells]
                md_lines.append(" | ".join(row_text))
            md_lines.append("")  # Empty line between tables

    output_file = docx_file.with_suffix(".md")

    emit(f"Writing to {output_file}...")
    with open(output_file, "w", encoding="utf-8") as f:  # noqa: PTH123
        f.write("\n\n".join(md_lines))

    emit("Done.")
    return output_file


if __name__ == "__main__":
    docx_path = Path("Proposta_TCC_Alexandre_Tavares.docx").resolve()
    convert_to_md(docx_path)
