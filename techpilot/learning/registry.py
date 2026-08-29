"""Code-owned Role registration and safe discovery of approved Skill packages."""

from __future__ import annotations

from pathlib import Path

from techpilot.runtime.extensions import RoleRegistry as RuntimeRoleRegistry
from techpilot.runtime.extensions import SkillPackage, parse_skill_spec
from techpilot.runtime.extensions import SkillRegistry as RuntimeSkillRegistry

from .contracts import RoleDefinition, SkillManifest

__all__ = [
    "DEVELOPER_LEARNING_COACH",
    "RoleRegistry",
    "SkillPackage",
    "SkillRegistry",
    "parse_skill_manifest",
]

DEVELOPER_LEARNING_COACH = RoleDefinition(
    id="developer-learning-coach",
    title="Developer Learning Coach",
    system_prompt=(
        "你是面向开发者的技术学习教练。使用与用户相同的语言交流；先直接讲解当前问题，"
        "再在需要时组织可完成的学习步骤、练习和结构化知识。用户明确提供的公开链接、"
        "GitHub 仓库和工作区文本资料可以读取并留档；自动全网搜索、测验和知识库同步"
        "尚未接入时必须如实说明，不能假装已完成。"
    ),
    allowed_skill_ids=("developer-learning",),
    artifact_types=("learning-goal", "learning-plan", "knowledge-draft"),
)


class RoleRegistry(RuntimeRoleRegistry):
    """Learning sample adapter for the generic Runtime Role registry."""

    @classmethod
    def with_builtin_roles(cls) -> RoleRegistry:
        return cls((DEVELOPER_LEARNING_COACH,))


class SkillRegistry(RuntimeSkillRegistry):
    """Learning sample adapter for the generic Runtime Skill registry."""

    @classmethod
    def with_builtin_skills(cls, role_registry: RoleRegistry | None = None) -> SkillRegistry:
        registry = cls(role_registry or RoleRegistry.with_builtin_roles())
        registry.discover(Path(__file__).resolve().parent / "skills", approved=True, builtin=True)
        return registry


def parse_skill_manifest(content: str, label: str = "SKILL.md") -> SkillManifest:
    """Compatibility parser for the Runtime-owned Skill specification."""

    return SkillManifest.model_validate(parse_skill_spec(content, label).model_dump())
