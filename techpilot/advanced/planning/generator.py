"""Deterministic first-pass implementation plan generation."""

import re
from collections.abc import Sequence

from ...domain import Plan, Task
from ...repository import CandidateFile, RepositoryProfile


class PlanGenerator:
    """Create a reviewable Plan from a task and selected repository files.

    This first version intentionally does not call an LLM. It creates a stable
    draft so the Plan schema and validation flow can be tested before adding
    model-generated reasoning.
    """

    def generate(
        self,
        task: Task,
        profile: RepositoryProfile,
        candidates: Sequence[CandidateFile],
    ) -> Plan:
        candidate_paths = sorted({candidate.path for candidate in candidates})
        explicit_paths = self._explicit_repository_paths(task.description, candidate_paths)
        if explicit_paths:
            candidate_paths = explicit_paths
        terms = set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", task.description.lower()))
        modify_files = [
            path for path in candidate_paths if self._is_modification_candidate(path, terms)
        ]
        steps = [
            "阅读候选文件并确认现有实现",
            f"修改相关文件以完成：{task.description}",
            "运行项目验证命令并检查结果",
        ]
        steps.extend(f"验证验收条件：{criterion}" for criterion in task.acceptance_criteria)

        return Plan(
            task_id=task.id,
            summary=task.description,
            assumptions=["本计划只覆盖仓库分析阶段筛选出的候选文件。"],
            steps=steps,
            read_files=candidate_paths,
            modify_files=modify_files,
            expected_files=[],
            validation_commands=[list(command) for command in profile.validation_commands],
            risks=["修改范围和最终 Diff 仍需要人工审核。"],
            open_questions=[],
        )

    @staticmethod
    def _is_modification_candidate(path: str, terms: set[str]) -> bool:
        lower_path = path.lower()
        if lower_path.endswith(".py") or "/tests/" in f"/{lower_path}/":
            return True
        if lower_path.endswith("readme.md"):
            return "readme" in terms or "doc" in terms or "documentation" in terms
        return any(lower_path.endswith(extension) for extension in (".toml", ".yaml", ".yml"))

    @staticmethod
    def _explicit_repository_paths(description: str, candidates: list[str]) -> list[str]:
        """Prefer an exact repository-relative path explicitly named by the user."""

        mentioned = {
            match.replace("\\", "/").lstrip("./").casefold()
            for match in re.findall(
                r"(?<![\w.-])(?:[\w.-]+[\\/])*[\w.-]+\.[A-Za-z0-9]+",
                description,
            )
        }
        return [path for path in candidates if path.casefold() in mentioned]
