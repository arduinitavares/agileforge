# utils/platform_support.py
"""Refuse native AgileForge execution on anything but Linux.

AgileForge ships as a Linux container behind Docker Compose. Every process
entrypoint calls :func:`require_linux` before it parses arguments or touches a
profile so an unsupported host fails fast without creating state.
"""

from __future__ import annotations

import sys
from typing import Final

UNSUPPORTED_PLATFORM_EXIT_CODE: Final[int] = 2
_LINUX_PREFIX: Final[str] = "linux"
_DOCS_PATH: Final[str] = "docs/linux-containers.md"


class UnsupportedPlatformError(RuntimeError):
    """Raised when AgileForge is started natively outside Linux."""

    def __init__(self, platform: str) -> None:
        """Record the refused platform tag and build the user-facing message."""
        self.platform: str = platform
        super().__init__(unsupported_platform_message(platform))


def current_platform() -> str:
    """Return the running interpreter platform tag (patched in tests)."""
    return sys.platform


def is_linux(platform: str) -> bool:
    """Return whether the platform tag names a Linux interpreter."""
    return platform.startswith(_LINUX_PREFIX)


def unsupported_platform_message(platform: str) -> str:
    """Explain the refusal and point at the supported runtime."""
    return (
        "AgileForge runs only on Linux; this process started on "
        f"{platform!r}. Use the Docker Compose runtime (`docker compose up -d` "
        f"from the checkout) instead. See {_DOCS_PATH}."
    )


def require_linux(platform: str | None = None) -> None:
    """Raise :class:`UnsupportedPlatformError` unless running on Linux."""
    resolved = current_platform() if platform is None else platform
    if not is_linux(resolved):
        raise UnsupportedPlatformError(resolved)
