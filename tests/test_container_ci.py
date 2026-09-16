"""Guard the bounded Linux container CI contract."""

from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
IMPORT_COMMAND = "scripts/container.py --project-name agileforge-ci import-source"
GATE_COMMAND = (
    "scripts/container.py --project-name agileforge-ci exec -- ./agileforge-dev check"
)
PRODUCTION_REHEARSAL = "scripts/verify_container.py --image agileforge-production:ci"


def test_linux_container_gate_uses_a_named_volume_and_host_transport() -> None:
    """CI runs the canonical gate in a copied Linux checkout, never a bind mount."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "linux-container:" in workflow
    assert "docker volume create agileforge-ci-workspace" in workflow
    assert IMPORT_COMMAND in workflow
    assert GATE_COMMAND in workflow
    assert PRODUCTION_REHEARSAL in workflow
    assert "type=bind" not in workflow
