"""Version metadata for agent workbench CLI envelopes."""

from importlib import metadata as importlib_metadata

COMMAND_VERSION = "1"
STORAGE_SCHEMA_VERSION = "8"


def agileforge_version() -> str:
    """Return the installed AgileForge package version."""
    try:
        return importlib_metadata.version("agileforge")
    except importlib_metadata.PackageNotFoundError:
        return "dev"
