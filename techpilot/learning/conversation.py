"""Natural-language learning routing over the shared Runtime."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from techpilot.runtime import TaskRuntime

from .role_runtime import LearningRoleRuntime
from .service import LearningGoalDraft, LearningService

LearningIntent = Literal["chat", "introduce", "start", "continue", "review"]
_INTENTS: frozenset[str] = frozenset({"chat", "introduce", "start", "continue", "review"})
_MIN_ROUTED_CONFIDENCE = 0.7


@dataclass(frozen=True)
class LearningRoute:
    intent: LearningIntent
    topic: str | None = None
    confidence: float = 0.0


@dataclass(frozen=True)
class LearningChoice:
    title: str
    options: tuple[str, ...]


@dataclass(frozen=True)
class LearningTurn:
    """Host-validated consequence of one user message."""

    user_input: str | None = None
    choice: LearningChoice | None = None
    notice: str | None = None
    stage: str | None = None
    allow_tools: bool = True
    clear_role_after_turn: bool = False

    @property
    def should_run_model(self) -> bool:
        return self.user_input is not None and self.choice is None


class LearningIntentRouter:
    """Ask the configured model for a constrained, non-authoritative intent."""

    def __init__(self, service: LearningService) -> None:
        self.service = service

    def route(self, runtime: TaskRuntime, message: str) -> LearningRoute:
        role = self.service.roles.get("developer-learning-coach")
        skills = self.service.skills.allowed_for_role(role.id)
        active = self.service.active_goal()
        manifest_lines = "\n".join(f"- {skill.manifest.name}: {skill.manifest.description}" for skill in skills)
        active_text = "无" if active is None else f"{active.topic} ({active.status})"
        messages = [
            {
                "role": "system",
                "content": "\n".join([
                    "你是 TechPilot 的受限学习意图路由器。只输出一个 JSON 对象，不要 Markdown、解释或思考过程。",
                    "字段：intent（chat、introduce、start、continue、review 之一）、topic（字符串或 null）、confidence（0 到 1）。",
                    "introduce：用户希望先解释、概览或简单了解主题，不创建学习路径。",
                    "start：用户明确希望系统开始一条学习路径。continue/review：用户希望继续或复习既有学习。",
                    "chat：代码任务、普通技术问答、没有学习意图或不确定时使用。宁可 chat，不要猜测 start。",
                    f"当前活跃学习路径：{active_text}",
                    "当前 Role 允许考虑的 Skill：",
                    manifest_lines or "- （无）",
                ]),
            },
            {"role": "user", "content": message},
        ]
        try:
            response = runtime.agent.llm.chat(messages)
            route = _parse_route(response.content)
        except Exception:
            route = LearningRoute("chat")
        if route.intent != "chat" and route.confidence < _MIN_ROUTED_CONFIDENCE:
            route = LearningRoute("chat")
        if runtime.session_sink is not None:
            runtime.session_sink.record("learning_intent_routed", runtime.agent.session_id, {
                "intent": route.intent,
                "topic": route.topic,
                "confidence": route.confidence,
            })
        return route


class LearningConversationController:
    """Apply model intent only after Host state and Role checks."""

    def __init__(
        self,
        service: LearningService | None = None,
        *,
        role_runtime: LearningRoleRuntime | None = None,
        router: LearningIntentRouter | None = None,
    ) -> None:
        self.service = service or LearningService()
        self.role_runtime = role_runtime or LearningRoleRuntime(self.service.roles, self.service.skills)
        self.router = router or LearningIntentRouter(self.service)
        self._pending: tuple[str, LearningGoalDraft, str] | None = None

    @property
    def pending_choice(self) -> LearningChoice | None:
        if self._pending is None:
            return None
        active_topic, draft, _ = self._pending
        return LearningChoice(
            title=f"你正在学习“{active_topic}”，同时提到了“{draft.topic}”。",
            options=(
                f"先简单了解 {draft.topic}，不改变当前学习路径",
                f"暂停 {active_topic}，开始学习 {draft.topic}",
                f"保留 {active_topic}，取消这次切换",
            ),
        )

    @staticmethod
    def should_route(message: str) -> bool:
        return _might_be_learning_intent(message)

    def prepare(self, runtime: TaskRuntime, message: str) -> LearningTurn:
        if self._pending is not None:
            return LearningTurn(notice="请先使用 1、2、3 或上下方向键完成当前选择。")
        active = self.service.active_goal()
        if not _might_be_learning_intent(message):
            runtime.clear_role()
            return LearningTurn(user_input=message)
        route = self.router.route(runtime, message)
        if route.intent == "chat":
            runtime.clear_role()
            return LearningTurn(user_input=message)
        if route.intent == "introduce":
            self.role_runtime.activate_quick_introduction(runtime)
            return LearningTurn(user_input=message, allow_tools=False, clear_role_after_turn=True)
        if route.intent in {"continue", "review"}:
            goal = self.service.continue_goal() if route.intent == "continue" else self.service.review_goal()
            activation = self.role_runtime.activate(runtime, goal, self.service.plan_for(goal) if goal is not None else None)
            stage = "正在恢复学习路径…" if route.intent == "continue" else "正在准备复习内容…"
            return LearningTurn(user_input=message, notice=activation, stage=stage)
        if route.intent == "start":
            draft = self._draft(route.topic)
            if draft is None:
                activation = self.role_runtime.activate(runtime, active, self.service.plan_for(active) if active is not None else None)
                return LearningTurn(user_input=message, notice=activation, stage="正在进入学习模式…")
            if active is not None and _same_topic(active.topic, draft.topic):
                activation = self.role_runtime.activate(runtime, active, self.service.plan_for(active))
                return LearningTurn(user_input=message, notice=activation, stage="正在继续当前学习路径…")
            if active is not None:
                self._pending = (active.topic, draft, message)
                return LearningTurn(choice=self.pending_choice)
            self._confirm(draft, runtime)
            goal = self.service.active_goal()
            plan = self.service.plan_for(goal) if goal is not None else None
            activation = self.role_runtime.activate(runtime, goal, plan)
            return LearningTurn(
                user_input=message,
                notice=f"{activation}\n\n{_start_notice(goal, plan)}",
                stage="正在准备第 1 步…",
            )
        runtime.clear_role()
        return LearningTurn(user_input=message)

    def choose(self, runtime: TaskRuntime, index: int) -> LearningTurn:
        if self._pending is None:
            return LearningTurn(notice="当前没有需要选择的学习操作。")
        active_topic, draft, original_message = self._pending
        self._pending = None
        if index == 0:
            # "先简单了解" is a one-turn detour. It uses neither the learning
            # Skill nor Runtime tools, and leaves the persisted path intact.
            self.role_runtime.activate_quick_introduction(runtime)
            return LearningTurn(user_input=original_message, allow_tools=False, clear_role_after_turn=True)
        if index == 1:
            self.service.pause_active_goal(
                session_sink=runtime.session_sink,
                session_id=runtime.agent.session_id,
            )
            self._confirm(draft, runtime)
            goal = self.service.active_goal()
            activation = self.role_runtime.activate(runtime, goal, self.service.plan_for(goal) if goal is not None else None)
            return LearningTurn(user_input=original_message, notice=activation, stage="正在准备第 1 步…")
        if index == 2:
            return LearningTurn(notice=f"已保留当前学习路径“{active_topic}”。")
        return LearningTurn(notice="无效选择。")

    def _confirm(self, draft: LearningGoalDraft, runtime: TaskRuntime) -> None:
        self.service.confirm(
            draft,
            baseline_notes=None,
            intended_outcome=None,
            weekly_minutes=None,
            session_sink=runtime.session_sink,
            session_id=runtime.agent.session_id,
        )

    @staticmethod
    def _draft(topic: str | None) -> LearningGoalDraft | None:
        if topic is None or not topic.strip():
            return None
        try:
            return LearningService.draft_from_command(topic)
        except ValueError:
            return None


def _parse_route(content: str) -> LearningRoute:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.IGNORECASE)
    data = json.loads(stripped)
    intent = str(data.get("intent", "chat")).casefold()
    if intent not in _INTENTS:
        return LearningRoute("chat")
    topic_value = data.get("topic")
    topic = topic_value.strip() if isinstance(topic_value, str) and topic_value.strip() else None
    confidence_value = data.get("confidence", 0.0)
    try:
        confidence = float(confidence_value)
    except (TypeError, ValueError):
        confidence = 0.0
    if not 0.0 <= confidence <= 1.0:
        confidence = 0.0
    return LearningRoute(intent, topic, confidence)  # type: ignore[arg-type]


def _same_topic(left: str, right: str) -> bool:
    return left.casefold().strip() == right.casefold().strip()


def _might_be_learning_intent(message: str) -> bool:
    """Avoid charging a router call to ordinary repository coding messages."""

    lowered = message.casefold()
    signals = (
        "learn", "study", "review", "continue learning", "tutorial", "practice",
        "学习", "我想学", "想学", "帮我学", "带我学", "学一下", "复习", "继续学习", "继续之前的学习", "入门", "掌握", "教程", "练习",
    )
    return any(signal in lowered for signal in signals)


def _start_notice(goal, plan) -> str:
    if goal is None or plan is None or not plan.steps:
        return "已创建学习路径。接下来我会直接带你开始第一步。"
    return "\n".join([
        f"已创建学习路径：{goal.topic}",
        "学习阶段：路径已创建，正在准备第一步。",
        f"第 1 步：{plan.steps[0].title}",
        "接下来我会直接开始讲解，不需要你再输入命令。",
    ])
