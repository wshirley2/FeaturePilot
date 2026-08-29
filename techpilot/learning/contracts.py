"""Stable, serializable contracts for the developer-learning Role.

These objects deliberately contain user learning facts and reusable Skill
metadata only. They do not carry provider credentials, tool permissions, or
concurrency declarations; those remain Runtime-owned facts.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from techpilot.runtime.extensions import RoleSpec, SkillSpec

_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_RECORD_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")


def _new_id() -> str:
    return uuid4().hex


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LearningModel(BaseModel):
    """Shared persistence behavior for learning-domain data."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("unsupported learning schema version")
        return value


class RoleDefinition(RoleSpec):
    """Compatibility alias for the historical developer-learning sample."""


class SkillManifest(SkillSpec):
    """Compatibility alias for a Runtime-owned portable Skill contract."""


class LearningProfile(LearningModel):
    id: str = Field(default_factory=_new_id)
    baseline_notes: str | None = None
    weekly_minutes: int | None = Field(default=None, ge=1)
    preferences: tuple[str, ...] = ()
    updated_at: str = Field(default_factory=_utc_now)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        _validate_record_id(value)
        return value


class LearningGoal(LearningModel):
    id: str = Field(default_factory=_new_id)
    topic: str
    status: Literal["draft", "active", "paused", "completed", "cancelled"] = "draft"
    intended_outcome: str | None = None
    created_at: str = Field(default_factory=_utc_now)
    updated_at: str = Field(default_factory=_utc_now)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        _validate_record_id(value)
        return value

    @field_validator("topic")
    @classmethod
    def _validate_topic(cls, value: str) -> str:
        return _non_empty(value, "learning goal topic")


class LearningStep(LearningModel):
    id: str = Field(default_factory=_new_id)
    title: str
    status: Literal["pending", "active", "completed", "skipped"] = "pending"
    acceptance_criteria: tuple[str, ...] = ()

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        _validate_record_id(value)
        return value

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        return _non_empty(value, "learning step title")


class LearningPlan(LearningModel):
    id: str = Field(default_factory=_new_id)
    goal_id: str
    title: str
    steps: tuple[LearningStep, ...] = ()

    @field_validator("id", "goal_id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        _validate_record_id(value)
        return value

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        return _non_empty(value, "learning plan title")


class LearningTask(LearningModel):
    id: str = Field(default_factory=_new_id)
    plan_id: str
    step_id: str
    title: str
    status: Literal["pending", "active", "completed", "skipped"] = "pending"
    estimated_minutes: int | None = Field(default=None, ge=1)
    source_ids: tuple[str, ...] = ()
    practice: str | None = None
    acceptance_criteria: tuple[str, ...] = ()

    @field_validator("id", "plan_id", "step_id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        _validate_record_id(value)
        return value

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        return _non_empty(value, "learning task title")

    @field_validator("source_ids")
    @classmethod
    def _validate_source_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("learning task source ids must not contain duplicates")
        for value in values:
            _validate_record_id(value)
        return values


class SourceRecord(LearningModel):
    id: str = Field(default_factory=_new_id)
    uri: str
    title: str
    goal_id: str | None = None
    source_type: Literal["web", "github", "document"] = "web"
    content_type: str | None = None
    content_hash: str | None = None
    summary: str | None = None
    version: str | None = None
    published_at: str | None = None
    retrieved_at: str = Field(default_factory=_utc_now)
    uncertainty: str | None = None

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        _validate_record_id(value)
        return value

    @field_validator("goal_id")
    @classmethod
    def _validate_goal_id(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_record_id(value)
        return value

    @field_validator("uri", "title")
    @classmethod
    def _validate_non_empty_text(cls, value: str) -> str:
        return _non_empty(value, "source uri and title")


class TrendBrief(LearningModel):
    id: str = Field(default_factory=_new_id)
    goal_id: str
    category: Literal["must-learn", "recommended", "watchlist"]
    title: str
    source_ids: tuple[str, ...] = ()
    rationale: str | None = None
    valid_as_of: str | None = None
    skip_if: str | None = None
    reviewed_at: str = Field(default_factory=_utc_now)

    @field_validator("id", "goal_id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        _validate_record_id(value)
        return value

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        return _non_empty(value, "trend brief title")

    @field_validator("source_ids")
    @classmethod
    def _validate_source_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("trend brief source ids must not contain duplicates")
        for value in values:
            _validate_record_id(value)
        return values


class DocumentRecord(LearningModel):
    id: str = Field(default_factory=_new_id)
    filename: str
    mime_type: str
    content_hash: str
    goal_id: str | None = None
    extraction_status: Literal["pending", "extracted", "unsupported", "failed"] = "pending"

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        _validate_record_id(value)
        return value

    @field_validator("goal_id")
    @classmethod
    def _validate_goal_id(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_record_id(value)
        return value

    @field_validator("filename", "mime_type", "content_hash")
    @classmethod
    def _validate_non_empty_text(cls, value: str) -> str:
        return _non_empty(value, "document metadata")


class ExtractedText(LearningModel):
    id: str = Field(default_factory=_new_id)
    document_id: str
    text: str
    locator: str | None = None

    @field_validator("id", "document_id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        _validate_record_id(value)
        return value

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _non_empty(value, "extracted text")


class QuizAttempt(LearningModel):
    id: str = Field(default_factory=_new_id)
    goal_id: str
    prompt: str
    answer: str
    feedback: str | None = None
    mastery: Literal["unknown", "needs-review", "developing", "demonstrated"] = "unknown"

    @field_validator("id", "goal_id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        _validate_record_id(value)
        return value

    @field_validator("prompt", "answer")
    @classmethod
    def _validate_non_empty_text(cls, value: str) -> str:
        return _non_empty(value, "quiz prompt and answer")


class KnowledgeDraft(LearningModel):
    id: str = Field(default_factory=_new_id)
    goal_id: str
    title: str
    markdown: str

    @field_validator("id", "goal_id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        _validate_record_id(value)
        return value

    @field_validator("title", "markdown")
    @classmethod
    def _validate_non_empty_text(cls, value: str) -> str:
        return _non_empty(value, "knowledge draft content")


class KnowledgeSync(LearningModel):
    id: str = Field(default_factory=_new_id)
    draft_id: str
    destination_kind: str
    idempotency_key: str
    status: Literal["pending", "synced", "failed", "paused"] = "pending"
    remote_reference: str | None = None

    @field_validator("id", "draft_id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        _validate_record_id(value)
        return value

    @field_validator("destination_kind", "idempotency_key")
    @classmethod
    def _validate_non_empty_text(cls, value: str) -> str:
        return _non_empty(value, "knowledge sync fields")


class SkillCandidate(LearningModel):
    id: str = Field(default_factory=_new_id)
    skill_name: str
    skill_markdown: str
    evidence_ids: tuple[str, ...] = ()
    suggested_action: Literal["discard", "create", "improve", "merge"]

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        _validate_record_id(value)
        return value

    @field_validator("skill_name")
    @classmethod
    def _validate_skill_name(cls, value: str) -> str:
        _validate_identifier(value, "skill name")
        return value

    @field_validator("skill_markdown")
    @classmethod
    def _validate_skill_markdown(cls, value: str) -> str:
        return _non_empty(value, "candidate SKILL.md")


class SkillRevision(LearningModel):
    skill_name: str
    version: int = Field(ge=1)
    skill_markdown: str
    source_candidate_id: str | None = None
    approved_at: str = Field(default_factory=_utc_now)
    review_note: str | None = None

    @field_validator("skill_name")
    @classmethod
    def _validate_skill_name(cls, value: str) -> str:
        _validate_identifier(value, "skill name")
        return value

    @field_validator("skill_markdown")
    @classmethod
    def _validate_skill_markdown(cls, value: str) -> str:
        return _non_empty(value, "revision SKILL.md")

    @field_validator("source_candidate_id")
    @classmethod
    def _validate_candidate_id(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_record_id(value)
        return value


def _validate_record_id(value: str) -> None:
    if not _RECORD_ID_PATTERN.fullmatch(value):
        raise ValueError("learning record ids must be UUID hex values")


def _validate_identifier(value: str, label: str) -> None:
    if not _IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(f"{label} must be lowercase kebab-case")


def _non_empty(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} must not be empty")
    return normalized
