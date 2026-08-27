import json

from featurepilot.cli import main
from featurepilot.config.user import (
    UserConfig,
    load_user_config,
    resolve_runtime_config,
    run_setup_wizard,
    save_user_config,
)


def test_first_run_enters_setup_wizard_and_then_uses_tui(tmp_path, monkeypatch):
    monkeypatch.setenv("FEATUREPILOT_CONFIG_DIR", str(tmp_path / "user-config"))
    calls = []

    class FakeTui:
        event_sink = object()
        permission_prompt = object()

        def bind_runtime(self, _runtime):
            calls.append("bound")

        def run(self):
            return 7

    class Bootstrap:
        def build(self, _inputs):
            return object()

    monkeypatch.setattr("featurepilot.cli.run_setup_wizard", lambda repairing: calls.append(("wizard", repairing)))
    monkeypatch.setattr("featurepilot.cli.FeaturePilotTui", FakeTui)
    monkeypatch.setattr("featurepilot.cli.RuntimeBootstrap", Bootstrap)
    monkeypatch.setattr("featurepilot.cli.tui_supported", lambda: True)
    monkeypatch.setattr("featurepilot.cli.sys.stdin", type("Input", (), {"isatty": lambda self: False})())

    assert main([]) == 7
    assert calls == [("wizard", False), "bound"]


def test_complete_user_config_does_not_repeat_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("FEATUREPILOT_CONFIG_DIR", str(tmp_path / "user-config"))
    save_user_config(UserConfig("openai", None, "configured-model", "secret"))
    calls = []

    class FakeTui:
        event_sink = object()
        permission_prompt = object()

        def bind_runtime(self, _runtime):
            calls.append("bound")

        def run(self):
            return 4

    monkeypatch.setattr("featurepilot.cli.run_setup_wizard", lambda repairing: calls.append(("wizard", repairing)))
    monkeypatch.setattr("featurepilot.cli.FeaturePilotTui", FakeTui)
    monkeypatch.setattr("featurepilot.cli.RuntimeBootstrap", lambda: type("Bootstrap", (), {"build": lambda self, inputs: object()})())
    monkeypatch.setattr("featurepilot.cli.tui_supported", lambda: True)
    monkeypatch.setattr("featurepilot.cli.sys.stdin", type("Input", (), {"isatty": lambda self: False})())

    assert main([]) == 4
    assert calls == ["bound"]


def test_damaged_user_config_enters_repair_wizard(tmp_path, monkeypatch):
    directory = tmp_path / "user-config"
    directory.mkdir()
    (directory / "config.json").write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("FEATUREPILOT_CONFIG_DIR", str(directory))
    calls = []

    class FakeTui:
        event_sink = object()
        permission_prompt = object()

        def bind_runtime(self, _runtime):
            pass

        def run(self):
            return 3

    monkeypatch.setattr("featurepilot.cli.run_setup_wizard", lambda repairing: calls.append(repairing))
    monkeypatch.setattr("featurepilot.cli.FeaturePilotTui", FakeTui)
    monkeypatch.setattr("featurepilot.cli.RuntimeBootstrap", lambda: type("Bootstrap", (), {"build": lambda self, inputs: object()})())
    monkeypatch.setattr("featurepilot.cli.tui_supported", lambda: True)
    monkeypatch.setattr("featurepilot.cli.sys.stdin", type("Input", (), {"isatty": lambda self: False})())

    assert main([]) == 3
    assert calls == [True]


def test_default_entry_falls_back_to_text_cli_without_a_tty(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FEATUREPILOT_CONFIG_DIR", str(tmp_path / "user-config"))
    save_user_config(UserConfig("openai", None, "configured-model", "secret"))

    class FakeChatSession:
        def __init__(self, _runtime, **_kwargs):
            pass

        def run(self):
            return 5

    monkeypatch.setattr("featurepilot.cli.ChatSession", FakeChatSession)
    monkeypatch.setattr("featurepilot.cli.RuntimeBootstrap", lambda: type("Bootstrap", (), {"build": lambda self, inputs: object()})())
    monkeypatch.setattr("featurepilot.cli.tui_supported", lambda: False)

    assert main([]) == 5
    assert "falling back" in capsys.readouterr().err


def test_runtime_configuration_priority_is_cli_then_environment_then_user_then_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FEATUREPILOT_CONFIG_DIR", str(tmp_path / "user-config"))
    save_user_config(UserConfig("litellm", "https://user.example/v1", "user-model", "user-key"))
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=dotenv-key\nOPENAI_BASE_URL=https://dotenv.example/v1\nFEATUREPILOT_MODEL=dotenv-model\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("FEATUREPILOT_MODEL", raising=False)
    monkeypatch.delenv("FEATUREPILOT_LOAD_DOTENV", raising=False)

    user = resolve_runtime_config()
    assert (user.api_key, user.base_url, user.model, user.provider) == (
        "user-key", "https://user.example/v1", "user-model", "litellm"
    )

    monkeypatch.setenv("OPENAI_API_KEY", "environment-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://environment.example/v1")
    monkeypatch.setenv("FEATUREPILOT_MODEL", "environment-model")
    environment = resolve_runtime_config()
    assert (environment.api_key, environment.base_url, environment.model) == (
        "environment-key", "https://environment.example/v1", "environment-model"
    )

    cli = resolve_runtime_config(model="cli-model", base_url="https://cli.example/v1", api_key="cli-key")
    assert (cli.api_key, cli.base_url, cli.model) == ("cli-key", "https://cli.example/v1", "cli-model")


def test_invalid_user_configuration_is_not_treated_as_complete(tmp_path, monkeypatch):
    monkeypatch.setenv("FEATUREPILOT_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"model": "missing-fields"}), encoding="utf-8")

    state = load_user_config()

    assert not state.is_complete
    assert "缺字段" in state.problem


def test_setup_wizard_saves_all_fields_without_echoing_api_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FEATUREPILOT_CONFIG_DIR", str(tmp_path / "user-config"))
    answers = iter(["litellm", "https://provider.example/v1", "provider-model"])
    monkeypatch.setattr("featurepilot.config.user.getpass", lambda _prompt: "hidden-api-key")

    config = run_setup_wizard(input_fn=lambda _prompt: next(answers))

    assert config == UserConfig("litellm", "https://provider.example/v1", "provider-model", "hidden-api-key")
    assert load_user_config().config == config
    assert "hidden-api-key" not in capsys.readouterr().out
