# ruff: noqa: PLR2004
# Expected protocol statuses, send counts and synthetic settings are literal assertions.
"""Provider-free acceptance of the host-bound, audited provider boundary."""

from __future__ import annotations

import ast
import asyncio
import importlib
import json
import tomllib
import traceback
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from litellm.exceptions import APIError, Timeout

from services.contracts.provider_retry import (
    ProviderAuditError,
    ProviderFailureSummary,
    ProviderRetryConfig,
    ProviderTransientFailure,
    ProviderTryAuditRecord,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable
    from contextvars import Context
    from types import ModuleType

    from google.adk.agents import BaseAgent
    from litellm import ModelResponse

    from adapters.adk.provider_retry import (
        ProviderActionContext,
        RetryingOpenRouterClient,
    )


def boundary() -> ModuleType:
    """Boundary."""
    module = importlib.import_module("adapters.adk.provider_retry")
    assert hasattr(module, "RetryingOpenRouterClient"), "host-bound client missing"
    return module


class Clock:
    """Provider-free clock fixture."""

    def __init__(self) -> None:
        """Capture this instance's injected deterministic seams."""
        self.value = 100.0
        self.waits: list[float] = []
        self.oversleep = 0.0

    def monotonic(self) -> float:
        """Monotonic."""
        return self.value

    def utc_now(self) -> datetime:
        """Utc now."""
        return datetime(2026, 10, 7, tzinfo=UTC) + timedelta(seconds=self.value)

    async def sleep(self, seconds: float) -> None:
        """Sleep."""
        self.waits.append(seconds)
        self.value += seconds + self.oversleep
        await asyncio.sleep(0)


class BackoffClock(Clock):
    """Hold the retry wait until its public completion task is cancelled."""

    def __init__(self) -> None:
        """Expose wait readiness and the exact cancellation raised by the wait."""
        super().__init__()
        self.backoff_started: asyncio.Event = asyncio.Event()
        self.release_backoff: asyncio.Event = asyncio.Event()
        self.cancellation: asyncio.CancelledError | None = None

    async def sleep(self, seconds: float) -> None:
        """Wait on a controllable event without advancing the fake clock."""
        self.waits.append(seconds)
        self.backoff_started.set()
        try:
            await self.release_backoff.wait()
        except asyncio.CancelledError as cancellation:
            self.cancellation = cancellation
            raise


class Audit:
    """Provider-free audit fixture."""

    def __init__(self, clock: Clock) -> None:
        """Capture this instance's injected deterministic seams."""
        self.clock = clock
        self.starts: list[ProviderTryAuditRecord] = []
        self.finishes: list[ProviderTryAuditRecord] = []
        self.fail_start = False
        self.fail_finish = False
        self.start_advance = 0.0
        self.checked: list[ProviderFailureSummary] = []

    def append_started(self, record: ProviderTryAuditRecord) -> None:
        """Append started."""
        if self.fail_start:
            raise ProviderAuditError()
        self.starts.append(record)
        if record.try_ordinal >= 2:
            self.clock.value += self.start_advance

    def append_finished(self, record: ProviderTryAuditRecord) -> None:
        """Append finished."""
        if self.fail_finish:
            raise ProviderAuditError()
        self.finishes.append(record)

    def terminal_failure(
        self,
        *,
        project_id: int,
        action_id: str,
        call_id: str,
        expected_summary: ProviderFailureSummary | None = None,
    ) -> ProviderFailureSummary | None:
        """Terminal failure."""
        assert (project_id, action_id, call_id) == (
            self.starts[0].project_id,
            self.starts[0].action_id,
            self.starts[0].call_id,
        )
        assert expected_summary is not None
        self.checked.append(expected_summary)
        return expected_summary


def error(status: int, retry_after: str | None = None) -> APIError:
    """Error."""
    result = APIError(status, "synthetic response", "openrouter", "test-model")
    if retry_after is not None:
        setattr(result, "litellm_response_headers", {"Retry-After": retry_after})  # noqa: B010
    return result


def context(clock: Clock, audit: Audit, **overrides: object) -> ProviderActionContext:
    """Context."""
    values: dict[str, object] = {
        "project_id": 1,
        "action_id": "action-one",
        "policy": ProviderRetryConfig(),
        "audit": audit,
        "action_deadline": clock.value + 120.0,
        "pre_try_check": lambda: None,
    }
    values.update(overrides)
    return boundary().ProviderActionContext(**values)


async def invoke(client: RetryingOpenRouterClient) -> ModelResponse:
    """Invoke."""
    return await client.acompletion(
        model="openrouter/test-model",
        messages=[{"role": "user", "content": "safe"}],
        tools=[],
        temperature=0.2,
        max_tokens=128,
        extra_body={"provider": {"allow_fallbacks": True, "order": ["same-route"]}},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("statuses", [[500], [429, 503]])
async def test_retry_success_preserves_semantics_and_audits_each_send(
    statuses: list[int],
) -> None:
    """Verify retry success preserves semantics and audits each send."""
    clock = Clock()
    audit = Audit(clock)
    sends: list[dict[str, object]] = []
    outcomes: list[object] = [*(error(status) for status in statuses), object()]

    async def send(**kwargs: object) -> object:
        """Send."""
        assert len(audit.starts) == len(sends) + 1
        assert len(audit.finishes) == len(sends)
        sends.append(kwargs)
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    module = boundary()
    client = module.RetryingOpenRouterClient(
        completion=send, clock=clock, uniform=lambda _low, high: high
    )
    with module.bind_provider_action(context(clock, audit)):
        assert await invoke(client) is not None
    assert len(sends) == len(statuses) + 1
    assert clock.waits == [1.0, 2.0][: len(statuses)]
    assert len({record.call_id for record in audit.starts}) == 1
    assert len({record.request_fingerprint for record in audit.starts}) == 1

    assert [record.disposition for record in audit.finishes] == [
        *("retry_scheduled" for _ in statuses),
        "success",
    ]
    for sent in sends:
        assert sent["num_retries"] == sent["max_retries"] == 0
        assert sent["temperature"] == 0.2
        assert sent["max_tokens"] == 128
        assert (
            cast("dict[str, dict[str, bool]]", sent["extra_body"])["provider"][
                "allow_fallbacks"
            ]
            is True
        )
    with pytest.raises(module.ProviderAttemptStopped):
        await invoke(client)
    assert len(sends) == len(statuses) + 1


@pytest.mark.asyncio
async def test_exhaustion_has_actual_send_count_and_same_call_summary() -> None:
    """Verify exhaustion has actual send count and same call summary."""
    clock, sends = Clock(), []
    audit = Audit(clock)

    async def send(**kwargs: object) -> object:
        """Send."""
        sends.append(kwargs)
        raise error(503, "0")

    module = boundary()
    client = module.RetryingOpenRouterClient(
        completion=send, clock=clock, uniform=lambda _low, _high: 0.0
    )
    with (
        module.bind_provider_action(context(clock, audit)),
        pytest.raises(ProviderTransientFailure) as caught,
    ):
        await invoke(client)
    summary = caught.value.summary
    assert summary.attempts == len(sends) == 3
    assert summary.http_status == 503
    assert summary.termination_reason == "attempts_exhausted"
    assert summary.retry_after_seconds == 0.0
    assert audit.checked == [summary]
    assert audit.finishes[-1].disposition == "exhausted"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 422, 408])
async def test_permanent_retry_response_keeps_original_error(status: int) -> None:
    """Verify permanent retry response keeps original error."""
    clock = Clock()
    audit = Audit(clock)
    outcomes = [error(429), error(status)]

    async def send(**kwargs: object) -> object:
        """Send."""
        del kwargs
        raise outcomes.pop(0)

    module = boundary()
    client = module.RetryingOpenRouterClient(
        completion=send, clock=clock, uniform=lambda _low, _high: 0.0
    )
    with (
        module.bind_provider_action(context(clock, audit)),
        pytest.raises(APIError) as caught,
    ):
        await invoke(client)
    assert caught.value.status_code == status
    assert audit.finishes[-1].disposition == "non_retryable"
    assert len(audit.starts) == 2
    assert not audit.checked


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", [True, False])
async def test_provider_timeout_408_remains_nonretryable(initial: bool) -> None:
    """Verify provider timeout 408 remains nonretryable."""
    clock = Clock()
    audit = Audit(clock)
    timeout = Timeout("provider timeout", "openrouter", "test-model")
    outcomes = [timeout] if initial else [error(500), timeout]

    async def send(**kwargs: object) -> object:
        """Send."""
        del kwargs
        raise outcomes.pop(0)

    module = boundary()
    with module.bind_provider_action(context(clock, audit)), pytest.raises(Timeout):
        await invoke(
            module.RetryingOpenRouterClient(
                completion=send, clock=clock, uniform=lambda _low, _high: 0.0
            )
        )
    assert len(audit.starts) == (1 if initial else 2)
    assert audit.finishes[-1].disposition == "non_retryable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("guidance", "reason", "waits"),
    [
        ("4", None, [4.0]),
        ("121", "retry_after_exceeds_budget", []),
        ("60", "retry_after_exceeds_budget", []),
    ],
)
async def test_retry_after_is_a_minimum_or_refuses_send(
    guidance: str, reason: str | None, waits: list[float]
) -> None:
    """Verify retry after is a minimum or refuses send."""
    clock = Clock()
    audit = Audit(clock)
    times: list[float] = []

    async def send(**kwargs: object) -> object:
        """Send."""
        del kwargs
        times.append(clock.monotonic())
        if len(times) == 1:
            raise error(429, guidance)
        return object()

    module = boundary()
    with module.bind_provider_action(context(clock, audit)):
        client = module.RetryingOpenRouterClient(
            completion=send, clock=clock, uniform=lambda _low, _high: 0.0
        )
        if reason:
            with pytest.raises(ProviderTransientFailure) as caught:
                await invoke(client)
            assert caught.value.summary.termination_reason == reason
            assert len(times) == 1
        else:
            await invoke(client)
            assert times == [100.0, 104.0]
    assert clock.waits == waits


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["oversleep", "start_write"])
async def test_budget_stops_do_not_fabricate_an_actual_send(phase: str) -> None:
    """Verify budget stops do not fabricate an actual send."""
    clock = Clock()
    audit = Audit(clock)
    clock.oversleep = 70.0 if phase == "oversleep" else 0.0
    audit.start_advance = 70.0 if phase == "start_write" else 0.0
    sends = 0

    async def send(**kwargs: object) -> object:
        """Send."""
        del kwargs
        nonlocal sends
        sends += 1
        raise error(503, "4")

    module = boundary()
    with (
        module.bind_provider_action(context(clock, audit)),
        pytest.raises(ProviderTransientFailure) as caught,
    ):
        await invoke(
            module.RetryingOpenRouterClient(
                completion=send, clock=clock, uniform=lambda _low, _high: 0.0
            )
        )
    assert sends == caught.value.summary.attempts == 1
    assert caught.value.summary.retry_after_seconds == 4.0
    assert caught.value.summary.termination_reason == "retry_budget_exhausted"
    assert audit.checked == [caught.value.summary]
    assert len(audit.starts) == (1 if phase == "oversleep" else 2)
    if phase == "start_write":
        assert audit.finishes[-1].disposition == "not_sent"
        assert audit.finishes[-1].http_status is None


@pytest.mark.asyncio
async def test_finish_audit_cost_stops_before_sleep_or_next_retry_start() -> None:
    """Audit commit time cannot leave a scheduled delay outside the retry budget."""
    clock = Clock()

    class SlowFinishAudit(Audit):
        """Consume virtual time only after the eligible finish has been recorded."""

        def append_finished(self, record: ProviderTryAuditRecord) -> None:
            super().append_finished(record)
            self.clock.value += 57.0

    audit = SlowFinishAudit(clock)
    sends = 0

    async def send(**kwargs: object) -> object:
        del kwargs
        nonlocal sends
        sends += 1
        raise error(503, "4")

    module = boundary()
    with (
        module.bind_provider_action(context(clock, audit)),
        pytest.raises(ProviderTransientFailure) as caught,
    ):
        await invoke(
            module.RetryingOpenRouterClient(
                completion=send, clock=clock, uniform=lambda _low, _high: 0.0
            )
        )
    assert sends == caught.value.summary.attempts == 1
    assert caught.value.summary.http_status == 503
    assert caught.value.summary.retry_after_seconds == 4.0
    assert caught.value.summary.termination_reason == "retry_budget_exhausted"
    assert not clock.waits
    assert len(audit.starts) == len(audit.finishes) == 1
    assert audit.finishes[0].disposition == "retry_scheduled"
    assert audit.finishes[0].selected_delay_seconds == 4.0
    assert audit.checked == [caught.value.summary]


@pytest.mark.asyncio
@pytest.mark.parametrize("minimum", [1.0, 2.0])
async def test_configured_positive_minimum_reserves_retry_execution_time(
    minimum: float,
) -> None:
    """The same positive remaining budget fits the default, but not a higher minimum."""
    clock = Clock()
    audit = Audit(clock)
    sends = 0

    async def send(**kwargs: object) -> object:
        del kwargs
        nonlocal sends
        sends += 1
        if sends == 1:
            raise error(500)
        return object()

    module = boundary()
    client = module.RetryingOpenRouterClient(
        completion=send, clock=clock, uniform=lambda _low, _high: 0.0
    )
    with module.bind_provider_action(
        context(
            clock,
            audit,
            policy=ProviderRetryConfig(min_remaining_seconds=minimum),
            action_deadline=clock.value + 3.5,
        )
    ):
        if minimum == 1.0:
            assert await invoke(client) is not None
            assert sends == len(audit.starts) == len(audit.finishes) == 2
            assert clock.waits == [0.0]
            assert audit.finishes[-1].disposition == "success"
            assert not audit.checked
        else:
            with pytest.raises(ProviderTransientFailure) as caught:
                await invoke(client)
            assert sends == caught.value.summary.attempts == 1
            assert caught.value.summary.http_status == 500
            assert caught.value.summary.termination_reason == "retry_budget_exhausted"
            assert not clock.waits
            assert len(audit.starts) == len(audit.finishes) == 1
            assert audit.finishes[0].disposition == "exhausted"
            assert audit.checked == [caught.value.summary]


@pytest.mark.asyncio
async def test_owned_retry_cap_cancels_slow_transport_and_preserves_500() -> None:
    """Verify owned retry cap cancels slow transport and preserves 500."""
    clock = Clock()
    audit = Audit(clock)
    sends = 0
    cleaned = False

    async def send(**kwargs: object) -> object:
        """Send."""
        del kwargs
        nonlocal sends, cleaned
        sends += 1
        if sends == 1:
            raise error(500)
        try:
            await asyncio.Event().wait()
        finally:
            cleaned = True

    async def owned_deadline(awaitable: Awaitable[object], seconds: float) -> object:
        """Owned deadline."""
        if sends == 0:
            return await awaitable
        task = asyncio.create_task(awaitable)
        await asyncio.sleep(0)
        clock.value += seconds
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        raise boundary().ProviderDeadlineExceeded

    module = boundary()
    with (
        module.bind_provider_action(context(clock, audit)),
        pytest.raises(ProviderTransientFailure) as caught,
    ):
        await invoke(
            module.RetryingOpenRouterClient(
                completion=send,
                clock=clock,
                uniform=lambda _low, _high: 0.0,
                timeout_runner=owned_deadline,
            )
        )
    assert sends == 2
    assert cleaned
    assert caught.value.summary.http_status == 500
    assert caught.value.summary.termination_reason == "retry_budget_exhausted"
    assert audit.finishes[-1].http_status is None
    assert audit.finishes[-1].retry_classification is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["start", "finish", "stream", "sync", "guard", "expired"]
)
async def test_fail_closed_before_sends_or_publication(failure: str) -> None:
    """Verify fail closed before sends or publication."""
    clock = Clock()
    audit = Audit(clock)
    audit.fail_start = failure == "start"
    audit.fail_finish = failure == "finish"
    sends = 0
    module = boundary()

    def guard() -> None:
        """Guard."""
        if failure == "guard":
            raise module.ProviderAttemptStopped()

    async def send(**kwargs: object) -> object:
        """Send."""
        del kwargs
        nonlocal sends
        sends += 1
        return object()

    client = module.RetryingOpenRouterClient(completion=send, clock=clock)
    ctx = context(
        clock,
        audit,
        pre_try_check=guard,
        action_deadline=clock.value if failure == "expired" else clock.value + 120.0,
    )
    expected = (
        ProviderAuditError
        if failure in {"start", "finish"}
        else module.ProviderAttemptStopped
    )
    with module.bind_provider_action(ctx), pytest.raises(expected):  # noqa: PT012
        if failure == "sync":
            client.completion("openrouter/test-model", [], [])
        elif failure == "stream":
            await client.acompletion("openrouter/test-model", [], [], stream=True)
        else:
            await invoke(client)
    assert sends == (1 if failure == "finish" else 0)


@pytest.mark.asyncio
async def test_cancellation_finishes_audit_and_propagates() -> None:
    """Verify cancellation finishes audit and propagates."""
    clock = Clock()
    audit = Audit(clock)

    async def send(**kwargs: object) -> object:
        """Send."""
        del kwargs
        raise asyncio.CancelledError

    module = boundary()
    with (
        module.bind_provider_action(context(clock, audit)),
        pytest.raises(asyncio.CancelledError),
    ):
        await invoke(module.RetryingOpenRouterClient(completion=send, clock=clock))
    assert audit.finishes[-1].disposition == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["started", "finished"])
@pytest.mark.parametrize("private_failure", [False, True])
async def test_backoff_cancellation_survives_audit_failure(
    monkeypatch: pytest.MonkeyPatch, phase: str, *, private_failure: bool
) -> None:
    """An audit write failure retains cancellation with only a safe cause."""
    clock = BackoffClock()
    audit = Audit(clock)
    sends = 0

    async def send(**kwargs: object) -> object:
        """Return an eligible failure before the controllable backoff."""
        del kwargs
        nonlocal sends
        sends += 1
        raise error(500)

    audit_error: Exception = (
        RuntimeError("private synthetic audit failure")
        if private_failure
        else ProviderAuditError()
    )

    def refuse_write(record: ProviderTryAuditRecord) -> None:
        """Fail only the newly cancelled reservation's requested audit phase."""
        del record
        raise audit_error

    module = boundary()
    client = module.RetryingOpenRouterClient(
        completion=send, clock=clock, uniform=lambda _low, high: high
    )
    with module.bind_provider_action(context(clock, audit)):
        task = asyncio.create_task(invoke(client))
        await asyncio.wait_for(clock.backoff_started.wait(), timeout=1.0)
        scheduled = audit.finishes[0]
        monkeypatch.setattr(audit, f"append_{phase}", refuse_write)
        task.cancel("synthetic backoff cancellation")
        with pytest.raises(asyncio.CancelledError) as caught:
            await task

    assert caught.value is clock.cancellation
    assert isinstance(caught.value.__cause__, ProviderAuditError)
    assert str(caught.value.__cause__) == (
        "Provider attempt audit could not be recorded or verified."
    )
    assert caught.value.__cause__.__context__ is None
    assert "private synthetic audit failure" not in "".join(
        traceback.format_exception(caught.value)
    )
    assert sends == 1
    assert audit.finishes == [scheduled]
    assert scheduled.disposition == "retry_scheduled"
    assert len(audit.starts) == (1 if phase == "started" else 2)
    assert audit.checked == []


@pytest.mark.asyncio
async def test_concurrent_bindings_and_distinct_model_calls_are_isolated() -> None:
    """Verify concurrent contexts and distinct calls remain isolated."""
    clock = Clock()
    audits = [Audit(clock), Audit(clock)]

    async def send(**kwargs: object) -> object:
        """Send."""
        del kwargs
        await asyncio.sleep(0)
        return object()

    module = boundary()
    client = module.RetryingOpenRouterClient(completion=send, clock=clock)

    async def action(index: int) -> None:
        """Run one independently bound action."""
        with module.bind_provider_action(
            context(
                clock, audits[index], project_id=index + 1, action_id=f"action-{index}"
            )
        ):
            await invoke(client)
            await invoke(client)

    await asyncio.gather(action(0), action(1))
    ids = {record.call_id for audit in audits for record in audit.starts}
    assert len(ids) == 4
    for index, audit in enumerate(audits):
        assert {record.project_id for record in audit.starts} == {index + 1}
        assert {record.action_id for record in audit.starts} == {f"action-{index}"}


def violations(source: str, *, module_name: str | None = None) -> list[str]:  # noqa: C901, PLR0912
    """Resolve import/assignment aliases and reject construction and client bypasses."""
    tree = ast.parse(source)
    aliases: dict[str, str] = {}
    found: list[str] = []
    literal_targets: dict[str, set[str]] = {}
    safe_dynamic_names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.value, ast.Dict)
        ):
            values = node.value.values
            if all(
                isinstance(value, ast.Constant) and isinstance(value.value, str)
                for value in values
            ):
                literal_targets[node.target.id] = {
                    str(value.value)
                    for value in values
                    if isinstance(value, ast.Constant)
                }
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "get"
            and isinstance(node.value.func.value, ast.Name)
        ):
            targets = literal_targets.get(node.value.func.value.id)
            if targets and all(
                not target.startswith(("litellm", "google.adk.models"))
                for target in targets
            ):
                safe_dynamic_names.update(
                    target.id for target in node.targets if isinstance(target, ast.Name)
                )

    def name(node: ast.AST) -> str:
        """Name."""
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return f"{name(node.value)}.{node.attr}"
        return ""

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
                if node.module == "google.adk.models.lite_llm" and alias.name in {
                    "LiteLlm",
                    "LiteLLMClient",
                }:
                    found.append("raw SDK import")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "llm_client":
                    found.append("client replacement")
                if isinstance(target, ast.Name) and node.value is not None:
                    aliases[target.id] = name(node.value) or target.id
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = name(node.func)
        if called.endswith(".LiteLlm") or (
            called.startswith("litellm.")
            and called.endswith(("completion", "acompletion"))
        ):
            found.append("raw SDK call")
        if (
            called in {"__import__", "importlib.import_module"}
            and (
                not node.args
                or not isinstance(node.args[0], ast.Constant)
                or str(node.args[0].value).startswith(("litellm", "google.adk.models"))
            )
            and not (
                node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in safe_dynamic_names
            )
        ):
            first = node.args[0] if node.args else None
            if not (
                isinstance(first, ast.JoinedStr)
                and first.values
                and isinstance(first.values[0], ast.Constant)
                and str(first.values[0].value).startswith(
                    ("models.", "services.", "workflow.")
                )
            ):
                local_module = (
                    isinstance(first, ast.JoinedStr)
                    and first.values
                    and isinstance(first.values[0], ast.FormattedValue)
                    and isinstance(first.values[0].value, ast.Name)
                    and first.values[0].value.id == "__name__"
                    and module_name is not None
                    and module_name.split(".")[0] in {"models", "services", "workflow"}
                )
                if not local_module:
                    found.append("dynamic import")
        if any(keyword.arg == "llm_client" for keyword in node.keywords):
            found.append("client keyword")
        if called.endswith((".Agent", ".LlmAgent")) and any(
            keyword.arg == "model"
            and isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, str)
            for keyword in node.keywords
        ):
            found.append("string model")
        if called.endswith(".model_copy") and any(
            isinstance(child, ast.Constant) and child.value == "llm_client"
            for child in ast.walk(node)
        ):
            found.append("copied client")
    return found


@pytest.mark.parametrize(
    "source",
    [
        "from google.adk.models.lite_llm import LiteLlm as Raw\n"
        "x=Raw(model='openrouter/x')",
        "import litellm as sdk\nf=sdk.acompletion\nf(model='openrouter/x')",
        "from google.adk.agents import Agent as A\nx=A(model='openrouter/x')",
        "from google.adk.agents import LlmAgent\nx=LlmAgent(model='x')",
        "model.llm_client=client",
        "factory(model_id='x',llm_client=client)",
        "model.model_copy(update={'llm_client': client})",
        "import importlib as i\nsdk=i.import_module('litellm')",
        "sdk=__import__('litellm')",
    ],
)
def test_structural_checker_detects_synthetic_bypasses(source: str) -> None:
    """Verify structural checker detects synthetic bypasses."""
    assert violations(source)


def test_all_retained_agent_constructors_use_factory() -> None:
    """Verify all retained agent constructors use factory."""
    root = Path(__file__).parents[2] / "adapters" / "adk"
    for file_path in (root / "agents").glob("*.py"):
        assert not violations(file_path.read_text()), file_path.name


def test_default_factory_does_not_parse_retry_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify default factory does not parse retry environment."""
    monkeypatch.setenv("OPENROUTER_RETRY_MAX_ATTEMPTS", "invalid")
    try:
        module = importlib.import_module("adapters.adk.provider_models")
    except ModuleNotFoundError:
        raise AssertionError from None
    model = module.create_openrouter_model(
        model_id="openrouter/test-model", max_tokens=17
    )
    assert isinstance(model.llm_client, boundary().RetryingOpenRouterClient)
    assert model._additional_args["max_tokens"] == 17


def _fake_production_tree(
    tmp_path: Path,
    *,
    extra_packages: tuple[str, ...] = (),
    extra_modules: tuple[str, ...] = (),
) -> Path:
    """Build a disposable tree from the checkout's packaging declarations."""
    metadata = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())
    setuptools = metadata["tool"]["setuptools"]
    package_find = setuptools["packages"]["find"]
    includes = [*package_find["include"], *extra_packages]
    modules = [*setuptools["py-modules"], *extra_modules]
    root = tmp_path / "packaged"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        "\n".join(
            (
                "[tool.setuptools]",
                f"py-modules = {json.dumps(modules)}",
                "[tool.setuptools.packages.find]",
                f"where = {json.dumps(package_find['where'])}",
                f"include = {json.dumps(includes)}",
                f"exclude = {json.dumps(package_find['exclude'])}",
            )
        )
    )
    for location in package_find["where"]:
        for pattern in includes:
            package_root = root / location / pattern.split(".")[0]
            package_root.mkdir(parents=True, exist_ok=True)
            (package_root / "__init__.py").write_text('"""Clean packaged fixture."""\n')
    for module in modules:
        file_path = root.joinpath(*module.split(".")).with_suffix(".py")
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text('"""Clean module fixture."""\n')
    return root


def _scan_fake_production_tree(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Exercise the production gate itself with a disposable checkout root."""
    monkeypatch.setitem(
        globals(),
        "__file__",
        str(root / "tests/adapters/test_provider_boundary_routes.py"),
    )
    test_structural_provider_routes_have_no_production_bypass()


@pytest.mark.parametrize(
    "source",
    [
        "import litellm\nlitellm.acompletion(model='openrouter/x')",
        "from google.adk.agents import Agent\nagent=Agent(model='openrouter/x')",
        "model.llm_client=client",
    ],
    ids=["direct-sdk", "string-agent", "client-replacement"],
)
@pytest.mark.parametrize(
    "relative_path",
    [
        "tools/bypass.py",
        "config/bypass.py",
        "db/bypass.py",
        "frontend/bypass.py",
        "routers/bypass.py",
        "agile_sqlmodel.py",
        "services/bypass.py",
    ],
)
def test_packaged_production_gate_rejects_bypass_at_declared_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
    source: str,
) -> None:
    """All declared production locations enforce the same boundary rules."""
    root = _fake_production_tree(tmp_path)
    (root / relative_path).write_text(source)
    with pytest.raises(AssertionError) as caught:
        _scan_fake_production_tree(monkeypatch, root)
    assert relative_path in str(caught.value)


@pytest.mark.parametrize("declaration", ["package", "module"])
@pytest.mark.parametrize(
    "source",
    [
        "import litellm\nlitellm.acompletion(model='openrouter/x')",
        "from google.adk.agents import Agent\nagent=Agent(model='openrouter/x')",
        "model.llm_client=client",
    ],
    ids=["direct-sdk", "string-agent", "client-replacement"],
)
def test_packaged_production_gate_follows_new_declarations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    declaration: str,
    source: str,
) -> None:
    """New package/module declarations cannot drift outside the structural gate."""
    package = "future_provider_helpers"
    module = "future_provider_adapter"
    root = _fake_production_tree(
        tmp_path,
        extra_packages=(package,) if declaration == "package" else (),
        extra_modules=(module,) if declaration == "module" else (),
    )
    relative_path = (
        f"{package}/bypass.py" if declaration == "package" else f"{module}.py"
    )
    (root / relative_path).write_text(source)
    with pytest.raises(AssertionError) as caught:
        _scan_fake_production_tree(monkeypatch, root)
    assert relative_path in str(caught.value)


def test_packaged_production_gate_keeps_only_exact_owner_exemptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clean declared files and approved owners pass; another same-named file fails."""
    root = _fake_production_tree(tmp_path)
    bypass = "import litellm\nlitellm.acompletion(model='openrouter/x')"
    owners = root / "adapters/adk"
    owners.mkdir()
    for name in ("provider_retry.py", "provider_models.py", "provider_transport.py"):
        (owners / name).write_text(bypass)
    (root / "unpackaged.py").write_text(bypass)
    _scan_fake_production_tree(monkeypatch, root)
    offending_path = "tools/provider_retry.py"
    (root / offending_path).write_text(bypass)
    with pytest.raises(AssertionError) as caught:
        _scan_fake_production_tree(monkeypatch, root)
    assert offending_path in str(caught.value)


def _declared_production_python_paths(root: Path) -> set[Path]:
    """Read packaged modules and package include/exclude patterns from metadata."""
    metadata = tomllib.loads((root / "pyproject.toml").read_text())
    setuptools = metadata["tool"]["setuptools"]
    file_paths: set[Path] = set()
    for module in setuptools["py-modules"]:
        file_path = root.joinpath(*module.split(".")).with_suffix(".py")
        assert file_path.is_file(), file_path
        file_paths.add(file_path)
    package_find = setuptools["packages"]["find"]
    includes = package_find["include"]
    excludes = package_find["exclude"]
    for location in package_find["where"]:
        source_root = root / location
        assert source_root.is_dir(), source_root
        package_roots = {
            candidate
            for pattern in includes
            for candidate in source_root.glob(pattern.split(".")[0])
            if candidate.is_dir()
        }
        for package_root in package_roots:
            for file_path in package_root.rglob("*.py"):
                package_name = ".".join(file_path.parent.relative_to(source_root).parts)
                if any(
                    fnmatchcase(package_name, pattern) for pattern in includes
                ) and not any(
                    fnmatchcase(package_name, pattern) for pattern in excludes
                ):
                    file_paths.add(file_path)
    return file_paths


def test_structural_provider_routes_have_no_production_bypass() -> None:
    """Verify every declared production package/module has no provider bypass."""
    root = Path(__file__).parents[2]
    allowed = {
        Path("adapters/adk/provider_retry.py"),
        Path("adapters/adk/provider_models.py"),
        Path("adapters/adk/provider_transport.py"),
    }
    for file_path in sorted(_declared_production_python_paths(root)):
        relative_path = file_path.relative_to(root)
        if relative_path in allowed:
            continue
        assert not violations(
            file_path.read_text(),
            module_name=".".join(relative_path.with_suffix("").parts),
        ), str(relative_path)


def test_catalog_roles_repair_builders_and_registered_recipes_use_boundary() -> None:
    """Verify catalog roles repair builders and registered recipes use boundary."""
    from adapters.adk.model_roles import (  # noqa: PLC0415
        AGENTIC_MODEL_ROLES,
        RETAINED_MODEL_ROLES,
    )
    from adapters.adk.recipes import (  # noqa: PLC0415
        AgenticRecipeNodes,
        build_agentic_recipe_registry,
    )
    from utils.model_config import get_model_id  # noqa: PLC0415
    from workflow.definitions.root import ROOT_GRAPH  # noqa: PLC0415

    modules = {
        "product_vision": "vision",
        "product_goal": "product_goal",
        "specification_structurer": "specification_author",
        "backlog_primer": "backlog",
        "roadmap_builder": "roadmap",
        "user_story_writer": "story",
        "sprint_planner": "sprint",
        "spec_validator": "spec_validator",
    }
    assert set(modules) == RETAINED_MODEL_ROLES
    leaves: dict[str, BaseAgent] = {}
    for role, name in modules.items():
        module = importlib.import_module(f"adapters.adk.agents.{name}")
        agent = (
            module.create_user_story_writer_agent()
            if name == "story"
            else module.root_agent
        )
        assert isinstance(
            agent.model.llm_client, boundary().RetryingOpenRouterClient
        ), role
        assert agent.model.model == get_model_id(role)
        leaves[role] = agent
    vision = importlib.import_module("adapters.adk.agents.vision")
    story = importlib.import_module("adapters.adk.agents.story")
    validator = importlib.import_module("adapters.adk.agents.spec_validator")
    for agent in (
        vision.repair_agent,
        story.create_user_story_patch_agent(),
        validator.build_spec_validator_agent(),
    ):
        assert hasattr(agent.model, "llm_client")
        assert isinstance(agent.model.llm_client, boundary().RetryingOpenRouterClient)
    registry = build_agentic_recipe_registry(
        nodes=AgenticRecipeNodes(
            vision_interview=leaves["product_vision"],
            vision_repair=vision.repair_agent,
            product_goal=leaves["product_goal"],
            specification_structurer=leaves["specification_structurer"],
            backlog_generation=leaves["backlog_primer"],
            roadmap_generation=leaves["roadmap_builder"],
            story_generation=leaves["user_story_writer"],
            story_correction=story.create_user_story_patch_agent(),
            sprint_planning=leaves["sprint_planner"],
        ),
        execution_settings={"timeout_seconds": 120.0, "max_attempts": 2},
    )
    assert (
        registry.node_ids == ROOT_GRAPH.agentic_node_ids == tuple(AGENTIC_MODEL_ROLES)
    )


@pytest.mark.asyncio
async def test_default_outer_deadline_cancels_without_real_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify default outer deadline cancels without real wait."""
    module = boundary()
    clock = Clock()
    audit = Audit(clock)
    cleaned = False
    sends = 0
    loop = asyncio.get_running_loop()
    original_call_at = loop.call_at

    def immediate_deadline(
        when: float,
        callback: Callable[..., object],
        *args: object,
        context: Context | None = None,
    ) -> asyncio.Handle:
        """Immediate deadline."""
        if sends == 1:
            clock.value += max(0.0, when - loop.time())
            return loop.call_soon(callback, *args, context=context)
        return original_call_at(when, callback, *args, context=context)

    monkeypatch.setattr(loop, "call_at", immediate_deadline)

    async def send(**kwargs: object) -> object:
        """Send."""
        del kwargs
        nonlocal sends, cleaned
        sends += 1
        if sends == 1:
            raise error(500)
        try:
            await asyncio.Event().wait()
        finally:
            cleaned = True

    with (
        module.bind_provider_action(context(clock, audit)),
        pytest.raises(ProviderTransientFailure) as caught,
    ):
        await invoke(
            module.RetryingOpenRouterClient(
                completion=send, clock=clock, uniform=lambda _low, _high: 0.0
            )
        )
    assert sends == 2
    assert cleaned
    assert caught.value.summary.http_status == 500
    assert caught.value.summary.termination_reason == "retry_budget_exhausted"


@pytest.mark.asyncio
async def test_earlier_original_request_timeout_does_not_become_budget_failure() -> (
    None
):
    """Verify earlier original request timeout does not become budget failure."""
    module = boundary()
    clock = Clock()
    audit = Audit(clock)
    sends = 0

    async def send(**kwargs: object) -> object:
        """Send."""
        del kwargs
        nonlocal sends
        sends += 1
        raise error(500) if sends == 1 else TimeoutError()

    with (
        module.bind_provider_action(context(clock, audit)),
        pytest.raises(TimeoutError),
    ):
        await module.RetryingOpenRouterClient(
            completion=send, clock=clock, uniform=lambda _low, _high: 0.0
        ).acompletion("openrouter/test-model", [], [], timeout=2.0)
    assert sends == 2
    assert not audit.checked
    assert audit.finishes[-1].disposition == "non_retryable"


@pytest.mark.asyncio
@pytest.mark.parametrize("eligible", [False, True])
async def test_guard_failure_after_finish_withholds_success_and_retry(
    eligible: bool,
) -> None:
    """Verify guard failure after finish withholds success and retry."""
    module = boundary()
    clock = Clock()
    audit = Audit(clock)
    sends = 0

    def guard() -> None:
        """Guard."""
        if audit.finishes:
            raise module.ProviderAttemptStopped()

    async def send(**kwargs: object) -> object:
        """Send."""
        del kwargs
        nonlocal sends
        sends += 1
        if eligible:
            raise error(503)
        return object()

    with (
        module.bind_provider_action(context(clock, audit, pre_try_check=guard)),
        pytest.raises(module.ProviderAttemptStopped),
    ):
        await invoke(module.RetryingOpenRouterClient(completion=send, clock=clock))
    assert sends == 1
    assert not audit.checked
    assert not clock.waits


@pytest.mark.asyncio
async def test_actual_adk_max_attempts_two_does_not_multiply_provider_tries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify actual adk max attempts two does not multiply provider tries."""
    from google.adk.agents import BaseAgent, InvocationContext  # noqa: PLC0415
    from google.adk.apps import App, ResumabilityConfig  # noqa: PLC0415
    from google.adk.events import Event  # noqa: PLC0415
    from google.adk.runners import Runner  # noqa: PLC0415
    from google.adk.sessions import InMemorySessionService  # noqa: PLC0415
    from google.genai import types  # noqa: PLC0415

    from adapters.adk.recipes import (  # noqa: PLC0415
        RecipeInput,
        _build_single_leaf_workflow,
    )

    module = boundary()
    clock = Clock()
    audit = Audit(clock)
    sends = 0

    async def send(**kwargs: object) -> object:
        """Send."""
        del kwargs
        nonlocal sends
        sends += 1
        raise error(503)

    client = module.RetryingOpenRouterClient(
        completion=send, clock=clock, uniform=lambda _low, _high: 0.0
    )
    original_sleep = asyncio.sleep

    async def no_wait(seconds: float, *args: object, **kwargs: object) -> object:
        """No wait."""
        del kwargs
        del args
        del seconds
        return await original_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", no_wait)

    class Leaf(BaseAgent):
        """Provider-free leaf fixture."""

        async def _run_async_impl(
            self, ctx: InvocationContext
        ) -> AsyncGenerator[Event, None]:
            """Run async impl."""
            del ctx
            await invoke(client)
            yield Event(author=self.name, output={})

    workflow = _build_single_leaf_workflow(
        workflow_name="provider_test",
        execution_node_name="provider_leaf",
        leaf_agent=Leaf(name="leaf"),
        execution_settings={"timeout_seconds": 120.0, "max_attempts": 2},
    )
    sessions = InMemorySessionService()
    await sessions.create_session(
        app_name="provider_test", user_id="user", session_id="1"
    )
    runner = Runner(
        app=App(
            name="provider_test",
            root_agent=workflow,
            resumability_config=ResumabilityConfig(is_resumable=True),
        ),
        session_service=sessions,
    )
    with (
        module.bind_provider_action(context(clock, audit)),
        pytest.raises(ProviderTransientFailure),
    ):
        async for _ in runner.run_async(
            user_id="user",
            session_id="1",
            new_message=types.Content(
                role="user",
                parts=[types.Part(text=RecipeInput(payload={}).model_dump_json())],
            ),
        ):
            pass
    assert workflow.retry_config is not None
    assert workflow.retry_config.max_attempts == 2
    assert sends == 3


@pytest.mark.asyncio
async def test_transport_timeout_error_is_not_an_owned_deadline_signal() -> None:
    """Verify transport timeout error is not an owned deadline signal."""
    module = boundary()
    clock = Clock()
    audit = Audit(clock)
    sends = 0

    async def send(**kwargs: object) -> object:
        """Send."""
        del kwargs
        nonlocal sends
        sends += 1
        raise error(500) if sends == 1 else TimeoutError()

    with (
        module.bind_provider_action(context(clock, audit)),
        pytest.raises(TimeoutError),
    ):
        await module.RetryingOpenRouterClient(
            completion=send, clock=clock, uniform=lambda _low, _high: 0.0
        ).acompletion("openrouter/test-model", [], [], timeout=120.0)
    assert sends == 2
    assert not audit.checked
    assert audit.finishes[-1].disposition == "non_retryable"


@pytest.mark.asyncio
async def test_each_send_uses_immutable_semantics_and_credentials_are_not_hashed() -> (
    None
):
    """Verify each send uses immutable semantics and credentials are not hashed."""
    module = boundary()
    clock = Clock()
    audit = Audit(clock)
    observed: list[str] = []

    async def send(**kwargs: object) -> object:
        """Send."""
        messages = cast("list[dict[str, str]]", kwargs["messages"])
        observed.append(messages[0]["content"])
        messages[0]["content"] = "mutated transport copy"
        if len(observed) == 1:
            raise error(500)
        return object()

    client = module.RetryingOpenRouterClient(
        completion=send, clock=clock, uniform=lambda _low, _high: 0.0
    )
    with module.bind_provider_action(context(clock, audit)):
        await client.acompletion(
            "openrouter/test-model",
            [{"role": "user", "content": "safe"}],
            [],
            api_key="first-key",
            extra_headers={"secret": "first-header"},
        )
        await client.acompletion(
            "openrouter/test-model",
            [{"role": "user", "content": "safe"}],
            [],
            api_key="second-key",
            extra_headers={"secret": "second-header"},
        )
    assert observed == ["safe"] * 3
    assert len({record.request_fingerprint for record in audit.starts}) == 1


@pytest.mark.asyncio
async def test_response_none_does_not_trigger_a_transport_retry() -> None:
    """Leave response validation to ADK without turning missing output into retry."""
    module = boundary()
    clock = Clock()
    audit = Audit(clock)
    sends = 0

    async def send(**kwargs: object) -> object:
        """Return an invalid output shape without a transport error."""
        del kwargs
        nonlocal sends
        sends += 1
        return None

    with module.bind_provider_action(context(clock, audit)):
        assert (
            await invoke(module.RetryingOpenRouterClient(completion=send, clock=clock))
            is None
        )
    assert sends == 1


def test_linux_fixture_snapshot_paths_exist_and_include_current_api_file() -> None:
    """Prove Linux setup copies existing runtime owners before the leaf rewrite."""
    root = Path(__file__).parents[2]
    tree = ast.parse(
        (root / "tests/dev_runtime/test_agent_failure_shutdown.py").read_text()
    )
    values = {
        node.target.id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.value is not None
        and node.target.id
        in {
            "_RUNTIME_SNAPSHOT_ROOTS",
            "_RUNTIME_SNAPSHOT_FILES",
            "_MODEL_IMPORT",
            "_MODEL_DEFINITION",
        }
    }
    for member in values["_RUNTIME_SNAPSHOT_ROOTS"]:
        assert (root / member).is_dir(), member
    for member in values.get("_RUNTIME_SNAPSHOT_FILES", ()):
        assert (root / member).is_file(), member
    assert "api.py" in values.get("_RUNTIME_SNAPSHOT_FILES", ())
    source = (root / "adapters/adk/agents/specification_author.py").read_text()
    assert source.count(values["_MODEL_IMPORT"]) == 1
    assert source.count(values["_MODEL_DEFINITION"]) == 1
    for dependency in (
        "adapters/adk/provider_retry.py",
        "adapters/adk/provider_models.py",
        "adapters/adk/provider_transport.py",
        "services/contracts/provider_retry.py",
        "repositories/provider_attempts.py",
        "utils/runtime_config.py",
        "models/enums.py",
        "adapters/adk/agents/specification_author.py",
    ):
        assert (root / dependency).is_file(), dependency
        assert dependency.split("/")[0] in values["_RUNTIME_SNAPSHOT_ROOTS"]
