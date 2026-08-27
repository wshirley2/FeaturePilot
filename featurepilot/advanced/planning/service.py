"""Application service for creating validated, persisted Plan drafts."""

from __future__ import annotations

from pathlib import Path

from ...domain import PlanRecord, Task
from ...repository import ContextSelector, RepositoryIndex, RepositoryProfiler
from .generator import PlanGenerator
from .store import PlanStore
from .validator import PlanValidationResult, PlanValidator


class PlanValidationError(ValueError):
    """A generated Plan failed deterministic validation."""

    def __init__(self, result: PlanValidationResult) -> None:
        super().__init__("Plan validation failed: " + "; ".join(result.errors))
        self.result = result


class PlanningService:
    """Turn one natural-language Task into a validated saved PlanRecord."""

    def __init__(
        self,
        store: PlanStore,
        *,
        profiler: RepositoryProfiler | None = None,
        generator: PlanGenerator | None = None,
        validator: PlanValidator | None = None,
    ) -> None:
        self.store = store
        self.profiler = profiler or RepositoryProfiler()
        self.generator = generator or PlanGenerator()
        self.validator = validator or PlanValidator()

    def create_draft(
        self,
        repository: Path,
        task: Task,
        *,
        name: str | None = None,
        limit: int = 10,
    ) -> PlanRecord:
        repository_path = repository.resolve()
        if not repository_path.is_dir():
            raise ValueError(f"Repository directory does not exist: {repository}")
        if limit < 1:
            raise ValueError("Candidate file limit must be at least 1")

        profile = self.profiler.profile(repository_path)
        index = RepositoryIndex.build(repository_path)
        candidates = ContextSelector(index).select(task.description, limit=limit)
        plan = self.generator.generate(task, profile, candidates)
        validation = self.validator.validate(plan, profile)
        if not validation.is_valid:
            raise PlanValidationError(validation)
        return self.store.save_draft(plan, repository_path, task=task, name=name)
