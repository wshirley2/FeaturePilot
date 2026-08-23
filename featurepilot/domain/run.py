"""Execution run domain model."""

from dataclasses import asdict, dataclass, field
from uuid import uuid4

RUN_STATUSES = {"created", "running", "succeeded", "failed", "cancelled", "limit_reached"}
_RUN_TRANSITIONS = {
    "created": {"running"},
    "running": {"succeeded", "failed", "cancelled", "limit_reached"},
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
    "limit_reached": set(),
}


@dataclass(slots=True)
class Run:
    """One execution attempt for an approved task plan."""

    task_id: str
    plan_id: str | None = None
    workspace_path: str | None = None
    source_snapshot: str | None = None
    status: str = "created"
    result: dict[str, object] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        if self.status not in RUN_STATUSES:
            allowed = ", ".join(sorted(RUN_STATUSES))
            raise ValueError(f"Unsupported run status {self.status!r}; expected one of: {allowed}")

    @property
    def display_id(self) -> str:
        """Short identifier used in terminal output and workspace paths."""

        return self.id[:8]

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["display_id"] = self.display_id
        return data

    def transition(self, status: str, *, result: dict[str, object] | None = None) -> None:
        """Move to the next lifecycle state without permitting invalid rewrites."""

        if status not in RUN_STATUSES:
            allowed = ", ".join(sorted(RUN_STATUSES))
            raise ValueError(f"Unsupported run status {status!r}; expected one of: {allowed}")
        if status not in _RUN_TRANSITIONS[self.status]:
            raise ValueError(f"Run status cannot transition from {self.status!r} to {status!r}")
        self.status = status
        if result is not None:
            self.result = result

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Run":
        normalized = dict(data)
        normalized.pop("display_id", None)
        return cls(**normalized)
