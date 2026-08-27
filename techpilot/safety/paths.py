"""Shared repository path policy for analysis and isolated workspace copies."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

DEFAULT_IGNORED_DIRECTORIES = frozenset({
    ".techpilot",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tmp",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "runs",
    "tmp",
    "venv",
})

_SAFE_ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template"})


def is_sensitive_file_name(name: str) -> bool:
    """Return whether a repository file commonly contains local credentials."""

    normalized = name.casefold()
    return normalized == ".env" or (
        normalized.startswith(".env.") and normalized not in _SAFE_ENV_TEMPLATES
    )


def should_ignore_repository_path(
    relative_path: Path,
    *,
    ignored_directories: Iterable[str] = DEFAULT_IGNORED_DIRECTORIES,
) -> bool:
    """Apply the same generated-artifact and secret policy to a relative path."""

    ignored = set(ignored_directories)
    return (
        any(part in ignored or part.endswith(".egg-info") for part in relative_path.parts)
        or is_sensitive_file_name(relative_path.name)
    )


def ignored_child_names(names: Iterable[str]) -> set[str]:
    """Adapt the shared policy to ``shutil.copytree``'s ignore callback."""

    return {
        name
        for name in names
        if name in DEFAULT_IGNORED_DIRECTORIES
        or name.endswith(".egg-info")
        or is_sensitive_file_name(name)
    }
