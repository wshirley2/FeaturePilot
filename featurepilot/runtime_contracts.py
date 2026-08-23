"""Stable identity contracts shared by FeaturePilot runtime modes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class RuntimeMode(str, Enum):
    """Supported interaction paths over the same Task Runtime."""

    CHAT = "chat"
    MANAGED_RUN = "managed_run"

    @property
    def display_name(self) -> str:
        return "Chat" if self is RuntimeMode.CHAT else "Managed Run"


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
