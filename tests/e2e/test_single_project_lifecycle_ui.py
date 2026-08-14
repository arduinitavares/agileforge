"""Provider-free browser verification for the human lifecycle dashboard."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from git import Actor, Repo
from playwright.sync_api import expect, sync_playwright

from cli.dev_main import main as dev_main
from cli.dev_profiles import load_profile, profile_environment, reset_profile
from cli.dev_server import (
    ExpectedUIRuntime,
    select_loopback_port,
    start_ui,
    stop_ui,
    wait_for_readiness,
)
from utils.runtime_controls import (
    LAUNCHER_CHILD_ENV,
    LAUNCHER_CHILD_VALUE,
    UI_LAUNCH_NONCE_ENV,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from playwright.sync_api import Browser, Page, Route
    from playwright.sync_api._generated import ViewportSize

type JsonValue = (
    str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
)
type JsonObject = dict[str, JsonValue]

pytestmark = pytest.mark.allow_hosts(["127.0.0.1"])

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PROJECT_ID = 1
_HTTP_OK = 200
_HTTP_CREATED = 201
_HTTP_CONFLICT = 409
_HTTP_SERVICE_UNAVAILABLE = 503
_UI_SETTLE_MS = 150
_DESKTOP_VIEWPORT: ViewportSize = {"width": 1440, "height": 900}
_MOBILE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}
_PNG_WIDTH_START = 16
_PNG_WIDTH_END = 20
_PNG_HEIGHT_START = 20
_PNG_HEIGHT_END = 24
_FORBIDDEN_BODY_FIELDS = {
    "expected_fact_fingerprint",
    "expected_decision_fingerprint",
    "expected_candidate_fingerprint",
    "graph_version",
    "model_id",
}

_ACTION_ENDPOINTS = {
    "compile_authority": "authority/compile",
    "decide_authority": "authority/decision",
    "decide_product_goal_review": "goals/review",
    "decide_specification": "specifications/review",
    "decide_vision_review": "vision/review",
    "generate_vision_bootstrap": "vision/bootstrap",
    "record_authority_feedback": "authority/feedback",
    "record_backlog_draft": "backlog/generate",
    "record_product_goal_interview_turn": "goals/respond",
    "register_specification_source": "specifications/source",
    "record_vision_interview_turn": "vision/respond",
    "repair_authority": "authority/repair",
    "structure_specification": "specifications/structure",
}

_ACTION_CHILDREN = {
    "compile_authority": "authority",
    "decide_authority": "authority",
    "decide_product_goal_review": "product_goal",
    "decide_specification": "specification",
    "decide_vision_review": "vision",
    "generate_vision_bootstrap": "vision",
    "record_authority_feedback": "authority",
    "record_backlog_draft": "backlog",
    "record_product_goal_interview_turn": "product_goal",
    "register_specification_source": "specification",
    "record_vision_interview_turn": "vision",
    "repair_authority": "authority",
    "structure_specification": "specification",
}


@dataclass(frozen=True)
class DashboardHarness:
    """One isolated dashboard origin and its browser process."""

    url: str
    profile: str
    browser: Browser


@dataclass
class FakeLifecycle:
    """Stateful Task 7 semantic API fake used by one browser context."""

    repositories: dict[str, JsonObject]
    project: JsonObject | None = None
    repository: JsonObject | None = None
    vision_transcript: list[JsonValue] = field(default_factory=list)
    vision_draft: JsonObject | None = None
    vision_candidate: JsonObject | None = None
    vision_accepted: bool = False
    goal_transcript: list[JsonValue] = field(default_factory=list)
    goal_candidate: JsonObject | None = None
    goal_accepted: bool = False
    specification_source: JsonObject | None = None
    specification: JsonObject | None = None
    specification_feedback: str | None = None
    specification_structure_reason: str | None = None
    specification_structure_decision_fingerprint: str = "sha256:hidden-decision"
    specification_accepted: bool = False
    authority_pending: JsonObject | None = None
    authority_accepted: JsonObject | None = None
    authority_rejected: bool = False
    authority_feedback: str | None = None
    authority_generation: int = 0
    fail_next_authority_feedback: bool = False
    refresh_count: int = 0
    api_errors: list[str] = field(default_factory=list)

    def handle(self, route: Route) -> None:
        """Fulfill one intercepted API call without reaching the backend."""
        try:
            status, payload = self._dispatch(route)
        except (AssertionError, KeyError, TypeError, ValueError) as error:
            self.api_errors.append(str(error))
            route.fulfill(
                status=500,
                content_type="application/json",
                body=json.dumps({"detail": {"message": str(error)}}),
            )
            return
        route.fulfill(
            status=status,
            content_type="application/json",
            body=json.dumps(payload),
        )

    def _dispatch(self, route: Route) -> tuple[int, JsonObject]:
        request = route.request
        path = urlsplit(request.url).path
        if path == "/api/projects":
            if request.method == "GET":
                items: list[JsonValue] = [] if self.project is None else [self.project]
                return _HTTP_OK, self._success({"items": items})
            return self._create_project(self._request_body(route))

        prefix = f"/api/projects/{_PROJECT_ID}"
        assert path.startswith(prefix), f"Unexpected API path: {path}"
        assert self.project is not None, "Project must exist before scoped reads."
        suffix = path.removeprefix(prefix)
        if request.method == "GET":
            if suffix == "/position":
                return _HTTP_OK, self.position_envelope()
            return _HTTP_OK, self._success(self._read(suffix))
        assert request.method == "POST", f"Unexpected method: {request.method}"
        return self._mutate(
            suffix,
            self._request_body(route),
            dict(request.headers),
        )

    @staticmethod
    def _request_body(route: Route) -> JsonObject:
        body = cast("object", route.request.post_data_json)
        assert isinstance(body, dict), "Mutation body must be an object."
        return cast("JsonObject", body)

    @staticmethod
    def _success(data: JsonObject) -> JsonObject:
        return {"status": "success", "data": data}

    @staticmethod
    def _mutation_result() -> JsonObject:
        return {"status": "success", "data": {"output": {"recorded": True}}}

    def _read(self, suffix: str) -> JsonObject:
        readers: dict[str, Callable[[], JsonObject]] = {
            "": self._project_projection,
            "/authority/review": self._authority_projection,
            "/goals/status": self._goal_projection,
            "/repository": self._repository_projection,
            "/specifications/review": self._specification_projection,
            "/vision/status": self._vision_projection,
        }
        reader = readers.get(suffix)
        assert reader is not None, f"Unexpected read path: {suffix}"
        return reader()

    def _mutate(
        self,
        suffix: str,
        body: JsonObject,
        headers: dict[str, str],
    ) -> tuple[int, JsonObject]:
        review_fingerprint = self._review_fingerprint(suffix)
        if review_fingerprint is not None:
            expected = headers.get("x-agileforge-expected-candidate")
            assert expected is not None, (
                "Browser review omitted its hidden expectation."
            )
            if expected != review_fingerprint:
                return _HTTP_CONFLICT, {
                    "detail": {
                        "error": {
                            "code": "STALE_POSITION",
                            "message": (
                                "The candidate changed after this review opened. "
                                "Reload and review the current candidate."
                            ),
                        }
                    }
                }
        if suffix == "/authority/feedback" and self.fail_next_authority_feedback:
            self._assert_fields(body, {"feedback"})
            self.fail_next_authority_feedback = False
            return _HTTP_SERVICE_UNAVAILABLE, {
                "detail": {"message": "Feedback storage was interrupted."}
            }
        handlers: dict[str, Callable[[JsonObject], None]] = {
            "/authority/compile": self._compile_authority,
            "/authority/decision": self._review_authority,
            "/authority/feedback": self._record_authority_feedback,
            "/authority/repair": self._repair_authority,
            "/goals/respond": self._record_goal_turn,
            "/goals/review": self._review_goal,
            "/repository": self._attach_repository,
            "/repository/refresh": self._refresh_repository,
            "/specifications/source": self._register_specification_source,
            "/specifications/structure": self._structure_specification,
            "/specifications/review": self._review_specification,
            "/vision/bootstrap": self._bootstrap_vision,
            "/vision/respond": self._record_vision_turn,
            "/vision/review": self._review_vision,
        }
        handler = handlers.get(suffix)
        assert handler is not None, f"Unexpected mutation path: {suffix}"
        handler(body)
        return _HTTP_OK, self._mutation_result()

    def _review_fingerprint(self, suffix: str) -> str | None:
        candidates = {
            "/authority/decision": (
                None
                if self.authority_pending is None
                else self.authority_pending.get("authority_fingerprint")
            ),
            "/goals/review": (
                None
                if self.goal_candidate is None
                else self.goal_candidate.get("fingerprint")
            ),
            "/specifications/review": "sha256:hidden-specification",
            "/vision/review": (
                None
                if self.vision_candidate is None
                else self.vision_candidate.get("review_fingerprint")
            ),
        }
        value = candidates.get(suffix)
        return value if isinstance(value, str) else None

    def _assert_fields(self, body: JsonObject, business_fields: set[str]) -> None:
        expected = {"actor", "idempotency_key", *business_fields}
        assert set(body) == expected
        assert body["actor"] == "dashboard-ui"
        idempotency_key = body["idempotency_key"]
        assert isinstance(idempotency_key, str)
        assert idempotency_key.startswith("dashboard-")
        assert _FORBIDDEN_BODY_FIELDS.isdisjoint(body)

    def _create_project(self, body: JsonObject) -> tuple[int, JsonObject]:
        self._assert_fields(body, {"description", "name", "repository_path"})
        name = body["name"]
        description = body["description"]
        repository_path = body["repository_path"]
        assert isinstance(name, str)
        assert name
        assert description is None or isinstance(description, str)
        assert repository_path is None or isinstance(repository_path, str)
        self.project = {
            "project_id": _PROJECT_ID,
            "name": name,
            "description": description,
            "user_stories_count": 0,
            "sprint_count": 0,
        }
        if repository_path is not None:
            self.repository = self.repositories[repository_path]
        return _HTTP_CREATED, self._success({"output": {"project_id": _PROJECT_ID}})

    def _bootstrap_vision(self, body: JsonObject) -> None:
        self._assert_fields(body, set())
        self.vision_draft = {
            "statement": "Product teams need durable lifecycle review.",
            "components": [
                {
                    "name": "target_user",
                    "value": "Product teams",
                    "source_kinds": ["evidence"],
                },
                {
                    "name": "differentiator",
                    "value": None,
                    "source_kinds": [],
                },
            ],
            "assumptions": [
                {
                    "text": "Teams can adopt one review workflow.",
                    "affected_components": ["key_benefit"],
                }
            ],
            "conflicts": [],
            "questions": [
                {
                    "question_id": "q-target-team",
                    "text": "Which product team should benefit first?",
                    "affected_components": ["target_user"],
                }
            ],
        }

    def _record_vision_turn(self, body: JsonObject) -> None:
        self._assert_fields(body, {"text"})
        text = body["text"]
        assert isinstance(text, str)
        assert text
        self.vision_transcript.append({"turn_number": 1, "user_text": text})
        self.vision_draft = None
        self.vision_candidate = {
            "statement": "Give product teams a durable, reviewable lifecycle.",
            "components": [
                {
                    "name": "target_user",
                    "value": "Product teams",
                    "source_kinds": ["human", "evidence"],
                },
                {
                    "name": "differentiator",
                    "value": "Decisions remain reviewable",
                    "source_kinds": ["human"],
                },
            ],
            "assumptions": [],
            "conflicts": [],
            "questions": [],
            "review_fingerprint": "sha256:hidden-vision",
        }

    def _review_vision(self, body: JsonObject) -> None:
        self._assert_fields(body, {"decision", "rationale"})
        assert body["decision"] == "accepted"
        assert isinstance(body["rationale"], str)
        self.vision_accepted = True

    def _record_goal_turn(self, body: JsonObject) -> None:
        self._assert_fields(body, {"text"})
        text = body["text"]
        assert isinstance(text, str)
        assert text
        self.goal_transcript.append({"goal_number": 1, "user_text": text})
        self.goal_candidate = {
            "statement": "Ship trusted lifecycle review for one pilot team.",
            "components": {
                "outcome": "One pilot team completes the lifecycle",
                "measure": "Every decision has durable review evidence",
            },
            "fingerprint": "sha256:hidden-goal",
        }

    def _review_goal(self, body: JsonObject) -> None:
        self._assert_fields(body, {"decision", "rationale"})
        assert body["decision"] == "accepted"
        assert isinstance(body["rationale"], str)
        self.goal_accepted = True

    def _register_specification_source(self, body: JsonObject) -> None:
        self._assert_fields(
            body,
            {"source_path", "preparation_capability", "adr_paths"},
        )
        assert body["source_path"] == "specs/product-specification.md"
        assert body["preparation_capability"] == "grill-with-docs"
        assert body["adr_paths"] == ["docs/adr/0004-registered-source.md"]
        self.specification_source = {
            "specification_source_id": 31,
            "source_fingerprint": "sha256:hidden-registered-source",
            "producer_capability": "to-spec",
            "preparation_capability": "grill-with-docs",
            "context": {"state": "absent", "document": None},
        }

    def _structure_specification(self, body: JsonObject) -> None:
        self._assert_fields(body, set())
        assert self.specification_source is not None
        self.specification = {
            "title": "Human lifecycle review",
            "rendered_markdown": (
                "# Human lifecycle review\n\n"
                "## Acceptance criteria\n\n"
                "- Every candidate has a scoped human decision."
            ),
        }

    def _review_specification(self, body: JsonObject) -> None:
        self._assert_fields(body, {"decision", "rationale"})
        assert body["decision"] == "accepted"
        assert isinstance(body["rationale"], str)
        self.specification_accepted = True

    def _compile_authority(self, body: JsonObject) -> None:
        self._assert_fields(body, set())
        self.authority_generation += 1
        self.authority_pending = {
            "authority_id": 91,
            "authority_fingerprint": (
                f"sha256:hidden-authority-{self.authority_generation}"
            ),
            "compiler_version": "hidden-compiler",
            "invariants": [
                {
                    "id": "INV-01",
                    "type": "REQUIRED_FIELD",
                    "parameters": {"field_name": "project_id"},
                },
                {
                    "id": "INV-02",
                    "type": "PROVENANCE_REQUIRED",
                    "parameters": {"artifact": "review_evidence"},
                },
            ],
            "findings": [
                {
                    "severity": "review",
                    "message": "Confirm pilot team ownership before delivery begins.",
                }
            ],
        }
        self.authority_rejected = False
        self.authority_feedback = None

    def _review_authority(self, body: JsonObject) -> None:
        self._assert_fields(body, {"decision", "rationale"})
        assert isinstance(body["rationale"], str)
        if body["decision"] == "accepted":
            self.authority_accepted = self.authority_pending
            self.authority_pending = None
            self.authority_rejected = False
            return
        assert body["decision"] == "rejected"
        self.authority_rejected = True

    def _record_authority_feedback(self, body: JsonObject) -> None:
        self._assert_fields(body, {"feedback"})
        feedback = body["feedback"]
        assert isinstance(feedback, str)
        assert feedback
        assert self.authority_rejected is True
        self.authority_feedback = feedback

    def _repair_authority(self, body: JsonObject) -> None:
        self._assert_fields(body, set())
        assert self.authority_feedback is not None
        self._compile_authority(body)

    def _attach_repository(self, body: JsonObject) -> None:
        self._assert_fields(body, {"path"})
        path = body["path"]
        assert isinstance(path, str)
        self.repository = self.repositories[path]

    def _refresh_repository(self, body: JsonObject) -> None:
        self._assert_fields(body, set())
        assert self.repository is not None
        self.refresh_count += 1

    def _project_projection(self) -> JsonObject:
        assert self.project is not None
        return {
            **self.project,
            "product_goal": (
                None
                if not self.goal_accepted
                else {"statement": self._goal_statement()}
            ),
            "repository": self.repository,
            "structure_counts": {"user_stories": 0, "sprints": 0},
        }

    def _vision_statement(self) -> str:
        assert self.vision_candidate is not None
        statement = self.vision_candidate["statement"]
        assert isinstance(statement, str)
        return statement

    def _goal_statement(self) -> str:
        assert self.goal_candidate is not None
        statement = self.goal_candidate["statement"]
        assert isinstance(statement, str)
        return statement

    def _vision_projection(self) -> JsonObject:
        current: JsonObject | None = (
            {"statement": self._vision_statement()} if self.vision_accepted else None
        )
        candidate = None if self.vision_accepted else self.vision_candidate
        review: JsonObject | None = (
            {"state": "pending", "rationale": None} if candidate is not None else None
        )
        return {
            "bootstrap_available": (
                not self.vision_accepted
                and self.vision_draft is None
                and self.vision_candidate is None
            ),
            "current": current,
            "draft": self.vision_draft,
            "transcript": self.vision_transcript,
            "candidate": candidate,
            "review": review,
            "stale_reason": None if self.vision_accepted else "VISION_NOT_ACCEPTED",
        }

    def _goal_projection(self) -> JsonObject:
        accepted_vision: JsonObject | None = (
            {"statement": self._vision_statement(), "fingerprint": "sha256:hidden"}
            if self.vision_accepted
            else None
        )
        candidate = None if self.goal_accepted else self.goal_candidate
        active: JsonObject | None = (
            {"statement": self._goal_statement(), "fingerprint": "sha256:hidden"}
            if self.goal_accepted
            else None
        )
        latest_questions: list[JsonValue] = (
            ["What observable result proves the pilot succeeded?"]
            if self.vision_accepted and self.goal_candidate is None
            else []
        )
        review: JsonObject | None = (
            {"state": "pending"} if candidate is not None else None
        )
        return {
            "accepted_vision": accepted_vision,
            "active": active,
            "transcript": self.goal_transcript,
            "latest_questions": latest_questions,
            "candidate": candidate,
            "review": review,
            "outcome": None,
            "stale_reason": None if self.goal_accepted else "GOAL_NOT_ACTIVE",
        }

    def _specification_projection(self) -> JsonObject:
        candidate: JsonObject | None = None
        if self.specification is not None:
            assert self.specification_source is not None
            candidate = {
                "specification_candidate_id": 32,
                "specification_source_id": self.specification_source[
                    "specification_source_id"
                ],
                "registered_source_fingerprint": self.specification_source[
                    "source_fingerprint"
                ],
                "candidate_fingerprint": "sha256:hidden-specification",
                "payload_fingerprint": "sha256:hidden-specification-payload",
                "rendered_markdown": self.specification["rendered_markdown"],
            }
        review: JsonObject | None = None
        if candidate is not None:
            if self.specification_accepted:
                state = "accepted"
            elif self.specification_feedback is not None:
                state = "feedback"
            else:
                state = "pending"
            review = {"state": state, "rationale": self.specification_feedback}
        return {
            "source": self.specification_source,
            "candidate": candidate,
            "review": review,
            "stale_reason": None,
        }

    def _authority_projection(self) -> JsonObject:
        findings = (
            []
            if self.authority_pending is None
            else self.authority_pending.get("findings", [])
        )
        assert isinstance(findings, list)
        return {
            "accepted_authority": self.authority_accepted,
            "pending_authority": self.authority_pending,
            "findings": findings,
        }

    def _repository_projection(self) -> JsonObject:
        return {"repository": self.repository}

    def _phase_action(self) -> str:
        phases = (
            (
                self.vision_draft is None and self.vision_candidate is None,
                "generate_vision_bootstrap",
            ),
            (self.vision_candidate is None, "record_vision_interview_turn"),
            (not self.vision_accepted, "decide_vision_review"),
            (self.goal_candidate is None, "record_product_goal_interview_turn"),
            (not self.goal_accepted, "decide_product_goal_review"),
            (
                self.specification_source is None,
                "register_specification_source",
            ),
            (
                self.specification_feedback is not None
                or self.specification is None,
                "structure_specification",
            ),
            (not self.specification_accepted, "decide_specification"),
            (
                self.authority_pending is None and self.authority_accepted is None,
                "compile_authority",
            ),
            (
                self.authority_rejected and self.authority_feedback is None,
                "record_authority_feedback",
            ),
            (
                self.authority_rejected and self.authority_feedback is not None,
                "repair_authority",
            ),
            (self.authority_pending is not None, "decide_authority"),
            (True, "record_backlog_draft"),
        )
        return next(action for active, action in phases if active)

    def _position_projection(self) -> JsonObject:
        request_kind = self._phase_action()
        child = _ACTION_CHILDREN[request_kind]
        endpoint = _ACTION_ENDPOINTS[request_kind]
        node_id = {
            "register_specification_source": "specification.source.register",
            "structure_specification": "specification.structure",
        }.get(request_kind, f"internal.{request_kind}")
        fact_references: list[JsonValue] = []
        if request_kind == "structure_specification":
            assert self.specification_source is not None
            fact_references.append(
                {
                    "fact_type": "specification_source",
                    "fact_id": str(
                        self.specification_source["specification_source_id"]
                    ),
                    "fingerprint": self.specification_source["source_fingerprint"],
                }
            )
            if self.specification_feedback is not None:
                fact_references.append(
                    {
                        "fact_type": "specification_candidate",
                        "fact_id": "32",
                        "fingerprint": "sha256:hidden-specification",
                    }
                )
            if self.specification_structure_reason == "SPECIFICATION_STRUCTURER_FAILED":
                fact_references.append(
                    {
                        "fact_type": "node_attempt",
                        "fact_id": "23",
                        "fingerprint": "sha256:hidden-failed-attempt",
                    }
                )
        specification_reason = self.specification_structure_reason
        if specification_reason is None and self.specification_feedback is not None:
            specification_reason = "SPECIFICATION_FEEDBACK_RETRY_AVAILABLE"
        decision: JsonObject = {
            "node_id": node_id,
            "child_graph_id": child,
            "request_kind": request_kind,
            "category": "available",
            "reason_code": specification_reason or "INTERNAL_REASON_CODE",
            "decision_fingerprint": self.specification_structure_decision_fingerprint,
            "fact_references": fact_references,
        }
        blocker: JsonObject = {
            "node_id": "internal.next_stage",
            "child_graph_id": "backlog" if child == "authority" else "authority",
            "request_kind": "record_backlog_draft",
            "category": "blocked",
            "reason_code": "INTERNAL_BLOCKER",
            "blockers": [
                {"message": "Finish the current human review before continuing."}
            ],
            "decision_fingerprint": "sha256:hidden-blocker",
        }
        return {
            "graph_version": "agileforge.workflow.hidden",
            "fact_fingerprint": "sha256:hidden-facts",
            "decisions": [decision, blocker],
            "terminal": False,
            "actions": [],
        } | {
            "_actions": [
                {
                    "node_id": decision["node_id"],
                    "instance_key": None,
                    "request_kind": request_kind,
                    "endpoint": endpoint,
                    "transport": "semantic",
                }
            ]
        }

    def position_envelope(self) -> JsonObject:
        """Return position data and advertised actions in the HTTP shape."""
        projection = self._position_projection()
        actions = projection.pop("_actions")
        assert isinstance(actions, list)
        return {
            "status": "success",
            "data": projection,
            "actions": actions,
        }


@pytest.fixture(scope="module")
def dashboard_harness() -> Iterator[DashboardHarness]:
    """Start one clean isolated AgileForge profile with no provider credentials."""
    profile_name = f"task8-e2e-{uuid4().hex[:10]}"
    status = dev_main(
        ["init", "--profile", profile_name, "--mode", "development"],
        checkout_root=_PROJECT_ROOT,
    )
    assert status == 0
    profile = load_profile(_PROJECT_ROOT, profile_name)
    environment = profile_environment(profile)
    environment[LAUNCHER_CHILD_ENV] = LAUNCHER_CHILD_VALUE
    launch_nonce = uuid4().hex
    environment[UI_LAUNCH_NONCE_ENV] = launch_nonce
    environment.pop("OPEN_ROUTER_API_KEY", None)
    port = select_loopback_port()
    child = start_ui(
        checkout_root=_PROJECT_ROOT,
        environment=environment,
        port=port,
        reload=False,
    )
    expected = ExpectedUIRuntime(
        checkout_root=_PROJECT_ROOT,
        commit=profile.checkout.commit,
        business_database=profile.business_database,
        trace_database=profile.trace_database,
        process_id=child.process.pid,
        launch_nonce=launch_nonce,
    )
    wait_for_readiness(child, expected=expected, timeout=15)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            yield DashboardHarness(
                url=f"{child.url}/dashboard",
                profile=profile_name,
                browser=browser,
            )
        finally:
            browser.close()
            stop_ui(child)
            reset_profile(_PROJECT_ROOT, profile_name, profile_name)


def _repository_fixture(path: Path, *, dirty: bool) -> JsonObject:
    path.mkdir(parents=True)
    repository = Repo.init(path, initial_branch="main")
    source = path / "product-direction.txt"
    source.write_text("Durable product direction.\n", encoding="utf-8")
    repository.index.add([source.name])
    actor = Actor("AgileForge UI Test", "ui-test@example.invalid")
    repository.index.commit("Initial fixture", author=actor, committer=actor)
    if dirty:
        source.write_text(
            "Durable product direction.\nUncommitted operator note.\n",
            encoding="utf-8",
        )
    assert repository.is_dirty(untracked_files=True) is dirty
    warning: list[JsonValue] = (
        [
            {
                "message": (
                    "Working tree has uncommitted changes in a deliberately long "
                    "nested source path that must wrap on narrow screens."
                )
            }
        ]
        if dirty
        else []
    )
    return {
        "worktree_path": str(path),
        "common_git_dir": str(path / ".git"),
        "head_sha": repository.head.commit.hexsha,
        "branch_name": "main",
        "detached_head": False,
        "dirty": dirty,
        "inspected_at": "2026-08-10T12:00:00Z",
        "warnings": warning,
        "remotes": [],
        "status_fingerprint": "sha256:hidden-repository-status",
    }


def _create_project(
    page: Page,
    *,
    name: str,
    description: str,
    repository_path: str | None,
) -> None:
    page.goto(page.url or "about:blank")
    page.locator("#open-create-project").click()
    page.locator("#modal-project-name").fill(name)
    page.locator("#modal-project-description").fill(description)
    if repository_path is not None:
        page.locator("#modal-repository-path").fill(repository_path)
    page.locator("#btn-submit-project").click()
    page.wait_for_url(f"**/dashboard/project.html?id={_PROJECT_ID}")


def _accept_review(page: Page, scope: str) -> None:
    page.locator(
        f'[data-review-scope="{scope}"][data-review-decision="accepted"]'
    ).click()
    expect(page.locator("#human-action-dialog")).to_be_visible()
    page.locator("#human-action-submit").click()
    expect(page.locator("#human-action-dialog")).not_to_be_visible()


def _assert_no_horizontal_overflow(page: Page) -> None:
    fits = page.evaluate(
        "document.documentElement.scrollWidth === document.documentElement.clientWidth"
    )
    assert fits is True


def _assert_no_control_overlap(page: Page) -> None:
    overlaps = page.evaluate(
        """() => {
            const nodes = [...document.querySelectorAll(
                'header a, header button, main button, main input, main textarea'
            )].filter((node) => {
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node);
                return rect.width > 0 && rect.height > 0
                    && style.visibility !== 'hidden' && style.display !== 'none';
            });
            const collisions = [];
            for (let leftIndex = 0; leftIndex < nodes.length; leftIndex += 1) {
                for (
                    let rightIndex = leftIndex + 1;
                    rightIndex < nodes.length;
                    rightIndex += 1
                ) {
                    const left = nodes[leftIndex];
                    const right = nodes[rightIndex];
                    if (left.contains(right) || right.contains(left)) continue;
                    const a = left.getBoundingClientRect();
                    const b = right.getBoundingClientRect();
                    const width = Math.min(a.right, b.right) - Math.max(a.left, b.left);
                    const height = Math.min(a.bottom, b.bottom)
                        - Math.max(a.top, b.top);
                    if (width > 1 && height > 1) {
                        const leftName = left.id || left.textContent.trim();
                        const rightName = right.id || right.textContent.trim();
                        collisions.push(`${leftName} / ${rightName}`);
                    }
                }
            }
            return collisions;
        }"""
    )
    assert overlaps == []


def _assert_human_only_surface(page: Page) -> None:
    body_text = page.locator("body").inner_text()
    for forbidden in (
        "INTERNAL_REASON_CODE",
        "agileforge.workflow.hidden",
        "authority.compile",
    ):
        assert forbidden not in body_text
    assert (
        page.locator(
            'input[id*="fingerprint"], input[id*="commit"], '
            'input[id*="dirty"], select[id*="model"], textarea[id*="payload"]'
        ).count()
        == 0
    )


def _assert_screenshot_size(path: Path, expected: ViewportSize) -> None:
    payload = path.read_bytes()
    width = int.from_bytes(payload[_PNG_WIDTH_START:_PNG_WIDTH_END], "big")
    height = int.from_bytes(payload[_PNG_HEIGHT_START:_PNG_HEIGHT_END], "big")
    assert (width, height) == (expected["width"], expected["height"])


def _complete_vision_and_goal(
    page: Page,
    fake: FakeLifecycle,
    *,
    replace_vision_during_review: bool = False,
) -> None:
    expect(page.locator("#vision-response")).not_to_be_visible()
    expect(page.get_by_role("button", name="Generate Vision draft")).to_be_visible()
    page.get_by_role("button", name="Generate Vision draft").click()
    expect(page.locator("#vision-response")).to_be_visible()
    expect(page.get_by_text("Which product team should benefit first?")).to_be_visible()
    page.locator("#vision-response").fill(
        "Product teams need one durable place to review lifecycle decisions."
    )
    page.locator('form[data-interview-scope="vision"] button[type="submit"]').click()
    expect(page.get_by_text("Vision candidate", exact=True)).to_be_visible()
    expect(
        page.get_by_text("Give product teams a durable, reviewable lifecycle.")
    ).to_be_visible()
    if replace_vision_during_review:
        page.locator(
            '[data-review-scope="vision"][data-review-decision="accepted"]'
        ).click()
        expect(page.locator("#human-action-dialog")).to_be_visible()
        fake.vision_candidate = {
            "statement": "Give product teams a replacement lifecycle candidate.",
            "components": [
                {
                    "name": "target_user",
                    "value": "Replacement pilot team",
                    "source_kinds": ["human"],
                }
            ],
            "assumptions": [],
            "conflicts": [],
            "questions": [],
            "review_fingerprint": "sha256:hidden-vision-replacement",
        }
        page.locator("#human-action-submit").click()
        expect(page.locator("#human-action-error")).to_contain_text("candidate changed")
        assert fake.vision_accepted is False
        page.locator("#human-action-close").click()
        page.locator("#refresh-project").click()
        expect(
            page.get_by_text("Give product teams a replacement lifecycle candidate.")
        ).to_be_visible()
    _accept_review(page, "vision")
    expect(page.locator("#goal-response")).to_be_visible()
    page.locator("#goal-response").fill(
        "One pilot team should finish the lifecycle with durable review evidence."
    )
    page.locator('form[data-interview-scope="goal"] button[type="submit"]').click()
    expect(page.get_by_text("Exact Product Goal candidate")).to_be_visible()
    _accept_review(page, "goal")
    expect(page.get_by_text("Active Product Goal")).to_be_visible()


def _record_and_review_definition(page: Page) -> None:
    page.locator("#specification-source-path").fill("specs/product-specification.md")
    page.locator("#specification-preparation-capability").select_option(
        "grill-with-docs"
    )
    page.locator("#specification-adr-paths").fill("docs/adr/0004-registered-source.md")
    page.locator('form[data-specification-source-form="true"]').evaluate(
        "form => form.requestSubmit()"
    )
    expect(page.get_by_role("button", name="Structure Specification")).to_be_visible()
    page.locator('[data-direct-action="structure_specification"]').click()
    expect(page.get_by_text("Human lifecycle review")).to_be_visible()
    expect(
        page.get_by_text("Every candidate has a scoped human decision.")
    ).to_be_visible()
    _accept_review(page, "specification")


def _compile_and_review_authority(page: Page) -> None:
    page.locator('[data-direct-action="compile_authority"]').click()
    expect(page.get_by_text("Exact Authority review packet")).to_be_visible()
    expect(page.get_by_text("Complete compiled Authority artifact")).to_be_visible()
    expect(page.get_by_text('"type": "REQUIRED_FIELD"')).to_be_visible()
    expect(page.get_by_text('"field_name": "project_id"')).to_be_visible()
    expect(
        page.get_by_text("Confirm pilot team ownership before delivery begins.")
    ).to_be_visible()
    page.locator(
        '[data-review-scope="authority"][data-review-decision="rejected"]'
    ).click()
    page.locator("#human-action-rationale").fill(
        "Clarify the ownership invariant before acceptance."
    )
    page.locator("#human-action-submit").click()
    expect(page.get_by_text("Record feedback", exact=True)).to_be_visible()
    page.locator('[data-authority-feedback="true"]').click()
    page.locator("#human-action-rationale").fill(
        "Require one named owner for lifecycle review."
    )
    page.locator("#human-action-submit").click()
    expect(page.get_by_text("Recompile", exact=True)).to_be_visible()
    page.locator('[data-direct-action="repair_authority"]').click()
    expect(page.get_by_text("Exact Authority review packet")).to_be_visible()
    _accept_review(page, "authority")
    expect(page.get_by_text("Authority accepted")).to_be_visible()


def _authority_ready_fake() -> FakeLifecycle:
    fake = FakeLifecycle(repositories={})
    fake.project = {
        "project_id": _PROJECT_ID,
        "name": "Authority recovery",
        "description": "Interrupted feedback recovery.",
        "user_stories_count": 0,
        "sprint_count": 0,
    }
    fake.vision_candidate = {
        "statement": "A durable recovery lifecycle.",
        "fingerprint": "sha256:recovery-vision",
    }
    fake.vision_accepted = True
    fake.goal_candidate = {
        "statement": "Recover every interrupted human review.",
        "fingerprint": "sha256:recovery-goal",
    }
    fake.goal_accepted = True
    fake.specification_source = {
        "specification_source_id": 31,
        "source_fingerprint": "sha256:recovery-source",
        "producer_capability": "to-spec",
        "preparation_capability": "grill-with-docs",
        "context": {"state": "absent", "document": None},
    }
    fake.specification = {
        "title": "Recoverable Authority feedback",
        "rendered_markdown": "# Recoverable Authority feedback",
    }
    fake.specification_accepted = True
    fake._compile_authority(
        {
            "actor": "dashboard-ui",
            "idempotency_key": "dashboard-seed-authority",
        }
    )
    return fake


def _assert_create_modal_keyboard_contract(page: Page) -> None:
    opener = page.locator("#open-create-project")
    opener.click()
    expect(page.locator("#create-project-modal")).to_be_visible()
    assert page.locator("#dashboard-content").evaluate("element => element.inert")
    page.keyboard.press("Escape")
    expect(page.locator("#create-project-modal")).not_to_be_visible()
    assert opener.evaluate("element => element === document.activeElement")

    opener.click()
    page.locator("#close-create-project").focus()
    page.keyboard.press("Shift+Tab")
    assert page.locator("#btn-submit-project").evaluate(
        "element => element === document.activeElement"
    )
    page.keyboard.press("Tab")
    assert page.locator("#close-create-project").evaluate(
        "element => element === document.activeElement"
    )
    page.keyboard.press("Escape")


def _attach_and_refresh_repository(
    page: Page,
    fake: FakeLifecycle,
    repository_path: Path,
) -> None:
    page.locator('[data-repository-action="attach"]').click()
    page.locator("#human-action-path").fill(str(repository_path))
    page.locator("#human-action-submit").click()
    expect(
        page.locator("#repository-panel").get_by_text(str(repository_path))
    ).to_be_visible()
    stage_before_refresh = page.locator("#lifecycle-stage-strip").inner_text()
    page.locator('[data-repository-action="refresh"]').click()
    page.wait_for_timeout(_UI_SETTLE_MS)
    assert fake.refresh_count == 1
    assert page.locator("#lifecycle-stage-strip").inner_text() == stage_before_refresh


def test_issue_204_structuring_reports_local_state_and_reloads_successor(
    dashboard_harness: DashboardHarness,
) -> None:
    """Keep deferred, failed, and successful structuring visible in place."""
    fake = FakeLifecycle(
        repositories={},
        project={
            "project_id": _PROJECT_ID,
            "name": "Issue 204 lifecycle",
            "description": "Provider-free Specification structuring coverage.",
        },
        vision_candidate={"statement": "Accepted Vision"},
        vision_accepted=True,
        goal_candidate={"statement": "Accepted Product Goal"},
        goal_accepted=True,
        specification_source={
            "specification_source_id": 31,
            "source_fingerprint": "sha256:issue-204-source",
            "producer_capability": "to-spec",
            "preparation_capability": "grill-with-docs",
            "context": {"state": "absent", "document": None},
        },
        specification={"rendered_markdown": "# Prior Feedback candidate"},
        specification_feedback="Restore the exact observable contract.",
    )
    context = dashboard_harness.browser.new_context(viewport=_DESKTOP_VIEWPORT)
    context.route("**/api/**", fake.handle)
    page = context.new_page()
    page.goto(
        f"{dashboard_harness.url}/project.html?id={_PROJECT_ID}",
        wait_until="networkidle",
    )
    page.evaluate(
        """() => {
            const originalFetch = window.fetch.bind(window);
            window.issue204Requests = [];
            window.resolveIssue204Structure = null;
            window.fetch = (input, init = {}) => {
                const url = String(input);
                if (url.endsWith('/specifications/structure')
                        && init.method === 'POST') {
                    window.issue204Requests.push({
                        headers: Object.fromEntries(new Headers(init.headers)),
                    });
                    return new Promise((resolve) => {
                        window.resolveIssue204Structure = (response) => {
                            window.resolveIssue204Structure = null;
                            resolve(new Response(JSON.stringify(response.body), {
                                status: response.status,
                                headers: { 'Content-Type': 'application/json' },
                            }));
                        };
                    });
                }
                return originalFetch(input, init);
            };
        }"""
    )

    button = page.locator('[data-direct-action="structure_specification"]')
    expect(button).to_be_visible()
    expect(button).to_contain_text("Retry structuring from unchanged source")
    button.click()
    page.wait_for_function("window.resolveIssue204Structure !== null")

    status = page.locator('[data-specification-structuring-status="true"]')
    expect(button).to_be_disabled()
    expect(button).to_have_attribute("aria-busy", "true")
    expect(button).to_contain_text("Structuring Specification...")
    expect(status).to_be_visible()
    expect(status).to_have_text("Structuring Specification...")
    assert page.evaluate("window.issue204Requests.length") == 1
    assert page.evaluate(
        "window.issue204Requests[0].headers['x-agileforge-expected-decision']"
    ) == "sha256:hidden-decision"

    fake.specification_structure_reason = "SPECIFICATION_STRUCTURER_FAILED"
    fake.specification_structure_decision_fingerprint = "sha256:failed-decision"
    page.evaluate(
        """window.resolveIssue204Structure({
            status: 409,
            body: {
                detail: {
                    error: {
                        code: 'SPECIFICATION_PRODUCER_FAILED',
                        message: 'Specification structurer provider execution failed.',
                    },
                },
            },
        })"""
    )
    expect(button).to_be_enabled()
    expect(button).not_to_have_attribute("aria-busy", "true")
    expect(status).to_contain_text(
        "Specification structurer provider execution failed."
    )
    expect(status).to_contain_text("No new candidate was produced.")
    expect(status).to_contain_text(
        "The prior candidate and Feedback remain current."
    )
    expect(
        page.get_by_text("Prior Feedback candidate", exact=False)
    ).to_be_visible()
    expect(button).to_be_visible()
    assert page.evaluate("window.issue204Requests.length") == 1

    button.click()
    page.wait_for_function("window.resolveIssue204Structure !== null")
    expect(button).to_be_disabled()
    assert [
        request["headers"]["x-agileforge-expected-decision"]
        for request in page.evaluate("window.issue204Requests")
    ] == ["sha256:hidden-decision", "sha256:failed-decision"]
    fake.specification = {"rendered_markdown": "# Successor pending candidate"}
    fake.specification_feedback = None
    page.evaluate(
        """window.resolveIssue204Structure({
            status: 200,
            body: { status: 'success', data: { output: { recorded: true } } },
        })"""
    )

    expect(
        page.get_by_text("Successor pending candidate", exact=False)
    ).to_be_visible()
    expect(
        page.locator(
            '[data-review-scope="specification"][data-review-decision="accepted"]'
        )
    ).to_be_visible()
    expect(
        page.get_by_role(
            "button",
            name="Retry structuring from unchanged source",
        )
    ).not_to_be_visible()
    assert fake.api_errors == []
    context.close()


def test_desktop_human_single_lifecycle(
    dashboard_harness: DashboardHarness,
    tmp_path: Path,
) -> None:
    """Complete the human definition lifecycle at 1440 by 900."""
    repository_path = tmp_path / "desktop-repository"
    repository = _repository_fixture(repository_path, dirty=False)
    fake = FakeLifecycle(repositories={str(repository_path): repository})
    context = dashboard_harness.browser.new_context(viewport=_DESKTOP_VIEWPORT)
    context.route("**/api/**", fake.handle)
    page = context.new_page()
    page.goto(dashboard_harness.url, wait_until="networkidle")
    _assert_create_modal_keyboard_contract(page)

    _create_project(
        page,
        name="Lifecycle Pilot",
        description="One durable product definition lifecycle.",
        repository_path=None,
    )
    _complete_vision_and_goal(
        page,
        fake,
        replace_vision_during_review=True,
    )
    _record_and_review_definition(page)
    _compile_and_review_authority(page)
    _attach_and_refresh_repository(page, fake, repository_path)

    _assert_human_only_surface(page)
    _assert_no_horizontal_overflow(page)
    page.locator("#authority-panel").scroll_into_view_if_needed()
    _assert_no_control_overlap(page)
    screenshot = tmp_path / "desktop-1440x900.png"
    page.screenshot(path=screenshot, full_page=False, animations="disabled")
    _assert_screenshot_size(screenshot, _DESKTOP_VIEWPORT)
    assert fake.api_errors == []
    context.close()


def test_interrupted_authority_feedback_recovers_without_a_dead_end(
    dashboard_harness: DashboardHarness,
) -> None:
    """Resume feedback after rejection succeeded but feedback storage failed."""
    fake = _authority_ready_fake()
    fake.fail_next_authority_feedback = True
    context = dashboard_harness.browser.new_context(viewport=_DESKTOP_VIEWPORT)
    context.route("**/api/**", fake.handle)
    page = context.new_page()
    page.goto(
        f"{dashboard_harness.url}/project.html?id={_PROJECT_ID}",
        wait_until="networkidle",
    )

    expect(page.get_by_text("Exact Authority review packet")).to_be_visible()
    page.locator(
        '[data-review-scope="authority"][data-review-decision="feedback"]'
    ).click()
    page.locator("#human-action-rationale").fill(
        "Require explicit ownership before delivery."
    )
    page.locator("#human-action-submit").click()

    expect(page.locator("#human-action-dialog")).not_to_be_visible()
    expect(page.locator("#project-error")).to_contain_text("feedback was not recorded")
    expect(page.get_by_text("Record feedback", exact=True)).to_be_visible()
    page.locator('[data-authority-feedback="true"]').click()
    page.locator("#human-action-rationale").fill(
        "Require explicit ownership before delivery."
    )
    page.locator("#human-action-submit").click()
    expect(page.get_by_text("Recompile", exact=True)).to_be_visible()
    page.locator('[data-direct-action="repair_authority"]').click()
    expect(page.get_by_text("Exact Authority review packet")).to_be_visible()
    _accept_review(page, "authority")
    expect(page.get_by_text("Authority accepted")).to_be_visible()

    _assert_human_only_surface(page)
    _assert_no_horizontal_overflow(page)
    assert fake.api_errors == []
    context.close()


def test_mobile_dirty_repository_wraps_without_overflow(
    dashboard_harness: DashboardHarness,
    tmp_path: Path,
) -> None:
    """Verify dirty repository provenance at 390 by 844."""
    repository_path = tmp_path / "mobile-repository-with-a-long-local-name"
    repository = _repository_fixture(repository_path, dirty=True)
    fake = FakeLifecycle(repositories={str(repository_path): repository})
    context = dashboard_harness.browser.new_context(viewport=_MOBILE_VIEWPORT)
    context.route("**/api/**", fake.handle)
    page = context.new_page()
    page.goto(dashboard_harness.url, wait_until="networkidle")

    _create_project(
        page,
        name="Mobile Lifecycle",
        description="Narrow viewport repository verification.",
        repository_path=str(repository_path),
    )
    expect(page.get_by_role("button", name="Generate Vision draft")).to_be_visible()
    expect(page.get_by_text("Dirty", exact=True)).to_be_visible()
    warning = page.get_by_text(
        "Working tree has uncommitted changes in a deliberately long nested "
        "source path that must wrap on narrow screens."
    )
    expect(warning).to_be_visible()
    expect(
        page.locator("#repository-panel").get_by_text(str(repository_path))
    ).to_be_visible()
    stage_before_refresh = page.locator("#lifecycle-stage-strip").inner_text()
    page.locator('[data-repository-action="refresh"]').click()
    page.wait_for_timeout(_UI_SETTLE_MS)
    assert fake.refresh_count == 1
    assert page.locator("#lifecycle-stage-strip").inner_text() == stage_before_refresh

    _assert_human_only_surface(page)
    _assert_no_horizontal_overflow(page)
    page.locator("#repository-panel").scroll_into_view_if_needed()
    _assert_no_control_overlap(page)
    screenshot = tmp_path / "mobile-390x844.png"
    page.screenshot(path=screenshot, full_page=False, animations="disabled")
    _assert_screenshot_size(screenshot, _MOBILE_VIEWPORT)
    assert fake.api_errors == []
    context.close()
