"""Application services for the initial TechPilot skeleton."""

from ..domain import Plan, Project, Run, Task


class TechPilotApplication:
    """Small in-memory application facade used before persistence exists."""

    def __init__(self) -> None:
        self.projects: dict[str, Project] = {}
        self.tasks: dict[str, Task] = {}
        self.plans: dict[str, Plan] = {}
        self.runs: dict[str, Run] = {}

    def register_project(self, project: Project) -> Project:
        self.projects[project.id] = project
        return project

    def create_task(self, task: Task) -> Task:
        if task.project_id not in self.projects:
            raise KeyError(f"Unknown project: {task.project_id}")
        self.tasks[task.id] = task
        return task

    def save_plan(self, plan: Plan) -> Plan:
        if plan.task_id not in self.tasks:
            raise KeyError(f"Unknown task: {plan.task_id}")
        self.plans[plan.id] = plan
        return plan

    def start_run(self, run: Run) -> Run:
        if run.task_id not in self.tasks:
            raise KeyError(f"Unknown task: {run.task_id}")
        self.runs[run.id] = run
        return run
