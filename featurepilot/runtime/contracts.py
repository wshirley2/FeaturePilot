"""Stable identity and result contracts shared by FeaturePilot runtimes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class RuntimeMode(str, Enum):
    """Supported interaction paths over the same Task Runtime."""

    CHAT = "chat"
    MANAGED_RUN = "managed_run"

    @property
    def display_name(self) -> str:
        return "Chat" if self is RuntimeMode.CHAT else "Managed Run"


class RuntimeResultScope(str, Enum):
    """Execution boundary summarized by one Runtime result."""

    TURN = "turn"
    RUN = "run"


class RuntimeResultStatus(str, Enum):
    """Terminal outcomes shared by Chat turns and Managed Runs."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LIMIT_REACHED = "limit_reached"
    ESCALATION_REQUIRED = "escalation_required"


@dataclass(frozen=True, slots=True)
class TaskRuntimePaths:
    """Canonical persistence and working-directory boundaries for one Runtime."""

    mode: RuntimeMode
    working_directory: Path
    session_directory: Path
    run_directory: Path | None = None

    def __post_init__(self) -> None:
        mode = RuntimeMode(self.mode)
        working_directory = self.working_directory.resolve()
        session_directory = self.session_directory.resolve()
        run_directory = self.run_directory.resolve() if self.run_directory is not None else None
        if mode is RuntimeMode.CHAT and run_directory is not None:
            raise ValueError("Chat Runtime cannot have a Managed Run directory")
        if mode is RuntimeMode.MANAGED_RUN:
            if run_directory is None:
                raise ValueError("Managed Run Runtime requires a Run directory")
            if working_directory != run_directory / "workspace":
                raise ValueError("Managed Run working directory must be <run>/workspace")
            if session_directory != run_directory / "sessions":
                raise ValueError("Managed Run Session directory must be <run>/sessions")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "working_directory", working_directory)
        object.__setattr__(self, "session_directory", session_directory)
        object.__setattr__(self, "run_directory", run_directory)

    @classmethod
    def for_runtime(
        cls,
        mode: RuntimeMode,
        working_directory: Path,
        session_directory: Path | None = None,
    ) -> TaskRuntimePaths:
        """Build the default path layout for Chat or one isolated Managed Run."""

        runtime_mode = RuntimeMode(mode)
        working = working_directory.resolve()
        if runtime_mode is RuntimeMode.CHAT:
            sessions = (
                session_directory.resolve()
                if session_directory is not None
                else working / ".featurepilot" / "sessions"
            )
            return cls(runtime_mode, working, sessions)
        run_directory = working.parent
        sessions = run_directory / "sessions"
        if session_directory is not None and session_directory.resolve() != sessions:
            raise ValueError("Managed Run Session directory is fixed at <run>/sessions")
        return cls(runtime_mode, working, sessions, run_directory)

    def require_run_directory(self) -> Path:
        if self.run_directory is None:
            raise ValueError("This Runtime does not have Managed Run artifacts")
        return self.run_directory

    @property
    def run_metadata_path(self) -> Path:
        return self.require_run_directory() / "run.json"

    @property
    def events_path(self) -> Path:
        return self.require_run_directory() / "events.jsonl"

    @property
    def validation_path(self) -> Path:
        return self.require_run_directory() / "validation.json"

    @property
    def patch_path(self) -> Path:
        return self.require_run_directory() / "changes.patch"

    @property
    def report_path(self) -> Path:
        return self.require_run_directory() / "report.md"

    def to_dict(self) -> dict[str, str | None]:
        return {
            "mode": self.mode.value,
            "working_directory": str(self.working_directory),
            "session_directory": str(self.session_directory),
            "run_directory": str(self.run_directory) if self.run_directory is not None else None,
        }


@dataclass(frozen=True, slots=True)
class TaskRuntimeResult:
    """Immutable terminal result for one turn or one complete managed run."""

    scope: RuntimeResultScope
    status: RuntimeResultStatus
    response: str = ""
    reason: str | None = None
    error_type: str | None = None
    limit: str | None = None
    actual: int | float | None = None
    maximum: int | float | None = None

    def __post_init__(self) -> None:
        scope = RuntimeResultScope(self.scope)
        status = RuntimeResultStatus(self.status)
        if status is RuntimeResultStatus.SUCCEEDED:
            if self.error_type is not None or self.limit is not None:
                raise ValueError("A successful Runtime result cannot contain an error or limit")
        elif not self.reason:
            raise ValueError("A non-success Runtime result requires a reason")
        if status is RuntimeResultStatus.FAILED and not self.error_type:
            raise ValueError("A failed Runtime result requires an error type")
        if status is RuntimeResultStatus.LIMIT_REACHED and not self.limit:
            raise ValueError("A limit-reached Runtime result requires a limit name")
        if status is not RuntimeResultStatus.LIMIT_REACHED and (
            self.limit is not None or self.actual is not None or self.maximum is not None
        ):
            raise ValueError("Limit facts are only valid for a limit-reached Runtime result")
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "status", status)

    @classmethod
    def from_terminal_event(
        cls,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> TaskRuntimeResult | None:
        """Project an existing terminal Runtime Event into the shared contract."""

        if event_type == "turn_completed":
            return cls(
                scope=RuntimeResultScope.TURN,
                status=RuntimeResultStatus.SUCCEEDED,
                response=_string(payload.get("content")),
            )
        if event_type == "turn_interrupted":
            return cls(
                scope=RuntimeResultScope.TURN,
                status=RuntimeResultStatus.CANCELLED,
                reason=_string(payload.get("reason")) or "Runtime turn interrupted",
            )
        if event_type == "turn_failed":
            return cls(
                scope=RuntimeResultScope.TURN,
                status=RuntimeResultStatus.FAILED,
                reason=_string(payload.get("error")) or "Runtime turn failed",
                error_type=_string(payload.get("error_type")) or "RuntimeError",
            )
        if event_type == "turn_limit_reached":
            limit = _string(payload.get("limit")) or "tool_rounds"
            return cls(
                scope=RuntimeResultScope.TURN,
                status=RuntimeResultStatus.LIMIT_REACHED,
                response=_string(payload.get("result")),
                reason=f"Runtime limit reached: {limit}",
                limit=limit,
                actual=_number(payload.get("actual", payload.get("max_rounds"))),
                maximum=_number(payload.get("maximum", payload.get("max_rounds"))),
            )
        return None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TaskRuntimeResult:
        return cls(
            scope=RuntimeResultScope(str(value["scope"])),
            status=RuntimeResultStatus(str(value["status"])),
            response=_string(value.get("response")),
            reason=_optional_string(value.get("reason")),
            error_type=_optional_string(value.get("error_type")),
            limit=_optional_string(value.get("limit")),
            actual=_number(value.get("actual")),
            maximum=_number(value.get("maximum")),
        )

    def to_dict(self) -> dict[str, str | int | float | None]:
        return {
            "scope": self.scope.value,
            "status": self.status.value,
            "response": self.response,
            "reason": self.reason,
            "error_type": self.error_type,
            "limit": self.limit,
            "actual": self.actual,
            "maximum": self.maximum,
        }


@dataclass(frozen=True, slots=True)
class TaskRuntimeIdentity:
    """Immutable correlation facts for one assembled Task Runtime."""

    mode: RuntimeMode
    session_id: str
    working_directory: Path
    source_repository: Path
    task_id: str | None = None
    run_id: str | None = None

    def __post_init__(self) -> None:
        mode = RuntimeMode(self.mode)
        session_id = self.session_id.strip()
        task_id = self.task_id.strip() if self.task_id is not None else None
        run_id = self.run_id.strip() if self.run_id is not None else None
        if not session_id:
            raise ValueError("Task Runtime requires a non-empty session id")
        if self.task_id is not None and not task_id:
            raise ValueError("Task Runtime task id cannot be empty")
        if self.run_id is not None and not run_id:
            raise ValueError("Task Runtime run id cannot be empty")
        if run_id is not None and task_id is None:
            raise ValueError("A Runtime run id requires a task id")
        if mode is RuntimeMode.MANAGED_RUN and run_id is None:
            raise ValueError("Managed Run Runtime requires task and run ids")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "task_id", task_id)
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "working_directory", self.working_directory.resolve())
        object.__setattr__(self, "source_repository", self.source_repository.resolve())

    @property
    def workspace_path(self) -> Path | None:
        """Return the isolated workspace when execution is not in the source repository."""

        if self.working_directory == self.source_repository:
            return None
        return self.working_directory

    def to_dict(self) -> dict[str, str | None]:
        return {
            "mode": self.mode.value,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "source_repository": str(self.source_repository),
            "working_directory": str(self.working_directory),
            "workspace_path": str(self.workspace_path) if self.workspace_path else None,
        }


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _number(value: object) -> int | float | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, (int, float)) else None
