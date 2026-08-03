"""Export read-only project snapshots as self-contained HTML."""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from markdown import markdown as _md
from sqlmodel import Session, select

from models.core import Epic, Feature, Project, Sprint, SprintStory, Theme, UserStory
from models.db import engine as default_engine
from models.enums import StoryStatus
from models.specs import SpecRegistry
from services.specs.authority_selection import (
    accepted_compiled_authority,
    latest_accepted_authority_decision,
    latest_compiled_authority,
)
from services.specs.compiler_service import (
    CompiledAuthorityReadFailure,
    compiled_authority_read_failure,
    load_compiled_artifact,
)
from utils.spec_schemas import (
    Invariant,
    InvariantType,
    SpecAuthorityCompilationSuccess,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from sqlalchemy.engine import Engine


class _ExportSnapshotError(ValueError):
    @classmethod
    def project_not_found(cls, project_id: int) -> _ExportSnapshotError:
        message = f"Project {project_id} not found"
        return cls(message)

    @classmethod
    def compiled_authority_invalid(
        cls,
        failure: CompiledAuthorityReadFailure,
    ) -> _ExportSnapshotError:
        """Build a stable pre-write failure for an unusable selected row."""
        remediation = " ".join(failure.remediation)
        return cls(
            f"{failure.error_code}: {failure.message} "
            f"details={failure.details}. {remediation}"
        )

    @classmethod
    def authority_not_compiled(
        cls,
        *,
        project_id: int,
        spec_version_id: int,
    ) -> _ExportSnapshotError:
        """Build a stable failure for a dangling accepted authority id."""
        return cls(
            "AUTHORITY_NOT_COMPILED: Accepted compiled authority row is unavailable. "
            f"details={{'project_id': {project_id}, "
            f"'spec_version_id': {spec_version_id}}}."
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
    spec_content: str
    spec_meta: dict[str, Any]
    authority: SpecAuthorityCompilationSuccess | None


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
    output_dir.mkdir(parents=True, exist_ok=True)

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

        approved_spec = _get_latest_approved_spec(session, project_id)
        spec_content, spec_meta = _resolve_spec_content(project, approved_spec)
        authority = _load_compiled_authority(session, approved_spec)

    render_context = _SnapshotRenderContext(
        project=project,
        themes=themes,
        epics=epics,
        features=features,
        stories=stories,
        all_stories=all_stories,
        sprints=sprints,
        sprint_story_map=sprint_story_map,
        spec_content=spec_content,
        spec_meta=spec_meta,
        authority=authority,
    )
    html_output = _render_snapshot_html(render_context)

    filename = f"snapshot_project_{project.project_id}.html"
    output_path = output_dir / filename
    output_path.write_text(html_output, encoding="utf-8")
    return output_path


def _get_latest_approved_spec(
    session: Session,
    project_id: int,
) -> SpecRegistry | None:
    specs = list(
        session.exec(
            select(SpecRegistry).where(
                SpecRegistry.project_id == project_id,
                SpecRegistry.status == "approved",
            )
        ).all()
    )
    if not specs:
        return None
    return sorted(
        specs,
        key=lambda spec: spec.approved_at or spec.created_at,
        reverse=True,
    )[0]


def _resolve_spec_content(
    project: Project,
    approved_spec: SpecRegistry | None,
) -> tuple[str, dict[str, Any]]:
    if approved_spec:
        meta: dict[str, Any] = {
            "status": "approved",
            "spec_version_id": approved_spec.spec_version_id,
            "approved_by": approved_spec.approved_by,
            "approved_at": approved_spec.approved_at,
            "approval_notes": approved_spec.approval_notes,
            "content_ref": approved_spec.content_ref,
        }
        return approved_spec.content, meta

    meta: dict[str, Any] = {
        "status": "draft",
        "spec_version_id": None,
        "approved_by": None,
        "approved_at": None,
        "approval_notes": None,
        "content_ref": project.spec_file_path,
    }
    return project.technical_spec or "(No technical spec available)", meta


def _load_compiled_authority(
    session: Session,
    approved_spec: SpecRegistry | None,
) -> SpecAuthorityCompilationSuccess | None:
    if not approved_spec or not approved_spec.spec_version_id:
        return None

    acceptance = latest_accepted_authority_decision(
        session,
        project_id=approved_spec.project_id,
        spec_version_id=approved_spec.spec_version_id,
    )
    authority = (
        accepted_compiled_authority(
            session,
            project_id=approved_spec.project_id,
            spec_version_id=approved_spec.spec_version_id,
        )
        if acceptance is not None
        else latest_compiled_authority(
            session,
            spec_version_id=approved_spec.spec_version_id,
        )
    )
    if authority is None and acceptance is not None:
        raise _ExportSnapshotError.authority_not_compiled(
            project_id=approved_spec.project_id,
            spec_version_id=approved_spec.spec_version_id,
        )
    if authority is None:
        return None

    loaded = load_compiled_artifact(authority)
    failure = compiled_authority_read_failure(
        loaded,
        project_id=approved_spec.project_id,
        spec_version_id=approved_spec.spec_version_id,
        authority_id=authority.authority_id,
    )
    if failure is not None:
        raise _ExportSnapshotError.compiled_authority_invalid(failure)
    return cast("SpecAuthorityCompilationSuccess", loaded.artifact)


def _render_snapshot_html(context: _SnapshotRenderContext) -> str:
    generated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    roadmap_html = _render_roadmap(
        context.themes,
        context.epics,
        context.features,
        context.all_stories,
    )
    stories_html = _render_stories_table(
        context.stories,
        context.epics,
        context.features,
        context.themes,
    )
    full_backlog_html = _render_all_stories_table(
        context.all_stories,
        context.epics,
        context.features,
        context.themes,
    )
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
    compiled_html = _render_compiled_authority(context.authority)
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
    <h2>Compiled Spec Authority</h2>
    {compiled_html}
  </div>

  <div class="section">
    <h2>Current Sprint Refined Stories</h2>
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
        vision_html=_markdown(context.project.vision or "(No vision set)"),
        roadmap_html=roadmap_html,
        spec_status_badge=_render_spec_status_badge(context.spec_meta),
        spec_meta_html=_render_spec_metadata(context.spec_meta),
        spec_toc_html=_render_spec_toc(spec_toc),
        spec_html=spec_html,
        compiled_html=compiled_html,
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
        return "No refined stories in the current sprint."
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
    refined = sum(1 for story in story_list if bool(story.is_refined))
    superseded = sum(1 for story in story_list if bool(story.is_superseded))
    return (
        f"Total {len(story_list)} | Refined {refined} | "
        f"Not refined {len(story_list) - refined} | Superseded {superseded}"
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
    stories: list[UserStory],
) -> str:
    if not themes:
        return '<p class="muted">No roadmap themes available.</p>'

    epics_by_theme = _group_by(epics, lambda epic: epic.theme_id)
    features_by_epic = _group_by(features, lambda feature: feature.epic_id)
    stories_by_feature = _group_by(stories, lambda story: story.feature_id)

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
            stories_for_theme = [
                story
                for feature in features_for_theme
                for story in stories_by_feature.get(feature.feature_id, [])
            ]
            sections.append(
                "".join(
                    [
                        '<div class="card">',
                        f"<strong>{html.escape(theme.title)}</strong>",
                        f'<p class="muted">{html.escape(theme.description or "")}</p>',
                        (
                            f'<p class="muted">Epics: {len(epics_for_theme)} | '
                            f"Features: {len(features_for_theme)} | "
                            f"Stories: {len(stories_for_theme)}</p>"
                        ),
                        "</div>",
                    ]
                )
            )
    return "".join(sections)


def _render_stories_table(
    stories: list[UserStory],
    epics: list[Epic],
    features: list[Feature],
    themes: list[Theme],
) -> str:
    if not stories:
        return '<p class="muted">No stories available.</p>'

    feature_to_epic = {feature.feature_id: feature.epic_id for feature in features}
    epic_to_theme = {epic.epic_id: epic.theme_id for epic in epics}
    theme_by_id = {theme.theme_id: theme for theme in themes}
    feature_by_id = {feature.feature_id: feature for feature in features}

    rows: list[str] = []
    for story in stories:
        epic_id = feature_to_epic.get(story.feature_id)
        theme_id = epic_to_theme.get(epic_id)
        theme = theme_by_id.get(theme_id) if theme_id in theme_by_id else None
        feature = feature_by_id.get(story.feature_id) if story.feature_id else None
        theme_title = theme.title if theme else ""
        feature_title = feature.title if feature else ""
        rows.append(
            "<tr>"
            f"<td>{story.story_id}</td>"
            f"<td>{html.escape(story.title)}</td>"
            f"<td>{html.escape(story.persona or '')}</td>"
            f"<td>{html.escape(story.status.value)}</td>"
            f"<td>{story.story_points or ''}</td>"
            f"<td>{html.escape(theme_title)}</td>"
            f"<td>{html.escape(feature_title)}</td>"
            f"<td>{html.escape(story.acceptance_criteria or '')}</td>"
            "</tr>"
        )

    return (
        "<table>"
        "<thead><tr>"
        "<th>ID</th><th>Title</th><th>Persona</th><th>Status</th><th>Points</th>"
        "<th>Theme</th><th>Feature</th><th>Acceptance Criteria</th>"
        "</tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table>"
    )


def _render_all_stories_table(
    stories: list[UserStory],
    epics: list[Epic],
    features: list[Feature],
    themes: list[Theme],
) -> str:
    if not stories:
        return '<p class="muted">No stories available.</p>'

    feature_to_epic = {feature.feature_id: feature.epic_id for feature in features}
    epic_to_theme = {epic.epic_id: epic.theme_id for epic in epics}
    theme_by_id = {theme.theme_id: theme for theme in themes}
    feature_by_id = {feature.feature_id: feature for feature in features}

    rows: list[str] = []
    ordered_stories = sorted(
        stories, key=lambda story: (story.rank or "", story.story_id or 0)
    )
    for story in ordered_stories:
        epic_id = feature_to_epic.get(story.feature_id)
        theme_id = epic_to_theme.get(epic_id)
        theme = theme_by_id.get(theme_id) if theme_id in theme_by_id else None
        feature = feature_by_id.get(story.feature_id) if story.feature_id else None
        rows.append(
            "<tr>"
            f"<td>{story.story_id}</td>"
            f"<td>{html.escape(story.title)}</td>"
            f"<td>{html.escape(story.status.value)}</td>"
            f"<td>{html.escape('yes' if bool(story.is_refined) else 'no')}</td>"
            f"<td>{html.escape('yes' if bool(story.is_superseded) else 'no')}</td>"
            f"<td>{html.escape(story.story_origin or '')}</td>"
            f"<td>{story.accepted_spec_version_id or ''}</td>"
            f"<td>{html.escape(theme.title if theme else '')}</td>"
            f"<td>{html.escape(feature.title if feature else '')}</td>"
            "</tr>"
        )

    return (
        "<table>"
        "<thead><tr>"
        "<th>ID</th><th>Title</th><th>Status</th><th>Refined</th><th>Superseded</th>"
        "<th>Origin</th><th>Spec Version</th><th>Theme</th><th>Feature</th>"
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


def _render_compiled_authority(
    authority: SpecAuthorityCompilationSuccess | None,
) -> str:
    if not authority:
        return '<p class="muted">No compiled authority available.</p>'

    invariants = "".join(
        f"<li>{html.escape(_render_invariant_summary(inv))}</li>"
        for inv in authority.invariants
    )
    scope_themes = "".join(
        f"<li>{html.escape(theme)}</li>" for theme in authority.scope_themes
    )
    gaps = "".join(f"<li>{html.escape(gap)}</li>" for gap in authority.gaps)
    raw_json = html.escape(authority.model_dump_json(indent=2))

    sections: list[str] = [
        '<div class="card">',
        f"<p><strong>Compiler:</strong> {html.escape(authority.compiler_version)}</p>",
        f"<p><strong>Prompt hash:</strong> {html.escape(authority.prompt_hash)}</p>",
        "<h3>Scope Themes</h3><ul>",
        scope_themes,
        "</ul>",
        "<h3>Invariants</h3><ul>",
        invariants,
        "</ul>",
    ]

    if gaps:
        sections.extend(["<h3>Spec Gaps</h3><ul>", gaps, "</ul>"])

    sections.extend(
        [
            "</div>",
            "<h3>Compiled Authority JSON</h3>",
            f"<pre>{raw_json}</pre>",
        ]
    )

    return "".join(sections)


def _render_invariant_summary(invariant: Invariant) -> str:
    if invariant.type == InvariantType.FORBIDDEN_CAPABILITY:
        capability = getattr(invariant.parameters, "capability", "")
        return f"{invariant.id}: FORBIDDEN_CAPABILITY {capability}"
    if invariant.type == InvariantType.REQUIRED_FIELD:
        field_name = getattr(invariant.parameters, "field_name", "")
        return f"{invariant.id}: REQUIRED_FIELD {field_name}"
    if invariant.type == InvariantType.MAX_VALUE:
        field_name = getattr(invariant.parameters, "field_name", "")
        max_value = getattr(invariant.parameters, "max_value", "")
        return f"{invariant.id}: MAX_VALUE {field_name} <= {max_value}"
    return f"{invariant.id}: {invariant.type}"


def _render_spec_metadata(meta: dict[str, Any]) -> str:
    items = {
        "Spec version": meta.get("spec_version_id") or "-",
        "Approved by": meta.get("approved_by") or "-",
        "Approved at": meta.get("approved_at") or "-",
        "Notes": meta.get("approval_notes") or "-",
        "Content ref": meta.get("content_ref") or "-",
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
        if story.story_id in sprint_story_ids
        and bool(story.is_refined)
        and not bool(story.is_superseded)
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
