"""User-owned persistence for learning data and Skill lifecycle records."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from ..config.user import user_config_directory
from .contracts import (
    DocumentRecord,
    ExtractedText,
    KnowledgeDraft,
    KnowledgeSync,
    LearningGoal,
    LearningModel,
    LearningPlan,
    LearningProfile,
    LearningTask,
    QuizAttempt,
    SkillCandidate,
    SkillRevision,
    SourceRecord,
    TrendBrief,
)
from .registry import parse_skill_manifest

_RECORD_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
_SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ModelT = TypeVar("ModelT", bound=BaseModel)
_RECORD_CATEGORIES: dict[type[LearningModel], str] = {
    LearningProfile: "profiles",
    LearningGoal: "goals",
    LearningPlan: "plans",
    LearningTask: "tasks",
    SourceRecord: "sources",
    TrendBrief: "trends",
    DocumentRecord: "documents",
    ExtractedText: "extracts",
    QuizAttempt: "assessments",
    KnowledgeDraft: "notes",
    KnowledgeSync: "sync",
}


def learning_data_directory() -> Path:
    """Return the user-level learning root, never a target repository path."""

    return user_config_directory() / "learning"


class LearningStore:
    """Persist small learning records with separate candidate, revision, and active paths."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = (directory or learning_data_directory()).expanduser()

    def save_goal(self, goal: LearningGoal) -> Path:
        return self.save_record(goal)

    def load_goal(self, goal_id: str) -> LearningGoal:
        return self.load_record(LearningGoal, goal_id)

    def list_goals(self) -> list[LearningGoal]:
        """Load valid goal records in a deterministic order without guessing on corruption."""

        return self.list_records(LearningGoal)

    def list_plans(self, goal_id: str) -> list[LearningPlan]:
        """Return plans for one known learning goal in durable creation order."""

        if not _RECORD_ID_PATTERN.fullmatch(goal_id):
            raise ValueError("learning record ids must be UUID hex values")
        return [plan for plan in self.list_records(LearningPlan) if plan.goal_id == goal_id]

    def list_records(self, model_type: type[ModelT]) -> list[ModelT]:
        """Load one record category rather than silently skipping damaged facts."""

        category = _RECORD_CATEGORIES.get(model_type)  # type: ignore[arg-type]
        if category is None:
            raise TypeError(f"unsupported learning record type: {model_type.__name__}")
        directory = self._safe_path(category)
        if not directory.exists():
            return []
        return [
            self._read_model(path, model_type, f"{category} record {path.stem}")
            for path in sorted(directory.glob("*.json"))
        ]

    def save_record(self, record: LearningModel) -> Path:
        """Persist a typed learning fact under its fixed user-level category."""

        try:
            category = _RECORD_CATEGORIES[type(record)]
        except KeyError as error:
            raise TypeError(f"unsupported learning record type: {type(record).__name__}") from error
        record_id = getattr(record, "id", None)
        if not isinstance(record_id, str):
            raise TypeError("learning records must expose a string id")
        path = self._record_path(category, record_id)
        self._write_model(path, record)
        return path

    def load_record(self, model_type: type[ModelT], record_id: str) -> ModelT:
        category = _RECORD_CATEGORIES.get(model_type)  # type: ignore[arg-type]
        if category is None:
            raise TypeError(f"unsupported learning record type: {model_type.__name__}")
        return self._read_model(self._record_path(category, record_id), model_type, f"{category} record {record_id}")

    def save_candidate(self, candidate: SkillCandidate) -> Path:
        path = self._record_path("skills/candidates", candidate.id)
        self._write_model(path, candidate)
        return path

    def load_candidate(self, candidate_id: str) -> SkillCandidate:
        path = self._record_path("skills/candidates", candidate_id)
        return self._read_model(path, SkillCandidate, f"skill candidate {candidate_id}")

    def save_revision(self, revision: SkillRevision) -> Path:
        manifest = parse_skill_manifest(revision.skill_markdown, f"{revision.skill_name}@{revision.version}")
        if manifest.name != revision.skill_name:
            raise ValueError("skill revision manifest name must match the revision skill name")
        path = self.revision_path(revision.skill_name, revision.version)
        self._write_model(path, revision)
        return path

    def load_revision(self, skill_name: str, version: int) -> SkillRevision:
        path = self.revision_path(skill_name, version)
        return self._read_model(path, SkillRevision, f"skill revision {skill_name}@{version}")

    def activate_revision(self, revision: SkillRevision) -> Path:
        """Make only an already-persisted revision active; candidates have no write path here."""

        try:
            persisted = self.load_revision(revision.skill_name, revision.version)
        except ValueError as error:
            raise ValueError("skill revision must be persisted before it can become active") from error
        if persisted != revision:
            raise ValueError("only the persisted skill revision can become active")
        active_directory = self.active_skill_directory(persisted.skill_name)
        skill_path = active_directory / "SKILL.md"
        self._atomic_write_text(skill_path, persisted.skill_markdown)
        self._write_model(active_directory / "meta.json", persisted)
        return skill_path

    def active_skill_directory(self, skill_name: str) -> Path:
        return self._safe_path("skills", "active", self._validated_skill_name(skill_name))

    def revision_path(self, skill_name: str, version: int) -> Path:
        if version < 1:
            raise ValueError("skill revision version must be at least 1")
        return self._safe_path("skills", "revisions", self._validated_skill_name(skill_name), f"{version}.json")

    def _record_path(self, category: str, record_id: str) -> Path:
        if not _RECORD_ID_PATTERN.fullmatch(record_id):
            raise ValueError("learning record ids must be UUID hex values")
        return self._safe_path(*category.split("/"), f"{record_id}.json")

    def _safe_path(self, *parts: str) -> Path:
        root = self.directory.resolve()
        candidate = root.joinpath(*parts).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise ValueError("learning data path escapes the user data directory") from error
        return candidate

    @staticmethod
    def _validated_skill_name(value: str) -> str:
        if not _SKILL_NAME_PATTERN.fullmatch(value):
            raise ValueError("skill name must be lowercase kebab-case")
        return value

    def _write_model(self, path: Path, model: BaseModel) -> None:
        self._atomic_write_text(path, json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n")

    def _read_model(self, path: Path, model_type: type[ModelT], label: str) -> ModelT:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return model_type.model_validate(payload)
        except FileNotFoundError as error:
            raise ValueError(f"saved {label} not found") from error
        except (OSError, TypeError, ValueError, json.JSONDecodeError, ValidationError) as error:
            raise ValueError(f"saved {label} is invalid: {error}") from error

    def _atomic_write_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}-", suffix=".tmp", delete=False
            ) as handle:
                handle.write(content)
                temporary = Path(handle.name)
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        if os.name != "nt":
            path.chmod(0o600)
