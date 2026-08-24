"""C2 RuntimeBootstrap and minimal event-driven CLI Chat tests."""

from __future__ import annotations

import shutil
import subprocess
import sys
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from prompt_toolkit.document import Document
from rich.console import Console

from corecoder.events import NullEventSink, RuntimeEvent, RuntimeEventType
from corecoder.llm import LLMResponse, ToolCall
from corecoder.permissions import PermissionDecision
from featurepilot.chat import ChatSession, SlashCommandCompleter, TerminalEventSink
from featurepilot.chat_executor import RepositoryToolExecutor, _normalized_command
from featurepilot.cli import _normalize_command
from featurepilot.managed import ManagedRunService
from featurepilot.path_policy import ignored_child_names
from featurepilot.planning import PlanStore
from featurepilot.runtime import ChatRuntime, RuntimeBootstrap, RuntimeBootstrapInput, TaskRuntime
from featurepilot.runtime_contracts import RuntimeMode, RuntimeResultStatus
from featurepilot.workspace import CopyWorkspaceBackend, WorkspaceService

BENCHMARK_ROOT = Path(__file__).parents[2] / "benchmarks" / "cli_data_tool"


def copy_benchmark(destination: Path) -> Path:
    """Copy only source-controlled benchmark inputs, not local runtime artifacts."""

    return Path(shutil.copytree(
        BENCHMARK_ROOT,
        destination,
        ignore=lambda _directory, names: ignored_child_names(names),
    ))


def test_slash_command_completer_lists_and_filters_local_commands():
    completer = SlashCommandCompleter()

    all_commands = [item.text for item in completer.get_completions(Document("/"), None)]
    assert "/help" in all_commands
    assert "/status" in all_commands
    assert "/exit" in all_commands

    status_commands = [item.text for item in completer.get_completions(Document("/s"), None)]
    assert "/status" in status_commands
    assert "/sessions" in status_commands
    assert "/help" not in status_commands


def test_slash_command_completer_only_shows_plan_when_plan_session_is_available():
    assert "/plan" not in [item.text for item in SlashCommandCompleter().get_completions(Document("/"), None)]
    assert "/plan" in [
        item.text for item in SlashCommandCompleter(include_plan=True).get_completions(Document("/"), None)
    ]


def test_limit_backfilled_read_file_is_rendered_as_not_executed():
    output = StringIO()
    sink = TerminalEventSink(Console(file=output, force_terminal=False, color_system=None))
    event_args = {
        "session_id": "session-1",
        "turn_id": "turn-1",
        "round_index": 2,
        "tool_call_id": "call-1",
    }

    sink.emit(RuntimeEvent(
        event_type=RuntimeEventType.TOOL_REQUESTED,
        payload={"tool_name": "read_file", "arguments": {"file_path": "README.md"}},
        **event_args,
    ))
    sink.emit(RuntimeEvent(
        event_type=RuntimeEventType.TOOL_COMPLETED,
        payload={"tool_name": "read_file", "result": "[limit reached]", "interrupted": False},
        **event_args,
    ))

    rendered = output.getvalue()
    assert "← read_file: not executed" in rendered
    assert "已读取 15 个字符" not in rendered
    assert "未执行：达到运行限制" in rendered


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
    session_directory: Path | None = None,
):
    sink = TerminalEventSink(console)
    bootstrap = RuntimeBootstrap(provider_factory=lambda config: provider)
    return bootstrap.build(RuntimeBootstrapInput(
        repository=repository,
        event_sink=sink,
        permission_prompt=permission_prompt,
        session_directory=session_directory,
    ))


def test_runtime_bootstrap_builds_profile_context_and_repository_scoped_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    provider = FakeProvider([LLMResponse(content="ok")])
    console = Console(file=StringIO(), force_terminal=False, color_system=None)

    runtime = make_runtime(
        BENCHMARK_ROOT,
        provider,
        console,
        session_directory=tmp_path / "sessions",
    )

    assert runtime.repository == BENCHMARK_ROOT.resolve()
    assert isinstance(runtime, TaskRuntime)
    assert ChatRuntime is TaskRuntime
    assert runtime.runtime_mode is RuntimeMode.CHAT
    assert runtime.identity.source_repository == BENCHMARK_ROOT.resolve()
    assert runtime.identity.working_directory == BENCHMARK_ROOT.resolve()
    assert runtime.identity.workspace_path is None
    assert runtime.profile is not None
    assert runtime.profile.language == "python"
    assert "src/cli_data_tool/cli.py" in runtime.profile.entrypoints
    assert "Repository root:" in runtime.agent._system
    assert "This is a lightweight profile" in runtime.agent._system
    assert "Product: FeaturePilot" in runtime.agent._system
    assert f"Current model: {runtime.config.model}" in runtime.agent._system
    if sys.platform == "win32":
        assert "use cmd-compatible commands such as `dir`, not Unix commands such as `ls -la`" in runtime.agent._system
    assert {tool.name for tool in runtime.tools} == {
        "read_file",
        "glob",
        "grep",
        "edit_file",
        "write_file",
        "bash",
        "now",
    }


def test_featurepilot_model_setting_overrides_legacy_config_but_not_cli(tmp_path, monkeypatch):
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
                session_directory=tmp_path / "sessions",
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


def test_chat_read_only_inspects_benchmark_without_modifying_files(tmp_path, monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    repository = copy_benchmark(tmp_path / "cli_data_tool")
    read_path = "README.md"
    original = (repository / read_path).read_bytes()
    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall("read-1", "read_file", {"file_path": read_path})]),
        LLMResponse(content="仓库说明已读取，未修改文件。"),
    ])
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=120)
    runtime = make_runtime(repository, provider, console)
    inputs = iter(["读取 README 并说明项目，不要修改文件", "/exit"])

    assert ChatSession(runtime, console=console, input_fn=lambda prompt: next(inputs)).run() == 0

    assert (repository / read_path).read_bytes() == original
    assert "← read_file: completed" in output.getvalue()
    assert len(provider.requests) == 2


def test_chat_reads_dependency_manifest_directly_without_prompting_or_isolating(tmp_path, monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    repository = copy_benchmark(tmp_path / "cli_data_tool")
    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall("read-manifest", "read_file", {"file_path": "pyproject.toml"})]),
        LLMResponse(content="依赖配置已读取。"),
    ])
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=120)
    prompt = DenyPrompt()
    runtime = make_runtime(repository, provider, console, permission_prompt=prompt)
    inputs = iter(["读取 pyproject.toml，不要修改文件", "/exit"])

    assert ChatSession(runtime, console=console, input_fn=lambda prompt: next(inputs)).run() == 0

    rendered = output.getvalue()
    assert "← read_file: completed" in rendered
    assert "需要隔离执行" not in rendered
    assert prompt.requests == []


def test_chat_directory_listing_command_executes_directly_without_prompting(tmp_path, monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    repository = copy_benchmark(tmp_path / "cli_data_tool")
    directory_command = "dir" if sys.platform == "win32" else "ls -la"
    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall("list-files", "bash", {"command": directory_command})]),
        LLMResponse(content="目录已列出。"),
    ])
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=120)
    prompt = DenyPrompt()
    runtime = make_runtime(repository, provider, console, permission_prompt=prompt)
    inputs = iter(["列出当前目录文件", "/exit"])

    assert ChatSession(runtime, console=console, input_fn=lambda prompt: next(inputs)).run() == 0

    rendered = output.getvalue()
    assert "← bash: completed" in rendered
    assert prompt.requests == []


def test_directory_listing_commands_are_normalized_as_read_only_shell_commands():
    assert _normalized_command("dir").kind.value == "read_only_shell"
    assert _normalized_command("ls -la").kind.value == "read_only_shell"


def test_chat_end_to_end_reads_edits_validates_and_continues_without_network(tmp_path, monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    repository = copy_benchmark(tmp_path / "cli_data_tool")
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


def test_blocked_command_stops_the_turn_without_a_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(
            "dangerous-command",
            "bash",
            {"command": "git reset --hard"},
        )]),
    ])
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    runtime = make_runtime(tmp_path, provider, console)

    response = runtime.run_turn("执行 git reset --hard")

    assert "该操作已被阻断，未执行" in response
    assert len(provider.requests) == 1
    assert "bash: denied" in output.getvalue()
    assert "git reset --hard" in runtime.agent.messages[-2]["content"]
    saved = runtime.session_store.replay(runtime.agent.session_id)
    assessment = next(event for event in saved.events if event.event_type == "execution_control_assessed")
    assert assessment.payload["required_control"] == "block"
    assert assessment.tool_call_id == "dangerous-command"
    assert assessment.payload["reasons"][0]["evidence"]


def test_chat_blocks_patch_application_without_a_source_promotion_capability(tmp_path, monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    target = tmp_path / "notes.txt"
    target.write_text("original\n", encoding="utf-8")
    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(
            "apply-patch",
            "bash",
            {"command": "git apply changes.patch"},
        )]),
    ])
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    runtime = make_runtime(tmp_path, provider, console)

    response = runtime.run_turn("把审查 Patch 应用到源仓库")

    assert "该操作已被阻断，未执行" in response
    assert "将 Patch 回写源仓库需要专用、可审查的应用能力" in response
    assert target.read_text(encoding="utf-8") == "original\n"
    assert len(provider.requests) == 1
    saved = runtime.session_store.replay(runtime.agent.session_id)
    assessment = next(event for event in saved.events if event.event_type == "execution_control_assessed")
    assert assessment.payload["required_control"] == "block"
    assert assessment.payload["reasons"][0]["code"] == "unsupported_patch_application"


def test_chat_blocks_patch_application_with_git_global_path_options(tmp_path, monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(
            "apply-patch-in-worktree",
            "bash",
            {"command": "git -C . apply changes.patch"},
        )]),
    ])
    runtime = make_runtime(tmp_path, provider, Console(file=StringIO(), force_terminal=False, color_system=None))

    response = runtime.run_turn("在当前工作目录应用审查 Patch")

    assert "该操作已被阻断，未执行" in response
    saved = runtime.session_store.replay(runtime.agent.session_id)
    assessment = next(event for event in saved.events if event.event_type == "execution_control_assessed")
    assert assessment.payload["reasons"][0]["code"] == "unsupported_patch_application"


def test_chat_execution_control_assesses_direct_and_confirm_before_effects(tmp_path, monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    target = tmp_path / "notes.py"
    target.write_text("before\n", encoding="utf-8")
    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall("read-1", "read_file", {"file_path": "notes.py"})]),
        LLMResponse(tool_calls=[ToolCall("write-1", "write_file", {"file_path": "notes.py", "content": "after\n"})]),
        LLMResponse(content="完成。"),
    ])
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    runtime = make_runtime(tmp_path, provider, console, permission_prompt=AllowOncePrompt())

    assert runtime.run_turn("先读取").startswith("完成")
    assert target.read_text(encoding="utf-8") == "after\n"
    events = runtime.session_store.replay(runtime.agent.session_id).events
    assessments = [event for event in events if event.event_type == "execution_control_assessed"]

    assert [event.payload["required_control"] for event in assessments] == ["direct", "confirm"]
    assert all(event.session_id == runtime.agent.session_id and event.turn_id for event in assessments)
    assert all(event.tool_call_id and event.payload["reasons"] for event in assessments)


def test_isolate_is_unexecuted_persisted_and_visible_after_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    lock_file = tmp_path / "poetry.lock"
    lock_file.write_text("original\n", encoding="utf-8")
    provider = FakeProvider([LLMResponse(tool_calls=[ToolCall(
        "lock-write",
        "write_file",
        {"file_path": "poetry.lock", "content": "changed\n"},
    )])])
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    prompt = AllowOncePrompt()
    runtime = RuntimeBootstrap(provider_factory=lambda config: provider).build(RuntimeBootstrapInput(
        repository=tmp_path,
        event_sink=TerminalEventSink(console),
        permission_prompt=prompt,
        session_directory=tmp_path / "sessions",
        task_id="chat-task-1",
    ))

    response = runtime.run_turn("更新锁文件")
    runtime.ensure_persisted()

    assert "需要隔离执行" in response
    assert "源仓库未修改" in output.getvalue()
    assert "可选择在隔离 Workspace 中继续" in output.getvalue()
    assert lock_file.read_text(encoding="utf-8") == "original\n"
    assert prompt.requests == []
    assert len(provider.requests) == 1
    assert runtime.last_result is not None
    assert runtime.last_result.status is RuntimeResultStatus.ESCALATION_REQUIRED
    assert len(runtime.pending_isolation_requests) == 1
    saved = runtime.session_store.replay(runtime.agent.session_id)
    assessment = next(event for event in saved.events if event.event_type == "execution_control_assessed")
    assert assessment.payload["required_control"] == "isolate"
    assert assessment.payload["task_id"] == "chat-task-1"
    assert saved.pending_isolation_requests[0]["tool_call_id"] == "lock-write"
    assert saved.last_result is not None
    assert saved.last_result.status is RuntimeResultStatus.ESCALATION_REQUIRED

    ChatSession(runtime, console=console)._session_command("show")
    assert "Last result: 需要隔离执行（本轮未执行）" in output.getvalue()

    resumed_provider = FakeProvider([])
    resumed = RuntimeBootstrap(provider_factory=lambda config: resumed_provider).build(RuntimeBootstrapInput(
        repository=tmp_path,
        event_sink=TerminalEventSink(Console(file=StringIO(), force_terminal=False, color_system=None)),
        session_directory=tmp_path / "sessions",
        resume_session_id=runtime.agent.session_id,
    ))

    assert resumed.pending_isolation_requests == saved.pending_isolation_requests
    assert lock_file.read_text(encoding="utf-8") == "original\n"
    assert resumed_provider.requests == []


def _isolation_service(tmp_path: Path, provider: FakeProvider) -> ManagedRunService:
    return ManagedRunService(
        plan_store=PlanStore(tmp_path / "plans"),
        workspace_service=WorkspaceService(CopyWorkspaceBackend(tmp_path / "runs")),
        runtime_bootstrap=RuntimeBootstrap(provider_factory=lambda _config: provider),
        event_sink=NullEventSink(),
    )


def test_chat_can_upgrade_an_isolated_write_without_touching_source_or_replaying_after_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    lock_file = tmp_path / "poetry.lock"
    lock_file.write_text("original\n", encoding="utf-8")
    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(
            "lock_write",
            "write_file",
            {"file_path": "poetry.lock", "content": "isolated\n"},
        )]),
        LLMResponse(tool_calls=[ToolCall(
            "workspace-write",
            "write_file",
            {"file_path": "poetry.lock", "content": "isolated\n"},
        )]),
        LLMResponse(content="隔离副本已更新。"),
    ])
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    runtime = make_runtime(tmp_path, provider, console, session_directory=tmp_path / "sessions")
    inputs = iter(["更新锁文件", "1", "/exit"])

    assert ChatSession(
        runtime,
        console=console,
        input_fn=lambda _prompt: next(inputs),
        isolation_service=_isolation_service(tmp_path, provider),
    ).run() == 0

    assert lock_file.read_text(encoding="utf-8") == "original\n"
    workspaces = list((tmp_path / "runs").glob("*/workspace"))
    assert len(workspaces) == 1
    assert (workspaces[0] / "poetry.lock").read_text(encoding="utf-8") == "isolated\n"
    run_directory = workspaces[0].parent
    assert (run_directory / "changes.patch").is_file()
    assert (run_directory / "validation.json").is_file()
    assert (run_directory / "report.md").is_file()
    assert (run_directory / "events.jsonl").is_file()
    assert runtime.pending_isolation_requests == []
    assert len(provider.requests) == 3
    rendered = output.getvalue()
    assert "需要隔离执行" in rendered
    assert "隔离执行已结束" in rendered
    assert "源仓库未修改" in rendered

    patch_path = run_directory / "changes.patch"
    provider.responses = iter([
        LLMResponse(tool_calls=[ToolCall(
            "review-patch",
            "read_file",
            {"file_path": str(patch_path)},
        )]),
        LLMResponse(content="已审查隔离 Patch。"),
    ])
    assert runtime.run_turn("读取这次隔离执行的 Patch") == "已审查隔离 Patch。"
    assert "+isolated" in runtime.agent.messages[-2]["content"]

    saved = runtime.session_store.replay(runtime.agent.session_id)
    assert saved.pending_isolation_requests == []
    assert str(patch_path) in saved.review_artifact_paths
    assert any(event.event_type == "isolation_upgrade_completed" for event in saved.events)
    resumed_provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(
            "review-after-resume",
            "read_file",
            {"file_path": str(patch_path)},
        )]),
        LLMResponse(content="恢复后仍可审查 Patch。"),
    ])
    resumed = RuntimeBootstrap(provider_factory=lambda _config: resumed_provider).build(RuntimeBootstrapInput(
        repository=tmp_path,
        event_sink=TerminalEventSink(Console(file=StringIO(), force_terminal=False, color_system=None)),
        session_directory=tmp_path / "sessions",
        resume_session_id=runtime.agent.session_id,
    ))
    assert resumed.pending_isolation_requests == []
    assert resumed.run_turn("恢复后读取 Patch") == "恢复后仍可审查 Patch。"
    assert len(resumed_provider.requests) == 2


def test_chat_blocks_unregistered_outside_artifact_reads(tmp_path, monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    outside = tmp_path.parent / "unregistered-artifact.txt"
    outside.write_text("do not expose\n", encoding="utf-8")
    provider = FakeProvider([LLMResponse(tool_calls=[ToolCall(
        "outside-read",
        "read_file",
        {"file_path": str(outside)},
    )])])
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    runtime = make_runtime(tmp_path, provider, console)

    response = runtime.run_turn("读取仓库外文件")

    assert "该操作已被阻断，未执行" in response
    assert "do not expose" not in "\n".join(str(message) for message in runtime.agent.messages)


def test_chat_can_keep_or_cancel_an_isolated_request_without_creating_a_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    lock_file = tmp_path / "poetry.lock"
    lock_file.write_text("original\n", encoding="utf-8")
    for choice, expected_pending in (("2", 1), ("0", 0)):
        provider = FakeProvider([LLMResponse(tool_calls=[ToolCall(
            f"lock-write-{choice}",
            "write_file",
            {"file_path": "poetry.lock", "content": "changed\n"},
        )])])
        output = StringIO()
        console = Console(file=output, force_terminal=False, color_system=None)
        session_directory = tmp_path / f"sessions-{choice}"
        runtime = make_runtime(tmp_path, provider, console, session_directory=session_directory)
        inputs = iter(["更新锁文件", choice, "/exit"])

        assert ChatSession(
            runtime,
            console=console,
            input_fn=lambda _prompt, active_inputs=inputs: next(active_inputs),
            isolation_service=_isolation_service(tmp_path / f"service-{choice}", provider),
        ).run() == 0

        assert lock_file.read_text(encoding="utf-8") == "original\n"
        assert runtime.pending_isolation_requests == [] if expected_pending == 0 else len(runtime.pending_isolation_requests) == 1
        assert not (tmp_path / f"service-{choice}" / "runs").exists()
        saved = runtime.session_store.replay(runtime.agent.session_id)
        assert len(saved.pending_isolation_requests) == expected_pending
        if choice == "0":
            assert any(event.event_type == "isolation_cancelled" for event in saved.events)


def test_chat_keeps_pending_isolation_when_workspace_creation_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    lock_file = tmp_path / "poetry.lock"
    lock_file.write_text("original\n", encoding="utf-8")
    provider = FakeProvider([LLMResponse(tool_calls=[ToolCall(
        "lock-write",
        "write_file",
        {"file_path": "poetry.lock", "content": "changed\n"},
    )])])
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    runtime = make_runtime(tmp_path, provider, console, session_directory=tmp_path / "sessions")
    service = _isolation_service(tmp_path, provider)

    def fail_create(_scope):
        raise OSError("workspace backend unavailable")

    monkeypatch.setattr(service.workspace_service, "create_for_scope", fail_create)
    inputs = iter(["更新锁文件", "1", "/exit"])
    assert ChatSession(
        runtime,
        console=console,
        input_fn=lambda _prompt: next(inputs),
        isolation_service=service,
    ).run() == 0

    assert lock_file.read_text(encoding="utf-8") == "original\n"
    assert len(runtime.pending_isolation_requests) == 1
    assert len(provider.requests) == 1
    saved = runtime.session_store.replay(runtime.agent.session_id)
    assert len(saved.pending_isolation_requests) == 1
    assert any(event.event_type == "isolation_upgrade_failed" for event in saved.events)
    assert "无法创建或启动隔离执行" in output.getvalue()


def test_chat_keeps_pending_isolation_after_agent_failure_and_retains_workspace_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    lock_file = tmp_path / "poetry.lock"
    lock_file.write_text("original\n", encoding="utf-8")
    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(
            "lock-write", "write_file", {"file_path": "poetry.lock", "content": "changed\n"},
        )]),
        RuntimeError("isolated provider unavailable"),
    ])
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    runtime = make_runtime(tmp_path, provider, console, session_directory=tmp_path / "sessions")
    inputs = iter(["更新锁文件", "1", "/exit"])

    assert ChatSession(
        runtime,
        console=console,
        input_fn=lambda _prompt: next(inputs),
        isolation_service=_isolation_service(tmp_path, provider),
    ).run() == 0

    assert lock_file.read_text(encoding="utf-8") == "original\n"
    assert len(runtime.pending_isolation_requests) == 1
    run_directories = list((tmp_path / "runs").glob("*"))
    assert len(run_directories) == 1
    assert (run_directories[0] / "changes.patch").is_file()
    assert (run_directories[0] / "report.md").is_file()
    assert "隔离执行失败，源仓库未修改" in output.getvalue()


def test_chat_returns_to_source_session_after_isolated_validation_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    lock_file = tmp_path / "poetry.lock"
    lock_file.write_text("original\n", encoding="utf-8")
    provider = FakeProvider([
        LLMResponse(tool_calls=[ToolCall(
            "lock-write", "write_file", {"file_path": "poetry.lock", "content": "changed\n"},
        )]),
        LLMResponse(tool_calls=[ToolCall(
            "workspace-write", "write_file", {"file_path": "poetry.lock", "content": "changed\n"},
        )]),
        LLMResponse(content="副本已写入。"),
    ])
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    runtime = make_runtime(tmp_path, provider, console, session_directory=tmp_path / "sessions")
    assert runtime.profile is not None
    runtime.profile.validation_commands = [[sys.executable, "-c", "import sys; sys.exit(7)"]]
    inputs = iter(["更新锁文件", "1", "/exit"])

    assert ChatSession(
        runtime,
        console=console,
        input_fn=lambda _prompt: next(inputs),
        isolation_service=_isolation_service(tmp_path, provider),
    ).run() == 0

    assert lock_file.read_text(encoding="utf-8") == "original\n"
    assert runtime.pending_isolation_requests == []
    run_directory = next((tmp_path / "runs").glob("*") )
    assert (run_directory / "validation.json").is_file()
    assert "状态：failed" in output.getvalue()
    saved = runtime.session_store.replay(runtime.agent.session_id)
    assert saved.pending_isolation_requests == []
    assert any(event.event_type == "isolation_upgrade_completed" for event in saved.events)


def test_chat_commands_and_eof_are_local_and_do_not_call_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    provider = FakeProvider([])
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    runtime = RuntimeBootstrap(provider_factory=lambda config: provider).build(RuntimeBootstrapInput(
        repository=BENCHMARK_ROOT,
        event_sink=TerminalEventSink(console),
        session_directory=tmp_path / "sessions",
    ))
    commands = iter(["/help", "/status", "/tools", "/files", "/diff", "/tokens", "/compact", "/save", "/sessions", "/session show", "/model", "/clear"])

    def input_fn(prompt):
        try:
            return next(commands)
        except StopIteration as error:
            raise EOFError from error

    assert ChatSession(runtime, console=console, input_fn=input_fn, plan_session=object()).run() == 0
    rendered = output.getvalue()
    assert "FeaturePilot Commands" in rendered
    assert "操作保护：写入和命令会在执行前按实际影响进行确认、隔离或阻断。" in rendered
    assert "/plan" in rendered
    assert "自动保存已开启" in rendered
    assert "FeaturePilot Sessions" in rendered
    assert "Session details" in rendered
    assert "Last result: -" in rendered
    assert "Current model:" in rendered
    assert "Bye!" in rendered
    assert provider.requests == []


def test_status_shows_live_session_and_context_summary_without_startup_panel(tmp_path):
    provider = FakeProvider([])
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    runtime = RuntimeBootstrap(provider_factory=lambda config: provider).build(RuntimeBootstrapInput(
        repository=tmp_path,
        event_sink=TerminalEventSink(console),
        session_directory=tmp_path / "sessions",
    ))

    ChatSession(runtime, console=console)._show_status()

    rendered = output.getvalue()
    assert "Session ID" in rendered
    assert runtime.agent.session_id in rendered
    assert "Context" in rendered
    assert "Usage" in rendered
    assert "Estimated cost" in rendered
    assert "Repository" not in rendered
    assert "FeaturePilot Chat" not in rendered
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


def test_ctrl_c_cancels_only_current_turn_and_chat_continues(tmp_path, monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    provider = FakeProvider([KeyboardInterrupt(), LLMResponse(content="second turn works")])
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    runtime = make_runtime(
        BENCHMARK_ROOT,
        provider,
        console,
        session_directory=tmp_path / "sessions",
    )
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
