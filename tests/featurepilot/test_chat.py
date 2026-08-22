"""C2 RuntimeBootstrap and minimal event-driven CLI Chat tests."""

from __future__ import annotations

import shutil
import subprocess
import sys
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console

from corecoder.llm import LLMResponse, ToolCall
from corecoder.permissions import PermissionDecision
from featurepilot.chat import ChatSession, TerminalEventSink
from featurepilot.chat_executor import RepositoryToolExecutor
from featurepilot.cli import _normalize_command
from featurepilot.runtime import RuntimeBootstrap, RuntimeBootstrapInput

BENCHMARK_ROOT = Path(__file__).parents[2] / "benchmarks" / "cli_data_tool"


class FakeProvider:
    model = "fake-coder"
    total_prompt_tokens = 12
    total_completion_tokens = 8
    estimated_cost = None

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def chat(self, messages, tools=None, on_token=None):
        self.requests.append({"messages": messages, "tools": tools})
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        if on_token and response.content:
            on_token(response.content)
        return response


class AllowOncePrompt:
    def __init__(self):
        self.requests = []

    def decide(self, request):
        self.requests.append(request)
        return PermissionDecision.allow("test approval")


class DenyPrompt(AllowOncePrompt):
    def decide(self, request):
        self.requests.append(request)
        return PermissionDecision.deny("test rejection")


def make_runtime(
    repository: Path,
    provider: FakeProvider,
    console: Console,
    *,
    permission_prompt=None,
):
    sink = TerminalEventSink(console)
    bootstrap = RuntimeBootstrap(provider_factory=lambda config: provider)
    return bootstrap.build(RuntimeBootstrapInput(
        repository=repository,
        event_sink=sink,
        permission_prompt=permission_prompt,
    ))


def test_runtime_bootstrap_builds_profile_context_and_repository_scoped_agent(monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    provider = FakeProvider([LLMResponse(content="ok")])
    console = Console(file=StringIO(), force_terminal=False, color_system=None)

    runtime = make_runtime(BENCHMARK_ROOT, provider, console)

    assert runtime.repository == BENCHMARK_ROOT.resolve()
    assert runtime.profile is not None
    assert runtime.profile.language == "python"
    assert "src/cli_data_tool/cli.py" in runtime.profile.entrypoints
    assert "Repository root:" in runtime.agent._system
    assert "This is a lightweight profile" in runtime.agent._system
    assert {tool.name for tool in runtime.tools} == {
        "read_file",
        "glob",
        "grep",
        "edit_file",
        "write_file",
        "bash",
        "now",
    }


def test_featurepilot_model_setting_overrides_legacy_config_but_not_cli(monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    monkeypatch.setenv("CORECODER_MODEL", "legacy-corecoder-model")
    monkeypatch.delenv("FEATUREPILOT_MODEL", raising=False)
    console = Console(file=StringIO(), force_terminal=False, color_system=None)

    def build(model=None):
        return RuntimeBootstrap(provider_factory=lambda config: FakeProvider([])).build(
            RuntimeBootstrapInput(
                repository=BENCHMARK_ROOT,
                event_sink=TerminalEventSink(console),
                model=model,
            )
        )

    assert build().config.model == "legacy-corecoder-model"

    monkeypatch.setenv("FEATUREPILOT_MODEL", "featurepilot-model")
    assert build().config.model == "featurepilot-model"
    assert build("command-line-model").config.model == "command-line-model"


def test_repository_executor_denies_paths_outside_repository(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    executor = RepositoryToolExecutor(repository)

    class NeverRun:
        name = "read_file"

        def execute(self, **kwargs):
            raise AssertionError("outside request should not execute")

    result = executor.execute(NeverRun(), {"file_path": str(outside)})
    assert result.startswith("Policy denied read_file")

    NeverRun.name = "glob"
    result = executor.execute(NeverRun(), {"path": ".", "pattern": "../*.txt"})
    assert result.startswith("Policy denied glob")


def test_default_cli_spelling_enters_chat_for_current_or_explicit_repository():
    assert _normalize_command([]) == ["chat", "."]
    assert _normalize_command(["."]) == ["chat", "."]
    assert _normalize_command([str(BENCHMARK_ROOT)]) == ["chat", str(BENCHMARK_ROOT)]
    assert _normalize_command(["profile", str(BENCHMARK_ROOT)]) == ["profile", str(BENCHMARK_ROOT)]


def test_chat_end_to_end_reads_edits_validates_and_continues_without_network(tmp_path, monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    repository = tmp_path / "cli_data_tool"
    shutil.copytree(BENCHMARK_ROOT, repository)
    read_path = "src/cli_data_tool/exporter.py"
    original = (repository / read_path).read_text(encoding="utf-8")
    old = 'return "\\n".join(items)'
    new = 'return "\\n".join(str(item) for item in items)'
    assert old in original
    test_command = subprocess.list2cmdline([
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
    ])

    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall("read-1", "read_file", {"file_path": read_path})]),
        LLMResponse(tool_calls=[ToolCall(
            "edit-1",
            "edit_file",
            {"file_path": read_path, "old_string": old, "new_string": new},
        )]),
        LLMResponse(tool_calls=[ToolCall(
            "test-1",
            "bash",
            {"command": test_command},
        )]),
        LLMResponse(content="修改和测试已经完成。"),
        LLMResponse(content="我还记得刚才的修改。"),
    ])
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=120)
    prompt = AllowOncePrompt()
    runtime = make_runtime(repository, provider, console, permission_prompt=prompt)
    inputs = iter(["完成一个小修改并运行测试", "总结刚才做了什么", "/exit"])

    assert ChatSession(runtime, console=console, input_fn=lambda prompt: next(inputs)).run() == 0

    assert new in (repository / read_path).read_text(encoding="utf-8")
    rendered = output.getvalue()
    assert "FeaturePilot Chat" in rendered
    assert "→ read_file" in rendered
    assert "← edit_file: completed" in rendered
    assert "← bash: completed" in rendered
    assert "3 passed" in rendered
    assert "修改和测试已经完成。" in rendered
    assert "我还记得刚才的修改。" in rendered
    assert len(provider.requests) == 5
    assert [request.tool_name for request in prompt.requests] == ["edit_file"]


def test_rejected_write_stops_the_current_turn_without_retrying_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    target = tmp_path / "notes.txt"
    target.write_text("original\n", encoding="utf-8")
    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(
            "edit-denied",
            "edit_file",
            {"file_path": "notes.txt", "old_string": "original", "new_string": "changed"},
        )]),
    ])
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    prompt = DenyPrompt()
    runtime = make_runtime(tmp_path, provider, console, permission_prompt=prompt)

    response = runtime.agent.chat("修改 notes.txt")

    assert response == "已按你的拒绝停止本轮后续操作；edit_file 没有执行。你可以继续说明下一步需求。"
    assert target.read_text(encoding="utf-8") == "original\n"
    assert len(provider.requests) == 1
    assert runtime.agent.messages[-2] == {
        "role": "tool",
        "tool_call_id": "edit-denied",
        "content": "Permission denied edit_file: test rejection",
    }
    assert runtime.agent.messages[-1]["content"] == response
    assert "edit_file: denied" in output.getvalue()


def test_policy_denial_still_allows_a_safe_explanation_from_the_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(
            "dangerous-command",
            "bash",
            {"command": "git reset --hard"},
        )]),
        LLMResponse(content="该危险命令已被系统拦截，未执行。"),
    ])
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    runtime = make_runtime(tmp_path, provider, console)

    response = runtime.agent.chat("执行 git reset --hard")

    assert response == "该危险命令已被系统拦截，未执行。"
    assert len(provider.requests) == 2
    assert "bash: denied" in output.getvalue()


def test_chat_commands_and_eof_are_local_and_do_not_call_provider(monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    provider = FakeProvider([])
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    runtime = make_runtime(BENCHMARK_ROOT, provider, console)
    commands = iter(["/help", "/status", "/tools", "/files", "/diff", "/tokens", "/compact", "/save", "/sessions", "/model", "/clear"])

    def input_fn(prompt):
        try:
            return next(commands)
        except StopIteration as error:
            raise EOFError from error

    assert ChatSession(runtime, console=console, input_fn=input_fn).run() == 0
    rendered = output.getvalue()
    assert "FeaturePilot Commands" in rendered
    assert "Event-based session save/resume is planned for C4" in rendered
    assert "Current model:" in rendered
    assert "Bye!" in rendered
    assert provider.requests == []


def test_diff_does_not_leak_a_parent_git_worktree(monkeypatch, tmp_path):
    parent = tmp_path / "parent-repository"
    repository = parent / "temporary-copy"
    repository.mkdir(parents=True)
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    session = ChatSession(SimpleNamespace(repository=repository), console=console)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs["cwd"]))
        return subprocess.CompletedProcess(command, 0, stdout=str(parent), stderr="")

    monkeypatch.setattr("featurepilot.chat.subprocess.run", fake_run)

    session._show_diff()

    assert calls == [(["git", "rev-parse", "--show-toplevel"], repository)]
    assert "已避免展示父级仓库的变更" in output.getvalue()


def test_ctrl_c_cancels_only_current_turn_and_chat_continues(monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    provider = FakeProvider([KeyboardInterrupt(), LLMResponse(content="second turn works")])
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    runtime = make_runtime(BENCHMARK_ROOT, provider, console)
    inputs = iter(["interrupt this turn", "continue", "/exit"])

    assert ChatSession(runtime, console=console, input_fn=lambda prompt: next(inputs)).run() == 0
    rendered = output.getvalue()
    assert "Turn cancelled. The chat session is still active." in rendered
    assert "second turn works" in rendered
    assert len(provider.requests) == 2


def test_profile_failure_warns_but_still_builds_chat(monkeypatch, tmp_path):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")

    class BrokenProfiler:
        def profile(self, repository):
            raise RuntimeError("broken parser")

    provider = FakeProvider([LLMResponse(content="fallback works")])
    sink = TerminalEventSink(Console(file=StringIO(), force_terminal=False, color_system=None))
    runtime = RuntimeBootstrap(
        provider_factory=lambda config: provider,
        profiler=BrokenProfiler(),
    ).build(RuntimeBootstrapInput(repository=tmp_path, event_sink=sink))

    assert runtime.profile is None
    assert runtime.profile_warning == "Repository profile unavailable: broken parser"
    assert runtime.agent.chat("hello") == "fallback works"
