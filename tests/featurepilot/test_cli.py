import json
from pathlib import Path
from types import SimpleNamespace

from featurepilot.cli import main
from featurepilot.planning import PlanningService
from featurepilot.runtime import RuntimeBootstrap
from featurepilot.sessions import SessionStore

BENCHMARK_ROOT = Path(__file__).parents[2] / "benchmarks" / "cli_data_tool"


def test_profile_command_prints_repository_profile(capsys):
    assert main(["profile", str(BENCHMARK_ROOT)]) == 0

    profile = json.loads(capsys.readouterr().out)

    assert profile["language"] == "python"
    assert "pyproject.toml" in profile["config_files"]
    assert "src/cli_data_tool/cli.py" in profile["entrypoints"]
    assert "tests/test_export.py" in profile["test_files"]


def test_profile_command_rejects_missing_repository(capsys):
    missing = BENCHMARK_ROOT / "missing-repository"

    try:
        main(["profile", str(missing)])
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("Missing repository should be rejected")

    assert "Repository directory does not exist" in capsys.readouterr().err


def test_profile_command_can_write_json_file(tmp_path, capsys):
    output = tmp_path / "repository_profile.json"

    assert main(["profile", str(BENCHMARK_ROOT), "--output", str(output)]) == 0

    profile = json.loads(output.read_text(encoding="utf-8"))
    assert Path(profile["root"]).resolve() == BENCHMARK_ROOT.resolve()
    assert "repository_profile.json" in capsys.readouterr().out


def test_plan_create_command_prints_a_saved_record_as_json(capsys, tmp_path):
    assert main(
        [
            "plan",
            "create",
            str(BENCHMARK_ROOT),
            "--task",
            "Add JSON export format and update README",
            "--acceptance",
            "Default text output remains unchanged",
            "--json",
            "--store-dir",
            str(tmp_path / "plans"),
        ]
    ) == 0

    record = json.loads(capsys.readouterr().out)
    plan = record["plan"]

    assert plan["summary"] == "Add JSON export format and update README"
    assert record["status"] == "draft"
    assert "src/cli_data_tool/cli.py" in plan["read_files"]
    assert "tests/test_export.py" in plan["modify_files"]
    assert ["python", "-m", "pytest", "-q"] in plan["validation_commands"]
    assert "Default text output remains unchanged" in plan["steps"][-1]


def test_plan_commands_regenerate_and_approve_by_reference(tmp_path, capsys):
    store_dir = tmp_path / "plans"
    arguments = [
        "plan",
        "create",
        str(BENCHMARK_ROOT),
        "--task",
        "Add JSON export format",
        "--name",
        "json-export",
        "--store-dir",
        str(store_dir),
        "--json",
    ]

    assert main(arguments) == 0
    first_record = json.loads(capsys.readouterr().out)
    assert first_record["reference"] == "json-export-v1"

    assert main(["plan", "show", "json-export-v1", "--store-dir", str(store_dir)]) == 0
    detail = capsys.readouterr().out
    assert "Task: Add JSON export format" in detail
    assert "Files to modify:" in detail

    assert main(
        [
            "plan",
            "regenerate",
            "json-export-v1",
            "--store-dir",
            str(store_dir),
            "--json",
        ]
    ) == 0
    second_record = json.loads(capsys.readouterr().out)
    assert second_record["reference"] == "json-export-v2"
    assert second_record["plan"]["task_id"] == first_record["plan"]["task_id"]

    assert main(["plan", "list", "--store-dir", str(store_dir)]) == 0
    listing = capsys.readouterr().out
    assert "REFERENCE" in listing
    assert "json-export-v2" in listing
    assert "json-export-v1" in listing
    assert "task_id" not in listing

    assert main(["plan", "approve", "json-export-v1", "--store-dir", str(store_dir)]) == 0
    approved = capsys.readouterr().out
    assert "Plan approved: json-export-v1" in approved
    assert "Status: approved" in approved


def test_plans_alias_and_legacy_create_spelling_remain_available(tmp_path, capsys):
    assert main(
        [
            "plan",
            str(BENCHMARK_ROOT),
            "--task",
            "Add JSON export format",
            "--json",
            "--store-dir",
            str(tmp_path / "plans"),
        ]
    ) == 0
    capsys.readouterr()

    assert main(["plans", "list", "--store-dir", str(tmp_path / "plans")]) == 0
    assert "REFERENCE" in capsys.readouterr().out


def test_plan_chat_command_builds_the_conversational_session(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    captured = {}

    class FakePlanChatSession:
        def __init__(self, selected_repository, **kwargs):
            captured["repository"] = selected_repository
            captured["kwargs"] = kwargs

        def run(self):
            return 7

    monkeypatch.setattr("featurepilot.cli.PlanChatSession", FakePlanChatSession)

    result = main([
        "plan",
        "chat",
        str(repository),
        "--store-dir",
        str(tmp_path / "plans"),
        "--runs-dir",
        str(tmp_path / "runs"),
        "--max-provider-calls",
        "3",
    ])

    assert result == 7
    assert captured["repository"] == repository
    assert isinstance(captured["kwargs"]["planning_service"], PlanningService)
    assert captured["kwargs"]["limits"].max_provider_calls == 3


def test_default_chat_entry_does_not_inject_an_embedded_plan_session(tmp_path, monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    repository = tmp_path / "repository"
    repository.mkdir()
    captured = {}

    class Provider:
        model = "fake-unified"

    class FakeChatSession:
        def __init__(self, runtime, **kwargs):
            captured["runtime"] = runtime
            captured["plan_session"] = kwargs.get("plan_session")

        def run(self):
            return 9

    bootstrap = RuntimeBootstrap(provider_factory=lambda config: Provider())
    monkeypatch.setattr("featurepilot.cli.RuntimeBootstrap", lambda: bootstrap)
    monkeypatch.setattr("featurepilot.cli.ChatSession", FakeChatSession)

    result = main([
        "chat",
        str(repository),
        "--store-dir",
        str(tmp_path / "plans"),
        "--runs-dir",
        str(tmp_path / "runs"),
        "--max-tool-rounds",
        "4",
    ])

    assert result == 9
    assert captured["runtime"].repository == repository.resolve()
    assert captured["plan_session"] is None


def test_chat_tui_option_uses_the_same_runtime_bootstrap(tmp_path, monkeypatch):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    repository = tmp_path / "repository"
    repository.mkdir()
    captured = {}

    class Provider:
        model = "fake-unified"

    class FakeTui:
        event_sink = SimpleNamespace(emit=lambda _event: None)
        permission_prompt = object()

        def bind_runtime(self, runtime):
            captured["runtime"] = runtime

        def run(self):
            return 7

    bootstrap = RuntimeBootstrap(provider_factory=lambda config: Provider())
    monkeypatch.setattr("featurepilot.cli.RuntimeBootstrap", lambda: bootstrap)
    monkeypatch.setattr("featurepilot.cli.FeaturePilotTui", FakeTui)
    monkeypatch.setattr("featurepilot.cli.tui_supported", lambda: True)

    assert main(["chat", str(repository), "--tui"]) == 7
    assert captured["runtime"].repository == repository.resolve()


def test_chat_tui_option_falls_back_to_the_standard_cli_without_a_tty(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CORECODER_LOAD_DOTENV", "0")
    repository = tmp_path / "repository"
    repository.mkdir()

    class Provider:
        model = "fake-unified"

    class FakeChatSession:
        def __init__(self, runtime, **_kwargs):
            assert runtime.repository == repository.resolve()

        def run(self):
            return 6

    bootstrap = RuntimeBootstrap(provider_factory=lambda config: Provider())
    monkeypatch.setattr("featurepilot.cli.RuntimeBootstrap", lambda: bootstrap)
    monkeypatch.setattr("featurepilot.cli.ChatSession", FakeChatSession)
    monkeypatch.setattr("featurepilot.cli.tui_supported", lambda: False)

    assert main(["chat", str(repository), "--tui"]) == 6
    assert "requires an interactive TTY" in capsys.readouterr().err


def test_tui_workspace_trust_gate_runs_before_runtime_bootstrap(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    captured = {}

    class FakeTui:
        event_sink = SimpleNamespace(emit=lambda _event: None)
        permission_prompt = object()

        def bind_runtime(self, runtime):
            captured["runtime"] = runtime

        def run(self):
            return 3

    class FailingBootstrap:
        def build(self, _inputs):
            raise AssertionError("trust denial must happen before RuntimeBootstrap.build")

    monkeypatch.setattr("featurepilot.cli.FeaturePilotTui", FakeTui)
    monkeypatch.setattr("featurepilot.cli.tui_supported", lambda: True)
    monkeypatch.setattr("featurepilot.cli.sys.stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("featurepilot.cli.confirm_workspace_access", lambda path, console: False)
    monkeypatch.setattr("featurepilot.cli.RuntimeBootstrap", FailingBootstrap)

    assert main(["chat", str(repository), "--tui"]) == 0
    assert "runtime" not in captured


def test_sessions_commands_list_and_show_event_sessions(tmp_path, capsys):
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SessionStore.for_repository(repository)
    store.create("session-for-cli", repository_root=repository, model="fake-model")

    assert main(["sessions", "list", str(repository)]) == 0
    assert "session-for-cli" in capsys.readouterr().out

    assert main(["sessions", "show", "session-for-cli", str(repository)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == "session-for-cli"
    assert payload["model"] == "fake-model"
