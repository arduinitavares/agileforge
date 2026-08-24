"""Exact immutable parent-chain and Sprint-stream selection."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

type LineageKey = tuple[object, ...]
type Decision = Literal["accepted", "feedback", "rejected"] | None


class PlanningLineageCode(StrEnum):
    """Closed failure codes for persisted planning ancestry."""

    LINEAGE_IDENTITY_DUPLICATE = "LINEAGE_IDENTITY_DUPLICATE"
    LINEAGE_CYCLE = "LINEAGE_CYCLE"
    LINEAGE_PARENT_MISSING = "LINEAGE_PARENT_MISSING"
    CROSS_KEY_PARENT = "CROSS_KEY_PARENT"
    LINEAGE_BRANCH = "LINEAGE_BRANCH"
    VERSION_INVALID = "VERSION_INVALID"
    VERSION_DUPLICATE = "VERSION_DUPLICATE"
    FIRST_VERSION_INVALID = "FIRST_VERSION_INVALID"
    IMMEDIATE_PRIOR_REQUIRED = "IMMEDIATE_PRIOR_REQUIRED"
    VERSION_GAP = "VERSION_GAP"
    LINEAGE_AMBIGUOUS = "LINEAGE_AMBIGUOUS"
    ACCEPTED_LEAF_MISSING = "ACCEPTED_LEAF_MISSING"
    ACCEPTED_LEAF_AMBIGUOUS = "ACCEPTED_LEAF_AMBIGUOUS"
    SPRINT_STREAM_ID_DUPLICATE = "SPRINT_STREAM_ID_DUPLICATE"
    SPRINT_STREAM_AMBIGUOUS = "SPRINT_STREAM_AMBIGUOUS"


class PlanningLineageError(RuntimeError):
    """A planning artifact chain is missing, mixed, cyclic, or ambiguous."""

    def __init__(self, code: PlanningLineageCode) -> None:
        """Expose one stable code as both message and attribute."""
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ArtifactLineageNode:
    """Minimal immutable projection shared by all reviewed artifact chains."""

    artifact_id: int
    chain_key: LineageKey
    version_number: int
    supersedes_artifact_id: int | None = None
    decision: Decision = None


@dataclass(frozen=True)
class SprintStreamState:
    """Persisted state needed to reuse or mint one Sprint planning stream."""

    spec_identity: tuple[int, str]
    stream_id: str
    created_order: int
    sprint_started: bool
    sprint_terminal: bool


def _nodes_by_id(
    nodes: tuple[ArtifactLineageNode, ...],
) -> dict[int, ArtifactLineageNode]:
    by_id = {node.artifact_id: node for node in nodes}
    if len(by_id) != len(nodes):
        raise PlanningLineageError(PlanningLineageCode.LINEAGE_IDENTITY_DUPLICATE)
    return by_id


def _reject_cycles(
    nodes: tuple[ArtifactLineageNode, ...],
    by_id: dict[int, ArtifactLineageNode],
) -> None:
    for origin in nodes:
        visited: set[int] = set()
        current = origin
        while current.supersedes_artifact_id is not None:
            if current.artifact_id in visited:
                raise PlanningLineageError(PlanningLineageCode.LINEAGE_CYCLE)
            visited.add(current.artifact_id)
            parent = by_id.get(current.supersedes_artifact_id)
            if parent is None:
                break
            current = parent


def _validate_parent_graph(
    nodes: tuple[ArtifactLineageNode, ...],
) -> dict[int, ArtifactLineageNode]:
    by_id = _nodes_by_id(nodes)
    _reject_cycles(nodes, by_id)
    for node in nodes:
        if node.supersedes_artifact_id is None:
            continue
        parent = by_id.get(node.supersedes_artifact_id)
        if parent is None:
            raise PlanningLineageError(PlanningLineageCode.LINEAGE_PARENT_MISSING)
        if parent.chain_key != node.chain_key:
            raise PlanningLineageError(PlanningLineageCode.CROSS_KEY_PARENT)
    return by_id


def _reject_branches(nodes: tuple[ArtifactLineageNode, ...]) -> None:
    children = Counter(
        node.supersedes_artifact_id
        for node in nodes
        if node.supersedes_artifact_id is not None
    )
    if any(count > 1 for count in children.values()):
        raise PlanningLineageError(PlanningLineageCode.LINEAGE_BRANCH)


def validate_artifact_lineage(nodes: tuple[ArtifactLineageNode, ...]) -> None:
    """Validate a complete closed chain set without choosing by maximum ID."""
    by_id = _validate_parent_graph(nodes)
    _reject_branches(nodes)

    versions_by_key: dict[LineageKey, set[int]] = defaultdict(set)
    for node in nodes:
        if node.version_number < 1:
            raise PlanningLineageError(PlanningLineageCode.VERSION_INVALID)
        if node.version_number in versions_by_key[node.chain_key]:
            raise PlanningLineageError(PlanningLineageCode.VERSION_DUPLICATE)
        versions_by_key[node.chain_key].add(node.version_number)

        if node.supersedes_artifact_id is None:
            if node.version_number != 1:
                raise PlanningLineageError(PlanningLineageCode.FIRST_VERSION_INVALID)
            continue
        parent = by_id[node.supersedes_artifact_id]
        if node.version_number != parent.version_number + 1:
            raise PlanningLineageError(PlanningLineageCode.IMMEDIATE_PRIOR_REQUIRED)

    for versions in versions_by_key.values():
        if versions != set(range(1, len(versions) + 1)):
            raise PlanningLineageError(PlanningLineageCode.VERSION_GAP)


def next_artifact_version(
    nodes: tuple[ArtifactLineageNode, ...],
    *,
    chain_key: LineageKey,
    supersedes_id: int | None,
) -> int:
    """Return the next version only for the exact terminal node in one key."""
    validate_artifact_lineage(nodes)
    chain = tuple(node for node in nodes if node.chain_key == chain_key)
    if not chain:
        if supersedes_id is not None:
            raise PlanningLineageError(PlanningLineageCode.FIRST_VERSION_INVALID)
        return 1

    parent_ids = {
        node.supersedes_artifact_id
        for node in chain
        if node.supersedes_artifact_id is not None
    }
    leaves = tuple(node for node in chain if node.artifact_id not in parent_ids)
    if len(leaves) != 1:
        raise PlanningLineageError(PlanningLineageCode.LINEAGE_AMBIGUOUS)
    leaf = leaves[0]
    if supersedes_id != leaf.artifact_id:
        raise PlanningLineageError(PlanningLineageCode.IMMEDIATE_PRIOR_REQUIRED)
    return leaf.version_number + 1


def select_physical_leaf(
    nodes: tuple[ArtifactLineageNode, ...],
    *,
    chain_key: LineageKey,
) -> ArtifactLineageNode:
    """Select the sole physical leaf of one fully validated artifact chain."""
    validate_artifact_lineage(nodes)
    chain = tuple(node for node in nodes if node.chain_key == chain_key)
    parent_ids = {
        node.supersedes_artifact_id
        for node in chain
        if node.supersedes_artifact_id is not None
    }
    leaves = tuple(node for node in chain if node.artifact_id not in parent_ids)
    if len(leaves) != 1:
        raise PlanningLineageError(PlanningLineageCode.LINEAGE_AMBIGUOUS)
    return leaves[0]


def _accepted_ancestor_ids(
    nodes: tuple[ArtifactLineageNode, ...],
    by_id: dict[int, ArtifactLineageNode],
) -> frozenset[int]:
    superseded: set[int] = set()
    for descendant in nodes:
        if descendant.decision != "accepted":
            continue
        current = descendant
        while current.supersedes_artifact_id is not None:
            parent = by_id[current.supersedes_artifact_id]
            if parent.decision == "accepted":
                superseded.add(parent.artifact_id)
            current = parent
    return frozenset(superseded)


def accepted_ancestor_ids(
    nodes: tuple[ArtifactLineageNode, ...],
) -> frozenset[int]:
    """Return accepted ancestors displaced by accepted transitive descendants."""
    validate_artifact_lineage(nodes)
    return _accepted_ancestor_ids(nodes, _nodes_by_id(nodes))


def select_current_accepted_artifact(
    nodes: tuple[ArtifactLineageNode, ...],
    *,
    chain_key: LineageKey,
) -> ArtifactLineageNode:
    """Select the sole accepted leaf using transitive supersession ancestry."""
    by_id = _validate_parent_graph(nodes)
    _reject_branches(nodes)
    superseded = _accepted_ancestor_ids(nodes, by_id)
    accepted = tuple(
        node
        for node in nodes
        if node.chain_key == chain_key and node.decision == "accepted"
    )
    leaves = tuple(node for node in accepted if node.artifact_id not in superseded)
    if not leaves:
        raise PlanningLineageError(PlanningLineageCode.ACCEPTED_LEAF_MISSING)
    if len(leaves) != 1:
        raise PlanningLineageError(PlanningLineageCode.ACCEPTED_LEAF_AMBIGUOUS)
    validate_artifact_lineage(nodes)
    return leaves[0]


def _matching_sprint_streams(
    states: tuple[SprintStreamState, ...],
    *,
    spec_identity: tuple[int, str],
) -> tuple[SprintStreamState, ...]:
    matching = tuple(state for state in states if state.spec_identity == spec_identity)
    if len({state.stream_id for state in matching}) != len(matching):
        raise PlanningLineageError(PlanningLineageCode.SPRINT_STREAM_ID_DUPLICATE)
    return matching


def _open_sprint_streams(
    states: tuple[SprintStreamState, ...],
) -> tuple[SprintStreamState, ...]:
    return tuple(
        state
        for state in states
        if not state.sprint_started and not state.sprint_terminal
    )


def select_reusable_sprint_stream(
    states: tuple[SprintStreamState, ...],
    *,
    spec_identity: tuple[int, str],
) -> str | None:
    """Reuse the sole unstarted current stream, or signal that minting is valid."""
    matching = _matching_sprint_streams(states, spec_identity=spec_identity)
    open_streams = _open_sprint_streams(matching)
    if len(open_streams) > 1:
        raise PlanningLineageError(PlanningLineageCode.SPRINT_STREAM_AMBIGUOUS)
    if not open_streams:
        return None
    return open_streams[0].stream_id


def select_current_sprint_stream(
    states: tuple[SprintStreamState, ...],
    *,
    spec_identity: tuple[int, str],
) -> str | None:
    """Select the sole open cycle or latest lifecycle-ordered closed cycle."""
    matching = _matching_sprint_streams(states, spec_identity=spec_identity)
    open_streams = _open_sprint_streams(matching)
    if len(open_streams) > 1:
        raise PlanningLineageError(PlanningLineageCode.SPRINT_STREAM_AMBIGUOUS)
    if open_streams:
        return open_streams[0].stream_id
    if not matching:
        return None
    latest_order = max(state.created_order for state in matching)
    latest = tuple(state for state in matching if state.created_order == latest_order)
    if len(latest) != 1:
        raise PlanningLineageError(PlanningLineageCode.SPRINT_STREAM_AMBIGUOUS)
    return latest[0].stream_id
