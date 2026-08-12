"""Structural regression contract for the issue #199 lifecycle hard break."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from models import product_definition, specs
from services.specs.profile_content import (
    SpecContentNormalizationError,
    normalize_spec_content_for_registry,
)
from workflow.definitions.authority import AUTHORITY_NODES
from workflow.definitions.product_discovery import PRODUCT_DISCOVERY_NODES
from workflow.requests import product_discovery as product_discovery_requests

REPOSITORY_ROOT: Path = Path(__file__).resolve().parents[1]


def _source(relative_path: str) -> str:
    """Read one explicitly scoped active-runtime source file."""
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


def _tree(relative_path: str) -> ast.Module:
    """Parse one explicitly scoped active-runtime source file."""
    return ast.parse(_source(relative_path), filename=relative_path)


def _top_level_definitions(relative_path: str) -> dict[str, ast.AST]:
    """Return named top-level classes and functions from one module."""
    return {
        node.name: node
        for node in _tree(relative_path).body
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _string_dict_keys(relative_path: str, assignment_name: str) -> set[str]:
    """Return literal string keys from one named top-level dictionary."""
    value: ast.expr | None = None
    for node in _tree(relative_path).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == assignment_name
            for target in node.targets
        ):
            value = node.value
            break
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == assignment_name
        ):
            value = node.value
            break
    if not isinstance(value, ast.Dict):
        return set()
    return {
        key.value
        for key in value.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


def _api_route_paths() -> set[str]:
    """Return literal FastAPI paths declared by active route decorators."""
    paths: set[str] = set()
    for node in ast.walk(_tree("api.py")):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"delete", "get", "patch", "post", "put"}:
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            paths.add(first.value)
    return paths


def _class_fields(node: ast.AST) -> set[str]:
    """Return annotated field names from one top-level class definition."""
    if not isinstance(node, ast.ClassDef):
        return set()
    return {
        item.target.id
        for item in node.body
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
    }


def _function_identifiers(node: ast.AST) -> set[str]:
    """Return referenced identifier and attribute names inside one function."""
    identifiers: set[str] = set()
    for item in ast.walk(node):
        if isinstance(item, ast.Name):
            identifiers.add(item.id)
        elif isinstance(item, ast.Attribute):
            identifiers.add(item.attr)
    return identifiers


def _valid_v1_payload() -> str:
    """Return a valid frozen-v1 payload used only to prove active rejection."""
    return json.dumps(
        {
            "schema_version": "agileforge.spec.v1",
            "artifact_id": "SPEC.issue-199-hard-break",
            "title": "Frozen v1 input",
            "status": "draft",
            "version": "0.1",
            "created_at": "2026-08-11",
            "updated_at": "2026-08-11",
            "summary": "This valid historical payload must not enter active runtime.",
            "problem_statement": "Issue #199 requires a new hard-break schema.",
            "items": [],
            "relations": [],
            "controlled_terms": [],
            "external_references": [],
            "rendering": {
                "markdown_profile": "agileforge.spec_markdown.v1",
                "rendered_markdown_sha256": None,
            },
        }
    )


def test_discovery_domain_model_and_request_are_absent_from_active_runtime() -> None:
    """Discovery may remain an activity, never an active persisted command."""
    assert not hasattr(product_definition, "DiscoveryArtifact")
    assert not hasattr(product_discovery_requests, "RecordDiscoveryArtifact")


def test_discovery_node_is_absent_from_active_workflow_graph() -> None:
    """Accepted Product Goal must route directly to Specification authoring."""
    node_ids = {node.node_id for node in PRODUCT_DISCOVERY_NODES}
    request_kinds = {node.request_kind for node in PRODUCT_DISCOVERY_NODES}

    assert "discovery.record" not in node_ids
    assert "record_discovery_artifact" not in request_kinds


def test_discovery_route_and_cli_command_are_absent() -> None:
    """Active API and CLI routing must not advertise persisted Discovery."""
    route_paths = _api_route_paths()
    api_request_kinds = _string_dict_keys("api.py", "SEMANTIC_API_PATHS")
    cli_request_kinds = _string_dict_keys(
        "cli/workflow_commands.py",
        "COMMAND_PREFIXES",
    )

    assert not any(route.endswith("/discovery") for route in route_paths)
    assert "record_discovery_artifact" not in api_request_kinds
    assert "record_discovery_artifact" not in cli_request_kinds


def test_dashboard_has_no_mandatory_discovery_surface() -> None:
    """The Project dashboard must not fetch or render a Discovery lifecycle card."""
    html = _source("frontend/project.html")
    javascript = _source("frontend/project.js")

    assert 'id="discovery-heading"' not in html
    assert 'id="discovery-panel"' not in html
    assert "function discoveryPanelMarkup" not in javascript
    assert "`${base}/discovery`" not in javascript


def test_active_registry_rejects_frozen_v1_and_plain_text() -> None:
    """Historical v1 and prose cannot enter the active Specification registry."""
    for raw_content in (_valid_v1_payload(), "# Plain-text specification"):
        with pytest.raises(SpecContentNormalizationError) as error:
            normalize_spec_content_for_registry(raw_content)
        assert error.value.error_code == "UNSUPPORTED_SPECIFICATION_SCHEMA"


def test_active_compiler_entrypoints_have_no_raw_content_or_file_bypass() -> None:
    """Authority compilation must resolve only an accepted typed Specification."""
    definitions = _top_level_definitions("services/specs/compiler_service.py")
    retired_entrypoints = {
        "CompileSpecAuthorityInput",
        "PreviewSpecAuthorityInput",
        "UpdateSpecAndCompileAuthorityInput",
        "_detect_spec_source_format",
        "_load_update_spec_content",
        "compile_spec_authority",
        "preview_spec_authority",
        "update_spec_and_compile_authority",
    }
    assert retired_entrypoints.isdisjoint(definitions)

    raw_input_fields = {"content", "content_ref", "spec_content"}
    active_input = definitions.get("CompileSpecAuthorityForVersionInput")
    assert active_input is not None
    assert raw_input_fields.isdisjoint(_class_fields(active_input))

    loader = definitions.get("_load_spec_content_for_compile")
    if loader is not None:
        identifiers = _function_identifiers(loader)
        assert {"Path", "content_ref", "read_text"}.isdisjoint(identifiers)


def test_separate_human_authority_review_model_and_node_remain() -> None:
    """Issue #199 must not collapse Authority compilation and human review."""
    assert hasattr(specs, "SpecAuthorityAcceptance")

    nodes = {node.node_id: node for node in AUTHORITY_NODES}
    assert nodes["authority.compile"].request_kind == "compile_authority"
    assert nodes["authority.review"].request_kind == "decide_authority"
    assert nodes["authority.compile"] is not nodes["authority.review"]
