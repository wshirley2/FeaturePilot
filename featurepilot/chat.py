"""Minimal event-driven terminal Chat for FeaturePilot C2."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from corecoder.context import estimate_tokens
from corecoder.events import RuntimeEvent, RuntimeEventType
from corecoder.tools.edit import _changed_files

from .runtime import ChatRuntime

InputFunction = Callable[[str], str]


class TerminalEventSink:
    """Render Runtime Events without understanding or duplicating AgentLoop."""

    def __init__(self, console: Console | None = None, *, result_limit: int = 800):
        self.console = console or Console()
        self.result_limit = result_limit
        self._tool_started: dict[str, float] = {}
        self._streamed = False
        self.last_turn_streamed = False

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
            self._tool_started[call_id] = time.monotonic()
            name = event.payload.get("tool_name", "unknown")
            arguments = _brief_arguments(event.payload.get("arguments", {}))
            prefix = "\n" if self._streamed else ""
            self.console.print(f"{prefix}[dim]→ {name}({arguments})[/dim]")
            self._streamed = False
        elif event.event_type is RuntimeEventType.TOOL_COMPLETED:
            call_id = event.tool_call_id or event.event_id
            started = self._tool_started.pop(call_id, None)
            elapsed = f" in {time.monotonic() - started:.2f}s" if started is not None else ""
            result = str(event.payload.get("result", ""))
            status = "interrupted" if event.payload.get("interrupted") else _tool_status(result)
            style = "yellow" if status == "interrupted" else ("red" if status in {"error", "denied"} else "green")
            self.console.print(f"[{style}]← {event.payload.get('tool_name', 'tool')}: {status}{elapsed}[/{style}]")
            if result:
                self.console.print(f"[dim]{_summarize(result, self.result_limit)}[/dim]")
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
            self.console.print("[yellow]Maximum tool-call rounds reached.[/yellow]")


class ChatSession:
    """Own terminal input and commands while Agent owns the conversation loop."""

    def __init__(
        self,
        runtime: ChatRuntime,
        *,
        console: Console | None = None,
        input_fn: InputFunction = input,
    ):
        self.runtime = runtime
        self.console = console or Console()
        self.input_fn = input_fn

    def run(self) -> int:
        self.show_startup()
        while True:
            try:
                user_input = self.input_fn("You > ").strip()
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
            if user_input.startswith("/"):
                self._handle_command(user_input)
                continue

            try:
                response = self.runtime.agent.chat(user_input)
                # Providers used by embedders may not stream token callbacks.
                sink = self.runtime.agent.event_sink
                if isinstance(sink, TerminalEventSink) and not sink.last_turn_streamed and response:
                    self.console.print(Markdown(response))
            except KeyboardInterrupt:
                self.console.print("[yellow]Turn cancelled. The chat session is still active.[/yellow]")
            except Exception as error:
                self.console.print(f"[red]Error: {error}[/red]")

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
            "Type /help for commands; Ctrl+C cancels a turn; /exit exits Chat.",
        ])
        self.console.print(Panel(body, border_style="blue"))

    def _handle_command(self, command_line: str) -> None:
        command, _, argument = command_line.partition(" ")
        handlers = {
            "/help": self._show_help,
            "/status": self.show_startup,
            "/tools": self._show_tools,
            "/files": self._show_files,
            "/diff": self._show_diff,
            "/tokens": self._show_tokens,
            "/compact": self._compact,
            "/save": self._session_pending,
            "/sessions": self._session_pending,
            "/model": lambda: self._model(argument.strip()),
            "/clear": self._clear,
        }
        handler = handlers.get(command.lower())
        if handler is None:
            self.console.print(f"[yellow]Unknown command: {command} (try /help)[/yellow]")
        else:
            handler()

    def _show_help(self) -> None:
        self.console.print(Panel(
            "/status  Runtime and repository summary\n"
            "/tools   Available tools\n"
            "/files   Files changed in this process\n"
            "/diff    Current Git working-tree diff\n"
            "/tokens  Provider token usage and estimated cost\n"
            "/compact Compress model context\n"
            "/model [name]  Show or switch model\n"
            "/clear   Clear conversation messages\n"
            "/save, /sessions  Available after C4 event sessions\n"
            "/exit    Exit Chat",
            title="FeaturePilot Commands",
        ))

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
            result = subprocess.run(
                ["git", "diff", "--no-ext-diff"],
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
        before = estimate_tokens(self.runtime.agent.messages)
        compressed = self.runtime.agent.context.maybe_compress(
            self.runtime.agent.messages,
            self.runtime.agent.llm,
        )
        after = estimate_tokens(self.runtime.agent.messages)
        message = f"Compressed: {before} → {after} tokens" if compressed else f"Nothing to compress ({before} tokens)"
        self.console.print(message)

    def _session_pending(self) -> None:
        self.console.print("[yellow]Event-based session save/resume is planned for C4 and is not active yet.[/yellow]")

    def _model(self, name: str) -> None:
        if name:
            self.runtime.agent.llm.model = name
            self.runtime.config.model = name
            self.console.print(f"Model switched to [cyan]{name}[/cyan]")
        else:
            self.console.print(f"Current model: [cyan]{self.runtime.config.model}[/cyan]")

    def _clear(self) -> None:
        self.runtime.agent.reset()
        self.console.print("[yellow]Conversation messages cleared.[/yellow]")

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
    if lowered.startswith(("policy denied", "⚠ blocked")):
        return "denied"
    if lowered.startswith("error") or "[exit code:" in lowered:
        return "error"
    return "completed"
