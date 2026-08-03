"""Boundary tests for the incremental models package migration."""

from __future__ import annotations

import ast
import os
import subprocess  # nosec B404
import sys
from pathlib import Path

# Boundary tests run fixed Python subprocesses.


def _imported_names_from(module_path: Path, import_source: str) -> set[str]:
    source_text = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text, filename=str(module_path))
    imported_names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == import_source:
            imported_names.update(alias.name for alias in node.names)

    return imported_names


def _module_level_imported_names_from(
    module_path: Path, import_source: str
) -> set[str]:
    source_text = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text, filename=str(module_path))
    imported_names: set[str] = set()

    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == import_source:
            imported_names.update(alias.name for alias in node.names)

    return imported_names


def _defined_class_names(module_path: Path) -> set[str]:
    source_text = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text, filename=str(module_path))
    class_names: set[str] = set()

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            class_names.add(node.name)

    return class_names


def test_selected_test_modules_import_hierarchy_models_from_models_core() -> None:
    """Verify selected test modules import hierarchy models from models core."""
    root = Path(__file__).resolve().parents[1]
    selected_modules = [
        Path("tests/test_export_snapshot.py"),
        Path("tests/unit/test_delete_project.py"),
        Path("tests/test_export_import_labels.py"),
        Path("tests/test_spec_validation_modes.py"),
        Path("tests/test_alignment_evidence_persistence.py"),
        Path("tests/test_db_tools.py"),
        Path("tests/test_story_validation_pinning.py"),
    ]

    for module_path in selected_modules:
        core_imports = _module_level_imported_names_from(
            root / module_path, "models.core"
        )
        agile_imports = _module_level_imported_names_from(
            root / module_path, "agile_sqlmodel"
        )

        assert {"Theme", "Epic", "Feature"} <= core_imports, module_path
        assert {"Theme", "Epic", "Feature"}.isdisjoint(agile_imports), module_path


def test_models_package_exports_enum_and_db_boundaries() -> None:
    """Verify models package exports enum and db boundaries."""
    import agile_sqlmodel  # noqa: PLC0415
    from models import enums  # noqa: PLC0415

    assert enums.TaskStatus.__module__ == "models.enums"
    assert enums.StoryStatus.__module__ == "models.enums"
    assert enums.StoryResolution.__module__ == "models.enums"
    assert enums.TaskAcceptanceResult.__module__ == "models.enums"

    assert agile_sqlmodel.TaskStatus is enums.TaskStatus
    assert agile_sqlmodel.StoryStatus is enums.StoryStatus
    assert agile_sqlmodel.StoryResolution is enums.StoryResolution
    assert agile_sqlmodel.TaskAcceptanceResult is enums.TaskAcceptanceResult

    root = Path(__file__).resolve().parents[1]
    agile_sqlmodel_text = (root / "agile_sqlmodel.py").read_text(encoding="utf-8")
    models_db_text = (root / "models" / "db.py").read_text(encoding="utf-8")

    assert "from models.db import (" not in agile_sqlmodel_text
    assert "def __getattr__(name: str):" in agile_sqlmodel_text
    assert "def get_engine(" in models_db_text
    assert "def ensure_business_db_ready(" in models_db_text


def test_models_package_exports_specs_and_events_boundaries() -> None:
    """Verify models package exports specs and events boundaries."""
    import agile_sqlmodel  # noqa: PLC0415
    from models import events, specs  # noqa: PLC0415

    assert specs.SpecRegistry.__module__ == "models.specs"
    assert specs.CompiledSpecAuthority.__module__ == "models.specs"
    assert specs.SpecAuthorityAcceptance.__module__ == "models.specs"
    assert events.TaskExecutionLog.__module__ == "models.events"
    assert events.StoryCompletionLog.__module__ == "models.events"
    assert events.WorkflowEvent.__module__ == "models.events"

    assert agile_sqlmodel.SpecRegistry is specs.SpecRegistry
    assert agile_sqlmodel.CompiledSpecAuthority is specs.CompiledSpecAuthority
    assert agile_sqlmodel.SpecAuthorityAcceptance is specs.SpecAuthorityAcceptance
    assert agile_sqlmodel.TaskExecutionLog is events.TaskExecutionLog
    assert agile_sqlmodel.StoryCompletionLog is events.StoryCompletionLog
    assert agile_sqlmodel.WorkflowEvent is events.WorkflowEvent

    root = Path(__file__).resolve().parents[1]
    agile_sqlmodel_text = (root / "agile_sqlmodel.py").read_text(encoding="utf-8")
    models_specs_text = (root / "models" / "specs.py").read_text(encoding="utf-8")
    models_events_text = (root / "models" / "events.py").read_text(encoding="utf-8")

    assert "from models.events import (" in agile_sqlmodel_text
    assert "from models.specs import (" in agile_sqlmodel_text
    assert "class SpecRegistry(SQLModel, table=True):" in models_specs_text
    assert "class WorkflowEvent(SQLModel, table=True):" in models_events_text


def test_specs_relationship_contract_is_preserved() -> None:
    """Verify specs relationship contract is preserved."""
    from sqlalchemy import inspect  # noqa: PLC0415

    from models import core, specs  # noqa: PLC0415

    product_relationships = inspect(core.Product).relationships
    spec_registry_relationships = inspect(specs.SpecRegistry).relationships
    compiled_authority_relationships = inspect(
        specs.CompiledSpecAuthority
    ).relationships

    assert product_relationships["spec_versions"].mapper.class_ is specs.SpecRegistry
    assert spec_registry_relationships["product"].mapper.class_ is core.Product
    assert (
        spec_registry_relationships["compiled_authority"].mapper.class_
        is specs.CompiledSpecAuthority
    )
    assert (
        compiled_authority_relationships["spec_version"].mapper.class_
        is specs.SpecRegistry
    )


def test_models_package_exports_core_persona_boundary() -> None:
    """Verify models package exports core persona boundary."""
    import agile_sqlmodel  # noqa: PLC0415
    from models import core  # noqa: PLC0415

    assert core.ProductPersona.__module__ == "models.core"
    assert agile_sqlmodel.ProductPersona is core.ProductPersona

    root = Path(__file__).resolve().parents[1]
    agile_sqlmodel_text = (root / "agile_sqlmodel.py").read_text(encoding="utf-8")
    models_core_text = (root / "models" / "core.py").read_text(encoding="utf-8")

    assert "from models.core import (" in agile_sqlmodel_text
    assert "ProductPersona," in agile_sqlmodel_text
    assert "class ProductPersona(SQLModel, table=True):" in models_core_text


def test_models_package_exports_core_product_boundary() -> None:
    """Verify models package exports core product boundary."""
    import agile_sqlmodel  # noqa: PLC0415
    from models import core  # noqa: PLC0415

    assert core.Product.__module__ == "models.core"
    assert agile_sqlmodel.Product is core.Product

    root = Path(__file__).resolve().parents[1]
    agile_sqlmodel_text = (root / "agile_sqlmodel.py").read_text(encoding="utf-8")
    models_core_text = (root / "models" / "core.py").read_text(encoding="utf-8")

    assert "from models.core import (" in agile_sqlmodel_text
    assert "Product," in agile_sqlmodel_text
    assert "class Product(SQLModel, table=True):" in models_core_text


def test_models_package_exports_core_task_boundary() -> None:
    """Verify models package exports core task boundary."""
    import agile_sqlmodel  # noqa: PLC0415
    from models import core  # noqa: PLC0415

    assert core.Task.__module__ == "models.core"
    assert agile_sqlmodel.Task is core.Task

    root = Path(__file__).resolve().parents[1]
    agile_sqlmodel_path = root / "agile_sqlmodel.py"
    models_core_path = root / "models" / "core.py"

    assert "Task" in _module_level_imported_names_from(
        agile_sqlmodel_path, "models.core"
    )
    assert "Task" not in _defined_class_names(agile_sqlmodel_path)
    assert "Task" in _defined_class_names(models_core_path)


def test_models_package_exports_core_sprint_boundary() -> None:
    """Verify models package exports core sprint boundary."""
    import agile_sqlmodel  # noqa: PLC0415
    from models import core  # noqa: PLC0415

    assert core.Sprint.__module__ == "models.core"
    assert agile_sqlmodel.Sprint is core.Sprint

    root = Path(__file__).resolve().parents[1]
    agile_sqlmodel_path = root / "agile_sqlmodel.py"
    models_core_path = root / "models" / "core.py"

    assert "Sprint" in _module_level_imported_names_from(
        agile_sqlmodel_path, "models.core"
    )
    assert "Sprint" not in _defined_class_names(agile_sqlmodel_path)
    assert "Sprint" in _defined_class_names(models_core_path)


def test_models_package_exports_core_user_story_boundary() -> None:
    """Verify models package exports core user story boundary."""
    import agile_sqlmodel  # noqa: PLC0415
    from models import core  # noqa: PLC0415

    assert core.UserStory.__module__ == "models.core"
    assert agile_sqlmodel.UserStory is core.UserStory


def test_models_package_exports_core_team_boundary() -> None:
    """Verify models package exports core team boundary."""
    import agile_sqlmodel  # noqa: PLC0415
    from models import core, enums  # noqa: PLC0415

    assert core.Team.__module__ == "models.core"
    assert core.TeamMember.__module__ == "models.core"
    assert agile_sqlmodel.Team is core.Team
    assert agile_sqlmodel.TeamMember is core.TeamMember
    assert agile_sqlmodel.TeamRole is enums.TeamRole

    root = Path(__file__).resolve().parents[1]
    agile_sqlmodel_text = (root / "agile_sqlmodel.py").read_text(encoding="utf-8")
    models_core_text = (root / "models" / "core.py").read_text(encoding="utf-8")

    assert "from models.core import (" in agile_sqlmodel_text
    assert "Team," in agile_sqlmodel_text
    assert "TeamMember," in agile_sqlmodel_text
    assert "TeamRole," in agile_sqlmodel_text
    assert "class Team(SQLModel, table=True):" in models_core_text
    assert "class TeamMember(SQLModel, table=True):" in models_core_text


def test_core_task_relationship_contract_is_preserved() -> None:
    """Verify core task relationship contract is preserved."""
    from sqlalchemy import inspect  # noqa: PLC0415

    from models import core  # noqa: PLC0415

    user_story_relationships = inspect(core.UserStory).relationships
    team_member_relationships = inspect(core.TeamMember).relationships
    task_relationships = inspect(core.Task).relationships

    assert "tasks" in user_story_relationships
    assert user_story_relationships["tasks"].mapper.class_ is core.Task
    assert "tasks" in team_member_relationships
    assert team_member_relationships["tasks"].mapper.class_ is core.Task
    assert "story" in task_relationships
    assert task_relationships["story"].mapper.class_ is core.UserStory
    assert "assignee" in task_relationships
    assert task_relationships["assignee"].mapper.class_ is core.TeamMember


def test_core_sprint_relationship_contract_is_preserved() -> None:
    """Verify core sprint relationship contract is preserved."""
    from sqlalchemy import inspect  # noqa: PLC0415

    from models import core  # noqa: PLC0415

    sprint_relationships = inspect(core.Sprint).relationships

    assert "product" in sprint_relationships
    assert sprint_relationships["product"].mapper.class_ is core.Product
    assert "team" in sprint_relationships
    assert sprint_relationships["team"].mapper.class_ is core.Team
    assert "stories" in sprint_relationships
    assert sprint_relationships["stories"].mapper.class_ is core.UserStory


def test_sprint_story_link_model_continuity_is_preserved() -> None:
    """Verify sprint story link model continuity is preserved."""
    import agile_sqlmodel  # noqa: PLC0415
    from models import core  # noqa: PLC0415

    assert core.SprintStory.__module__ == "models.core"
    assert agile_sqlmodel.SprintStory is core.SprintStory
    assert (
        core.Sprint.__sqlmodel_relationships__["stories"].link_model is core.SprintStory
    )
    assert (
        core.UserStory.__sqlmodel_relationships__["sprints"].link_model
        is core.SprintStory
    )
    assert core.SprintStory.__sqlmodel_relationships__ == {}


def test_core_persona_relationship_contract_is_preserved() -> None:
    """Verify core persona relationship contract is preserved."""
    from sqlalchemy import inspect  # noqa: PLC0415

    import agile_sqlmodel  # noqa: PLC0415
    from models import core  # noqa: PLC0415

    product_relationships = inspect(agile_sqlmodel.Product).relationships
    persona_relationships = inspect(core.ProductPersona).relationships

    assert "personas" in product_relationships
    assert product_relationships["personas"].mapper.class_ is core.ProductPersona
    assert "product" in persona_relationships
    assert persona_relationships["product"].mapper.class_ is agile_sqlmodel.Product


def test_core_product_relationship_contract_is_preserved() -> None:
    """Verify core product relationship contract is preserved."""
    from sqlalchemy import inspect  # noqa: PLC0415

    import agile_sqlmodel  # noqa: PLC0415
    from models import core, specs  # noqa: PLC0415

    product_relationships = inspect(core.Product).relationships
    team_relationships = inspect(core.Team).relationships
    theme_relationships = inspect(core.Theme).relationships
    persona_relationships = inspect(core.ProductPersona).relationships
    spec_registry_relationships = inspect(specs.SpecRegistry).relationships
    sprint_relationships = inspect(core.Sprint).relationships
    story_relationships = inspect(agile_sqlmodel.UserStory).relationships

    assert product_relationships["teams"].mapper.class_ is core.Team
    assert product_relationships["themes"].mapper.class_ is core.Theme
    assert product_relationships["stories"].mapper.class_ is agile_sqlmodel.UserStory
    assert product_relationships["sprints"].mapper.class_ is core.Sprint
    assert product_relationships["personas"].mapper.class_ is core.ProductPersona
    assert product_relationships["spec_versions"].mapper.class_ is specs.SpecRegistry
    assert team_relationships["products"].mapper.class_ is core.Product
    assert theme_relationships["product"].mapper.class_ is core.Product
    assert persona_relationships["product"].mapper.class_ is core.Product
    assert spec_registry_relationships["product"].mapper.class_ is core.Product
    assert sprint_relationships["product"].mapper.class_ is core.Product
    assert story_relationships["product"].mapper.class_ is core.Product


def test_core_team_relationship_contract_is_preserved() -> None:
    """Verify core team relationship contract is preserved."""
    from sqlalchemy import inspect  # noqa: PLC0415

    import agile_sqlmodel  # noqa: PLC0415
    from models import core  # noqa: PLC0415

    product_relationships = inspect(agile_sqlmodel.Product).relationships
    team_relationships = inspect(core.Team).relationships
    member_relationships = inspect(core.TeamMember).relationships
    sprint_relationships = inspect(core.Sprint).relationships
    task_relationships = inspect(agile_sqlmodel.Task).relationships

    assert "teams" in product_relationships
    assert product_relationships["teams"].mapper.class_ is core.Team
    assert "products" in team_relationships
    assert team_relationships["products"].mapper.class_ is agile_sqlmodel.Product
    assert "members" in team_relationships
    assert team_relationships["members"].mapper.class_ is core.TeamMember
    assert "sprints" in team_relationships
    assert team_relationships["sprints"].mapper.class_ is core.Sprint
    assert "teams" in member_relationships
    assert member_relationships["teams"].mapper.class_ is core.Team
    assert "tasks" in member_relationships
    assert member_relationships["tasks"].mapper.class_ is core.Task
    assert "team" in sprint_relationships
    assert sprint_relationships["team"].mapper.class_ is core.Team
    assert "assignee" in task_relationships
    assert task_relationships["assignee"].mapper.class_ is core.TeamMember
    assert "story" in task_relationships
    assert task_relationships["story"].mapper.class_ is agile_sqlmodel.UserStory


def test_models_package_exports_core_link_boundaries() -> None:
    """Verify models package exports core link boundaries."""
    import agile_sqlmodel  # noqa: PLC0415
    from models import core  # noqa: PLC0415

    assert core.TeamMembership.__module__ == "models.core"
    assert core.ProductTeam.__module__ == "models.core"
    assert core.SprintStory.__module__ == "models.core"

    assert agile_sqlmodel.TeamMembership is core.TeamMembership
    assert agile_sqlmodel.ProductTeam is core.ProductTeam
    assert agile_sqlmodel.SprintStory is core.SprintStory

    root = Path(__file__).resolve().parents[1]
    agile_sqlmodel_text = (root / "agile_sqlmodel.py").read_text(encoding="utf-8")
    models_core_text = (root / "models" / "core.py").read_text(encoding="utf-8")

    assert "from models.core import (" in agile_sqlmodel_text
    assert "class TeamMembership(SQLModel, table=True):" in models_core_text
    assert "class ProductTeam(SQLModel, table=True):" in models_core_text
    assert "class SprintStory(SQLModel, table=True):" in models_core_text


def test_models_package_exports_core_hierarchy_boundaries() -> None:
    """Verify models package exports core hierarchy boundaries."""
    import agile_sqlmodel  # noqa: PLC0415
    from models import core  # noqa: PLC0415

    assert core.Theme.__module__ == "models.core"
    assert core.Epic.__module__ == "models.core"
    assert core.Feature.__module__ == "models.core"

    assert agile_sqlmodel.Theme is core.Theme
    assert agile_sqlmodel.Epic is core.Epic
    assert agile_sqlmodel.Feature is core.Feature

    root = Path(__file__).resolve().parents[1]
    agile_sqlmodel_text = (root / "agile_sqlmodel.py").read_text(encoding="utf-8")
    models_core_text = (root / "models" / "core.py").read_text(encoding="utf-8")

    assert "from models.core import (" in agile_sqlmodel_text
    assert "Theme," in agile_sqlmodel_text
    assert "Epic," in agile_sqlmodel_text
    assert "Feature," in agile_sqlmodel_text
    assert "class Theme(SQLModel, table=True):" in models_core_text
    assert "class Epic(SQLModel, table=True):" in models_core_text
    assert "class Feature(SQLModel, table=True):" in models_core_text


def test_core_hierarchy_relationship_contract_is_preserved() -> None:
    """Verify core hierarchy relationship contract is preserved."""
    from sqlalchemy import inspect  # noqa: PLC0415

    import agile_sqlmodel  # noqa: PLC0415
    from models import core  # noqa: PLC0415

    product_relationships = inspect(agile_sqlmodel.Product).relationships
    story_relationships = inspect(agile_sqlmodel.UserStory).relationships
    theme_relationships = inspect(core.Theme).relationships
    epic_relationships = inspect(core.Epic).relationships
    feature_relationships = inspect(core.Feature).relationships

    assert product_relationships["themes"].mapper.class_ is core.Theme
    assert theme_relationships["product"].mapper.class_ is agile_sqlmodel.Product
    assert theme_relationships["epics"].mapper.class_ is core.Epic
    assert epic_relationships["theme"].mapper.class_ is core.Theme
    assert epic_relationships["features"].mapper.class_ is core.Feature
    assert feature_relationships["epic"].mapper.class_ is core.Epic
    assert feature_relationships["stories"].mapper.class_ is agile_sqlmodel.UserStory
    assert story_relationships["feature"].mapper.class_ is core.Feature


def test_core_user_story_relationship_contract_is_preserved() -> None:
    """Verify core user story relationship contract is preserved."""
    from sqlalchemy import inspect  # noqa: PLC0415

    from models import core  # noqa: PLC0415

    product_relationships = inspect(core.Product).relationships
    feature_relationships = inspect(core.Feature).relationships
    story_relationships = inspect(core.UserStory).relationships

    assert product_relationships["stories"].mapper.class_ is core.UserStory
    assert feature_relationships["stories"].mapper.class_ is core.UserStory
    assert story_relationships["product"].mapper.class_ is core.Product
    assert story_relationships["feature"].mapper.class_ is core.Feature
    assert story_relationships["sprints"].mapper.class_ is core.Sprint
    assert story_relationships["tasks"].mapper.class_ is core.Task


def test_core_user_story_boundary_is_safe_in_fresh_process(
    tmp_path: Path,
) -> None:
    """Verify core user story boundary is safe in fresh process."""
    root = Path(__file__).resolve().parents[1]
    command = (
        "from models import core; "
        "from sqlalchemy import inspect; "
        "rels = inspect(core.UserStory).relationships; "
        "assert rels['sprints'].mapper.class_.__name__ == 'Sprint'; "
        "assert rels['tasks'].mapper.class_.__name__ == 'Task'; "
        "assert rels['sprints'].mapper.class_.__module__ == 'models.core'; "
        "assert rels['tasks'].mapper.class_.__module__ == 'models.core'"
    )
    env = os.environ.copy()
    env["AGILEFORGE_DB_URL"] = f"sqlite:///{tmp_path / 'fresh-process.db'}"

    result = subprocess.run(  # noqa: S603  # nosec B603
        [sys.executable, "-c", command],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_models_core_import_does_not_require_db_env() -> None:
    """Verify models core import does not require db env."""
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.pop("AGILEFORGE_DB_URL", None)

    result = subprocess.run(  # nosec B603
        [sys.executable, "-c", "import models.core"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_models_core_task_boundary_does_not_require_db_env() -> None:
    """Verify models core task boundary does not require db env."""
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.pop("AGILEFORGE_DB_URL", None)

    result = subprocess.run(  # nosec B603
        [
            sys.executable,
            "-c",
            "from models import core; assert core.Task.__module__ == 'models.core'",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_models_core_sprint_boundary_does_not_require_db_env() -> None:
    """Verify models core sprint boundary does not require db env."""
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.pop("AGILEFORGE_DB_URL", None)

    result = subprocess.run(  # nosec B603
        [
            sys.executable,
            "-c",
            "from models import core; assert core.Sprint.__module__ == 'models.core'",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_agile_sqlmodel_script_entrypoint_stays_safe_after_user_story_move(
    tmp_path: Path,
) -> None:
    """Verify agile sqlmodel script entrypoint stays safe after user story move."""
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["AGILEFORGE_DB_URL"] = f"sqlite:///{tmp_path / 'business.db'}"

    result = subprocess.run(  # noqa: S603  # nosec B603
        [sys.executable, str(root / "agile_sqlmodel.py")],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_runtime_modules_import_new_model_boundaries() -> None:
    """Verify runtime modules import new model boundaries."""
    from utils import api_schemas  # noqa: PLC0415

    assert api_schemas.TaskStatus.__module__ == "models.enums"
    assert api_schemas.TaskAcceptanceResult.__module__ == "models.enums"
    assert api_schemas.StoryResolution.__module__ == "models.enums"

    root = Path(__file__).resolve().parents[1]
    api_text = (root / "api.py").read_text(encoding="utf-8")
    application_text = (root / "services" / "application.py").read_text(
        encoding="utf-8"
    )
    product_repo_text = (root / "repositories" / "product.py").read_text(
        encoding="utf-8"
    )
    story_close_text = (root / "services" / "story_close_service.py").read_text(
        encoding="utf-8"
    )
    task_execution_text = (root / "services" / "task_execution_service.py").read_text(
        encoding="utf-8"
    )

    assert "from models.core" not in api_text
    assert "from models.enums" not in api_text
    assert (
        "from models.db import ensure_business_db_ready, get_engine"
        in application_text
    )
    assert "from models.db import get_engine" in product_repo_text
    assert "from models.enums import StoryStatus" in story_close_text
    assert "from models.enums import TaskAcceptanceResult" in task_execution_text


def test_runtime_modules_import_new_core_boundary() -> None:
    """Verify runtime modules import new core boundary."""
    root = Path(__file__).resolve().parents[1]
    db_tools_text = (root / "tools" / "db_tools.py").read_text(encoding="utf-8")

    assert "from models.core import " in db_tools_text
    assert "ProductPersona" in db_tools_text


def test_runtime_modules_import_new_core_product_boundary() -> None:
    """Verify runtime modules import new core product boundary."""
    root = Path(__file__).resolve().parents[1]

    orchestrator_query_core_imports = _imported_names_from(
        root / "services" / "orchestrator_query_service.py",
        "models.core",
    )
    orchestrator_query_agile_imports = _imported_names_from(
        root / "services" / "orchestrator_query_service.py",
        "agile_sqlmodel",
    )
    orchestrator_context_core_imports = _imported_names_from(
        root / "services" / "orchestrator_context_service.py",
        "models.core",
    )
    orchestrator_context_agile_imports = _imported_names_from(
        root / "services" / "orchestrator_context_service.py",
        "agile_sqlmodel",
    )
    compiler_core_imports = _imported_names_from(
        root / "services" / "specs" / "compiler_service.py",
        "models.core",
    )
    compiler_agile_imports = _imported_names_from(
        root / "services" / "specs" / "compiler_service.py",
        "agile_sqlmodel",
    )
    lifecycle_core_imports = _imported_names_from(
        root / "services" / "specs" / "lifecycle_service.py",
        "models.core",
    )
    lifecycle_agile_imports = _imported_names_from(
        root / "services" / "specs" / "lifecycle_service.py",
        "agile_sqlmodel",
    )

    assert {"Product", "SprintStory"} <= orchestrator_query_core_imports
    assert "Product" not in orchestrator_query_agile_imports
    assert {"Product", "Epic", "Feature", "Theme"} <= orchestrator_context_core_imports
    assert "Product" not in orchestrator_context_agile_imports
    assert {"Product"} <= compiler_core_imports
    assert "Product" not in compiler_agile_imports
    assert {"Product"} <= lifecycle_core_imports
    assert "Product" not in lifecycle_agile_imports


def test_runtime_modules_import_new_core_link_boundary() -> None:
    """Verify runtime modules import new core link boundary."""
    root = Path(__file__).resolve().parents[1]
    orchestrator_query_core_imports = _imported_names_from(
        root / "services" / "orchestrator_query_service.py",
        "models.core",
    )
    sprint_planner_core_imports = _imported_names_from(
        root
        / "orchestrator_agent"
        / "agent_tools"
        / "sprint_planner_tool"
        / "tools.py",
        "models.core",
    )
    benchmark_text = (root / "scripts" / "benchmark_sprint_planning.py").read_text(
        encoding="utf-8"
    )

    assert "SprintStory" in orchestrator_query_core_imports
    assert "SprintStory" in sprint_planner_core_imports
    assert "from models.core import ProductTeam" in benchmark_text


def test_runtime_modules_import_new_core_hierarchy_boundary() -> None:
    """Verify runtime modules import new core hierarchy boundary."""
    root = Path(__file__).resolve().parents[1]
    orchestrator_context_text = (
        root / "services" / "orchestrator_context_service.py"
    ).read_text(encoding="utf-8")
    story_validation_text = (
        root / "services" / "specs" / "story_validation_service.py"
    ).read_text(encoding="utf-8")
    db_tools_text = (root / "tools" / "db_tools.py").read_text(encoding="utf-8")  # noqa: F841

    assert "from models.core import Epic, Feature, Theme" in orchestrator_context_text
    assert "from models.core import Feature" in story_validation_text
    assert {"Epic", "Feature", "ProductPersona", "Theme"} <= _imported_names_from(
        root / "tools" / "db_tools.py", "models.core"
    )


def test_runtime_modules_import_new_core_hierarchy_cleanup_boundary() -> None:
    """Verify runtime modules import new core hierarchy cleanup boundary."""
    root = Path(__file__).resolve().parents[1]
    expected_names = {"Epic", "Feature", "Theme"}

    story_query_imports = _imported_names_from(
        root / "tools" / "story_query_tools.py",
        "models.core",
    )
    export_snapshot_imports = _imported_names_from(
        root / "tools" / "export_snapshot.py",
        "models.core",
    )
    product_repo_imports = _imported_names_from(
        root / "repositories" / "product.py",
        "models.core",
    )

    assert expected_names <= story_query_imports
    assert expected_names <= export_snapshot_imports
    assert expected_names <= product_repo_imports


def test_runtime_scripts_import_hierarchy_models_from_core() -> None:
    """Verify runtime scripts import hierarchy models from core."""
    root = Path(__file__).resolve().parents[1]
    script_expectations = {
        "scripts/verify_query_features_performance.py": {"Theme", "Epic", "Feature"},
        "scripts/verify_backlog_optimization.py": {"Theme", "Epic", "Feature"},
        "scripts/benchmark_product_structure.py": {"Theme", "Epic", "Feature"},
        "scripts/fix_persona_drift.py": {"Feature"},
        "scripts/benchmark_sprint_planning.py": {"Theme", "Epic", "Feature"},
    }

    for script_relpath, expected_names in script_expectations.items():
        script_path = root / script_relpath
        core_imports = _imported_names_from(script_path, "models.core")
        agile_imports = _imported_names_from(script_path, "agile_sqlmodel")

        assert expected_names <= core_imports, script_relpath
        assert expected_names.isdisjoint(agile_imports), script_relpath


def test_benchmark_project_details_imports_hierarchy_models_from_core() -> None:
    """Verify benchmark project details imports hierarchy models from core."""
    root = Path(__file__).resolve().parents[1]
    script_path = root / "scripts" / "benchmark_project_details.py"

    core_imports = _imported_names_from(script_path, "models.core")
    agile_imports = _imported_names_from(script_path, "agile_sqlmodel")

    assert {"Theme", "Epic", "Feature"} <= core_imports
    assert {"Theme", "Epic", "Feature"}.isdisjoint(agile_imports)
    assert {"Product", "UserStory", "StoryStatus"} <= agile_imports


def test_runtime_modules_import_new_spec_and_event_boundaries() -> None:
    """Verify runtime modules import new spec and event boundaries."""
    root = Path(__file__).resolve().parents[1]
    api_text = (root / "api.py").read_text(encoding="utf-8")
    authority_handler_text = (
        root / "workflow" / "handlers" / "authority.py"
    ).read_text(encoding="utf-8")
    execution_handler_text = (
        root / "workflow" / "handlers" / "execution.py"
    ).read_text(encoding="utf-8")
    orchestrator_context_text = (
        root / "services" / "orchestrator_context_service.py"
    ).read_text(encoding="utf-8")
    compiler_service_text = (
        root / "services" / "specs" / "compiler_service.py"
    ).read_text(encoding="utf-8")
    story_validation_text = (
        root / "services" / "specs" / "story_validation_service.py"
    ).read_text(encoding="utf-8")

    assert "from models.events" not in api_text
    assert "from models.specs" not in api_text
    assert "from models.specs import " in authority_handler_text
    assert "from services.task_execution_service import (" in execution_handler_text
    assert "from models.specs import " in orchestrator_context_text
    assert "from models.specs import " in compiler_service_text
    assert "from models.specs import " in story_validation_text
