"""Task domain model."""

from dataclasses import asdict, dataclass, field
from uuid import uuid4

TASK_TYPES = {"feature", "bug_fix", "refactor", "configuration", "documentation"}


@dataclass(slots=True)
class Task:
    """A user request with explicit acceptance criteria."""

    project_id: str
    description: str
    task_type: str = "feature"
    acceptance_criteria: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        if self.task_type not in TASK_TYPES:
            allowed = ", ".join(sorted(TASK_TYPES))
            raise ValueError(f"Unsupported task type {self.task_type!r}; expected one of: {allowed}")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Task":
        return cls(**data)
