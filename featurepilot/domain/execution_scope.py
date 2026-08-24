"""Approved execution facts shared by Plan and Chat isolation paths."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .plan import PlanRecord
from .task import Task


@dataclass(frozen=True, slots=True)
class ExecutionScope:
    """The bounded facts authorised for one isolated execution attempt.

    A scope is not a Plan. A saved, approved Plan can be adapted to one, while
    a Chat escalation records only the concrete Tool Call approved for an
    isolated Workspace.
    """

    task: Task
    source_repository: Path
    reference: str
    summary: str
    steps: tuple[str, ...] = ()
    read_files: tuple[str, ...] = ()
    modify_files: tuple[str, ...] = ()
    expected_files: tuple[str, ...] = ()
    validation_commands: tuple[tuple[str, ...], ...] = ()
    allowed_commands: tuple[tuple[str, ...], ...] = ()
    allowed_effects: frozenset[str] = field(default_factory=lambda: frozenset({"read", "write", "execute"}))
    risks: tuple[str, ...] = ()
    trigger_reason: str = ""
    trigger_reasons: tuple[dict[str, Any], ...] = ()
    chat_session_id: str | None = None
    chat_turn_id: str | None = None
    trigger_tool_call_id: str | None = None
    trigger_tool_name: str | None = None
    trigger_arguments: dict[str, Any] = field(default_factory=dict)
    plan_id: str | None = None
    run_id: str | None = None
    workspace_path: Path | None = None

    def __post_init__(self) -> None:
        source = self.source_repository.resolve()
        workspace = self.workspace_path.resolve() if self.workspace_path is not None else None
        if not self.reference:
            raise ValueError("ExecutionScope requires a display reference")
        if not self.summary:
            raise ValueError("ExecutionScope requires a summary")
        if self.workspace_path is None and self.run_id is not None:
            raise ValueError("An ExecutionScope run id requires a Workspace path")
        if self.workspace_path is not None and self.run_id is None:
            raise ValueError("An ExecutionScope Workspace requires a run id")
        object.__setattr__(self, "source_repository", source)
        object.__setattr__(self, "workspace_path", workspace)
        object.__setattr__(self, "trigger_arguments", dict(self.trigger_arguments))
        object.__setattr__(self, "trigger_reasons", tuple(dict(reason) for reason in self.trigger_reasons))

    @classmethod
    def from_plan(cls, record: PlanRecord) -> ExecutionScope:
        """Adapt an approved persisted Plan without changing its lifecycle."""

        if record.status != "approved":
            raise ValueError("ExecutionScope requires an approved PlanRecord")
        task = record.task or Task(
            project_id=record.repository,
            description=record.plan.summary,
            id=record.plan.task_id,
        )
        return cls(
            task=task,
            source_repository=Path(record.repository),
            reference=record.reference,
            summary=record.plan.summary,
            steps=tuple(record.plan.steps),
            read_files=tuple(record.plan.read_files),
            modify_files=tuple(record.plan.modify_files),
            expected_files=tuple(record.plan.expected_files),
            validation_commands=tuple(tuple(command) for command in record.plan.validation_commands),
            allowed_commands=tuple(tuple(command) for command in record.plan.validation_commands),
            risks=tuple(record.plan.risks),
            trigger_reason="Approved Plan",
            plan_id=record.id,
        )

    @classmethod
    def from_chat_escalation(
        cls,
        *,
        task: Task,
        source_repository: Path,
        chat_session_id: str,
        chat_turn_id: str | None,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        summary: dict[str, Any],
        reasons: list[dict[str, Any]],
        validation_commands: list[list[str]] | tuple[tuple[str, ...], ...] = (),
        trigger_reason: str,
    ) -> ExecutionScope:
        """Create a narrow scope from the unexecuted Chat Tool Call."""

        paths = tuple(
            str(path)
            for path in summary.get("affected_paths", [])
            if isinstance(path, str) and path
        )
        operation = summary.get("operation")
        return cls(
            task=task,
            source_repository=source_repository,
            reference=f"chat-{chat_session_id[:8]}-{tool_call_id[:8]}",
            summary=f"Isolated continuation of Chat Tool Call {tool_name}",
            steps=(f"Execute the approved Chat operation: {tool_name}",),
            modify_files=paths if operation in {"write", "delete", "move", "rename"} else (),
            validation_commands=tuple(tuple(command) for command in validation_commands),
            risks=("Triggered by Chat execution control routing.",),
            trigger_reason=trigger_reason,
            trigger_reasons=tuple(reasons),
            chat_session_id=chat_session_id,
            chat_turn_id=chat_turn_id,
            trigger_tool_call_id=tool_call_id,
            trigger_tool_name=tool_name,
            trigger_arguments=arguments,
        )

    def with_execution(self, *, run_id: str, workspace_path: Path) -> ExecutionScope:
        """Bind the already approved scope to its newly created Run boundary."""

        return ExecutionScope(
            task=self.task,
            source_repository=self.source_repository,
            reference=self.reference,
            summary=self.summary,
            steps=self.steps,
            read_files=self.read_files,
            modify_files=self.modify_files,
            expected_files=self.expected_files,
            validation_commands=self.validation_commands,
            allowed_commands=self.allowed_commands,
            allowed_effects=self.allowed_effects,
            risks=self.risks,
            trigger_reason=self.trigger_reason,
            trigger_reasons=self.trigger_reasons,
            chat_session_id=self.chat_session_id,
            chat_turn_id=self.chat_turn_id,
            trigger_tool_call_id=self.trigger_tool_call_id,
            trigger_tool_name=self.trigger_tool_name,
            trigger_arguments=self.trigger_arguments,
            plan_id=self.plan_id,
            run_id=run_id,
            workspace_path=workspace_path,
        )

    @property
    def is_plan_backed(self) -> bool:
        return self.plan_id is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "task": self.task.to_dict(),
            "source_repository": str(self.source_repository),
            "reference": self.reference,
            "summary": self.summary,
            "steps": list(self.steps),
            "read_files": list(self.read_files),
            "modify_files": list(self.modify_files),
            "expected_files": list(self.expected_files),
            "validation_commands": [list(command) for command in self.validation_commands],
            "allowed_commands": [list(command) for command in self.allowed_commands],
            "allowed_effects": sorted(self.allowed_effects),
            "risks": list(self.risks),
            "trigger_reason": self.trigger_reason,
            "trigger_reasons": [dict(reason) for reason in self.trigger_reasons],
            "chat_session_id": self.chat_session_id,
            "chat_turn_id": self.chat_turn_id,
            "trigger_tool_call_id": self.trigger_tool_call_id,
            "trigger_tool_name": self.trigger_tool_name,
            "trigger_arguments": dict(self.trigger_arguments),
            "plan_id": self.plan_id,
            "run_id": self.run_id,
            "workspace_path": str(self.workspace_path) if self.workspace_path else None,
        }
