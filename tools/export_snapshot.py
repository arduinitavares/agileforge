"""Export read-only project snapshots as self-contained HTML."""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from markdown import markdown as _md
from sqlmodel import Session, select

from models.core import Epic, Feature, Project, Sprint, SprintStory, Theme, UserStory
from models.db import engine as default_engine
from models.enums import StoryStatus
from models.product_definition import (
    SpecificationCandidate,
    VisionArtifact,
    VisionArtifactDecision,
)
from services.specs.accepted_specification import (
    AcceptedSpecification,
    AcceptedSpecificationIntegrityError,
    load_current_accepted_specification,
)
from services.specs.candidate_contract import (
    SpecificationCandidateEnvelope,
    load_candidate_contract,
    render_candidate_review_markdown,
)
from workflow.fingerprints import canonical_json

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from utils.agileforge_spec_profile_v2 import SpecificationPayload


class _ExportSnapshotError(ValueError):
    @classmethod
    def project_not_found(cls, project_id: int) -> _ExportSnapshotError:
        message = f"Project {project_id} not found"
        return cls(message)

    @classmethod
    def specification_candidate_invalid(cls, reason: str) -> _ExportSnapshotError:
        """Build a stable pre-write failure for unusable specification source."""
        return cls(
            "SPECIFICATION_CANDIDATE_INVALID: Approved specification source is "
            f"unavailable or invalid. details={{'reason': {reason!r}}}."
        )

    @classmethod
    def specification_candidate_missing(cls) -> _ExportSnapshotError:
        """Build a stable failure when the exact registry source is absent."""
        return cls.specification_candidate_invalid(
            "registry source candidate identity does not resolve"
        )

    @classmethod
    def specification_candidate_identity(cls) -> _ExportSnapshotError:
        """Build a stable failure for conflicting durable candidate identity."""
        return cls.specification_candidate_invalid(
            "registry, candidate, and canonical envelope identities differ"
        )

    @classmethod
    def accepted_specification_invalid(
        cls,
        error: AcceptedSpecificationIntegrityError,
    ) -> _ExportSnapshotError:
        """Preserve the deep loader's stable acceptance failure code."""
        return cls(f"{error.code}: {error}")


_ACCEPTANCE_CRITERIA_INVALID = (
    "Story acceptance criteria must be a canonical non-empty JSON string list."
)


@dataclass(frozen=True)
class _SnapshotRenderContext:
    project: Project
    themes: list[Theme]
    epics: list[Epic]
    features: list[Feature]
    stories: list[UserStory]
    all_stories: list[UserStory]
    sprints: list[Sprint]
    sprint_story_map: dict[int, list[int]]
    vision_statement: str
    spec_content: str
    spec_meta: dict[str, Any]


def _markdown(text: str, extensions: list[str] | None = None) -> str:
    return _md(text, extensions=extensions or [])


def export_project_snapshot_html(
    *,
    project_id: int,
    output_dir: Path,
    engine_override: Engine | None = None,
) -> Path:
    """Export a project snapshot as a single HTML file.

    Args:
        project_id: Project identifier.
        output_dir: Destination folder.
        engine_override: Optional SQLAlchemy engine for testing.

    Returns:
        Path to the generated HTML file.
    """
    engine_to_use = engine_override or default_engine

    with Session(engine_to_use) as session:
        project = session.get(Project, project_id)
        if not project:
            raise _ExportSnapshotError.project_not_found(project_id)

        themes = list(
            session.exec(select(Theme).where(Theme.project_id == project_id)).all()
        )
        theme_ids = [theme.theme_id for theme in themes if theme.theme_id is not None]
        epics = list(session.exec(select(Epic)).all())
        epics = [epic for epic in epics if epic.theme_id in theme_ids]

        epic_ids = [epic.epic_id for epic in epics if epic.epic_id is not None]
        features = list(session.exec(select(Feature)).all())
        features = [feature for feature in features if feature.epic_id in epic_ids]
        all_stories = list(
            session.exec(
                select(UserStory).where(UserStory.project_id == project_id)
            ).all()
        )
        sprints = list(
            session.exec(select(Sprint).where(Sprint.project_id == project_id)).all()
        )
        sprint_story_map = _load_sprint_story_map(
            session, [s.sprint_id for s in sprints]
        )
        stories = _select_refined_current_sprint_stories(
            all_stories,
            sprints,
            sprint_story_map,
        )

        vision_statement = _get_latest_accepted_vision(session, project_id)
        try:
            accepted_spec = load_current_accepted_specification(
                session,
                project_id=project_id,
            )
        except AcceptedSpecificationIntegrityError as exc:
            if exc.code in {
                "SPECIFICATION_CANONICAL_BYTES_INVALID",
                "SPECIFICATION_IDENTITY_MISMATCH",
            }:
                raise _ExportSnapshotError.specification_candidate_invalid(
                    str(exc)
                ) from exc
            raise _ExportSnapshotError.accepted_specification_invalid(exc) from exc
        spec_content, spec_meta = _resolve_spec_content(session, accepted_spec)

    render_context = _SnapshotRenderContext(
        project=project,
        themes=themes,
        epics=epics,
        features=features,
        stories=stories,
        all_stories=all_stories,
        sprints=sprints,
        sprint_story_map=sprint_story_map,
        vision_statement=vision_statement,
        spec_content=spec_content,
        spec_meta=spec_meta,
    )
    html_output = _render_snapshot_html(render_context)

    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"snapshot_project_{project.project_id}.html"
    output_path = output_dir / filename
    output_path.write_text(html_output, encoding="utf-8")
    return output_path


def _get_latest_accepted_vision(session: Session, project_id: int) -> str:
    """Return the latest fingerprint-bound accepted Vision statement."""
    decisions = list(
        session.exec(
            select(VisionArtifactDecision).where(
                VisionArtifactDecision.project_id == project_id,
                VisionArtifactDecision.decision == "accepted",
            )
        ).all()
    )
    ordered = sorted(
        decisions,
        key=lambda item: (
            item.decided_at,
            item.vision_artifact_decision_id or -1,
        ),
        reverse=True,
    )
    for decision in ordered:
        artifact = session.get(VisionArtifact, decision.vision_artifact_id)
        if (
            artifact is not None
            and artifact.project_id == project_id
            and artifact.content_fingerprint == decision.artifact_fingerprint
        ):
            return artifact.statement
    return "(No accepted Vision available)"


def _resolve_spec_content(
    session: Session,
    accepted_spec: AcceptedSpecification | None,
) -> tuple[str, dict[str, Any]]:
    if accepted_spec:
        payload, envelope = _load_specification_candidate(
            session,
            accepted_spec,
        )
        meta: dict[str, Any] = {
            "status": "approved",
            "spec_version_id": accepted_spec.spec_version_id,
            "spec_hash": accepted_spec.spec_hash,
            "specification_decision_id": accepted_spec.specification_decision_id,
            "accepted_by": accepted_spec.accepted_by,
            "accepted_at": accepted_spec.accepted_at,
            "acceptance_notes": accepted_spec.acceptance_notes,
            "candidate_fingerprint": envelope.candidate_fingerprint,
            "payload_fingerprint": envelope.payload_fingerprint,
            "source_manifest_fingerprint": envelope.source_manifest_fingerprint,
        }
        return render_candidate_review_markdown(payload, envelope), meta

    meta: dict[str, Any] = {
        "status": "unavailable",
        "spec_version_id": None,
        "spec_hash": None,
        "specification_decision_id": None,
        "accepted_by": None,
        "accepted_at": None,
        "acceptance_notes": None,
        "candidate_fingerprint": None,
        "payload_fingerprint": None,
        "source_manifest_fingerprint": None,
    }
    return "(No accepted specification available)", meta


def _load_specification_candidate(
    session: Session,
    accepted_spec: AcceptedSpecification,
) -> tuple[SpecificationPayload, SpecificationCandidateEnvelope]:
    """Reload the exact candidate already proven by the shared deep loader."""
    candidate = session.exec(
        select(SpecificationCandidate).where(
            SpecificationCandidate.project_id == accepted_spec.project_id,
            SpecificationCandidate.specification_candidate_id
            == accepted_spec.source_specification_candidate_id,
            SpecificationCandidate.candidate_fingerprint
            == accepted_spec.source_specification_candidate_fingerprint,
            SpecificationCandidate.payload_fingerprint == accepted_spec.spec_hash,
        )
    ).one_or_none()
    if candidate is None:
        raise _ExportSnapshotError.specification_candidate_missing()
    try:
        payload, envelope = load_candidate_contract(
            candidate.canonical_envelope_json,
            expected_candidate_fingerprint=candidate.candidate_fingerprint,
        )
    except (TypeError, ValueError) as exc:
        raise _ExportSnapshotError.specification_candidate_invalid(str(exc)) from exc
    if (
        envelope.candidate_fingerprint
        != accepted_spec.source_specification_candidate_fingerprint
        or envelope.payload_fingerprint != accepted_spec.spec_hash
        or payload != accepted_spec.payload
    ):
        raise _ExportSnapshotError.specification_candidate_identity()
    return payload, envelope


def _render_snapshot_html(context: _SnapshotRenderContext) -> str:
    generated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    roadmap_html = _render_roadmap(
        context.themes,
        context.epics,
        context.features,
    )
    stories_html = _render_stories_table(context.stories)
    full_backlog_html = _render_all_stories_table(context.all_stories)
    sprint_html = _render_sprint_summary(
        context.sprints,
        context.stories,
        context.sprint_story_map,
    )
    spec_html = _markdown(
        context.spec_content or "",
        extensions=["fenced_code", "tables"],
    )
    spec_toc = _extract_markdown_headings(context.spec_content or "")
    styles = _render_snapshot_styles()

    return """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Project Snapshot</title>
  <style>
{styles}
  </style>
</head>
<body>
  <h1>Project Snapshot</h1>
  <p class="muted">Generated at {generated_at} (UTC)</p>
  <div class="card">
    <h2>{project_name}</h2>
    <p>{project_description}</p>
    <p class="muted">Read-only snapshot of current project state.</p>
  </div>

  <div class="section">
    <h2>Executive Summary</h2>
    <div class="card">
      <p><strong>Story status:</strong> {story_summary}</p>
      {sprint_summary}
    </div>
  </div>

  <div class="section">
    <h2>product vision</h2>
    <div class="card">{vision_html}</div>
  </div>

  <div class="section">
    <h2>Roadmap</h2>
    {roadmap_html}
  </div>

  <div class="section">
    <h2>Technical Spec</h2>
    <p class="muted">Status: {spec_status_badge}</p>
    {spec_meta_html}
    {spec_toc_html}
    <div class="card">{spec_html}</div>
  </div>

  <div class="section">
    <h2>Current Sprint Stories</h2>
    {stories_html}
  </div>

  <div class="section">
    <h2>Project Backlog (All Stories)</h2>
    <div class="card">
      <p><strong>Backlog summary:</strong> {all_story_summary}</p>
    </div>
    {full_backlog_html}
  </div>

  <div class="section">
    <h2>Sprint Status</h2>
    {sprint_html}
  </div>
</body>
</html>
""".format(
        generated_at=generated_at,
        styles=styles,
        project_name=html.escape(context.project.name or "(Unnamed Project)"),
        project_description=html.escape(context.project.description or ""),
        story_summary=_format_story_summary(context.stories),
        all_story_summary=_format_all_story_summary(context.all_stories),
        sprint_summary=_format_sprint_summary_line(
            context.sprints,
            context.stories,
            context.sprint_story_map,
        ),
        vision_html=_markdown(context.vision_statement),
        roadmap_html=roadmap_html,
        spec_status_badge=_render_spec_status_badge(context.spec_meta),
        spec_meta_html=_render_spec_metadata(context.spec_meta),
        spec_toc_html=_render_spec_toc(spec_toc),
        spec_html=spec_html,
        stories_html=stories_html,
        full_backlog_html=full_backlog_html,
        sprint_html=sprint_html,
    )


def _render_snapshot_styles() -> str:
    return "\n".join(
        [
            (
                '        body { font-family: "Segoe UI", Arial, sans-serif; '
                "margin: 32px; color: #1a1a1a; }"
            ),
            "        h1, h2, h3 { color: #0f172a; }",
            "        .muted { color: #64748b; }",
            "        .section { margin-top: 28px; }",
            (
                "        .card { border: 1px solid #e2e8f0; "
                "border-radius: 8px; padding: 16px; margin-top: 12px; }"
            ),
            (
                "        table { width: 100%; border-collapse: collapse; "
                "margin-top: 12px; }"
            ),
            (
                "        th, td { border: 1px solid #e2e8f0; padding: 8px; "
                "text-align: left; vertical-align: top; }"
            ),
            "        th { background: #f8fafc; }",
            (
                "        pre { background: #f8fafc; padding: 12px; "
                "border-radius: 6px; overflow-x: auto; }"
            ),
            (
                "        .badge { display: inline-block; padding: 2px 8px; "
                "border-radius: 999px; font-size: 12px; }"
            ),
            "        .badge-ok { background: #dcfce7; color: #166534; }",
            "        .badge-warn { background: #fef9c3; color: #854d0e; }",
            "        .acceptance-criteria > li { white-space: pre-wrap; }",
            "        .toc li { margin-bottom: 4px; }",
            "        .toc-level-2 { margin-left: 16px; }",
            "        .toc-level-3 { margin-left: 32px; }",
            "        .toc-level-4 { margin-left: 48px; }",
            "        .toc-level-5 { margin-left: 64px; }",
            "        .toc-level-6 { margin-left: 80px; }",
        ]
    )


def _format_story_summary(stories: Iterable[UserStory]) -> str:
    story_list = list(stories)
    total = len(story_list)
    if total == 0:
        return "No stories in the current sprint."
    counts = {
        StoryStatus.TO_DO: 0,
        StoryStatus.IN_PROGRESS: 0,
        StoryStatus.DONE: 0,
        StoryStatus.ACCEPTED: 0,
    }
    for story in story_list:
        counts[story.status] = counts.get(story.status, 0) + 1
    return (
        f"Total {total} | To Do {counts[StoryStatus.TO_DO]} | "
        f"In Progress {counts[StoryStatus.IN_PROGRESS]} | "
        f"Done {counts[StoryStatus.DONE]} | Accepted {counts[StoryStatus.ACCEPTED]}"
    )


def _format_all_story_summary(stories: Iterable[UserStory]) -> str:
    story_list = list(stories)
    if not story_list:
        return "No stories in product backlog."
    superseded = sum(1 for story in story_list if bool(story.is_superseded))
    return (
        f"Total {len(story_list)} | Active {len(story_list) - superseded} | "
        f"Superseded {superseded}"
    )


def _format_sprint_summary_line(
    sprints: list[Sprint],
    stories: list[UserStory],
    sprint_story_map: dict[int, list[int]],
) -> str:
    sprint = _pick_current_sprint(sprints)
    if not sprint:
        return "<p>No sprint data available.</p>"

    sprint_story_ids = set(sprint_story_map.get(sprint.sprint_id or 0, []))
    if not sprint_story_ids:
        sprint_name = html.escape(sprint.goal or "Unnamed sprint")
        return f"<p><strong>Current sprint:</strong> {sprint_name}</p>"

    sprint_stories = [story for story in stories if story.story_id in sprint_story_ids]
    done_count = sum(1 for story in sprint_stories if story.status == StoryStatus.DONE)
    total = len(sprint_stories)
    completion = (done_count / total) * 100 if total else 0
    sprint_name = html.escape(sprint.goal or "Unnamed sprint")
    return (
        f"<p><strong>Current sprint:</strong> {sprint_name} "
        f"({completion:.1f}% complete)</p>"
    )


def _render_roadmap(
    themes: list[Theme],
    epics: list[Epic],
    features: list[Feature],
) -> str:
    if not themes:
        return '<p class="muted">No roadmap themes available.</p>'

    epics_by_theme = _group_by(epics, lambda epic: epic.theme_id)
    features_by_epic = _group_by(features, lambda feature: feature.epic_id)

    sections: list[str] = []
    for time_frame in ("Now", "Next", "Later", None):
        frame_themes = [
            theme
            for theme in themes
            if (theme.time_frame.value if theme.time_frame else None) == time_frame
        ]
        if not frame_themes:
            continue
        heading = time_frame or "Unscheduled"
        sections.append(f"<h3>{html.escape(heading)}</h3>")
        for theme in frame_themes:
            epics_for_theme = epics_by_theme.get(theme.theme_id, [])
            features_for_theme = [
                feature
                for epic in epics_for_theme
                for feature in features_by_epic.get(epic.epic_id, [])
            ]
            sections.append(
                "".join(
                    [
                        '<div class="card">',
                        f"<strong>{html.escape(theme.title)}</strong>",
                        f'<p class="muted">{html.escape(theme.description or "")}</p>',
                        (
                            f'<p class="muted">Epics: {len(epics_for_theme)} | '
                            f"Features: {len(features_for_theme)}</p>"
                        ),
                        "</div>",
                    ]
                )
            )
    return "".join(sections)


def _render_stories_table(stories: list[UserStory]) -> str:
    if not stories:
        return '<p class="muted">No stories available.</p>'

    rows = [
        (
            "<tr>"
            f"<td>{story.story_id}</td>"
            f"<td>{html.escape(story.title)}</td>"
            f"<td>{html.escape(story.persona or '')}</td>"
            f"<td>{html.escape(story.status.value)}</td>"
            f"<td>{story.story_points or ''}</td>"
            f"<td>{html.escape(story.spec_item_ids_json)}</td>"
            f"<td>{_render_acceptance_criteria(story.acceptance_criteria_json)}</td>"
            "</tr>"
        )
        for story in stories
    ]

    return (
        "<table>"
        "<thead><tr>"
        "<th>ID</th><th>Title</th><th>Persona</th><th>Status</th><th>Points</th>"
        "<th>Specification Items</th><th>Acceptance Criteria</th>"
        "</tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table>"
    )


def _render_acceptance_criteria(acceptance_criteria_json: str) -> str:
    """Render canonical Story criteria as escaped semantic list items."""
    try:
        criteria = json.loads(acceptance_criteria_json)
    except json.JSONDecodeError as exc:
        raise ValueError(_ACCEPTANCE_CRITERIA_INVALID) from exc
    if (
        not isinstance(criteria, list)
        or not criteria
        or not all(
            isinstance(criterion, str) and criterion.strip() for criterion in criteria
        )
        or canonical_json(criteria) != acceptance_criteria_json
    ):
        raise ValueError(_ACCEPTANCE_CRITERIA_INVALID)
    return (
        '<ul class="acceptance-criteria">'
        + "".join(f"<li>{html.escape(criterion)}</li>" for criterion in criteria)
        + "</ul>"
    )


def _render_all_stories_table(stories: list[UserStory]) -> str:
    if not stories:
        return '<p class="muted">No stories available.</p>'

    ordered_stories = sorted(
        stories, key=lambda story: (story.rank or "", story.story_id or 0)
    )
    rows = [
        (
            "<tr>"
            f"<td>{story.story_id}</td>"
            f"<td>{html.escape(story.title)}</td>"
            f"<td>{html.escape(story.status.value)}</td>"
            f"<td>{html.escape('yes' if bool(story.is_superseded) else 'no')}</td>"
            f"<td>{story.accepted_spec_version_id}</td>"
            f"<td>{html.escape(story.spec_item_ids_json)}</td>"
            "</tr>"
        )
        for story in ordered_stories
    ]

    return (
        "<table>"
        "<thead><tr>"
        "<th>ID</th><th>Title</th><th>Status</th><th>Superseded</th>"
        "<th>Spec Version</th><th>Specification Items</th>"
        "</tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table>"
    )


def _render_sprint_summary(
    sprints: list[Sprint],
    stories: list[UserStory],
    sprint_story_map: dict[int, list[int]],
) -> str:
    sprint = _pick_current_sprint(sprints)
    if not sprint:
        return '<p class="muted">No sprint data available.</p>'

    sprint_story_ids = set(sprint_story_map.get(sprint.sprint_id or 0, []))
    sprint_stories = [story for story in stories if story.story_id in sprint_story_ids]
    done_count = sum(1 for story in sprint_stories if story.status == StoryStatus.DONE)
    total = len(sprint_stories)
    completion = (done_count / total) * 100 if total else 0

    rows = [
        "<tr>"
        f"<td>{story.story_id}</td>"
        f"<td>{html.escape(story.title)}</td>"
        f"<td>{html.escape(story.status.value)}</td>"
        "</tr>"
        for story in sprint_stories
    ]

    return (
        '<div class="card">'
        f"<p><strong>Goal:</strong> {html.escape(sprint.goal or 'Unnamed sprint')}</p>"
        f"<p><strong>Dates:</strong> {sprint.start_date} → {sprint.end_date}</p>"
        f"<p><strong>Status:</strong> {sprint.status.value}</p>"
        f"<p><strong>Completion:</strong> {completion:.1f}% ({done_count}/{total})</p>"
        "</div>"
        "<table>"
        "<thead><tr><th>ID</th><th>Story</th><th>Status</th></tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table>"
    )


def _render_spec_metadata(meta: dict[str, Any]) -> str:
    items = {
        "Spec version": meta.get("spec_version_id") or "-",
        "Spec hash": meta.get("spec_hash") or "-",
        "Acceptance decision": meta.get("specification_decision_id") or "-",
        "Accepted by": meta.get("accepted_by") or "-",
        "Accepted at": meta.get("accepted_at") or "-",
        "Notes": meta.get("acceptance_notes") or "-",
        "Candidate fingerprint": meta.get("candidate_fingerprint") or "-",
        "Payload fingerprint": meta.get("payload_fingerprint") or "-",
        "Source manifest fingerprint": (meta.get("source_manifest_fingerprint") or "-"),
    }
    rows = "".join(
        f"<tr><th>{html.escape(str(label))}</th><td>{html.escape(str(value))}</td></tr>"
        for label, value in items.items()
    )
    return "<table><tbody>" + rows + "</tbody></table>"


def _render_spec_status_badge(meta: dict[str, Any]) -> str:
    status = meta.get("status", "draft")
    if status == "approved":
        return '<span class="badge badge-ok">approved</span>'
    return '<span class="badge badge-warn">draft</span>'


def _extract_markdown_headings(text: str) -> list[tuple[int, str]]:
    headings: list[tuple[int, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            title = stripped[level:].strip()
            if title:
                headings.append((min(max(level, 1), 6), title))
    return headings


def _render_spec_toc(headings: list[tuple[int, str]]) -> str:
    if not headings:
        return ""
    items = "".join(
        f'<li class="toc-level-{level}">{html.escape(title)}</li>'
        for level, title in headings
    )
    return (
        '<div class="card"><strong>Contents</strong><ul class="toc">'
        + items
        + "</ul></div>"
    )


def _pick_current_sprint(sprints: list[Sprint]) -> Sprint | None:
    if not sprints:
        return None
    active = [sprint for sprint in sprints if sprint.status.value == "Active"]
    if active:
        return sorted(active, key=lambda sprint: sprint.end_date, reverse=True)[0]
    return sorted(sprints, key=lambda sprint: sprint.end_date, reverse=True)[0]


def _select_refined_current_sprint_stories(
    stories: list[UserStory],
    sprints: list[Sprint],
    sprint_story_map: dict[int, list[int]],
) -> list[UserStory]:
    sprint = _pick_current_sprint(sprints)
    if not sprint or not sprint.sprint_id:
        return []

    sprint_story_ids = set(sprint_story_map.get(sprint.sprint_id, []))
    if not sprint_story_ids:
        return []

    selected = [
        story
        for story in stories
        if story.story_id in sprint_story_ids and not bool(story.is_superseded)
    ]
    return sorted(selected, key=lambda story: (story.rank or "", story.story_id or 0))


def _group_by(
    items: Iterable[Any], key_fn: Callable[[Any], Any]
) -> dict[Any, list[Any]]:
    grouped: dict[Any, list[Any]] = {}
    for item in items:
        key = key_fn(item)
        grouped.setdefault(key, []).append(item)
    return grouped


def _load_sprint_story_map(
    session: Session,
    sprint_ids: list[int | None],
) -> dict[int, list[int]]:
    valid_ids = [sid for sid in sprint_ids if sid is not None]
    if not valid_ids:
        return {}
    rows = [
        row
        for row in session.exec(select(SprintStory)).all()
        if row.sprint_id in valid_ids
    ]
    mapping: dict[int, list[int]] = {}
    for row in rows:
        mapping.setdefault(row.sprint_id, []).append(row.story_id)
    return mapping
