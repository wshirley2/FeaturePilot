"""Plan-driven orchestration for one controlled TechPilot Agent run."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from techpilot.engine.events import EventSink
from techpilot.engine.runtime_control import CancellationToken, RuntimeLimits

from ..domain import ExecutionScope, PlanRecord, Run
from ..execution import (
    ExecutionContext,
    ValidationArtifact,
    ValidationService,
    WorkspaceToolExecutor,
    build_techpilot_tools,
)
from ..runtime import RuntimeBootstrap, RuntimeBootstrapInput, TaskRuntime
from ..runtime.contracts import (
    RuntimeMode,
    RuntimeResultScope,
    RuntimeResultStatus,
    TaskRuntimePaths,
    TaskRuntimeResult,
)
from .changes import ChangeArtifact, ChangeService, RepositorySnapshot
from .planning import PlanStore
from .reporting import ReportService, RunMetrics, collect_run_metrics
from .run_events import ManagedRunEventSink, RunEventLog
from .workspace import Workspace, WorkspaceService


@dataclass(frozen=True)
class ManagedRunResult:
    """The retained state and final Agent response for a completed Managed Run."""

    record: PlanRecord | None
    scope: ExecutionScope
    run: Run
    workspace: Workspace
    runtime: TaskRuntime
    response: str
    validation: ValidationArtifact | None
    validation_path: Path | None
    events_path: Path
    changes: ChangeArtifact
    patch_path: Path
    report_path: Path
    metrics: RunMetrics
    runtime_result: TaskRuntimeResult


class ManagedRunExecutionError(RuntimeError):
    """An Agent/runtime failure whose Run and Workspace were retained."""

    def __init__(
        self,
        run: Run,
        workspace: Workspace,
        cause: Exception,
        *,
        events_path: Path | None = None,
        patch_path: Path | None = None,
        report_path: Path | None = None,
    ) -> None:
        super().__init__(f"Managed Run {run.display_id} failed: {cause}")
        self.run = run
        self.workspace = workspace
        self.cause = cause
        self.events_path = events_path
        self.patch_path = patch_path
        self.report_path = report_path


class _ControlledStopArtifactError(RuntimeError):
    """A cancelled or limited Run ended correctly but could not retain artifacts."""


@dataclass(frozen=True, slots=True)
class _GeneratedArtifacts:
    changes: ChangeArtifact
    patch_path: Path
    report_path: Path
    metrics: RunMetrics


class ManagedRunService:
    """Load, isolate, assemble, execute, and persist one approved Plan."""

    def __init__(
        self,
        *,
        plan_store: PlanStore,
        workspace_service: WorkspaceService,
        runtime_bootstrap: RuntimeBootstrap,
        event_sink: EventSink,
        validation_service: ValidationService | None = None,
        change_service: ChangeService | None = None,
        report_service: ReportService | None = None,
    ) -> None:
        self.plan_store = plan_store
        self.workspace_service = workspace_service
        self.runtime_bootstrap = runtime_bootstrap
        self.event_sink = event_sink
        self.validation_service = validation_service or ValidationService()
        self.change_service = change_service or ChangeService()
        self.report_service = report_service or ReportService()

    def execute(
        self,
        plan_reference: str,
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        limits: RuntimeLimits | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> ManagedRunResult:
        record = self.plan_store.load(plan_reference)
        if record.status != "approved":
            raise ValueError(
                f"Only approved plans can run; current status is {record.status!r}"
            )

        return self.execute_scope(
            ExecutionScope.from_plan(record),
            record=record,
            model=model,
            base_url=base_url,
            api_key=api_key,
            limits=limits,
            cancellation_token=cancellation_token,
        )

    def execute_scope(
        self,
        scope: ExecutionScope,
        *,
        record: PlanRecord | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        limits: RuntimeLimits | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> ManagedRunResult:
        """Execute an already approved Plan or Chat escalation scope in isolation."""

        started = time.monotonic()
        run, workspace = self.workspace_service.create_for_scope(scope)
        scope = scope.with_execution(run_id=run.id, workspace_path=workspace.path)
        paths = TaskRuntimePaths.for_runtime(RuntimeMode.MANAGED_RUN, workspace.path)
        event_log = RunEventLog.create_at(run.id, paths.events_path)
        events_path = event_log.path
        event_log.record("run_created", {
            "plan_reference": record.reference if record is not None else None,
            "plan_id": scope.plan_id,
            "task_id": scope.task.id,
            "execution_scope": scope.to_dict(),
            "workspace": str(workspace.path),
            "source_snapshot": workspace.source_snapshot,
            "runtime_paths": paths.to_dict(),
        })
        managed_sink = ManagedRunEventSink(event_log, self.event_sink)
        baseline = self.change_service.capture(workspace.source_path)
        context = ExecutionContext(scope=scope, run=run, workspace=workspace)
        executor = WorkspaceToolExecutor(context)
        run.transition("running")
        self.workspace_service.save_run(run, paths)
        event_log.record("run_started", {
            "workspace": str(workspace.path),
            "permission_mode": "approved Plan scope in an isolated Workspace",
            "runtime_limits": _runtime_limits_payload(limits),
        })

        runtime: TaskRuntime | None = None
        response = ""
        validation: ValidationArtifact | None = None
        validation_path: Path | None = None
        try:
            runtime = self.runtime_bootstrap.build(RuntimeBootstrapInput(
                repository=workspace.path,
                event_sink=managed_sink,
                model=model,
                base_url=base_url,
                api_key=api_key,
                tool_executor=executor,
                tools=build_techpilot_tools(),
                system_context=_managed_system_context(scope),
                permission_mode="approved execution scope in an isolated Workspace",
                mode=RuntimeMode.MANAGED_RUN,
                task_id=scope.task.id,
                run_id=run.id,
                source_repository=workspace.source_path,
                limits=limits,
                paths=paths,
            ))
            response = runtime.run_turn(
                _managed_task(scope),
                cancellation_token=cancellation_token,
            )
            runtime.ensure_persisted()
            if runtime.last_result is not None and runtime.last_result.status in {
                RuntimeResultStatus.CANCELLED,
                RuntimeResultStatus.LIMIT_REACHED,
            }:
                status = runtime.last_result.status.value
                result_payload = _control_stop_payload(runtime.last_result, response)
                artifacts = self._finalize_terminal_run(
                    scope=scope,
                    run=run,
                    workspace=workspace,
                    paths=paths,
                    baseline=baseline,
                    runtime=runtime,
                    response=response,
                    validation=None,
                    validation_path=None,
                    event_log=event_log,
                    events_path=events_path,
                    status=status,
                    result_payload=result_payload,
                    started=started,
                )
                _record_terminal_event(event_log, run.status, result_payload)
                self.workspace_service.save_run(run, paths)
                if artifacts is None:
                    raise _ControlledStopArtifactError(
                        "Could not generate artifacts for the controlled Runtime stop"
                    )
                return ManagedRunResult(
                    record=record,
                    scope=scope,
                    run=run,
                    workspace=workspace,
                    runtime=runtime,
                    response=response,
                    validation=None,
                    validation_path=None,
                    events_path=events_path,
                    changes=artifacts.changes,
                    patch_path=artifacts.patch_path,
                    report_path=artifacts.report_path,
                    metrics=artifacts.metrics,
                    runtime_result=runtime.last_result,
                )
            event_log.record("validation_started", {
                "commands": [list(command) for command in scope.validation_commands],
            })
            validation, validation_path = self.validation_service.validate(
                run.id,
                workspace.path,
                scope.validation_commands,
                cancellation_token=cancellation_token,
                artifact_path=paths.validation_path,
            )
            event_log.record("validation_completed", {
                "status": validation.status,
                "path": str(validation_path),
                "command_count": len(validation.commands),
            })
        except KeyboardInterrupt:
            result_payload: dict[str, object] = {
                "response": response,
                "error_type": "KeyboardInterrupt",
                "error": "Managed Run cancelled by user",
            }
            artifacts = self._finalize_terminal_run(
                scope=scope,
                run=run,
                workspace=workspace,
                paths=paths,
                baseline=baseline,
                runtime=runtime,
                response=response,
                validation=validation,
                validation_path=validation_path,
                event_log=event_log,
                events_path=events_path,
                status="cancelled",
                result_payload=result_payload,
                started=started,
            )
            _record_terminal_event(event_log, run.status, result_payload)
            self.workspace_service.save_run(run, paths)
            raise
        except _ControlledStopArtifactError as error:
            self.workspace_service.save_run(run, paths)
            raise ManagedRunExecutionError(
                run,
                workspace,
                error,
                events_path=events_path,
            ) from error
        except Exception as error:
            result_payload = {
                "response": response,
                "error_type": type(error).__name__,
                "error": str(error),
            }
            artifacts = self._finalize_terminal_run(
                scope=scope,
                run=run,
                workspace=workspace,
                paths=paths,
                baseline=baseline,
                runtime=runtime,
                response=response,
                validation=validation,
                validation_path=validation_path,
                event_log=event_log,
                events_path=events_path,
                status="failed",
                result_payload=result_payload,
                started=started,
            )
            _record_terminal_event(event_log, run.status, result_payload)
            self.workspace_service.save_run(run, paths)
            raise ManagedRunExecutionError(
                run,
                workspace,
                error,
                events_path=events_path,
                patch_path=artifacts.patch_path if artifacts else None,
                report_path=artifacts.report_path if artifacts else None,
            ) from error

        assert runtime is not None
        assert validation is not None
        assert validation_path is not None
        result_payload: dict[str, object] = {
            "response": response,
            "validation": {
                "status": validation.status,
                "path": str(validation_path),
            },
        }
        final_status = "succeeded"
        if validation.status == "cancelled":
            final_status = "cancelled"
            result_payload.update({
                "error_type": "ValidationCancelled",
                "error": _validation_cancellation_reason(validation, cancellation_token),
            })
        elif validation.status != "passed":
            final_status = "failed"
            result_payload.update({
                "error_type": "ValidationFailed",
                "error": "One or more approved validation commands did not pass",
            })
        try:
            artifacts = self._generate_artifacts(
                scope=scope,
                run=run,
                workspace=workspace,
                paths=paths,
                baseline=baseline,
                runtime=runtime,
                response=response,
                validation=validation,
                validation_path=validation_path,
                event_log=event_log,
                events_path=events_path,
                status=final_status,
                result_payload=result_payload,
                started=started,
            )
        except Exception as error:
            failure_result = {
                **result_payload,
                "error_type": "ArtifactGenerationError",
                "error": str(error),
            }
            _attach_runtime_result(
                runtime,
                status="failed",
                response=response,
                result=failure_result,
            )
            run.transition("failed", result=failure_result)
            _record_terminal_event(event_log, run.status, failure_result)
            self.workspace_service.save_run(run, paths)
            raise ManagedRunExecutionError(
                run,
                workspace,
                error,
                events_path=events_path,
            ) from error

        try:
            runtime_result = _attach_runtime_result(
                runtime,
                status=final_status,
                response=response,
                result=result_payload,
            )
            event_log.record("run_finished", _terminal_event_payload(final_status, result_payload))
        except Exception as error:
            failure_result = {
                **result_payload,
                "error_type": "EventLogError",
                "error": str(error),
            }
            _attach_runtime_result(
                runtime,
                status="failed",
                response=response,
                result=failure_result,
            )
            run.transition("failed", result=failure_result)
            self.workspace_service.save_run(run, paths)
            raise ManagedRunExecutionError(
                run,
                workspace,
                error,
                events_path=events_path,
                patch_path=artifacts.patch_path,
                report_path=artifacts.report_path,
            ) from error
        run.transition(final_status, result=result_payload)
        self.workspace_service.save_run(run, paths)
        return ManagedRunResult(
            record=record,
            scope=scope,
            run=run,
            workspace=workspace,
            runtime=runtime,
            response=response,
            validation=validation,
            validation_path=validation_path,
            events_path=events_path,
            changes=artifacts.changes,
            patch_path=artifacts.patch_path,
            report_path=artifacts.report_path,
            metrics=artifacts.metrics,
            runtime_result=runtime_result,
        )

    def _finalize_terminal_run(
        self,
        *,
        scope: ExecutionScope,
        run: Run,
        workspace: Workspace,
        paths: TaskRuntimePaths,
        baseline: RepositorySnapshot,
        runtime: TaskRuntime | None,
        response: str,
        validation: ValidationArtifact | None,
        validation_path: Path | None,
        event_log: RunEventLog,
        events_path: Path,
        status: str,
        result_payload: dict[str, object],
        started: float,
    ) -> _GeneratedArtifacts | None:
        try:
            artifacts = self._generate_artifacts(
                scope=scope,
                run=run,
                workspace=workspace,
                paths=paths,
                baseline=baseline,
                runtime=runtime,
                response=response,
                validation=validation,
                validation_path=validation_path,
                event_log=event_log,
                events_path=events_path,
                status=status,
                result_payload=result_payload,
                started=started,
            )
        except Exception as artifact_error:
            result_payload["artifact_error"] = {
                "error_type": type(artifact_error).__name__,
                "error": str(artifact_error),
            }
            artifacts = None
        _attach_runtime_result(
            runtime,
            status=status,
            response=response,
            result=result_payload,
        )
        run.transition(status, result=result_payload)
        return artifacts

    def _generate_artifacts(
        self,
        *,
        scope: ExecutionScope,
        run: Run,
        workspace: Workspace,
        paths: TaskRuntimePaths,
        baseline: RepositorySnapshot,
        runtime: TaskRuntime | None,
        response: str,
        validation: ValidationArtifact | None,
        validation_path: Path | None,
        event_log: RunEventLog,
        events_path: Path,
        status: str,
        result_payload: dict[str, object],
        started: float,
    ) -> _GeneratedArtifacts:
        changes, patch_path = self.change_service.generate(
            baseline,
            workspace,
            scope,
            output_path=paths.patch_path,
        )
        event_log.record("changes_generated", {
            "path": str(patch_path),
            "file_count": len(changes.files),
            "out_of_plan_files": list(changes.out_of_plan_files),
        })
        metrics = collect_run_metrics(runtime, time.monotonic() - started)
        preview_data = run.to_dict()
        preview_data.update({"status": status, "result": result_payload})
        preview_run = Run.from_dict(preview_data)
        report_path = self.report_service.generate(
            scope=scope,
            run=preview_run,
            workspace=workspace,
            response=response,
            validation=validation,
            validation_path=validation_path,
            events_path=events_path,
            changes=changes,
            patch_path=patch_path,
            metrics=metrics,
            session_path=runtime.session_path if runtime is not None else None,
            output_path=paths.report_path,
        )
        event_log.record("report_generated", {
            "path": str(report_path),
            "status": status,
        })
        result_payload.update({
            "changes": {**changes.to_dict(), "path": str(patch_path)},
            "metrics": metrics.to_dict(),
            "artifacts": {
                "events": str(events_path),
                "session": str(runtime.session_path) if runtime is not None else None,
                "patch": str(patch_path),
                "validation": str(validation_path) if validation_path else None,
                "report": str(report_path),
            },
        })
        return _GeneratedArtifacts(
            changes=changes,
            patch_path=patch_path,
            report_path=report_path,
            metrics=metrics,
        )


def _record_terminal_event(
    event_log: RunEventLog,
    status: str,
    result: dict[str, object],
) -> None:
    try:
        event_log.record("run_finished", _terminal_event_payload(status, result))
    except Exception as error:
        result["event_log_error"] = {
            "error_type": type(error).__name__,
            "error": str(error),
        }


def _terminal_event_payload(status: str, result: dict[str, object]) -> dict[str, object]:
    payload: dict[str, object] = {"status": status}
    for key in ("error_type", "error", "validation", "artifacts", "runtime_result"):
        if key in result:
            payload[key] = result[key]
    return payload


def _control_stop_payload(
    turn_result: TaskRuntimeResult,
    response: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "response": response,
        "error_type": (
            "RuntimeLimitExceeded"
            if turn_result.status is RuntimeResultStatus.LIMIT_REACHED
            else "RuntimeCancelled"
        ),
        "error": turn_result.reason or turn_result.status.value,
    }
    if turn_result.status is RuntimeResultStatus.LIMIT_REACHED:
        payload.update({
            "limit": turn_result.limit,
            "actual": turn_result.actual,
            "maximum": turn_result.maximum,
        })
    return payload


def _runtime_limits_payload(limits: RuntimeLimits | None) -> dict[str, int | float]:
    if limits is None:
        return {}
    names = (
        "max_provider_calls",
        "max_tool_rounds",
        "max_turn_seconds",
        "max_input_tokens",
        "max_total_tokens",
        "max_cost_usd",
    )
    return {
        name: value
        for name in names
        if (value := getattr(limits, name)) is not None
    }


def _validation_cancellation_reason(
    validation: ValidationArtifact,
    cancellation_token: CancellationToken | None,
) -> str:
    for command in reversed(validation.commands):
        if command.status == "cancelled" and command.error:
            return command.error
    if cancellation_token is not None and cancellation_token.cancelled:
        return cancellation_token.reason
    return "Validation cancelled"


def _attach_runtime_result(
    runtime: TaskRuntime | None,
    *,
    status: str,
    response: str,
    result: dict[str, object],
) -> TaskRuntimeResult:
    if status == "succeeded":
        runtime_result = TaskRuntimeResult(
            scope=RuntimeResultScope.RUN,
            status=RuntimeResultStatus.SUCCEEDED,
            response=response,
        )
    elif status == "cancelled":
        runtime_result = TaskRuntimeResult(
            scope=RuntimeResultScope.RUN,
            status=RuntimeResultStatus.CANCELLED,
            response=response,
            reason=str(result.get("error") or "Managed Run cancelled"),
        )
    elif status == "limit_reached":
        actual = result.get("actual")
        maximum = result.get("maximum")
        runtime_result = TaskRuntimeResult(
            scope=RuntimeResultScope.RUN,
            status=RuntimeResultStatus.LIMIT_REACHED,
            response=response,
            reason=str(result.get("error") or "Managed Run limit reached"),
            limit=str(result.get("limit") or "unknown"),
            actual=actual if isinstance(actual, (int, float)) and not isinstance(actual, bool) else None,
            maximum=(
                maximum
                if isinstance(maximum, (int, float)) and not isinstance(maximum, bool)
                else None
            ),
        )
    else:
        runtime_result = TaskRuntimeResult(
            scope=RuntimeResultScope.RUN,
            status=RuntimeResultStatus.FAILED,
            response=response,
            reason=str(result.get("error") or "Managed Run failed"),
            error_type=str(result.get("error_type") or "ManagedRunError"),
        )
    result["runtime_result"] = runtime_result.to_dict()
    if runtime is not None:
        runtime.record_result(runtime_result)
        runtime.ensure_persisted()
    return runtime_result


def _managed_system_context(scope: ExecutionScope) -> str:
    return "\n".join([
        "You are executing an approved TechPilot isolated run.",
        "Work only inside the isolated Workspace shown as the repository root.",
        "The injected tool executor enforces the approved execution scope; do not attempt to bypass it.",
        f"Execution scope: {scope.reference}",
    ])


def _managed_task(scope: ExecutionScope) -> str:
    acceptance = scope.task.acceptance_criteria

    def section(title: str, values: list[str]) -> list[str]:
        return [title, *(f"- {value}" for value in values)] if values else [title, "- (none)"]

    lines = [
        "Execute the approved implementation scope below in this isolated Workspace.",
        "Use tools to inspect and implement the task. Do not modify the source repository.",
        "You may read any file inside the Workspace, but writes are limited to the approved execution scope.",
        "",
        f"Task: {scope.task.description}",
        f"Scope summary: {scope.summary}",
        f"Trigger: {scope.trigger_reason or 'explicit approval'}",
        f"Original Tool Call: {scope.trigger_tool_name or '(Plan scope)'} {scope.trigger_arguments}",
        "",
        *section("Steps:", list(scope.steps)),
        "",
        *section("Approved files to modify:", list(scope.modify_files)),
        "",
        *section("Approved files that may be created:", list(scope.expected_files)),
        "",
        *section("Acceptance criteria:", acceptance),
        "",
        *section("Approved validation commands:", [" ".join(command) for command in scope.validation_commands]),
        "",
        "TechPilot will execute every approved validation command after this Agent turn.",
        "Do not invoke validation commands yourself; finish the implementation and return your summary.",
        "",
        "When implementation is complete, return a concise summary of the work performed.",
    ]
    return "\n".join(lines)
