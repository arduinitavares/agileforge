"""Real launcher shutdown after durable terminal agent failure."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess  # nosec B404
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from git import Repo
from sqlmodel import Session, col, create_engine, select

from adapters.git.repository_probe import GitPythonRepositoryProbe
from cli.dev_profiles import profile_paths
from models.core import Project
from models.product_definition import SpecificationCandidate
from models.repository import RepositoryBinding
from models.workflow import WorkflowNodeAttempt, WorkflowNodeAttemptOutcome
from services.specification_source_registration import (
    SpecificationSourceRegistrationRequest,
    SpecificationSourceRegistrationService,
)
from tests.workflow.lifecycle_fixtures import _seed_accepted_vision_and_goal
from workflow.clock import FixedClock
from workflow.definitions.root import ROOT_GRAPH
from workflow.domain import WorkflowDomain
from workflow.fingerprints import canonical_json
from workflow.requests import RegisterSpecificationSource

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from sqlalchemy.engine import Engine

_SOURCE_ROOT: Path = Path(__file__).parents[2]
_NOW: datetime = datetime(2026, 8, 13, 12, tzinfo=UTC)
_SHUTDOWN_TIMEOUT_SECONDS: float = 8.0
_PROCESS_STOP_TIMEOUT_SECONDS: float = 3.0
_KILL_PROCESS_GROUP: Callable[[int, int], None] = cast(
    "Callable[[int, int], None]",
    getattr(os, "killpg", None),
)
_SIGNAL_KILL: int = int(getattr(signal, "SIGKILL", 9))
pytestmark = pytest.mark.skipif(
    not hasattr(os, "killpg"),
    reason="requires Unix process groups",
)
_ISSUE_200_INCOMPLETE_MESSAGE: str = (
    "Specification structurer returned incomplete output. Increase "
    "SPECIFICATION_STRUCTURER_MAX_TOKENS or select a provider that can return "
    "the complete structured payload, then retry Structure Specification."
)
_MODEL_IMPORT: str = "from google.adk.models.lite_llm import LiteLlm\n"
_MODEL_DEFINITION: str = """model: LiteLlm = LiteLlm(
    model=_model_id,
    api_key=get_openrouter_api_key(),
    drop_params=True,
    extra_body=get_openrouter_extra_body(),
)
"""


@dataclass(frozen=True, slots=True)
class _FailureExpectation:
    mode: str
    code: str
    message: str


def _sanitized_launcher_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for key in (
        "AGILEFORGE_DB_URL",
        "AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL",
        "MODEL_CONFIG_PATH",
        "OPEN_ROUTER_API_KEY",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONUSERBASE",
        "UV_PROJECT",
        "UV_PROJECT_ENVIRONMENT",
        "VIRTUAL_ENV",
    ):
        environment.pop(key, None)
    environment.update({"UV_OFFLINE": "1", "UV_NO_PROGRESS": "1"})
    return environment


def _run(
    arguments: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    timeout: float = 180,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(  # noqa: S603  # nosec B603
        arguments,
        cwd=cwd,
        env=None if env is None else dict(env),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert completed.returncode == 0, (
        f"command failed: {arguments!r}\nstdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
    return completed


def _git(checkout: Path, *arguments: str) -> str:
    return _run(("git", "-C", str(checkout), *arguments), cwd=checkout).stdout.strip()


def _install_provider_free_model(checkout: Path) -> None:
    agent_path = checkout / "adapters" / "adk" / "agents" / "specification_author.py"
    source = agent_path.read_text(encoding="utf-8")
    assert source.count(_MODEL_IMPORT) == 1
    assert source.count(_MODEL_DEFINITION) == 1
    source = source.replace(
        _MODEL_IMPORT,
        "from tests.issue_201_launcher_model import Issue201LauncherModel\n",
    ).replace(
        _MODEL_DEFINITION,
        "model: Issue201LauncherModel = Issue201LauncherModel(model=_model_id)\n",
    )
    agent_path.write_text(source, encoding="utf-8")


@pytest.fixture(scope="module")
def launcher_checkout(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Commit one disposable checkout whose only test double is the model leaf."""
    root = tmp_path_factory.mktemp("issue-201-launcher")
    checkout = root / "checkout"
    _run(
        ("git", "clone", "--no-hardlinks", str(_SOURCE_ROOT), str(checkout)),
        cwd=root,
    )
    _git(checkout, "config", "user.name", "Issue 201 Launcher Test")
    _git(checkout, "config", "user.email", "issue-201@example.invalid")
    shutil.copy2(
        _SOURCE_ROOT / "adapters" / "adk" / "runner.py",
        checkout / "adapters" / "adk" / "runner.py",
    )
    shutil.copy2(
        _SOURCE_ROOT / "tests" / "issue_201_launcher_model.py",
        checkout / "tests" / "issue_201_launcher_model.py",
    )
    _install_provider_free_model(checkout)
    _git(
        checkout,
        "add",
        "adapters/adk/agents/specification_author.py",
        "adapters/adk/runner.py",
        "tests/issue_201_launcher_model.py",
    )
    _git(checkout, "commit", "-m", "test: install issue 201 provider-free leaf")
    return checkout


def _initialize_profile(checkout: Path, profile_name: str) -> None:
    completed = _run(
        (
            str(checkout / "agileforge-dev"),
            "init",
            "--profile",
            profile_name,
            "--json",
        ),
        cwd=checkout,
        env=_sanitized_launcher_environment(),
    )
    payload = json.loads(completed.stdout)
    assert payload["profile"]["name"] == profile_name


def _seed_registered_source(
    engine: Engine,
    repository: Path,
) -> int:
    repository.mkdir()
    (repository / "SPECIFICATION.md").write_text(
        "# Issue 201\n\nThe product MUST stop after a terminal agent failure.\n",
        encoding="utf-8",
    )
    with Repo.init(repository) as repo:
        with repo.config_writer() as config:
            config.set_value("user", "name", "Issue 201 Launcher Test")
            config.set_value("user", "email", "issue-201@example.invalid")
        repo.index.add(["SPECIFICATION.md"])
        repo.index.commit("register source")
    probe = GitPythonRepositoryProbe()
    observed = probe.inspect(repository)
    with Session(engine) as session:
        project = Project(name="Issue 201 launcher shutdown")
        session.add(project)
        session.flush()
        assert project.project_id is not None
        project_id = project.project_id
        _seed_accepted_vision_and_goal(
            session,
            project_id=project_id,
            recorded_at=_NOW - timedelta(minutes=1),
        )
        binding = RepositoryBinding(
            project_id=project_id,
            worktree_path=observed.worktree_path,
            common_git_dir=observed.common_git_dir,
            head_sha=observed.head_sha,
            branch_name=observed.branch_name,
            detached_head=observed.detached_head,
            dirty=observed.dirty,
            status_fingerprint=observed.status_fingerprint,
            status_entries_json=canonical_json(
                [item.model_dump(mode="json") for item in observed.status_entries]
            ),
            remotes_json=canonical_json(list(observed.remotes)),
            warnings_json=canonical_json(
                [item.model_dump(mode="json") for item in observed.warnings]
            ),
            probe_version=observed.probe_version,
            inspected_at=_NOW - timedelta(seconds=30),
            recorded_by="operator@example.invalid",
        )
        session.add(binding)
        session.flush()
        assert binding.repository_binding_id is not None
        project.active_repository_binding_id = binding.repository_binding_id
        session.add(project)
        session.commit()
    domain = WorkflowDomain(
        engine=engine,
        graph=ROOT_GRAPH,
        clock=FixedClock(now_value=_NOW),
        specification_registration_check=lambda _prepared: None,
    )
    prepared = SpecificationSourceRegistrationService(
        engine=engine,
        repository_probe=probe,
    ).prepare(
        SpecificationSourceRegistrationRequest(
            project_id=project_id,
            source_path="SPECIFICATION.md",
            preparation_capability="grill-with-docs",
            idempotency_key="issue-201-register-source",
            actor="operator@example.invalid",
        )
    )
    position = domain.position(project_id)
    decision = next(
        item
        for item in position.decisions
        if item.node_id == "specification.source.register"
    )
    registered = domain.transition(
        RegisterSpecificationSource(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=decision.decision_fingerprint,
            idempotency_key="issue-201-register-source",
            actor="operator@example.invalid",
            accepted_vision_artifact_id=prepared.accepted_vision_artifact_id,
            accepted_product_goal_artifact_id=(
                prepared.accepted_product_goal_artifact_id
            ),
            repository_binding_id=prepared.repository_binding_id,
            repository_binding_fingerprint=prepared.repository_binding_fingerprint,
            capture_request_fingerprint=prepared.request_fingerprint,
            source_fingerprint=prepared.source_fingerprint,
            bundle=prepared.bundle,
        )
    )
    assert registered.ok is True
    return project_id


def _stop_process_group(process: subprocess.Popen[str]) -> tuple[str, str]:
    with suppress(ProcessLookupError):
        _KILL_PROCESS_GROUP(process.pid, signal.SIGTERM)
    try:
        return process.communicate(timeout=_PROCESS_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            _KILL_PROCESS_GROUP(process.pid, _SIGNAL_KILL)
        return process.communicate(timeout=_PROCESS_STOP_TIMEOUT_SECONDS)


def _pid_exists(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    return True


def _process_group_exists(process_group_id: int) -> bool:
    try:
        _KILL_PROCESS_GROUP(process_group_id, 0)
    except ProcessLookupError:
        return False
    return True


def _assert_durable_single_failure(
    engine: Engine,
    *,
    project_id: int,
    expected_code: str,
    expected_message: str,
) -> None:
    with Session(engine) as session:
        attempts = session.exec(
            select(WorkflowNodeAttempt).where(
                col(WorkflowNodeAttempt.project_id) == project_id,
                col(WorkflowNodeAttempt.node_id) == "specification.structure",
            )
        ).all()
        assert len(attempts) == 1
        attempt_id = attempts[0].workflow_node_attempt_id
        assert attempt_id is not None
        outcomes = session.exec(
            select(WorkflowNodeAttemptOutcome).where(
                col(WorkflowNodeAttemptOutcome.project_id) == project_id,
                col(WorkflowNodeAttemptOutcome.workflow_node_attempt_id) == attempt_id,
            )
        ).all()
        candidates = session.exec(
            select(SpecificationCandidate).where(
                col(SpecificationCandidate.project_id) == project_id
            )
        ).all()
    assert len(outcomes) == 1
    assert outcomes[0].status == "failure"
    assert outcomes[0].failure_code == expected_code
    assert outcomes[0].failure_message == expected_message
    assert candidates == []


def _run_structure(
    checkout: Path,
    *,
    profile_name: str,
    project_id: int,
) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603  # nosec B603
        (
            str(checkout / "agileforge-dev"),
            "cli",
            "--profile",
            profile_name,
            "--json",
            "--",
            "specification",
            "structure",
            "--project-id",
            str(project_id),
            "--idempotency-key",
            f"issue-201-{profile_name}",
            "--actor",
            "operator@example.invalid",
            "--correlation-id",
            f"issue-201-{profile_name}",
        ),
        cwd=checkout,
        env=_sanitized_launcher_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def _communicate_initial_failure(
    process: subprocess.Popen[str],
    *,
    engine: Engine,
    paths_root: Path,
    project_id: int,
    expected: _FailureExpectation,
) -> tuple[str, str, int]:
    try:
        stdout, stderr = process.communicate(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        call_pid: int | None = None
        try:
            _assert_durable_single_failure(
                engine,
                project_id=project_id,
                expected_code=expected.code,
                expected_message=expected.message,
            )
            call_pids = [
                int(value)
                for value in (paths_root / "logs" / "issue-201-provider-calls")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            assert len(call_pids) == 1
            call_pid = call_pids[0]
            assert _pid_exists(process.pid)
            assert _pid_exists(call_pid)
        finally:
            _stop_process_group(process)
        pytest.fail(
            "durable terminal failure was committed, but the checkout-local "
            f"launcher PID {process.pid} and product CLI PID {call_pid} remained alive",
        )
        raise AssertionError from error
    call_pids = [
        int(value)
        for value in (paths_root / "logs" / "issue-201-provider-calls")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(call_pids) == 1
    return stdout, stderr, call_pids[0]


@pytest.mark.parametrize(
    "expected",
    [
        pytest.param(
            _FailureExpectation(
                mode="authentication",
                code="SPECIFICATION_PRODUCER_FAILED",
                message="Specification structurer provider execution failed.",
            ),
            id="provider-authentication",
        ),
        pytest.param(
            _FailureExpectation(
                mode="incomplete",
                code="SPECIFICATION_OUTPUT_INCOMPLETE",
                message=_ISSUE_200_INCOMPLETE_MESSAGE,
            ),
            id="incomplete-structured-output",
        ),
    ],
)
def test_launcher_exits_after_durable_specification_failure(
    launcher_checkout: Path,
    tmp_path: Path,
    expected: _FailureExpectation,
) -> None:
    """Return one failure envelope and leave no model child or partial candidate."""
    profile_name = f"issue-201-{expected.mode}"
    _initialize_profile(launcher_checkout, profile_name)
    paths = profile_paths(launcher_checkout, profile_name)
    engine = create_engine(f"sqlite:///{paths.business_database.as_posix()}")
    project_id = _seed_registered_source(engine, tmp_path / "registered-source")
    (paths.logs / "issue-201-failure-mode").write_text(
        expected.mode,
        encoding="utf-8",
    )
    process = _run_structure(
        launcher_checkout,
        profile_name=profile_name,
        project_id=project_id,
    )

    stdout, stderr, call_pid = _communicate_initial_failure(
        process,
        engine=engine,
        paths_root=paths.root,
        project_id=project_id,
        expected=expected,
    )

    assert process.returncode == 1
    assert stderr == ""
    decoder = json.JSONDecoder()
    decoded, end = decoder.raw_decode(stdout)
    assert stdout[end:].strip() == ""
    assert stdout.endswith("\n")
    envelope = cast("dict[str, object]", decoded)
    assert envelope["exit_code"] == 1
    result = cast("dict[str, object]", envelope["result"])
    error_payload = cast("dict[str, object]", result["error"])
    assert result["ok"] is False
    assert error_payload == {
        "code": expected.code,
        "message": expected.message,
        "blockers": [],
    }
    assert not _pid_exists(call_pid)
    assert not _process_group_exists(process.pid)
    _assert_durable_single_failure(
        engine,
        project_id=project_id,
        expected_code=expected.code,
        expected_message=expected.message,
    )

    replay = _run_structure(
        launcher_checkout,
        profile_name=profile_name,
        project_id=project_id,
    )
    try:
        replay_stdout, replay_stderr = replay.communicate(
            timeout=_SHUTDOWN_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        _stop_process_group(replay)
        raise
    replay_envelope = json.loads(replay_stdout)
    replay_result = replay_envelope["result"]
    assert replay.returncode == 1
    assert replay_stderr == ""
    assert replay_result["replayed"] is True
    assert replay_result["error"] == error_payload
    assert not _process_group_exists(replay.pid)
    assert (paths.logs / "issue-201-provider-calls").read_text(
        encoding="utf-8"
    ).splitlines() == [str(call_pid)]
    _assert_durable_single_failure(
        engine,
        project_id=project_id,
        expected_code=expected.code,
        expected_message=expected.message,
    )
