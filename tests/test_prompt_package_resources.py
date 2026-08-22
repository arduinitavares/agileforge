"""Install-boundary tests for retained ADK prompt resources."""

from __future__ import annotations

import importlib
import importlib.resources
import importlib.util
import multiprocessing
import os
import shutil
import sys
import zipfile
from pathlib import Path

import anyio
from sqlmodel import SQLModel

_RETAINED_PROMPTS = (
    "backlog.txt",
    "product_goal.txt",
    "roadmap.txt",
    "spec_validator.txt",
    "sprint.txt",
    "story.txt",
    "story_patch.txt",
    "vision.txt",
)
_RETAINED_LEAVES = (
    "adapters.adk.agents.backlog",
    "adapters.adk.agents.product_goal",
    "adapters.adk.agents.roadmap",
    "adapters.adk.agents.spec_validator",
    "adapters.adk.agents.sprint",
    "adapters.adk.agents.story",
    "adapters.adk.agents.vision",
)
_OBSOLETE_WHEEL_PATHS = (
    "services/agent_workbench/mutation_" + "ledger.py",
    "services/agent_workbench/backlog_refinement_" + "events.py",
    "models/" + "brown" + "field.py",
    "services/contracts/" + "brown" + "field.py",
    "adapters/adk/agents/" + "brown" + "field.py",
    "utils/" + "brown" + "field_annotations.py",
    "workflow/definitions/onboarding.py",
    "workflow/definitions/scope_extension.py",
    "workflow/requests/onboarding.py",
    "workflow/requests/scope_extension.py",
    "workflow/requests/project_shell.py",
    "workflow/handlers/onboarding.py",
    "workflow/handlers/scope_extension.py",
    "workflow/handlers/project_shell.py",
    "services/agent_workbench/repository_inventory.py",
    "models/agent_workbench.py",
    "services/specs/lifecycle_service.py",
    "services/agent_workbench/backlog_reconciliation.py",
    "services/agent_workbench/vision_phase.py",
    "services/vision_runtime.py",
)
_OBSOLETE_WHEEL_MODULES = (
    "services.agent_workbench.mutation_" + "ledger",
    "services.agent_workbench.backlog_refinement_" + "events",
    "models." + "brown" + "field",
    "services.contracts." + "brown" + "field",
    "adapters.adk.agents." + "brown" + "field",
    "utils." + "brown" + "field_annotations",
    "workflow.definitions.onboarding",
    "workflow.definitions.scope_extension",
    "workflow.requests.onboarding",
    "workflow.requests.scope_extension",
    "workflow.requests.project_shell",
    "workflow.handlers.onboarding",
    "workflow.handlers.scope_extension",
    "workflow.handlers.project_shell",
    "services.agent_workbench.repository_inventory",
    "models.agent_workbench",
    "services.specs.lifecycle_service",
    "services.agent_workbench.backlog_reconciliation",
    "services.agent_workbench.vision_phase",
    "services.vision_runtime",
)


async def _build_wheel(repository_root: Path, wheel_dir: Path) -> None:
    """Build one offline wheel from a clean temporary source snapshot."""
    uv_executable = shutil.which("uv")
    assert uv_executable is not None
    source_root = wheel_dir.parent / "source"
    shutil.copytree(
        repository_root,
        source_root,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".pytest_cache",
            "*.egg-info",
            "__pycache__",
            "build",
            "dist",
        ),
    )
    completed = await anyio.run_process(
        [
            uv_executable,
            "build",
            "--wheel",
            "--offline",
            "--out-dir",
            str(wheel_dir),
        ],
        cwd=source_root,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode()


def _wheel_import_worker(wheel_value: str, repository_root_value: str) -> None:
    """Import retained leaves from only the built wheel and dependencies."""
    wheel = Path(wheel_value).resolve()
    repository_root = Path(repository_root_value).resolve()
    os.environ.pop("MODEL_CONFIG_PATH", None)
    os.environ.update(
        {
            "OPENROUTER_API_KEY": "offline-test-key",
            "RELAX_ZDR_FOR_TESTS": "true",
        }
    )
    for module_name in tuple(sys.modules):
        if module_name.split(".", 1)[0] in {
            "adapters",
            "config",
            "models",
            "services",
            "utils",
        }:
            sys.modules.pop(module_name)
    sys.path = [
        entry for entry in sys.path if Path(entry or ".").resolve() != repository_root
    ]
    sys.path.insert(0, str(wheel))

    for module_name in _RETAINED_LEAVES:
        module = importlib.import_module(module_name)
        assert module.__file__ is not None
        assert str(wheel) in module.__file__
    for module_name in _OBSOLETE_WHEEL_MODULES:
        assert importlib.util.find_spec(module_name) is None
    assert "cli_" + "mutation" + "_ledger" not in SQLModel.metadata.tables
    prompt_root = importlib.resources.files("adapters.adk.prompts")
    for prompt_name in _RETAINED_PROMPTS:
        assert prompt_root.joinpath(prompt_name).read_text(encoding="utf-8").strip()


def test_retained_prompt_loaders_use_package_resources() -> None:
    """Keep prompt loading independent from the source checkout layout."""
    prompt_root = importlib.resources.files("adapters.adk.prompts")
    for prompt_name in _RETAINED_PROMPTS:
        assert prompt_root.joinpath(prompt_name).read_text(encoding="utf-8").strip()

    for path in Path("adapters/adk").rglob("*.py"):
        content = path.read_text(encoding="utf-8")
        assert "Path(__file__)" not in content, path


def test_spec_validator_prompt_has_one_direct_specification_root() -> None:
    """Package the direct Story/PBI/Specification prompt without old contracts."""
    prompt = (
        importlib.resources.files("adapters.adk.prompts")
        .joinpath("spec_validator.txt")
        .read_text(encoding="utf-8")
    )
    assert prompt.count("accepted_specification_json") == 1
    assert "parent_backlog_item_id" in prompt
    assert "parent_backlog_spec_item_ids" in prompt
    assert "StorySpecificationReviewOutput" in prompt
    assert "invariant" not in prompt.casefold()


def test_built_wheel_contains_and_loads_retained_prompt_resources(
    tmp_path: Path,
) -> None:
    """Build the wheel and import every retained leaf from the archive alone."""
    repository_root = Path(__file__).resolve().parents[1]
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    anyio.run(_build_wheel, repository_root, wheel_dir)
    wheel = next(wheel_dir.glob("*.whl"))

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    expected_resources = {
        f"adapters/adk/prompts/{prompt_name}" for prompt_name in _RETAINED_PROMPTS
    }
    assert expected_resources <= names
    assert set(_OBSOLETE_WHEEL_PATHS).isdisjoint(names)

    process = multiprocessing.get_context("spawn").Process(
        target=_wheel_import_worker,
        args=(str(wheel), str(repository_root)),
    )
    process.start()
    process.join(timeout=60)
    if process.is_alive():
        process.terminate()
        process.join()
    assert process.exitcode == 0
