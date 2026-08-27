"""Shared `/learn` command behavior for text Chat and the TUI."""

from __future__ import annotations

from techpilot.runtime.sessions import SessionEventSink

from .service import LearningGoalDraft, LearningService

_REVIEW_REQUESTS = (
    "i want to review what i learned before",
    "i want to review my previous learning",
    "help me review what i learned before",
    "continue my previous learning",
    "我想复习之前学过的内容",
    "帮我复习之前学过的内容",
    "复习之前学过的内容",
    "继续之前的学习",
    "继续学习",
)


class LearningCommandController:
    """Route explicit new, continue, and review requests to one learning state."""

    def __init__(
        self,
        service: LearningService | None = None,
        *,
        session_sink: SessionEventSink | None = None,
        session_id: str | None = None,
    ) -> None:
        self.service = service or LearningService()
        self.session_sink = session_sink
        self.session_id = session_id
    def start_from_message(self, message: str) -> str | None:
        if _normalized_message(message) in _REVIEW_REQUESTS:
            return self._review_message()
        draft = self.service.draft_from_message(message)
        if draft is None:
            return None
        return self._start(draft)

    def handle(self, argument: str) -> str:
        value = argument.strip()
        if not value:
            return self._landing_message()
        if value.casefold() == "status":
            return self._status_message()
        if value.casefold() in {"cancel", "stop"}:
            try:
                goal = self.service.cancel_active_goal(session_sink=self.session_sink, session_id=self.session_id)
            except ValueError as error:
                return f"学习：{error}。"
            return f"已结束学习目标：{goal.topic}。已保存的计划仍可通过 /learn review 查看。"
        if value.casefold() == "continue":
            return self._continue_message()
        if value.casefold() == "review":
            return self._review_message()
        if value.casefold().startswith("new "):
            value = value[4:].strip()
        try:
            draft = self.service.draft_from_command(value)
        except ValueError as error:
            return f"学习：{error}。"
        return self._start(draft)

    def _start(self, draft: LearningGoalDraft) -> str:
        try:
            result = self.service.confirm(
                draft,
                baseline_notes=None,
                intended_outcome=None,
                weekly_minutes=None,
                session_sink=self.session_sink,
                session_id=self.session_id,
            )
        except ValueError as error:
            active = self.service.active_goal()
            if active is not None:
                return "\n".join([
                    f"你当前有一条正在进行的学习路径：{active.topic}。",
                    "输入 /learn continue 继续，/learn review 查看，或先用 /learn stop 结束它再开始新主题。",
                ])
            return f"学习：{error}。"
        steps = "\n".join(f"  {index}. {step.title}" for index, step in enumerate(result.plan.steps, start=1))
        return "\n".join([
            f"已创建学习路径：{result.goal.topic}",
            "我先按每周约两小时、建立实用基础来安排；之后只有会影响计划时，才需要补充你的目标和时间。",
            "起步计划：",
            steps,
            "准备继续时输入 /learn continue；想重新查看计划可输入 /learn review。",
        ])

    def _status_message(self) -> str:
        try:
            goal = self.service.active_goal()
        except ValueError as error:
            return f"学习：{error}。"
        if goal is None:
            return "目前没有正在进行的学习目标。可用 /learn <主题> 开始，或用 /learn review 查看上一次保存的路径。"
        return "\n".join([
            f"当前学习目标：{goal.topic}",
            f"状态：{_goal_status_text(goal.status)}",
            f"预期成果：{goal.intended_outcome or '未设置'}",
            "输入 /learn continue 继续，/learn review 查看，或先用 /learn stop 结束它再开始新主题。",
        ])

    def _continue_message(self) -> str:
        goal = self.service.continue_goal()
        if goal is None:
            return "没有可继续的学习路径。已结束的路径可用 /learn review 查看；想学习新主题时直接告诉我即可。"
        return self._plan_message(goal, "继续学习")

    def _review_message(self) -> str:
        goal = self.service.review_goal()
        if goal is None:
            return "还没有可复习的学习路径。可用 /learn <主题> 开始一条新的路径。"
        return self._plan_message(goal, "查看学习路径")

    def _plan_message(self, goal, heading: str) -> str:
        plan = self.service.plan_for(goal)
        if plan is None:
            return f"学习路径“{goal.topic}”还没有已保存的计划。"
        steps = "\n".join(
            f"  {index}. {step.title}\n     验收：{step.acceptance_criteria[0]}"
            for index, step in enumerate(plan.steps, start=1)
        )
        return "\n".join([
            f"{heading}：{goal.topic}",
            f"目标：{goal.intended_outcome}",
            steps,
            "资料研究、来源整理、互动教学和测验将在后续阶段接入。",
        ])

    def _landing_message(self) -> str:
        goal = self.service.active_goal()
        if goal is None:
            return "目前没有正在进行的学习路径。想开始时输入 /learn <主题>；/learn review 可查看上一次保存的路径。"
        return "\n".join([
            f"当前学习路径：{goal.topic}",
            "输入 /learn continue 继续，/learn review 查看，/learn stop 结束。",
        ])


def _normalized_message(value: str) -> str:
    return value.strip().casefold().rstrip("。！？.!?").strip()


def _goal_status_text(status: str) -> str:
    return {
        "draft": "草稿",
        "active": "进行中",
        "paused": "已暂停",
        "completed": "已完成",
        "cancelled": "已结束",
    }.get(status, status)
