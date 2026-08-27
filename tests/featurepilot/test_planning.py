from pathlib import Path

import pytest
from pydantic import ValidationError

from featurepilot.advanced.planning import PlanGenerator, PlanStore, PlanValidator
from featurepilot.domain import Plan, Task
from featurepilot.repository import ContextSelector, RepositoryIndex, RepositoryProfiler

BENCHMARK_ROOT = Path(__file__).parents[2] / "benchmarks" / "cli_data_tool"


def test_plan_generator_creates_a_valid_benchmark_plan():
    profile = RepositoryProfiler().profile(BENCHMARK_ROOT)
    index = RepositoryIndex.build(BENCHMARK_ROOT)
    candidates = ContextSelector(index).select(
        "add export json format and update README",
        limit=5,
    )
    task = Task(
        project_id="project",
        description="Add JSON export format and update README",
        acceptance_criteria=["Default text output remains unchanged"],
    )

    plan = PlanGenerator().generate(task, profile, candidates)
    validation = PlanValidator().validate(plan, profile)

    assert validation.is_valid
    assert "src/cli_data_tool/cli.py" in plan.read_files
    assert "tests/test_export.py" in plan.modify_files
    assert ["python", "-m", "pytest", "-q"] in plan.validation_commands


def test_plan_validator_rejects_unsafe_paths_and_unknown_commands():
    profile = RepositoryProfiler().profile(BENCHMARK_ROOT)
    task = Task(project_id="project", description="Invalid plan")
    plan = PlanGenerator().generate(task, profile, [])
    plan.read_files = ["../../outside.py"]
    plan.modify_files = ["src/unknown.py"]
    plan.validation_commands = [["python", "-c", "dangerous"]]

    validation = PlanValidator().validate(plan, profile)

    assert not validation.is_valid
    assert any("unsafe path" in error for error in validation.errors)
    assert any("outside the profile" in error for error in validation.errors)
    assert any("not allowed" in error for error in validation.errors)


def test_plan_store_versions_and_decisions(tmp_path):
    store = PlanStore(tmp_path / "plans")
    first = store.save_draft(
        Plan(task_id="same-task", summary="First draft", steps=["Read a file"]),
        BENCHMARK_ROOT,
    )
    second = store.save_draft(
        Plan(task_id="same-task", summary="Second draft", steps=["Read a file"]),
        BENCHMARK_ROOT,
    )

    assert first.version == 1
    assert second.version == 2
    assert first.reference == "first-draft-v1"
    assert second.reference == "first-draft-v2"

    # A fast or coarse clock may give successive drafts the same timestamp;
    # PlanStore must still return the newer version first.
    first.created_at = second.created_at
    store._write(first)
    store._write(second)
    assert [record.version for record in store.list(repository=BENCHMARK_ROOT)] == [2, 1]

    approved = store.approve(first.reference)
    rejected = store.reject(second.reference, "需要缩小修改范围")

    assert approved.status == "approved"
    assert rejected.status == "rejected"
    assert rejected.decision_reason == "需要缩小修改范围"
    with pytest.raises(ValueError, match="Only draft plans"):
        store.approve(first.id)


def test_plan_schema_rejects_unknown_fields_and_wrong_types():
    with pytest.raises(ValidationError):
        Plan.from_dict(
            {
                "task_id": "task",
                "summary": "Schema validation",
                "steps": "This must be a list",
                "unexpected": "not allowed",
            }
        )


def test_plan_generator_limits_an_explicit_readme_path_to_the_repository_root(tmp_path):
    (tmp_path / "README.md").write_text("# Root\n", encoding="utf-8")
    nested = tmp_path / "examples" / "README.md"
    nested.parent.mkdir()
    nested.write_text("# Nested\n", encoding="utf-8")
    noisy = tmp_path / ".tmp" / "README.md"
    noisy.parent.mkdir()
    noisy.write_text("# Generated\n", encoding="utf-8")
    profile = RepositoryProfiler().profile(tmp_path)
    index = RepositoryIndex.build(tmp_path)
    candidates = ContextSelector(index).select(
        "在 README.md 末尾增加一行 M1-P verification",
        limit=10,
    )
    task = Task(project_id=str(tmp_path), description="在 README.md 末尾增加一行 M1-P verification")

    plan = PlanGenerator().generate(task, profile, candidates)

    assert plan.read_files == ["README.md"]
    assert plan.modify_files == ["README.md"]
