"""File inventory and lightweight symbol index."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..safety.paths import DEFAULT_IGNORED_DIRECTORIES, should_ignore_repository_path
from .python_ast import PythonModule, parse_python_source


@dataclass(slots=True)
class RepositoryIndex:
    root: Path
    files: list[str] = field(default_factory=list)
    file_texts: dict[str, str] = field(default_factory=dict)
    python_modules: dict[str, PythonModule] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        root: str | Path,
        ignored_directories: set[str] | None = None,
    ) -> "RepositoryIndex":
        repository_root = Path(root).resolve()
        ignored = DEFAULT_IGNORED_DIRECTORIES if ignored_directories is None else ignored_directories
        index = cls(root=repository_root)

        # ``Path.rglob`` recursively enters every directory before a caller
        # can filter the resulting path.  That makes a normal Chat launch
        # inspect virtualenvs and retained test artifacts even though they are
        # later excluded from the profile.  Prune ``os.walk`` in-place so the
        # ignored directories are never enumerated.
        for directory, child_directories, child_files in os.walk(repository_root, topdown=True):
            directory_path = Path(directory)
            relative_directory = directory_path.relative_to(repository_root)
            child_directories[:] = [
                name
                for name in sorted(child_directories)
                if not should_ignore_repository_path(relative_directory / name, ignored_directories=ignored)
            ]
            for name in sorted(child_files):
                relative_path = relative_directory / name
                if should_ignore_repository_path(relative_path, ignored_directories=ignored):
                    continue
                path = directory_path / name
                if not path.is_file():
                    continue
                normalized_path = relative_path.as_posix()
                index.files.append(normalized_path)
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                index.file_texts[normalized_path] = text
                if path.suffix == ".py":
                    index.python_modules[normalized_path] = parse_python_source(text, normalized_path)
        return index

    def to_dict(self) -> dict[str, object]:
        return {
            "root": str(self.root),
            "files": self.files,
            "python_modules": {
                path: module.to_dict() for path, module in self.python_modules.items()
            },
        }

    def import_graph(self) -> dict[str, list[str]]:
        """Resolve imports that point to another Python file in this repository."""
        module_paths: dict[str, str] = {}
        for path in self.python_modules:
            for module_name in _module_name_candidates(path):
                module_paths.setdefault(module_name, path)

        graph: dict[str, list[str]] = {}
        for path, module in self.python_modules.items():
            targets: set[str] = set()
            for imported in module.imports:
                module_name = imported.lstrip(".")
                if not module_name:
                    continue
                for candidate, target in module_paths.items():
                    if (
                        target != path
                        and (candidate == module_name or candidate.startswith(f"{module_name}."))
                    ):
                        targets.add(target)
            graph[path] = sorted(targets)
        return graph


def _module_name_candidates(path: str) -> list[str]:
    parts = list(Path(path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return [".".join(parts[start:]) for start in range(len(parts)) if parts[start:]]
