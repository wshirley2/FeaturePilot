"""File inventory and lightweight symbol index."""

from dataclasses import dataclass, field
from pathlib import Path

from .python_ast import PythonModule, parse_python_source

DEFAULT_IGNORED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}


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
        ignored = ignored_directories or DEFAULT_IGNORED_DIRECTORIES
        index = cls(root=repository_root)

        for path in sorted(repository_root.rglob("*")):
            if not path.is_file() or any(
                part in ignored or part.endswith(".egg-info") for part in path.parts
            ):
                continue
            relative_path = path.relative_to(repository_root).as_posix()
            index.files.append(relative_path)
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            index.file_texts[relative_path] = text
            if path.suffix == ".py":
                index.python_modules[relative_path] = parse_python_source(text, relative_path)
        return index

    def to_dict(self) -> dict[str, object]:
        return {
            "root": str(self.root),
            "files": self.files,
            "python_modules": {
                path: module.to_dict() for path, module in self.python_modules.items()
            },
        }
