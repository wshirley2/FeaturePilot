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


def test_repository_profile_extracts_fastapi_routes_and_import_graph(tmp_path):
    package = tmp_path / "app"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "service.py").write_text("def list_items():\n    return []\n", encoding="utf-8")
    (package / "api.py").write_text(
        "from fastapi import APIRouter\n"
        "from app.service import list_items\n\n"
        "router = APIRouter()\n\n"
        '@router.get("/items")\n'
        "def get_items():\n"
        "    return list_items()\n",
        encoding="utf-8",
    )

    profile = RepositoryProfiler().profile(tmp_path)

    assert "fastapi" in profile.frameworks
    assert profile.routes["app/api.py"] == ["GET /items"]
    assert profile.import_graph["app/api.py"] == ["app/service.py"]
