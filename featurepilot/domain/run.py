"""Execution run domain model."""

from dataclasses import asdict, dataclass, field
from uuid import uuid4

RUN_STATUSES = {"created", "running", "succeeded", "failed", "cancelled"}


@dataclass(slots=True)
class Run:
    """One execution attempt for an approved task plan."""

    task_id: str
    status: str = "created"
    result: dict[str, object] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        if self.status not in RUN_STATUSES:
            allowed = ", ".join(sorted(RUN_STATUSES))
            raise ValueError(f"Unsupported run status {self.status!r}; expected one of: {allowed}")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Run":
        return cls(**data)
