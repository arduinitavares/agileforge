"""Guard Compose ownership and namespace ordering in helper runbooks."""

import re
import shlex
from pathlib import Path

import pytest
import yaml

ROOT: Path = Path(__file__).resolve().parents[1]
RUNBOOK: Path = ROOT / "docs/linux-containers.md"
REPAIR_HEADING: str = "### Repair ownership warnings for existing volumes"


def _section(heading: str) -> str:
    text = RUNBOOK.read_text(encoding="utf-8")
    section = text.split(heading + "\n", 1)[1]
    return re.split(r"^#{2,3} ", section, maxsplit=1, flags=re.MULTILINE)[0]


def _commands(section: str) -> list[list[str]]:
    blocks = re.findall(r"```sh\n(.*?)```", section, flags=re.DOTALL)
    return [
        shlex.split(line)
        for block in blocks
        for line in block.replace("\\\n", " ").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


@pytest.mark.parametrize(
    ("heading", "service", "profile"),
    [
        ("## Test image and canonical gate", "development", "development"),
        ("### Promote a development profile into the packaged app", "production", None),
    ],
)
def test_compose_creates_managed_volumes_before_helper_mounts(
    heading: str, service: str, profile: str | None
) -> None:
    """Helpers must not create unlabeled volumes or recreate active services."""
    section = _section(heading)
    commands = _commands(section)
    creates = [
        (index, command)
        for index, command in enumerate(commands)
        if command[:2] == ["docker", "compose"] and "create" in command
    ]
    assert creates, f"{service} needs Compose creation before helper mounts"
    create_index, create = creates[0]
    assert create[create.index("--project-name") + 1] == "$project_name"
    assert create[create.index("create") + 1 :] == ["--no-recreate", service]
    if profile:
        assert create[create.index("--profile") + 1] == profile
    helpers = [
        index
        for index, command in enumerate(commands)
        if command[:2] == ["docker", "run"] and "--mount" in command
    ]
    assert helpers
    assert all(create_index < index for index in helpers)
    if service == "production":
        assert section.index("create --no-recreate production") < section.index(
            "Copy the bundle"
        ), "Compose must create volumes before copying, not only before chown"


def test_promotion_restore_attach_and_startup_select_same_project() -> None:
    """A nondefault helper namespace must also select restore and startup."""
    commands = _commands(
        _section("### Promote a development profile into the packaged app")
    )
    subsequent = [
        command
        for command in commands
        if command[:2] == ["docker", "compose"]
        and ("run" in command or "up" in command)
    ]
    assert subsequent
    for command in subsequent:
        assert "--project-name" in command, f"Missing project scope: {command}"
        assert command[command.index("--project-name") + 1] == "$project_name"


@pytest.mark.parametrize("volume", ["workspace", "cache", "production-state"])
def test_shipped_durable_volumes_remain_compose_managed(volume: str) -> None:
    """Fresh installs retain Compose ownership and project-scoped volume names."""
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    declaration = compose["volumes"][volume]
    assert not declaration.get("external", False)
    assert declaration["name"] == "${COMPOSE_PROJECT_NAME:-agileforge}-" + volume


def test_legacy_repair_override_only_externalizes_existing_durable_volumes() -> None:
    """A local repair must preserve base names and fresh-install ownership."""
    text = RUNBOOK.read_text(encoding="utf-8")
    assert REPAIR_HEADING in text, "Missing existing-volume configuration repair"
    section = _section(REPAIR_HEADING)
    blocks = re.findall(r"```yaml\n(.*?)```", section, flags=re.DOTALL)
    assert blocks, "Repair needs a parseable local override"
    override = yaml.safe_load(blocks[0])
    assert override == {
        "volumes": {
            "workspace": {"external": True},
            "production-state": {"external": True},
        }
    }, "Only affected durable volumes may become external; inherit base names"
    base = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    for volume in override["volumes"]:
        assert not base["volumes"][volume].get("external", False)
        assert base["volumes"][volume]["name"] == (
            "${COMPOSE_PROJECT_NAME:-agileforge}-" + volume
        )


def test_legacy_repair_inspects_and_verifies_same_project_persistently() -> None:
    """Repair must inspect existing labels and include the override in verification."""
    text = RUNBOOK.read_text(encoding="utf-8")
    assert REPAIR_HEADING in text, "Missing existing-volume configuration repair"
    section = _section(REPAIR_HEADING)
    commands = _commands(section)
    inspections = [c for c in commands if c[:3] == ["docker", "volume", "inspect"]]
    assert {c[-1] for c in inspections} == {
        "${project_name}-workspace",
        "${project_name}-production-state",
    }
    for command in inspections:
        assert ".Labels" in command[command.index("--format") + 1]
    verification = [
        c for c in commands if c[:2] == ["docker", "compose"] and "config" in c
    ]
    assert verification, "Review the merged configuration before startup"
    for command in verification:
        assert command[command.index("--project-name") + 1] == "$project_name"
        files = [
            command[i + 1] for i, argument in enumerate(command) if argument == "-f"
        ]
        assert files == ["compose.yaml", "compose.override.yaml"]
