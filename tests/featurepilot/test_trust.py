from io import StringIO

from rich.console import Console

from featurepilot.trust import WorkspaceTrustStore, confirm_workspace_access


def test_workspace_trust_gate_accepts_enter_and_shows_canonical_path(tmp_path):
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=200)
    store = WorkspaceTrustStore(tmp_path / "trusted_workspaces.json")

    assert confirm_workspace_access(tmp_path, console=console, input_fn=lambda _prompt: "", trust_store=store) is True
    rendered = output.getvalue()
    assert "Workspace trust" in rendered
    assert "Accessing workspace:" in rendered
    assert str(tmp_path.resolve()) in rendered
    assert "..." not in rendered
    assert "FeaturePilot will be able to read, edit, and execute files here." in rendered


def test_workspace_trust_gate_accepts_number_and_rejects_number(tmp_path):
    console = Console(file=StringIO(), force_terminal=False, color_system=None)
    accepted_store = WorkspaceTrustStore(tmp_path / "accepted.json")
    rejected_store = WorkspaceTrustStore(tmp_path / "rejected.json")
    assert confirm_workspace_access(tmp_path, console=console, input_fn=lambda _prompt: "1", trust_store=accepted_store) is True
    assert confirm_workspace_access(tmp_path, console=console, input_fn=lambda _prompt: "2", trust_store=rejected_store) is False


def test_workspace_trust_gate_reprompts_invalid_input(tmp_path):
    answers = iter(["maybe", "yes"])
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    store = WorkspaceTrustStore(tmp_path / "trusted_workspaces.json")

    assert confirm_workspace_access(tmp_path, console=console, input_fn=lambda _prompt: next(answers), trust_store=store) is True
    assert "Please choose 1 to continue or 2 to exit." in output.getvalue()


def test_workspace_trust_clears_only_real_terminal_after_acceptance(tmp_path):
    class FakeConsole:
        is_terminal = True

        def __init__(self) -> None:
            self.cleared = False
            self.messages: list[str] = []

        def print(self, message, *args, **kwargs) -> None:
            self.messages.append(str(message))

        def clear(self) -> None:
            self.cleared = True

    console = FakeConsole()
    store = WorkspaceTrustStore(tmp_path / "trusted_workspaces.json")

    assert confirm_workspace_access(tmp_path, console=console, input_fn=lambda _prompt: "1", trust_store=store) is True
    assert console.cleared


def test_workspace_trust_is_reused_across_process_like_store_instances(tmp_path):
    trust_file = tmp_path / "user-config" / "trusted_workspaces.json"
    first_store = WorkspaceTrustStore(trust_file)
    first_output = StringIO()
    first_console = Console(file=first_output, force_terminal=False, color_system=None)

    assert confirm_workspace_access(tmp_path, console=first_console, input_fn=lambda _prompt: "1", trust_store=first_store)
    assert trust_file.exists()

    second_output = StringIO()
    second_console = Console(file=second_output, force_terminal=False, color_system=None)
    second_store = WorkspaceTrustStore(trust_file)
    assert confirm_workspace_access(
        tmp_path,
        console=second_console,
        input_fn=lambda _prompt: (_ for _ in ()).throw(AssertionError("trusted workspace should not prompt")),
        trust_store=second_store,
    )
    assert second_output.getvalue() == ""


def test_corrupt_workspace_trust_registry_fails_closed(tmp_path):
    trust_file = tmp_path / "trusted_workspaces.json"
    trust_file.write_text("not json", encoding="utf-8")
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    store = WorkspaceTrustStore(trust_file)

    assert confirm_workspace_access(tmp_path, console=console, input_fn=lambda _prompt: "2", trust_store=store) is False
    assert "Workspace trust" in output.getvalue()
