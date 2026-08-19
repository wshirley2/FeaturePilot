"""Natural-language Plan review and explicit approval session."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from .domain import PlanRecord, Task
from .managed import ManagedRunExecutionError, ManagedRunResult, ManagedRunService
from .planning import PlanningService, PlanStore, PlanValidationError
from .workspace import WorkspaceCreationError

InputFunction = Callable[[str], str]

_APPROVE = {"批准", "同意", "批准计划", "批准这个计划", "approve"}
_APPROVE_AND_RUN = {"批准并执行", "同意并执行", "批准并开始", "approve and run"}
_RUN = {"执行", "开始执行", "运行", "run"}
_REJECT = {"拒绝", "拒绝计划", "reject"}
_EXIT = {"退出", "取消", "exit", "quit", "/exit", "/quit"}
_SHOW = {"查看计划", "显示计划", "计划", "show"}


@dataclass(frozen=True)
class PlanTurnResult:
    """Tell a standalone or embedded terminal loop what happened this turn."""

    action: str = "continue"
    exit_code: int = 0


class PlanChatSession:
    """Drive one Plan from natural-language task to explicit execution approval."""

    def __init__(
        self,
        repository: Path,
        *,
        planning_service: PlanningService,
        plan_store: PlanStore,
        managed_service: ManagedRunService,
        console: Console | None = None,
        input_fn: InputFunction = input,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.repository = repository.resolve()
        self.planning_service = planning_service
        self.plan_store = plan_store
        self.managed_service = managed_service
        self.console = console or Console()
        self.input_fn = input_fn
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.record: PlanRecord | None = None
        self.last_result: ManagedRunResult | None = None

    def run(self) -> int:
        if not self.repository.is_dir():
            raise ValueError(f"Repository directory does not exist: {self.repository}")
        self.console.print(Panel(
            "用自然语言描述任务。生成草稿后，可以回复“批准并执行”，"
            "或输入一段新的完整任务描述来生成修订版本。\n"
            "只有明确批准后，FeaturePilot 才会创建 Workspace 并执行。",
            title="FeaturePilot Plan Chat",
        ))

        while True:
            try:
                user_input = self.input_fn("You > ").strip()
            except EOFError:
                self.console.print("\n已退出 Plan Chat。")
                return 0
            except KeyboardInterrupt:
                self.console.print("\n已退出 Plan Chat。")
                return 0

            if not user_input:
                continue
            outcome = self.handle(user_input)
            if outcome.action != "continue":
                return outcome.exit_code

    def reset(self) -> None:
        """Start a fresh Plan conversation while keeping configured services."""

        self.record = None
        self.last_result = None

    def handle(self, user_input: str) -> PlanTurnResult:
        """Handle one Plan-mode input without owning the surrounding terminal loop."""

        normalized = _normalize_intent(user_input)
        if normalized in _EXIT:
            self.console.print("已退出 Plan Chat。")
            return PlanTurnResult("exit")
        if self.record is None:
            self._create_initial_draft(user_input)
            return PlanTurnResult()
        if normalized in _SHOW:
            self._show_plan(self.record)
            return PlanTurnResult()
        if normalized in _REJECT:
            self.record = self.plan_store.reject(
                self.record.reference,
                "用户在 Plan Chat 中明确拒绝",
            )
            self.console.print(f"[yellow]已拒绝计划 {self.record.reference}，未执行。[/yellow]")
            return PlanTurnResult("completed")
        if normalized in _APPROVE_AND_RUN:
            if self.record.status == "draft":
                self.record = self.plan_store.approve(self.record.reference)
            return self._execute_approved()
        if normalized in _APPROVE:
            if self.record.status != "draft":
                self.console.print(f"计划当前状态为 {self.record.status}。")
                return PlanTurnResult()
            self.record = self.plan_store.approve(self.record.reference)
            self.console.print(
                f"[green]已批准 {self.record.reference}。[/green] 回复“执行”即可启动隔离运行。"
            )
            return PlanTurnResult()
        if normalized in _RUN:
            if self.record.status != "approved":
                self.console.print("[yellow]计划尚未批准。请明确回复“批准并执行”。[/yellow]")
                return PlanTurnResult()
            return self._execute_approved()
        if self.record.status != "draft":
            self.console.print("已批准的计划不能再修改；请回复“执行”或“退出”。")
            return PlanTurnResult()
        self._revise_draft(user_input)
        return PlanTurnResult()

    def _create_initial_draft(self, description: str) -> None:
        task = Task(project_id=str(self.repository), description=description)
        try:
            self.record = self.planning_service.create_draft(self.repository, task)
        except PlanValidationError as error:
            self.console.print("[red]计划校验失败：[/red]")
            self.console.print("\n".join(f"- {item}" for item in error.result.errors))
            return
        self._show_plan(self.record)

    def _revise_draft(self, description: str) -> None:
        assert self.record is not None
        source_task = self.record.task or Task(
            project_id=self.record.repository,
            description=self.record.plan.summary,
            id=self.record.plan.task_id,
        )
        revised_task = Task(
            project_id=str(self.repository),
            description=description,
            task_type=source_task.task_type,
            acceptance_criteria=source_task.acceptance_criteria,
            id=source_task.id,
        )
        try:
            self.record = self.planning_service.create_draft(
                self.repository,
                revised_task,
                name=self.record.name or None,
            )
        except PlanValidationError as error:
            self.console.print("[red]修订计划校验失败：[/red]")
            self.console.print("\n".join(f"- {item}" for item in error.result.errors))
            return
        self.console.print("[cyan]已将输入作为新的完整任务描述，并生成修订版本。[/cyan]")
        self._show_plan(self.record)

    def _execute_approved(self) -> PlanTurnResult:
        assert self.record is not None
        self.console.print(f"[green]已批准 {self.record.reference}，开始隔离执行。[/green]")
        try:
            self.last_result = self.managed_service.execute(
                self.record.reference,
                model=self.model,
                base_url=self.base_url,
                api_key=self.api_key,
            )
        except WorkspaceCreationError as error:
            self.console.print("[red]Managed Run 未启动：无法创建隔离 Workspace。[/red]")
            self.console.print(str(error))
            self.console.print("原仓库未修改。修复环境问题后可再次回复“执行”。")
            return PlanTurnResult()
        except KeyboardInterrupt:
            self.console.print("[yellow]Managed Run 已取消，Workspace 已保留。[/yellow]")
            return PlanTurnResult("completed", 130)
        except ManagedRunExecutionError as error:
            self.console.print(f"[red]{error}[/red]")
            self.console.print(f"Workspace 已保留：{error.workspace.path}")
            return PlanTurnResult("completed", 1)

        assert self.last_result is not None
        sink = self.last_result.runtime.agent.event_sink
        if not getattr(sink, "last_turn_streamed", False) and self.last_result.response:
            self.console.print(self.last_result.response)
        self.console.print("[green]Managed Run 执行完成。[/green]")
        self.console.print(f"Run: {self.last_result.run.display_id}")
        self.console.print(f"Workspace: {self.last_result.workspace.path}")
        return PlanTurnResult("completed")

    def _show_plan(self, record: PlanRecord) -> None:
        plan = record.plan

        def values(items: list[str]) -> str:
            return "\n".join(f"- {item}" for item in items) or "- （无）"

        commands = [" ".join(command) for command in plan.validation_commands]
        body = "\n".join([
            f"状态：{record.status}",
            f"仓库：{record.repository}",
            f"任务：{plan.summary}",
            "",
            "读取：",
            values(plan.read_files),
            "",
            "修改：",
            values(plan.modify_files),
            "",
            "验证：",
            values(commands),
        ])
        self.console.print(Panel(body, title=f"Plan {record.reference}"))
        self.console.print("回复“批准并执行”，或输入新的完整任务描述生成修订版本。")


def _normalize_intent(value: str) -> str:
    """Normalize harmless sentence punctuation without weakening explicit approval."""

    return value.strip().casefold().rstrip("。.!！")
