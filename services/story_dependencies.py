"""Story dependency graph loading and diagnostics."""

from dataclasses import dataclass
from typing import Any, cast

from sqlmodel import Session, select

from models.core import UserStory, UserStoryDependency


@dataclass(frozen=True)
class DependencyGraphIssue:
    """A dependency graph issue safe to expose in CLI JSON."""

    code: str
    message: str
    story_ids: list[int]
    edge_status: str | None = None
    dependency_id: int | None = None
    dependent_story_id: int | None = None
    prerequisite_story_id: int | None = None


@dataclass(frozen=True)
class DependencyGraph:
    """Validated active/proposed dependency graph split by review status."""

    active_edges: dict[int, set[int]]
    proposed_edges: dict[int, set[int]]
    issues: list[DependencyGraphIssue]
    cycle_paths: list[list[int]]


class StoryDependencyGraphError(RuntimeError):
    """Raised when active dependencies are not safe for sprint planning."""

    def __init__(self, issues: list[DependencyGraphIssue]) -> None:
        """Initialize with graph issues to expose through CLI diagnostics."""
        self.issues = issues
        codes = ", ".join(sorted({issue.code for issue in issues}))
        super().__init__(f"Story dependency graph invalid for sprint planning: {codes}")


def load_story_dependency_graph(
    session: Session,
    *,
    project_id: int,
) -> DependencyGraph:
    """Load dependency graph, excluding invalid edges from planner-ready output."""
    edge_rows = session.exec(
        select(UserStoryDependency)
        .where(UserStoryDependency.product_id == project_id)
        .order_by(cast("Any", UserStoryDependency.dependent_story_id))
    ).all()
    endpoint_ids = {
        story_id
        for edge in edge_rows
        for story_id in (edge.dependent_story_id, edge.prerequisite_story_id)
    }
    stories_by_id = _load_stories_by_id(session, endpoint_ids)

    active_edges: dict[int, set[int]] = {}
    proposed_edges: dict[int, set[int]] = {}
    issues: list[DependencyGraphIssue] = []

    for edge in edge_rows:
        edge_issue = _edge_issue(
            edge,
            stories_by_id=stories_by_id,
            project_id=project_id,
        )
        if edge_issue is not None:
            issues.append(edge_issue)
            continue

        if edge.status == "active":
            active_edges.setdefault(edge.dependent_story_id, set()).add(
                edge.prerequisite_story_id
            )
        elif edge.status == "proposed":
            proposed_edges.setdefault(edge.dependent_story_id, set()).add(
                edge.prerequisite_story_id
            )

    cycle_paths = detect_dependency_cycles(active_edges)
    issues.extend(
        [
            DependencyGraphIssue(
                code="STORY_DEPENDENCY_CYCLE",
                message="Active story dependency graph contains a cycle.",
                story_ids=cycle_path,
                edge_status="active",
            )
            for cycle_path in cycle_paths
        ]
    )

    return DependencyGraph(
        active_edges=active_edges,
        proposed_edges=proposed_edges,
        issues=issues,
        cycle_paths=cycle_paths,
    )


def detect_dependency_cycles(edges: dict[int, set[int]]) -> list[list[int]]:
    """Return deterministic directed cycle paths from dependency edges."""
    visited: set[int] = set()
    active: set[int] = set()
    path: list[int] = []
    cycles: list[list[int]] = []
    seen_cycle_keys: set[tuple[int, ...]] = set()
    nodes = set(edges)
    for prerequisites in edges.values():
        nodes.update(prerequisites)

    def visit(node_id: int) -> None:
        visited.add(node_id)
        active.add(node_id)
        path.append(node_id)

        for prerequisite_id in sorted(edges.get(node_id, set())):
            if prerequisite_id not in visited:
                visit(prerequisite_id)
            elif prerequisite_id in active:
                cycle_start = path.index(prerequisite_id)
                cycle_path = [*path[cycle_start:], prerequisite_id]
                cycle_key = _canonical_cycle_key(cycle_path)
                if cycle_key not in seen_cycle_keys:
                    seen_cycle_keys.add(cycle_key)
                    cycles.append(cycle_path)

        path.pop()
        active.remove(node_id)

    for node_id in sorted(nodes):
        if node_id not in visited:
            visit(node_id)

    return cycles


def dependency_inspect_payload(
    session: Session,
    *,
    project_id: int,
) -> dict[str, Any]:
    """Build JSON-friendly dependency inspect payload."""
    graph = load_story_dependency_graph(session, project_id=project_id)
    story_ids = set(graph.active_edges) | set(graph.proposed_edges)
    for prerequisites in [*graph.active_edges.values(), *graph.proposed_edges.values()]:
        story_ids.update(prerequisites)
    for cycle_path in graph.cycle_paths:
        story_ids.update(cycle_path)
    stories_by_id = _load_stories_by_id(session, story_ids)
    edge_rows_by_key = _load_edge_rows_by_key(session, project_id=project_id)

    active_edges = _edge_payloads(
        graph.active_edges,
        stories_by_id=stories_by_id,
        edge_rows_by_key=edge_rows_by_key,
    )
    proposed_edges = _edge_payloads(
        graph.proposed_edges,
        stories_by_id=stories_by_id,
        edge_rows_by_key=edge_rows_by_key,
    )

    return {
        "project_id": project_id,
        "active_edge_count": len(active_edges),
        "active_edges": active_edges,
        "proposed_edge_count": len(proposed_edges),
        "proposed_edges": proposed_edges,
        "issue_count": len(graph.issues),
        "issues": [_issue_payload(issue) for issue in graph.issues],
        "cycle_count": len(graph.cycle_paths),
        "cycle_paths": [
            {
                "story_ids": cycle_path,
                "story_titles": [
                    _story_title(stories_by_id=stories_by_id, story_id=story_id)
                    for story_id in cycle_path
                ],
            }
            for cycle_path in graph.cycle_paths
        ],
    }


def assert_dependency_graph_valid_for_sprint(
    session: Session,
    *,
    project_id: int,
) -> None:
    """Fail when active dependencies are unsafe for sprint planning."""
    graph = load_story_dependency_graph(session, project_id=project_id)
    blocking_issues = [
        issue
        for issue in graph.issues
        if issue.edge_status == "active" or issue.code == "STORY_DEPENDENCY_CYCLE"
    ]
    if blocking_issues:
        raise StoryDependencyGraphError(blocking_issues)


def _load_stories_by_id(
    session: Session,
    story_ids: set[int],
) -> dict[int, UserStory]:
    if not story_ids:
        return {}
    rows = session.exec(
        select(UserStory).where(cast("Any", UserStory.story_id).in_(story_ids))
    ).all()
    return {story.story_id: story for story in rows if story.story_id is not None}


def _load_edge_rows_by_key(
    session: Session,
    *,
    project_id: int,
) -> dict[tuple[int, int], UserStoryDependency]:
    edge_rows = session.exec(
        select(UserStoryDependency).where(UserStoryDependency.product_id == project_id)
    ).all()
    return {
        (edge.dependent_story_id, edge.prerequisite_story_id): edge
        for edge in edge_rows
    }


def _edge_issue(
    edge: UserStoryDependency,
    *,
    stories_by_id: dict[int, UserStory],
    project_id: int,
) -> DependencyGraphIssue | None:
    if edge.dependent_story_id == edge.prerequisite_story_id:
        return DependencyGraphIssue(
            code="STORY_DEPENDENCY_SELF_EDGE",
            message="Dependency edge cannot point from a story to itself.",
            story_ids=[edge.dependent_story_id],
            edge_status=edge.status,
            dependency_id=edge.dependency_id,
            dependent_story_id=edge.dependent_story_id,
            prerequisite_story_id=edge.prerequisite_story_id,
        )

    missing_story_ids = [
        story_id
        for story_id in (edge.dependent_story_id, edge.prerequisite_story_id)
        if story_id not in stories_by_id
    ]
    if missing_story_ids:
        return DependencyGraphIssue(
            code="STORY_DEPENDENCY_ORPHAN",
            message="Dependency edge references missing story row(s).",
            story_ids=missing_story_ids,
            edge_status=edge.status,
            dependency_id=edge.dependency_id,
            dependent_story_id=edge.dependent_story_id,
            prerequisite_story_id=edge.prerequisite_story_id,
        )

    dependent = stories_by_id[edge.dependent_story_id]
    prerequisite = stories_by_id[edge.prerequisite_story_id]
    cross_project_ids = [
        story.story_id
        for story in (dependent, prerequisite)
        if story.story_id is not None and story.product_id != project_id
    ]
    if cross_project_ids:
        return DependencyGraphIssue(
            code="STORY_DEPENDENCY_CROSS_PROJECT",
            message="Dependency edge references story row outside this project.",
            story_ids=cross_project_ids,
            edge_status=edge.status,
            dependency_id=edge.dependency_id,
            dependent_story_id=edge.dependent_story_id,
            prerequisite_story_id=edge.prerequisite_story_id,
        )

    superseded_story_ids = [
        story.story_id
        for story in (dependent, prerequisite)
        if story.story_id is not None and story.is_superseded
    ]
    if superseded_story_ids:
        return DependencyGraphIssue(
            code="STORY_DEPENDENCY_SUPERSEDED_STORY",
            message="Dependency edge references superseded story row(s).",
            story_ids=superseded_story_ids,
            edge_status=edge.status,
            dependency_id=edge.dependency_id,
            dependent_story_id=edge.dependent_story_id,
            prerequisite_story_id=edge.prerequisite_story_id,
        )

    return None


def _edge_payloads(
    edges: dict[int, set[int]],
    *,
    stories_by_id: dict[int, UserStory],
    edge_rows_by_key: dict[tuple[int, int], UserStoryDependency],
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for dependent_story_id, prerequisite_ids in sorted(edges.items()):
        for prerequisite_story_id in sorted(prerequisite_ids):
            edge = edge_rows_by_key[(dependent_story_id, prerequisite_story_id)]
            payloads.append(
                {
                    "dependency_id": edge.dependency_id,
                    "dependent_story_id": dependent_story_id,
                    "dependent_story_title": _story_title(
                        stories_by_id=stories_by_id,
                        story_id=dependent_story_id,
                    ),
                    "prerequisite_story_id": prerequisite_story_id,
                    "prerequisite_story_title": _story_title(
                        stories_by_id=stories_by_id,
                        story_id=prerequisite_story_id,
                    ),
                    "status": edge.status,
                    "source": edge.source,
                    "confidence": edge.confidence,
                    "reason": edge.reason,
                }
            )
    return payloads


def _issue_payload(issue: DependencyGraphIssue) -> dict[str, Any]:
    payload = {
        "code": issue.code,
        "message": issue.message,
        "story_ids": issue.story_ids,
        "edge_status": issue.edge_status,
        "dependency_id": issue.dependency_id,
    }
    if issue.dependent_story_id is not None:
        payload["dependent_story_id"] = issue.dependent_story_id
    if issue.prerequisite_story_id is not None:
        payload["prerequisite_story_id"] = issue.prerequisite_story_id
    return payload


def _story_title(
    *,
    stories_by_id: dict[int, UserStory],
    story_id: int,
) -> str:
    story = stories_by_id.get(story_id)
    if story is None:
        return f"<missing story {story_id}>"
    return story.title


def _canonical_cycle_key(cycle_path: list[int]) -> tuple[int, ...]:
    cycle_body = cycle_path[:-1]
    if not cycle_body:
        return ()
    rotations = [
        tuple(cycle_body[index:] + cycle_body[:index])
        for index in range(len(cycle_body))
    ]
    return min(rotations)
