"""Code-owned Role registration and safe discovery of approved Skill packages."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .contracts import RoleDefinition, SkillManifest

_FRONTMATTER_BOUNDARY = "---"
_FORBIDDEN_MANIFEST_FIELDS = {
    "effect",
    "effects",
    "concurrency",
    "permission",
    "permissions",
    "resource",
    "resources",
    "tool",
    "tools",
    "allowed_tools",
}
_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")

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


class RoleRegistry:
    """Resolve code-owned Roles without giving Roles execution privileges."""

    def __init__(self, definitions: tuple[RoleDefinition, ...] = ()) -> None:
        self._definitions: dict[str, RoleDefinition] = {}
        for definition in definitions:
            self.register(definition)

    @classmethod
    def with_builtin_roles(cls) -> RoleRegistry:
        return cls((DEVELOPER_LEARNING_COACH,))

    def register(self, definition: RoleDefinition) -> None:
        if definition.id in self._definitions:
            raise ValueError(f"role is already registered: {definition.id}")
        self._definitions[definition.id] = definition

    def get(self, role_id: str) -> RoleDefinition:
        try:
            return self._definitions[role_id]
        except KeyError as error:
            raise ValueError(f"unknown role: {role_id}") from error

    def all(self) -> tuple[RoleDefinition, ...]:
        return tuple(self._definitions.values())


@dataclass(frozen=True)
class SkillPackage:
    manifest: SkillManifest
    skill_markdown: str
    root: Path
    approved: bool
    builtin: bool = False


class SkillRegistry:
    """Discover packages recursively while exposing only approved or built-in Skills."""

    def __init__(self, role_registry: RoleRegistry) -> None:
        self.role_registry = role_registry
        self._packages: dict[str, SkillPackage] = {}

    @classmethod
    def with_builtin_skills(cls, role_registry: RoleRegistry | None = None) -> SkillRegistry:
        registry = cls(role_registry or RoleRegistry.with_builtin_roles())
        registry.discover(cls._builtin_skills_root(), approved=True, builtin=True)
        return registry

    @staticmethod
    def _builtin_skills_root() -> Path:
        return Path(__file__).resolve().parent / "skills"

    def discover(self, root: Path, *, approved: bool = False, builtin: bool = False) -> tuple[SkillPackage, ...]:
        """Index public Skill packages below one controlled root without running their code."""

        resolved_root = root.expanduser().resolve()
        if not resolved_root.is_dir():
            raise ValueError(f"skill root does not exist: {root}")
        discovered: list[SkillPackage] = []
        for path in sorted(resolved_root.rglob("SKILL.md")):
            package = self._read_package(path, resolved_root, approved=approved, builtin=builtin)
            self._register_package(package)
            discovered.append(package)
        return tuple(discovered)

    def approve(self, skill_name: str) -> SkillPackage:
        package = self._get_any(skill_name)
        approved = SkillPackage(
            manifest=package.manifest,
            skill_markdown=package.skill_markdown,
            root=package.root,
            approved=True,
            builtin=package.builtin,
        )
        self._packages[skill_name] = approved
        return approved

    def get(self, skill_name: str) -> SkillPackage:
        package = self._get_any(skill_name)
        if not (package.approved or package.builtin):
            raise ValueError(f"skill is not approved: {skill_name}")
        return package

    def allowed_for_role(self, role_id: str) -> tuple[SkillPackage, ...]:
        role = self.role_registry.get(role_id)
        return tuple(self.get(name) for name in role.allowed_skill_ids if name in self._packages and self._is_available(name))

    def match(self, role_id: str, request: str) -> tuple[SkillPackage, ...]:
        """Return approved allowlisted Skills ordered by a deterministic text match."""

        candidates = self.allowed_for_role(role_id)
        request_tokens = set(_TOKEN_PATTERN.findall(request.lower()))

        def score(package: SkillPackage) -> tuple[int, str]:
            manifest_tokens = set(_TOKEN_PATTERN.findall(f"{package.manifest.name} {package.manifest.description}".lower()))
            return (len(request_tokens & manifest_tokens), package.manifest.name)

        return tuple(sorted(candidates, key=score, reverse=True))

    def _is_available(self, skill_name: str) -> bool:
        package = self._packages[skill_name]
        return package.approved or package.builtin

    def _get_any(self, skill_name: str) -> SkillPackage:
        try:
            return self._packages[skill_name]
        except KeyError as error:
            raise ValueError(f"unknown skill: {skill_name}") from error

    def _register_package(self, package: SkillPackage) -> None:
        name = package.manifest.name
        if name in self._packages:
            raise ValueError(f"duplicate skill name: {name}")
        self._packages[name] = package

    @staticmethod
    def _read_package(path: Path, root: Path, *, approved: bool, builtin: bool) -> SkillPackage:
        resolved_path = path.resolve()
        if path.is_symlink() or not resolved_path.is_file():
            raise ValueError(f"skill manifest must be a regular file: {path}")
        try:
            resolved_path.relative_to(root)
        except ValueError as error:
            raise ValueError(f"skill manifest escapes its discovery root: {path}") from error
        try:
            content = resolved_path.read_text(encoding="utf-8")
        except OSError as error:
            raise ValueError(f"could not read skill manifest {path}: {error}") from error
        manifest = parse_skill_manifest(content, resolved_path.name)
        return SkillPackage(manifest=manifest, skill_markdown=content, root=resolved_path.parent, approved=approved, builtin=builtin)


def parse_skill_manifest(content: str, label: str = "SKILL.md") -> SkillManifest:
    """Parse public metadata while rejecting Runtime-owned safety declarations."""

    lines = content.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_BOUNDARY:
        raise ValueError(f"skill manifest {label} must start with YAML frontmatter")
    try:
        closing_index = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == _FRONTMATTER_BOUNDARY)
    except StopIteration as error:
        raise ValueError(f"skill manifest {label} has unterminated YAML frontmatter") from error
    try:
        parsed = yaml.safe_load("\n".join(lines[1:closing_index]))
    except yaml.YAMLError as error:
        raise ValueError(f"skill manifest {label} has invalid YAML frontmatter: {error}") from error
    if not isinstance(parsed, dict):
        raise TypeError(f"skill manifest {label} frontmatter must be a mapping")
    forbidden = sorted(str(key) for key in parsed if str(key).lower() in _FORBIDDEN_MANIFEST_FIELDS)
    if forbidden:
        raise ValueError(f"skill manifest {label} cannot declare runtime safety fields: {', '.join(forbidden)}")
    try:
        return SkillManifest.model_validate(parsed)
    except ValueError as error:
        raise ValueError(f"skill manifest {label} is invalid: {error}") from error
