"""Provider-free tests for exact immutable planning lineage selection."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, Mock

import pytest

if TYPE_CHECKING:
    from types import ModuleType

    from services.planning_lineage import ArtifactLineageNode


def _lineage_module() -> ModuleType:
    """Import the Task 3 lineage service only when a test executes."""
    return importlib.import_module("services.planning_lineage")


def _accepted_chain(length: int, *, reverse: bool) -> tuple[ArtifactLineageNode, ...]:
    lineage = _lineage_module()
    nodes = tuple(
        lineage.ArtifactLineageNode(
            artifact_id=version,
            chain_key=("long-chain",),
            version_number=version,
            supersedes_artifact_id=version - 1 if version > 1 else None,
            decision="accepted",
        )
        for version in range(1, length + 1)
    )
    return tuple(reversed(nodes)) if reverse else nodes


@pytest.mark.parametrize("reverse", [False, True], ids=["oldest-first", "newest-first"])
def test_cycle_check_visits_each_parent_at_most_once(*, reverse: bool) -> None:
    """Bound parent lookups independently of machine speed and input order."""
    lineage = _lineage_module()
    nodes = _accepted_chain(128, reverse=reverse)
    by_id = Mock(wraps={node.artifact_id: node for node in nodes})

    lineage._reject_cycles(nodes, by_id)

    assert by_id.get.call_count <= len(nodes) - 1


@pytest.mark.parametrize("reverse", [False, True], ids=["oldest-first", "newest-first"])
def test_accepted_ancestry_visits_each_parent_at_most_once(*, reverse: bool) -> None:
    """Avoid rewalking accepted history already covered by another descendant."""
    lineage = _lineage_module()
    nodes = _accepted_chain(128, reverse=reverse)
    by_id = MagicMock(spec_set=dict)
    by_id.__getitem__.side_effect = {
        node.artifact_id: node for node in nodes
    }.__getitem__

    superseded = lineage._accepted_ancestor_ids(nodes, by_id)

    assert superseded == frozenset(range(1, len(nodes)))
    assert by_id.__getitem__.call_count <= len(nodes) - 1


@pytest.mark.parametrize("reverse", [False, True], ids=["oldest-first", "newest-first"])
def test_long_lineage_selection_remains_iterative(*, reverse: bool) -> None:
    """Select beyond the recursion limit without depending on row order."""
    lineage = _lineage_module()
    nodes = _accepted_chain(2048, reverse=reverse)

    selected = lineage.select_current_accepted_artifact(
        nodes, chain_key=("long-chain",)
    )

    assert selected.artifact_id == len(nodes)
    assert lineage.accepted_ancestor_ids(nodes) == frozenset(range(1, len(nodes)))


def test_linear_chain_requires_first_version_and_immediate_prior_parent() -> None:
    """Reject skipped versions and successors that do not name the prior node."""
    lineage = _lineage_module()
    node = lineage.ArtifactLineageNode
    chain_key = (1, 10, "sha256:spec")

    expected_second_version = 2
    assert (
        lineage.next_artifact_version((), chain_key=chain_key, supersedes_id=None) == 1
    )
    first = node(artifact_id=8, chain_key=chain_key, version_number=1)
    assert (
        lineage.next_artifact_version(
            (first,),
            chain_key=chain_key,
            supersedes_id=8,
        )
        == expected_second_version
    )

    with pytest.raises(lineage.PlanningLineageError, match="IMMEDIATE_PRIOR_REQUIRED"):
        lineage.next_artifact_version(
            (first,),
            chain_key=chain_key,
            supersedes_id=None,
        )


def test_lineage_rejects_cross_key_parent_cycle_and_branch() -> None:
    """One artifact chain is linear, acyclic, and cannot cross its closed key."""
    lineage = _lineage_module()
    node = lineage.ArtifactLineageNode
    key_a = (1, 10, "sha256:a")
    key_b = (1, 11, "sha256:b")

    with pytest.raises(lineage.PlanningLineageError, match="CROSS_KEY_PARENT"):
        lineage.validate_artifact_lineage(
            (
                node(artifact_id=1, chain_key=key_a, version_number=1),
                node(
                    artifact_id=2,
                    chain_key=key_b,
                    version_number=2,
                    supersedes_artifact_id=1,
                ),
            )
        )

    with pytest.raises(lineage.PlanningLineageError, match="LINEAGE_CYCLE"):
        lineage.validate_artifact_lineage(
            (
                node(
                    artifact_id=1,
                    chain_key=key_a,
                    version_number=1,
                    supersedes_artifact_id=2,
                ),
                node(
                    artifact_id=2,
                    chain_key=key_a,
                    version_number=2,
                    supersedes_artifact_id=1,
                ),
            )
        )

    with pytest.raises(lineage.PlanningLineageError, match="LINEAGE_BRANCH"):
        lineage.validate_artifact_lineage(
            (
                node(artifact_id=1, chain_key=key_a, version_number=1),
                node(
                    artifact_id=2,
                    chain_key=key_a,
                    version_number=2,
                    supersedes_artifact_id=1,
                ),
                node(
                    artifact_id=3,
                    chain_key=key_a,
                    version_number=3,
                    supersedes_artifact_id=1,
                ),
            )
        )


@pytest.mark.parametrize(
    ("parents", "expected_code"),
    [
        ((99,), "LINEAGE_PARENT_MISSING"),
        ((3,), "LINEAGE_CYCLE"),
        ((4, 3), "LINEAGE_CYCLE"),
        ((4, 5, 4), "LINEAGE_CYCLE"),
    ],
    ids=["missing-parent", "self-cycle", "two-node-cycle", "tail-into-cycle"],
)
def test_completed_parent_walk_does_not_hide_invalid_components(
    parents: tuple[int, ...],
    expected_code: str,
) -> None:
    """A previously checked chain must not suppress a later graph error."""
    lineage = _lineage_module()
    valid = _accepted_chain(2, reverse=False)
    invalid = tuple(
        lineage.ArtifactLineageNode(
            artifact_id=artifact_id,
            chain_key=("invalid-component",),
            version_number=artifact_id,
            supersedes_artifact_id=parent,
            decision="accepted",
        )
        for artifact_id, parent in enumerate(parents, start=3)
    )

    with pytest.raises(lineage.PlanningLineageError) as raised:
        lineage.select_current_accepted_artifact(
            (*valid, *invalid), chain_key=("long-chain",)
        )

    assert raised.value.code == lineage.PlanningLineageCode(expected_code)


def test_transitive_accepted_leaf_ignores_feedback_until_accepted_successor() -> None:
    """Feedback alone cannot displace A; accepted C through B does displace A."""
    lineage = _lineage_module()
    node = lineage.ArtifactLineageNode
    key = (4, 20, "sha256:root")
    accepted_a = node(
        artifact_id=30,
        chain_key=key,
        version_number=1,
        decision="accepted",
    )
    feedback_b = node(
        artifact_id=31,
        chain_key=key,
        version_number=2,
        supersedes_artifact_id=30,
        decision="feedback",
    )

    assert (
        lineage.select_current_accepted_artifact(
            (accepted_a, feedback_b), chain_key=key
        )
        == accepted_a
    )
    assert lineage.select_physical_leaf((accepted_a, feedback_b), chain_key=key) == (
        feedback_b
    )

    accepted_c = node(
        artifact_id=32,
        chain_key=key,
        version_number=3,
        supersedes_artifact_id=31,
        decision="accepted",
    )
    assert (
        lineage.select_current_accepted_artifact(
            (accepted_a, feedback_b, accepted_c),
            chain_key=key,
        )
        == accepted_c
    )


def test_accepted_ancestor_ids_displace_only_accepted_history() -> None:
    """Derive readable superseded status from accepted transitive ancestry."""
    lineage = _lineage_module()
    node = lineage.ArtifactLineageNode
    key = (4, 20, "sha256:root")
    accepted_a = node(
        artifact_id=30,
        chain_key=key,
        version_number=1,
        decision="accepted",
    )
    feedback_b = node(
        artifact_id=31,
        chain_key=key,
        version_number=2,
        supersedes_artifact_id=30,
        decision="feedback",
    )
    accepted_c = node(
        artifact_id=32,
        chain_key=key,
        version_number=3,
        supersedes_artifact_id=31,
        decision="accepted",
    )

    assert lineage.accepted_ancestor_ids((accepted_a, feedback_b)) == frozenset()
    assert lineage.accepted_ancestor_ids(
        (accepted_a, feedback_b, accepted_c)
    ) == frozenset({accepted_a.artifact_id})


@pytest.mark.parametrize(
    "reverse", [False, True], ids=["mixed-order", "reversed-order"]
)
def test_accepted_ancestry_preserves_separate_mixed_decision_chains(
    *,
    reverse: bool,
) -> None:
    """Only an accepted descendant displaces accepted transitive ancestors."""
    lineage = _lineage_module()
    rows = (
        (1, "first", 1, None, "accepted"),
        (2, "first", 2, 1, "feedback"),
        (3, "first", 3, 2, "accepted"),
        (4, "first", 4, 3, "rejected"),
        (5, "first", 5, 4, None),
        (6, "second", 1, None, "accepted"),
        (7, "second", 2, 6, "feedback"),
        (8, "third", 1, None, "feedback"),
        (9, "third", 2, 8, "accepted"),
        (10, "third", 3, 9, "accepted"),
        (11, "third", 4, 10, None),
    )
    nodes = tuple(
        lineage.ArtifactLineageNode(
            artifact_id=artifact_id,
            chain_key=(key,),
            version_number=version,
            supersedes_artifact_id=parent,
            decision=decision,
        )
        for artifact_id, key, version, parent, decision in rows
    )
    nodes = nodes[::2] + nodes[1::2]
    if reverse:
        nodes = tuple(reversed(nodes))

    assert lineage.accepted_ancestor_ids(nodes) == frozenset({1, 9})
    for key, expected_id in (("first", 3), ("second", 6), ("third", 10)):
        selected = lineage.select_current_accepted_artifact(nodes, chain_key=(key,))
        assert selected.artifact_id == expected_id


def test_accepted_leaf_selection_fails_for_zero_or_ambiguous_current_rows() -> None:
    """Required current-parent selection never guesses from IDs or versions."""
    lineage = _lineage_module()
    node = lineage.ArtifactLineageNode
    key = (1, "one-chain")

    with pytest.raises(lineage.PlanningLineageError, match="ACCEPTED_LEAF_MISSING"):
        lineage.select_current_accepted_artifact(
            (node(artifact_id=1, chain_key=key, version_number=1),),
            chain_key=key,
        )

    with pytest.raises(lineage.PlanningLineageError, match="ACCEPTED_LEAF_AMBIGUOUS"):
        lineage.select_current_accepted_artifact(
            (
                node(
                    artifact_id=9,
                    chain_key=key,
                    version_number=1,
                    decision="accepted",
                ),
                node(
                    artifact_id=3,
                    chain_key=key,
                    version_number=2,
                    decision="accepted",
                ),
            ),
            chain_key=key,
        )


def test_accepted_leaf_selection_rejects_a_branched_parent_graph() -> None:
    """Current selection cannot hide a non-linear persisted parent graph."""
    lineage = _lineage_module()
    node = lineage.ArtifactLineageNode
    key = (1, "branched-chain")
    accepted = node(
        artifact_id=1,
        chain_key=key,
        version_number=1,
        decision="accepted",
    )
    feedback = node(
        artifact_id=2,
        chain_key=key,
        version_number=2,
        supersedes_artifact_id=1,
        decision="feedback",
    )
    rejected_branch = node(
        artifact_id=3,
        chain_key=key,
        version_number=2,
        supersedes_artifact_id=1,
        decision="rejected",
    )

    with pytest.raises(lineage.PlanningLineageError, match="LINEAGE_BRANCH"):
        lineage.select_current_accepted_artifact(
            (accepted, feedback, rejected_branch),
            chain_key=key,
        )


@pytest.mark.parametrize(
    ("nodes", "expected_code"),
    [
        (
            ((1, 7, None, "accepted"),),
            "FIRST_VERSION_INVALID",
        ),
        (
            (
                (1, 1, None, "feedback"),
                (2, 3, 1, "accepted"),
            ),
            "IMMEDIATE_PRIOR_REQUIRED",
        ),
        (
            (
                (1, 1, None, "feedback"),
                (2, 1, 1, "accepted"),
            ),
            "VERSION_DUPLICATE",
        ),
        (
            (
                (1, 1, None, "feedback"),
                (3, 3, 1, "feedback"),
                (2, 2, 3, "accepted"),
            ),
            "IMMEDIATE_PRIOR_REQUIRED",
        ),
    ],
    ids=(
        "invalid-root-version",
        "version-gap",
        "duplicate-version",
        "non-immediate-successor",
    ),
)
def test_accepted_leaf_selection_validates_complete_closed_lineage(
    nodes: tuple[tuple[int, int, int | None, str], ...],
    expected_code: str,
) -> None:
    """Selection rejects every corrupt version shape before choosing a leaf."""
    lineage = _lineage_module()
    key = (1, "corrupt-selection")
    projected = tuple(
        lineage.ArtifactLineageNode(
            artifact_id=artifact_id,
            chain_key=key,
            version_number=version_number,
            supersedes_artifact_id=supersedes_artifact_id,
            decision=decision,
        )
        for artifact_id, version_number, supersedes_artifact_id, decision in nodes
    )

    with pytest.raises(lineage.PlanningLineageError) as raised:
        lineage.select_current_accepted_artifact(projected, chain_key=key)

    assert raised.value.code == lineage.PlanningLineageCode(expected_code)


def test_sprint_stream_selection_reuses_only_one_unstarted_current_cycle() -> None:
    """Reuse feedback/unstarted streams; mint after start or lineage change."""
    lineage = _lineage_module()
    state = lineage.SprintStreamState
    spec_a = (1, "sha256:a")
    spec_b = (2, "sha256:b")

    open_a = state(
        spec_identity=spec_a,
        stream_id="SPS-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        created_order=1,
        sprint_started=False,
        sprint_terminal=False,
    )
    assert lineage.select_reusable_sprint_stream((open_a,), spec_identity=spec_a) == (
        open_a.stream_id
    )
    assert (
        lineage.select_reusable_sprint_stream((open_a,), spec_identity=spec_b) is None
    )

    started_a = state(
        spec_identity=spec_a,
        stream_id=open_a.stream_id,
        created_order=1,
        sprint_started=True,
        sprint_terminal=False,
    )
    assert (
        lineage.select_reusable_sprint_stream((started_a,), spec_identity=spec_a)
        is None
    )

    other_open = state(
        spec_identity=spec_a,
        stream_id="SPS-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        created_order=2,
        sprint_started=False,
        sprint_terminal=False,
    )
    with pytest.raises(lineage.PlanningLineageError, match="SPRINT_STREAM_AMBIGUOUS"):
        lineage.select_reusable_sprint_stream(
            (open_a, other_open),
            spec_identity=spec_a,
        )


def test_current_sprint_stream_prefers_open_then_latest_lifecycle_order() -> None:
    """Select a cycle by open state and lifecycle order, never stream identity."""
    lineage = _lineage_module()
    state = lineage.SprintStreamState
    spec = (1, "sha256:a")
    older_closed = state(
        spec_identity=spec,
        stream_id="SPS-ffffffffffffffffffffffffffffffff",
        created_order=1,
        sprint_started=True,
        sprint_terminal=True,
    )
    newer_closed = state(
        spec_identity=spec,
        stream_id="SPS-00000000000000000000000000000000",
        created_order=2,
        sprint_started=True,
        sprint_terminal=True,
    )
    open_stream = state(
        spec_identity=spec,
        stream_id="SPS-11111111111111111111111111111111",
        created_order=1,
        sprint_started=False,
        sprint_terminal=False,
    )
    started_stream = state(
        spec_identity=spec,
        stream_id="SPS-33333333333333333333333333333333",
        created_order=3,
        sprint_started=True,
        sprint_terminal=False,
    )

    assert (
        lineage.select_current_sprint_stream(
            (older_closed, newer_closed), spec_identity=spec
        )
        == newer_closed.stream_id
    )
    assert (
        lineage.select_current_sprint_stream(
            (newer_closed, open_stream), spec_identity=spec
        )
        == open_stream.stream_id
    )
    assert (
        lineage.select_current_sprint_stream(
            (newer_closed, started_stream), spec_identity=spec
        )
        == started_stream.stream_id
    )

    with pytest.raises(lineage.PlanningLineageError, match="SPRINT_STREAM_AMBIGUOUS"):
        lineage.select_current_sprint_stream(
            (
                open_stream,
                state(
                    spec_identity=spec,
                    stream_id="SPS-22222222222222222222222222222222",
                    created_order=2,
                    sprint_started=False,
                    sprint_terminal=False,
                ),
            ),
            spec_identity=spec,
        )
