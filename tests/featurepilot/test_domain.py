from featurepilot.domain import Plan, Project, Run, Task


def test_domain_models_can_serialize_and_restore():
    project = Project(name="demo", path="/tmp/demo")
    task = Task(
        project_id=project.id,
        description="Add JSON export",
        acceptance_criteria=["JSON is valid"],
    )
    plan = Plan(
        task_id=task.id,
        summary="Add a JSON output branch.",
        steps=["Read the CLI", "Add serialization", "Run tests"],
        modify_files=["src/app/cli.py"],
    )
    run = Run(task_id=task.id, status="succeeded", result={"tests": "passed"})

    assert Project.from_dict(project.to_dict()) == project
    assert Task.from_dict(task.to_dict()) == task
    assert Plan.from_dict(plan.to_dict()) == plan
    assert Run.from_dict(run.to_dict()) == run


def test_task_and_run_reject_unknown_states():
    try:
        Task(project_id="project", description="bad", task_type="unknown")
    except ValueError as error:
        assert "Unsupported task type" in str(error)
    else:
        raise AssertionError("Unknown task type should be rejected")

    try:
        Run(task_id="task", status="unknown")
    except ValueError as error:
        assert "Unsupported run status" in str(error)
    else:
        raise AssertionError("Unknown run status should be rejected")
