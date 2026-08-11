"""Copy-based isolated workspace implementation."""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

from .backend import Workspace

_IGNORED_NAMES = {
    ".featurepilot",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "runs",
    "venv",
}


class CopyWorkspaceBackend:
    """Create a workspace by copying a repository into a run-specific directory."""

    def __init__(self, runs_directory: Path) -> None:
        self.runs_directory = runs_directory

    def create(self, source_path: Path, run_id: str, label: str | None = None) -> Workspace:
        source_root = source_path.resolve()
        if not source_root.is_dir():
            raise ValueError(f"Source repository does not exist: {source_path}")
        if not _is_safe_run_id(run_id):
            raise ValueError("Run id must be a 32-character lowercase hexadecimal UUID")
        if label is not None and not _is_safe_label(label):
            raise ValueError("Workspace label must contain only letters, numbers, dots or hyphens")

        runs_root = self.runs_directory.resolve()
        _reject_source_inside_runs(source_root, runs_root)
        display_id = run_id[:8]
        directory_name = f"{label}-{display_id}" if label else display_id
        run_directory = runs_root / directory_name
        workspace_path = run_directory / "workspace"
        if run_directory.exists():
            raise ValueError(f"Short Run id collision: {display_id}")

        source_snapshot = self.source_snapshot(source_root)
        run_directory.mkdir(parents=True, exist_ok=False)
        try:
            shutil.copytree(source_root, workspace_path, ignore=_ignore_workspace_files)
        except OSError as error:
            raise ValueError(f"Could not create workspace: {error}") from error
        if self.source_snapshot(source_root) != source_snapshot:
            raise RuntimeError("Source repository changed while the workspace was being created")
        return Workspace(
            run_id=run_id,
            source_path=source_root,
            path=workspace_path,
            source_snapshot=source_snapshot,
        )

    def source_snapshot(self, source_path: Path) -> str:
        """Return a stable digest of repository files relevant to a workspace copy."""

        digest = hashlib.sha256()
        source_root = source_path.resolve()
        for path in sorted(source_root.rglob("*")):
            relative_path = path.relative_to(source_root)
            if _should_ignore(relative_path):
                continue
            if not path.is_file():
                continue
            digest.update(relative_path.as_posix().encode("utf-8"))
            digest.update(b"\0")
            with path.open("rb") as source_file:
                for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
                    digest.update(chunk)
        return digest.hexdigest()

    def source_is_unchanged(self, workspace: Workspace) -> bool:
        """Check that the original repository still matches its pre-copy snapshot."""

        return self.source_snapshot(workspace.source_path) == workspace.source_snapshot


def _ignore_workspace_files(_: str, names: list[str]) -> set[str]:
    return {name for name in names if name in _IGNORED_NAMES}


def _should_ignore(relative_path: Path) -> bool:
    return any(part in _IGNORED_NAMES for part in relative_path.parts)


def _is_safe_run_id(run_id: str) -> bool:
    return len(run_id) == 32 and all(character in "0123456789abcdef" for character in run_id)


def _is_safe_label(label: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", label))


def _reject_source_inside_runs(source_root: Path, runs_root: Path) -> None:
    try:
        source_root.relative_to(runs_root)
    except ValueError:
        return
    raise ValueError("Source repository cannot be inside the runs directory")
