"""Workflow session orchestration service and legacy migration helpers."""

import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from google.adk.sessions.database_session_service import DatabaseSessionService

from orchestrator_agent.fsm.controller import FSMController
from orchestrator_agent.fsm.states import OrchestratorState
from repositories.product import ProductRepository
from repositories.session import WorkflowSessionRepository
from services.orchestrator_query_service import get_real_business_state
from utils.runtime_config import WORKFLOW_RUNNER_IDENTITY, RunnerIdentity

if TYPE_CHECKING:
    from orchestrator_agent.fsm.definitions import StateDefinition

logger: logging.Logger = logging.getLogger(__name__)


class WorkflowService:
    """Application service coordinating session state and optional ADK-driven turns."""

    def __init__(
        self,
        runner_identity: RunnerIdentity = WORKFLOW_RUNNER_IDENTITY,
    ) -> None:
        """Initialize workflow dependencies for a single runner identity."""
        self.runner_identity: RunnerIdentity = runner_identity
        self.app_name: str = runner_identity.app_name
        self.user_id: str = runner_identity.user_id

        self.fsm = FSMController()
        self.session_repo = WorkflowSessionRepository()
        self.product_repo = ProductRepository()

    async def initialize_session(self, session_id: str | None = None) -> str:
        """Create a new workflow session and return its ID."""
        if not session_id:
            session_id = str(uuid.uuid4())

        initial_state: dict[str, Any] = get_real_business_state()
        initial_state["fsm_state"] = OrchestratorState.SETUP_REQUIRED.value
        initial_state["fsm_state_entered_at"] = (
            datetime.now(UTC).isoformat().replace("+00:00", "Z")
        )

        session_service: Any = DatabaseSessionService(self.session_repo.adk_db_url)
        try:
            await session_service.create_session(
                app_name=self.app_name,
                user_id=self.user_id,
                session_id=session_id,
                state=initial_state,
            )
        finally:
            await session_service.close()
        logger.info("Initialized new UI Workflow Session: %s", session_id)
        return session_id

    def get_session_status(self, session_id: str) -> dict[str, Any]:
        """Return the current session state payload."""
        return self.session_repo.get_session_state(
            app_name=self.app_name,
            user_id=self.user_id,
            session_id=session_id,
        )

    def get_session_statuses(self, session_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Return current session state payloads keyed by session ID."""
        return self.session_repo.get_session_states_batch(
            app_name=self.app_name,
            user_id=self.user_id,
            session_ids=session_ids,
        )

    def update_session_status(
        self, session_id: str, partial_update: dict[str, Any]
    ) -> None:
        """Apply partial update to session state."""
        self.session_repo.update_session_state(
            app_name=self.app_name,
            user_id=self.user_id,
            session_id=session_id,
            partial_update=partial_update,
        )

    def delete_session(self, session_id: str) -> bool:
        """Delete a workflow session state from the database."""
        return self.session_repo.delete_session(
            app_name=self.app_name,
            user_id=self.user_id,
            session_id=session_id,
        )

    def migrate_legacy_setup_state(self) -> int:
        """One-time migration: convert legacy ROUTING_MODE session states."""
        if not self.session_repo.has_sessions_table():
            logger.debug(
                "Session store is not initialized yet; skipping legacy migration."
            )
            return 0

        migrated = 0
        with sqlite3.connect(self.session_repo.db_path) as conn:
            cursor: sqlite3.Cursor = conn.cursor()
            cursor.execute(
                "SELECT id, state FROM sessions WHERE app_name=? AND user_id=?",
                (self.app_name, self.user_id),
            )
            rows: list[Any] = cursor.fetchall()

            for session_id, state_json in rows:
                try:
                    state = json.loads(state_json or "{}")
                except json.JSONDecodeError:
                    continue

                if state.get("fsm_state") != "ROUTING_MODE":
                    continue

                state["fsm_state"] = OrchestratorState.SETUP_REQUIRED.value
                cursor.execute(
                    "UPDATE sessions SET state=? "
                    "WHERE app_name=? AND user_id=? AND id=?",
                    (
                        json.dumps(state),
                        self.app_name,
                        self.user_id,
                        session_id,
                    ),
                )
                migrated += 1

            conn.commit()

        return migrated

    def advance_fsm_to_next_phase(
        self,
        session_id: str,
        trigger_tool_name: str,
        tool_output: dict[str, Any],
        user_input: str,
    ) -> str:
        """Force-evaluate FSM using a simulated tool result."""
        current_data: dict[str, Any] = self.get_session_status(session_id)
        current_state_key = current_data.get(
            "fsm_state", OrchestratorState.SETUP_REQUIRED.value
        )

        try:
            current_state = OrchestratorState(current_state_key)
        except ValueError:
            current_state: Literal[OrchestratorState.SETUP_REQUIRED] = (
                OrchestratorState.SETUP_REQUIRED
            )

        next_state: OrchestratorState = self.fsm.determine_next_state(
            current_state=current_state,
            tool_name=trigger_tool_name,
            tool_output=tool_output,
            user_input=user_input,
        )

        self.update_session_status(
            session_id,
            {
                "fsm_state": next_state.value,
                "fsm_state_entered_at": datetime.now(UTC)
                .isoformat()
                .replace("+00:00", "Z"),
            },
        )
        return next_state.value

    async def trigger_agent_turn(
        self, session_id: str, user_input: str
    ) -> dict[str, Any]:
        """
        Legacy generic agent turn entrypoint.

        Kept for non-dashboard callers.
        """
        from google.adk.runners import Runner  # noqa: PLC0415
        from google.genai import types  # noqa: PLC0415

        from orchestrator_agent.agent import root_agent  # noqa: PLC0415

        session_service = DatabaseSessionService(self.session_repo.adk_db_url)
        try:
            runner = Runner(
                agent=root_agent,
                app_name=self.app_name,
                session_service=session_service,
            )

            full_state: dict[str, Any] = self.get_session_status(session_id)
            current_state_key: Any = full_state.get(
                "fsm_state", OrchestratorState.SETUP_REQUIRED.value
            )
            try:
                current_state = OrchestratorState(current_state_key)
            except ValueError:
                current_state: Literal[OrchestratorState.SETUP_REQUIRED] = (
                    OrchestratorState.SETUP_REQUIRED
                )

            state_def: StateDefinition = self.fsm.get_state_definition(current_state)

            inner_agent: Any = getattr(runner.agent, "agent", None)
            if inner_agent:
                inner_agent.instruction = state_def.instruction
                inner_agent.tools = state_def.tools

            vision_draft = full_state.get("vision_components", "NO_HISTORY")
            prompt_with_state = (
                f"<prior_vision_state>\n{vision_draft}\n</prior_vision_state>\n\n"
                f"<user_raw_text>\n{user_input}\n</user_raw_text>"
            )

            new_message = types.Content(
                role="user",
                parts=[types.Part.from_text(text=prompt_with_state)],
            )

            full_response_text = ""
            latest_tool_data: dict[str, Any] = {}
            last_tool_name = None

            logger.info(
                "Triggering background agent for session %s in state %s",
                session_id,
                current_state.value,
            )

            async for event in runner.run_async(
                user_id=self.user_id,
                session_id=session_id,
                new_message=new_message,
            ):
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if part.function_response:
                            last_tool_name: str | None = part.function_response.name
                            latest_tool_data = part.function_response.response or {}
                        if part.text:
                            full_response_text += part.text

            next_state: OrchestratorState = self.fsm.determine_next_state(
                current_state=current_state,
                tool_name=last_tool_name,
                tool_output=latest_tool_data,
                user_input=user_input,
            )

            update_payload: dict[str, Any] = {"fsm_state": next_state.value}
            if next_state != current_state:
                update_payload["fsm_state_entered_at"] = (
                    datetime.now(UTC).isoformat().replace("+00:00", "Z")
                )

            if latest_tool_data and "updated_components" in latest_tool_data:
                update_payload["vision_components"] = latest_tool_data[
                    "updated_components"
                ]

            self.update_session_status(session_id, update_payload)

            return {
                "previous_state": current_state.value,
                "new_state": next_state.value,
                "tool_executed": last_tool_name,
                "response_text": full_response_text,
            }
        finally:
            await session_service.close()
