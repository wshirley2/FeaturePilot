"""Plan-driven orchestration for one controlled FeaturePilot Agent run."""

from __future__ import annotations

from dataclasses import dataclass

from corecoder.events import EventSink

from .domain import PlanRecord, Run
from .execution import ExecutionContext, WorkspaceToolExecutor, build_featurepilot_tools
from .planning import PlanStore
from .runtime import ChatRuntime, RuntimeBootstrap, RuntimeBootstrapInput
from .workspace import Workspace, WorkspaceService


@dataclass(frozen=True)
class ManagedRunResult:
    """The retained state and final Agent response for a completed Managed Run."""

    record: PlanRecord
    run: Run
    workspace: Workspace
    runtime: ChatRuntime
    response: str


class ManagedRunExecutionError(RuntimeError):
    """An Agent/runtime failure whose Run and Workspace were retained."""

    def __init__(self, run: Run, workspace: Workspace, cause: Exception) -> None:
        super().__init__(f"Managed Run {run.display_id} failed: {cause}")
        self.run = run
        self.workspace = workspace
        self.cause = cause


class ManagedRunService:
    """Load, isolate, assemble, execute, and persist one approved Plan."""

    def __init__(
        self,
        *,
        plan_store: PlanStore,
        workspace_service: WorkspaceService,
        runtime_bootstrap: RuntimeBootstrap,
        event_sink: EventSink,
    ) -> None:
        self.plan_store = plan_store
        self.workspace_service = workspace_service
        self.runtime_bootstrap = runtime_bootstrap
        self.event_sink = event_sink

    def execute(
        self,
        plan_reference: str,
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> ManagedRunResult:
        record = self.plan_store.load(plan_reference)
        if record.status != "approved":
            raise ValueError(
                f"Only approved plans can run; current status is {record.status!r}"
            )

        run, workspace = self.workspace_service.create_for_plan(record)
        context = ExecutionContext(record=record, run=run, workspace=workspace)
        executor = WorkspaceToolExecutor(context)
        run.transition("running")
        self.workspace_service.save_run(run)

        try:
            runtime = self.runtime_bootstrap.build(RuntimeBootstrapInput(
                repository=workspace.path,
                event_sink=self.event_sink,
                model=model,
                base_url=base_url,
                api_key=api_key,
                tool_executor=executor,
                tools=build_featurepilot_tools(),
                system_context=_managed_system_context(record),
                permission_mode="approved Plan scope in an isolated Workspace",
            ))
            response = runtime.agent.chat(_managed_task(record))
        except KeyboardInterrupt:
            run.transition("cancelled", result={
                "error_type": "KeyboardInterrupt",
                "error": "Managed Run cancelled by user",
            })
            self.workspace_service.save_run(run)
            raise
        except Exception as error:
            run.transition("failed", result={
                "error_type": type(error).__name__,
                "error": str(error),
            })
            self.workspace_service.save_run(run)
            raise ManagedRunExecutionError(run, workspace, error) from error

        run.transition("succeeded", result={"response": response})
        self.workspace_service.save_run(run)
        return ManagedRunResult(
            record=record,
            run=run,
            workspace=workspace,
            runtime=runtime,
            response=response,
        )


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
        "When implementation is complete, return a concise summary of the work performed.",
    ]
    return "\n".join(lines)
