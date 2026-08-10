"""Structured implementation plan domain models."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .task import Task

PLAN_STATUSES = {"draft", "approved", "rejected"}


class Plan(BaseModel):
    """A reviewable plan produced before code changes are made."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    summary: str
    steps: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    read_files: list[str] = Field(default_factory=list)
    modify_files: list[str] = Field(default_factory=list)
    expected_files: list[str] = Field(default_factory=list)
    validation_commands: list[list[str]] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    id: str = Field(default_factory=lambda: uuid4().hex)

    def to_dict(self) -> dict[str, object]:
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Plan":
        return cls.model_validate(data)


@dataclass(slots=True)
class PlanRecord:
    """A persisted, reviewable version of a generated Plan."""

    plan: Plan
    repository: str
    version: int
    task: Task | None = None
    name: str = ""
    status: str = "draft"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    decision_reason: str | None = None
    decided_at: str | None = None

    def __post_init__(self) -> None:
        if self.status not in PLAN_STATUSES:
            allowed = ", ".join(sorted(PLAN_STATUSES))
            raise ValueError(f"Unsupported plan status {self.status!r}; expected one of: {allowed}")
        if self.version < 1:
            raise ValueError("Plan version must be at least 1")
        if self.task and self.task.id != self.plan.task_id:
            raise ValueError("PlanRecord task id must match the contained Plan task id")

    @property
    def id(self) -> str:
        """Expose the contained Plan identifier for storage and CLI lookup."""

        return self.plan.id

    @property
    def reference(self) -> str:
        """Return the human-friendly stable reference for this Plan version."""

        name = self.name or f"task-{self.plan.task_id[:8]}"
        return f"{name}-v{self.version}"

    def decide(self, status: str, reason: str | None = None) -> None:
        """Record one final human decision for a draft Plan."""

        if status not in {"approved", "rejected"}:
            raise ValueError("Plan decisions must be 'approved' or 'rejected'")
        if self.status != "draft":
            raise ValueError(f"Only draft plans can be decided; current status is {self.status!r}")
        if status == "rejected" and not reason:
            raise ValueError("A rejection reason is required")
        self.status = status
        self.decision_reason = reason
        self.decided_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, object]:
        return {
            "plan": self.plan.to_dict(),
            "repository": self.repository,
            "version": self.version,
            "task": self.task.to_dict() if self.task else None,
            "name": self.name,
            "reference": self.reference,
            "status": self.status,
            "created_at": self.created_at,
            "decision_reason": self.decision_reason,
            "decided_at": self.decided_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "PlanRecord":
        plan_data = data.get("plan")
        if not isinstance(plan_data, dict):
            raise TypeError("Plan record must contain a plan object")
        task_data = data.get("task")
        if task_data is not None and not isinstance(task_data, dict):
            raise TypeError("Plan record task must be an object or null")
        return cls(
            plan=Plan.from_dict(plan_data),
            repository=str(data["repository"]),
            version=int(data["version"]),
            task=Task.from_dict(task_data) if task_data else None,
            name=str(data.get("name", "")),
            status=str(data.get("status", "draft")),
            created_at=str(data["created_at"]),
            decision_reason=data.get("decision_reason") if isinstance(data.get("decision_reason"), str) else None,
            decided_at=data.get("decided_at") if isinstance(data.get("decided_at"), str) else None,
        )
