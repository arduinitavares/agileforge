"""Prove the retired product-definition surface is closed after issue #210."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import re
import subprocess  # nosec B404
import sys
from collections import Counter
from pathlib import Path
from typing import ForwardRef

import pytest
from sqlmodel import SQLModel

ROOT = Path(__file__).resolve().parents[2]
SCANNER_PATH = "tests/issue_210/test_authority_surface_removed.py"
FRAMED_LAYER_TOKEN = "Authority " + "layer"
FRAMED_LAYER_SENTENCE = f"The former {FRAMED_LAYER_TOKEN} is historical."

REMOVED_MODULES = (
    "adapters.adk.agents.authority",
    "adapters.adk.agents.specification",
    "adapters.adk.prompts.specification",
    "services.agent_workbench.authority_projection",
    "services.authority_compilation_input",
    "services.authority_review_projection",
    "services.contracts.authority",
    "services.contracts.authority_input_v2",
    "services.contracts.specification",
    "services.contracts.specification_normalizer",
    "services.specs.authority_curation_diff",
    "services.specs.authority_quality",
    "services.specs.authority_selection",
    "services.specs.compiler_service",
    "utils.authority_curation_trace",
    "utils.schemes",
    "utils.spec_authority_assumptions",
    "utils.spec_authority_ir",
)

REMOVED_PATHS = (
    "artifacts/compiled_authority_product_4.html",
    "artifacts/generate_example",
    "artifacts/spec_authority_extraction",
    "benchmarks/authority-quality",
    "input_for_test.txt",
    "scripts/_recompile_authority.py",
    "scripts/authority_quality_benchmark.py",
    "scripts/build_validation_benchmark_cases.py",
    "scripts/export_benchmark_for_labeling.py",
    "scripts/import_human_labels.py",
    "scripts/summarize_smoke_runs.py",
    "scripts/verify_smoke_runs.py",
    "specs/smoke_spec_full.md",
    "tests/fixtures/authority",
    "tests/test_summarize_smoke_runs.py",
    "tests/test_verify_smoke_runs.py",
)

RELOCATED_FIXTURE_HASHES = {
    "agileforge/generated-spec/spec.json": (
        "bcb3429a293f69c3d6f043007a5757ec35ab58b887e631f703555f2489a0e562"
    ),
    "agileforge/generated-spec/structured-spec-review.md": (
        "1e237a43682c98123f915c83aefc534508967047fc2b24fad5d1f3a536a7fd06"
    ),
    "agileforge/gold-spec/change-log.md": (
        "b8d080e546cddefa2d4304ca05d6d0e152aa7a87e84b1479155343439b7d0642"
    ),
    "agileforge/gold-spec/spec.json": (
        "c7bf11bf6979e959c8b321dccc02083ae947f70732d350b40a5940ea6cc2a598"
    ),
    "oracle/evaluation.json": (
        "b17bced524ae49c8d6dc316f37c352bb54f1200fcbd75804264eb19c6272080a"
    ),
    "source/source.md": (
        "5a20d93949756331ae85a38d0f151ca4849c6e41ec928154e3f4f90eff35b219"
    ),
    "source/source.sha256": (
        "52df020d1b6d8abee5f5d3f551d4287391a26674f1e04e903638f8cd0c6561ac"
    ),
}

OPAQUE_FILE_HASHES = {
    "tests/fixtures/issue_200/CONTEXT.md": (
        "7f3d98698f2741a3a200a7558c98ee0415bbd670c8184c406cc854db44de64d7"
    ),
    "tests/fixtures/issue_200/complete-provider-output.json": (
        "252b3ab7edafadf2af7d43bb9a6ee8bafea2d396a18199990ea883001589a9a5"
    ),
    "tests/fixtures/issue_200/to-spec-source.md": (
        "7d1cb963d06f9e40c82204bc32093b505f6b10bc46027162104b05b4a0ba507a"
    ),
}

OPAQUE_GOLD_HASHES = {
    "tests/fixtures/issue_210/gold/canonical-specification.json": (
        "4f39ae394d3910bc52d73256eddc11edd66e57074025e1ec7f037e8e69a33025"
    ),
    "tests/fixtures/issue_210/gold/manifest.json": (
        "42d463c0170750047e9453119b899e999391c2c07bf5caf1142ebaf7d38f13b0"
    ),
    "tests/fixtures/issue_210/gold/specification-candidate.json": (
        "22e1cfa4f72306089115488f7b56c92e6a3f44b999d3fc053c7a63ba81ad063d"
    ),
}

OPAQUE_LEGACY_HASHES = {
    "tests/fixtures/issue_210/legacy_authority/outer-envelope.json": (
        "5e18990c5e304782e14b18b3d119bf49e70a310659ad3c3d665ee4394a3457eb"
    ),
    "tests/fixtures/issue_210/legacy_authority/compiler-input.json": (
        "111a61e61d5bdeb801e510a9defe41158632338512d617d108835c076a3b7467"
    ),
    "tests/fixtures/issue_210/legacy_authority/authority-input.json": (
        "34bbc82966ce3bd05123e039667c7ee2fa40b9b9a4a30dff42877fbb57ee9a19"
    ),
    "tests/fixtures/issue_210/legacy_authority/initial-output.json": (
        "88f091dc2cde24bd0113d954018cefd8a0b8f2e99eea1bc39d04dfadaa81a1c6"
    ),
    "tests/fixtures/issue_210/legacy_authority/repaired-output.json": (
        "4670cc02da585c64b140d017b0387241dba0a58856a4677621fa46c066ab7594"
    ),
    "tests/fixtures/issue_210/legacy_authority/manifest.json": (
        "bbfa26a9d72c1793989b59a3adfc860542adef59d8a92846acf19f31115266ae"
    ),
}

OPAQUE_BASELINE_HASHES = {
    "tests/fixtures/issue_210/baseline-business-schema.sql": (
        "cc32e2f6ca536112e15507793fd2c40e623190703774c4551e86dc7c2bf4988f"
    ),
}

OPAQUE_ROOT_MEMBERS = {
    "tests/fixtures/issue_200/": frozenset(
        {
            "CONTEXT.md",
            "complete-provider-output.json",
            "to-spec-source.md",
        }
    ),
    "tests/fixtures/issue_210/gold/": frozenset(
        {
            "canonical-specification.json",
            "manifest.json",
            "specification-candidate.json",
        }
    ),
    "tests/fixtures/issue_210/legacy_authority/": frozenset(
        {
            "authority-input.json",
            "compiler-input.json",
            "initial-output.json",
            "manifest.json",
            "outer-envelope.json",
            "repaired-output.json",
        }
    ),
}

OPAQUE_HASHES = {
    **OPAQUE_FILE_HASHES,
    **OPAQUE_GOLD_HASHES,
    **OPAQUE_LEGACY_HASHES,
    **OPAQUE_BASELINE_HASHES,
}

HISTORICAL_ROOTS = (
    "artifacts/historical-schema/",
    "docs/superpowers/plans/",
    "docs/superpowers/specs/",
)
SUPERSEDED_ADRS = frozenset(
    {
        "docs/adr/0002-store-discovery-artifacts-in-agileforge-state.md",
        "docs/adr/0003-make-to-spec-the-canonical-specification-boundary.md",
        "docs/adr/0004-register-to-spec-source-before-structuring.md",
    }
)
HISTORICAL_FEEDBACK_FILES = frozenset(
    {
        "docs/feedback/2026-08-20-issue-210-authority-contract-decision-brief.md",
        "docs/feedback/2026-08-20-issue-210-authority-ir-validation-handoff.md",
    }
)

LIVE_ROOTS = (
    ".env.example",
    "pyproject.toml",
    "README.md",
    "CONTEXT.md",
    "scrum_agentic_system_lifecycle.mmd",
    "adapters",
    "api.py",
    "artifacts",
    "benchmarks",
    "cli",
    "config",
    "docs",
    "frontend",
    "models",
    "repositories",
    "scripts",
    "services",
    "specs",
    "tests",
    "tools",
    "utils",
    "workflow",
)
TEXT_SUFFIXES = {
    "",
    ".csv",
    ".bash",
    ".html",
    ".js",
    ".json",
    ".log",
    ".md",
    ".mmd",
    ".py",
    ".sh",
    ".sql",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
    ".zsh",
}
MIN_PARAMETRIZE_ARGS = 2
MIN_NAMESPACE_CALL_ARGS = 2
FRESH_SCHEMA_PROOF_STATEMENTS = 3
EXPECTED_HASATTR_ARGUMENTS = 2
FRESH_SCHEMA_PATH = "tests/workflow/test_fresh_project_schema.py"
LEGACY_FIXTURE_REFERENCE = "legacy_authority"
LEGACY_FIXTURE_REFERENCE_PATHS = frozenset(
    {SCANNER_PATH, "tests/issue_210/test_fixture_integrity.py"}
)
PYTEST_MODULE_DISABLERS = frozenset({"importorskip", "skip", "xfail"})
DYNAMIC_BUILTINS = frozenset({"compile", "eval", "exec"})
TYPING_CONSTRUCTS = frozenset({"Annotated", "ForwardRef", "Literal", "TypeAlias"})
COMPILE_MODE_ARG_INDEX = 2
PYTEST_NO_TESTS_COLLECTED = 5
FRESH_SCHEMA_PROOF_NAMES = (
    "test_issue_210_retired_authority_tables_are_rejected",
    "test_issue_210_retired_columns_are_rejected",
)

PROHIBITED_TEXT = re.compile(
    r"\bAuthority layer\b"
    r"|\bAuthority[A-Z][A-Za-z0-9_]*\b"
    r"|\bSpecAuthority(?:[A-Z][A-Za-z0-9_]*)?\b"
    r"|CompiledSpecAuthority"
    r"|\b(?:compiled|spec)_authority(?:_[a-z][a-z0-9_]*)?\b"
    r"|\bauthority_(?:id|fingerprint|compile|repair|compiler|quality|selection"
    r"|curation|projection|review|input|gate|assumptions|ir|feedback)"
    r"(?:_[a-z0-9_]+)?\b"
    r"|relevant_invariant_ids|evaluated_invariant_ids"
    r"|SPEC_AUTHORITY_[A-Z_]+|AUTHORITY_[A-Z_]+"
    r"|authority\.(?:compile|review|repair|curate)"
    r"|compile_authority|decide_authority|repair_authority|curate_authority"
    r"|/authority"
)

PROHIBITED_ALGEBRA = re.compile(
    r"\b(?:InvariantType|InvariantParameters|InvariantStrength"
    r"|AuthorityQualityMergedItem|AuthorityQualityReport"
    r"|AuthorityQualityReviewGroup|AuthorityQualitySummary"
    r"|EligibleFeatureRule|SourceMapEntry|ForbiddenCapabilityParams"
    r"|RequiredFieldParams|MaxValueParams|DataContractParams"
    r"|RouteContractParams|StateTransitionParams|UserInteractionParams"
    r"|VisibilityRuleParams)\b"
)

RETIRED_PATH_COMPONENT = re.compile(
    r"(^|/)(?:authority|spec_authority|compiled_authority|authority-quality)"
    r"(?:[^/]*)(/|$)"
)

MODEL_DB_RETIRED_TABLES = frozenset(
    {
        "discovery_artifacts",
        "compiled_spec_authority",
        "spec_authority_acceptance",
        "authority_feedback_attempts",
        "authority_curation_attempts",
    }
)
MODEL_DB_RETIRED_COLUMNS = frozenset({"authority_id", "authority_fingerprint"})
MODEL_CLASS_NAMES = frozenset({"CompiledSpecAuthority", "SpecAuthorityAcceptance"})
REMOVED_MODEL_MODULE = "models.authority_curation"
PRODUCTION_READ_RETIRED_PREFIX = "AuthorityReview"
LEGACY_TASK_METADATA_V1 = (
    '{"artifact_targets": [], "checklist_items": [], '
    '"relevant_invariant_ids": [], "task_kind": "other", '
    '"version": "task_metadata.v1", "workstream_tags": []}'
)

NOT_IN_RULES = {
    "tests/test_spec_validation_modes.py": {
        "validate_story_with_spec_authority": frozenset(
            {"tool_module.__all__", "service_package.__all__"}
        ),
    },
    "tests/test_roadmap_runtime.py": {
        "compiled_authority": frozenset({"dumped"}),
        "compiled_authority_cached": frozenset({"dumped"}),
    },
    "tests/test_sprint_selection.py": {
        "compiled_authority": frozenset({"builder_input"}),
        "evaluated_invariant_ids": frozenset({"dumped"}),
    },
    "tests/workflow/test_direct_specification_lineage.py": {
        "authority_id": frozenset({"type(request).model_fields"}),
        "authority_fingerprint": frozenset({"type(request).model_fields"}),
    },
}

SCANNER_POLICY_ASSIGNMENTS = frozenset(
    {
        "REMOVED_MODULES",
        "REMOVED_PATHS",
        "RELOCATED_FIXTURE_HASHES",
        "OPAQUE_FILE_HASHES",
        "OPAQUE_GOLD_HASHES",
        "OPAQUE_LEGACY_HASHES",
        "OPAQUE_BASELINE_HASHES",
        "OPAQUE_ROOT_MEMBERS",
        "OPAQUE_HASHES",
        "HISTORICAL_ROOTS",
        "SUPERSEDED_ADRS",
        "HISTORICAL_FEEDBACK_FILES",
        "PROHIBITED_TEXT",
        "PROHIBITED_ALGEBRA",
        "RETIRED_PATH_COMPONENT",
        "MODEL_DB_RETIRED_TABLES",
        "MODEL_DB_RETIRED_COLUMNS",
        "MODEL_CLASS_NAMES",
        "REMOVED_MODEL_MODULE",
        "PRODUCTION_READ_RETIRED_PREFIX",
        "LEGACY_TASK_METADATA_V1",
        "NOT_IN_RULES",
    }
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _iter_live_text_files() -> tuple[Path, ...]:
    paths: set[Path] = set()
    for relative_root in LIVE_ROOTS:
        root = ROOT / relative_root
        if root.is_file():
            paths.add(root)
        elif root.is_dir():
            paths.update(
                path
                for path in root.rglob("*")
                if path.is_file()
                and path.suffix in TEXT_SUFFIXES
                and not any(
                    part in {".git", ".venv", "__pycache__"} for part in path.parts
                )
            )
    return tuple(sorted(paths))


def _repo_relative_files() -> frozenset[str]:
    return frozenset(
        _relative(path)
        for path in ROOT.rglob("*")
        if path.is_file()
        and not any(part in {".git", ".venv", "__pycache__"} for part in path.parts)
    )


def _opaque_membership_findings(paths: frozenset[str]) -> tuple[str, ...]:
    findings: list[str] = []
    for root, expected in OPAQUE_ROOT_MEMBERS.items():
        actual = frozenset(
            path.removeprefix(root) for path in paths if path.startswith(root)
        )
        if actual != expected:
            findings.append(root)
    return tuple(findings)


def _assignment_name(node: ast.AST) -> str | None:
    if (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    ):
        return node.targets[0].id
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    return None


def _assignment_value(node: ast.AST) -> ast.expr | None:
    if isinstance(node, ast.Assign):
        return node.value
    if isinstance(node, ast.AnnAssign):
        return node.value
    return None


def _ancestor_chain(
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
) -> tuple[ast.AST, ...]:
    ancestors: list[ast.AST] = []
    while node in parents:
        node = parents[node]
        ancestors.append(node)
    return tuple(ancestors)


def _literal_string_set(node: ast.AST) -> frozenset[str] | None:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "frozenset"
        and len(node.args) == 1
    ):
        return _literal_string_set(node.args[0])
    if isinstance(node, (ast.Set, ast.Tuple, ast.List)):
        values = [
            item.value
            for item in node.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        ]
        if len(values) == len(node.elts):
            return frozenset(values)
    return None


def _retired_schema_helper_is_exact(
    tree: ast.Module,
    parents: dict[ast.AST, ast.AST],
    retired_check: ast.FunctionDef,
    schema_assertion: ast.FunctionDef,
) -> bool:
    sentinel_loads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id in {"_RETIRED_TABLES", "_RETIRED_COLUMNS"}
    ]
    if {node.id for node in sentinel_loads} != {
        "_RETIRED_TABLES",
        "_RETIRED_COLUMNS",
    } or any(
        retired_check not in _ancestor_chain(node, parents) for node in sentinel_loads
    ):
        return False

    retired_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_retired_schema_references"
    ]
    retired_loads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id == "_retired_schema_references"
    ]
    if len(retired_calls) != 1 or len(retired_loads) != 1:
        return False
    retired_call = retired_calls[0]
    list_call = parents.get(retired_call)
    assignment = parents.get(list_call) if list_call is not None else None
    return bool(
        retired_loads[0] is retired_call.func
        and isinstance(list_call, ast.Call)
        and isinstance(list_call.func, ast.Name)
        and list_call.func.id == "list"
        and list_call.args == [retired_call]
        and not list_call.keywords
        and tuple(ast.unparse(argument) for argument in retired_call.args)
        == ("target_engine",)
        and not retired_call.keywords
        and isinstance(assignment, ast.Assign)
        and assignment in schema_assertion.body
        and len(assignment.targets) == 1
        and isinstance(assignment.targets[0], ast.Name)
        and assignment.targets[0].id == "incompatible"
        and assignment.value is list_call
    )


def _schema_preflight_is_exact(
    tree: ast.Module,
    parents: dict[ast.AST, ast.AST],
    ensure: ast.FunctionDef,
) -> bool:
    assertion_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_assert_current_business_schema"
    ]
    assertion_loads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id == "_assert_current_business_schema"
    ]
    create_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and ast.unparse(node.func) == "SQLModel.metadata.create_all"
    ]
    if len(assertion_calls) != 1 or len(assertion_loads) != 1 or len(create_calls) != 1:
        return False
    assertion_call = assertion_calls[0]
    create_call = create_calls[0]
    assertion_statement = parents.get(assertion_call)
    create_statement = parents.get(create_call)
    return bool(
        assertion_loads[0] is assertion_call.func
        and isinstance(assertion_statement, ast.Expr)
        and assertion_statement in ensure.body
        and tuple(ast.unparse(argument) for argument in assertion_call.args)
        == ("target_engine",)
        and not assertion_call.keywords
        and isinstance(create_statement, ast.Expr)
        and create_statement in ensure.body
        and tuple(ast.unparse(argument) for argument in create_call.args)
        == ("target_engine",)
        and not create_call.keywords
        and ensure.body.index(assertion_statement) < ensure.body.index(create_statement)
    )


def _models_db_sentinel_nodes(tree: ast.Module) -> tuple[ast.Constant, ...]:
    assignments = {
        _assignment_name(node): _assignment_value(node)
        for node in tree.body
        if _assignment_name(node) is not None
    }
    table_node = assignments.get("_RETIRED_TABLES")
    column_node = assignments.get("_RETIRED_COLUMNS")
    if (
        table_node is None
        or _literal_string_set(table_node) != MODEL_DB_RETIRED_TABLES
        or not isinstance(column_node, ast.Dict)
    ):
        return ()

    backlog_values: ast.AST | None = None
    for key, value in zip(column_node.keys, column_node.values, strict=True):
        if isinstance(key, ast.Constant) and key.value == "backlog_artifacts":
            backlog_values = value
            break
    if (
        backlog_values is None
        or _literal_string_set(backlog_values) != MODEL_DB_RETIRED_COLUMNS
    ):
        return ()

    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    retired_check = functions.get("_retired_schema_references")
    schema_assertion = functions.get("_assert_current_business_schema")
    ensure = functions.get("ensure_business_db_ready")
    if retired_check is None or schema_assertion is None or ensure is None:
        return ()
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    if not _retired_schema_helper_is_exact(
        tree,
        parents,
        retired_check,
        schema_assertion,
    ) or not _schema_preflight_is_exact(tree, parents, ensure):
        return ()

    approved = MODEL_DB_RETIRED_TABLES | MODEL_DB_RETIRED_COLUMNS
    return tuple(
        node
        for root in (table_node, backlog_values)
        for node in ast.walk(root)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in approved
    )


def _exact_not_in_nodes(
    relative_path: str,
    tree: ast.Module,
    parents: dict[ast.AST, ast.AST],
) -> tuple[ast.Constant, ...]:
    rules = NOT_IN_RULES.get(relative_path, {})
    allowed: list[ast.Constant] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.NotIn)
            and len(node.comparators) == 1
            and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, str)
            and isinstance(parent := parents.get(node), ast.Assert)
            and parent.test is node
        ):
            continue
        targets = rules.get(node.left.value, frozenset())
        if ast.unparse(node.comparators[0]) in targets:
            allowed.append(node.left)
    return tuple(allowed)


def _is_module_scope_node(
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    return not any(
        isinstance(
            ancestor,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda),
        )
        for ancestor in _ancestor_chain(node, parents)
    )


def _node_binds_name(node: ast.AST, name: str) -> bool:
    named_binding = (
        isinstance(
            node,
            (
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.ExceptHandler,
                ast.MatchAs,
                ast.MatchStar,
                ast.TypeVar,
                ast.ParamSpec,
                ast.TypeVarTuple,
            ),
        )
        and node.name == name
    )
    return any(
        (
            named_binding,
            isinstance(node, ast.Name)
            and node.id == name
            and isinstance(node.ctx, (ast.Store, ast.Del)),
            isinstance(node, ast.arg) and node.arg == name,
            isinstance(node, ast.MatchMapping) and node.rest == name,
            isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names,
            isinstance(node, ast.alias)
            and (node.name in (name, "*") or node.asname == name),
        )
    )


def _is_current_module_object(node: ast.expr) -> bool:
    return ast.unparse(node) == "sys.modules[__name__]"


def _is_current_module_mapping(
    node: ast.expr,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    rendered = ast.unparse(node)
    if rendered in {"globals()", "sys.modules[__name__].__dict__"}:
        return True
    if rendered == "vars(sys.modules[__name__])":
        return True
    return rendered in {"locals()", "vars()"} and _is_module_scope_node(
        node,
        parents,
    )


def _is_literal_name(node: ast.expr, name: str) -> bool:
    return isinstance(node, ast.Constant) and node.value == name


def _literal_dict_writes_name(dictionary: ast.AST, name: str) -> bool:
    return bool(
        isinstance(dictionary, ast.Dict)
        and any(
            key is not None and _is_literal_name(key, name) for key in dictionary.keys
        )
    )


def _mapping_call_mutates_name(
    call: ast.Call,
    name: str,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    if not (
        isinstance(call.func, ast.Attribute)
        and _is_current_module_mapping(call.func.value, parents)
    ):
        return False
    if call.func.attr in {"__delitem__", "__setitem__", "pop", "setdefault"}:
        return bool(call.args and _is_literal_name(call.args[0], name))
    if call.func.attr != "update":
        return False
    positional_write = any(
        _literal_dict_writes_name(argument, name) for argument in call.args
    )
    keyword_write = any(keyword.arg == name for keyword in call.keywords)
    unpacked_literal_write = any(
        keyword.arg is None and _literal_dict_writes_name(keyword.value, name)
        for keyword in call.keywords
    )
    return positional_write or keyword_write or unpacked_literal_write


def _node_mutates_module_name(
    node: ast.AST,
    name: str,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.ctx, (ast.Store, ast.Del))
        and _is_current_module_mapping(node.value, parents)
        and _is_literal_name(node.slice, name)
    ):
        return True
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.ctx, (ast.Store, ast.Del))
        and node.attr == name
        and _is_current_module_object(node.value)
    ):
        return True
    if not isinstance(node, ast.Call):
        return False
    if (
        isinstance(node.func, ast.Name)
        and node.func.id in {"delattr", "setattr"}
        and len(node.args) >= MIN_NAMESPACE_CALL_ARGS
        and _is_current_module_object(node.args[0])
        and _is_literal_name(node.args[1], name)
    ):
        return True
    return _mapping_call_mutates_name(node, name, parents)


def _node_binds_module_name(
    node: ast.AST,
    name: str,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    if _node_mutates_module_name(node, name, parents):
        return True
    if isinstance(node, (ast.Global, ast.Nonlocal)) and _node_binds_name(node, name):
        return True
    return _is_module_scope_node(node, parents) and _node_binds_name(node, name)


def _fresh_schema_guard_binding_is_exact(tree: ast.Module) -> bool:
    guard_name = "_assert_current_business_schema"
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    guard_imports = [
        (node, alias)
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "models.db"
        for alias in node.names
        if alias.name == guard_name
    ]
    if len(guard_imports) != 1 or guard_imports[0][1].asname is not None:
        return False
    approved_guard_alias = guard_imports[0][1]
    return not any(
        node is not approved_guard_alias
        and _node_binds_module_name(node, guard_name, parents)
        for node in ast.walk(tree)
    )


def _is_exact_schema_rejection(statement: ast.stmt) -> bool:
    return bool(
        isinstance(statement, ast.With)
        and len(statement.items) == 1
        and ast.unparse(statement.items[0].context_expr)
        == "pytest.raises(RuntimeError, match='UNSUPPORTED_BUSINESS_SCHEMA')"
        and statement.items[0].optional_vars is None
        and len(statement.body) == 1
        and ast.unparse(statement.body[0]) == "_assert_current_business_schema(mixed)"
    )


def _fresh_schema_proof_is_exact(
    function: ast.FunctionDef,
    parameter_name: str | tuple[str, ...],
) -> bool:
    expected_parameters = (
        (parameter_name,) if isinstance(parameter_name, str) else parameter_name
    )
    if len(function.decorator_list) != 1:
        return False
    if tuple(argument.arg for argument in function.args.args) != expected_parameters:
        return False
    if not (
        function.body
        and isinstance(function.body[0], ast.Expr)
        and isinstance(function.body[0].value, ast.Constant)
        and isinstance(function.body[0].value.value, str)
    ):
        return False
    proof_body = function.body[1:]
    if len(proof_body) != FRESH_SCHEMA_PROOF_STATEMENTS:
        return False
    contamination_source = {
        "test_issue_210_retired_authority_tables_are_rejected": (
            "_inject_retired_table(mixed, retired_table)"
        ),
        "test_issue_210_retired_columns_are_rejected": (
            "_inject_retired_column(mixed, table_name=table_name, "
            "column_name=column_name)"
        ),
    }[function.name]
    return (
        ast.unparse(proof_body[0]) == "mixed = _complete_current_schema()"
        and ast.unparse(proof_body[1]) == contamination_source
        and _is_exact_schema_rejection(proof_body[2])
    )


def _parameter_name_is_exact(
    parameter: ast.expr,
    expected: str | tuple[str, ...],
) -> bool:
    if isinstance(expected, str):
        return isinstance(parameter, ast.Constant) and parameter.value == expected
    constants = (
        tuple(item for item in parameter.elts if isinstance(item, ast.Constant))
        if isinstance(parameter, ast.Tuple)
        else ()
    )
    return bool(
        isinstance(parameter, ast.Tuple)
        and len(constants) == len(parameter.elts) == len(expected)
        and tuple(item.value for item in constants) == expected
    )


def _direct_scalar_parameter_nodes(
    elements: list[ast.expr],
    expected: frozenset[str],
) -> tuple[ast.Constant, ...]:
    nodes = tuple(
        element
        for element in elements
        if isinstance(element, ast.Constant) and isinstance(element.value, str)
    )
    values = tuple(node.value for node in nodes)
    if len(nodes) == len(elements) == len(expected) and frozenset(values) == expected:
        return nodes
    return ()


def _direct_tuple_parameter_nodes(
    elements: list[ast.expr],
    parameter_count: int,
    expected: frozenset[str],
) -> tuple[ast.Constant, ...]:
    tuple_nodes = tuple(
        element for element in elements if isinstance(element, ast.Tuple)
    )
    row_nodes = tuple(
        tuple(item for item in element.elts if isinstance(item, ast.Constant))
        for element in tuple_nodes
    )
    if len(tuple_nodes) != len(elements) or any(
        len(nodes) != parameter_count
        or any(not isinstance(item.value, str) for item in nodes)
        for nodes in row_nodes
    ):
        return ()
    rows = [tuple(item.value for item in nodes) for nodes in row_nodes]
    required_rows = {("backlog_artifacts", value) for value in expected}
    if any(rows.count(required) != 1 for required in required_rows):
        return ()
    retired_nodes = tuple(
        item for nodes in row_nodes for item in nodes if item.value in expected
    )
    return retired_nodes if len(retired_nodes) == len(expected) else ()


def _direct_parameter_nodes(
    decorator: ast.expr,
    parameter_name: str | tuple[str, ...],
    expected: frozenset[str],
) -> tuple[ast.Constant, ...]:
    if not (
        isinstance(decorator, ast.Call)
        and ast.unparse(decorator.func) == "pytest.mark.parametrize"
        and len(decorator.args) == MIN_PARAMETRIZE_ARGS
        and not decorator.keywords
        and _parameter_name_is_exact(decorator.args[0], parameter_name)
        and isinstance(decorator.args[1], ast.List)
    ):
        return ()
    elements = decorator.args[1].elts
    if isinstance(parameter_name, str):
        return _direct_scalar_parameter_nodes(elements, expected)
    return _direct_tuple_parameter_nodes(elements, len(parameter_name), expected)


def _pytest_import_bindings(
    module_nodes: tuple[ast.AST, ...],
) -> tuple[set[str], set[str]]:
    pytest_modules = {"pytest"}
    disablers: set[str] = set()
    for node in module_nodes:
        if isinstance(node, ast.Import):
            pytest_modules.update(
                alias.asname or "pytest"
                for alias in node.names
                if alias.name == "pytest"
            )
        if isinstance(node, ast.ImportFrom) and node.module == "pytest":
            for alias in node.names:
                if alias.name in PYTEST_MODULE_DISABLERS:
                    disablers.add(alias.asname or alias.name)
                elif alias.name == "*":
                    disablers.update(PYTEST_MODULE_DISABLERS)
    return pytest_modules, disablers


def _assigned_disabler_alias(
    node: ast.AST,
    pytest_modules: set[str],
    disablers: set[str],
) -> str | None:
    if not isinstance(node, (ast.Assign, ast.AnnAssign)):
        return None
    bound_name = _assignment_name(node)
    value = _assignment_value(node)
    if bound_name is None or value is None:
        return None
    if isinstance(value, ast.Name) and value.id in disablers:
        return bound_name
    if (
        isinstance(value, ast.Attribute)
        and value.attr in PYTEST_MODULE_DISABLERS
        and isinstance(value.value, ast.Name)
        and value.value.id in pytest_modules
    ):
        return bound_name
    return None


def _pytest_disabler_bindings(
    tree: ast.Module,
    parents: dict[ast.AST, ast.AST],
) -> tuple[frozenset[str], frozenset[str]]:
    module_nodes = tuple(
        node for node in ast.walk(tree) if _is_module_scope_node(node, parents)
    )
    pytest_modules, disablers = _pytest_import_bindings(module_nodes)

    changed = True
    while changed:
        changed = False
        for node in module_nodes:
            bound_name = _assigned_disabler_alias(
                node,
                pytest_modules,
                disablers,
            )
            if bound_name is not None and bound_name not in disablers:
                disablers.add(bound_name)
                changed = True
    return frozenset(pytest_modules), frozenset(disablers)


def _call_is_module_pytest_disabler(
    call: ast.Call,
    pytest_modules: frozenset[str],
    disablers: frozenset[str],
) -> bool:
    return bool(
        (isinstance(call.func, ast.Name) and call.func.id in disablers)
        or (
            isinstance(call.func, ast.Attribute)
            and call.func.attr in PYTEST_MODULE_DISABLERS
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id in pytest_modules
        )
    )


def _fresh_schema_module_is_enabled(tree: ast.Module) -> bool:
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    pytest_modules, disablers = _pytest_disabler_bindings(tree, parents)
    for node in ast.walk(tree):
        if _node_binds_module_name(node, "pytestmark", parents) or (
            _node_binds_module_name(node, "__test__", parents)
        ):
            return False
        if (
            isinstance(node, ast.Call)
            and _is_module_scope_node(node, parents)
            and _call_is_module_pytest_disabler(node, pytest_modules, disablers)
        ):
            return False
    return True


def _resolved_construct_name(
    node: ast.expr,
    module_aliases: set[str] | frozenset[str],
    bindings: dict[str, str],
    constructs: frozenset[str],
) -> str | None:
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if (
        isinstance(node, ast.Attribute)
        and node.attr in constructs
        and isinstance(node.value, ast.Name)
        and node.value.id in module_aliases
    ):
        return node.attr
    return None


def _initial_finite_bindings(
    module_nodes: tuple[ast.AST, ...],
    module_names: frozenset[str],
    constructs: frozenset[str],
) -> tuple[set[str], dict[str, str]]:
    module_aliases = set(module_names)
    bindings = {name: name for name in constructs}
    for node in module_nodes:
        if isinstance(node, ast.Import):
            module_aliases.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name in module_names
            )
        if isinstance(node, ast.ImportFrom) and node.module in module_names:
            for alias in node.names:
                if alias.name in constructs:
                    bindings[alias.asname or alias.name] = alias.name
    return module_aliases, bindings


def _extend_finite_bindings(
    module_nodes: tuple[ast.AST, ...],
    module_aliases: set[str],
    bindings: dict[str, str],
    constructs: frozenset[str],
) -> None:
    changed = True
    while changed:
        changed = False
        for node in module_nodes:
            bound_name = _assignment_name(node)
            value = _assignment_value(node)
            if bound_name is None or value is None:
                continue
            if isinstance(value, ast.Name) and value.id in module_aliases:
                if bound_name not in module_aliases:
                    module_aliases.add(bound_name)
                    changed = True
                continue
            resolved = _resolved_construct_name(
                value,
                module_aliases,
                bindings,
                constructs,
            )
            if resolved is not None and bindings.get(bound_name) != resolved:
                bindings[bound_name] = resolved
                changed = True


def _finite_module_binding_table(
    tree: ast.Module,
    parents: dict[ast.AST, ast.AST],
    *,
    module_names: frozenset[str],
    constructs: frozenset[str],
) -> tuple[frozenset[str], dict[str, str]]:
    module_nodes = tuple(
        node for node in ast.walk(tree) if _is_module_scope_node(node, parents)
    )
    module_aliases, bindings = _initial_finite_bindings(
        module_nodes,
        module_names,
        constructs,
    )
    _extend_finite_bindings(
        module_nodes,
        module_aliases,
        bindings,
        constructs,
    )
    return frozenset(module_aliases), bindings


def _builtins_binding_table(
    tree: ast.Module,
    parents: dict[ast.AST, ast.AST],
) -> tuple[frozenset[str], dict[str, str]]:
    return _finite_module_binding_table(
        tree,
        parents,
        module_names=frozenset({"builtins"}),
        constructs=DYNAMIC_BUILTINS,
    )


def _compile_mode(call: ast.Call) -> str | None:
    mode: ast.expr | None = (
        call.args[COMPILE_MODE_ARG_INDEX]
        if len(call.args) > COMPILE_MODE_ARG_INDEX
        else None
    )
    if mode is None:
        mode = next(
            (keyword.value for keyword in call.keywords if keyword.arg == "mode"),
            None,
        )
    return (
        mode.value
        if isinstance(mode, ast.Constant) and isinstance(mode.value, str)
        else None
    )


def _literal_dynamic_code_is_absent(tree: ast.Module) -> bool:
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    module_aliases, bindings = _builtins_binding_table(tree, parents)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        construct = _resolved_construct_name(
            node.func,
            module_aliases,
            bindings,
            DYNAMIC_BUILTINS,
        )
        if construct in {"eval", "exec"}:
            return False
        if construct == "compile" and _compile_mode(node) in {"eval", "exec"}:
            return False
    return True


def _expression_is_proof_reference(
    node: ast.expr,
    aliases: frozenset[str] | set[str],
    proof_name: str,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    if isinstance(node, ast.Name) and node.id in aliases:
        return True
    if (
        isinstance(node, ast.Subscript)
        and _is_current_module_mapping(node.value, parents)
        and _is_literal_name(node.slice, proof_name)
    ):
        return True
    return bool(
        isinstance(node, ast.Attribute)
        and node.attr == proof_name
        and _is_current_module_object(node.value)
    )


def _proof_binding_aliases(
    tree: ast.Module,
    proof_name: str,
    parents: dict[ast.AST, ast.AST],
) -> frozenset[str]:
    module_nodes = tuple(
        node for node in ast.walk(tree) if _is_module_scope_node(node, parents)
    )
    aliases = {proof_name}
    changed = True
    while changed:
        changed = False
        for node in module_nodes:
            bound_name = _assignment_name(node)
            value = _assignment_value(node)
            if (
                bound_name is not None
                and value is not None
                and _expression_is_proof_reference(
                    value,
                    aliases,
                    proof_name,
                    parents,
                )
                and bound_name not in aliases
            ):
                aliases.add(bound_name)
                changed = True
    return frozenset(aliases)


def _node_suppresses_proof(
    node: ast.AST,
    aliases: frozenset[str],
    proof_name: str,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    suppressing_attributes = {"__code__", "__test__", "pytestmark"}

    def literal_dictionary_writes_suppressing_attribute(
        dictionary: ast.AST,
    ) -> bool:
        return any(
            _literal_dict_writes_name(dictionary, attribute)
            for attribute in suppressing_attributes
        )

    def is_proof_dictionary(expression: ast.AST) -> bool:
        return bool(
            isinstance(expression, ast.Attribute)
            and expression.attr == "__dict__"
            and _expression_is_proof_reference(
                expression.value,
                aliases,
                proof_name,
                parents,
            )
        )

    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.ctx, (ast.Store, ast.Del))
        and node.attr in suppressing_attributes
        and _expression_is_proof_reference(
            node.value,
            aliases,
            proof_name,
            parents,
        )
    ) or (
        isinstance(node, ast.Subscript)
        and isinstance(node.ctx, (ast.Store, ast.Del))
        and isinstance(node.slice, ast.Constant)
        and node.slice.value in suppressing_attributes
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "__dict__"
        and _expression_is_proof_reference(
            node.value.value,
            aliases,
            proof_name,
            parents,
        )
    ):
        return True
    if (
        isinstance(node, ast.AugAssign)
        and isinstance(node.op, ast.BitOr)
        and is_proof_dictionary(node.target)
        and literal_dictionary_writes_suppressing_attribute(node.value)
    ):
        return True
    if not isinstance(node, ast.Call):
        return False
    if (
        isinstance(node.func, ast.Name)
        and node.func.id in {"delattr", "setattr"}
        and len(node.args) >= MIN_NAMESPACE_CALL_ARGS
        and _expression_is_proof_reference(
            node.args[0],
            aliases,
            proof_name,
            parents,
        )
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value in suppressing_attributes
    ):
        return True
    if (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "update"
        and is_proof_dictionary(node.func.value)
        and (
            any(
                literal_dictionary_writes_suppressing_attribute(argument)
                for argument in node.args
            )
            or any(
                keyword.arg in suppressing_attributes
                for keyword in node.keywords
                if keyword.arg is not None
            )
            or any(
                keyword.arg is None
                and literal_dictionary_writes_suppressing_attribute(keyword.value)
                for keyword in node.keywords
            )
        )
    ):
        return True
    return bool(
        isinstance(node.func, ast.Attribute)
        and is_proof_dictionary(node.func.value)
        and node.func.attr in {"__delitem__", "__setitem__", "pop", "setdefault"}
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value in suppressing_attributes
    )


def _fresh_schema_proof_bindings_are_exact(tree: ast.Module) -> bool:
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    for proof_name in FRESH_SCHEMA_PROOF_NAMES:
        definitions = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == proof_name
        ]
        if len(definitions) != 1:
            return False
        approved_definition = definitions[0]
        proof_aliases = _proof_binding_aliases(tree, proof_name, parents)
        for node in ast.walk(tree):
            if node is approved_definition:
                continue
            if _node_binds_module_name(node, proof_name, parents) or (
                _node_suppresses_proof(
                    node,
                    proof_aliases,
                    proof_name,
                    parents,
                )
            ):
                return False
    return True


def _fresh_schema_rules() -> dict[str, tuple[str | tuple[str, ...], frozenset[str]]]:
    return {
        FRESH_SCHEMA_PROOF_NAMES[0]: (
            "retired_table",
            MODEL_DB_RETIRED_TABLES - {"discovery_artifacts"},
        ),
        FRESH_SCHEMA_PROOF_NAMES[1]: (
            ("table_name", "column_name"),
            MODEL_DB_RETIRED_COLUMNS,
        ),
    }


def _fresh_schema_proof_contract_is_exact(tree: ast.Module) -> bool:
    if not (
        _fresh_schema_guard_binding_is_exact(tree)
        and _fresh_schema_module_is_enabled(tree)
        and _literal_dynamic_code_is_absent(tree)
        and _fresh_schema_proof_bindings_are_exact(tree)
    ):
        return False
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    for function_name, (parameter_name, expected) in _fresh_schema_rules().items():
        function = functions[function_name]
        if not _fresh_schema_proof_is_exact(function, parameter_name):
            return False
        parameter_nodes = _direct_parameter_nodes(
            function.decorator_list[0],
            parameter_name,
            expected,
        )
        if len(parameter_nodes) != len(expected):
            return False
    return True


def _fresh_schema_rejection_nodes(tree: ast.Module) -> tuple[ast.Constant, ...]:
    if not _fresh_schema_proof_contract_is_exact(tree):
        return ()
    approved: list[ast.Constant] = []
    rules = _fresh_schema_rules()
    for function in (node for node in tree.body if isinstance(node, ast.FunctionDef)):
        if function.name not in rules:
            continue
        parameter_name, expected = rules[function.name]
        if not _fresh_schema_proof_is_exact(function, parameter_name):
            continue
        approved.extend(
            _direct_parameter_nodes(
                function.decorator_list[0],
                parameter_name,
                expected,
            )
        )
    return tuple(approved)


def _product_definition_negative_nodes(
    tree: ast.Module,
    parents: dict[ast.AST, ast.AST],
) -> tuple[ast.expr, ...]:
    allowed: list[ast.expr] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.Is)
            and len(node.comparators) == 1
            and isinstance(node.comparators[0], ast.Constant)
            and node.comparators[0].value is None
            and isinstance(parents.get(node), ast.Assert)
            and isinstance(node.left, ast.Call)
            and ast.unparse(node.left.func) == "importlib.util.find_spec"
            and len(node.left.args) == 1
            and isinstance(node.left.args[0], ast.Constant)
            and node.left.args[0].value == REMOVED_MODEL_MODULE
        ):
            allowed.append(node.left.args[0])
        if (
            isinstance(node, ast.Assert)
            and isinstance(node.test, ast.UnaryOp)
            and isinstance(node.test.op, ast.Not)
            and isinstance(node.test.operand, ast.Call)
            and isinstance(node.test.operand.func, ast.Name)
            and node.test.operand.func.id == "hasattr"
            and len(node.test.operand.args) == EXPECTED_HASATTR_ARGUMENTS
            and ast.unparse(node.test.operand.args[0]) == "specs"
            and isinstance(node.test.operand.args[1], ast.Constant)
            and node.test.operand.args[1].value in MODEL_CLASS_NAMES
        ):
            allowed.append(node.test.operand.args[1])
        if (
            isinstance(node, ast.Assert)
            and isinstance(node.test, ast.Call)
            and isinstance(node.test.func, ast.Attribute)
            and node.test.func.attr == "isdisjoint"
            and isinstance(node.test.func.value, ast.Set)
            and len(node.test.args) == 1
        ):
            values = frozenset(
                item.value
                for item in node.test.func.value.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            )
            comparator = ast.unparse(node.test.args[0])
            if (
                values == MODEL_DB_RETIRED_COLUMNS
                and comparator == "backlog_columns.keys()"
            ) or (
                values == MODEL_DB_RETIRED_TABLES - {"discovery_artifacts"}
                and comparator == "SQLModel.metadata.tables"
            ):
                allowed.extend(node.test.func.value.elts)
    return tuple(allowed)


def _legacy_metadata_rejection_nodes(
    tree: ast.Module,
    parents: dict[ast.AST, ast.AST],
) -> tuple[ast.Constant, ...]:
    allowed: list[ast.Constant] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Constant) and node.value == LEGACY_TASK_METADATA_V1
        ):
            continue
        function = next(
            (
                ancestor
                for ancestor in _ancestor_chain(node, parents)
                if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef))
            ),
            None,
        )
        if (
            function is not None
            and function.name == "test_task_metadata_load_requires_valid_canonical_json"
            and any(
                isinstance(item, ast.With)
                and any(
                    isinstance(context.context_expr, ast.Call)
                    and ast.unparse(context.context_expr.func) == "pytest.raises"
                    for context in item.items
                )
                for item in ast.walk(function)
            )
        ):
            allowed.append(node)
    return tuple(allowed)


def _production_read_absence_nodes(
    tree: ast.Module,
    parents: dict[ast.AST, ast.AST],
) -> tuple[ast.Constant, ...]:
    return tuple(
        node.left.left
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.NotIn)
            and len(node.comparators) == 1
            and ast.unparse(node.comparators[0]) == "source"
            and isinstance(parent := parents.get(node), ast.Assert)
            and parent.test is node
            and isinstance(node.left, ast.BinOp)
            and isinstance(node.left.op, ast.Add)
            and isinstance(node.left.left, ast.Constant)
            and node.left.left.value == PRODUCTION_READ_RETIRED_PREFIX
            and isinstance(node.left.right, ast.Constant)
            and node.left.right.value == "Service"
        )
    )


def _self_policy_nodes(tree: ast.Module) -> tuple[ast.expr, ...]:
    return tuple(
        value
        for node in tree.body
        if _assignment_name(node) in SCANNER_POLICY_ASSIGNMENTS
        if (value := _assignment_value(node)) is not None
    )


def _permitted_python_matches(
    relative_path: str,
    source: str,
) -> Counter[tuple[int, str]]:
    tree = ast.parse(source)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    allowed_nodes: list[ast.expr] = list(
        _exact_not_in_nodes(relative_path, tree, parents)
    )
    if relative_path == "models/db.py":
        allowed_nodes.extend(_models_db_sentinel_nodes(tree))
    if relative_path == "tests/workflow/test_fresh_project_schema.py":
        allowed_nodes.extend(_fresh_schema_rejection_nodes(tree))
    if relative_path == "tests/workflow/test_product_definition_models.py":
        allowed_nodes.extend(_product_definition_negative_nodes(tree, parents))
    if relative_path == "tests/workflow/test_planning_transitions.py":
        allowed_nodes.extend(_legacy_metadata_rejection_nodes(tree, parents))
    if relative_path == "tests/adapters/test_production_read_surfaces.py":
        allowed_nodes.extend(_production_read_absence_nodes(tree, parents))
    if relative_path == SCANNER_PATH:
        allowed_nodes.extend(_self_policy_nodes(tree))

    source_lines = source.splitlines()
    allowed: Counter[tuple[int, str]] = Counter()
    for node in dict.fromkeys(allowed_nodes):
        for line_number in range(node.lineno, (node.end_lineno or node.lineno) + 1):
            line = source_lines[line_number - 1]
            start = node.col_offset if line_number == node.lineno else 0
            end = (
                node.end_col_offset
                if line_number == (node.end_lineno or node.lineno)
                else len(line)
            )
            segment = line[start:end]
            for pattern in (PROHIBITED_TEXT, PROHIBITED_ALGEBRA):
                allowed.update(
                    (line_number, match.group(0)) for match in pattern.finditer(segment)
                )
    return allowed


def _is_framed_historical_document(relative_path: str, source: str) -> bool:
    if relative_path.startswith("artifacts/historical-schema/"):
        return True
    if relative_path.startswith(("docs/superpowers/plans/", "docs/superpowers/specs/")):
        filename = Path(relative_path).name
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}-.+\.md", filename))
    if relative_path in SUPERSEDED_ADRS:
        return (
            "**Status:** Superseded" in source
            and "0005-use-accepted-specification-as-delivery-contract.md" in source
        )
    return relative_path in HISTORICAL_FEEDBACK_FILES


def _is_python_identity(node: ast.AST, identity: str) -> bool:
    identity_fields = (
        (ast.Name, "id"),
        (ast.ClassDef, "name"),
        (ast.FunctionDef, "name"),
        (ast.AsyncFunctionDef, "name"),
        (ast.Attribute, "attr"),
        (ast.arg, "arg"),
        (ast.keyword, "arg"),
        (ast.ExceptHandler, "name"),
        (ast.MatchAs, "name"),
        (ast.MatchStar, "name"),
        (ast.TypeVar, "name"),
        (ast.ParamSpec, "name"),
        (ast.TypeVarTuple, "name"),
    )
    field_identity = False
    for node_type, field in identity_fields:
        if isinstance(node, node_type):
            field_identity = getattr(node, field) == identity
            break
    return any(
        (
            isinstance(node, ast.ImportFrom)
            and identity in (node.module or "").split("."),
            isinstance(node, ast.MatchClass) and identity in node.kwd_attrs,
            field_identity,
            isinstance(node, ast.alias)
            and (identity in node.name.split(".") or node.asname == identity),
            isinstance(node, (ast.Global, ast.Nonlocal)) and identity in node.names,
            isinstance(node, ast.MatchMapping) and node.rest == identity,
        )
    )


def _typing_binding_table(
    tree: ast.Module,
) -> tuple[frozenset[str], dict[str, str]]:
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    return _finite_module_binding_table(
        tree,
        parents,
        module_names=frozenset({"typing", "typing_extensions"}),
        constructs=TYPING_CONSTRUCTS,
    )


def _annotation_expressions(
    node: ast.AST,
    module_aliases: frozenset[str],
    bindings: dict[str, str],
) -> tuple[ast.expr, ...]:
    expressions: tuple[ast.expr | None, ...] = ()
    if isinstance(node, ast.arg):
        expressions = (node.annotation,)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        expressions = (node.returns,)
    elif isinstance(node, ast.AnnAssign):
        type_alias_value = (
            node.value
            if _resolved_construct_name(
                node.annotation,
                module_aliases,
                bindings,
                TYPING_CONSTRUCTS,
            )
            == "TypeAlias"
            else None
        )
        expressions = (node.annotation, type_alias_value)
    elif isinstance(node, ast.TypeVar):
        default_value = vars(node).get("default_value")
        expressions = (
            node.bound,
            default_value if isinstance(default_value, ast.expr) else None,
        )
    elif isinstance(node, (ast.ParamSpec, ast.TypeVarTuple)):
        default_value = vars(node).get("default_value")
        expressions = (default_value if isinstance(default_value, ast.expr) else None,)
    elif isinstance(node, ast.TypeAlias):
        expressions = (node.value,)
    return tuple(expression for expression in expressions if expression is not None)


def _annotation_string_uses_identity(value: str, identity: str) -> bool:
    try:
        expression = ast.parse(value, mode="eval")
    except SyntaxError:
        return False
    return any(_is_python_identity(node, identity) for node in ast.walk(expression))


def _quoted_annotation_identity_nodes(
    expression: ast.expr,
    identity: str,
    module_aliases: frozenset[str],
    bindings: dict[str, str],
) -> tuple[ast.Constant, ...]:
    if isinstance(expression, ast.Constant):
        return (
            (expression,)
            if isinstance(expression.value, str)
            and _annotation_string_uses_identity(expression.value, identity)
            else ()
        )
    if isinstance(expression, ast.Subscript):
        construct = _resolved_construct_name(
            expression.value,
            module_aliases,
            bindings,
            TYPING_CONSTRUCTS,
        )
        if construct == "Literal":
            return ()
        if construct == "Annotated":
            annotated_values = (
                expression.slice.elts
                if isinstance(expression.slice, ast.Tuple)
                else (expression.slice,)
            )
            return (
                _quoted_annotation_identity_nodes(
                    annotated_values[0],
                    identity,
                    module_aliases,
                    bindings,
                )
                if annotated_values
                else ()
            )
    return tuple(
        finding
        for child in ast.iter_child_nodes(expression)
        if isinstance(child, ast.expr)
        for finding in _quoted_annotation_identity_nodes(
            child,
            identity,
            module_aliases,
            bindings,
        )
    )


def _forward_ref_identity_nodes(
    tree: ast.Module,
    identity: str,
    module_aliases: frozenset[str],
    bindings: dict[str, str],
) -> tuple[ast.Constant, ...]:
    def forward_ref_argument(call: ast.Call) -> ast.expr | None:
        if call.args:
            return call.args[0]
        return next(
            (keyword.value for keyword in call.keywords if keyword.arg == "arg"),
            None,
        )

    identity_nodes: list[ast.Constant] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and _resolved_construct_name(
                node.func,
                module_aliases,
                bindings,
                TYPING_CONSTRUCTS,
            )
            == "ForwardRef"
        ):
            continue
        argument = forward_ref_argument(node)
        if (
            isinstance(argument, ast.Constant)
            and isinstance(argument.value, str)
            and _annotation_string_uses_identity(argument.value, identity)
        ):
            identity_nodes.append(argument)
    return tuple(identity_nodes)


def _python_identity_findings(source: str) -> tuple[tuple[int, str], ...]:
    """Reject the retired Python identity without policing human prose."""
    identity = "In" + "variant"
    tree = ast.parse(source)
    module_aliases, bindings = _typing_binding_table(tree)
    identity_nodes = {
        node for node in ast.walk(tree) if _is_python_identity(node, identity)
    }
    annotation_nodes = {
        child
        for node in ast.walk(tree)
        for expression in _annotation_expressions(node, module_aliases, bindings)
        for child in _quoted_annotation_identity_nodes(
            expression,
            identity,
            module_aliases,
            bindings,
        )
    }
    annotation_nodes.update(
        _forward_ref_identity_nodes(tree, identity, module_aliases, bindings)
    )
    findings = {
        (int(vars(node)["lineno"]), identity)
        for node in identity_nodes | annotation_nodes
    }
    return tuple(sorted(findings))


def _source_findings(
    relative_path: str,
    source: str,
) -> tuple[tuple[tuple[int, str], ...], Counter[tuple[int, str]]]:
    permitted = (
        _permitted_python_matches(relative_path, source)
        if relative_path.endswith(".py")
        else Counter()
    )
    historical = _is_framed_historical_document(relative_path, source)
    findings: list[tuple[int, str]] = []
    if relative_path == FRESH_SCHEMA_PATH and not (
        _fresh_schema_proof_contract_is_exact(ast.parse(source))
    ):
        findings.append((1, "fresh-schema proof contract"))
    if relative_path.endswith(".py") and not historical:
        findings.extend(_python_identity_findings(source))
    if (
        not historical
        and relative_path not in LEGACY_FIXTURE_REFERENCE_PATHS
        and LEGACY_FIXTURE_REFERENCE in source
    ):
        findings.append((1, LEGACY_FIXTURE_REFERENCE))
    for line_number, line in enumerate(source.splitlines(), start=1):
        for pattern in (PROHIBITED_TEXT, PROHIBITED_ALGEBRA):
            for match in pattern.finditer(line):
                key = (line_number, match.group(0))
                if historical:
                    continue
                if (
                    match.group(0) == FRAMED_LAYER_TOKEN
                    and line.strip() == FRAMED_LAYER_SENTENCE
                ):
                    continue
                if permitted[key]:
                    permitted[key] -= 1
                else:
                    findings.append(key)
    return tuple(findings), +permitted


@pytest.mark.parametrize("module_name", REMOVED_MODULES)
def test_removed_module_is_unimportable(module_name: str) -> None:
    """Require every retired import path to remain absent."""
    assert importlib.util.find_spec(module_name) is None


def test_retired_artifacts_are_absent_and_direct_spec_fixture_is_exact() -> None:
    """Require retired artifacts absent and relocated evidence byte-exact."""
    present = [
        relative_path
        for relative_path in REMOVED_PATHS
        if (ROOT / relative_path).exists()
    ]
    assert present == []

    destination = ROOT / (
        "tests/fixtures/specification_structuring/string-calculator-negative-diagnostic"
    )
    assert frozenset(
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    ) == frozenset(RELOCATED_FIXTURE_HASHES)
    assert {
        relative_path: _sha256(destination / relative_path)
        for relative_path in RELOCATED_FIXTURE_HASHES
    } == RELOCATED_FIXTURE_HASHES


def test_opaque_fixture_file_sets_and_hashes_are_exact() -> None:
    """Pin every opaque byte and reject any unreviewed file under an opaque root."""
    repo_files = _repo_relative_files()
    assert _opaque_membership_findings(repo_files) == ()
    assert {
        relative_path: _sha256(ROOT / relative_path) for relative_path in OPAQUE_HASHES
    } == OPAQUE_HASHES

    first_root = next(iter(OPAQUE_ROOT_MEMBERS))
    assert _opaque_membership_findings(repo_files | {f"{first_root}unreviewed.txt"})


def test_fresh_metadata_exposes_no_retired_identity() -> None:
    """Require fresh SQLModel metadata to expose no retired identity."""
    assert MODEL_DB_RETIRED_TABLES.isdisjoint(SQLModel.metadata.tables)
    for table in SQLModel.metadata.tables.values():
        assert {column.name for column in table.columns}.isdisjoint(
            MODEL_DB_RETIRED_COLUMNS
        )


def test_historical_records_are_framed_and_current_docs_are_direct_spec() -> None:
    """Require historical framing and direct-Spec current documentation."""
    superpowers_frame = (ROOT / "docs/superpowers/README.md").read_text(
        encoding="utf-8"
    )
    schema_frame = (ROOT / "artifacts/historical-schema/README.md").read_text(
        encoding="utf-8"
    )
    assert "historical" in superpowers_frame.casefold()
    assert "ADR 0005" in superpowers_frame
    assert "historical" in schema_frame.casefold()
    assert "not current" in schema_frame.casefold()
    assert "schema or runtime guidance" in schema_frame.casefold()

    for adr_name in SUPERSEDED_ADRS:
        content = (ROOT / adr_name).read_text(encoding="utf-8")
        assert _is_framed_historical_document(adr_name, content)

    for current_path in (
        "README.md",
        "CONTEXT.md",
        "docs/agent-cli-manual.md",
        "docs/testing/workflow-graph-acceptance-checklist.md",
        "scrum_agentic_system_lifecycle.mmd",
    ):
        content = (ROOT / current_path).read_text(encoding="utf-8")
        findings, unused = _source_findings(current_path, content)
        assert findings == ()
        assert not unused


def test_static_scanner_rejects_review_adversarial_mutations() -> None:
    """Close each production, polarity, receiver, metadata, and self-skip repro."""
    retired_class = "CompiledSpec" + "Authority"
    retired_algebra = "In" + "variant"
    command = "authority" + ".compile"
    retired_literal = "compiled_" + "authority"
    removed_module = "models." + "authority_" + "curation"
    retired_script = "authority_" + "quality_benchmark"
    positive_product_instruction = "Use the Authority " + "layer for compilation."

    cases = (
        ("models/db.py", f"{retired_class} = object"),
        ("services/probe.py", f"class {retired_algebra}: pass"),
        ("services/probe.py", f"{retired_algebra} = object"),
        (
            "services/probe.py",
            f"def classify(value):\n"
            f"    match value:\n"
            f"        case {retired_algebra}:\n"
            f"            return 1\n",
        ),
        (
            "services/probe.py",
            f"def transform[{retired_algebra}](value):\n    return value\n",
        ),
        (
            "services/probe.py",
            f'def transform(value: "{retired_algebra}"):\n    return value\n',
        ),
        (
            "services/probe.py",
            f'def transform(value: "{retired_algebra} | None"):\n    return value\n',
        ),
        (
            "services/probe.py",
            f"from package.{retired_algebra} import value\n",
        ),
        (
            "services/probe.py",
            f"import package.{retired_algebra}.submodule\n",
        ),
        (
            "services/probe.py",
            f"match value:\n"
            f"    case Box({retired_algebra}=captured):\n"
            f"        result = captured\n",
        ),
        ("CONTEXT.md", f"Run {command} now."),
        ("CONTEXT.md", positive_product_instruction),
        (
            "tests/test_roadmap_runtime.py",
            f'legacy_enabled = "{retired_literal}" not in disabled_features',
        ),
        (
            "tests/test_roadmap_runtime.py",
            f'assert "{retired_literal}" not in disabled_features',
        ),
        (
            "tests/workflow/test_product_definition_models.py",
            f'assert resolver.find_spec("{removed_module}") is None',
        ),
        ("pyproject.toml", f'legacy = "{retired_script}:main"'),
        ("scripts/check.sh", f"python scripts/{retired_script}.py"),
        (SCANNER_PATH, f"{retired_class} = object"),
    )
    for relative_path, source in cases:
        findings, _unused = _source_findings(relative_path, source)
        assert findings, relative_path

    ordinary_invariant_prose = (
        ("README.md", f"{retired_algebra}: retries preserve idempotency."),
        ("services/probe.py", f"# {retired_algebra}: keep retries idempotent."),
        (
            "services/probe.py",
            f'"""{retired_algebra}: keep retries idempotent."""',
        ),
        ("services/probe.py", f'label = "{retired_algebra}"'),
    )
    for relative_path, source in ordinary_invariant_prose:
        findings, unused = _source_findings(relative_path, source)
        assert findings == ()
        assert not unused

    quoted_type_identities = (
        f'from typing import TypeAlias\nAlias: TypeAlias = "{retired_algebra}"\n',
        f'from typing import ForwardRef\nAlias = ForwardRef("{retired_algebra}")\n',
    )
    for source in quoted_type_identities:
        findings, _unused = _source_findings("services/probe.py", source)
        assert findings

    quoted_literal_data = (
        f'from typing import Literal\nlabel: Literal["{retired_algebra}"] = '
        f'"{retired_algebra}"\n',
        f'from typing import Annotated\nlabel: Annotated[str, "{retired_algebra}"] '
        '= "ok"\n',
    )
    for source in quoted_literal_data:
        findings, unused = _source_findings("services/probe.py", source)
        assert findings == ()
        assert not unused

    allowed_negative = f'assert "{retired_literal}" not in dumped'
    findings, unused = _source_findings(
        "tests/test_roadmap_runtime.py",
        allowed_negative,
    )
    assert findings == ()
    assert not unused

    allowed_find_spec = f'assert importlib.util.find_spec("{removed_module}") is None'
    findings, unused = _source_findings(
        "tests/workflow/test_product_definition_models.py",
        allowed_find_spec,
    )
    assert findings == ()
    assert not unused

    prose, unused = _source_findings(
        "CONTEXT.md",
        "The former " + "Authority " + "layer is historical.",
    )
    assert prose == ()
    assert not unused


def test_models_db_only_allows_exact_fail_before_create_all_sentinels() -> None:
    """Reject positive production declarations while retaining hard-break sentinels."""
    source = (ROOT / "models/db.py").read_text(encoding="utf-8")
    findings, unused = _source_findings("models/db.py", source)
    assert findings == ()
    assert not unused

    retired_class = "CompiledSpec" + "Authority"
    mutated = source + f"\n{retired_class} = object\n"
    findings, _unused = _source_findings("models/db.py", mutated)
    assert findings

    compatibility_reader = (
        source
        + """
def read_retired_rows(target_engine):
    return {
        table_name: inspect(target_engine).get_columns(table_name)
        for table_name in _RETIRED_TABLES
    }
"""
    )
    findings, _unused = _source_findings("models/db.py", compatibility_reader)
    assert findings

    disconnected = source.replace(
        "    incompatible = list(_retired_schema_references(target_engine))",
        "    incompatible = []",
        1,
    )
    assert disconnected != source
    findings, _unused = _source_findings("models/db.py", disconnected)
    assert findings


def test_fresh_schema_allowance_requires_real_contamination_and_rejection() -> None:
    """Reject negative-proof lookalikes that never build and reject old schema."""
    retired_tables = sorted(MODEL_DB_RETIRED_TABLES - {"discovery_artifacts"})
    fake_table_proof = f"""
@pytest.mark.parametrize("retired_table", {retired_tables!r})
def test_issue_210_retired_authority_tables_are_rejected(retired_table):
    assert retired_table
    _assert_current_business_schema(_complete_current_schema())
"""
    findings, _unused = _source_findings(
        "tests/workflow/test_fresh_project_schema.py",
        fake_table_proof,
    )
    assert findings

    retired_columns = sorted(MODEL_DB_RETIRED_COLUMNS)
    fake_column_proof = f"""
@pytest.mark.parametrize(
    ("table_name", "column_name"),
    [("backlog_artifacts", column_name) for column_name in {retired_columns!r}],
)
def test_issue_210_retired_columns_are_rejected(table_name, column_name):
    assert table_name and column_name
    _assert_current_business_schema(_complete_current_schema())
"""
    findings, _unused = _source_findings(
        "tests/workflow/test_fresh_project_schema.py",
        fake_column_proof,
    )
    assert findings


def test_fresh_schema_allowance_rejects_a_locally_shadowed_schema_guard() -> None:
    """Require both negative proofs to call the imported production guard."""
    source = (ROOT / "tests/workflow/test_fresh_project_schema.py").read_text(
        encoding="utf-8"
    )
    mutations = (
        (
            "    mixed = _complete_current_schema()\n"
            "    _inject_retired_table(mixed, retired_table)",
            "    mixed = _complete_current_schema()\n\n"
            "    def _assert_current_business_schema(_target_engine):\n"
            '        raise RuntimeError("UNSUPPORTED_BUSINESS_SCHEMA")\n\n'
            "    _inject_retired_table(mixed, retired_table)",
        ),
        (
            "    mixed = _complete_current_schema()\n    _inject_retired_column(\n",
            "    mixed = _complete_current_schema()\n\n"
            "    def _assert_current_business_schema(_target_engine):\n"
            '        raise RuntimeError("UNSUPPORTED_BUSINESS_SCHEMA")\n\n'
            "    _inject_retired_column(\n",
        ),
    )
    for needle, replacement in mutations:
        mutated = source.replace(needle, replacement, 1)
        assert mutated != source
        findings, _unused = _source_findings(
            "tests/workflow/test_fresh_project_schema.py",
            mutated,
        )
        assert findings


def test_fresh_schema_allowance_rejects_a_second_guard_import() -> None:
    """Reject a later import that replaces the authenticated production guard."""
    source = (ROOT / "tests/workflow/test_fresh_project_schema.py").read_text(
        encoding="utf-8"
    )
    production_import = (
        "from models.db import _assert_current_business_schema, "
        "ensure_business_db_ready"
    )
    mutated = source.replace(
        production_import,
        production_import
        + "\nfrom fake_schema_guard import _assert_current_business_schema",
        1,
    )
    assert mutated != source

    findings, _unused = _source_findings(
        "tests/workflow/test_fresh_project_schema.py",
        mutated,
    )

    assert findings


def test_fresh_schema_allowance_rejects_module_namespace_guard_writes() -> None:
    """Reject direct writes that replace the imported guard in this module."""
    source = (ROOT / "tests/workflow/test_fresh_project_schema.py").read_text(
        encoding="utf-8"
    )
    production_import = (
        "from models.db import _assert_current_business_schema, "
        "ensure_business_db_ready"
    )
    rebindings = (
        'globals()["_assert_current_business_schema"] = fake_schema_guard',
        'vars()["_assert_current_business_schema"] = fake_schema_guard',
        "sys.modules[__name__]._assert_current_business_schema = fake_schema_guard",
        'setattr(sys.modules[__name__], "_assert_current_business_schema", '
        "fake_schema_guard)",
    )
    for rebinding in rebindings:
        mutated = source.replace(
            production_import,
            production_import + "\n" + rebinding,
            1,
        )
        assert mutated != source

        findings, _unused = _source_findings(
            "tests/workflow/test_fresh_project_schema.py",
            mutated,
        )

        assert findings


def test_fresh_schema_allowance_rejects_disabled_proofs() -> None:
    """Require both negative proofs to execute rather than skip or xfail."""
    source = (ROOT / "tests/workflow/test_fresh_project_schema.py").read_text(
        encoding="utf-8"
    )
    function_names = (
        "test_issue_210_retired_authority_tables_are_rejected",
        "test_issue_210_retired_columns_are_rejected",
    )
    for function_name in function_names:
        mutated = source.replace(
            f"def {function_name}(",
            f"@pytest.mark.skip\ndef {function_name}(",
            1,
        )
        assert mutated != source

        findings, _unused = _source_findings(
            "tests/workflow/test_fresh_project_schema.py",
            mutated,
        )

        assert findings


def test_fresh_schema_allowance_requires_direct_enabled_parameter_rows() -> None:
    """Reject module skips, dead parameter branches, and skipped required rows."""
    source = (ROOT / "tests/workflow/test_fresh_project_schema.py").read_text(
        encoding="utf-8"
    )
    production_import = (
        "from models.db import _assert_current_business_schema, "
        "ensure_business_db_ready"
    )
    module_skip = source.replace(
        production_import,
        production_import + "\npytestmark = pytest.mark.skip",
        1,
    )
    module_skip_call = source.replace(
        production_import,
        production_import + '\npytest.skip("disabled", allow_module_level=True)',
        1,
    )
    table_order = (
        "compiled_spec_" + "authority",
        "spec_" + "authority" + "_acceptance",
        "authority" + "_feedback_attempts",
        "authority" + "_curation_attempts",
    )
    table_values = (
        "[\n"
        + "\n".join(f'        "{retired_table}",' for retired_table in table_order)
        + "\n    ]"
    )
    dead_parameters = source.replace(
        table_values,
        "[] if True else " + table_values,
        1,
    )
    skipped_rows = source
    for retired_table in sorted(MODEL_DB_RETIRED_TABLES - {"discovery_artifacts"}):
        skipped_rows = skipped_rows.replace(
            f'        "{retired_table}",',
            f'        pytest.param("{retired_table}", marks=pytest.mark.skip),',
            1,
        )
    for retired_column in sorted(MODEL_DB_RETIRED_COLUMNS):
        skipped_rows = skipped_rows.replace(
            f'        ("backlog_artifacts", "{retired_column}"),',
            f'        pytest.param("backlog_artifacts", "{retired_column}", '
            "marks=pytest.mark.skip),",
            1,
        )

    for mutated in (module_skip, module_skip_call, dead_parameters, skipped_rows):
        assert mutated != source
        findings, _unused = _source_findings(
            "tests/workflow/test_fresh_project_schema.py",
            mutated,
        )
        assert findings


def test_fresh_schema_allowance_rejects_alias_disabling_and_proof_rebinding() -> None:
    """Authenticate the collected proof bindings, not only their source bodies."""
    source = (ROOT / "tests/workflow/test_fresh_project_schema.py").read_text(
        encoding="utf-8"
    )
    production_import = (
        "from models.db import _assert_current_business_schema, "
        "ensure_business_db_ready"
    )
    table_proof = "test_issue_210_retired_authority_tables_are_rejected"
    mutations = (
        source.replace(
            production_import,
            production_import
            + "\nfrom pytest import skip as disable_module\n"
            + 'disable_module("disabled", allow_module_level=True)',
            1,
        ),
        source.replace(
            production_import,
            production_import
            + "\nimport pytest as alternate_pytest\n"
            + 'alternate_pytest.importorskip("missing_dependency")',
            1,
        ),
        source.replace(
            production_import,
            production_import + '\nglobals()["pytestmark"] = pytest.mark.skip',
            1,
        ),
        source.replace(
            production_import,
            production_import + "\n__test__ = False",
            1,
        ),
        source + f"\n{table_proof} = None\n",
        source + f"\ndel {table_proof}\n",
        source + f"\n{table_proof}.__test__ = False\n",
        source
        + f"\ndef {table_proof}(retired_table):\n"
        + "    raise AssertionError(retired_table)\n",
    )
    for mutated in mutations:
        assert mutated != source
        findings, _unused = _source_findings(
            "tests/workflow/test_fresh_project_schema.py",
            mutated,
        )
        assert findings


def test_fresh_schema_allowance_rejects_literal_dynamic_guard_code() -> None:
    """Do not let direct dynamic code replace the authenticated guard binding."""
    source = (ROOT / "tests/workflow/test_fresh_project_schema.py").read_text(
        encoding="utf-8"
    )
    production_import = (
        "from models.db import _assert_current_business_schema, "
        "ensure_business_db_ready"
    )
    for function_name in ("exec", "eval"):
        mutated = source.replace(
            production_import,
            production_import
            + "\ndef fake_schema_guard(_target_engine):\n"
            + '    raise RuntimeError("UNSUPPORTED_BUSINESS_SCHEMA")\n'
            + f'{function_name}("_assert_current_business_schema = '
            + 'fake_schema_guard")',
            1,
        )
        assert mutated != source
        findings, _unused = _source_findings(
            "tests/workflow/test_fresh_project_schema.py",
            mutated,
        )
        assert findings


def test_fresh_schema_allowance_ignores_unrelated_runtime_uses() -> None:
    """Keep proof authentication narrow to module bindings and module disabling."""
    source = (ROOT / "tests/workflow/test_fresh_project_schema.py").read_text(
        encoding="utf-8"
    )
    production_import = (
        "from models.db import _assert_current_business_schema, "
        "ensure_business_db_ready"
    )
    benign_module_uses = (
        'GUARD_LABEL = "_assert_current_business_schema"',
        "helper(_assert_current_business_schema=object())",
        "box._assert_current_business_schema = object()",
    )
    for benign_use in benign_module_uses:
        mutated = source.replace(
            production_import,
            production_import + "\n" + benign_use,
            1,
        )
        findings, unused = _source_findings(
            "tests/workflow/test_fresh_project_schema.py",
            mutated,
        )
        assert findings == ()
        assert not unused

    nested_skip = (
        source + '\n\ndef test_optional_tool():\n    pytest.skip("optional")\n'
    )
    findings, unused = _source_findings(
        "tests/workflow/test_fresh_project_schema.py",
        nested_skip,
    )
    assert findings == ()
    assert not unused


def test_fresh_schema_allowance_resolves_module_namespace_mappings() -> None:
    """Treat module locals and vars(module) as globals without banning locals."""
    source = (ROOT / FRESH_SCHEMA_PATH).read_text(encoding="utf-8")
    production_import = (
        "from models.db import _assert_current_business_schema, "
        "ensure_business_db_ready"
    )
    proof_name = FRESH_SCHEMA_PROOF_NAMES[0]
    module_writes = (
        'locals()["pytestmark"] = pytest.mark.skip',
        'locals()["__test__"] = False',
        f'locals()["{proof_name}"] = None',
        'locals()["_assert_current_business_schema"] = fake_schema_guard',
        'vars(sys.modules[__name__])["_assert_current_business_schema"] = '
        "fake_schema_guard",
        'globals().update(**{"_assert_current_business_schema": fake_schema_guard})',
        'locals().update(**{"pytestmark": pytest.mark.skip})',
        f'locals().update(**{{"{proof_name}": None}})',
    )
    for module_write in module_writes:
        mutated = source.replace(
            production_import,
            production_import + "\n" + module_write,
            1,
        )
        findings, _unused = _source_findings(FRESH_SCHEMA_PATH, mutated)
        assert findings

    nested_locals = (
        source
        + "\n\ndef test_local_namespace():\n"
        + '    locals()["pytestmark"] = "local data"\n'
        + f'    vars()["{proof_name}"] = "local data"\n'
    )
    findings, unused = _source_findings(FRESH_SCHEMA_PATH, nested_locals)
    assert findings == ()
    assert not unused


def test_fresh_schema_allowance_resolves_dynamic_and_proof_aliases() -> None:
    """Reject only finite builtins and proof aliases that defeat real execution."""
    source = (ROOT / FRESH_SCHEMA_PATH).read_text(encoding="utf-8")
    production_import = (
        "from models.db import _assert_current_business_schema, "
        "ensure_business_db_ready"
    )
    fake_guard = (
        "\ndef fake_schema_guard(_target_engine):\n"
        '    raise RuntimeError("UNSUPPORTED_BUSINESS_SCHEMA")\n'
    )
    dynamic_code = '"_assert_current_business_schema = fake_schema_guard"'
    dynamic_mutations = (
        f"run_code = exec\nrun_code({dynamic_code})",
        f"run_code = exec\nrun_alias = run_code\nrun_alias({dynamic_code})",
        f"import builtins\nbuiltins.exec({dynamic_code})",
        f"import builtins as runtime\nruntime.eval({dynamic_code})",
        f"from builtins import exec as run_code\nrun_code({dynamic_code})",
        "code = compile(" + dynamic_code + ', "<schema-proof>", "exec")\nexec(code)',
    )
    for dynamic_mutation in dynamic_mutations:
        mutated = source.replace(
            production_import,
            production_import + fake_guard + dynamic_mutation,
            1,
        )
        findings, _unused = _source_findings(FRESH_SCHEMA_PATH, mutated)
        assert findings

    proof_name = FRESH_SCHEMA_PROOF_NAMES[0]
    proof_mutations = (
        f"proof_alias = {proof_name}\nproof_alias.__test__ = False",
        f"proof_alias = {proof_name}\nproof_chain = proof_alias\n"
        "proof_chain.__test__ = False",
        f'proof_alias = {proof_name}\nsetattr(proof_alias, "__test__", False)',
    )
    for proof_mutation in proof_mutations:
        mutated = source + "\n" + proof_mutation + "\n"
        findings, _unused = _source_findings(FRESH_SCHEMA_PATH, mutated)
        assert findings


@pytest.mark.parametrize(
    "proof_suppression",
    [
        'proof_alias.__dict__.update({"__test__": False})',
        "proof_alias.__dict__.update(__test__=False)",
        'proof_alias.__dict__ |= {"__test__": False}',
        'proof_alias.__dict__.update(**{"__test__": False})',
    ],
)
def test_fresh_schema_allowance_rejects_literal_proof_dictionary_suppression(
    proof_suppression: str,
    tmp_path: Path,
) -> None:
    """Keep ruled proofs collectable despite literal function-dict suppression."""
    proof_name = FRESH_SCHEMA_PROOF_NAMES[0]
    proof_module = tmp_path / "test_suppressed_proof.py"
    proof_module.write_text(
        "def test_required_proof():\n"
        "    pass\n\n"
        "proof_alias = test_required_proof\n"
        f"{proof_suppression}\n",
        encoding="utf-8",
    )
    collection = subprocess.run(  # noqa: S603  # nosec B603
        [sys.executable, "-m", "pytest", "--collect-only", "-q", proof_module],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        text=True,
    )
    assert collection.returncode == PYTEST_NO_TESTS_COLLECTED
    assert "test_required_proof" not in collection.stdout

    source = (ROOT / FRESH_SCHEMA_PATH).read_text(encoding="utf-8")
    mutated = source + f"\nproof_alias = {proof_name}\n{proof_suppression}\n"
    findings, _unused = _source_findings(FRESH_SCHEMA_PATH, mutated)
    assert findings


def test_fresh_schema_allowance_keeps_unrelated_dictionary_updates_allowed() -> None:
    """Do not turn finite ruled-proof suppression into general dict analysis."""
    source = (ROOT / FRESH_SCHEMA_PATH).read_text(encoding="utf-8")
    proof_name = FRESH_SCHEMA_PROOF_NAMES[0]
    benign_mutations = (
        f"proof_alias = {proof_name}\nproof_alias.__dict__.update(label='proof')",
        "def helper():\n    pass\nhelper.__dict__.update({'__test__': False})",
    )
    for benign_mutation in benign_mutations:
        findings, unused = _source_findings(
            FRESH_SCHEMA_PATH,
            source + "\n" + benign_mutation + "\n",
        )
        assert findings == ()
        assert not unused


def test_static_scanner_resolves_only_the_four_ruled_typing_bindings() -> None:
    """Make imported and assigned typing aliases match their direct forms."""
    retired_algebra = "In" + "variant"
    rejected = (
        f'from typing import TypeAlias as TA\nAlias: TA = "{retired_algebra}"\n',
        f'from typing import ForwardRef as Ref\nAlias = Ref("{retired_algebra}")\n',
        f"from typing import TypeAlias\nTA = TypeAlias\n"
        f'Alias: TA = "{retired_algebra}"\n',
        f"from typing import ForwardRef\nRef = ForwardRef\n"
        f'Alias = Ref("{retired_algebra}")\n',
        f"from typing_extensions import TypeAlias as TA\n"
        f'Alias: TA = "{retired_algebra}"\n',
        f'from typing import ForwardRef\nAlias = ForwardRef(arg="{retired_algebra}")\n',
        f'from typing import ForwardRef as Ref\nAlias = Ref(arg="{retired_algebra}")\n',
    )
    for source in rejected:
        findings, _unused = _source_findings("services/probe.py", source)
        assert findings

    allowed = (
        f'from typing import Literal as L\nlabel: L["{retired_algebra}"] = '
        f'"{retired_algebra}"\n',
        f"from typing import Annotated as A\n"
        f'label: A[str, "{retired_algebra}"] = "ok"\n',
        f"from typing import Literal\nL = Literal\n"
        f'label: L["{retired_algebra}"] = "{retired_algebra}"\n',
        f"from typing import Annotated\nA = Annotated\n"
        f'label: A[str, "{retired_algebra}"] = "ok"\n',
    )
    for source in allowed:
        findings, unused = _source_findings("services/probe.py", source)
        assert findings == ()
        assert not unused

    assert ForwardRef(arg=retired_algebra).__forward_arg__ == retired_algebra


def test_static_scanner_keeps_legacy_fixture_evidence_unreachable() -> None:
    """Allow attempt-30 bytes only in the two exact evidence-proof modules."""
    legacy_fixture_name = "legacy_authority"
    production_references = (
        (
            "services/probe.py",
            'Path("tests/fixtures/issue_210/legacy_authority/outer-envelope.json")',
        ),
        (
            "services/probe.py",
            'FIXTURE_ROOT / "legacy_authority" / "outer-envelope.json"',
        ),
        ("services/probe.py", "import tests.fixtures.issue_210.legacy_authority"),
        (
            "scripts/probe.sh",
            "cat tests/fixtures/issue_210/legacy_authority/outer-envelope.json",
        ),
        (
            "scripts/probe.bash",
            "cat tests/fixtures/issue_210/legacy_authority/outer-envelope.json",
        ),
        (
            "scripts/probe.zsh",
            "cat tests/fixtures/issue_210/legacy_authority/outer-envelope.json",
        ),
    )
    for relative_path, reference in production_references:
        findings, _unused = _source_findings(relative_path, reference)
        assert findings, reference

    prose_findings, prose_unused = _source_findings(
        "scripts/probe.sh",
        "legacy authority fixture is historical evidence",
    )
    assert prose_findings == ()
    assert not prose_unused

    for allowed_path in (SCANNER_PATH, "tests/issue_210/test_fixture_integrity.py"):
        source = (ROOT / allowed_path).read_text(encoding="utf-8")
        findings, unused = _source_findings(allowed_path, source)
        assert findings == ()
        assert not unused
        assert legacy_fixture_name in source


def test_live_inventory_scans_supported_shell_script_suffixes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Do not let executable shell variants bypass retired-surface scanning."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    removed_module = "services." + "authority_" + "review_projection"
    for suffix in (".bash", ".zsh"):
        (scripts / f"probe{suffix}").write_text(
            f"python -m {removed_module}\n",
            encoding="utf-8",
        )

    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    inventoried = _iter_live_text_files()

    assert {path.suffix for path in inventoried} == {".bash", ".zsh"}
    for path in inventoried:
        findings, _unused = _source_findings(
            _relative(path),
            path.read_text(encoding="utf-8"),
        )
        assert findings


def test_live_product_surface_has_no_retired_tokens_or_paths() -> None:
    """Reject retired tokens and paths outside the closed structural allowlist."""
    findings: list[tuple[str, int, str]] = []
    for path in _iter_live_text_files():
        relative_path = _relative(path)
        if relative_path in OPAQUE_HASHES:
            continue
        source = path.read_text(encoding="utf-8", errors="strict")
        source_findings, unused = _source_findings(relative_path, source)
        findings.extend(
            (relative_path, line_number, token)
            for line_number, token in source_findings
        )
        assert not unused, (relative_path, unused)

    assert findings == []

    path_findings = sorted(
        relative_path
        for relative_path in (_relative(path) for path in _iter_live_text_files())
        if RETIRED_PATH_COMPONENT.search(relative_path)
        and relative_path not in OPAQUE_HASHES
        and relative_path != SCANNER_PATH
        and not _is_framed_historical_document(
            relative_path,
            (ROOT / relative_path).read_text(encoding="utf-8", errors="strict"),
        )
    )
    assert path_findings == []
