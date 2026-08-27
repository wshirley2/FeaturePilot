"""Structured validation command execution for FeaturePilot Workspaces."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from featurepilot.engine.runtime_control import CancellationToken

_PROCESS_POLL_SECONDS = 0.05
_PROCESS_STOP_GRACE_SECONDS = 0.5


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

    def run(
        self,
        command: Sequence[str],
        workspace_path: Path,
        *,
        cancellation_token: CancellationToken | None = None,
    ) -> str:
        """Run an exact command list in the Workspace and return a tool-style result."""

        result = self.execute(
            command,
            workspace_path,
            cancellation_token=cancellation_token,
        )
        if result.status == "cancelled":
            return f"Error: validation command cancelled: {result.error}"
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
        *,
        cancellation_token: CancellationToken | None = None,
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
        if cancellation_token is not None and cancellation_token.cancelled:
            return _stopped_result(
                argv=argv,
                resolved_argv=resolved_argv,
                cwd=cwd,
                status="cancelled",
                started=started,
                error=cancellation_token.reason,
            )
        try:
            process = subprocess.Popen(
                resolved_argv,
                cwd=workspace_path,
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=os.name != "nt",
                creationflags=(
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    if os.name == "nt"
                    else 0
                ),
            )
            windows_job = _create_windows_job(process)
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

        deadline = started + self.timeout_seconds
        while True:
            if cancellation_token is not None and cancellation_token.cancelled:
                stdout, stderr = _stop_process_tree(process, windows_job)
                return _stopped_result(
                    argv=argv,
                    resolved_argv=resolved_argv,
                    cwd=cwd,
                    status="cancelled",
                    started=started,
                    stdout=stdout,
                    stderr=stderr,
                    error=cancellation_token.reason,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stdout, stderr = _stop_process_tree(process, windows_job)
                return _stopped_result(
                    argv=argv,
                    resolved_argv=resolved_argv,
                    cwd=cwd,
                    status="timed_out",
                    started=started,
                    stdout=stdout,
                    stderr=stderr,
                    error=f"timed out after {self.timeout_seconds}s",
                )
            try:
                stdout, stderr = process.communicate(
                    timeout=min(_PROCESS_POLL_SECONDS, remaining)
                )
                break
            except KeyboardInterrupt:
                stdout, stderr = _stop_process_tree(process, windows_job)
                return _stopped_result(
                    argv=argv,
                    resolved_argv=resolved_argv,
                    cwd=cwd,
                    status="cancelled",
                    started=started,
                    stdout=stdout,
                    stderr=stderr,
                    error="Validation cancelled by user",
                )
            except subprocess.TimeoutExpired:
                continue

        _close_windows_job(windows_job)
        return ValidationCommandResult(
            argv=argv,
            resolved_argv=resolved_argv,
            cwd=cwd,
            status="passed" if process.returncode == 0 else "failed",
            exit_code=process.returncode,
            duration_seconds=_elapsed(started),
            stdout=stdout,
            stderr=stderr,
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
        *,
        cancellation_token: CancellationToken | None = None,
        artifact_path: Path | None = None,
    ) -> tuple[ValidationArtifact, Path]:
        started_at = datetime.now(timezone.utc).isoformat()
        results: list[ValidationCommandResult] = []
        for command in commands:
            result = self.runner.execute(
                command,
                workspace_path,
                cancellation_token=cancellation_token,
            )
            results.append(result)
            if result.status == "cancelled":
                break
        cancelled = (
            cancellation_token is not None and cancellation_token.cancelled
        ) or any(result.status == "cancelled" for result in results)
        artifact = ValidationArtifact(
            run_id=run_id,
            status=(
                "cancelled"
                if cancelled
                else ("passed" if all(result.status == "passed" for result in results) else "failed")
            ),
            started_at=started_at,
            completed_at=datetime.now(timezone.utc).isoformat(),
            commands=results,
        )
        path = self.save(artifact, workspace_path, artifact_path=artifact_path)
        return artifact, path

    @staticmethod
    def save(
        artifact: ValidationArtifact,
        workspace_path: Path,
        *,
        artifact_path: Path | None = None,
    ) -> Path:
        run_directory = workspace_path.resolve().parent
        artifact_path = (artifact_path or run_directory / "validation.json").resolve()
        if artifact_path != run_directory / "validation.json":
            raise ValueError("Validation artifact must be stored at <run>/validation.json")
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


def _stopped_result(
    *,
    argv: list[str],
    resolved_argv: list[str],
    cwd: str,
    status: str,
    started: float,
    error: str,
    stdout: str = "",
    stderr: str = "",
) -> ValidationCommandResult:
    return ValidationCommandResult(
        argv=argv,
        resolved_argv=resolved_argv,
        cwd=cwd,
        status=status,
        exit_code=None,
        duration_seconds=_elapsed(started),
        stdout=stdout,
        stderr=stderr,
        error=error,
    )


def _stop_process_tree(
    process: subprocess.Popen[str],
    windows_job: int | None,
) -> tuple[str, str]:
    """Stop the validation process and descendants, then collect retained output."""

    if windows_job is not None:
        _terminate_windows_job(windows_job)
        _close_windows_job(windows_job)
    elif process.poll() is None:
        _signal_process_tree(process, force=False)
    try:
        stdout, stderr = process.communicate(timeout=_PROCESS_STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_process_tree(process, force=True)
        stdout, stderr = process.communicate()
    return stdout or "", stderr or ""


def _create_windows_job(process: subprocess.Popen[str]) -> int | None:
    """Assign a Windows process to a Job so descendants can be cancelled together."""

    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(process._handle)):
        kernel32.CloseHandle(job)
        return None
    return int(job)


def _terminate_windows_job(job: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject(wintypes.HANDLE(job), 1)


def _close_windows_job(job: int | None) -> None:
    if job is None or os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle(wintypes.HANDLE(job))


def _signal_process_tree(process: subprocess.Popen[str], *, force: bool) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=_PROCESS_STOP_GRACE_SECONDS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode != 0 and process.poll() is None:
                process.kill()
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        process.kill() if force else process.terminate()


def _output_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
