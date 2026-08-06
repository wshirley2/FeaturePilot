from pathlib import Path

from featurepilot.repository import ContextSelector, RepositoryIndex, RepositoryProfiler

BENCHMARK_ROOT = Path(__file__).parents[2] / "benchmarks" / "cli_data_tool"


def test_repository_profiler_finds_python_project_parts():
    profile = RepositoryProfiler().profile(BENCHMARK_ROOT)

    assert profile.language == "python"
    assert "pyproject.toml" in profile.config_files
    assert "tests/test_export.py" in profile.test_files
    assert "src/cli_data_tool/cli.py" in profile.entrypoints
    assert ["python", "-m", "pytest", "-q"] in profile.validation_commands
    assert not any(path.endswith(".egg-info/PKG-INFO") for path in profile.files)


def test_context_selector_explains_export_task_files():
    index = RepositoryIndex.build(BENCHMARK_ROOT)
    candidates = ContextSelector(index).select("add export json format and update README", limit=5)
    paths = {candidate.path for candidate in candidates}

    assert "src/cli_data_tool/cli.py" in paths
    assert "tests/test_export.py" in paths
    assert "README.md" in paths
    assert all(candidate.reasons for candidate in candidates)
