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
    "roadmap.txt",
    "spec_validator.txt",
    "specification.txt",
    "sprint.txt",
    "story.txt",
    "story_patch.txt",
    "vision.txt",
)
_RETAINED_LEAVES = (
    "adapters.adk.agents.authority",
    "adapters.adk.agents.backlog",
    "adapters.adk.agents.brownfield",
    "adapters.adk.agents.roadmap",
    "adapters.adk.agents.spec_validator",
    "adapters.adk.agents.specification",
    "adapters.adk.agents.sprint",
    "adapters.adk.agents.story",
    "adapters.adk.agents.vision",
)
_OBSOLETE_WHEEL_PATHS = (
    "services/agent_workbench/mutation_" + "ledger.py",
    "services/agent_workbench/backlog_refinement_" + "events.py",
)
_OBSOLETE_WHEEL_MODULES = (
    "services.agent_workbench.mutation_" + "ledger",
    "services.agent_workbench.backlog_refinement_" + "events",
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
        entry
        for entry in sys.path
        if Path(entry or ".").resolve() != repository_root
    ]
    sys.path.insert(0, str(wheel))

    for module_name in _RETAINED_LEAVES:
        module = importlib.import_module(module_name)
        assert module.__file__ is not None
        assert str(wheel) in module.__file__
    for module_name in _OBSOLETE_WHEEL_MODULES:
        assert importlib.util.find_spec(module_name) is None
    model_module = importlib.import_module("models.agent_workbench")
    assert not hasattr(model_module, "Cli" + "MutationLedger")
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
        model_source = archive.read("models/agent_workbench.py").decode("utf-8")
    expected_resources = {
        f"adapters/adk/prompts/{prompt_name}" for prompt_name in _RETAINED_PROMPTS
    }
    assert expected_resources <= names
    assert set(_OBSOLETE_WHEEL_PATHS).isdisjoint(names)
    assert "Cli" + "MutationLedger" not in model_source
    assert "cli_" + "mutation" + "_ledger" not in model_source

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
