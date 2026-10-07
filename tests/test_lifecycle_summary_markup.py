# tests/test_lifecycle_summary_markup.py
"""Verify the project template stays neutral until lifecycle evidence arrives."""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PROJECT_TEMPLATE: Path = (
    Path(__file__).resolve().parents[1] / "frontend" / "project.html"
)
VOID_ELEMENTS: frozenset[str] = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


@dataclass
class MarkupElement:
    """Retain structure, attributes, and descendant text without running scripts."""

    tag: str
    attrs: dict[str, str | None]
    parent: MarkupElement | None = None
    children: list[MarkupElement] = field(default_factory=list)
    text_chunks: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        """Return rendered text with template whitespace normalized."""
        return " ".join("".join(self.text_chunks).split())


class ProjectMarkupParser(HTMLParser):
    """Parse the actual template and expose its ID and script bindings."""

    def __init__(self) -> None:
        """Initialize an empty document and its binding indexes."""
        super().__init__(convert_charrefs=True)
        self.root = MarkupElement("document", {})
        self.stack = [self.root]
        self.by_id: dict[str, MarkupElement] = {}
        self.scripts: list[MarkupElement] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Attach a node and retain its ID and script attributes."""
        node = MarkupElement(tag, dict(attrs), self.stack[-1])
        self.stack[-1].children.append(node)
        element_id = node.attrs.get("id")
        if element_id is not None:
            assert element_id not in self.by_id, f"duplicate DOM binding: {element_id}"
            self.by_id[element_id] = node
        if tag == "script":
            self.scripts.append(node)
        if tag not in VOID_ELEMENTS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Attach a self-closing node without retaining an open scope."""
        self.handle_starttag(tag, attrs)
        if tag not in VOID_ELEMENTS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        """Close the matching open scope."""
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        """Retain text for each ancestor's rendered content."""
        for node in self.stack:
            node.text_chunks.append(data)


def _project_markup() -> ProjectMarkupParser:
    parser = ProjectMarkupParser()
    parser.feed(PROJECT_TEMPLATE.read_text(encoding="utf-8"))
    parser.close()
    return parser


def test_initial_lifecycle_labels_are_neutral() -> None:
    """Initial template labels must not advertise unconfirmed work or acceptance."""
    markup = _project_markup()
    for element_id in (
        "cockpit-goal-status",
        "cockpit-active-stage-label",
        "cockpit-phase-detail",
        "nav-vision-badge",
        "nav-goal-badge",
        "nav-specification-badge",
        "nav-backlog-badge",
        "nav-roadmap-badge",
        "nav-sprint-badge",
        "workbench-stage-title",
        "workbench-stage-kicker",
    ):
        assert markup.by_id[element_id].text == "Loading…", element_id
    assert markup.by_id["cockpit-vision-anchor"].text == "Vision: Loading…"


def test_legacy_badge_bindings_and_visibility_are_preserved() -> None:
    """Keep each badge inside its retained hidden navigation and stage button."""
    markup = _project_markup()
    assert "hidden" in (markup.by_id["top-cockpit"].attrs.get("class") or "").split()
    assert markup.by_id["top-cockpit"].attrs["aria-hidden"] == "true"
    navigation = markup.by_id["master-stage-nav"]
    assert "hidden" in (navigation.attrs.get("class") or "").split()
    assert navigation.attrs["aria-label"] == "Legacy lifecycle navigation"
    for element_id, stage in (
        ("nav-vision-badge", "Vision"),
        ("nav-goal-badge", "Product Goal"),
        ("nav-specification-badge", "Specification"),
        ("nav-backlog-badge", "Backlog"),
        ("nav-roadmap-badge", "Roadmap"),
        ("nav-sprint-badge", "Sprint"),
    ):
        badge = markup.by_id[element_id]
        assert "stage-status-badge" in (badge.attrs.get("class") or "").split()
        assert badge.parent is not None
        assert badge.parent.tag == "button"
        assert badge.parent.attrs["data-stage"] == stage
        ancestor = badge.parent
        while ancestor is not None and ancestor is not navigation:
            ancestor = ancestor.parent
        assert ancestor is navigation, element_id
    assert markup.by_id["nav-stories-count"].parent is not None
    assert markup.by_id["nav-stories-count"].parent.attrs["data-stage"] == "Stories"
    title_parent = markup.by_id["workbench-stage-title"].parent
    assert title_parent is not None
    assert markup.by_id["workbench-stage-actions"].parent is title_parent.parent
    sources = [
        urlsplit(source) for node in markup.scripts if (source := node.attrs.get("src"))
    ]
    local_script_paths = [source.path for source in sources if not source.netloc]
    assert local_script_paths == [
        "/dashboard/lifecycle-workspace.js",
        "/dashboard/project.js",
    ]
    project_script = next(
        source for source in sources if source.path == "/dashboard/project.js"
    )
    assert parse_qs(project_script.query).get("v")
