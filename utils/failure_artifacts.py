"""Helpers for persisting structured failure artifacts from agent runtimes."""

from __future__ import annotations

import hashlib
import json
import traceback
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict, Unpack

from utils.runtime_ownership import runtime_output_root

REPO_ROOT = Path(__file__).resolve().parents[1]
_OUTPUT_ROOT = runtime_output_root()
LOGS_DIR = (_OUTPUT_ROOT or REPO_ROOT) / "logs"
FAILURES_DIR = (
    _OUTPUT_ROOT / "artifacts" / "failures"
    if _OUTPUT_ROOT is not None
    else LOGS_DIR / "failures"
)
RAW_OUTPUT_PREVIEW_LIMIT = 500

type JsonPrimitive = None | bool | int | float | str
type JsonValue = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]
type FailureMetadataValue = str | bool | None


class FailureMetadataDict(TypedDict):
    """Serialized form of failure metadata returned to runtime callers."""

    failure_artifact_id: str
    failure_stage: str
    failure_summary: str
    raw_output_preview: str | None
    has_full_artifact: bool


class FailureArtifactOptions(TypedDict, total=False):
    """Optional artifact fields accepted by write_failure_artifact."""

    raw_output: str | None
    context: Mapping[str, object] | None
    model_info: Mapping[str, object] | None
    validation_errors: object | None
    exception: BaseException | None
    traceback_text: str | None
    extra: Mapping[str, object] | None


class FailureArtifactResult(TypedDict):
    """Return payload for persisted failure artifacts."""

    artifact: dict[str, JsonValue]
    artifact_path: Path
    metadata: FailureMetadataDict


@dataclass(frozen=True)
class FailureMetadata:
    """Compact metadata attached to phase responses after a failure."""

    failure_artifact_id: str
    failure_stage: str
    failure_summary: str
    raw_output_preview: str | None
    has_full_artifact: bool

    def as_dict(self) -> FailureMetadataDict:
        """Return a JSON-serializable dictionary for API responses."""
        return {
            "failure_artifact_id": self.failure_artifact_id,
            "failure_stage": self.failure_stage,
            "failure_summary": self.failure_summary,
            "raw_output_preview": self.raw_output_preview,
            "has_full_artifact": self.has_full_artifact,
        }


class AgentInvocationError(RuntimeError):
    """Raised when an ADK runner fails after producing partial output."""

    def __init__(
        self,
        message: str,
        *,
        partial_output: str | None = None,
        event_count: int = 0,
        validation_errors: list[dict[str, object]] | None = None,
    ) -> None:
        """Store partial output and validation context alongside the error."""
        super().__init__(message)
        self.partial_output = partial_output
        self.event_count = event_count
        self.validation_errors = validation_errors


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _normalize_jsonable_source(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump()
    return value


def _jsonable(value: object) -> JsonValue:
    normalized = _normalize_jsonable_source(value)
    if isinstance(normalized, Mapping):
        return {str(key): _jsonable(item) for key, item in normalized.items()}
    if isinstance(normalized, (list, tuple, set)):
        return [_jsonable(item) for item in normalized]
    if isinstance(normalized, Path):
        return str(normalized)
    if isinstance(normalized, (str, int, float, bool)) or normalized is None:
        return normalized
    return str(normalized)


def _preview_text(raw_output: str | None) -> str | None:
    if not raw_output:
        return None
    return raw_output[:RAW_OUTPUT_PREVIEW_LIMIT]


def build_failure_metadata(
    *,
    artifact_id: str,
    failure_stage: str,
    failure_summary: str,
    raw_output: str | None,
) -> FailureMetadata:
    """Build the lightweight metadata returned alongside a stored artifact."""
    return FailureMetadata(
        failure_artifact_id=artifact_id,
        failure_stage=failure_stage,
        failure_summary=failure_summary,
        raw_output_preview=_preview_text(raw_output),
        has_full_artifact=True,
    )


def _artifact_path(phase: str, artifact_id: str) -> Path:
    return FAILURES_DIR / phase / f"{artifact_id}.json"


def _artifact_id(
    *,
    phase: str,
    project_id: int | None,
    failure_stage: str,
    failure_summary: str,
    raw_output: str | None,
) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    digest = hashlib.sha256(
        json.dumps(
            {
                "phase": phase,
                "project_id": project_id,
                "failure_stage": failure_stage,
                "failure_summary": failure_summary,
                "raw_output": raw_output or "",
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()[:12]
    return f"{phase}-{timestamp}-{digest}"


def write_failure_artifact(
    *,
    phase: str,
    project_id: int | None,
    failure_stage: str,
    failure_summary: str,
    **options: Unpack[FailureArtifactOptions],
) -> FailureArtifactResult:
    """Persist a structured failure artifact and return its metadata."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    FAILURES_DIR.mkdir(parents=True, exist_ok=True)

    raw_output = options.get("raw_output")
    context = options.get("context")
    model_info = options.get("model_info")
    validation_errors = options.get("validation_errors")
    exception = options.get("exception")
    traceback_text = options.get("traceback_text")
    extra = options.get("extra")

    artifact_id = _artifact_id(
        phase=phase,
        project_id=project_id,
        failure_stage=failure_stage,
        failure_summary=failure_summary,
        raw_output=raw_output,
    )
    artifact_path = _artifact_path(phase, artifact_id)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)

    exception_type = type(exception).__name__ if exception else None
    exception_message = str(exception) if exception else None
    if traceback_text is None and exception is not None:
        traceback_text = "".join(
            traceback.format_exception(
                type(exception), exception, exception.__traceback__
            )
        )

    artifact: dict[str, JsonValue] = {
        "artifact_id": artifact_id,
        "created_at": _now_iso(),
        "phase": phase,
        "project_id": project_id,
        "failure_stage": failure_stage,
        "failure_summary": failure_summary,
        "raw_output": raw_output,
        "raw_output_length": len(raw_output) if raw_output is not None else 0,
        "context": _jsonable(context),
        "model_info": _jsonable(model_info),
        "validation_errors": _jsonable(validation_errors),
        "exception_type": exception_type,
        "exception_message": exception_message,
        "traceback": traceback_text,
        "extra": _jsonable(extra),
    }
    artifact_path.write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    metadata = build_failure_metadata(
        artifact_id=artifact_id,
        failure_stage=failure_stage,
        failure_summary=failure_summary,
        raw_output=raw_output,
    )
    return {
        "artifact": artifact,
        "artifact_path": artifact_path,
        "metadata": metadata.as_dict(),
    }


def read_failure_artifact(artifact_id: str) -> dict[str, JsonValue] | None:
    """Load a previously persisted failure artifact by artifact ID."""
    if not artifact_id.strip():
        return None

    for path in FAILURES_DIR.glob(f"*/*{artifact_id}.json"):
        if path.stem == artifact_id:
            loaded = _jsonable(json.loads(path.read_text(encoding="utf-8")))
            if isinstance(loaded, dict):
                return loaded
    return None
