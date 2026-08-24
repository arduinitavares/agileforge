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
        Path("tests/unit/test_delete_project.py"),
        Path("tests/test_db_tools.py"),
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
    assert events.TaskExecutionLog.__module__ == "models.events"
    assert events.StoryCompletionLog.__module__ == "models.events"
    assert events.WorkflowEvent.__module__ == "models.events"

    assert agile_sqlmodel.SpecRegistry is specs.SpecRegistry
    assert agile_sqlmodel.TaskExecutionLog is events.TaskExecutionLog
    assert agile_sqlmodel.StoryCompletionLog is events.StoryCompletionLog
    assert agile_sqlmodel.WorkflowEvent is events.WorkflowEvent

    root = Path(__file__).resolve().parents[1]
    agile_sqlmodel_text = (root / "agile_sqlmodel.py").read_text(encoding="utf-8")
    models_specs_text = (root / "models" / "specs.py").read_text(encoding="utf-8")
    models_events_text = (root / "models" / "events.py").read_text(encoding="utf-8")

    assert "from models.events import (" in agile_sqlmodel_text
    assert "from models.specs import SpecRegistry" in agile_sqlmodel_text
    assert "class SpecRegistry(SQLModel, table=True):" in models_specs_text
    assert "class WorkflowEvent(SQLModel, table=True):" in models_events_text


def test_specs_relationship_contract_is_preserved() -> None:
    """Verify specs relationship contract is preserved."""
    from sqlalchemy import inspect  # noqa: PLC0415

    from models import core, specs  # noqa: PLC0415

    project_relationships = inspect(core.Project).relationships
    spec_registry_relationships = inspect(specs.SpecRegistry).relationships

    assert project_relationships["spec_versions"].mapper.class_ is specs.SpecRegistry
    assert spec_registry_relationships["project"].mapper.class_ is core.Project


def test_models_package_exports_core_persona_boundary() -> None:
    """Verify models package exports core persona boundary."""
    import agile_sqlmodel  # noqa: PLC0415
    from models import core  # noqa: PLC0415

    assert core.ProjectPersona.__module__ == "models.core"
    assert agile_sqlmodel.ProjectPersona is core.ProjectPersona

    root = Path(__file__).resolve().parents[1]
    agile_sqlmodel_text = (root / "agile_sqlmodel.py").read_text(encoding="utf-8")
    models_core_text = (root / "models" / "core.py").read_text(encoding="utf-8")

    assert "from models.core import (" in agile_sqlmodel_text
    assert "ProjectPersona," in agile_sqlmodel_text
    assert "class ProjectPersona(SQLModel, table=True):" in models_core_text


def test_models_package_exports_core_project_boundary() -> None:
    """Verify models package exports core project boundary."""
    import agile_sqlmodel  # noqa: PLC0415
    from models import core  # noqa: PLC0415

    assert core.Project.__module__ == "models.core"
    assert agile_sqlmodel.Project is core.Project

    root = Path(__file__).resolve().parents[1]
    agile_sqlmodel_text = (root / "agile_sqlmodel.py").read_text(encoding="utf-8")
    models_core_text = (root / "models" / "core.py").read_text(encoding="utf-8")

    assert "from models.core import (" in agile_sqlmodel_text
    assert "Project," in agile_sqlmodel_text
    assert "class Project(SQLModel, table=True):" in models_core_text


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

    assert "project" in sprint_relationships
    assert sprint_relationships["project"].mapper.class_ is core.Project
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

    project_relationships = inspect(agile_sqlmodel.Project).relationships
    persona_relationships = inspect(core.ProjectPersona).relationships

    assert "personas" in project_relationships
    assert project_relationships["personas"].mapper.class_ is core.ProjectPersona
    assert "project" in persona_relationships
    assert persona_relationships["project"].mapper.class_ is agile_sqlmodel.Project


def test_core_project_relationship_contract_is_preserved() -> None:
    """Verify core project relationship contract is preserved."""
    from sqlalchemy import inspect  # noqa: PLC0415

    import agile_sqlmodel  # noqa: PLC0415
    from models import core, specs  # noqa: PLC0415

    project_relationships = inspect(core.Project).relationships
    team_relationships = inspect(core.Team).relationships
    theme_relationships = inspect(core.Theme).relationships
    persona_relationships = inspect(core.ProjectPersona).relationships
    spec_registry_relationships = inspect(specs.SpecRegistry).relationships
    sprint_relationships = inspect(core.Sprint).relationships
    story_relationships = inspect(agile_sqlmodel.UserStory).relationships

    assert project_relationships["teams"].mapper.class_ is core.Team
    assert project_relationships["themes"].mapper.class_ is core.Theme
    assert project_relationships["stories"].mapper.class_ is agile_sqlmodel.UserStory
    assert project_relationships["sprints"].mapper.class_ is core.Sprint
    assert project_relationships["personas"].mapper.class_ is core.ProjectPersona
    assert project_relationships["spec_versions"].mapper.class_ is specs.SpecRegistry
    assert team_relationships["projects"].mapper.class_ is core.Project
    assert theme_relationships["project"].mapper.class_ is core.Project
    assert persona_relationships["project"].mapper.class_ is core.Project
    assert spec_registry_relationships["project"].mapper.class_ is core.Project
    assert sprint_relationships["project"].mapper.class_ is core.Project
    assert story_relationships["project"].mapper.class_ is core.Project


def test_core_team_relationship_contract_is_preserved() -> None:
    """Verify core team relationship contract is preserved."""
    from sqlalchemy import inspect  # noqa: PLC0415

    import agile_sqlmodel  # noqa: PLC0415
    from models import core  # noqa: PLC0415

    project_relationships = inspect(agile_sqlmodel.Project).relationships
    team_relationships = inspect(core.Team).relationships
    member_relationships = inspect(core.TeamMember).relationships
    sprint_relationships = inspect(core.Sprint).relationships
    task_relationships = inspect(agile_sqlmodel.Task).relationships

    assert "teams" in project_relationships
    assert project_relationships["teams"].mapper.class_ is core.Team
    assert "projects" in team_relationships
    assert team_relationships["projects"].mapper.class_ is agile_sqlmodel.Project
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
    assert core.ProjectTeam.__module__ == "models.core"
    assert core.SprintStory.__module__ == "models.core"

    assert agile_sqlmodel.TeamMembership is core.TeamMembership
    assert agile_sqlmodel.ProjectTeam is core.ProjectTeam
    assert agile_sqlmodel.SprintStory is core.SprintStory

    root = Path(__file__).resolve().parents[1]
    agile_sqlmodel_text = (root / "agile_sqlmodel.py").read_text(encoding="utf-8")
    models_core_text = (root / "models" / "core.py").read_text(encoding="utf-8")

    assert "from models.core import (" in agile_sqlmodel_text
    assert "class TeamMembership(SQLModel, table=True):" in models_core_text
    assert "class ProjectTeam(SQLModel, table=True):" in models_core_text
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

    project_relationships = inspect(agile_sqlmodel.Project).relationships
    theme_relationships = inspect(core.Theme).relationships
    epic_relationships = inspect(core.Epic).relationships
    feature_relationships = inspect(core.Feature).relationships

    assert project_relationships["themes"].mapper.class_ is core.Theme
    assert theme_relationships["project"].mapper.class_ is agile_sqlmodel.Project
    assert theme_relationships["epics"].mapper.class_ is core.Epic
    assert epic_relationships["theme"].mapper.class_ is core.Theme
    assert epic_relationships["features"].mapper.class_ is core.Feature
    assert feature_relationships["epic"].mapper.class_ is core.Epic


def test_core_user_story_relationship_contract_is_preserved() -> None:
    """Verify core user story relationship contract is preserved."""
    from sqlalchemy import inspect  # noqa: PLC0415

    from models import core  # noqa: PLC0415

    project_relationships = inspect(core.Project).relationships
    story_relationships = inspect(core.UserStory).relationships

    assert project_relationships["stories"].mapper.class_ is core.UserStory
    assert story_relationships["project"].mapper.class_ is core.Project
    assert story_relationships["sprints"].mapper.class_ is core.Sprint
    assert story_relationships["tasks"].mapper.class_ is core.Task


def test_core_user_story_boundary_is_safe_in_fresh_process(
    tmp_path: Path,
) -> None:
    """Verify core user story boundary is safe in fresh process."""
    root = Path(__file__).resolve().parents[1]
    command = (
        "from models import core, workflow; "
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


def test_story_validation_evidence_schema_is_complete_in_fresh_process() -> None:
    """Resolve the Task 9 evidence schema without relying on import order."""
    root = Path(__file__).resolve().parents[1]
    command = (
        "from utils.spec_schemas import ValidationEvidence; "
        "schema = ValidationEvidence.model_json_schema(); "
        "assert schema['properties']['validated_at']['format'] == 'date-time'"
    )

    result = subprocess.run(  # noqa: S603  # nosec B603
        [sys.executable, "-c", command],
        cwd=root,
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
    project_repo_text = (root / "repositories" / "project.py").read_text(
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
        "from models.db import ensure_business_db_ready, get_engine" in application_text
    )
    assert "from models.db import get_engine" in project_repo_text
    assert "from models.enums import StoryStatus" in story_close_text
    assert "from models.enums import TaskAcceptanceResult" in task_execution_text


def test_runtime_modules_import_new_core_boundary() -> None:
    """Verify runtime modules import new core boundary."""
    root = Path(__file__).resolve().parents[1]
    db_tools_text = (root / "tools" / "db_tools.py").read_text(encoding="utf-8")

    assert "from models.core import " in db_tools_text
    assert "ProjectPersona" in db_tools_text


def test_runtime_modules_import_new_core_link_boundary() -> None:
    """Verify the canonical repository imports Project link models."""
    root = Path(__file__).resolve().parents[1]
    repository_imports = _imported_names_from(
        root / "repositories" / "project.py",
        "models.core",
    )

    assert {"Project", "SprintStory"} <= repository_imports


def test_runtime_modules_import_new_core_hierarchy_boundary() -> None:
    """Verify runtime modules import new core hierarchy boundary."""
    root = Path(__file__).resolve().parents[1]

    assert {"Epic", "Feature", "ProjectPersona", "Theme"} <= _imported_names_from(
        root / "tools" / "db_tools.py", "models.core"
    )


def test_runtime_modules_import_new_core_hierarchy_cleanup_boundary() -> None:
    """Verify runtime modules import new core hierarchy cleanup boundary."""
    root = Path(__file__).resolve().parents[1]
    expected_names = {"Epic", "Feature", "Theme"}

    export_snapshot_imports = _imported_names_from(
        root / "tools" / "export_snapshot.py",
        "models.core",
    )
    project_repo_imports = _imported_names_from(
        root / "repositories" / "project.py",
        "models.core",
    )

    assert expected_names <= export_snapshot_imports
    assert expected_names <= project_repo_imports


def test_runtime_scripts_import_hierarchy_models_from_core() -> None:
    """Verify runtime scripts import hierarchy models from core."""
    root = Path(__file__).resolve().parents[1]
    script_expectations = {
        "scripts/benchmark_project_structure.py": {"Theme", "Epic", "Feature"},
    }

    for script_relpath, expected_names in script_expectations.items():
        script_path = root / script_relpath
        core_imports = _imported_names_from(script_path, "models.core")
        agile_imports = _imported_names_from(script_path, "agile_sqlmodel")

        assert expected_names <= core_imports, script_relpath
        assert expected_names.isdisjoint(agile_imports), script_relpath


def test_runtime_modules_import_new_spec_and_event_boundaries() -> None:
    """Verify runtime modules import new spec and event boundaries."""
    root = Path(__file__).resolve().parents[1]
    api_text = (root / "api.py").read_text(encoding="utf-8")
    execution_handler_text = (
        root / "workflow" / "handlers" / "execution.py"
    ).read_text(encoding="utf-8")
    story_validation_text = (
        root / "services" / "specs" / "story_validation_service.py"
    ).read_text(encoding="utf-8")

    assert "from models.events" not in api_text
    assert "from models.specs" not in api_text
    assert "from services.task_execution_service import (" in execution_handler_text
    assert "from models.specs import " not in story_validation_text
