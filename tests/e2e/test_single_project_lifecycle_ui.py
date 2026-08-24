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

    from playwright.sync_api import Browser, BrowserContext, Locator, Page, Route
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
    "decide_backlog": "backlog/decide",
    "decide_product_goal_review": "goals/review",
    "decide_roadmap": "roadmap/decide",
    "decide_specification": "specifications/review",
    "decide_sprint_plan": "sprint/decide",
    "decide_story": "story/decide",
    "decide_vision_review": "vision/review",
    "generate_vision_bootstrap": "vision/bootstrap",
    "record_backlog_draft": "backlog/generate",
    "record_product_goal_interview_turn": "goals/respond",
    "record_roadmap_draft": "roadmap/generate",
    "record_sprint_plan": "sprint/generate",
    "record_story_draft": "story/generate",
    "register_specification_source": "specifications/source",
    "record_vision_interview_turn": "vision/respond",
    "structure_specification": "specifications/structure",
}

_ACTION_CHILDREN = {
    "decide_backlog": "backlog",
    "decide_product_goal_review": "product_goal",
    "decide_roadmap": "planning",
    "decide_specification": "specification",
    "decide_sprint_plan": "planning",
    "decide_story": "planning",
    "decide_vision_review": "vision",
    "generate_vision_bootstrap": "vision",
    "record_backlog_draft": "backlog",
    "record_product_goal_interview_turn": "product_goal",
    "record_roadmap_draft": "planning",
    "record_sprint_plan": "planning",
    "record_story_draft": "planning",
    "register_specification_source": "specification",
    "record_vision_interview_turn": "vision",
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
    backlog_candidate: JsonObject | None = None
    backlog_accepted: bool = False
    backlog_decision_fingerprint: str = "sha256:hidden-backlog-decision"
    roadmap_candidate: JsonObject | None = None
    roadmap_accepted: bool = False
    roadmap_decision_fingerprint: str = "sha256:hidden-roadmap-decision"
    story_candidate: JsonObject | None = None
    story_accepted: bool = False
    story_decision_fingerprint: str = "sha256:hidden-story-decision"
    sprint_plan_candidate: JsonObject | None = None
    sprint_plan_accepted: bool = False
    sprint_plan_decision_fingerprint: str = "sha256:hidden-sprint-decision"
    delivery_generation_failure: str | None = None
    delivery_requests: list[tuple[str, JsonObject]] = field(default_factory=list)
    planning_review_overrides: dict[str, JsonObject] = field(default_factory=dict)
    story_pending_override: JsonObject | None = None
    position_override: JsonObject | None = None
    on_story_decide: Callable[[JsonObject], None] | None = None
    story_decisions: list[JsonObject] = field(default_factory=list)
    stories: list[JsonValue] = field(default_factory=list)
    story_dependencies: list[JsonValue] = field(default_factory=list)
    sprint_candidates: list[JsonValue] = field(default_factory=list)
    story_validation_requests: list[JsonObject] = field(default_factory=list)
    story_validation_failure: JsonObject | None = None
    dependency_apply_requests: list[JsonObject] = field(default_factory=list)
    reject_stale_delivery_actions: bool = False
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

    def _planning_review_data(
        self,
        suffix: str,
    ) -> JsonObject | None:
        if suffix in self.planning_review_overrides:
            return self.planning_review_overrides[suffix]
        if (
            suffix == "/backlog/review"
            and self.backlog_candidate is not None
            and not self.backlog_accepted
        ):
            return {
                "binding": {
                    "decision_fingerprint": (self.backlog_decision_fingerprint),
                    "instance_key": None,
                },
                "review": {
                    "phase": "backlog",
                    "candidate": self.backlog_candidate,
                },
            }
        if (
            suffix == "/roadmap/review"
            and self.roadmap_candidate is not None
            and not self.roadmap_accepted
        ):
            return {
                "binding": {
                    "decision_fingerprint": (self.roadmap_decision_fingerprint),
                    "instance_key": None,
                },
                "review": {
                    "phase": "roadmap",
                    "candidate": self.roadmap_candidate,
                },
            }
        if (
            suffix == "/story/reviews"
            and self.story_candidate is not None
            and not self.story_accepted
        ):
            return {
                "items": [
                    {
                        "binding": {
                            "decision_fingerprint": (self.story_decision_fingerprint),
                            "instance_key": "backlog_item:PBI-000001",
                        },
                        "review": {
                            "phase": "story",
                            "lineage": {
                                "backlog_item": {
                                    "backlog_item_id": "PBI-000001",
                                    "requirement": ("Delivery workflow requirement"),
                                    "priority": "high",
                                    "value_driver": "core",
                                    "estimated_effort": "medium",
                                    "justification": ("Core delivery item."),
                                }
                            },
                            "candidate": self.story_candidate,
                        },
                    }
                ]
            }
        if (
            suffix == "/sprint/plan/review"
            and self.sprint_plan_candidate is not None
            and not self.sprint_plan_accepted
        ):
            return {
                "binding": {
                    "decision_fingerprint": (self.sprint_plan_decision_fingerprint),
                    "instance_key": None,
                },
                "review": {
                    "phase": "sprint_plan",
                    "candidate": self.sprint_plan_candidate,
                },
            }
        return None

    def _planning_review_response(
        self,
        suffix: str,
    ) -> tuple[int, JsonObject] | None:
        if suffix not in {
            "/backlog/review",
            "/roadmap/review",
            "/story/reviews",
            "/sprint/plan/review",
        }:
            return None
        data = self._planning_review_data(suffix)
        if data is not None:
            return _HTTP_OK, self._success(data)
        return (
            _HTTP_CONFLICT,
            {
                "detail": {
                    "errors": [
                        {
                            "code": "PLANNING_REVIEW_NOT_AVAILABLE",
                            "message": "No planning review is current.",
                        }
                    ]
                }
            },
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
            planning_response = self._planning_review_response(suffix)
            if planning_response is not None:
                return planning_response
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
            "/goals/status": self._goal_projection,
            "/repository": self._repository_projection,
            "/specifications/review": self._specification_projection,
            "/story/pending": self._story_pending_projection,
            "/story/dependencies": self._story_dependencies_projection,
            "/sprint/candidates": self._sprint_candidates_projection,
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
        if suffix in {
            "/backlog/generate",
            "/roadmap/generate",
            "/story/generate",
            "/sprint/generate",
        }:
            self.delivery_requests.append((suffix, dict(body)))
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
        handlers: dict[str, Callable[[JsonObject], JsonObject | None]] = {
            "/backlog/decide": self._decide_backlog,
            "/backlog/generate": self._generate_backlog,
            "/goals/respond": self._record_goal_turn,
            "/goals/review": self._review_goal,
            "/repository": self._attach_repository,
            "/repository/refresh": self._refresh_repository,
            "/roadmap/decide": self._decide_roadmap,
            "/roadmap/generate": self._generate_roadmap,
            "/specifications/source": self._register_specification_source,
            "/specifications/structure": self._structure_specification,
            "/specifications/review": self._review_specification,
            "/sprint/decide": self._decide_sprint_plan,
            "/sprint/generate": self._generate_sprint_plan,
            "/story/decide": self._decide_story,
            "/story/dependencies/apply": self._apply_story_dependencies,
            "/story/generate": self._generate_story,
            "/story/validate": self._validate_story,
            "/vision/bootstrap": self._bootstrap_vision,
            "/vision/respond": self._record_vision_turn,
            "/vision/review": self._review_vision,
        }
        handler = handlers.get(suffix)
        assert handler is not None, f"Unexpected mutation path: {suffix}"
        result = handler(body)
        return _HTTP_OK, result if result is not None else self._mutation_result()

    def _review_fingerprint(self, suffix: str) -> str | None:
        candidates = {
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

    def _repository_projection(self) -> JsonObject:
        return {"repository": self.repository}

    def _story_pending_projection(self) -> JsonObject:
        if self.story_pending_override is not None:
            return self.story_pending_override
        raw_items = (
            self.backlog_candidate.get("backlog_items")
            if isinstance(self.backlog_candidate, dict)
            else None
        )
        items: list[JsonValue] = (
            [
                {
                    "backlog_item_id": item.get("backlog_item_id", ""),
                    "requirement": item.get("requirement", ""),
                    "status": "pending",
                }
                for item in raw_items
                if isinstance(item, dict)
            ]
            if isinstance(raw_items, list)
            else []
        )
        return {
            "items": items,
            "count": len(items),
            "pending_count": len(items),
        }

    def _story_dependencies_projection(self) -> JsonObject:
        return {
            "stories": self.stories,
            "edges": self.story_dependencies,
        }

    def _sprint_candidates_projection(self) -> JsonObject:
        return {
            "items": self.sprint_candidates,
            "count": len(self.sprint_candidates),
        }

    def _generate_backlog(self, body: JsonObject) -> None:
        self._assert_fields(body, set())
        if self.delivery_generation_failure:
            raise ValueError(self.delivery_generation_failure)
        self.backlog_candidate = {
            "backlog_items": [
                {
                    "backlog_item_id": "PBI-000001",
                    "requirement": "Delivery generation workflow",
                    "priority": "high",
                    "value_driver": "core",
                    "estimated_effort": "medium",
                    "justification": "Required for delivery progress.",
                }
            ],
            "is_complete": True,
            "clarifying_questions": [],
        }

    def _decide_backlog(self, body: JsonObject) -> None:
        self._assert_fields(body, {"decision", "rationale"})
        assert body["decision"] == "accepted"
        self.backlog_accepted = True

    def _generate_roadmap(self, body: JsonObject) -> None:
        self._assert_fields(body, set())
        if self.delivery_generation_failure:
            raise ValueError(self.delivery_generation_failure)
        self.roadmap_candidate = {
            "roadmap_summary": "Delivery Roadmap Summary",
            "roadmap_releases": [
                {
                    "release_name": "Release 1",
                    "theme": "Foundations",
                    "focus_area": "Core Engine",
                    "reasoning": "Initial MVP",
                    "backlog_items": [
                        {
                            "requirement": "Delivery generation workflow",
                            "priority": "high",
                            "value_driver": "core",
                            "estimated_effort": "medium",
                            "justification": "Required for delivery progress.",
                        }
                    ],
                }
            ],
            "is_complete": True,
            "clarifying_questions": [],
        }

    def _decide_roadmap(self, body: JsonObject) -> None:
        self._assert_fields(body, {"decision", "rationale"})
        assert body["decision"] == "accepted"
        self.roadmap_accepted = True

    def _generate_story(self, body: JsonObject) -> None:
        self._assert_fields(body, {"instance_key"})
        if self.reject_stale_delivery_actions and self.position_override is not None:
            actions = self.position_override.get("_actions")
            assert isinstance(actions, list)
            current_instances = {
                action.get("instance_key")
                for action in actions
                if isinstance(action, dict)
                and action.get("request_kind") == "record_story_draft"
            }
            if body["instance_key"] not in current_instances:
                message = (
                    "This delivery action changed. Reload the current Story actions."
                )
                raise ValueError(message)
        if self.delivery_generation_failure:
            raise ValueError(self.delivery_generation_failure)
        self.story_candidate = {
            "story_items": [
                {
                    "story_title": "Delivery story draft",
                    "statement": "As an operator, I want delivery generation.",
                    "persona": "Operator",
                    "acceptance_criteria": ["Controls render when available."],
                    "specification_evidence": [],
                }
            ],
            "is_complete": True,
            "clarifying_questions": [],
        }

    def _decide_story(self, body: JsonObject) -> None:
        self._assert_fields(body, {"decision", "rationale"})
        decision = body["decision"]
        assert decision in {"accepted", "feedback", "rejected"}
        if decision == "accepted":
            self.story_accepted = True
        elif decision == "feedback":
            self.story_feedback = cast("str", body.get("rationale"))
        elif decision == "rejected":
            self.story_rejected = True
        self.story_decisions.append(dict(body))
        if self.on_story_decide is not None:
            self.on_story_decide(body)
        else:
            self.planning_review_overrides.pop("/story/reviews", None)

    def _validate_story(self, body: JsonObject) -> JsonObject:
        self._assert_fields(body, {"story_id", "mode"})
        self.story_validation_requests.append(dict(body))
        story_id = body["story_id"]
        if self.story_validation_failure:
            failure = self.story_validation_failure
            for s in self.stories:
                if isinstance(s, dict) and s.get("story_id") == story_id:
                    s["validation_status"] = "failed"
                    s["validation_failures"] = [failure]
                    s["readiness_blockers"] = ["STORY_VALIDATION_REQUIRED"]
                    s["sprint_candidate"] = False
            return {
                "status": "success",
                "data": {
                    "success": True,
                    "ready_for_sprint": False,
                    "story_id": story_id,
                    "mode": body["mode"],
                    "structural_failures": [failure],
                    "structural_warnings": [],
                    "semantic_review_state": "not_requested",
                    "semantic_findings": [],
                    "validation_evidence": None,
                },
                "warnings": [],
            }
        for s in self.stories:
            if isinstance(s, dict) and s.get("story_id") == story_id:
                s["readiness_blockers"] = []
                s["sprint_candidate"] = True
                s["validation_status"] = "validated"
                s["validation_failures"] = []
                if s not in self.sprint_candidates:
                    self.sprint_candidates.append(s)
        return {
            "status": "success",
            "data": {
                "success": True,
                "ready_for_sprint": True,
                "story_id": story_id,
                "mode": body["mode"],
                "structural_failures": [],
                "structural_warnings": [],
                "semantic_review_state": "not_requested",
                "semantic_findings": [],
                "validation_evidence": {"ready_for_sprint": True},
            },
            "warnings": [],
        }

    def _apply_story_dependencies(self, body: JsonObject) -> None:
        self._assert_fields(body, {"selected_story_ids", "reviewed_edges"})
        reviewed_edges = body.get("reviewed_edges")
        assert isinstance(reviewed_edges, list)
        for edge in reviewed_edges:
            assert isinstance(edge, dict)
            assert set(edge.keys()) == {
                "dependent_story_id",
                "prerequisite_story_id",
                "reason",
            }
        self.dependency_apply_requests.append(dict(body))

    def _generate_sprint_plan(self, body: JsonObject) -> None:
        self._assert_fields(body, {"team_name"})
        if self.delivery_generation_failure:
            raise ValueError(self.delivery_generation_failure)
        self.sprint_plan_candidate = {
            "team_name": body["team_name"],
            "sprint_goal": "Deliver Sprint 1 MVP",
            "selected_stories": [
                {
                    "story_title": "Delivery story draft",
                    "statement": "As an operator, I want delivery generation.",
                    "persona": "Operator",
                    "acceptance_criteria": ["Controls render when available."],
                    "tasks": [
                        {
                            "description": "Expose dashboard actions",
                            "task_kind": "implementation",
                            "checklist_items": ["Add buttons"],
                        }
                    ],
                }
            ],
        }

    def _decide_sprint_plan(self, body: JsonObject) -> None:
        self._assert_fields(body, {"decision", "rationale"})
        assert body["decision"] == "accepted"
        self.sprint_plan_accepted = True

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
                self.specification_feedback is not None or self.specification is None,
                "structure_specification",
            ),
            (not self.specification_accepted, "decide_specification"),
            (self.backlog_candidate is None, "record_backlog_draft"),
            (not self.backlog_accepted, "decide_backlog"),
            (self.roadmap_candidate is None, "record_roadmap_draft"),
            (not self.roadmap_accepted, "decide_roadmap"),
            (self.story_candidate is None, "record_story_draft"),
            (not self.story_accepted, "decide_story"),
            (self.sprint_plan_candidate is None, "record_sprint_plan"),
            (not self.sprint_plan_accepted, "decide_sprint_plan"),
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
            "record_backlog_draft": "backlog.generate",
            "decide_backlog": "backlog.review",
            "record_roadmap_draft": "planning.roadmap.generate",
            "decide_roadmap": "planning.roadmap.review",
            "record_story_draft": "planning.story.generate",
            "decide_story": "planning.story.review",
            "record_sprint_plan": "planning.sprint.plan",
            "decide_sprint_plan": "planning.sprint.review",
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
        decision_category = (
            "waiting" if request_kind.startswith("decide_") else "available"
        )
        decision_fingerprint = {
            "decide_backlog": self.backlog_decision_fingerprint,
            "decide_roadmap": self.roadmap_decision_fingerprint,
            "decide_story": self.story_decision_fingerprint,
            "decide_sprint_plan": self.sprint_plan_decision_fingerprint,
        }.get(request_kind, self.specification_structure_decision_fingerprint)
        instance_key = "backlog_item:PBI-000001" if "story" in request_kind else None
        decision: JsonObject = {
            "node_id": node_id,
            "child_graph_id": child,
            "request_kind": request_kind,
            "category": decision_category,
            "instance_key": instance_key,
            "reason_code": specification_reason or "INTERNAL_REASON_CODE",
            "decision_fingerprint": decision_fingerprint,
            "fact_references": fact_references,
        }
        decisions: list[JsonValue] = [decision]
        return {
            "graph_version": "agileforge.workflow.hidden",
            "fact_fingerprint": "sha256:hidden-facts",
            "decisions": decisions,
            "terminal": False,
            "actions": [],
        } | {
            "_actions": [
                {
                    "node_id": decision["node_id"],
                    "instance_key": instance_key,
                    "request_kind": request_kind,
                    "endpoint": endpoint,
                    "transport": "semantic",
                }
            ]
        }

    def position_envelope(self) -> JsonObject:
        """Return position data and advertised actions in the HTTP shape."""
        projection = (
            dict(self.position_override)
            if self.position_override is not None
            else self._position_projection()
        )
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
    assert (
        page.evaluate(
            "window.issue204Requests[0].headers['x-agileforge-expected-decision']"
        )
        == "sha256:hidden-decision"
    )

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
    expect(status).to_contain_text("The prior candidate and Feedback remain current.")
    expect(page.get_by_text("Prior Feedback candidate", exact=False)).to_be_visible()
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

    expect(page.get_by_text("Successor pending candidate", exact=False)).to_be_visible()
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
    _attach_and_refresh_repository(page, fake, repository_path)

    _assert_human_only_surface(page)
    _assert_no_horizontal_overflow(page)
    page.locator("#delivery-panel").scroll_into_view_if_needed()
    _assert_no_control_overlap(page)
    screenshot = tmp_path / "desktop-1440x900.png"
    page.screenshot(path=screenshot, full_page=False, animations="disabled")
    _assert_screenshot_size(screenshot, _DESKTOP_VIEWPORT)
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


_PROJECT_HTML = Path("frontend/project.html")
_PROJECT_JS = Path("frontend/project.js")


def test_single_project_dashboard_uses_direct_specification_lifecycle() -> None:
    """Keep the direct human lifecycle stages in their visible order."""
    source = _PROJECT_JS.read_text(encoding="utf-8")

    expected = (
        "Vision",
        "Product Goal",
        "Specification",
        "Backlog",
        "Roadmap",
        "Stories",
        "Sprint",
        "Execution",
        "Review",
    )
    positions = [source.index(f"'{stage}'") for stage in expected]
    assert positions == sorted(positions)


def test_single_project_dashboard_loads_every_exact_planning_review() -> None:
    """Load every dedicated planning review surface for one Project."""
    source = _PROJECT_JS.read_text(encoding="utf-8")

    for endpoint in (
        "/backlog/review",
        "/roadmap/review",
        "/story/reviews",
        "/sprint/plan/review",
    ):
        assert endpoint in source


def test_review_evidence_is_rendered_before_decision_controls() -> None:
    """Keep human evidence before planning decision controls."""
    source = _PROJECT_JS.read_text(encoding="utf-8")
    function = source[source.index("function planningReviewCardMarkup") :]

    assert function.index("${content}") < function.index("data-planning-review=")
    assert "escapeWorkflowText" in function
    assert "planningReviewContentMarkup" in function
    assert "specificationEvidenceMarkup" in source
    assert "<pre" not in function
    assert "visiblePlanningReview" not in source


def test_dashboard_sends_machine_binding_outside_semantic_body() -> None:
    """Keep review guard values in headers instead of semantic JSON."""
    source = _PROJECT_JS.read_text(encoding="utf-8")

    assert "X-AgileForge-Expected-Decision" in source
    assert "X-AgileForge-Expected-Instance" in source
    assert "body: JSON.stringify(semanticMutationPayload(fields))" in source


def test_dashboard_live_surface_has_no_retired_stage_or_copy() -> None:
    """Keep removed lifecycle terminology out of the live dashboard surface."""
    source = (
        _PROJECT_HTML.read_text(encoding="utf-8")
        + _PROJECT_JS.read_text(encoding="utf-8")
    ).casefold()

    assert "auth" + "ority" not in source
    assert "invar" + "iant" not in source


def _verify_backlog_lifecycle_flow(page: Page, fake: FakeLifecycle) -> None:
    backlog_card = page.locator('[data-lifecycle-card="Backlog"]')
    expect(backlog_card).to_contain_text("Ready")
    expect(backlog_card).to_contain_text("Ready for your input.")

    generate_backlog_btn = page.locator('[data-direct-action="record_backlog_draft"]')
    expect(generate_backlog_btn).to_be_visible()
    expect(generate_backlog_btn).to_contain_text("Generate Backlog")

    fake.delivery_generation_failure = "Transient generator timeout."
    generate_backlog_btn.click()
    page.wait_for_timeout(_UI_SETTLE_MS)
    expect(page.locator("#project-error")).to_contain_text(
        "Transient generator timeout."
    )
    status_locator = page.locator('[data-delivery-action-status="true"]')
    expect(status_locator).to_be_visible()
    expect(status_locator).to_contain_text("Transient generator timeout.")
    expect(generate_backlog_btn).to_be_enabled()

    fake.delivery_generation_failure = None
    generate_backlog_btn.click()
    review_card = page.locator('[data-planning-review-card="backlog"]')
    expect(review_card).to_be_visible()
    expect(review_card).to_contain_text("Backlog review")
    expect(review_card).to_contain_text("Delivery generation workflow")

    page.locator(
        '[data-planning-review="backlog"][data-review-decision="accepted"]'
    ).click()
    expect(page.locator("#human-action-dialog")).to_be_visible()
    page.locator("#human-action-submit").click()
    expect(page.locator("#human-action-dialog")).not_to_be_visible()
    page.wait_for_timeout(_UI_SETTLE_MS)


def _verify_roadmap_lifecycle_flow(page: Page) -> None:
    roadmap_card = page.locator('[data-lifecycle-card="Roadmap"]')
    expect(roadmap_card).to_contain_text("Ready")
    generate_roadmap_btn = page.locator('[data-direct-action="record_roadmap_draft"]')
    expect(generate_roadmap_btn).to_be_visible()
    expect(generate_roadmap_btn).to_contain_text("Generate Roadmap")

    generate_roadmap_btn.click()
    review_card = page.locator('[data-planning-review-card="roadmap"]')
    expect(review_card).to_be_visible()
    expect(review_card).to_contain_text("Roadmap review")

    page.locator(
        '[data-planning-review="roadmap"][data-review-decision="accepted"]'
    ).click()
    expect(page.locator("#human-action-dialog")).to_be_visible()
    page.locator("#human-action-submit").click()
    expect(page.locator("#human-action-dialog")).not_to_be_visible()
    page.wait_for_timeout(_UI_SETTLE_MS)


def _verify_story_lifecycle_flow(page: Page) -> None:
    stories_card = page.locator('[data-lifecycle-card="Stories"]')
    expect(stories_card).to_contain_text("Ready")
    generate_story_btn = page.locator('[data-direct-action="record_story_draft"]')
    expect(generate_story_btn).to_be_visible()
    expect(generate_story_btn).to_contain_text("Generate Stories")

    generate_story_btn.click()
    expect(page.locator("#human-action-dialog")).to_be_visible()
    page.locator("#human-action-submit").click()
    expect(page.locator("#human-action-dialog")).not_to_be_visible()
    page.wait_for_timeout(_UI_SETTLE_MS)

    review_card = page.locator('[data-planning-review-card="story"]')
    expect(review_card).to_be_visible()
    expect(review_card).to_contain_text("Story review")

    page.locator(
        '[data-planning-review="story"][data-review-decision="accepted"]'
    ).click()
    expect(page.locator("#human-action-dialog")).to_be_visible()
    page.locator("#human-action-submit").click()
    expect(page.locator("#human-action-dialog")).not_to_be_visible()
    page.wait_for_timeout(_UI_SETTLE_MS)


def _verify_sprint_lifecycle_flow(page: Page, fake: FakeLifecycle) -> None:
    sprint_card = page.locator('[data-lifecycle-card="Sprint"]')
    expect(sprint_card).to_contain_text("Ready")
    sprint_form = page.locator('[data-delivery-generation-form="record_sprint_plan"]')
    team_name = sprint_form.locator('[name="team_name"]')
    expect(team_name).to_be_visible()
    expect(team_name).to_have_attribute("required", "")
    team_name.fill("Delivery Team")
    generate_sprint_btn = sprint_form.locator('button[type="submit"]')
    expect(generate_sprint_btn).to_be_visible()
    expect(generate_sprint_btn).to_contain_text("Generate Sprint plan")

    generate_sprint_btn.click()
    review_card = page.locator('[data-planning-review-card="sprint"]')
    expect(review_card).to_be_visible()
    expect(review_card).to_contain_text("Sprint plan review")
    expect(review_card).to_contain_text("Delivery Team")
    assert fake.delivery_requests[-1][1]["team_name"] == "Delivery Team"


def test_issue_212_delivery_generation_lifecycle_flow(
    dashboard_harness: DashboardHarness,
    tmp_path: Path,
) -> None:
    """Verify delivery generation flow from Backlog through Sprint plan."""
    repository_path = tmp_path / "delivery-repository"
    repository = _repository_fixture(repository_path, dirty=False)
    fake = FakeLifecycle(repositories={str(repository_path): repository})
    context = dashboard_harness.browser.new_context(viewport=_DESKTOP_VIEWPORT)
    context.route("**/api/**", fake.handle)
    page = context.new_page()
    page.goto(dashboard_harness.url, wait_until="networkidle")

    _create_project(
        page,
        name="Delivery Lifecycle Pilot",
        description="One product definition and delivery generation lifecycle.",
        repository_path=None,
    )
    _complete_vision_and_goal(
        page,
        fake,
        replace_vision_during_review=False,
    )
    _record_and_review_definition(page)

    _verify_backlog_lifecycle_flow(page, fake)
    _verify_roadmap_lifecycle_flow(page)
    _verify_story_lifecycle_flow(page)
    _verify_sprint_lifecycle_flow(page, fake)

    context.close()


def _story_generation_action(instance_key: str) -> JsonObject:
    return {
        "node_id": "planning.story.generate",
        "instance_key": instance_key,
        "request_kind": "record_story_draft",
        "endpoint": "story/generate",
        "transport": "semantic",
    }


def _sprint_generation_action() -> JsonObject:
    return {
        "node_id": "planning.sprint.plan",
        "instance_key": None,
        "request_kind": "record_sprint_plan",
        "endpoint": "sprint/generate",
        "transport": "semantic",
    }


def _delivery_position(actions: list[JsonObject]) -> JsonObject:
    decisions: list[JsonValue] = [
        {
            "node_id": action["node_id"],
            "child_graph_id": "planning",
            "request_kind": action["request_kind"],
            "category": "available",
            "instance_key": action["instance_key"],
            "reason_code": "DELIVERY_GENERATION_AVAILABLE",
            "decision_fingerprint": f"decision-{index}",
            "fact_references": [],
        }
        for index, action in enumerate(actions)
    ]
    return {
        "graph_version": "agileforge.workflow.hidden",
        "fact_fingerprint": "sha256:hidden-facts",
        "decisions": decisions,
        "terminal": False,
        "actions": [],
        "_actions": [dict(action) for action in actions],
    }


def _story_review(instance_key: str) -> JsonObject:
    return {
        "binding": {
            "decision_fingerprint": f"decision-{instance_key}",
            "instance_key": instance_key,
        },
        "review": {
            "phase": "story",
            "lineage": {
                "backlog_item": {
                    "requirement": "Keep Story actions bound to their backlog item.",
                    "priority": "high",
                    "value_driver": "correctness",
                    "estimated_effort": "medium",
                    "justification": "The operator must select the intended item.",
                }
            },
            "candidate": {
                "story_items": [
                    {
                        "story_title": "Exact Story review",
                        "statement": "As an operator, I review the intended Story.",
                        "persona": "Operator",
                        "acceptance_criteria": ["The selector remains exact."],
                        "specification_evidence": [],
                    }
                ],
                "is_complete": True,
                "clarifying_questions": [],
            },
        },
    }


def _delivery_ready_fake(actions: list[JsonObject]) -> FakeLifecycle:
    fake = FakeLifecycle(repositories={})
    fake.project = {
        "project_id": _PROJECT_ID,
        "name": "Delivery action contract",
        "description": "Provider-free browser fixture.",
        "user_stories_count": 0,
        "sprint_count": 0,
    }
    fake.vision_candidate = {
        "statement": "Expose exact delivery actions.",
        "review_fingerprint": "sha256:hidden-vision",
    }
    fake.vision_accepted = True
    fake.goal_candidate = {
        "statement": "Keep each operator action explicit.",
        "fingerprint": "sha256:hidden-goal",
    }
    fake.goal_accepted = True
    fake.specification_source = {
        "specification_source_id": 31,
        "source_fingerprint": "sha256:hidden-source",
        "producer_capability": "to-spec",
        "preparation_capability": "grill-with-docs",
        "context": {"state": "absent", "document": None},
    }
    fake.specification = {
        "title": "Delivery action contract",
        "rendered_markdown": "# Delivery action contract",
    }
    fake.specification_accepted = True
    fake.backlog_candidate = {
        "backlog_items": [
            {
                "backlog_item_id": "PBI-000001",
                "requirement": "Persist exact delivery lineage.",
            },
            {
                "backlog_item_id": "PBI-000002",
                "requirement": "Keep Story actions bound to their backlog item.",
            },
            {
                "backlog_item_id": "PBI-000003",
                "requirement": "Verify fresh retry after stale action rejection.",
            },
            {
                "backlog_item_id": "PBI-000004",
                "requirement": "Provide CLI calculation interface.",
            },
        ]
    }
    fake.position_override = _delivery_position(actions)
    return fake


def _open_project_page(
    dashboard_harness: DashboardHarness,
    fake: FakeLifecycle,
) -> tuple[BrowserContext, Page]:
    context = dashboard_harness.browser.new_context(viewport=_DESKTOP_VIEWPORT)
    context.route("**/api/**", fake.handle)
    page = context.new_page()
    page.goto(
        f"{dashboard_harness.url}/project.html?id={_PROJECT_ID}",
        wait_until="networkidle",
    )
    return context, page


def test_story_generation_rejects_stale_rendered_selector_and_retries_fresh(
    dashboard_harness: DashboardHarness,
) -> None:
    """Bind each Story control to its rendered action, then reconcile stale state."""
    first = _story_generation_action("backlog_item:PBI-000001")
    stale = _story_generation_action("backlog_item:PBI-000002")
    fresh = _story_generation_action("backlog_item:PBI-000003")
    fake = _delivery_ready_fake([first, stale])
    fake.reject_stale_delivery_actions = True
    context, page = _open_project_page(dashboard_harness, fake)

    stale_control = page.locator(
        '[data-delivery-action-instance="backlog_item:PBI-000002"] button'
    )
    expect(stale_control).to_be_visible()
    fake.position_override = _delivery_position([first, fresh])

    stale_control.click()
    expect(page.locator("#human-action-dialog")).to_be_visible()
    page.locator("#human-action-submit").click()
    expect(page.locator("#human-action-dialog")).not_to_be_visible()
    page.wait_for_timeout(_UI_SETTLE_MS)
    assert fake.delivery_requests[-1][1]["instance_key"] == stale["instance_key"]
    assert fake.api_errors[-1] == (
        "This delivery action changed. Reload the current Story actions."
    )
    expect(page.locator("#project-error")).to_contain_text(
        "This delivery action changed."
    )
    expect(stale_control).not_to_be_attached()

    fresh_action = page.locator(
        '[data-delivery-action-instance="backlog_item:PBI-000003"]'
    )
    fresh_status = fresh_action.locator('[data-delivery-action-status="true"]')
    expect(fresh_status).to_be_hidden()
    expect(fresh_status).to_have_text("")
    fresh_control = fresh_action.locator("button")
    expect(fresh_control).to_be_visible()
    fresh_control.click()
    expect(page.locator("#human-action-dialog")).to_be_visible()
    page.locator("#human-action-submit").click()
    expect(page.locator("#human-action-dialog")).not_to_be_visible()
    page.wait_for_timeout(_UI_SETTLE_MS)
    assert fake.delivery_requests[-1][1]["instance_key"] == fresh["instance_key"]

    context.close()


def _non_contiguous_story_position(
    action_pbi2: JsonObject,
    action_pbi4: JsonObject,
) -> JsonObject:
    return {
        "graph_version": "agileforge.workflow.hidden",
        "fact_fingerprint": "sha256:hidden-facts",
        "decisions": [
            {
                "node_id": action_pbi2["node_id"],
                "child_graph_id": "planning",
                "request_kind": action_pbi2["request_kind"],
                "category": "available",
                "instance_key": action_pbi2["instance_key"],
                "reason_code": "STORY_GENERATION_REQUIRED",
                "recommendation_kind": "required",
                "decision_fingerprint": "decision-pbi2",
                "fact_references": [],
            },
            {
                "node_id": action_pbi4["node_id"],
                "child_graph_id": "planning",
                "request_kind": action_pbi4["request_kind"],
                "category": "available",
                "instance_key": action_pbi4["instance_key"],
                "reason_code": "STORY_CORRECTION_AVAILABLE",
                "recommendation_kind": "optional_reentry",
                "decision_fingerprint": "decision-pbi4",
                "fact_references": [],
            },
        ],
        "terminal": False,
        "actions": [],
        "_actions": [dict(action_pbi2), dict(action_pbi4)],
    }


def _non_contiguous_story_pending() -> JsonObject:
    return {
        "items": [
            {
                "backlog_item_id": "PBI-000001",
                "requirement": "Persist exact delivery lineage.",
                "status": "accepted",
            },
            {
                "backlog_item_id": "PBI-000002",
                "requirement": "Support accepted Number List language.",
                "status": "pending",
            },
            {
                "backlog_item_id": "PBI-000003",
                "requirement": "Reject negative numeric values.",
                "status": "pending_review",
            },
            {
                "backlog_item_id": "PBI-000004",
                "requirement": "Provide the installed CLI.",
                "status": "pending",
            },
        ],
        "count": 4,
        "pending_count": 3,
    }


def _verify_dialog_content(
    page: Page,
    *,
    title: str,
    description: str,
    submit_label: str,
) -> Locator:
    dialog = page.locator("#human-action-dialog")
    expect(dialog).to_be_visible()
    expect(page.locator("#human-action-title")).to_have_text(title)
    expect(page.locator("#human-action-description")).to_have_text(description)
    expect(page.locator("#human-action-submit")).to_have_text(submit_label)
    return dialog


def test_story_generation_non_contiguous_labels_intent_confirmation_and_reconciliation(
    dashboard_harness: DashboardHarness,
) -> None:
    """Verify non-contiguous labels and immediate accept reconciliation."""
    action_pbi2 = _story_generation_action("backlog_item:PBI-000002")
    action_pbi4 = _story_generation_action("backlog_item:PBI-000004")
    actions = [action_pbi2, action_pbi4]

    fake = _delivery_ready_fake(actions)
    fake.position_override = _non_contiguous_story_position(action_pbi2, action_pbi4)
    fake.story_pending_override = _non_contiguous_story_pending()
    fake.planning_review_overrides["/story/reviews"] = {
        "items": [_story_review("backlog_item:PBI-000003")]
    }

    context, page = _open_project_page(dashboard_harness, fake)

    # 1. Verify Non-contiguous labels and review card header
    review_card = page.locator('[data-planning-review-card="story"]')
    expect(review_card).to_be_visible()
    expect(review_card).to_contain_text("Story review for PBI-000003")

    pbi2_btn = page.locator(
        '[data-delivery-action-instance="backlog_item:PBI-000002"] button'
    )
    expect(pbi2_btn).to_be_visible()
    expect(pbi2_btn).to_contain_text(
        "Generate Stories for PBI-000002: Support accepted Number List language."
    )

    pbi4_btn = page.locator(
        '[data-delivery-action-instance="backlog_item:PBI-000004"] button'
    )
    expect(pbi4_btn).to_be_visible()
    expect(pbi4_btn).to_contain_text(
        "Correct Stories for PBI-000004: Provide the installed CLI."
    )

    # Ensure no array ordinals appear in delivery panel
    delivery_panel = page.locator("#delivery-panel")
    expect(delivery_panel).not_to_contain_text("backlog item 1")
    expect(delivery_panel).not_to_contain_text("backlog item 2")

    # 2. Verify Confirmation Modal Cancel flow (PBI-000002)
    pbi2_btn.click()
    dialog = _verify_dialog_content(
        page,
        title="Generate Stories for PBI-000002",
        description=(
            "Confirm Story generation for PBI-000002: "
            "Support accepted Number List language."
        ),
        submit_label="Generate Stories",
    )
    page.locator("#human-action-cancel").click()
    expect(dialog).not_to_be_visible()
    assert len(fake.delivery_requests) == 0

    # 3. Verify Confirmation Modal Submit flow for Correction (PBI-000004)
    pbi4_btn.click()
    dialog = _verify_dialog_content(
        page,
        title="Correct Stories for PBI-000004",
        description=(
            "Confirm Story correction for PBI-000004: Provide the installed CLI."
        ),
        submit_label="Correct Stories",
    )
    page.locator("#human-action-submit").click()
    expect(dialog).not_to_be_visible()
    page.wait_for_timeout(_UI_SETTLE_MS)
    assert len(fake.delivery_requests) == 1
    assert fake.delivery_requests[0][0] == "/story/generate"
    assert fake.delivery_requests[0][1]["instance_key"] == "backlog_item:PBI-000004"

    # 4. Verify Immediate Post-Decision Reconciliation (Accepting review for PBI-000003)
    accept_review_btn = page.locator(
        '[data-planning-review="story"][data-review-decision="accepted"]'
    )
    accept_review_btn.click()
    expect(dialog).to_be_visible()
    expect(page.locator("#human-action-title")).to_contain_text(
        "Accept Story review for PBI-000003"
    )
    page.locator("#human-action-submit").click()
    expect(dialog).not_to_be_visible()
    page.wait_for_timeout(_UI_SETTLE_MS)

    # Immediately reconciled: review card gone, PBI-000002 button remains exact
    expect(review_card).not_to_be_attached()
    expect(pbi2_btn).to_be_visible()
    expect(pbi2_btn).to_contain_text(
        "Generate Stories for PBI-000002: Support accepted Number List language."
    )
    assert len(fake.story_decisions) == 1
    assert fake.story_decisions[0]["decision"] == "accepted"

    context.close()


def test_story_generation_feedback_decision_reconciles_to_revision_intent(
    dashboard_harness: DashboardHarness,
) -> None:
    """Verify request-changes decision reconciles immediately to revision intent."""
    action_pbi2 = _story_generation_action("backlog_item:PBI-000002")
    action_pbi4 = _story_generation_action("backlog_item:PBI-000004")
    action_pbi3_rev = _story_generation_action("backlog_item:PBI-000003")
    actions = [action_pbi2, action_pbi4]

    fake = _delivery_ready_fake(actions)
    fake.position_override = _non_contiguous_story_position(action_pbi2, action_pbi4)
    fake.story_pending_override = _non_contiguous_story_pending()
    fake.planning_review_overrides["/story/reviews"] = {
        "items": [_story_review("backlog_item:PBI-000003")]
    }

    def on_decide(_body: JsonObject) -> None:
        fake.planning_review_overrides.pop("/story/reviews", None)
        fake.position_override = {
            "graph_version": "agileforge.workflow.hidden",
            "fact_fingerprint": "sha256:hidden-facts",
            "decisions": [
                {
                    "node_id": action_pbi2["node_id"],
                    "child_graph_id": "planning",
                    "request_kind": action_pbi2["request_kind"],
                    "category": "available",
                    "instance_key": action_pbi2["instance_key"],
                    "reason_code": "STORY_GENERATION_REQUIRED",
                    "recommendation_kind": "required",
                    "decision_fingerprint": "decision-pbi2",
                    "fact_references": [],
                },
                {
                    "node_id": action_pbi3_rev["node_id"],
                    "child_graph_id": "planning",
                    "request_kind": action_pbi3_rev["request_kind"],
                    "category": "available",
                    "instance_key": action_pbi3_rev["instance_key"],
                    "reason_code": "STORY_REVISION_REQUIRED",
                    "recommendation_kind": "required",
                    "decision_fingerprint": "decision-pbi3-rev",
                    "fact_references": [],
                },
            ],
            "terminal": False,
            "actions": [],
            "_actions": [dict(action_pbi2), dict(action_pbi3_rev)],
        }

    fake.on_story_decide = on_decide

    context, page = _open_project_page(dashboard_harness, fake)

    review_card = page.locator('[data-planning-review-card="story"]')
    expect(review_card).to_be_visible()

    # Click Request changes (feedback)
    feedback_btn = page.locator(
        '[data-planning-review="story"][data-review-decision="feedback"]'
    )
    feedback_btn.click()
    dialog = page.locator("#human-action-dialog")
    expect(dialog).to_be_visible()
    expect(page.locator("#human-action-title")).to_contain_text(
        "Request changes for Story review for PBI-000003"
    )
    rationale_input = page.locator("#human-action-rationale")
    expect(rationale_input).to_be_visible()
    rationale_input.fill("Add boundary tests for negative zero.")

    page.locator("#human-action-submit").click()
    expect(dialog).not_to_be_visible()
    page.wait_for_timeout(_UI_SETTLE_MS)

    # Immediately reconciled: review card gone, PBI-000003 is Revision
    expect(review_card).not_to_be_attached()
    assert len(fake.story_decisions) == 1
    assert fake.story_decisions[0]["decision"] == "feedback"
    assert (
        fake.story_decisions[0]["rationale"] == "Add boundary tests for negative zero."
    )

    pbi3_rev_btn = page.locator(
        '[data-delivery-action-instance="backlog_item:PBI-000003"] button'
    )
    expect(pbi3_rev_btn).to_be_visible()
    expect(pbi3_rev_btn).to_contain_text(
        "Revise Stories for PBI-000003: Reject negative numeric values."
    )

    # Clicking Revise opens modal with revision intent
    pbi3_rev_btn.click()
    _verify_dialog_content(
        page,
        title="Revise Stories for PBI-000003",
        description=(
            "Confirm Story revision for PBI-000003: Reject negative numeric values."
        ),
        submit_label="Revise Stories",
    )
    page.locator("#human-action-cancel").click()
    expect(dialog).not_to_be_visible()
    assert len(fake.delivery_requests) == 0

    context.close()


def test_story_generation_reject_decision_reconciles_immediately(
    dashboard_harness: DashboardHarness,
) -> None:
    """Verify reject decision reconciles immediately without manual refresh."""
    action_pbi2 = _story_generation_action("backlog_item:PBI-000002")
    action_pbi4 = _story_generation_action("backlog_item:PBI-000004")
    actions = [action_pbi2, action_pbi4]

    fake = _delivery_ready_fake(actions)
    fake.position_override = _non_contiguous_story_position(action_pbi2, action_pbi4)
    fake.story_pending_override = _non_contiguous_story_pending()
    fake.planning_review_overrides["/story/reviews"] = {
        "items": [_story_review("backlog_item:PBI-000003")]
    }

    context, page = _open_project_page(dashboard_harness, fake)

    review_card = page.locator('[data-planning-review-card="story"]')
    expect(review_card).to_be_visible()

    # Click Reject
    reject_btn = page.locator(
        '[data-planning-review="story"][data-review-decision="rejected"]'
    )
    reject_btn.click()
    dialog = page.locator("#human-action-dialog")
    expect(dialog).to_be_visible()
    expect(page.locator("#human-action-title")).to_contain_text(
        "Reject Story review for PBI-000003"
    )
    rationale_input = page.locator("#human-action-rationale")
    expect(rationale_input).to_be_visible()
    rationale_input.fill("Output does not conform to parent requirement.")

    page.locator("#human-action-submit").click()
    expect(dialog).not_to_be_visible()
    page.wait_for_timeout(_UI_SETTLE_MS)

    # Immediately reconciled without manual refresh: review card gone
    expect(review_card).not_to_be_attached()
    assert len(fake.story_decisions) == 1
    assert fake.story_decisions[0]["decision"] == "rejected"
    assert (
        fake.story_decisions[0]["rationale"]
        == "Output does not conform to parent requirement."
    )

    pbi2_btn = page.locator(
        '[data-delivery-action-instance="backlog_item:PBI-000002"] button'
    )
    expect(pbi2_btn).to_be_visible()
    expect(pbi2_btn).to_contain_text(
        "Generate Stories for PBI-000002: Support accepted Number List language."
    )

    context.close()


def test_pending_story_review_keeps_another_generation_action_visible(
    dashboard_harness: DashboardHarness,
) -> None:
    """Render a pending Story review without hiding another available selector."""
    action = _story_generation_action("backlog_item:PBI-000002")
    fake = _delivery_ready_fake([action])
    fake.planning_review_overrides["/story/reviews"] = {
        "items": [_story_review("backlog_item:PBI-000001")]
    }
    context, page = _open_project_page(dashboard_harness, fake)

    expect(page.locator('[data-planning-review-card="story"]')).to_be_visible()
    expect(
        page.locator('[data-delivery-action-instance="backlog_item:PBI-000002"]')
    ).to_be_visible()

    context.close()


def test_sprint_generation_requires_team_and_blocks_duplicate_submission(
    dashboard_harness: DashboardHarness,
) -> None:
    """Collect operator-owned Sprint input and keep one mutation in flight."""
    fake = _delivery_ready_fake([_sprint_generation_action()])
    context, page = _open_project_page(dashboard_harness, fake)
    form = page.locator('[data-delivery-generation-form="record_sprint_plan"]')
    team_name = form.locator('[name="team_name"]')
    expect(team_name).to_be_visible()
    expect(team_name).to_have_attribute("required", "")

    team_name.fill("Product Team")
    busy_state = form.evaluate(
        """form => {
            form.requestSubmit();
            form.requestSubmit();
            const button = form.querySelector('button[type="submit"]');
            const status = form.querySelector('[data-delivery-action-status="true"]');
            return {
                submitting: form.dataset.submitting,
                disabled: button.disabled,
                busy: button.getAttribute('aria-busy'),
                status: status.textContent,
            };
        }"""
    )
    assert busy_state == {
        "submitting": "true",
        "disabled": True,
        "busy": "true",
        "status": "Generating Sprint plan...",
    }

    review_card = page.locator('[data-planning-review-card="sprint"]')
    expect(review_card).to_be_visible()
    expect(review_card).to_contain_text("Product Team")
    sprint_requests = [
        body for suffix, body in fake.delivery_requests if suffix == "/sprint/generate"
    ]
    assert len(sprint_requests) == 1
    assert sprint_requests[0]["team_name"] == "Product Team"

    context.close()


def _seed_progressive_stories(
    fake: FakeLifecycle,
    story1_id: int,
    story2_id: int,
) -> None:
    fake.backlog_candidate = {
        "backlog_items": [
            {"backlog_item_id": "PBI-000001", "requirement": "Spec workflow"},
            {"backlog_item_id": "PBI-000002", "requirement": "Story validation"},
            {"backlog_item_id": "PBI-000003", "requirement": "Sprint execution"},
        ]
    }
    story1: JsonObject = {
        "story_id": story1_id,
        "source_story_item_id": "US-001",
        "backlog_item_id": "PBI-000001",
        "status": "to_do",
        "story_points": 3,
        "rank": "0|hzzzzz:",
        "sprint_candidate": True,
        "content_accepted": True,
        "readiness_blockers": [],
        "validation_status": "validated",
        "validation_failures": [],
    }
    story2: JsonObject = {
        "story_id": story2_id,
        "source_story_item_id": "US-002",
        "backlog_item_id": "PBI-000002",
        "status": "to_do",
        "story_points": 5,
        "rank": "0|hzzzzz:1",
        "sprint_candidate": False,
        "content_accepted": True,
        "readiness_blockers": ["STORY_VALIDATION_REQUIRED"],
        "validation_status": "unvalidated",
        "validation_failures": [],
    }
    fake.stories = [story1, story2]
    fake.sprint_candidates = [story1]
    fake.story_dependencies = [
        {
            "dependency_id": 1,
            "dependent_story_id": story2_id,
            "prerequisite_story_id": story1_id,
            "status": "proposed",
            "source": "story_writer",
            "confidence": "inferred",
            "reason": "Requires US-001 foundation",
        }
    ]


def test_progressive_story_readiness_partial_refinement_to_sprint_planning(
    dashboard_harness: DashboardHarness,
) -> None:
    """Unlock Sprint planning with ready Story, unvalidated sibling, and edge."""
    story1_id, story2_id = 101, 102
    pbi3_action = _story_generation_action("backlog_item:PBI-000003")
    fake = _delivery_ready_fake(
        [
            pbi3_action,
            {
                "node_id": "planning.story_dependencies",
                "instance_key": None,
                "request_kind": "apply_story_dependencies",
                "endpoint": "story/dependencies/apply",
                "transport": "semantic",
            },
        ]
    )
    _seed_progressive_stories(fake, story1_id, story2_id)

    context, page = _open_project_page(dashboard_harness, fake)

    expect(
        page.locator('[data-delivery-action-instance="backlog_item:PBI-000003"]')
    ).to_be_visible()

    readiness = page.locator('[data-story-readiness-section="true"]')
    expect(readiness).to_be_visible()
    expect(readiness).to_contain_text("US-001")
    expect(readiness).to_contain_text("Validated")
    expect(readiness).to_contain_text("US-002")
    expect(readiness).to_contain_text("Unvalidated")

    dep_section = page.locator('[data-dependency-review-section="true"]')
    expect(dep_section).to_be_visible()
    expect(dep_section).to_contain_text("US-001")
    expect(dep_section).not_to_contain_text("US-002")
    expect(dep_section).to_contain_text("None (independent stories)")

    confirm_dep_btn = page.locator('[data-apply-dependencies="true"]')
    expect(confirm_dep_btn).to_be_visible()
    confirm_dep_btn.click()
    page.wait_for_timeout(200)

    assert len(fake.dependency_apply_requests) == 1
    assert fake.dependency_apply_requests[0]["selected_story_ids"] == [story1_id]
    assert fake.dependency_apply_requests[0]["reviewed_edges"] == []

    sprint_plan_action = _sprint_generation_action()
    fake.position_override = _delivery_position([pbi3_action, sprint_plan_action])

    page.locator("#refresh-project").click()
    page.wait_for_timeout(200)

    candidate_pool = page.locator('[data-candidate-pool-section="true"]')
    expect(candidate_pool).to_be_visible()
    expect(candidate_pool).to_contain_text("1 candidate ready")
    expect(candidate_pool).to_contain_text("US-001")
    expect(candidate_pool).not_to_contain_text("US-002")

    sprint_form = page.locator('[data-delivery-generation-form="record_sprint_plan"]')
    expect(sprint_form).to_be_visible()

    validate_btn = page.locator(f'[data-story-validate-id="{story2_id}"]')
    expect(validate_btn).to_be_visible()
    validate_btn.click()
    page.wait_for_timeout(200)

    assert len(fake.story_validation_requests) == 1
    assert fake.story_validation_requests[0]["story_id"] == story2_id

    page.locator("#refresh-project").click()
    page.wait_for_timeout(200)

    expect(candidate_pool).to_contain_text("2 candidates ready")
    expect(candidate_pool).to_contain_text("US-001")
    expect(candidate_pool).to_contain_text("US-002")

    expect(
        page.locator('[data-delivery-action-instance="backlog_item:PBI-000003"]')
    ).to_be_visible()

    context.close()


def test_progressive_story_readiness_failure_diagnostics_persist_on_reload(
    dashboard_harness: DashboardHarness,
) -> None:
    """Validation failure diagnostics persist in per-story row after reload."""
    story_id = 101
    pbi1_action = _story_generation_action("backlog_item:PBI-000001")
    fake = _delivery_ready_fake([pbi1_action])

    fake.backlog_candidate = {
        "backlog_items": [
            {
                "backlog_item_id": "PBI-000001",
                "requirement": "Specification authoring workflow",
            },
        ]
    }

    fake.stories = [
        {
            "story_id": story_id,
            "source_story_item_id": "US-001",
            "backlog_item_id": "PBI-000001",
            "status": "to_do",
            "story_points": 3,
            "rank": "0|hzzzzz:",
            "sprint_candidate": False,
            "content_accepted": True,
            "readiness_blockers": ["STORY_VALIDATION_REQUIRED"],
            "validation_status": "unvalidated",
            "validation_failures": [],
        }
    ]
    fake.story_validation_failure = {
        "code": "STORY_SPEC_REFERENCE_INVALID",
        "message": "Story references invalid specification items: REQ.099",
    }

    context, page = _open_project_page(dashboard_harness, fake)

    readiness_section = page.locator('[data-story-readiness-section="true"]')
    expect(readiness_section).to_be_visible()
    expect(readiness_section).to_contain_text("Unvalidated")

    validate_button = page.locator(f'[data-story-validate-id="{story_id}"]')
    expect(validate_button).to_be_visible()

    validate_button.click()
    page.wait_for_timeout(300)

    expect(readiness_section).to_contain_text("Validation Failed")
    diagnostics = page.locator('[data-story-validation-diagnostics="true"]')
    expect(diagnostics).to_be_visible()
    expect(diagnostics).to_contain_text("STORY_SPEC_REFERENCE_INVALID")
    expect(diagnostics).to_contain_text(
        "Story references invalid specification items: REQ.099"
    )

    revalidate_button = page.locator(f'[data-story-validate-id="{story_id}"]')
    expect(revalidate_button).to_be_visible()
    expect(revalidate_button).to_contain_text("Revalidate")

    error_banner = page.locator("#project-error")
    expect(error_banner).to_be_visible()
    expect(error_banner).to_contain_text("Story structural validation failed.")
    expect(error_banner).to_contain_text("STORY_SPEC_REFERENCE_INVALID")

    context.close()
