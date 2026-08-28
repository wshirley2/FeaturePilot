"""Runtime activation for the code-owned developer learning Role."""

from __future__ import annotations

from techpilot.runtime import TaskRuntime

from .contracts import LearningGoal, LearningPlan
from .registry import RoleRegistry, SkillRegistry


class LearningRoleRuntime:
    """Project learning facts into the shared Runtime's system context."""

    def __init__(
        self,
        roles: RoleRegistry | None = None,
        skills: SkillRegistry | None = None,
    ) -> None:
        self.roles = roles or RoleRegistry.with_builtin_roles()
        self.skills = skills or SkillRegistry.with_builtin_skills(self.roles)

    def activate(
        self,
        runtime: TaskRuntime,
        goal: LearningGoal | None = None,
        plan: LearningPlan | None = None,
    ) -> str:
        role = self.roles.get("developer-learning-coach")
        packages = self.skills.allowed_for_role(role.id)
        skill_context = "\n\n".join(package.skill_markdown for package in packages)
        if goal is None:
            goal_context = "当前没有已保存的学习路径。"
        else:
            goal_lines = [
                f"当前学习路径：{goal.topic}",
                f"当前状态：{goal.status}",
                f"学习目标：{goal.intended_outcome or '未设置'}",
            ]
            if plan is not None and plan.goal_id == goal.id and plan.steps:
                goal_lines.append(f"当前第 1 步：{plan.steps[0].title}")
            goal_context = "\n".join(goal_lines)
        runtime.activate_role(
            role.id,
            f"# Active Role: Developer Learning Coach\n\n"
            f"{role.system_prompt}\n\n"
            "当前是学习对话，不要修改仓库、运行命令或调用编码工具，除非用户在本轮明确要求代码实践。\n\n"
            "先直接回答用户当前的学习问题；不要要求用户记忆 Slash 命令。只有用户明确希望建立、切换或暂停学习路径时，才讨论状态选择。\n\n"
            f"{goal_context}\n\n"
            f"# Allowed Skill\n\n{skill_context}",
        )
        return "✻ Skill 已准备好，开始第 1 步…"
