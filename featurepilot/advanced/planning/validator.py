"""Validation for structured implementation plans."""

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from ...domain import Plan
from ...repository import RepositoryProfile


@dataclass(slots=True)
class PlanValidationResult:
    """Validation errors and non-blocking warnings for a Plan."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def raise_if_invalid(self) -> None:
        if self.errors:
            details = "\n".join(f"- {error}" for error in self.errors)
            raise ValueError(f"Invalid plan:\n{details}")


class PlanValidator:
    """Check Plan structure, repository paths and validation commands."""

    def validate(self, plan: Plan, profile: RepositoryProfile) -> PlanValidationResult:
        result = PlanValidationResult()
        if not plan.summary.strip():
            result.errors.append("summary must not be empty")
        if not plan.steps:
            result.errors.append("steps must contain at least one step")
        if not plan.read_files and not plan.modify_files:
            result.errors.append("Plan must reference at least one repository file")

        known_files = set(profile.files)
        expected_files = set(plan.expected_files)
        self._validate_file_list("read_files", plan.read_files, known_files, set(), result)
        self._validate_file_list(
            "modify_files",
            plan.modify_files,
            known_files,
            expected_files,
            result,
        )
        self._validate_file_list("expected_files", plan.expected_files, known_files, set(), result)

        allowed_commands = {tuple(command) for command in profile.validation_commands}
        for command in plan.validation_commands:
            if not command or not all(isinstance(part, str) and part for part in command):
                result.errors.append("validation_commands must contain non-empty string commands")
            elif tuple(command) not in allowed_commands:
                result.errors.append(f"validation command is not allowed by the repository profile: {command}")

        if not plan.validation_commands:
            result.warnings.append("Plan has no validation command")
        if not set(plan.modify_files).issubset(set(plan.read_files) | expected_files):
            result.warnings.append("modify_files contains files that are not listed in read_files or expected_files")
        return result

    @staticmethod
    def _validate_file_list(
        field_name: str,
        paths: list[str],
        known_files: set[str],
        allowed_new_files: set[str],
        result: PlanValidationResult,
    ) -> None:
        seen: set[str] = set()
        for path in paths:
            if not isinstance(path, str) or not path:
                result.errors.append(f"{field_name} contains an empty or invalid path")
                continue
            if path in seen:
                result.errors.append(f"{field_name} contains a duplicate path: {path}")
            seen.add(path)
            if not PlanValidator._is_safe_relative_path(path):
                result.errors.append(f"{field_name} contains an unsafe path: {path}")
                continue
            if path not in known_files and path not in allowed_new_files:
                result.errors.append(f"{field_name} references a file outside the profile: {path}")

    @staticmethod
    def _is_safe_relative_path(path: str) -> bool:
        normalized = path.replace("\\", "/")
        return not (
            normalized.startswith(("/", "\\"))
            or re.match(r"^[A-Za-z]:", normalized)
            or ".." in PurePosixPath(normalized).parts
        )
