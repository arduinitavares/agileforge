"""Production composition boundary tests for the atomic graph cutover."""

import os
import subprocess  # nosec B404
import sys
from pathlib import Path


def test_production_composition_does_not_load_legacy_authority() -> None:
    """Build the real eight-leaf application without importing old runtime code."""
    root = Path(__file__).parents[2]
    environment = dict(os.environ)
    environment.update(
        {
            "MODEL_CONFIG_PATH": str(root / "config" / "models.test.yaml"),
            "RELAX_ZDR_FOR_TESTS": "true",
            "OPENROUTER_API_KEY": "offline-test-key",
            "AGILEFORGE_DB_URL": "sqlite:///:memory:",
            "AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL": (
                "sqlite:////tmp/agileforge-task16-test-trace.sqlite3"
            ),
        }
    )
    script = """
import json
import sys
from services.application import production_application

def deny_network(event, args):
    if event not in {"socket.connect", "socket.getaddrinfo"}:
        return
    raise AssertionError("production composition attempted network access")

sys.addaudithook(deny_network)
application = production_application()
legacy = sorted(
    name for name in sys.modules
    if name == "orchestrator_agent" or name.startswith("orchestrator_agent.")
)
print(json.dumps(legacy))
"""

    result = subprocess.run(  # nosec B603  # noqa: S603
        [sys.executable, "-c", script],
        cwd=root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "[]"
