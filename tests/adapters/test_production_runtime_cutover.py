"""Production composition boundary tests for the atomic graph cutover."""

import json
import os
import subprocess  # nosec B404
import sys
from pathlib import Path


def test_production_composition_does_not_load_retired_runtime_code() -> None:
    """Build the v2 application without retired authority setup composition."""
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
deleted_root = "orchestrator" + "_agent"
legacy = sorted(
    name for name in sys.modules
    if name == deleted_root or name.startswith(f"{deleted_root}.")
)
brownfield = sorted(
    name for name in sys.modules
    if name == "adapters.adk.agents.brownfield"
    or name.startswith("adapters.adk.agents.brownfield.")
)
print(json.dumps({"legacy": legacy, "brownfield": brownfield,
                  "recipe_nodes": application._recipe_registry.node_ids}))
"""

    result = subprocess.run(  # nosec B603  # noqa: S603
        [sys.executable, "-c", script],
        cwd=root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == {
        "legacy": [],
        "brownfield": [],
        "recipe_nodes": [
            "authority.compile",
            "authority.repair",
            "vision.interview",
            "goal.interview",
            "backlog.generate",
            "planning.roadmap.generate",
            "planning.story.generate",
            "planning.sprint.plan",
        ],
    }
