"""Structured implementation plan domain model."""

from dataclasses import asdict, dataclass, field
from uuid import uuid4


@dataclass(slots=True)
class Plan:
    """A reviewable plan produced before code changes are made."""

    task_id: str
    summary: str
    steps: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    read_files: list[str] = field(default_factory=list)
    modify_files: list[str] = field(default_factory=list)
    expected_files: list[str] = field(default_factory=list)
    validation_commands: list[list[str]] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid4().hex)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Plan":
        return cls(**data)
