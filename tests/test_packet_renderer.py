"""Closed direct-Spec packet renderer tests."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

import pytest
from sqlmodel import Session

from services.packet_renderer import PacketRenderError, render_packet
from services.packets.canonical import build_story_packet, build_task_packet
from services.read_projections import DurableReadProjectionService
from tests.workflow.execution_fixtures import seed_started_execution
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from workflow.contracts import JsonObject, JsonValue


def _object(value: JsonValue) -> JsonObject:
    assert isinstance(value, dict)
    return value


def _items(value: JsonValue) -> list[JsonValue]:
    assert isinstance(value, list)
    return value


def _packets(engine: Engine) -> tuple[JsonObject, JsonObject]:
    project_id, sprint_id, story_id, task_id = seed_started_execution(engine)
    with Session(engine) as session:
        return (
            build_story_packet(
                session,
                project_id=project_id,
                sprint_id=sprint_id,
                story_id=story_id,
            ),
            build_task_packet(
                session,
                project_id=project_id,
                sprint_id=sprint_id,
                task_id=task_id,
            ),
        )


def _with_solo_owner(packet: JsonObject) -> JsonObject:
    projected = copy.deepcopy(packet)
    context = _object(projected["context"])
    project = _object(context["project"])
    sprint = _object(context["sprint"])
    owner_key = (
        f"agileforge:sprint-owner:solo-project:v1:project:{project['project_id']}"
    )
    sprint["owner_kind"] = "solo_project"
    sprint["owner_key"] = owner_key
    sprint["team_name"] = f"[{owner_key}] Solo operator for String Calculator Lab"
    _object(projected["metadata"])["source_fingerprint"] = canonical_hash(
        {key: projected[key] for key in ("lineage", "context", "evidence", "work")}
    )
    return projected


def test_human_renderer_shows_exact_language_without_machine_identity(
    engine: Engine,
) -> None:
    """Human output exposes evidence and contracts, never raw durable identity."""
    story, task = _packets(engine)
    story_text = render_packet(story, "human")
    task_text = render_packet(task, "human")

    assert "Specification: current" in story_text
    assert "Backlog requirement:" in story_text
    assert "Roadmap release:" in story_text
    assert "Story acceptance criteria:" in story_text
    assert "Level: MUST" in story_text
    assert "Verification: acceptance-test" in story_text
    assert "The Roadmap references Plan immutable work exactly once." in story_text
    assert "Task checklist:" in task_text
    assert (
        "Sprint owner: Legacy named team — Task 12 normalized execution team"
        in story_text
    )
    assert "Team:" not in story_text
    assert "Team:" not in task_text
    for forbidden in (
        "sha256:",
        "spec_version_id",
        "artifact_id",
        "fingerprint",
        "instance_key",
        "story_id",
        "task_id",
    ):
        assert forbidden not in story_text
        assert forbidden not in task_text


def test_human_story_and_task_renderers_hide_solo_owner_key(
    engine: Engine,
) -> None:
    """Human packet text uses display ownership without changing packet bytes."""
    raw_story, raw_task = _packets(engine)
    story = _with_solo_owner(raw_story)
    task = _with_solo_owner(raw_task)
    story_bytes = canonical_json(story)
    task_bytes = canonical_json(task)

    story_text = render_packet(story, "human")
    task_text = render_packet(task, "human")

    for rendered in (story_text, task_text):
        assert (
            "Sprint owner: Solo project — Solo operator for String Calculator Lab"
            in rendered
        )
        assert "agileforge:sprint-owner:" not in rendered
    assert canonical_json(story) == story_bytes
    assert canonical_json(task) == task_bytes


def test_agent_renderer_keeps_domain_evidence_but_not_internal_lineage(
    engine: Engine,
) -> None:
    """Agent output keeps work evidence while omitting database/receipt internals."""
    story, task = _packets(engine)
    story_text = render_packet(story, "agent")
    task_text = render_packet(task, "agent")

    assert "<execution_packet>" in story_text
    assert '<item id="REQ.' in story_text
    assert 'level="MUST"' in story_text
    assert 'verification_method="acceptance-test"' in story_text
    assert "<criterion>The Roadmap references Plan immutable work exactly once." in (
        story_text
    )
    assert "<acceptance_criteria>" in story_text
    assert "<checklist>" in task_text
    assert (
        '<sprint_owner kind="legacy_named_team">'
        "Task 12 normalized execution team</sprint_owner>"
    ) in story_text
    for forbidden in ("sha256:", "artifact_id", "fingerprint", "instance_key"):
        assert forbidden not in story_text
        assert forbidden not in task_text


@pytest.mark.parametrize("flavor", ["markdown", "brief", "cursor", "xml", "bogus"])
def test_renderer_rejects_every_noncanonical_flavor(
    engine: Engine,
    flavor: str,
) -> None:
    """Aliases and unknown flavor values cannot silently select a prompt."""
    story, _task = _packets(engine)
    with pytest.raises(PacketRenderError) as error:
        render_packet(story, flavor)
    assert error.value.code == "PACKET_FLAVOR_UNSUPPORTED"


@pytest.mark.parametrize(
    ("schema", "kind"),
    [
        ("story_packet." + "v1", "story"),
        ("task_packet." + "v3", "task"),
        ("story_packet.v3", "task"),
        ("unknown", "story"),
    ],
)
def test_renderer_rejects_old_unknown_or_mismatched_schema(
    engine: Engine,
    schema: str,
    kind: str,
) -> None:
    """Schema and packet kind form one closed discriminator pair."""
    story, _task = _packets(engine)
    story["schema_version"] = schema
    story["packet_kind"] = kind
    with pytest.raises(PacketRenderError) as error:
        render_packet(story, "human")
    assert error.value.code == "PACKET_SCHEMA_UNSUPPORTED"


@pytest.mark.parametrize("mutation", ["missing", "unknown", "malformed", "nonfinite"])
def test_renderer_rejects_incomplete_unknown_malformed_or_nonfinite_packet(
    engine: Engine,
    mutation: str,
) -> None:
    """Every supported discriminator still requires the complete closed schema."""
    story, _task = _packets(engine)
    invalid = copy.deepcopy(story)
    if mutation == "missing":
        invalid.pop("work")
    elif mutation == "unknown":
        invalid["unexpected"] = "value"
    elif mutation == "malformed":
        invalid["context"] = []
    else:
        sprint = _object(_object(invalid["context"])["sprint"])
        sprint["goal"] = float("nan")

    with pytest.raises(PacketRenderError) as error:
        render_packet(invalid, "human")

    assert error.value.code == "PACKET_CONTENT_INVALID"


def test_flavored_read_returns_separate_view_and_empty_flavor_is_rejected(
    engine: Engine,
) -> None:
    """Rendering never mutates the seven-key packet and every supplied flavor closes."""
    project_id, sprint_id, story_id, _task_id = seed_started_execution(engine)
    reads = DurableReadProjectionService(engine=engine)

    canonical = reads.story_packet(
        project_id=project_id,
        sprint_id=sprint_id,
        story_id=story_id,
    )
    flavored = reads.story_packet(
        project_id=project_id,
        sprint_id=sprint_id,
        story_id=story_id,
        flavor="human",
    )
    empty = reads.story_packet(
        project_id=project_id,
        sprint_id=sprint_id,
        story_id=story_id,
        flavor="",
    )

    assert canonical["ok"] is True
    assert flavored["ok"] is True
    canonical_packet = _object(canonical["data"])
    flavored_view = _object(flavored["data"])
    assert list(canonical_packet) == [
        "schema_version",
        "packet_kind",
        "metadata",
        "lineage",
        "context",
        "evidence",
        "work",
    ]
    assert flavored_view["packet"] == canonical_packet
    assert isinstance(flavored_view["render"], str)
    assert "render" not in canonical_packet
    assert empty["ok"] is False
    errors = _items(empty["errors"])
    assert _object(errors[0])["code"] == "PACKET_FLAVOR_UNSUPPORTED"
