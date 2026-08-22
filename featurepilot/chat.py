"""Minimal event-driven terminal Chat for FeaturePilot C2."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from corecoder.context import estimate_tokens
from corecoder.events import RuntimeEvent, RuntimeEventType
from corecoder.tools.edit import _changed_files

from .runtime import ChatRuntime

if TYPE_CHECKING:
    from .plan_chat import PlanChatSession

InputFunction = Callable[[str], str]


class SlashCommandCompleter(Completer):
    """Offer local Chat commands while the user is typing a slash command."""

    _COMMANDS = (
        ("/help", "显示命令帮助"),
        ("/status", "显示会话与上下文摘要"),
        ("/tools", "查看可用工具"),
        ("/files", "查看本次进程修改的文件"),
        ("/diff", "查看当前 Git 差异"),
        ("/tokens", "查看 Token 用量与估算费用"),
        ("/compact", "压缩模型上下文"),
        ("/save", "查看自动保存的 Session"),
        ("/sessions", "列出可恢复的 Session"),
        ("/session", "查看指定 Session"),
        ("/resume", "恢复指定 Session"),
        ("/details", "切换工具结果详情显示"),
        ("/model", "查看或切换模型"),
        ("/clear", "清空当前对话消息"),
        ("/exit", "退出 Chat"),
        ("/quit", "退出 Chat"),
    )

    def __init__(self, *, include_plan: bool = False):
        self.include_plan = include_plan

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text or "\t" in text:
            return
        commands = self._COMMANDS
        if self.include_plan:
            commands = commands + (("/plan", "进入 Plan 模式"),)
        for command, description in commands:
            if command.startswith(text):
                yield Completion(
                    command,
                    start_position=-len(text),
                    display=command,
                    display_meta=description,
                )


class TerminalEventSink:
    """Render Runtime Events without understanding or duplicating AgentLoop."""

    def __init__(self, console: Console | None = None, *, result_limit: int = 800):
        self.console = console or Console()
        self.result_limit = result_limit
        self._tool_started: dict[str, tuple[float, str]] = {}
        self._streamed = False
        self.last_turn_streamed = False
        self.show_full_results = False

    def emit(self, event: RuntimeEvent) -> None:
        if event.event_type is RuntimeEventType.TURN_STARTED:
            self._streamed = False
            self.last_turn_streamed = False
        elif event.event_type is RuntimeEventType.ASSISTANT_TOKEN:
            self.console.print(event.payload.get("token", ""), end="", markup=False, highlight=False)
            self._streamed = True
            self.last_turn_streamed = True
        elif event.event_type is RuntimeEventType.TOOL_REQUESTED:
            call_id = event.tool_call_id or event.event_id
            name = event.payload.get("tool_name", "unknown")
            self._tool_started[call_id] = (time.monotonic(), str(name))
            arguments = _brief_arguments(event.payload.get("arguments", {}))
            prefix = "\n" if self._streamed else ""
            self.console.print(f"{prefix}[dim]→ {name}({arguments})[/dim]")
            self._streamed = False
        elif event.event_type is RuntimeEventType.TOOL_COMPLETED:
            call_id = event.tool_call_id or event.event_id
            started = self._tool_started.pop(call_id, None)
            elapsed = f" in {time.monotonic() - started[0]:.2f}s" if started is not None else ""
            tool_name = str(event.payload.get("tool_name") or (started[1] if started else "tool"))
            result = str(event.payload.get("result", ""))
            status = "interrupted" if event.payload.get("interrupted") else _tool_status(result)
            style = "yellow" if status in {"interrupted", "not executed"} else ("red" if status in {"error", "denied"} else "green")
            self.console.print(f"[{style}]← {tool_name}: {status}{elapsed}[/{style}]")
            if result:
                if status == "not executed":
                    self.console.print("[dim]未执行：达到运行限制。[/dim]")
                elif status in {"error", "denied", "interrupted"} or self.show_full_results or tool_name != "read_file":
                    self.console.print(f"[dim]{_summarize(result, self.result_limit)}[/dim]")
                else:
                    self.console.print(
                        f"[dim]已读取 {len(result)} 个字符；完整内容已折叠（/details on 查看后续完整结果）。[/dim]"
                    )
        elif event.event_type is RuntimeEventType.CONTEXT_COMPRESSED:
            self.console.print("[dim]Context compressed.[/dim]")
        elif event.event_type is RuntimeEventType.TURN_COMPLETED and self._streamed:
            self.console.print()
            self._streamed = False
        elif event.event_type is RuntimeEventType.TURN_FAILED:
            self.console.print(f"[red]Turn failed: {event.payload.get('error', 'unknown error')}[/red]")
        elif event.event_type is RuntimeEventType.TURN_INTERRUPTED:
            self.console.print("[yellow]Current turn interrupted.[/yellow]")
        elif event.event_type is RuntimeEventType.TURN_LIMIT_REACHED:
            limit = event.payload.get("limit") or "tool-call rounds"
            self.console.print(f"[yellow]Runtime limit reached: {limit}.[/yellow]")


class ChatSession:
    """Own terminal input and commands while Agent owns the conversation loop."""

    def __init__(
        self,
        runtime: ChatRuntime,
        *,
        console: Console | None = None,
        input_fn: InputFunction | None = None,
        plan_session: PlanChatSession | None = None,
    ):
        self.runtime = runtime
        self.console = console or Console()
        self.input_fn = input_fn
        self.plan_session = plan_session
        self._plan_mode = False
        self._prompt_session: PromptSession[str] | None = None

    def run(self) -> int:
        self.show_startup()
        while True:
            try:
                prompt = "Plan > " if self._plan_mode else "You > "
                user_input = self._read_input(prompt).strip()
            except EOFError:
                self.console.print("\nBye!")
                return 0
            except KeyboardInterrupt:
                self.console.print("\nBye!")
                return 0

            if not user_input:
                continue
            if user_input.lower() in {"exit", "quit", "/exit", "/quit"}:
                self.console.print("Bye!")
                return 0
            if self._plan_mode:
                if _is_return_to_chat(user_input):
                    self._leave_plan_mode()
                    continue
                assert self.plan_session is not None
                outcome = self.plan_session.handle(user_input)
                if outcome.action in {"completed", "exit"}:
                    self._remember_managed_run()
                    self._leave_plan_mode()
                continue
            matched, plan_task = _extract_plan_request(user_input)
            if self.plan_session is not None and matched:
                self._enter_plan_mode()
                if plan_task:
                    outcome = self.plan_session.handle(plan_task)
                    if outcome.action in {"completed", "exit"}:
                        self._remember_managed_run()
                        self._leave_plan_mode()
                continue
            if user_input.startswith("/"):
                self._handle_command(user_input)
                continue

            try:
                response = self.runtime.agent.chat(user_input)
                # Providers used by embedders may not stream token callbacks.
                sink = self.runtime.agent.event_sink
                if not getattr(sink, "last_turn_streamed", False) and response:
                    self.console.print(Markdown(response))
            except KeyboardInterrupt:
                self.console.print("[yellow]Turn cancelled. The chat session is still active.[/yellow]")
            except Exception as error:
                self.console.print(f"[red]Error: {error}[/red]")
            finally:
                self._ensure_session_persisted()

    def _read_input(self, prompt: str) -> str:
        if self.input_fn is not None:
            return self.input_fn(prompt)
        if self._prompt_session is None:
            self._prompt_session = PromptSession()
        return self._prompt_session.prompt(
            prompt,
            completer=SlashCommandCompleter(include_plan=self.plan_session is not None),
            complete_while_typing=True,
        )

    def show_startup(self) -> None:
        profile = self.runtime.profile
        profile_lines = [f"Language: {profile.language}"] if profile else []
        if profile:
            profile_lines.extend([
                f"Frameworks: {', '.join(profile.frameworks) or '(none detected)'}",
                f"Entrypoints: {', '.join(profile.entrypoints[:5]) or '(none detected)'}",
                f"Tests: {', '.join(profile.test_files[:5]) or '(none detected)'}",
                "Validation: " + (", ".join(" ".join(c) for c in profile.validation_commands) or "(none detected)"),
            ])
        if self.runtime.profile_warning:
            profile_lines.append(f"Profile warning: {self.runtime.profile_warning}")
        body = "\n".join([
            "[bold]FeaturePilot Chat[/bold]",
            f"Repository: [cyan]{self.runtime.repository}[/cyan]",
            f"Model: [cyan]{self.runtime.config.model}[/cyan]",
            f"Permission: [yellow]{self.runtime.permission_mode}[/yellow]",
            f"Session: [dim]{self.runtime.agent.session_id[:8]}[/dim]",
            f"Tools: {', '.join(tool.name for tool in self.runtime.tools)}",
            *profile_lines,
            *(
                [
                    "Plan：输入“先制定计划：<任务>”进入 Plan 模式；明确回复“批准并执行”后才会创建隔离 Workspace。"
                ]
                if self.plan_session is not None
                else []
            ),
            "Type /help for commands; Ctrl+C cancels a turn; /exit exits Chat.",
        ])
        self.console.print(Panel(body, border_style="blue"))

    def _handle_command(self, command_line: str) -> None:
        command, _, argument = command_line.partition(" ")
        handlers = {
            "/help": self._show_help,
            "/status": self._show_status,
            "/tools": self._show_tools,
            "/files": self._show_files,
            "/diff": self._show_diff,
            "/tokens": self._show_tokens,
            "/compact": self._compact,
            "/save": self._show_save,
            "/sessions": self._show_sessions,
            "/session": lambda: self._session_command(argument.strip()),
            "/resume": lambda: self._resume(argument.strip()),
            "/details": lambda: self._details(argument.strip()),
            "/model": lambda: self._model(argument.strip()),
            "/clear": self._clear,
        }
        handler = handlers.get(command.lower())
        if handler is None:
            self.console.print(f"[yellow]Unknown command: {command} (try /help)[/yellow]")
        else:
            handler()

    def _show_help(self) -> None:
        lines = [
            "/status  Runtime and repository summary",
            "/tools   Available tools",
            "/files   Files changed in this process",
            "/diff    Current Git working-tree diff",
            "/tokens  Provider token usage and estimated cost",
            "/compact Compress model context",
            "/model [name]  Show or switch model",
            "/clear   Clear conversation messages",
        ]
        if self.plan_session is not None:
            lines.append("/plan    进入 Plan 模式；也可以说“先制定计划：<任务>”")
        lines.extend([
            "/save    Show the active auto-saved event Session",
            "/sessions  List recoverable Sessions",
            "/session show [id]  Show Session facts and recovery warnings",
            "/resume <id>  Resume an event Session for this repository",
            "/details [on|off]  Toggle full future Tool Result output",
            "/exit    Exit Chat",
        ])
        self.console.print(Panel("\n".join(lines), title="FeaturePilot Commands"))

    def _show_status(self) -> None:
        """Show the small set of live facts most useful during a Chat turn."""

        agent = self.runtime.agent
        llm = agent.llm
        prompt = getattr(llm, "total_prompt_tokens", 0)
        completion = getattr(llm, "total_completion_tokens", 0)
        cost = getattr(llm, "estimated_cost", None)
        if callable(cost):
            cost = cost()
        cost_text = f"${cost:.6f}" if isinstance(cost, (int, float)) else "unknown"
        current_tokens = estimate_tokens(agent.messages)
        max_context = getattr(agent.context, "max_tokens", None)
        context_text = str(current_tokens)
        if max_context:
            remaining = max(0, max_context - current_tokens)
            context_text = f"{current_tokens:,} used / {remaining:,} remaining (max {max_context:,})"

        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("Field", style="cyan")
        table.add_column("Value")
        table.add_row("Session ID", agent.session_id)
        table.add_row("Model", getattr(self.runtime.config, "model", getattr(llm, "model", "unknown")))
        table.add_row("Context", context_text)
        table.add_row("Usage", f"{prompt} prompt + {completion} completion = {prompt + completion} total")
        table.add_row("Estimated cost", cost_text)
        self.console.print(table)

    def _show_tools(self) -> None:
        for tool in self.runtime.tools:
            self.console.print(f"[cyan]{tool.name}[/cyan]  {tool.description}")

    def _show_files(self) -> None:
        files = self._repository_changed_files()
        if not files:
            self.console.print("[dim]No files changed in this process.[/dim]")
            return
        for file_path in files:
            self.console.print(file_path)

    def _show_diff(self) -> None:
        try:
            root_result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=self.runtime.repository,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            self.console.print(f"[red]Could not read Git diff: {error}[/red]")
            return
        repository = self.runtime.repository.resolve()
        git_root = Path(root_result.stdout.strip()).resolve() if root_result.returncode == 0 else None
        if git_root != repository:
            self.console.print(
                "[yellow]当前目录不是独立 Git 仓库，已避免展示父级仓库的变更。"
                "可使用 /files 查看本次会话修改的文件。[/yellow]"
            )
            return
        try:
            result = subprocess.run(
                ["git", "diff", "--no-ext-diff"],
                cwd=repository,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            self.console.print(f"[red]Could not read Git diff: {error}[/red]")
            return
        output = result.stdout or result.stderr
        if output.strip():
            self.console.print(output, markup=False)
        else:
            self.console.print("[dim]No Git diff.[/dim]")

    def _show_tokens(self) -> None:
        llm = self.runtime.agent.llm
        prompt = getattr(llm, "total_prompt_tokens", 0)
        completion = getattr(llm, "total_completion_tokens", 0)
        line = f"Tokens: {prompt} prompt + {completion} completion = {prompt + completion} total"
        cost = getattr(llm, "estimated_cost", None)
        if cost is not None:
            line += f" (~${cost:.4f})"
        self.console.print(line)

    def _compact(self) -> None:
        compact = getattr(self.runtime.agent, "compact_context", None)
        if compact is not None:
            compressed, before, after = compact()
        else:
            before = estimate_tokens(self.runtime.agent.messages)
            compressed = self.runtime.agent.context.maybe_compress(
                self.runtime.agent.messages,
                self.runtime.agent.llm,
            )
            after = estimate_tokens(self.runtime.agent.messages)
        message = f"Compressed: {before} → {after} tokens" if compressed else f"Nothing to compress ({before} tokens)"
        self.console.print(message)

    def _show_save(self) -> None:
        store = getattr(self.runtime, "session_store", None)
        if store is None:
            self.console.print("[yellow]Event Session storage is unavailable for this runtime.[/yellow]")
            return
        self._ensure_session_persisted()
        self.console.print(
            f"[green]自动保存已开启。[/green] Session: {self.runtime.agent.session_id}\n"
            f"Events: {store.directory / (self.runtime.agent.session_id + '.jsonl')}"
        )

    def _show_sessions(self) -> None:
        store = getattr(self.runtime, "session_store", None)
        if store is None:
            self.console.print("[yellow]Event Session storage is unavailable for this runtime.[/yellow]")
            return
        items = store.list()
        if not items:
            self.console.print("[dim]No saved Sessions.[/dim]")
            return
        table = Table(title="FeaturePilot Sessions")
        table.add_column("Session")
        table.add_column("Status")
        table.add_column("Model")
        table.add_column("Events", justify="right")
        table.add_column("Warnings", justify="right")
        for item in items:
            table.add_row(
                item.session_id,
                item.status,
                item.model or "?",
                str(len(item.events)),
                str(len(item.warnings)),
            )
        self.console.print(table)

    def _session_command(self, argument: str) -> None:
        values = argument.split()
        if not values or values[0].lower() != "show":
            self.console.print("[yellow]Usage: /session show [session-id][/yellow]")
            return
        store = getattr(self.runtime, "session_store", None)
        if store is None:
            self.console.print("[yellow]Event Session storage is unavailable for this runtime.[/yellow]")
            return
        session_id = values[1] if len(values) > 1 else self.runtime.agent.session_id
        try:
            item = store.replay(session_id)
        except (OSError, ValueError) as error:
            self.console.print(f"[red]Could not load Session: {error}[/red]")
            return
        warnings = "\n".join(f"- {warning}" for warning in item.warnings) or "- none"
        self.console.print(Panel(
            "\n".join([
                f"Session: {item.session_id}",
                f"Repository: {item.repository_root or '?'}",
                f"Model: {item.model or '?'}",
                f"Status: {item.status}",
                f"Events: {len(item.events)}",
                f"Messages: {len(item.messages)} (model projection: {len(item.model_messages)})",
                f"Usage: {item.prompt_tokens} prompt + {item.completion_tokens} completion",
                "Recovery warnings:",
                warnings,
            ]),
            title="Session details",
        ))

    def _resume(self, session_id: str) -> None:
        if not session_id:
            self.console.print("[yellow]Usage: /resume <session-id>[/yellow]")
            return
        resume = getattr(self.runtime, "resume_session", None)
        if resume is None:
            self.console.print("[yellow]Event Session storage is unavailable for this runtime.[/yellow]")
            return
        try:
            item = resume(session_id)
        except (OSError, ValueError) as error:
            self.console.print(f"[red]Could not resume Session: {error}[/red]")
            return
        warning = f" Warnings: {len(item.warnings)}." if item.warnings else ""
        self.console.print(f"[green]Resumed Session {item.session_id} ({len(item.model_messages)} messages).[/green]{warning}")

    def _details(self, value: str) -> None:
        sink = self.runtime.agent.event_sink
        terminal_sink = getattr(sink, "downstream", sink)
        if not isinstance(terminal_sink, TerminalEventSink):
            self.console.print("[yellow]Tool detail mode is unavailable for this output target.[/yellow]")
            return
        normalized = value.lower()
        if normalized not in {"", "on", "off"}:
            self.console.print("[yellow]Usage: /details [on|off][/yellow]")
            return
        if normalized:
            terminal_sink.show_full_results = normalized == "on"
        state = "on" if terminal_sink.show_full_results else "off"
        self.console.print(f"Tool detail mode: [cyan]{state}[/cyan]")

    def _ensure_session_persisted(self) -> None:
        sink = getattr(self.runtime.agent, "event_sink", None)
        ensure = getattr(sink, "ensure_persisted", None)
        if ensure is None:
            return
        try:
            ensure()
        except OSError as error:
            self.console.print(f"[red]Session autosave error: {error}[/red]")

    def _model(self, name: str) -> None:
        if name:
            change_model = getattr(self.runtime, "set_model", None)
            if change_model is not None:
                change_model(name)
            else:
                self.runtime.agent.llm.model = name
                self.runtime.config.model = name
            self.console.print(f"Model switched to [cyan]{name}[/cyan]")
        else:
            self.console.print(f"Current model: [cyan]{self.runtime.config.model}[/cyan]")

    def _clear(self) -> None:
        self.runtime.agent.reset()
        self.console.print("[yellow]Conversation messages cleared.[/yellow]")

    def _enter_plan_mode(self) -> None:
        assert self.plan_session is not None
        self.plan_session.reset()
        self._plan_mode = True
        self.console.print(
            "[cyan]已进入 Plan 模式。[/cyan] 描述任务后可继续修订，明确回复“批准并执行”才会启动。"
        )

    def _leave_plan_mode(self) -> None:
        assert self.plan_session is not None
        self._plan_mode = False
        self.plan_session.reset()
        self.console.print("[cyan]已返回 Chat 模式。[/cyan]")

    def _remember_managed_run(self) -> None:
        assert self.plan_session is not None
        result = self.plan_session.last_result
        if result is None:
            return
        self.runtime.agent.messages.append({
            "role": "system",
            "content": "\n".join([
                "A FeaturePilot Managed Run completed during this chat.",
                f"Run: {result.run.id}",
                f"Status: {result.run.status}",
                f"Workspace: {result.workspace.path}",
                f"Validation: {result.validation.status}",
                f"Validation artifact: {result.validation_path}",
                f"Events: {result.events_path}",
                f"Patch: {result.patch_path}",
                f"Report: {result.report_path}",
                f"Changed files: {len(result.changes.files)} (+{result.changes.additions}/-{result.changes.deletions})",
                "The Managed changes remain in that isolated Workspace; Chat tools still target the source repository.",
                f"Managed Agent summary: {result.response}",
            ]),
        })

    def _repository_changed_files(self) -> list[str]:
        files = []
        for value in _changed_files:
            path = Path(value).resolve()
            try:
                files.append(path.relative_to(self.runtime.repository).as_posix())
            except ValueError:
                continue
        return sorted(files)


def _brief_arguments(arguments: object, max_length: int = 120) -> str:
    if not isinstance(arguments, dict):
        return _summarize(str(arguments), max_length)
    value = ", ".join(f"{key}={item!r}" for key, item in arguments.items())
    return _summarize(value, max_length)


def _summarize(value: str, limit: int) -> str:
    normalized = value.strip()
    if len(normalized) <= limit:
        return normalized
    head = max(1, limit * 2 // 3)
    tail = max(1, limit - head - 24)
    return f"{normalized[:head]}\n... output folded ...\n{normalized[-tail:]}"


def _tool_status(result: str) -> str:
    lowered = result.lstrip().lower()
    if lowered.startswith("[limit reached]"):
        return "not executed"
    if lowered.startswith(("policy denied", "permission denied", "⚠ blocked")):
        return "denied"
    if lowered.startswith("error") or "[exit code:" in lowered:
        return "error"
    return "completed"


def _extract_plan_request(value: str) -> tuple[bool, str]:
    normalized = value.strip()
    lowered = normalized.casefold()
    prefixes = (
        "先制定计划",
        "先制定一个计划",
        "请先制定计划",
        "请先制定一个计划",
        "帮我制定计划",
        "帮我制定一个计划",
        "帮我先制定计划",
        "帮我先制定一个计划",
        "我想先制定计划",
        "我想先制定一个计划",
        "先做计划",
        "进入计划模式",
        "使用计划模式",
        "用计划模式",
        "我想用计划模式",
        "进入plan模式",
        "用plan模式",
        "/plan",
        "plan mode",
    )
    for prefix in prefixes:
        if lowered.startswith(prefix.casefold()):
            task = normalized[len(prefix):].lstrip("：:，,。.!！ ")
            return True, task
    return False, ""


def _is_return_to_chat(value: str) -> bool:
    normalized = value.strip().casefold().rstrip("。.!！")
    return normalized in {"返回聊天", "返回chat", "退出计划模式", "回到聊天", "/chat"}
