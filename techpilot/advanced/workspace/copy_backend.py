"""Copy-based isolated workspace implementation."""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

from ...safety.paths import ignored_child_names, should_ignore_repository_path
from .backend import Workspace


class WorkspaceCreationError(ValueError):
    """An isolated workspace could not be created without touching the source."""


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

        try:
            source_snapshot = self.source_snapshot(source_root)
        except OSError as error:
            raise WorkspaceCreationError(
                f"Could not inspect source repository: {_brief_os_error(error, source_root)}"
            ) from error
        run_directory.mkdir(parents=True, exist_ok=False)
        try:
            shutil.copytree(source_root, workspace_path, ignore=_ignore_workspace_files)
            if self.source_snapshot(source_root) != source_snapshot:
                raise RuntimeError("Source repository changed while the workspace was being created")
        except (OSError, RuntimeError) as error:
            cleanup_error = _remove_incomplete_run(run_directory, runs_root)
            detail = _brief_copy_error(error, source_root)
            if cleanup_error is not None:
                detail += f"; incomplete workspace could not be removed: {cleanup_error}"
            raise WorkspaceCreationError(f"Could not create workspace: {detail}") from error
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
            if should_ignore_repository_path(relative_path):
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
    return ignored_child_names(names)


def _remove_incomplete_run(run_directory: Path, runs_root: Path) -> OSError | None:
    """Remove only the exact run directory created by this backend call."""

    if run_directory.parent != runs_root or not run_directory.name:
        return OSError("refused unsafe cleanup target")
    try:
        shutil.rmtree(run_directory)
    except OSError as error:
        return error
    return None


def _brief_copy_error(error: OSError | RuntimeError, source_root: Path) -> str:
    if isinstance(error, shutil.Error) and error.args and isinstance(error.args[0], list):
        failures = error.args[0]
        if failures:
            source, _, detail = failures[0]
            path = _relative_display(Path(source), source_root)
            return f"{len(failures)} item(s) failed; first: {path}: {detail}"
    if isinstance(error, OSError):
        return _brief_os_error(error, source_root)
    return str(error)


def _brief_os_error(error: OSError, source_root: Path) -> str:
    path = Path(error.filename) if error.filename else None
    location = f" ({_relative_display(path, source_root)})" if path else ""
    return f"{error.strerror or type(error).__name__}{location}"


def _relative_display(path: Path, source_root: Path) -> str:
    try:
        return path.resolve().relative_to(source_root).as_posix()
    except ValueError:
        return path.name or str(path)


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
