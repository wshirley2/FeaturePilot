import json
from pathlib import Path

from featurepilot.cli import main

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
