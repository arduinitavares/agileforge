# tests/adapters/test_provider_cold_start.py
"""Cold provider composition and failure handling without metadata HTTP."""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404
import sys
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

_ROOT: Path = Path(__file__).resolve().parents[2]
_PREAMBLE: str = """
import json
import os
import socket
import sys
from pathlib import Path

import httpx

root = Path(sys.argv[1])
metadata_requests = []

def blocked_get(url, *args, **kwargs):
    metadata_requests.append(str(url))
    raise RuntimeError("metadata HTTP is forbidden in this unit process")

def blocked_socket(*args, **kwargs):
    raise AssertionError("external socket is forbidden in this unit process")

httpx.get = blocked_get
socket.socket.connect = blocked_socket
socket.socket.connect_ex = blocked_socket
socket.create_connection = blocked_socket
"""


def _environment(root: Path) -> Mapping[str, str]:
    """Select only disposable application state without changing HOME."""
    environment = dict(os.environ)
    for key in (
        "AGILEFORGE_PRODUCTION_PROFILE",
        "LITELLM_LOCAL_MODEL_COST_MAP",
        "LITELLM_MODEL_COST_MAP_URL",
        "LITELLM_MODE",
    ):
        environment.pop(key, None)
    environment.update(
        AGILEFORGE_DB_URL=f"sqlite:///{root / 'business.sqlite3'}",
        AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL=f"sqlite:///{root / 'trace.sqlite3'}",
        MODEL_CONFIG_PATH=str(_ROOT / "config" / "models.yaml"),
        OPEN_ROUTER_API_KEY="fixture-not-secret",
        AGILEFORGE_LAUNCHER_CHILD="1",
    )
    return environment


def _run_cold(root: Path, body: str) -> dict[str, object]:
    """Run a unit/composition seam with HTTP denied before project imports."""
    completed = subprocess.run(  # noqa: S603  # nosec B603
        [sys.executable, "-c", _PREAMBLE + textwrap.dedent(body), str(root)],
        cwd=_ROOT,
        env=_environment(root),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    records = [
        line.removeprefix("COLD_RESULT ")
        for line in completed.stdout.splitlines()
        if line.startswith("COLD_RESULT ")
    ]
    assert len(records) == 1, (completed.stdout, completed.stderr)
    return json.loads(records[0])


def test_cli_import_and_model_construction_do_not_fetch_sdk_metadata(
    tmp_path: Path,
) -> None:
    """Catch eager SDK imports in control-plane or default client construction."""
    result = _run_cold(
        tmp_path,
        """
        import cli.main
        from adapters.adk.provider_models import create_openrouter_model
        from adapters.adk.provider_retry import (
            ProviderAttemptStopped,
            RetryingOpenRouterClient,
            classify_openrouter_failure,
        )

        model = create_openrouter_model(model_id="openrouter/synthetic/test-model")
        client = RetryingOpenRouterClient()
        try:
            client.completion(model="openrouter/test", messages=[], tools=[])
        except ProviderAttemptStopped:
            pass
        else:
            raise AssertionError("unsupported sync completion was admitted")
        assert classify_openrouter_failure(
            RuntimeError("unstructured"), model_id="openrouter/test"
        ) is None
        print("COLD_RESULT " + json.dumps({
            "model": model.model,
            "sdk_loaded": "litellm" in sys.modules,
            "metadata_requests": metadata_requests,
        }))
        """,
    )

    assert result == {
        "model": "openrouter/synthetic/test-model",
        "sdk_loaded": False,
        "metadata_requests": [],
    }


def test_cold_authentication_failure_is_durable_and_exits_without_metadata_http(
    tmp_path: Path,
) -> None:
    """Catch metadata initialization while the real application handles fake 401."""
    result = _run_cold(
        tmp_path,
        """
        from cli.main import _emit_result
        from tests.dev_runtime.test_agent_failure_shutdown import (
            _assert_durable_single_failure,
            _seed_registered_source,
        )
        from sqlmodel import SQLModel, create_engine

        logs = root / "logs"
        logs.mkdir()
        (logs / "issue-201-failure-mode").write_text("authentication")
        engine = create_engine(
            os.environ["AGILEFORGE_DB_URL"],
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(engine)
        project_id = _seed_registered_source(engine, root / "registered-source")
        from adapters.adk.agents import specification_author
        from tests.issue_201_launcher_model import Issue201LauncherModel
        specification_author.root_agent = specification_author.root_agent.model_copy(
            update={"model": Issue201LauncherModel(model="fake")}
        )
        from models import db
        db.get_engine = lambda: engine
        from services.application import (
            SpecificationStructuringRequest,
            production_application,
        )
        from utils import runtime_ownership
        runtime_ownership.runtime_roots = lambda: (root,)
        from utils.logging_config import configure_logging

        with runtime_ownership.runtime_access():
            configure_logging(console=False)
            result = production_application().structure_specification(
                SpecificationStructuringRequest(
                    project_id=project_id,
                    idempotency_key="cold-authentication",
                    actor="operator@example.invalid",
                    correlation_id="cold-authentication",
                )
            )
            assert result.error is not None
            code = result.error.code.value
            message = result.error.message
            _assert_durable_single_failure(
                engine,
                project_id=project_id,
                expected_code="SPECIFICATION_PRODUCER_FAILED",
                expected_message="Specification structurer provider execution failed.",
            )
            exit_code = _emit_result(result)
        engine.dispose()
        print("COLD_RESULT " + json.dumps({
            "code": code,
            "message": message,
            "exit_code": exit_code,
            "provider_calls": len(
                (logs / "issue-201-provider-calls").read_text().splitlines()
            ),
            "sdk_loaded": "litellm" in sys.modules,
            "metadata_requests": metadata_requests,
        }))
        """,
    )

    assert result == {
        "code": "SPECIFICATION_PRODUCER_FAILED",
        "message": "Specification structurer provider execution failed.",
        "exit_code": 1,
        "provider_calls": 1,
        "sdk_loaded": False,
        "metadata_requests": [],
    }


def test_bound_model_call_initializes_sdk_and_enforces_its_controls(
    tmp_path: Path,
) -> None:
    """Catch skipped ADK initialization, debug suppression or global control checks."""
    result = _run_cold(
        tmp_path,
        """
        import asyncio
        import time
        from google.adk.models import lite_llm
        from google.adk.models.llm_request import LlmRequest
        from google.genai import types
        from adapters.adk.provider_models import create_openrouter_model
        from adapters.adk.provider_retry import (
            ProviderActionContext,
            ProviderAttemptStopped,
            RetryingOpenRouterClient,
            bind_provider_action,
        )
        from services.contracts.provider_retry import ProviderRetryConfig

        audit_records = []
        class Audit:
            def append_started(self, record):
                audit_records.append(record)
            def append_finished(self, record):
                audit_records.append(record)

        sends = []
        async def completion(**kwargs):
            sends.append(kwargs)
            sdk = sys.modules["litellm"]
            assert sdk.suppress_debug_info is True
            assert sdk.add_function_to_prompt is True
            assert lite_llm.litellm is sdk
            assert os.environ["LITELLM_MODE"] == "PRODUCTION"
            return sdk.ModelResponse(
                id="cold-bound-model",
                model="synthetic/test-model",
                choices=[{
                    "index": 0,
                    "message": {"role": "assistant", "content": "safe"},
                    "finish_reason": "stop",
                }],
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )

        model = create_openrouter_model(model_id="openrouter/synthetic/test-model")
        model.llm_client = RetryingOpenRouterClient(completion=completion)
        sdk_loaded_before_call = "litellm" in sys.modules
        context = ProviderActionContext(
            project_id=1,
            action_id="cold-bound-model",
            policy=ProviderRetryConfig(max_attempts=1),
            audit=Audit(),
            action_deadline=time.monotonic() + 30,
            pre_try_check=lambda: None,
        )
        request = LlmRequest(contents=[types.Content(
            role="user", parts=[types.Part.from_text(text="safe")]
        )])
        async def run():
            with bind_provider_action(context):
                responses = [
                    response async for response in model.generate_content_async(request)
                ]
                sys.modules["litellm"].num_retries = 2
                try:
                    [
                        response async for response
                        in model.generate_content_async(request)
                    ]
                except ProviderAttemptStopped:
                    pass
                else:
                    raise AssertionError("implicit global retries were admitted")
            return responses
        responses = asyncio.run(run())
        print("COLD_RESULT " + json.dumps({
            "sdk_loaded_before_call": sdk_loaded_before_call,
            "sdk_loaded_after_call": "litellm" in sys.modules,
            "text": responses[0].content.parts[0].text,
            "sends": len(sends),
            "audit_dispositions": [record.disposition for record in audit_records],
        }))
        """,
    )

    assert result == {
        "sdk_loaded_before_call": False,
        "sdk_loaded_after_call": True,
        "text": "safe",
        "sends": 1,
        "audit_dispositions": [None, "success"],
    }
