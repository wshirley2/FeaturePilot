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
