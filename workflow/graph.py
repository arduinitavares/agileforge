"""Pure hierarchical workflow graph evaluation."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from workflow.contracts import (
    Blocker,
    FactReference,
    InputField,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
    WorkflowPosition,
)
from workflow.facts import WorkflowFactSnapshot
from workflow.fingerprints import decision_fingerprint, fact_fingerprint


class RuleCategory(StrEnum):
    """Internal result category returned by a node rule."""

    SATISFIED = "satisfied"
    AVAILABLE = "available"
    WAITING = "waiting"
    BLOCKED = "blocked"
    INVALID = "invalid"


@dataclass(frozen=True)
class RuleEvaluation:
    """Pure rule output for one stable node instance."""

    category: RuleCategory
    reason_code: str
    instance_key: str | None = None
    fact_references: tuple[FactReference, ...] = ()
    blockers: tuple[Blocker, ...] = ()
    valid_until: datetime | None = None
    recommendation_kind: RecommendationKind | None = None


NodeRule = Callable[[WorkflowFactSnapshot, datetime], tuple[RuleEvaluation, ...]]


@dataclass(frozen=True)
class NodeSpec:
    """Static node identity, request contract, and pure evaluation rule."""

    node_id: str
    child_graph_id: str
    request_kind: str
    recommendation_kind: RecommendationKind
    required_inputs: tuple[InputField, ...]
    evaluate_rule: NodeRule


@dataclass(frozen=True)
class ChildGraphSpec:
    """One ordered node group in the workflow hierarchy."""

    child_graph_id: str
    nodes: tuple[NodeSpec, ...]
    children: tuple[ChildGraphSpec, ...] = ()

    def iter_nodes(self) -> Iterable[NodeSpec]:
        """Yield nodes in depth-first hierarchy order."""
        yield from self.nodes
        for child in self.children:
            yield from child.iter_nodes()


@dataclass(frozen=True)
class WorkflowGraph:
    """Validated hierarchy that derives a position from immutable facts."""

    graph_version: str
    root: ChildGraphSpec

    def __post_init__(self) -> None:
        """Reject ambiguous identifiers anywhere in the graph hierarchy."""
        child_graph_ids: set[str] = set()
        node_ids: set[str] = set()
        pending = [self.root]

        while pending:
            child_graph = pending.pop()
            if child_graph.child_graph_id in child_graph_ids:
                msg = f"Duplicate child graph ID: {child_graph.child_graph_id!r}."
                raise ValueError(msg)
            child_graph_ids.add(child_graph.child_graph_id)

            for node in child_graph.nodes:
                if node.node_id in node_ids:
                    msg = f"Duplicate node ID: {node.node_id!r}."
                    raise ValueError(msg)
                node_ids.add(node.node_id)

            pending.extend(reversed(child_graph.children))

    def evaluate(
        self,
        snapshot: WorkflowFactSnapshot,
        evaluated_at: datetime,
    ) -> WorkflowPosition:
        """Evaluate all rules deterministically against one immutable snapshot."""
        facts_hash = fact_fingerprint(snapshot)
        decisions: list[NodeDecision] = []

        for node in self.root.iter_nodes():
            evaluations = node.evaluate_rule(snapshot, evaluated_at)
            instance_keys: set[str | None] = set()
            for evaluation in evaluations:
                if evaluation.instance_key in instance_keys:
                    msg = (
                        f"Duplicate instance key {evaluation.instance_key!r} "
                        f"for node {node.node_id!r}."
                    )
                    raise ValueError(msg)
                instance_keys.add(evaluation.instance_key)

            for evaluation in sorted(
                evaluations,
                key=lambda item: (
                    item.instance_key is not None,
                    item.instance_key or "",
                ),
            ):
                fact_references = self._decision_fact_references(
                    node,
                    evaluation,
                    snapshot,
                    evaluated_at,
                )
                decision = self._decision(
                    node,
                    evaluation,
                    facts_hash,
                    fact_references,
                )
                if decision is not None:
                    decisions.append(decision)

        decision_tuple = tuple(decisions)
        available = tuple(
            item.node_id
            for item in decision_tuple
            if item.category is NodeCategory.AVAILABLE
        )
        waiting = tuple(
            item.node_id
            for item in decision_tuple
            if item.category is NodeCategory.WAITING
        )
        blocked = tuple(
            item.node_id
            for item in decision_tuple
            if item.category is NodeCategory.BLOCKED
        )
        invalid = tuple(
            item.node_id
            for item in decision_tuple
            if item.category is NodeCategory.INVALID
        )
        terminal = not any(
            item.recommendation_kind
            in {RecommendationKind.REQUIRED, RecommendationKind.RECOVERY}
            for item in decision_tuple
        )
        return WorkflowPosition(
            project_id=snapshot.project.project_id,
            graph_version=self.graph_version,
            fact_fingerprint=facts_hash,
            evaluated_at=evaluated_at,
            available_nodes=available,
            waiting_nodes=waiting,
            blocked_nodes=blocked,
            invalid_nodes=invalid,
            terminal=terminal,
            decisions=decision_tuple,
        )

    def _decision(
        self,
        node: NodeSpec,
        evaluation: RuleEvaluation,
        facts_hash: str,
        fact_references: tuple[FactReference, ...],
    ) -> NodeDecision | None:
        """Project one internal evaluation into a public decision."""
        if evaluation.category is RuleCategory.SATISFIED:
            return None

        category = NodeCategory(evaluation.category.value)
        recommendation_kind = (
            evaluation.recommendation_kind or node.recommendation_kind
        )
        payload: dict[str, object] = {
            "graph_version": self.graph_version,
            "fact_fingerprint": facts_hash,
            "node_id": node.node_id,
            "instance_key": evaluation.instance_key,
            "request_kind": node.request_kind,
            "category": category,
            "recommendation_kind": recommendation_kind,
            "reason_code": evaluation.reason_code,
            "required_inputs": tuple(
                item.model_dump(mode="json") for item in node.required_inputs
            ),
            "fact_references": tuple(
                item.model_dump(mode="json") for item in fact_references
            ),
            "blockers": tuple(
                item.model_dump(mode="json") for item in evaluation.blockers
            ),
            "valid_until": evaluation.valid_until,
        }
        return NodeDecision(
            node_id=node.node_id,
            instance_key=evaluation.instance_key,
            child_graph_id=node.child_graph_id,
            request_kind=node.request_kind,
            category=category,
            recommendation_kind=recommendation_kind,
            reason_code=evaluation.reason_code,
            required_inputs=node.required_inputs,
            fact_references=fact_references,
            blockers=evaluation.blockers,
            valid_until=evaluation.valid_until,
            decision_fingerprint=decision_fingerprint(payload),
        )

    @staticmethod
    def _decision_fact_references(
        node: NodeSpec,
        evaluation: RuleEvaluation,
        snapshot: WorkflowFactSnapshot,
        evaluated_at: datetime,
    ) -> tuple[FactReference, ...]:
        """Append the exact failed or expired attempt to recovery decisions."""
        recommendation = evaluation.recommendation_kind or node.recommendation_kind
        if recommendation is not RecommendationKind.RECOVERY:
            return evaluation.fact_references
        candidates = tuple(
            attempt
            for attempt in snapshot.node_attempts
            if attempt.node_id == node.node_id
            and attempt.instance_key == evaluation.instance_key
            and (
                attempt.outcome in {"failure", "obsolete"}
                or (
                    attempt.outcome is None
                    and evaluated_at >= attempt.lease_expires_at
                )
            )
        )
        if not candidates:
            return evaluation.fact_references
        attempt = max(candidates, key=lambda item: item.attempt_id)
        reference = FactReference(
            fact_type="node_attempt",
            fact_id=str(attempt.attempt_id),
            fingerprint=attempt.attempt_fingerprint,
        )
        if reference in evaluation.fact_references:
            return evaluation.fact_references
        return (*evaluation.fact_references, reference)
