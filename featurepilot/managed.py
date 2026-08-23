"""Plan-driven orchestration for one controlled FeaturePilot Agent run."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from corecoder.events import EventSink

from .changes import ChangeArtifact, ChangeService, RepositorySnapshot
from .domain import PlanRecord, Run
from .execution import (
    ExecutionContext,
    ValidationArtifact,
    ValidationService,
    WorkspaceToolExecutor,
    build_featurepilot_tools,
)
from .planning import PlanStore
from .reporting import ReportService, RunMetrics, collect_run_metrics
from .run_events import ManagedRunEventSink, RunEventLog
from .runtime import RuntimeBootstrap, RuntimeBootstrapInput, TaskRuntime
from .runtime_contracts import RuntimeMode
from .workspace import Workspace, WorkspaceService


@dataclass(frozen=True)
class ManagedRunResult:
    """The retained state and final Agent response for a completed Managed Run."""

    record: PlanRecord
    run: Run
    workspace: Workspace
    runtime: TaskRuntime
    response: str
    validation: ValidationArtifact
    validation_path: Path
    events_path: Path
    changes: ChangeArtifact
    patch_path: Path
    report_path: Path
    metrics: RunMetrics


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
    ) -> ManagedRunResult:
        started = time.monotonic()
        record = self.plan_store.load(plan_reference)
        if record.status != "approved":
            raise ValueError(
                f"Only approved plans can run; current status is {record.status!r}"
            )

        run, workspace = self.workspace_service.create_for_plan(record)
        event_log = RunEventLog.create(run.id, workspace.path.parent)
        events_path = event_log.path
        event_log.record("run_created", {
            "plan_reference": record.reference,
            "plan_id": record.id,
            "task_id": record.plan.task_id,
            "workspace": str(workspace.path),
            "source_snapshot": workspace.source_snapshot,
        })
        managed_sink = ManagedRunEventSink(event_log, self.event_sink)
        baseline = self.change_service.capture(workspace.source_path)
        context = ExecutionContext(record=record, run=run, workspace=workspace)
        executor = WorkspaceToolExecutor(context)
        run.transition("running")
        self.workspace_service.save_run(run)
        event_log.record("run_started", {
            "workspace": str(workspace.path),
            "permission_mode": "approved Plan scope in an isolated Workspace",
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
                tools=build_featurepilot_tools(),
                system_context=_managed_system_context(record),
                permission_mode="approved Plan scope in an isolated Workspace",
                mode=RuntimeMode.MANAGED_RUN,
                task_id=record.plan.task_id,
                run_id=run.id,
                source_repository=workspace.source_path,
            ))
            response = runtime.agent.chat(_managed_task(record))
            managed_sink.ensure_persisted()
            event_log.record("validation_started", {
                "commands": [list(command) for command in record.plan.validation_commands],
            })
            validation, validation_path = self.validation_service.validate(
                run.id,
                workspace.path,
                record.plan.validation_commands,
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
                record=record,
                run=run,
                workspace=workspace,
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
            self.workspace_service.save_run(run)
            raise
        except Exception as error:
            result_payload = {
                "response": response,
                "error_type": type(error).__name__,
                "error": str(error),
            }
            artifacts = self._finalize_terminal_run(
                record=record,
                run=run,
                workspace=workspace,
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
            self.workspace_service.save_run(run)
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
        if validation.status != "passed":
            final_status = "failed"
            result_payload.update({
                "error_type": "ValidationFailed",
                "error": "One or more approved validation commands did not pass",
            })
        try:
            artifacts = self._generate_artifacts(
                record=record,
                run=run,
                workspace=workspace,
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
            run.transition("failed", result=failure_result)
            _record_terminal_event(event_log, run.status, failure_result)
            self.workspace_service.save_run(run)
            raise ManagedRunExecutionError(
                run,
                workspace,
                error,
                events_path=events_path,
            ) from error

        try:
            event_log.record("run_finished", _terminal_event_payload(final_status, result_payload))
        except Exception as error:
            failure_result = {
                **result_payload,
                "error_type": "EventLogError",
                "error": str(error),
            }
            run.transition("failed", result=failure_result)
            self.workspace_service.save_run(run)
            raise ManagedRunExecutionError(
                run,
                workspace,
                error,
                events_path=events_path,
                patch_path=artifacts.patch_path,
                report_path=artifacts.report_path,
            ) from error
        run.transition(final_status, result=result_payload)
        self.workspace_service.save_run(run)
        return ManagedRunResult(
            record=record,
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
        )

    def _finalize_terminal_run(
        self,
        *,
        record: PlanRecord,
        run: Run,
        workspace: Workspace,
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
                record=record,
                run=run,
                workspace=workspace,
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
        run.transition(status, result=result_payload)
        return artifacts

    def _generate_artifacts(
        self,
        *,
        record: PlanRecord,
        run: Run,
        workspace: Workspace,
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
        changes, patch_path = self.change_service.generate(baseline, workspace, record)
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
            record=record,
            run=preview_run,
            workspace=workspace,
            response=response,
            validation=validation,
            validation_path=validation_path,
            events_path=events_path,
            changes=changes,
            patch_path=patch_path,
            metrics=metrics,
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
    for key in ("error_type", "error", "validation", "artifacts"):
        if key in result:
            payload[key] = result[key]
    return payload


def _managed_system_context(record: PlanRecord) -> str:
    return "\n".join([
        "You are executing an approved FeaturePilot Managed Run.",
        "Work only inside the isolated Workspace shown as the repository root.",
        "The injected tool executor enforces the approved Plan; do not attempt to bypass it.",
        f"Approved Plan: {record.reference}",
    ])


def _managed_task(record: PlanRecord) -> str:
    plan = record.plan
    acceptance = record.task.acceptance_criteria if record.task else []

    def section(title: str, values: list[str]) -> list[str]:
        return [title, *(f"- {value}" for value in values)] if values else [title, "- (none)"]

    lines = [
        "Execute the approved implementation Plan below in this isolated Workspace.",
        "Use tools to inspect and implement the task. Do not modify the source repository.",
        "You may read any file inside the Workspace, but writes are limited to the approved Plan scope.",
        "",
        f"Task: {record.task.description if record.task else plan.summary}",
        f"Plan summary: {plan.summary}",
        "",
        *section("Steps:", plan.steps),
        "",
        *section("Plan files to inspect first:", plan.read_files),
        "",
        *section("Approved files to modify:", plan.modify_files),
        "",
        *section("Approved files that may be created:", plan.expected_files),
        "",
        *section("Acceptance criteria:", acceptance),
        "",
        *section("Approved validation commands:", [" ".join(command) for command in plan.validation_commands]),
        "",
        "FeaturePilot will execute every approved validation command after this Agent turn.",
        "Do not invoke validation commands yourself; finish the implementation and return your summary.",
        "",
        "When implementation is complete, return a concise summary of the work performed.",
    ]
    return "\n".join(lines)
