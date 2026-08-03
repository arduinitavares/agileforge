"""Seed the 'gestor da arena' persona for product ID 4."""

import sys
from pathlib import Path

from utils.cli_output import emit

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlmodel import Session, select

from agile_sqlmodel import engine
from models.core import ProjectPersona


def seed_arena_persona() -> None:
    """Add 'gestor da arena' persona to product ID 4."""
    with Session(engine) as session:
        # Check if persona already exists
        existing = session.exec(
            select(ProjectPersona).where(
                ProjectPersona.project_id == 4,  # noqa: PLR2004
                ProjectPersona.persona_name == "gestor da arena",
            )
        ).first()

        if existing:
            emit("✓ Persona 'gestor da arena' already exists for product ID 4")
            return

        # Create new persona
        persona = ProjectPersona(
            project_id=4,
            persona_name="gestor da arena",
            is_default=True,
            category="primary_user",
            description="Arena manager responsible for operational compliance and monitoring",  # noqa: E501
        )

        session.add(persona)
        session.commit()
        emit("✓ Persona 'gestor da arena' added successfully for product ID 4")


if __name__ == "__main__":
    seed_arena_persona()
