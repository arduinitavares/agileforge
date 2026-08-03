"""Prompt sources owned by the Google ADK adapter."""

from importlib.resources import files


def load_prompt(name: str) -> str:
    """Load a packaged ADK prompt by resource name."""
    return files(__package__).joinpath(name).read_text(encoding="utf-8")


__all__ = ["load_prompt"]
