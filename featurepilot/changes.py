"""Source snapshot comparison and reviewable patch generation for Managed Runs."""

from __future__ import annotations

import difflib
import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path

from .domain import PlanRecord
from .path_policy import should_ignore_repository_path
from .workspace import Workspace


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    """In-memory source contents captured before the Agent can make changes."""

    root: Path
    digest: str
    files: dict[str, bytes]


@dataclass(frozen=True, slots=True)
class FileChange:
    """One added, modified, or deleted repository file."""

    path: str
    status: str
    planned: bool
    binary: bool
    additions: int
    deletions: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ChangeArtifact:
    """Structured summary paired with the generated ``changes.patch`` file."""

    source_digest: str
    files: list[FileChange]

    @property
    def additions(self) -> int:
        return sum(change.additions for change in self.files)

    @property
    def deletions(self) -> int:
        return sum(change.deletions for change in self.files)

    @property
    def out_of_plan_files(self) -> list[str]:
        return [change.path for change in self.files if not change.planned]

    def to_dict(self) -> dict[str, object]:
        return {
            "source_digest": self.source_digest,
            "additions": self.additions,
            "deletions": self.deletions,
            "out_of_plan_files": self.out_of_plan_files,
            "files": [change.to_dict() for change in self.files],
        }


class ChangeService:
    """Capture a source baseline and compare it with an isolated Workspace."""

    def capture(self, source_path: Path) -> RepositorySnapshot:
        root = source_path.resolve()
        files = _read_repository_files(root)
        return RepositorySnapshot(root=root, digest=_digest_files(files), files=files)

    def generate(
        self,
        snapshot: RepositorySnapshot,
        workspace: Workspace,
        record: PlanRecord,
        *,
        output_path: Path | None = None,
    ) -> tuple[ChangeArtifact, Path]:
        workspace_files = _read_repository_files(workspace.path.resolve())
        approved = {
            path.replace("\\", "/").casefold()
            for path in (*record.plan.modify_files, *record.plan.expected_files)
        }
        changes: list[FileChange] = []
        patch_parts: list[str] = []
        all_paths = sorted(set(snapshot.files) | set(workspace_files))
        for path in all_paths:
            old = snapshot.files.get(path)
            new = workspace_files.get(path)
            if old == new:
                continue
            status = "added" if old is None else ("deleted" if new is None else "modified")
            binary = _is_binary(old) or _is_binary(new)
            additions = 0
            deletions = 0
            if binary:
                patch_parts.append(_binary_patch_line(path, status))
            else:
                patch, additions, deletions = _unified_patch(path, old, new)
                patch_parts.append(patch)
            changes.append(FileChange(
                path=path,
                status=status,
                planned=path.casefold() in approved,
                binary=binary,
                additions=additions,
                deletions=deletions,
            ))

        artifact = ChangeArtifact(source_digest=snapshot.digest, files=changes)
        run_directory = workspace.path.resolve().parent
        patch_path = (output_path or run_directory / "changes.patch").resolve()
        if patch_path != run_directory / "changes.patch":
            raise ValueError("Managed Run patch must be stored at <run>/changes.patch")
        _atomic_write_text(patch_path, "".join(patch_parts), suffix=workspace.run_id)
        return artifact, patch_path


def _read_repository_files(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if should_ignore_repository_path(relative) or not path.is_file():
            continue
        files[relative.as_posix()] = path.read_bytes()
    return files


def _digest_files(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for path, content in sorted(files.items()):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
    return digest.hexdigest()


def _is_binary(content: bytes | None) -> bool:
    if content is None:
        return False
    if b"\0" in content:
        return True
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _unified_patch(path: str, old: bytes | None, new: bytes | None) -> tuple[str, int, int]:
    old_text = "" if old is None else old.decode("utf-8")
    new_text = "" if new is None else new.decode("utf-8")
    fromfile = "/dev/null" if old is None else f"a/{path}"
    tofile = "/dev/null" if new is None else f"b/{path}"
    lines = list(difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=fromfile,
        tofile=tofile,
        lineterm="\n",
    ))
    additions = sum(line.startswith("+") and not line.startswith("+++") for line in lines)
    deletions = sum(line.startswith("-") and not line.startswith("---") for line in lines)
    return _render_patch_lines(lines), additions, deletions


def _render_patch_lines(lines: list[str]) -> str:
    rendered: list[str] = []
    for line in lines:
        rendered.append(line)
        if (
            line[:1] in {" ", "+", "-"}
            and not line.startswith(("+++ ", "--- "))
            and not line.endswith(("\n", "\r"))
        ):
            rendered.append("\n\\ No newline at end of file\n")
    return "".join(rendered)


def _binary_patch_line(path: str, status: str) -> str:
    fromfile = "/dev/null" if status == "added" else f"a/{path}"
    tofile = "/dev/null" if status == "deleted" else f"b/{path}"
    return f"Binary files {fromfile} and {tofile} differ\n"


def _atomic_write_text(path: Path, content: str, *, suffix: str) -> None:
    temporary = path.parent / f".{path.name}-{suffix}.tmp"
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
