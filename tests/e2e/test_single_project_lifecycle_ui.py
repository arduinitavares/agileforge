"""Provider-free browser verification for the human lifecycle dashboard."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from hashlib import sha256
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
_SPRINT_CAPACITY_POINTS = 8
_DESKTOP_VIEWPORT: ViewportSize = {"width": 1440, "height": 900}
_MOBILE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}
_EXPECTED_REMOVE_SELECTION_REQUESTS = 2
_EXPECTED_RETRIED_DEPENDENCY_REQUESTS = 2
_PNG_WIDTH_START = 16
_PNG_WIDTH_END = 20
_PNG_HEIGHT_START = 20
_PNG_HEIGHT_END = 24
_ISSUE_213_ACTIVE_STATUS = (
    "Backlog correction is in progress. The recorded Feedback remains current."
)
_ISSUE_213_FAILED_STATUS = (
    "Backlog correction failed. No corrected candidate was produced; "
    "the recorded Feedback remains current."
)
_ISSUE_213_EXPIRED_STATUS = (
    "The previous Backlog correction attempt expired. "
    "The recorded Feedback remains current and can be retried."
)
_FORBIDDEN_BODY_FIELDS = {
    "expected_fact_fingerprint",
    "expected_decision_fingerprint",
    "expected_candidate_fingerprint",
    "graph_version",
    "model_id",
}


def _fingerprint(seed: str) -> str:
    """Build a parseable fake fingerprint without exposing a real identity."""
    return f"sha256:{seed[0].lower() * 64}"


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


def _valid_invest_assessment_payload() -> JsonObject:
    return {
        "independent": {
            "result": "pass",
            "rationale": "Self-contained Story increment.",
            "evidence": "No unbuilt dependencies required.",
        },
        "negotiable": {
            "result": "pass",
            "rationale": "Implementation details open to refinement.",
            "evidence": "Outcome focused statement.",
        },
        "valuable": {
            "result": "pass",
            "rationale": "Directly delivers operator review capability.",
            "evidence": "Addresses parent requirement.",
        },
        "estimable": {
            "result": "pass",
            "rationale": "Clear boundaries and discrete criteria.",
            "evidence": "Single verification criterion.",
        },
        "small": {
            "result": "pass",
            "rationale": "Sized comfortably for one iteration.",
            "evidence": "Effort is small.",
        },
        "testable": {
            "result": "pass",
            "rationale": "Deterministic verification condition.",
            "evidence": "Verifiable pass/fail outcome.",
        },
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
    specification_source_registrations: list[JsonObject] = field(default_factory=list)
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
    sprint_capacity: JsonObject = field(
        default_factory=lambda: {
            "status": "recommended",
            "recommended_max_story_points": _SPRINT_CAPACITY_POINTS,
            "source": "project_metrics",
            "rationale": "8 points, based on the last 1 completed Sprints: 8.",
        }
    )
    structural_reconcile_requests: list[JsonObject] = field(default_factory=list)
    sprint_selection_requests: list[JsonObject] = field(default_factory=list)
    dependency_apply_requests: list[JsonObject] = field(default_factory=list)
    dependency_reload_failure: str | None = None
    dependency_reload_conflict: str | None = None
    story_reload_conflict: str | None = None
    dependency_selected_story_ids: list[int] | None = None
    dependency_selected_scope_fingerprint: str | None = None
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
                    "review": {"state": "pending"},
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
                    "review": {"state": "pending"},
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
                            "review": {"state": "pending"},
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
                    "review": {"state": "pending"},
                    "project_id": _PROJECT_ID,
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
            response: tuple[int, JsonObject] | None = None
            if (
                suffix == "/story/dependencies"
                and self.dependency_reload_conflict
                and self.dependency_apply_requests
            ):
                response = (
                    _HTTP_CONFLICT,
                    {
                        "detail": {
                            "error": {
                                "code": "STALE_DEPENDENCY_PROJECTION",
                                "message": self.dependency_reload_conflict,
                            }
                        }
                    },
                )
            elif (
                suffix == "/story/dependencies"
                and self.story_reload_conflict
                and (
                    self.sprint_selection_requests or self.structural_reconcile_requests
                )
            ):
                response = (
                    _HTTP_CONFLICT,
                    {
                        "detail": {
                            "error": {
                                "code": "STALE_STORY_PROJECTION",
                                "message": self.story_reload_conflict,
                            }
                        }
                    },
                )
            elif suffix == "/position":
                response = (_HTTP_OK, self.position_envelope())
            else:
                response = self._planning_review_response(suffix)
                if response is None:
                    response = (_HTTP_OK, self._success(self._read(suffix)))
            return response
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
        if self.dependency_reload_failure and self.dependency_apply_requests:
            raise ValueError(self.dependency_reload_failure)
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
            "/story/structural-eligibility/reconcile": (
                self._reconcile_story_eligibility
            ),
            "/story/sprint-selection": self._apply_story_sprint_selection,
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
        source_path = body["source_path"]
        preparation_capability = body["preparation_capability"]
        adr_paths = body["adr_paths"]
        assert isinstance(source_path, str)
        assert isinstance(preparation_capability, str)
        assert preparation_capability == "grill-with-docs"
        assert isinstance(adr_paths, list)
        assert all(isinstance(path, str) for path in adr_paths)
        self.specification_source_registrations.append(dict(body))
        registration_number = len(self.specification_source_registrations)
        self.specification_source = {
            "specification_source_id": 30 + registration_number,
            "source_fingerprint": (
                f"sha256:hidden-registered-source-{registration_number}"
            ),
            "producer_capability": "to-spec",
            "preparation_capability": preparation_capability,
            "source": {"relative_path": source_path},
            "context": {"state": "absent", "document": None},
            "adrs": [{"relative_path": path} for path in adr_paths],
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
        selected_story_ids = self.dependency_selected_story_ids
        if selected_story_ids is None:
            selected_story_ids = [
                story["story_id"]
                for story in self.stories
                if isinstance(story, dict)
                and story.get("sprint_selection_state") == "selected"
                and isinstance(story.get("story_id"), int)
            ]
        scope_fingerprint = self.dependency_selected_scope_fingerprint
        if scope_fingerprint is None:
            scope_fingerprint = next(
                (
                    story.get("selected_scope_fingerprint")
                    for story in self.stories
                    if isinstance(story, dict)
                    and story.get("story_id") in selected_story_ids
                    and isinstance(story.get("selected_scope_fingerprint"), str)
                ),
                None,
            )
        return cast(
            "JsonObject",
            {
                "stories": self.stories,
                "edges": self.story_dependencies,
                "selected_story_ids": selected_story_ids,
                "selected_scope_fingerprint": scope_fingerprint,
                "structural_evidence_scope": {
                    "proves": [
                        "exact Story identity",
                        "immutable accepted Story artifact/item binding",
                        "accepted Backlog and Specification lineage",
                        "parent-bounded Specification references",
                        "required Story shape",
                        "non-empty acceptance criteria",
                        "current evidence and input fingerprints",
                    ],
                    "does_not_prove": [
                        "semantic/model quality",
                        "product value",
                        "human Sprint selection",
                        "dependency safety",
                        "Sprint candidacy",
                        "Sprint-generation readiness",
                    ],
                },
            },
        )

    def _sprint_candidates_projection(self) -> JsonObject:
        return {
            "project_id": _PROJECT_ID,
            "items": self.sprint_candidates,
            "count": len(self.sprint_candidates),
            "capacity": self.sprint_capacity,
            "sprint_owner": {
                "kind": "solo_project",
                "key": "agileforge:sprint-owner:solo-project:v1:project:1",
                "label": (
                    "[agileforge:sprint-owner:solo-project:v1:project:1] "
                    "Solo operator for Exact Project"
                ),
                "display_label": "Solo operator for Exact Project",
                "named_team_override_allowed": True,
            },
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
                    "invest_assessment": _valid_invest_assessment_payload(),
                    "estimated_effort": "M",
                    "effort_rationale": "Bounded delivery generation slice.",
                    "order_rationale": "First priority in backlog item.",
                    "order": 1,
                    "rank": "101",
                    "story_points": 3,
                    "dependency_candidates": [],
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

    def _reconcile_story_eligibility(self, body: JsonObject) -> JsonObject:
        self._assert_fields(body, {"story_ids"})
        story_ids = body["story_ids"]
        assert isinstance(story_ids, list)
        assert len(story_ids) == 1
        self.structural_reconcile_requests.append(dict(body))
        return {"ok": True, "data": {}, "errors": []}

    def _apply_story_sprint_selection(self, body: JsonObject) -> JsonObject:
        self._assert_fields(
            body,
            {"story_id", "intent", "expected_state_fingerprint", "rationale"},
        )
        self.sprint_selection_requests.append(dict(body))
        story_id = body["story_id"]
        intent = body["intent"]
        assert isinstance(story_id, int)
        assert isinstance(intent, str)
        assert intent in {"select", "remove", "defer"}
        for story in self.stories:
            if isinstance(story, dict) and story.get("story_id") == story_id:
                assert (
                    body["expected_state_fingerprint"]
                    == story["sprint_selection_state_fingerprint"]
                )
                story["sprint_selection_state"] = {
                    "select": "selected",
                    "remove": "unselected",
                    "defer": "deferred",
                }[intent]
                story["sprint_selection_state_fingerprint"] = _fingerprint(
                    str(story_id + len(self.sprint_selection_requests))
                )
                story["dependency_safe"] = False
                story["sprint_candidate"] = False
                return {"ok": True, "data": {}, "errors": []}
        message = f"Story {story_id} not found"
        raise ValueError(message)

    def _apply_story_dependencies(self, body: JsonObject) -> None:
        self._assert_fields(
            body,
            {
                "selected_story_ids",
                "selected_scope_fingerprint",
                "reviewed_edges",
            },
        )
        assert (
            body["selected_scope_fingerprint"]
            == self._story_dependencies_projection()["selected_scope_fingerprint"]
        )
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
        selected_ids = body["selected_story_ids"]
        assert isinstance(selected_ids, list)
        self.sprint_candidates = []
        for story in self.stories:
            if isinstance(story, dict):
                selected = story.get("story_id") in selected_ids
                story["dependency_safe"] = selected
                story["sprint_candidate"] = selected
                if selected:
                    self.sprint_candidates.append(story)

    def _generate_sprint_plan(self, body: JsonObject) -> None:
        if "team_name" in body:
            self._assert_fields(
                body,
                {"team_name", "selected_story_ids", "max_story_points"},
            )
        else:
            self._assert_fields(body, {"selected_story_ids", "max_story_points"})
        max_story_points = body["max_story_points"]
        assert isinstance(max_story_points, int)
        assert not isinstance(max_story_points, bool)
        assert max_story_points > 0
        projected_candidate_ids = [
            story["story_id"]
            for story in self.sprint_candidates
            if isinstance(story, dict) and isinstance(story.get("story_id"), int)
        ]
        assert body["selected_story_ids"] == projected_candidate_ids
        if self.delivery_generation_failure:
            raise ValueError(self.delivery_generation_failure)
        team_name = body.get(
            "team_name",
            "[agileforge:sprint-owner:solo-project:v1:project:1] "
            "Solo operator for Exact Project",
        )
        assert isinstance(team_name, str)
        owner_kind = "solo_project" if "team_name" not in body else "named_team"
        owner_key = (
            "agileforge:sprint-owner:solo-project:v1:project:1"
            if owner_kind == "solo_project"
            else (
                "agileforge:sprint-owner:named-team:v1:sha256:"
                f"{sha256(team_name.encode()).hexdigest()}"
            )
        )
        self.sprint_plan_candidate = {
            "team_name": team_name,
            "sprint_owner": {
                "kind": owner_kind,
                "key": owner_key,
                "label": team_name,
                "display_label": body.get(
                    "team_name",
                    "Solo operator for Exact Project",
                ),
            },
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
        actions: list[JsonValue] = [
            {
                "node_id": decision["node_id"],
                "instance_key": instance_key,
                "request_kind": request_kind,
                "endpoint": endpoint,
                "transport": "semantic",
            }
        ]
        revision_registration_available = self.specification_source is not None and (
            self.specification is None or self.specification_feedback is not None
        )
        if revision_registration_available:
            registration_references: list[JsonValue] = [
                {
                    "fact_type": "specification_source",
                    "fact_id": str(
                        self.specification_source["specification_source_id"]
                    ),
                    "fingerprint": self.specification_source["source_fingerprint"],
                }
            ]
            if self.specification_feedback is not None:
                registration_references.append(
                    {
                        "fact_type": "specification_candidate",
                        "fact_id": "32",
                        "fingerprint": "sha256:hidden-specification",
                    }
                )
            decisions.append(
                {
                    "node_id": "specification.source.register",
                    "child_graph_id": "specification",
                    "request_kind": "register_specification_source",
                    "category": "available",
                    "instance_key": None,
                    "reason_code": (
                        "SPECIFICATION_FEEDBACK_SOURCE_REVISION_AVAILABLE"
                        if self.specification_feedback is not None
                        else "SPECIFICATION_SOURCE_REPLACEMENT_AVAILABLE"
                    ),
                    "decision_fingerprint": "sha256:hidden-source-revision",
                    "fact_references": registration_references,
                }
            )
            actions.append(
                {
                    "node_id": "specification.source.register",
                    "instance_key": None,
                    "request_kind": "register_specification_source",
                    "endpoint": "specifications/source",
                    "transport": "semantic",
                }
            )
        return {
            "graph_version": "agileforge.workflow.hidden",
            "fact_fingerprint": "sha256:hidden-facts",
            "decisions": decisions,
            "terminal": False,
            "actions": [],
        } | {"_actions": actions}

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


@dataclass
class BacklogFeedbackLifecycle(FakeLifecycle):
    """Durable #213 Backlog correction lifecycle shared by routed pages."""

    correction_state: str = "pending"
    review_requests: list[JsonObject] = field(default_factory=list)
    correction_requests: list[JsonObject] = field(default_factory=list)
    provider_entry_count: int = 0
    backlog_read_failures: int = 0

    def __post_init__(self) -> None:
        """Seed the shared project projection required by every routed page."""
        self.project = {
            "project_id": _PROJECT_ID,
            "name": "Issue 213 Backlog Feedback",
            "description": "Provider-free durable correction lifecycle.",
            "user_stories_count": 0,
            "sprint_count": 0,
        }

    @staticmethod
    def _lineage() -> JsonObject:
        return {
            "specification": {
                "spec_version_id": 41,
                "spec_hash": "sha256:issue-213-specification",
            },
            "product_goal": {
                "product_goal_artifact_id": 11,
                "product_goal_fingerprint": "sha256:issue-213-goal",
            },
        }

    @staticmethod
    def _candidate(
        artifact_id: int,
        version: int,
        *,
        supersedes: int | None = None,
    ) -> JsonObject:
        return {
            "backlog_artifact_id": artifact_id,
            "artifact_fingerprint": f"sha256:issue-213-backlog-{artifact_id}",
            "version_number": version,
            "supersedes_backlog_artifact_id": supersedes,
            "backlog_items": [
                {
                    "backlog_item_id": "PBI-000001",
                    "requirement": "Keep the retry boundary visible.",
                    "priority": "high",
                    "value_driver": "operator confidence",
                    "estimated_effort": "small",
                    "justification": "The correction state must survive reload.",
                }
            ],
            "is_complete": True,
            "clarifying_questions": [],
        }

    def _decision_fingerprint(self) -> str:
        return {
            "pending": "sha256:issue-213-review-v1",
            "feedback": "sha256:issue-213-feedback-v1",
            "active": "sha256:issue-213-active-v1",
            "failed-retry": "sha256:issue-213-failed-v1",
            "expired-recovery": "sha256:issue-213-expired-v1",
            "replacement": "sha256:issue-213-replacement-v1",
            "success": "sha256:issue-213-review-v2",
        }[self.correction_state]

    def _candidate_for_state(self) -> JsonObject:
        if self.correction_state == "success":
            return self._candidate(8, 2, supersedes=7)
        return self._candidate(7, 1)

    @staticmethod
    def _correction_action() -> JsonObject:
        return {
            "node_id": "backlog.generate",
            "instance_key": None,
            "request_kind": "record_backlog_draft",
            "endpoint": "backlog/generate",
            "transport": "semantic",
        }

    def _references(self, *, attempt: bool) -> list[JsonValue]:
        candidate = self._candidate_for_state()
        references: list[JsonValue] = [
            {
                "fact_type": "backlog",
                "fact_id": candidate["backlog_artifact_id"],
                "fingerprint": candidate["artifact_fingerprint"],
            },
            {
                "fact_type": "specification",
                "fact_id": 41,
                "fingerprint": "sha256:issue-213-specification",
            },
            {
                "fact_type": "product_goal",
                "fact_id": 11,
                "fingerprint": "sha256:issue-213-goal",
            },
        ]
        if attempt:
            references.append(
                {
                    "fact_type": "node_attempt",
                    "fact_id": 91,
                    "fingerprint": "sha256:issue-213-attempt",
                }
            )
        return references

    def _planning_review_data(self, suffix: str) -> JsonObject | None:
        if suffix != "/backlog/review":
            return super()._planning_review_data(suffix)
        candidate = self._candidate_for_state()
        if self.correction_state == "success":
            return {
                "binding": {
                    "decision_fingerprint": self._decision_fingerprint(),
                    "instance_key": None,
                },
                "review": {
                    "phase": "backlog",
                    "review": {"state": "pending"},
                    "candidate": candidate,
                    "lineage": self._lineage(),
                },
            }
        if self.correction_state == "pending":
            return {
                "binding": {
                    "decision_fingerprint": self._decision_fingerprint(),
                    "instance_key": None,
                },
                "review": {
                    "phase": "backlog",
                    "review": {"state": "pending"},
                    "candidate": candidate,
                    "lineage": self._lineage(),
                },
            }
        return {
            "continuation": {
                "binding": {
                    "node_id": "backlog.generate",
                    "decision_fingerprint": self._decision_fingerprint(),
                    "instance_key": None,
                },
                "review": {
                    "phase": "backlog",
                    "review": {
                        "state": "feedback",
                        "rationale": "Show the retry boundary.",
                    },
                    "candidate": candidate,
                    "lineage": self._lineage(),
                },
            }
        }

    def _planning_review_response(
        self,
        suffix: str,
    ) -> tuple[int, JsonObject] | None:
        if suffix == "/backlog/review" and self.backlog_read_failures:
            self.backlog_read_failures -= 1
            return _HTTP_CONFLICT, {
                "detail": {
                    "error": {
                        "code": "BACKLOG_RELOAD_FAILED",
                        "message": (
                            "The authoritative Backlog projection is temporarily "
                            "unavailable."
                        ),
                    }
                }
            }
        return super()._planning_review_response(suffix)

    def _position_projection(self) -> JsonObject:
        if self.correction_state == "success":
            decision = {
                "node_id": "backlog.review",
                "child_graph_id": "backlog",
                "request_kind": "decide_backlog",
                "category": "waiting",
                "recommendation_kind": "required",
                "instance_key": None,
                "reason_code": "BACKLOG_REVIEW_REQUIRED",
                "decision_fingerprint": self._decision_fingerprint(),
                "fact_references": self._references(attempt=False),
            }
            actions: list[JsonValue] = []
        else:
            mode = {
                "pending": ("waiting", "required", "BACKLOG_REVIEW_REQUIRED", False),
                "feedback": (
                    "available",
                    "recovery",
                    "BACKLOG_REVISION_REQUIRED",
                    False,
                ),
                "active": ("waiting", "required", "BACKLOG_GENERATION_ACTIVE", False),
                "failed-retry": (
                    "available",
                    "recovery",
                    "BACKLOG_GENERATION_FAILED",
                    True,
                ),
                "expired-recovery": (
                    "available",
                    "recovery",
                    "BACKLOG_GENERATION_RECOVERY_REQUIRED",
                    True,
                ),
                "replacement": (
                    "available",
                    "recovery",
                    "BACKLOG_REVISION_REQUIRED",
                    False,
                ),
            }[self.correction_state]
            if self.correction_state == "pending":
                decision = {
                    "node_id": "backlog.review",
                    "child_graph_id": "backlog",
                    "request_kind": "decide_backlog",
                    "category": mode[0],
                    "recommendation_kind": mode[1],
                    "instance_key": None,
                    "reason_code": mode[2],
                    "decision_fingerprint": self._decision_fingerprint(),
                    "fact_references": self._references(attempt=False),
                }
                actions = []
            else:
                decision = {
                    "node_id": "backlog.generate",
                    "child_graph_id": "backlog",
                    "request_kind": "record_backlog_draft",
                    "category": mode[0],
                    "recommendation_kind": mode[1],
                    "instance_key": None,
                    "reason_code": mode[2],
                    "decision_fingerprint": self._decision_fingerprint(),
                    "fact_references": self._references(attempt=mode[3]),
                }
                actions = (
                    []
                    if self.correction_state == "active"
                    else [self._correction_action()]
                )
        return cast(
            "JsonObject",
            {
                "graph_version": "agileforge.workflow.hidden",
                "fact_fingerprint": "sha256:issue-213-facts",
                "decisions": [decision],
                "terminal": False,
                "actions": [],
                "_actions": actions,
            },
        )

    def _mutate(
        self,
        suffix: str,
        body: JsonObject,
        headers: dict[str, str],
    ) -> tuple[int, JsonObject]:
        if suffix == "/backlog/decide":
            self._assert_fields(body, {"decision", "rationale"})
            assert body["decision"] == "feedback"
            assert body["rationale"] == "Show the retry boundary."
            assert (
                headers["x-agileforge-expected-decision"]
                == self._decision_fingerprint()
            )
            self.review_requests.append(dict(body))
            self.correction_state = "feedback"
            return _HTTP_OK, self._mutation_result()
        if suffix == "/backlog/generate":
            self._assert_fields(body, set())
            self.correction_requests.append(dict(body))
            if self.correction_state == "active":
                return _HTTP_CONFLICT, {
                    "detail": {
                        "error": {
                            "code": "TRANSITION_NOT_AVAILABLE",
                            "message": "A Backlog correction is already active.",
                        }
                    }
                }
            if self.correction_state == "replacement":
                return _HTTP_CONFLICT, {
                    "detail": {
                        "error": {
                            "code": "STALE_POSITION",
                            "message": "The Backlog correction action was replaced.",
                        }
                    }
                }
            self.provider_entry_count += 1
            return _HTTP_OK, self._mutation_result()
        return super()._mutate(suffix, body, headers)

    def begin_correction(self) -> None:
        """Persist a single simulated provider entry and its active lease."""
        assert self.correction_state in {"feedback", "failed-retry", "replacement"}
        self.provider_entry_count += 1
        self.correction_state = "active"


def test_sprint_review_browser_shows_accepted_invest_without_false_gate() -> None:
    """Render accepted INVEST evidence without changing profile or Sprint state."""
    source = (_PROJECT_ROOT / "frontend" / "project.js").read_text(encoding="utf-8")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport=_DESKTOP_VIEWPORT)
            page.set_content('<main id="review-root"></main>')
            page.add_script_tag(content=source)
            page.evaluate(
                """async (assessment) => {
                    const owner = {
                        kind: 'solo_project',
                        key: 'agileforge:sprint-owner:solo-project:v1:project:1',
                        label: [
                            '[agileforge:sprint-owner:solo-project:v1:project:1]',
                            'Solo operator for Exact Project',
                        ].join(' '),
                        display_label: 'Solo operator for Exact Project',
                    };
                    if (!await validateSprintOwnerProjection(owner, 1)) {
                        throw new Error('Sprint owner fixture is invalid.');
                    }
                    const selected = {
                        binding: {
                            decision_fingerprint: 'sha256:sprint-review-decision',
                            instance_key: null,
                        },
                        review: {
                            phase: 'sprint_plan',
                            project_id: 1,
                            candidate: {
                                sprint_owner: owner,
                                sprint_goal: 'Deliver exact accepted evidence.',
                                selected_stories: [{
                                    title: 'Delivery story draft',
                                    statement: (
                                        'As an operator, I want exact review '
                                        + 'evidence.'
                                    ),
                                    persona: 'operator',
                                    acceptance_criteria: [
                                        'Accepted evidence is visible.',
                                    ],
                                    specification_evidence: [],
                                    invest_assessment: assessment,
                                    reason_for_selection: 'Highest accepted value.',
                                    tasks: [{
                                        description: 'Render the accepted evidence',
                                        task_kind: 'implementation',
                                        checklist_items: ['Verify review output'],
                                        specification_evidence: [],
                                    }],
                                }],
                            },
                        },
                    };
                    document.querySelector('#review-root').innerHTML =
                        planningReviewCardMarkup(
                            'Sprint plan review', selected, 'sprint', 0,
                        );
                }""",
                _valid_invest_assessment_payload(),
            )

            review = page.locator('[data-planning-review-card="sprint"]')
            expect(review).to_be_visible()
            expect(review.locator('[data-invest-assessment="true"]')).to_be_visible()
            expect(review).to_contain_text("Self-contained Story increment.")
            expect(review).not_to_contain_text("Quality Assessment Incomplete")
            expect(review).not_to_contain_text("Acceptance is disabled.")
            expect(
                review.locator(
                    '[data-planning-review="sprint"][data-review-decision="accepted"]'
                )
            ).to_be_enabled()
        finally:
            browser.close()


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
    _submit_specification_source(
        page,
        "specs/product-specification.md",
        "docs/adr/0004-registered-source.md",
    )
    expect(page.get_by_role("button", name="Structure Specification")).to_be_visible()
    page.locator('[data-direct-action="structure_specification"]').click()
    expect(page.get_by_text("Human lifecycle review")).to_be_visible()
    expect(
        page.get_by_text("Every candidate has a scoped human decision.")
    ).to_be_visible()
    _accept_review(page, "specification")


def _submit_specification_source(
    page: Page,
    source_path: str,
    adr_path: str,
) -> None:
    form = page.locator('form[data-specification-source-form="true"]')
    form.locator('[name="source_path"]').fill(source_path)
    form.locator('[name="adr_paths"]').fill(adr_path)
    form.locator('[name="preparation_capability"]').select_option("grill-with-docs")
    form.evaluate("form => form.requestSubmit()")


def _assert_specification_refresh_stays_locked(page: Page) -> None:
    page.evaluate(
        """() => {
            window.issue204OriginalStructureButton = document.querySelector(
                '[data-direct-action="structure_specification"]',
            );
        }"""
    )
    page.locator("#refresh-project").click()
    page.wait_for_function(
        """() => window.issue204OriginalStructureButton
            !== document.querySelector(
                '[data-direct-action="structure_specification"]',
            )"""
    )
    replacement = page.locator('[data-direct-action="structure_specification"]')
    expect(replacement).to_be_disabled()
    expect(replacement).to_have_attribute("aria-busy", "true")
    expect(
        page.locator('[data-specification-revision-registration="true"]')
    ).to_have_attribute("aria-disabled", "true")
    replacement.evaluate("button => button.click()")
    page.wait_for_timeout(_UI_SETTLE_MS)
    assert page.evaluate("window.issue204Requests.length") == 1


def _assert_issue_204_failure_restores_source_controls(page: Page) -> None:
    button = page.locator('[data-direct-action="structure_specification"]')
    status = page.locator('[data-specification-structuring-status="true"]')
    expect(button).to_be_enabled()
    expect(button).not_to_have_attribute("aria-busy", "true")
    expect(status).to_contain_text(
        "Specification structurer provider execution failed."
    )
    expect(status).to_contain_text("No new candidate was produced.")
    expect(status).to_contain_text("The prior candidate and Feedback remain current.")
    expect(page.get_by_text("Prior Feedback candidate", exact=False)).to_be_visible()
    expect(button).to_be_visible()
    current = page.locator('[data-current-specification-source="true"]')
    expect(current).to_be_visible()
    expect(current).to_contain_text("Current registered Specification source")
    expect(current).to_contain_text("specs/issue-204.md")
    revision = page.locator('[data-specification-revision-registration="true"]')
    expect(revision).to_be_visible()
    expect(revision).not_to_have_attribute("open", "")
    expect(
        revision.locator('form[data-specification-source-form="true"]')
    ).not_to_be_visible()
    expect(revision).not_to_have_attribute("aria-disabled", "true")
    assert revision.evaluate("details => !details.hasAttribute('inert')")
    assert (
        revision.locator("button, input, textarea, select").evaluate_all(
            "controls => controls.every((control) => !control.disabled)"
        )
        is True
    )


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
            "source": {"relative_path": "specs/issue-204.md"},
            "context": {"state": "absent", "document": None},
            "adrs": [],
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
    expect(page.locator('[data-current-specification-source="true"]')).to_be_visible()
    revision = page.locator('[data-specification-revision-registration="true"]')
    expect(revision).not_to_have_attribute("open", "")
    expect(revision).to_have_attribute("aria-disabled", "true")
    assert (
        revision.locator("button, input, textarea, select").evaluate_all(
            "controls => controls.every((control) => control.disabled)"
        )
        is True
    )
    assert page.evaluate("window.issue204Requests.length") == 1
    assert (
        page.evaluate(
            "window.issue204Requests[0].headers['x-agileforge-expected-decision']"
        )
        == "sha256:hidden-decision"
    )

    _assert_specification_refresh_stays_locked(page)

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
    _assert_issue_204_failure_restores_source_controls(page)
    assert page.evaluate("window.issue204Requests.length") == 1

    button = page.locator('[data-direct-action="structure_specification"]')
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


def test_issue_211_shows_current_source_through_specification_lifecycle(
    dashboard_harness: DashboardHarness,
) -> None:
    """Keep initial and revised source registration visibly distinct."""
    fake = FakeLifecycle(
        repositories={},
        project={
            "project_id": _PROJECT_ID,
            "name": "Issue 211 lifecycle",
            "description": "Provider-free registered source state coverage.",
        },
        vision_candidate={"statement": "Accepted Vision"},
        vision_accepted=True,
        goal_candidate={"statement": "Accepted Product Goal"},
        goal_accepted=True,
    )
    context, page = _open_project_page(dashboard_harness, fake)

    _submit_specification_source(
        page,
        "specs/current-specification.md",
        "docs/adr/0004-source.md",
    )

    current = page.locator('[data-current-specification-source="true"]')
    expect(current).to_be_visible()
    expect(current).to_have_attribute("role", "status")
    expect(current).to_contain_text("Current registered Specification source")
    expect(current).to_contain_text("specs/current-specification.md")
    expect(current).to_contain_text("docs/adr/0004-source.md")
    expect(current).to_contain_text("grill-with-docs")
    revision = page.locator('[data-specification-revision-registration="true"]')
    expect(revision).to_be_visible()
    expect(revision).not_to_have_attribute("open", "")
    expect(
        revision.locator('form[data-specification-source-form="true"]')
    ).not_to_be_visible()
    revision.locator("summary").click()
    expect(
        revision.locator('form[data-specification-source-form="true"]')
    ).to_be_visible()
    _submit_specification_source(
        page,
        "specs/revised-source.md",
        "docs/adr/0005-revised.md",
    )

    expect(current).to_contain_text("specs/revised-source.md")
    expect(current).to_contain_text("docs/adr/0005-revised.md")
    revision = page.locator('[data-specification-revision-registration="true"]')
    expect(revision).not_to_have_attribute("open", "")
    expect(
        revision.locator('form[data-specification-source-form="true"]')
    ).not_to_be_visible()
    assert [
        request["source_path"] for request in fake.specification_source_registrations
    ] == ["specs/current-specification.md", "specs/revised-source.md"]

    fake.specification = {"rendered_markdown": "# Pending source review"}
    page.locator("#refresh-project").click()
    expect(current).to_be_visible()
    expect(page.get_by_text("Pending source review", exact=False)).to_be_visible()
    expect(
        page.locator(
            '[data-review-scope="specification"][data-review-decision="accepted"]'
        )
    ).to_be_visible()

    fake.specification_feedback = "Use a more observable source contract."
    page.locator("#refresh-project").click()
    expect(current).to_be_visible()
    expect(
        page.get_by_text("Choose how to address Specification Feedback")
    ).to_be_visible()
    expect(revision).to_be_visible()
    expect(revision).not_to_have_attribute("open", "")
    expect(
        revision.locator('form[data-specification-source-form="true"]')
    ).not_to_be_visible()

    assert fake.specification_source is not None
    fake.specification_source["adrs"] = []
    page.locator("#refresh-project").click()
    expect(current).to_be_visible()
    expect(current).to_contain_text("No ADRs")
    assert fake.api_errors == []
    context.close()


def test_issue_211_fails_closed_for_malformed_and_hostile_source_projections(
    dashboard_harness: DashboardHarness,
) -> None:
    """Prevent malformed source evidence from exposing mutations or markup."""
    context = dashboard_harness.browser.new_context(viewport=_DESKTOP_VIEWPORT)
    page = context.new_page()
    page.goto(
        f"{dashboard_harness.url}/project.html?id={_PROJECT_ID}",
        wait_until="networkidle",
    )
    registration_action: JsonObject = {
        "request_kind": "register_specification_source",
        "endpoint": "specifications/source",
        "transport": "semantic",
    }
    structuring_action: JsonObject = {
        "request_kind": "structure_specification",
        "endpoint": "specifications/structure",
        "transport": "semantic",
    }
    valid_source: JsonObject = {
        "specification_source_id": 31,
        "source_fingerprint": "sha256:issue-211-source",
        "preparation_capability": "grill-with-docs",
        "source": {"relative_path": "specs/current-specification.md"},
        "adrs": [{"relative_path": "docs/adr/0004-source.md"}],
    }
    candidate: JsonObject = {"rendered_markdown": "# Candidate"}
    position: JsonObject = {
        "decisions": [
            {
                "node_id": "specification.structure",
                "request_kind": "structure_specification",
                "category": "available",
                "decision_fingerprint": "sha256:issue-211-structure",
                "fact_references": [
                    {
                        "fact_type": "specification_source",
                        "fact_id": "31",
                        "fingerprint": "sha256:issue-211-source",
                    }
                ],
            }
        ]
    }

    for review_state in ("rejected", "accepted"):
        page.evaluate(
            """({ projection, actions }) => {
                document.querySelector('#specification-panel').innerHTML =
                    specificationPanelMarkup(projection, actions, {});
            }""",
            {
                "projection": {
                    "source": valid_source,
                    "candidate": candidate,
                    "review": {"state": review_state},
                },
                "actions": [registration_action],
            },
        )
        revision = page.locator('[data-specification-revision-registration="true"]')
        expect(revision).to_be_visible()
        expect(revision).not_to_have_attribute("open", "")
        expect(
            revision.locator('form[data-specification-source-form="true"]')
        ).not_to_be_visible()

    malformed_sources = [
        {**valid_source, "source": {"relative_path": "   "}},
        {**valid_source, "preparation_capability": "   "},
        {**valid_source, "adrs": "docs/adr/not-an-array.md"},
        {**valid_source, "adrs": [{"relative_path": "   "}]},
    ]
    for source in malformed_sources:
        page.evaluate(
            """({ projection, actions, position }) => {
                document.querySelector('#specification-panel').innerHTML =
                    specificationPanelMarkup(projection, actions, position);
            }""",
            {
                "projection": {"source": source, "candidate": None, "review": None},
                "actions": [registration_action, structuring_action],
                "position": position,
            },
        )
        unavailable = page.locator(
            '[data-current-specification-source-unavailable="true"]'
        )
        expect(unavailable).to_be_visible()
        expect(unavailable).to_have_attribute("role", "alert")
        expect(
            page.locator('form[data-specification-source-form="true"]')
        ).to_have_count(0)
        expect(
            page.locator('[data-direct-action="structure_specification"]')
        ).to_have_count(0)

    hostile = '<img src=x onerror="window.issue211Xss=true">'
    hostile_source = {
        **valid_source,
        "source": {"relative_path": hostile},
        "preparation_capability": hostile,
        "adrs": [{"relative_path": hostile}],
    }
    page.evaluate(
        """({ projection }) => {
            window.issue211Xss = false;
            document.querySelector('#specification-panel').innerHTML =
                specificationPanelMarkup(projection, [], {});
        }""",
        {"projection": {"source": hostile_source, "candidate": None, "review": None}},
    )
    current = page.locator('[data-current-specification-source="true"]')
    expect(current).to_contain_text(hostile)
    assert current.locator("img").count() == 0
    assert "<img" not in current.inner_html()
    assert page.evaluate("window.issue211Xss") is False
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
    source = _PROJECT_HTML.read_text(encoding="utf-8").casefold()

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
    expect(team_name).not_to_have_attribute("required", "")
    expect(sprint_form.get_by_text("Sprint owner", exact=True)).to_be_visible()
    expect(sprint_form.get_by_text("Solo operator for Exact Project")).to_be_visible()
    expect(sprint_form).not_to_contain_text("agileforge:sprint-owner:")
    expect(sprint_form).to_contain_text(
        "Generate the Sprint plan from the selected Sprint candidates."
    )
    generate_sprint_btn = sprint_form.locator('button[type="submit"]')
    expect(generate_sprint_btn).to_be_visible()
    expect(generate_sprint_btn).to_contain_text("Generate Sprint plan")

    generate_sprint_btn.click()
    review_card = page.locator('[data-planning-review-card="sprint"]')
    expect(review_card).to_be_visible()
    expect(review_card).to_contain_text("Sprint plan review")
    expect(review_card).to_contain_text("Sprint owner")
    expect(review_card).to_contain_text("Solo project")
    expect(review_card).to_contain_text("Solo operator for Exact Project")
    expect(review_card).not_to_contain_text("agileforge:sprint-owner:")
    assert "team_name" not in fake.delivery_requests[-1][1]


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

    _seed_progressive_stories(fake, 101, 102)
    fake.position_override = _delivery_position(
        [
            {
                "node_id": "planning.story_dependencies",
                "instance_key": None,
                "request_kind": "apply_story_dependencies",
                "endpoint": "story/dependencies/apply",
                "transport": "semantic",
            },
            _sprint_generation_action(),
        ]
    )
    page.locator("#refresh-project").click()
    page.wait_for_timeout(_UI_SETTLE_MS)
    page.locator(
        '[data-story-selection-id="101"][data-story-selection-intent="select"]'
    ).click()
    page.wait_for_timeout(_UI_SETTLE_MS)
    page.locator('[data-apply-dependencies="true"]').click()
    page.wait_for_timeout(_UI_SETTLE_MS)
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


def _story_review(
    instance_key: str,
    *,
    invest_assessment: JsonObject | None = None,
) -> JsonObject:
    assessment = (
        _valid_invest_assessment_payload()
        if invest_assessment is None
        else invest_assessment
    )
    story_item: JsonObject = {
        "story_title": "Exact Story review",
        "statement": "As an operator, I review the intended Story.",
        "persona": "Operator",
        "acceptance_criteria": ["The selector remains exact."],
        "specification_evidence": [],
        "estimated_effort": "M",
        "effort_rationale": "Clear bounded scope for UI test.",
        "order_rationale": "First priority in backlog item.",
        "order": 1,
        "rank": "101",
        "story_points": 3,
        "dependency_candidates": [],
    }
    if assessment:
        story_item["invest_assessment"] = assessment

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
                "story_items": [story_item],
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
    expect(review_card).to_contain_text("INVEST assessment")
    expect(review_card).to_contain_text("Independent")
    expect(review_card).to_contain_text("Pass")
    expect(
        review_card.locator('[data-review-error="invalid-story-evidence"]')
    ).not_to_be_attached()
    accept_review_btn = review_card.locator(
        '[data-planning-review="story"][data-review-decision="accepted"]'
    )
    expect(accept_review_btn).to_be_enabled()

    pbi2_btn = page.locator(
        '[data-delivery-action-instance="backlog_item:PBI-000002"] button'
    )
    expect(pbi2_btn).to_contain_text(
        "Generate Stories for PBI-000002: Support accepted Number List language."
    )

    pbi4_btn = page.locator(
        '[data-delivery-action-instance="backlog_item:PBI-000004"] button'
    )
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


def test_sprint_generation_defaults_to_solo_owner_and_blocks_duplicate_submission(
    dashboard_harness: DashboardHarness,
) -> None:
    """Show the resolved owner before one blank-override transport."""
    fake = _delivery_ready_fake([_sprint_generation_action()])
    _seed_progressive_stories(fake, 101, 102)
    candidate = cast("JsonObject", fake.stories[0])
    candidate["sprint_selection_state"] = "selected"
    candidate["dependency_safe"] = True
    candidate["sprint_candidate"] = True
    fake.sprint_candidates = [candidate]
    context, page = _open_project_page(dashboard_harness, fake)
    form = page.locator('[data-delivery-generation-form="record_sprint_plan"]')
    team_name = form.locator('[name="team_name"]')
    capacity = form.locator('[name="max_story_points"]')
    expect(team_name).to_be_visible()
    expect(team_name).not_to_have_attribute("required", "")
    expect(capacity).to_have_value("8")
    expect(form.get_by_text("Sprint owner", exact=True)).to_be_visible()
    expect(form.get_by_text("Solo operator for Exact Project")).to_be_visible()
    expect(form).not_to_contain_text("agileforge:sprint-owner:")
    expect(form).to_contain_text(
        "Generate the Sprint plan from the selected Sprint candidates."
    )

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
    expect(review_card).to_contain_text("Sprint owner")
    expect(review_card).to_contain_text("Solo project")
    expect(review_card).to_contain_text("Solo operator for Exact Project")
    expect(review_card).not_to_contain_text("agileforge:sprint-owner:")
    sprint_requests = [
        body for suffix, body in fake.delivery_requests if suffix == "/sprint/generate"
    ]
    assert len(sprint_requests) == 1
    assert "team_name" not in sprint_requests[0]
    assert sprint_requests[0]["selected_story_ids"] == [101]
    assert sprint_requests[0]["max_story_points"] == _SPRINT_CAPACITY_POINTS

    context.close()


def test_first_sprint_capacity_requires_manual_positive_integer(
    dashboard_harness: DashboardHarness,
) -> None:
    """Keep first-Sprint generation provider-free until capacity is supplied."""
    fake = _delivery_ready_fake([_sprint_generation_action()])
    _seed_progressive_stories(fake, 101, 102)
    candidate = cast("JsonObject", fake.stories[0])
    candidate["sprint_selection_state"] = "selected"
    candidate["dependency_safe"] = True
    candidate["sprint_candidate"] = True
    fake.sprint_candidates = [candidate]
    fake.sprint_capacity = {
        "status": "manual_required",
        "recommended_max_story_points": None,
        "source": None,
        "rationale": (
            "No completed Sprint capacity history is available. "
            "Enter a positive Maximum story points value."
        ),
    }
    context, page = _open_project_page(dashboard_harness, fake)
    form = page.locator('[data-delivery-generation-form="record_sprint_plan"]')
    capacity = form.locator('[name="max_story_points"]')
    submit = form.locator('button[type="submit"]')

    expect(capacity).to_be_visible()
    expect(capacity).to_have_value("")
    expect(submit).to_be_disabled()
    assert not [
        body for suffix, body in fake.delivery_requests if suffix == "/sprint/generate"
    ]

    capacity.fill("8")
    expect(submit).to_be_enabled()
    submit.click()

    sprint_requests = [
        body for suffix, body in fake.delivery_requests if suffix == "/sprint/generate"
    ]
    assert len(sprint_requests) == 1
    assert sprint_requests[0]["max_story_points"] == _SPRINT_CAPACITY_POINTS
    context.close()


def test_sprint_generation_posts_a_trimmed_named_team_override(
    dashboard_harness: DashboardHarness,
) -> None:
    """Transport only a meaningful, normalized named-Team override."""
    fake = _delivery_ready_fake([_sprint_generation_action()])
    _seed_progressive_stories(fake, 101, 102)
    candidate = cast("JsonObject", fake.stories[0])
    candidate["sprint_selection_state"] = "selected"
    candidate["dependency_safe"] = True
    candidate["sprint_candidate"] = True
    fake.sprint_candidates = [candidate]
    context, page = _open_project_page(dashboard_harness, fake)
    form = page.locator('[data-delivery-generation-form="record_sprint_plan"]')
    form.locator('[name="team_name"]').fill("  Delivery Team  ")
    form.locator('button[type="submit"]').click()

    expect(page.locator('[data-planning-review-card="sprint"]')).to_be_visible()
    expect(page.locator('[data-planning-review-card="sprint"]')).to_contain_text(
        "Delivery Team"
    )
    sprint_requests = [
        body for suffix, body in fake.delivery_requests if suffix == "/sprint/generate"
    ]
    assert len(sprint_requests) == 1
    assert sprint_requests[0]["team_name"] == "Delivery Team"
    assert sprint_requests[0]["selected_story_ids"] == [101]
    assert sprint_requests[0]["max_story_points"] == _SPRINT_CAPACITY_POINTS

    context.close()


def test_sprint_generation_blocks_torn_candidate_dependency_scope(
    dashboard_harness: DashboardHarness,
) -> None:
    """A stale candidate scope cannot expose or reach Sprint generation."""
    fake = _delivery_ready_fake([_sprint_generation_action()])
    _seed_progressive_stories(fake, 101, 102)
    current = cast("JsonObject", fake.stories[0])
    current["sprint_selection_state"] = "selected"
    current["dependency_safe"] = True
    current["sprint_candidate"] = True
    stale_candidate = dict(current)
    stale_candidate["selected_scope_fingerprint"] = _fingerprint("c")
    fake.sprint_candidates = [stale_candidate]

    context, page = _open_project_page(dashboard_harness, fake)

    expect(
        page.locator('[data-sprint-candidate-projection-error="true"]')
    ).to_be_visible()
    expect(
        page.locator('[data-delivery-generation-form="record_sprint_plan"]')
    ).not_to_be_attached()
    assert not [
        body for suffix, body in fake.delivery_requests if suffix == "/sprint/generate"
    ]

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
        "structurally_eligible": True,
        "structural_eligibility_status": "eligible",
        "sprint_selection_state": "unselected",
        "sprint_selection_state_fingerprint": _fingerprint("a"),
        "selected_scope_fingerprint": _fingerprint("b"),
        "dependency_safe": False,
        "sprint_candidate": False,
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
        "structurally_eligible": True,
        "structural_eligibility_status": "eligible",
        "sprint_selection_state": "unselected",
        "sprint_selection_state_fingerprint": _fingerprint("c"),
        "selected_scope_fingerprint": _fingerprint("b"),
        "dependency_safe": False,
        "sprint_candidate": False,
        "content_accepted": True,
        "readiness_blockers": [],
        "validation_status": "validated",
        "validation_failures": [],
    }
    fake.stories = [story1, story2]
    fake.sprint_candidates = []
    fake.story_dependencies = [
        {
            "dependency_id": 1,
            "dependent_story_id": story1_id,
            "prerequisite_story_id": story2_id,
            "status": "proposed",
            "source": "story_writer",
            "confidence": "inferred",
            "reason": "US-001 requires external US-002 foundation",
        }
    ]


def test_progressive_story_readiness_partial_refinement_to_sprint_planning(  # noqa: PLR0915
    dashboard_harness: DashboardHarness,
) -> None:
    """One selected Story advances while an accepted sibling stays outside scope."""
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
    expect(readiness).to_contain_text("Structurally eligible")
    expect(readiness).to_contain_text("Unselected")
    expect(readiness).to_contain_text("US-002")
    expect(readiness).to_contain_text("exact Story identity")
    expect(readiness).to_contain_text("human Sprint selection")

    select_story1 = page.locator(
        f'[data-story-selection-id="{story1_id}"][data-story-selection-intent="select"]'
    )
    expect(select_story1).to_be_visible()
    select_story1.click()
    page.wait_for_timeout(_UI_SETTLE_MS)
    assert len(fake.sprint_selection_requests) == 1
    assert fake.sprint_selection_requests[0]["story_id"] == story1_id
    assert fake.sprint_selection_requests[0]["intent"] == "select"
    assert not [
        body for suffix, body in fake.delivery_requests if suffix == "/sprint/generate"
    ]

    expect(readiness).to_contain_text("Selected for Sprint")
    expect(readiness).to_contain_text("US-002")
    expect(readiness).to_contain_text("Unselected")
    focused_story_id = page.evaluate(
        """() => document.activeElement?.closest(
            '[data-story-readiness-row]'
        )?.dataset.storyReadinessRow ?? null"""
    )
    assert focused_story_id == str(story1_id)

    sprint_plan_action = _sprint_generation_action()
    fake.position_override = _delivery_position(
        [
            pbi3_action,
            {
                "node_id": "planning.story_dependencies",
                "instance_key": None,
                "request_kind": "apply_story_dependencies",
                "endpoint": "story/dependencies/apply",
                "transport": "semantic",
            },
            sprint_plan_action,
        ]
    )
    page.locator("#refresh-project").click()
    page.wait_for_timeout(_UI_SETTLE_MS)
    expect(
        page.locator('[data-sprint-candidate-projection-error="true"]')
    ).to_be_visible()
    expect(
        page.locator('[data-delivery-generation-form="record_sprint_plan"]')
    ).not_to_be_attached()
    assert not [
        body for suffix, body in fake.delivery_requests if suffix == "/sprint/generate"
    ]

    dep_section = page.locator('[data-dependency-review-section="true"]')
    expect(dep_section).to_be_visible()
    expect(dep_section).to_contain_text("US-001")
    expect(dep_section).to_contain_text("US-002")
    expect(dep_section).to_contain_text("External/excluded prerequisite")

    confirm_dep_btn = page.locator('[data-apply-dependencies="true"]')
    expect(confirm_dep_btn).to_be_visible()
    confirm_dep_btn.click()
    page.wait_for_timeout(200)

    assert len(fake.dependency_apply_requests) == 1
    assert fake.dependency_apply_requests[0]["selected_story_ids"] == [story1_id]
    assert fake.dependency_apply_requests[0]["reviewed_edges"] == [
        {
            "dependent_story_id": story1_id,
            "prerequisite_story_id": story2_id,
            "reason": "US-001 requires external US-002 foundation",
        }
    ]

    page.locator("#refresh-project").click()
    page.wait_for_timeout(200)

    candidate_pool = page.locator('[data-candidate-pool-section="true"]')
    expect(candidate_pool).to_be_visible()
    expect(candidate_pool).to_contain_text("1 candidate ready")
    expect(candidate_pool).to_contain_text("US-001")
    expect(candidate_pool).not_to_contain_text("US-002")

    sprint_form = page.locator('[data-delivery-generation-form="record_sprint_plan"]')
    expect(sprint_form).to_be_visible()

    remove_story1 = page.locator(
        f'[data-story-selection-id="{story1_id}"][data-story-selection-intent="remove"]'
    )
    expect(remove_story1).to_be_visible()
    remove_story1.click()
    page.wait_for_timeout(200)
    assert len(fake.sprint_selection_requests) == _EXPECTED_REMOVE_SELECTION_REQUESTS
    defer_story1 = page.locator(
        f'[data-story-selection-id="{story1_id}"][data-story-selection-intent="defer"]'
    )
    expect(defer_story1).to_be_visible()
    defer_story1.click()
    page.wait_for_timeout(200)
    expect(readiness).to_contain_text("Deferred")
    reselect_story1 = page.locator(
        f'[data-story-selection-id="{story1_id}"][data-story-selection-intent="select"]'
    )
    expect(reselect_story1).to_be_visible()
    reselect_story1.click()
    page.wait_for_timeout(200)
    assert [request["intent"] for request in fake.sprint_selection_requests] == [
        "select",
        "remove",
        "defer",
        "select",
    ]

    expect(
        page.locator('[data-delivery-action-instance="backlog_item:PBI-000003"]')
    ).to_be_visible()

    assert not [
        body for suffix, body in fake.delivery_requests if suffix == "/sprint/generate"
    ]

    context.close()


def test_completed_sprint_next_scope_confirms_only_the_projected_future_story(
    dashboard_harness: DashboardHarness,
) -> None:
    """Historical selected intent cannot re-enter the next dependency submission."""
    completed_story_id, future_story_id = 101, 102
    fake = _delivery_ready_fake(
        [
            {
                "node_id": "planning.story_dependencies",
                "instance_key": None,
                "request_kind": "apply_story_dependencies",
                "endpoint": "story/dependencies/apply",
                "transport": "semantic",
            }
        ]
    )
    _seed_progressive_stories(fake, completed_story_id, future_story_id)
    completed = cast("JsonObject", fake.stories[0])
    completed["sprint_selection_state"] = "selected"
    fake.dependency_selected_story_ids = [future_story_id]
    fake.dependency_selected_scope_fingerprint = _fingerprint("b")

    context, page = _open_project_page(dashboard_harness, fake)
    page.locator(
        f'[data-story-selection-id="{future_story_id}"][data-story-selection-intent="select"]'
    ).click()
    page.wait_for_timeout(_UI_SETTLE_MS)

    confirm = page.locator('[data-apply-dependencies="true"]')
    expect(confirm).to_be_enabled()
    confirm.click()
    page.wait_for_timeout(_UI_SETTLE_MS)

    assert fake.sprint_selection_requests[0]["story_id"] == future_story_id
    assert fake.dependency_apply_requests[0]["selected_story_ids"] == [future_story_id]
    assert completed["sprint_selection_state"] == "selected"
    assert not [
        body for suffix, body in fake.delivery_requests if suffix == "/sprint/generate"
    ]

    context.close()


def test_story_selection_stays_locked_through_409_until_current_projection_recovers(
    dashboard_harness: DashboardHarness,
) -> None:
    """A committed Story selection cannot be repeated after its reload conflicts."""
    fake = _delivery_ready_fake([])
    _seed_progressive_stories(fake, 101, 102)
    fake.story_reload_conflict = "Story authority projection conflicted."

    context, page = _open_project_page(dashboard_harness, fake)
    page.locator(
        '[data-story-selection-id="101"][data-story-selection-intent="select"]'
    ).click()
    page.wait_for_timeout(_UI_SETTLE_MS)

    locked = page.locator(
        '[data-story-selection-id="101"][data-story-selection-intent="select"]'
    )
    expect(locked).to_be_disabled()
    expect(locked).to_have_attribute("aria-disabled", "true")
    expect(locked).to_have_attribute("aria-busy", "true")
    expect(page.locator("#project-error")).to_contain_text(
        "Story authority projection conflicted."
    )
    locked.evaluate("button => button.click()")
    page.wait_for_timeout(_UI_SETTLE_MS)
    assert len(fake.sprint_selection_requests) == 1

    fake.story_reload_conflict = None
    page.locator("#refresh-project").click()
    page.wait_for_timeout(_UI_SETTLE_MS)
    recovered = page.locator(
        '[data-story-selection-id="101"][data-story-selection-intent="remove"]'
    )
    expect(recovered).to_be_enabled()
    assert len(fake.sprint_selection_requests) == 1

    context.close()


def test_dependency_confirmation_stays_locked_when_authority_reload_fails(
    dashboard_harness: DashboardHarness,
) -> None:
    """A committed dependency review cannot be repeated on a stale projection."""
    fake = _delivery_ready_fake(
        [
            {
                "node_id": "planning.story_dependencies",
                "instance_key": None,
                "request_kind": "apply_story_dependencies",
                "endpoint": "story/dependencies/apply",
                "transport": "semantic",
            }
        ]
    )
    _seed_progressive_stories(fake, 101, 102)
    story = cast("JsonObject", fake.stories[0])
    story["sprint_selection_state"] = "selected"
    fake.dependency_reload_failure = "Dependency authority reload failed."

    context, page = _open_project_page(dashboard_harness, fake)
    confirm = page.locator('[data-apply-dependencies="true"]')
    expect(confirm).to_be_visible()
    confirm.click()
    page.wait_for_timeout(_UI_SETTLE_MS)

    expect(confirm).to_be_disabled()
    expect(confirm).to_have_attribute("aria-busy", "true")
    expect(page.locator("#project-error")).to_contain_text(
        "Dependency authority reload failed."
    )
    confirm.evaluate("button => button.click()")
    page.wait_for_timeout(_UI_SETTLE_MS)
    assert len(fake.dependency_apply_requests) == 1
    assert fake.api_errors

    fake.dependency_reload_failure = None
    page.locator("#refresh-project").click()
    page.wait_for_timeout(_UI_SETTLE_MS)
    refreshed_confirm = page.locator('[data-apply-dependencies="true"]')
    expect(refreshed_confirm).to_be_enabled()
    refreshed_confirm.click()
    page.wait_for_timeout(_UI_SETTLE_MS)
    assert len(fake.dependency_apply_requests) == _EXPECTED_RETRIED_DEPENDENCY_REQUESTS
    assert not [
        body for suffix, body in fake.delivery_requests if suffix == "/sprint/generate"
    ]

    context.close()


def test_dependency_confirmation_replacement_stays_locked_on_reload_conflict(
    dashboard_harness: DashboardHarness,
) -> None:
    """A 409 rerender cannot replace a committed control with an inert enabled one."""
    fake = _delivery_ready_fake(
        [
            {
                "node_id": "planning.story_dependencies",
                "instance_key": None,
                "request_kind": "apply_story_dependencies",
                "endpoint": "story/dependencies/apply",
                "transport": "semantic",
            }
        ]
    )
    _seed_progressive_stories(fake, 101, 102)
    story = cast("JsonObject", fake.stories[0])
    story["sprint_selection_state"] = "selected"
    fake.dependency_reload_conflict = "Dependency authority projection conflicted."

    context, page = _open_project_page(dashboard_harness, fake)
    page.locator('[data-apply-dependencies="true"]').click()
    page.wait_for_timeout(_UI_SETTLE_MS)

    replacement = page.locator('[data-apply-dependencies="true"]')
    expect(replacement).to_be_disabled()
    expect(replacement).to_have_attribute("aria-disabled", "true")
    expect(replacement).to_have_attribute("aria-busy", "true")
    expect(page.locator("#project-error")).to_contain_text(
        "Dependency authority projection conflicted."
    )
    replacement.evaluate("button => button.click()")
    page.wait_for_timeout(_UI_SETTLE_MS)
    assert len(fake.dependency_apply_requests) == 1

    fake.dependency_reload_conflict = None
    page.locator("#refresh-project").click()
    page.wait_for_timeout(_UI_SETTLE_MS)
    refreshed_confirm = page.locator('[data-apply-dependencies="true"]')
    expect(refreshed_confirm).to_be_enabled()
    refreshed_confirm.click()
    page.wait_for_timeout(_UI_SETTLE_MS)
    assert len(fake.dependency_apply_requests) == _EXPECTED_RETRIED_DEPENDENCY_REQUESTS

    context.close()


def test_dependency_submission_survives_manual_refresh_race(
    dashboard_harness: DashboardHarness,
) -> None:
    """A refresh begun while POST is pending cannot release its mutation token."""
    fake = _delivery_ready_fake(
        [
            {
                "node_id": "planning.story_dependencies",
                "instance_key": None,
                "request_kind": "apply_story_dependencies",
                "endpoint": "story/dependencies/apply",
                "transport": "semantic",
            }
        ]
    )
    _seed_progressive_stories(fake, 101, 102)
    story = cast("JsonObject", fake.stories[0])
    story["sprint_selection_state"] = "selected"

    context, page = _open_project_page(dashboard_harness, fake)
    page.evaluate(
        """() => {
            const originalFetch = window.fetch.bind(window);
            window.issue223DependencyRace = { requests: [], resolvers: [] };
            window.fetch = (input, init = {}) => {
                const url = String(input);
                if (url.endsWith('/story/dependencies/apply')
                        && init.method === 'POST') {
                    window.issue223DependencyRace.requests.push(JSON.parse(init.body));
                    return new Promise((resolve) => {
                        window.issue223DependencyRace.resolvers.push(resolve);
                    });
                }
                return originalFetch(input, init);
            };
        }"""
    )

    page.locator('[data-apply-dependencies="true"]').click()
    page.wait_for_function("window.issue223DependencyRace.requests.length === 1")
    page.locator("#refresh-project").click()
    expect(page.locator("#refresh-project")).to_be_enabled()

    replacement = page.locator('[data-apply-dependencies="true"]')
    submitting_snapshot = replacement.evaluate(
        """button => ({
            disabled: button.disabled,
            ariaDisabled: button.getAttribute('aria-disabled'),
            ariaBusy: button.getAttribute('aria-busy'),
            status: button.closest('[data-dependency-review-section]')
                .querySelector('[data-delivery-action-status="true"]').textContent,
        })"""
    )
    replacement.evaluate("button => button.click()")
    page.wait_for_timeout(_UI_SETTLE_MS)
    page.evaluate(
        """() => {
            const payload = JSON.stringify({
                status: 'success',
                data: { output: { recorded: true } },
            });
            for (const resolve of window.issue223DependencyRace.resolvers.splice(0)) {
                resolve(new Response(payload, {
                    status: 200,
                    headers: { 'Content-Type': 'application/json' },
                }));
            }
        }"""
    )
    page.wait_for_timeout(_UI_SETTLE_MS * 2)

    requests = page.evaluate("window.issue223DependencyRace.requests")
    assert submitting_snapshot == {
        "disabled": True,
        "ariaDisabled": "true",
        "ariaBusy": "true",
        "status": "Dependency review is being submitted; controls remain locked.",
    }
    assert len(requests) == 1
    assert len({request["idempotency_key"] for request in requests}) == 1
    expect(page.locator('[data-apply-dependencies="true"]')).to_be_enabled()

    context.close()


def test_progressive_story_readiness_failure_diagnostics_persist_on_reload(
    dashboard_harness: DashboardHarness,
) -> None:
    """Only stale operational evidence offers structural reconciliation."""
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
            "structurally_eligible": False,
            "structural_eligibility_status": "stale",
            "sprint_selection_state": "selected",
            "sprint_selection_state_fingerprint": _fingerprint("d"),
            "selected_scope_fingerprint": _fingerprint("e"),
            "dependency_safe": False,
            "sprint_candidate": False,
            "content_accepted": True,
            "readiness_blockers": [],
            "validation_status": "validated",
            "validation_failures": [],
        }
    ]

    context, page = _open_project_page(dashboard_harness, fake)

    readiness_section = page.locator('[data-story-readiness-section="true"]')
    expect(readiness_section).to_be_visible()
    expect(readiness_section).to_contain_text("Structural evidence stale")
    expect(readiness_section).to_contain_text("Selected for Sprint")

    validate_button = page.locator(f'[data-story-structural-reconcile-id="{story_id}"]')
    expect(validate_button).to_have_text("Re-run structural checks")

    validate_button.click()
    page.wait_for_timeout(300)

    assert len(fake.structural_reconcile_requests) == 1
    assert fake.structural_reconcile_requests[0]["story_ids"] == [story_id]
    expect(readiness_section).to_contain_text("Selected for Sprint")
    assert not [
        body for suffix, body in fake.delivery_requests if suffix == "/sprint/generate"
    ]

    context.close()


def test_story_review_disables_acceptance_when_required_evidence_is_malformed(
    dashboard_harness: DashboardHarness,
) -> None:
    """Verify incomplete Story evidence disables acceptance."""
    action_pbi2 = _story_generation_action("backlog_item:PBI-000002")
    action_pbi4 = _story_generation_action("backlog_item:PBI-000004")
    actions = [action_pbi2, action_pbi4]

    fake = _delivery_ready_fake(actions)
    fake.position_override = _non_contiguous_story_position(action_pbi2, action_pbi4)
    fake.story_pending_override = _non_contiguous_story_pending()
    # Missing invest_assessment
    fake.planning_review_overrides["/story/reviews"] = {
        "items": [_story_review("backlog_item:PBI-000003", invest_assessment={})]
    }

    context, page = _open_project_page(dashboard_harness, fake)

    review_card = page.locator('[data-planning-review-card="story"]')
    expect(review_card).to_be_visible()
    expect(review_card).to_contain_text("Story review for PBI-000003")

    # Visible evidence error banner
    error_banner = review_card.locator('[data-review-error="invalid-story-evidence"]')
    expect(error_banner).to_be_visible()
    expect(error_banner).to_contain_text(
        "Story proposal cannot be accepted: required INVEST, sizing, or ordering "
        "evidence is missing or malformed. Acceptance is disabled."
    )

    # Incomplete INVEST assessment section rendered
    invalid_invest = review_card.locator('[data-invest-assessment="invalid"]')
    expect(invalid_invest).to_be_visible()
    expect(invalid_invest).to_contain_text("Quality Assessment Incomplete")

    # Accept button is disabled
    accept_btn = review_card.locator(
        '[data-planning-review="story"][data-review-decision="accepted"]'
    )
    expect(accept_btn).to_be_disabled()

    # Request changes and Reject buttons remain enabled
    changes_btn = review_card.locator(
        '[data-planning-review="story"][data-review-decision="feedback"]'
    )
    expect(changes_btn).to_be_enabled()
    reject_btn = review_card.locator(
        '[data-planning-review="story"][data-review-decision="rejected"]'
    )
    expect(reject_btn).to_be_enabled()

    context.close()


def _defer_issue_213_correction(page: Page) -> None:
    """Hold only the Backlog correction POST in the browser, never the route."""
    page.evaluate(
        """(projectId) => {
            const originalFetch = window.fetch.bind(window);
            window.issue213CorrectionRequests = [];
            window.resolveIssue213Correction = null;
            window.fetch = (input, init = {}) => {
                const requestUrl = new URL(
                    input instanceof Request ? input.url : String(input),
                    window.location.href,
                );
                const correctionPath =
                    `/api/projects/${projectId}/backlog/generate`;
                if (requestUrl.origin === window.location.origin
                        && requestUrl.pathname === correctionPath
                        && requestUrl.search === ''
                        && init.method === 'POST') {
                    window.issue213CorrectionRequests.push({
                        headers: Object.fromEntries(new Headers(init.headers)),
                    });
                    return new Promise((resolve) => {
                        window.resolveIssue213Correction = (response) => {
                            window.resolveIssue213Correction = null;
                            resolve(new Response(JSON.stringify(response.body), {
                                status: response.status,
                                headers: { 'Content-Type': 'application/json' },
                            }));
                        };
                    });
                }
                return originalFetch(input, init);
            };
        }""",
        _PROJECT_ID,
    )


def _assert_issue_213_feedback(page: Page, *, status: str) -> Locator:
    continuation = page.locator('[data-backlog-feedback-continuation="true"]')
    expect(continuation).to_be_visible()
    expect(continuation).to_contain_text(status)
    expect(continuation).to_contain_text("Backlog candidate v1 (#7)")
    expect(continuation).to_contain_text("Keep the retry boundary visible.")
    expect(continuation).to_contain_text("Priority")
    expect(continuation).to_contain_text("high")
    expect(continuation).to_contain_text("Value driver")
    expect(continuation).to_contain_text("operator confidence")
    expect(continuation).to_contain_text("Estimated effort")
    expect(continuation).to_contain_text("small")
    expect(continuation).to_contain_text("Justification")
    expect(continuation).to_contain_text("The correction state must survive reload.")
    expect(continuation).to_contain_text("Show the retry boundary.")
    correction = continuation.locator('[data-backlog-correction-action="true"]')
    if "in progress" in status:
        expect(correction).not_to_be_attached()
    else:
        expect(correction).to_be_visible()
        action = correction.locator('button[data-direct-action="record_backlog_draft"]')
        expect(action).to_be_enabled()
        expect(action).to_contain_text("Regenerate Backlog from feedback")
    return continuation


def _request_issue_213_feedback(page: Page) -> None:
    page.locator(
        '[data-planning-review="backlog"][data-review-decision="feedback"]'
    ).click()
    page.locator("#human-action-rationale").fill("Show the retry boundary.")
    page.locator("#human-action-submit").dblclick()
    expect(page.locator("#human-action-dialog")).not_to_be_visible()


def _assert_focus(page: Page, selector: str) -> None:
    assert (
        page.locator(selector).evaluate("element => element === document.activeElement")
        is True
    )


def _assert_issue_213_deferred_endpoint_is_exact(page: Page) -> None:
    """Prove wrong project and prefix POSTs cannot consume the deferred slot."""
    intercepted = page.evaluate(
        f"""() => {{
            const init = {{ method: 'POST', body: JSON.stringify({{}}) }};
            const wrongPrefix =
                '/wrong-api/projects/{_PROJECT_ID}/backlog/generate';
            void window.fetch('/api/projects/{_PROJECT_ID + 1}/backlog/generate', init);
            void window.fetch(wrongPrefix, init);
            return window.issue213CorrectionRequests.length;
        }}"""
    )
    assert intercepted == 0


def _assert_issue_213_corrected_pending(page: Page) -> Locator:
    """Verify corrected review authority has no residual Feedback controls."""
    corrected = page.locator('[data-planning-review-card="backlog"]')
    expect(corrected).to_be_visible()
    expect(corrected).to_contain_text(
        "Corrected Backlog candidate v2 (#8), replacing #7"
    )
    expect(
        corrected.locator(
            '[data-planning-review="backlog"][data-review-decision="accepted"]'
        )
    ).to_be_enabled()
    expect(
        page.locator('[data-backlog-feedback-continuation="true"]')
    ).not_to_be_attached()
    expect(page.locator('[data-backlog-correction-action="true"]')).not_to_be_attached()
    return corrected


def test_issue_213_feedback_context_survives_refresh_and_new_tab(
    dashboard_harness: DashboardHarness,
) -> None:
    """Rebuild durable Feedback context without leaking initiating-tab focus."""
    fake = BacklogFeedbackLifecycle(repositories={})
    context, page = _open_project_page(dashboard_harness, fake)

    _request_issue_213_feedback(page)
    assert len(fake.review_requests) == 1
    continuation = _assert_issue_213_feedback(
        page,
        status="Backlog Feedback recorded",
    )
    correction = page.locator('[data-backlog-correction-action="true"]')
    expect(correction).to_contain_text("Regenerate Backlog from feedback")
    _assert_focus(page, '[data-backlog-correction-action="true"]')

    _defer_issue_213_correction(page)
    _assert_issue_213_deferred_endpoint_is_exact(page)

    page.locator("#refresh-project").click()
    _assert_issue_213_feedback(page, status="Backlog Feedback recorded")

    second = context.new_page()
    second.goto(
        f"{dashboard_harness.url}/project.html?id={_PROJECT_ID}",
        wait_until="networkidle",
    )
    _assert_issue_213_feedback(second, status="Backlog Feedback recorded")
    assert (
        second.evaluate(
            """() => !document.activeElement?.matches(
            '[data-backlog-correction-action="true"], '
            + '[data-backlog-feedback-continuation="true"]'
        )"""
        )
        is True
    )
    assert len(fake.api_errors) == 1
    assert fake.api_errors[0].startswith(
        "Unexpected API path: /api/projects/2/backlog/generate"
    )
    assert continuation.is_visible()
    context.close()


def _assert_issue_213_active_duplicate(
    context: BrowserContext,
    page: Page,
    fake: BacklogFeedbackLifecycle,
    dashboard_harness: DashboardHarness,
) -> None:
    """Verify active state reconstructs and rejects an extra provider entry."""
    page.locator("#refresh-project").click()
    _assert_issue_213_feedback(page, status=_ISSUE_213_ACTIVE_STATUS)
    expect(page.locator('[data-backlog-correction-action="true"]')).not_to_be_attached()

    second = context.new_page()
    second.goto(
        f"{dashboard_harness.url}/project.html?id={_PROJECT_ID}",
        wait_until="networkidle",
    )
    _assert_issue_213_feedback(second, status=_ISSUE_213_ACTIVE_STATUS)
    duplicate = second.evaluate(
        f"""async () => {{
            const url = '/api/projects/{_PROJECT_ID}/backlog/generate';
            const response = await fetch(url, {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{
                    actor: 'dashboard-ui', idempotency_key: 'dashboard-duplicate',
                }}),
            }});
            return {{ status: response.status, body: await response.json() }};
        }}"""
    )
    assert duplicate == {
        "status": _HTTP_CONFLICT,
        "body": {
            "detail": {
                "error": {
                    "code": "TRANSITION_NOT_AVAILABLE",
                    "message": "A Backlog correction is already active.",
                }
            }
        },
    }
    assert fake.provider_entry_count == 1
    expect(
        second.locator('[data-backlog-correction-action="true"]')
    ).not_to_be_attached()
    second.reload(wait_until="networkidle")
    _assert_issue_213_feedback(second, status=_ISSUE_213_ACTIVE_STATUS)
    assert fake.provider_entry_count == 1


def test_issue_213_active_failure_and_expiry_are_durable(
    dashboard_harness: DashboardHarness,
) -> None:
    """Retain durable active, failed, and expired Feedback recovery state."""
    fake = BacklogFeedbackLifecycle(repositories={})
    context, page = _open_project_page(dashboard_harness, fake)
    _request_issue_213_feedback(page)
    _defer_issue_213_correction(page)

    original = page.locator('[data-backlog-correction-action="true"] button')
    original_element = original.element_handle()
    assert original_element is not None
    original.click()
    page.wait_for_function("window.resolveIssue213Correction !== null")
    expect(original).to_be_disabled()
    expect(original).to_have_attribute("aria-busy", "true")
    expect(original).to_contain_text("Regenerating Backlog from feedback...")
    assert page.evaluate("window.issue213CorrectionRequests.length") == 1
    fake.begin_correction()
    assert fake.provider_entry_count == 1

    _assert_issue_213_active_duplicate(context, page, fake, dashboard_harness)

    fake.correction_state = "failed-retry"
    page.evaluate(
        """window.resolveIssue213Correction({
            status: 409,
            body: { detail: { error: {
                code: 'EXTERNAL_EXECUTION_FAILED',
                message: 'The Backlog correction provider failed.',
            } } },
        })"""
    )
    _assert_issue_213_feedback(
        page,
        status=_ISSUE_213_FAILED_STATUS,
    )
    retry = page.locator('[data-backlog-correction-action="true"] button')
    expect(retry).to_be_enabled()
    _assert_focus(page, '[data-backlog-correction-action="true"]')
    assert original_element.evaluate("element => element.disabled") is True
    assert original_element.evaluate("element => element.isConnected") is False

    page.reload(wait_until="networkidle")
    _assert_issue_213_feedback(
        page,
        status=_ISSUE_213_FAILED_STATUS,
    )
    expect(
        page.locator('[data-backlog-correction-action="true"] button')
    ).to_be_enabled()
    third = context.new_page()
    third.goto(
        f"{dashboard_harness.url}/project.html?id={_PROJECT_ID}",
        wait_until="networkidle",
    )
    _assert_issue_213_feedback(
        third,
        status=_ISSUE_213_FAILED_STATUS,
    )
    expect(
        third.locator('[data-backlog-correction-action="true"] button')
    ).to_be_enabled()

    _defer_issue_213_correction(page)
    retry = page.locator('[data-backlog-correction-action="true"] button')
    retry.click()
    page.wait_for_function("window.resolveIssue213Correction !== null")
    fake.begin_correction()
    fake.correction_state = "expired-recovery"
    page.reload(wait_until="networkidle")
    _assert_issue_213_feedback(
        page,
        status=_ISSUE_213_EXPIRED_STATUS,
    )
    expect(
        page.locator('[data-backlog-correction-action="true"] button')
    ).to_be_enabled()
    fourth = context.new_page()
    fourth.goto(
        f"{dashboard_harness.url}/project.html?id={_PROJECT_ID}",
        wait_until="networkidle",
    )
    _assert_issue_213_feedback(
        fourth,
        status=_ISSUE_213_EXPIRED_STATUS,
    )
    expect(
        fourth.locator('[data-backlog-correction-action="true"] button')
    ).to_be_enabled()
    assert fake.api_errors == []
    context.close()


def test_issue_213_correction_reconciles_stale_and_successful_outcomes(
    dashboard_harness: DashboardHarness,
) -> None:
    """Discard stale actions only after authority and retain success uncertainty."""
    fake = BacklogFeedbackLifecycle(repositories={})
    context, page = _open_project_page(dashboard_harness, fake)
    _request_issue_213_feedback(page)
    _defer_issue_213_correction(page)

    stale = page.locator('[data-backlog-correction-action="true"] button')
    stale_element = stale.element_handle()
    assert stale_element is not None
    stale.click()
    page.wait_for_function("window.resolveIssue213Correction !== null")
    fake.correction_state = "replacement"
    page.evaluate(
        """window.resolveIssue213Correction({
            status: 409,
            body: { detail: { error: {
                code: 'STALE_POSITION',
                message: 'The Backlog correction action was replaced.',
            } } },
        })"""
    )
    replacement = page.locator('[data-backlog-correction-action="true"] button')
    expect(replacement).to_be_enabled()
    _assert_focus(page, '[data-backlog-correction-action="true"]')
    assert stale_element.evaluate("element => element.isConnected") is False

    _defer_issue_213_correction(page)
    replacement = page.locator('[data-backlog-correction-action="true"] button')
    replacement.click()
    page.wait_for_function("window.resolveIssue213Correction !== null")
    fake.begin_correction()
    fake.correction_state = "success"
    fake.backlog_read_failures = 1
    page.evaluate(
        """window.resolveIssue213Correction({
            status: 200,
            body: { status: 'success', data: { output: { recorded: true } } },
        })"""
    )
    expect(page.locator("#project-error")).to_contain_text(
        "The authoritative Backlog projection is temporarily unavailable."
    )
    expect(
        page.locator('[data-backlog-correction-action="true"] button')
    ).not_to_be_attached()
    expect(replacement).not_to_be_attached()

    page.locator("#refresh-project").click()
    _assert_issue_213_corrected_pending(page)
    _assert_focus(page, '[data-planning-review-card="backlog"]')

    page.reload(wait_until="networkidle")
    _assert_issue_213_corrected_pending(page)
    second = context.new_page()
    second.goto(
        f"{dashboard_harness.url}/project.html?id={_PROJECT_ID}",
        wait_until="networkidle",
    )
    _assert_issue_213_corrected_pending(second)
    assert fake.api_errors == []
    context.close()
