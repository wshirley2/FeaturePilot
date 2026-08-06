"""Project domain model."""

from dataclasses import asdict, dataclass, field
from uuid import uuid4


@dataclass(slots=True)
class Project:
    """A local code repository that FeaturePilot can work on."""

    name: str
    path: str
    id: str = field(default_factory=lambda: uuid4().hex)
    description: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "Project":
        return cls(**data)
