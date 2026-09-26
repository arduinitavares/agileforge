"""Fixed non-secret controls shared by launcher and runtime children."""

from typing import Final

LAUNCHER_CHILD_ENV: Final[str] = "AGILEFORGE_LAUNCHER_CHILD"
LAUNCHER_CHILD_VALUE: Final[str] = "1"
SPECIFICATION_STRUCTURER_MAX_TOKENS_DEFAULT: Final[int] = 32_768
VISION_MAX_TOKENS_DEFAULT: Final[int] = 128_000
VISION_TIMEOUT_SECONDS: Final[int] = 600
VISION_LEASE_SECONDS: Final[int] = 660
UI_LAUNCH_NONCE_ENV: Final[str] = "AGILEFORGE_UI_LAUNCH_NONCE"
