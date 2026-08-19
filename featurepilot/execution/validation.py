"""Structured validation command execution for FeaturePilot Workspaces."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ValidationCommandResult:
    """Auditable outcome of one exact approved validation command."""

    argv: list[str]
    resolved_argv: list[str]
    cwd: str
    status: str
    exit_code: int | None
    duration_seconds: float
    stdout: str
    stderr: str
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ValidationArtifact:
    """Aggregate validation facts retained beside one Managed Run."""

    run_id: str
    status: str
    started_at: str
    completed_at: str
    commands: list[ValidationCommandResult]

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "commands": [command.to_dict() for command in self.commands],
        }


class ValidationCommandRunner:
    """Run one already-approved argument vector without invoking a shell."""

    def __init__(self, timeout_seconds: int = 120) -> None:
        self.timeout_seconds = timeout_seconds

    def run(self, command: Sequence[str], workspace_path: Path) -> str:
        """Run an exact command list in the Workspace and return a tool-style result."""

        result = self.execute(command, workspace_path)
        if result.status == "timed_out":
            return f"Error: validation command timed out after {self.timeout_seconds}s"
        if result.status == "startup_error":
            return f"Error running validation command: {result.error}"

        output = result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        if result.exit_code:
            output += f"\n[exit code: {result.exit_code}]"
        return output.strip() or "(no output)"

    def execute(
        self,
        command: Sequence[str],
        workspace_path: Path,
    ) -> ValidationCommandResult:
        """Execute one command and retain stdout/stderr even when it cannot pass."""

        started = time.monotonic()
        argv = list(command)
        cwd = str(workspace_path.resolve())
        if not argv or any(not isinstance(item, str) or not item for item in argv):
            return ValidationCommandResult(
                argv=argv,
                resolved_argv=argv,
                cwd=cwd,
                status="startup_error",
                exit_code=None,
                duration_seconds=_elapsed(started),
                stdout="",
                stderr="",
                error="validation command must be a non-empty list of strings",
            )

        resolved_argv = _resolve_python_command(argv)
        try:
            completed = subprocess.run(
                resolved_argv,
                cwd=workspace_path,
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            return ValidationCommandResult(
                argv=argv,
                resolved_argv=resolved_argv,
                cwd=cwd,
                status="timed_out",
                exit_code=None,
                duration_seconds=_elapsed(started),
                stdout=_output_text(error.stdout),
                stderr=_output_text(error.stderr),
                error=f"timed out after {self.timeout_seconds}s",
            )
        except OSError as error:
            return ValidationCommandResult(
                argv=argv,
                resolved_argv=resolved_argv,
                cwd=cwd,
                status="startup_error",
                exit_code=None,
                duration_seconds=_elapsed(started),
                stdout="",
                stderr="",
                error=str(error),
            )

        return ValidationCommandResult(
            argv=argv,
            resolved_argv=resolved_argv,
            cwd=cwd,
            status="passed" if completed.returncode == 0 else "failed",
            exit_code=completed.returncode,
            duration_seconds=_elapsed(started),
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class ValidationService:
    """Run every approved command and atomically persist ``validation.json``."""

    def __init__(self, runner: ValidationCommandRunner | None = None) -> None:
        self.runner = runner or ValidationCommandRunner()

    def validate(
        self,
        run_id: str,
        workspace_path: Path,
        commands: Sequence[Sequence[str]],
    ) -> tuple[ValidationArtifact, Path]:
        started_at = datetime.now(timezone.utc).isoformat()
        results = [self.runner.execute(command, workspace_path) for command in commands]
        artifact = ValidationArtifact(
            run_id=run_id,
            status="passed" if all(result.status == "passed" for result in results) else "failed",
            started_at=started_at,
            completed_at=datetime.now(timezone.utc).isoformat(),
            commands=results,
        )
        path = self.save(artifact, workspace_path)
        return artifact, path

    @staticmethod
    def save(artifact: ValidationArtifact, workspace_path: Path) -> Path:
        run_directory = workspace_path.resolve().parent
        artifact_path = run_directory / "validation.json"
        temporary_path = run_directory / f".validation-{artifact.run_id}.tmp"
        payload = f"{json.dumps(artifact.to_dict(), ensure_ascii=False, indent=2)}\n"
        try:
            temporary_path.write_text(payload, encoding="utf-8")
            temporary_path.replace(artifact_path)
        except OSError:
            temporary_path.unlink(missing_ok=True)
            raise
        return artifact_path


def _resolve_python_command(argv: list[str]) -> list[str]:
    resolved = list(argv)
    if resolved[0].casefold() in {"python", "python.exe"}:
        resolved[0] = sys.executable
    return resolved


def _elapsed(started: float) -> float:
    return round(time.monotonic() - started, 6)


def _output_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
