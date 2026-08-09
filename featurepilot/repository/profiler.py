"""Repository profile generation."""

from dataclasses import asdict, dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python < 3.11
    import tomli as tomllib

from .index import RepositoryIndex


@dataclass(slots=True)
class RepositoryProfile:
    root: str
    language: str
    frameworks: list[str] = field(default_factory=list)
    entrypoints: list[str] = field(default_factory=list)
    config_files: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    validation_commands: list[list[str]] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    symbols: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class RepositoryProfiler:
    """Build a deterministic, explainable profile for a local repository."""

    def profile(self, root: str | Path) -> RepositoryProfile:
        index = RepositoryIndex.build(root)
        config_files = [
            path
            for path in index.files
            if Path(path).name in {"pyproject.toml", "setup.cfg", "setup.py", "tox.ini"}
        ]
        test_files = [
            path
            for path in index.files
            if Path(path).name.startswith("test_")
            or Path(path).name.endswith("_test.py")
            or "/tests/" in f"/{path}/"
        ]

        imports = {
            imported.split(".", maxsplit=1)[0]
            for module in index.python_modules.values()
            for imported in module.imports
        }
        frameworks = [name for name in ("fastapi", "flask", "click", "typer") if name in imports]
        entrypoints = self._find_entrypoints(index)
        validation_commands = self._find_validation_commands(index, test_files, config_files)
        symbols = {
            path: [symbol.name for symbol in module.symbols]
            for path, module in index.python_modules.items()
            if module.symbols
        }
        return RepositoryProfile(
            root=str(index.root),
            language="python" if index.python_modules else "unknown",
            frameworks=frameworks,
            entrypoints=entrypoints,
            config_files=config_files,
            test_files=test_files,
            validation_commands=validation_commands,
            files=index.files,
            symbols=symbols,
        )

    @staticmethod
    def _find_entrypoints(index: RepositoryIndex) -> list[str]:
        entrypoints: list[str] = []
        for path in index.python_modules:
            text = index.file_texts.get(path, "")
            if (
                Path(path).name in {"cli.py", "main.py", "__main__.py"}
                or "if __name__ == \"__main__\"" in text
                or "if __name__ == '__main__'" in text
            ):
                entrypoints.append(path)
        return sorted(set(entrypoints))

    @staticmethod
    def _find_validation_commands(
        index: RepositoryIndex,
        test_files: list[str],
        config_files: list[str],
    ) -> list[list[str]]:
        commands: list[list[str]] = []
        pyproject = index.root / "pyproject.toml"
        config: dict[str, object] = {}
        if pyproject.exists():
            try:
                config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError):
                config = {}
        if test_files or "pytest" in str(config):
            commands.append(["python", "-m", "pytest", "-q"])
        if "ruff" in str(config) or any(path.endswith(".py") for path in index.files):
            commands.append(["python", "-m", "ruff", "check", "."])
        return commands
