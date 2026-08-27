"""Runtime activation for the code-owned developer learning Role."""

from __future__ import annotations

from techpilot.runtime import TaskRuntime

from .contracts import LearningGoal
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

    def activate(self, runtime: TaskRuntime, goal: LearningGoal | None = None) -> None:
        role = self.roles.get("developer-learning-coach")
        packages = self.skills.allowed_for_role(role.id)
        skill_context = "\n\n".join(package.skill_markdown for package in packages)
        goal_context = "当前没有已保存的学习路径。" if goal is None else "\n".join([
            f"当前学习路径：{goal.topic}",
            f"当前状态：{goal.status}",
            f"学习目标：{goal.intended_outcome or '未设置'}",
        ])
        runtime.activate_role(role.id, "\n\n".join([
            "# Active Role: Developer Learning Coach",
            role.system_prompt,
            "当前是学习对话，不要修改仓库、运行命令或调用编码工具，除非用户在本轮明确要求代码实践。",
            "先直接回答用户当前的学习问题；不要要求用户记忆 Slash 命令。只有用户明确希望建立、切换或暂停学习路径时，才讨论状态选择。",
            goal_context,
            "# Allowed Skill",
            skill_context,
        ]))
