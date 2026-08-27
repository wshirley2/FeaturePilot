"""Deterministic first-entry workflow for the developer learning Role.

S1-B deliberately creates only a confirmed learning goal and a small,
observable starter plan.  Research, provider-backed teaching, and knowledge
sync belong to later slices, so this module never makes freshness claims or
contacts a network service.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from techpilot.runtime.sessions import SessionEventSink

from .contracts import LearningGoal, LearningPlan, LearningProfile, LearningStep
from .registry import RoleRegistry, SkillPackage, SkillRegistry
from .store import LearningStore

_NATURAL_LANGUAGE_PATTERNS = (
    re.compile(r"^i\s+(?:would\s+)?(?:like|want)\s+to\s+learn\s+(.+?)\s*[.!?]*$", re.IGNORECASE),
    re.compile(r"^help\s+me\s+learn\s+(.+?)\s*[.!?]*$", re.IGNORECASE),
    re.compile(r"^(?:我想学习|我想学|想学习|想学|帮我学习|帮我学|带我学习|带我学)\s*(.+?)\s*[。！？.!?]*$"),
)


@dataclass(frozen=True)
class LearningGoalDraft:
    """An unpersisted user choice which needs an explicit confirmation."""

    topic: str
    source: str
    role_id: str = "developer-learning-coach"
    skill_name: str = "developer-learning"


@dataclass(frozen=True)
class ConfirmedLearningGoal:
    """The durable result of confirming one learning-goal draft."""

    profile: LearningProfile
    goal: LearningGoal
    plan: LearningPlan
    skill: SkillPackage


class LearningService:
    """Own S1-B learning state without creating a second Agent Runtime."""

    def __init__(
        self,
        store: LearningStore | None = None,
        roles: RoleRegistry | None = None,
        skills: SkillRegistry | None = None,
    ) -> None:
        self.store = store or LearningStore()
        self.roles = roles or RoleRegistry.with_builtin_roles()
        self.skills = skills or SkillRegistry.with_builtin_skills(self.roles)

    @staticmethod
    def draft_from_command(topic: str) -> LearningGoalDraft:
        normalized = _topic(topic)
        return LearningGoalDraft(topic=normalized, source="command")

    @staticmethod
    def draft_from_message(message: str) -> LearningGoalDraft | None:
        normalized = message.strip()
        for pattern in _NATURAL_LANGUAGE_PATTERNS:
            match = pattern.fullmatch(normalized)
            if match:
                return LearningGoalDraft(topic=_topic(match.group(1)), source="natural-language")
        return None

    def active_goal(self) -> LearningGoal | None:
        active = [goal for goal in self.store.list_goals() if goal.status == "active"]
        if len(active) > 1:
            raise ValueError("multiple active learning goals found; resolve stored data before continuing")
        return active[0] if active else None

    def review_goal(self) -> LearningGoal | None:
        """Choose the active goal or the most recently updated prior learning record."""

        active = self.active_goal()
        if active is not None:
            return active
        goals = [goal for goal in self.store.list_goals() if goal.status != "draft"]
        return max(goals, key=lambda goal: goal.updated_at) if goals else None

    def continue_goal(self) -> LearningGoal | None:
        """Return the active goal or resume the most recently paused one.

        Cancelled and completed paths are intentionally review-only.  They must
        never become active again merely because the user asks to continue.
        """

        active = self.active_goal()
        if active is not None:
            return active
        paused = [goal for goal in self.store.list_goals() if goal.status == "paused"]
        if not paused:
            return None
        selected = max(paused, key=lambda goal: goal.updated_at)
        resumed = selected.model_copy(update={"status": "active"})
        self.store.save_goal(resumed)
        return resumed

    def plan_for(self, goal: LearningGoal) -> LearningPlan | None:
        plans = self.store.list_plans(goal.id)
        return plans[-1] if plans else None

    def confirm(
        self,
        draft: LearningGoalDraft,
        *,
        baseline_notes: str | None,
        intended_outcome: str | None = None,
        weekly_minutes: int | None = None,
        session_sink: SessionEventSink | None = None,
        session_id: str | None = None,
    ) -> ConfirmedLearningGoal:
        if self.active_goal() is not None:
            raise ValueError("an active learning goal already exists; complete or cancel it before starting another")
        resolved_weekly_minutes = weekly_minutes if weekly_minutes is not None else 120
        if resolved_weekly_minutes < 1:
            raise ValueError("weekly learning minutes must be at least 1")
        outcome = (intended_outcome or "").strip() or f"为 {draft.topic} 建立可实践的基础。"

        role = self.roles.get(draft.role_id)
        packages = self.skills.match(role.id, draft.topic)
        if not packages or packages[0].manifest.name != draft.skill_name:
            raise ValueError("the developer-learning Skill is unavailable for this Role")

        profile = LearningProfile(
            baseline_notes=_optional_text(baseline_notes),
            weekly_minutes=resolved_weekly_minutes,
            preferences=("english-first",),
        )
        goal = LearningGoal(topic=draft.topic, status="active", intended_outcome=outcome)
        plan = LearningPlan(
            goal_id=goal.id,
            title=f"{goal.topic}：起步学习计划",
            steps=(
                LearningStep(
                    title=f"梳理 {goal.topic} 的学习范围",
                    acceptance_criteria=("能用自己的话说明主题边界和预期成果。",),
                ),
                LearningStep(
                    title=f"完成一个小型 {goal.topic} 练习",
                    acceptance_criteria=("产出可运行示例或完整的技术说明。",),
                ),
                LearningStep(
                    title=f"复习并自测 {goal.topic}",
                    acceptance_criteria=("完成一组小测，或说明一个取舍和一个常见误区。",),
                ),
            ),
        )
        self.store.save_record(profile)
        self.store.save_goal(goal)
        self.store.save_record(plan)
        if session_sink is not None and session_id is not None:
            session_sink.record(
                "learning_goal_confirmed",
                session_id,
                {
                    "goal_id": goal.id,
                    "plan_id": plan.id,
                    "role_id": role.id,
                    "skill_name": packages[0].manifest.name,
                },
            )
        return ConfirmedLearningGoal(profile=profile, goal=goal, plan=plan, skill=packages[0])

    def cancel_active_goal(self, *, session_sink: SessionEventSink | None = None, session_id: str | None = None) -> LearningGoal:
        goal = self.active_goal()
        if goal is None:
            raise ValueError("no active learning goal")
        cancelled = goal.model_copy(update={"status": "cancelled"})
        self.store.save_goal(cancelled)
        if session_sink is not None and session_id is not None:
            session_sink.record("learning_goal_cancelled", session_id, {"goal_id": goal.id})
        return cancelled

    def pause_active_goal(self, *, session_sink: SessionEventSink | None = None, session_id: str | None = None) -> LearningGoal:
        goal = self.active_goal()
        if goal is None:
            raise ValueError("no active learning goal")
        paused = goal.model_copy(update={"status": "paused"})
        self.store.save_goal(paused)
        if session_sink is not None and session_id is not None:
            session_sink.record("learning_goal_paused", session_id, {"goal_id": goal.id})
        return paused


def _topic(value: str) -> str:
    normalized = value.strip().rstrip("。！？.!?").strip()
    if not normalized:
        raise ValueError("a learning topic is required")
    if len(normalized) > 200:
        raise ValueError("learning topic must be at most 200 characters")
    return normalized


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None
