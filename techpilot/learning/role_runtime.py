"""Runtime activation for the code-owned developer learning Role."""

from __future__ import annotations

from techpilot.runtime import RoleSkillActivator, TaskRuntime, ToolAllowlist

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
        self.activator = RoleSkillActivator(
            self.roles,
            self.skills,
            (ToolAllowlist(
                role_id="developer-learning-coach",
                tool_names=("research_url", "research_document"),
            ),),
        )

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
        context = (
            f"# Active Role: Developer Learning Coach\n\n"
            f"{role.system_prompt}\n\n"
            "当前是学习对话，不要修改仓库、运行命令或调用编码工具，除非用户在本轮明确要求代码实践。\n\n"
            "先直接回答用户当前的学习问题；不要要求用户记忆 Slash 命令。只有用户明确希望建立、切换或暂停学习路径时，才讨论状态选择。\n\n"
            "当用户明确提供 URL、公开 GitHub 仓库链接或工作区资料文件并要求研究、阅读或加入学习时，"
            "必须调用对应 research 工具；读取失败时如实说明，不能编造来源、版本、热点或任务。\n\n"
            f"{goal_context}\n\n"
            f"# Allowed Skill\n\n{skill_context}"
        )
        self.activator.activate(
            runtime,
            role_id=role.id,
            role_context=context,
            skill_names=tuple(package.spec.name for package in packages),
        )
        return "✻ Skill 已准备好，开始第 1 步…"

    @staticmethod
    def activate_quick_introduction(runtime: TaskRuntime) -> None:
        """Use a one-turn teaching overlay without loading a Skill or tools."""

        runtime.activate_role(
            "quick-technical-introduction",
            "# Temporary Role: Quick Technical Introduction\n\n"
            "仅回答用户本轮要求的技术简介。使用用户的语言，先给简明结论，再解释关键概念；"
            "可提供非常短的示例，但不要创建或修改文件、运行命令、调用工具、规划项目、"
            "追问学习背景、建立学习路径，或主动提及既有学习路径。回答完成后立即结束。",
        )
