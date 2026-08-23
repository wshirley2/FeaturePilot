"""Human-readable review report generation for Managed Runs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .changes import ChangeArtifact
from .domain import PlanRecord, Run
from .execution import ValidationArtifact
from .runtime import TaskRuntime
from .workspace import Workspace


@dataclass(frozen=True, slots=True)
class RunMetrics:
    """Provider usage and end-to-end elapsed time captured for a report."""

    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: float | None
    duration_seconds: float

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, object]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "duration_seconds": self.duration_seconds,
        }


class ReportService:
    """Render one deterministic Markdown review report beside a Run Workspace."""

    def generate(
        self,
        *,
        record: PlanRecord,
        run: Run,
        workspace: Workspace,
        response: str,
        validation: ValidationArtifact | None,
        validation_path: Path | None,
        events_path: Path,
        changes: ChangeArtifact,
        patch_path: Path,
        metrics: RunMetrics,
        session_path: Path | None = None,
        output_path: Path | None = None,
    ) -> Path:
        run_directory = workspace.path.resolve().parent
        report_path = (output_path or run_directory / "report.md").resolve()
        if report_path != run_directory / "report.md":
            raise ValueError("Managed Run report must be stored at <run>/report.md")
        content = _render_report(
            record=record,
            run=run,
            workspace=workspace,
            response=response,
            validation=validation,
            validation_path=validation_path,
            events_path=events_path,
            changes=changes,
            patch_path=patch_path,
            metrics=metrics,
            session_path=session_path,
        )
        _atomic_write_report(report_path, content, run.id)
        return report_path


def collect_run_metrics(runtime: TaskRuntime | None, duration_seconds: float) -> RunMetrics:
    provider = runtime.agent.llm if runtime is not None else None
    prompt_tokens = int(getattr(provider, "total_prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(provider, "total_completion_tokens", 0) or 0)
    estimated_cost = getattr(provider, "estimated_cost", None)
    if callable(estimated_cost):
        estimated_cost = estimated_cost()
    if not isinstance(estimated_cost, (int, float)):
        estimated_cost = None
    return RunMetrics(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        estimated_cost_usd=float(estimated_cost) if estimated_cost is not None else None,
        duration_seconds=round(duration_seconds, 6),
    )


def _render_report(
    *,
    record: PlanRecord,
    run: Run,
    workspace: Workspace,
    response: str,
    validation: ValidationArtifact | None,
    validation_path: Path | None,
    events_path: Path,
    changes: ChangeArtifact,
    patch_path: Path,
    metrics: RunMetrics,
    session_path: Path | None = None,
) -> str:
    task = record.task.description if record.task else record.plan.summary
    lines = [
        "# FeaturePilot Managed Run Report",
        "",
        "## Summary",
        "",
        f"- Task: {task}",
        f"- Plan: {record.reference}",
        f"- Run: {run.id}",
        f"- Status: **{run.status}**",
        f"- Workspace: `{workspace.path}`",
        f"- Events: `{events_path}`",
        f"- Session: `{session_path}`" if session_path else "- Session: not produced",
        f"- Patch: `{patch_path}`",
        f"- Validation: `{validation_path}`" if validation_path else "- Validation: not produced",
        "",
        "## Plan",
        "",
        *(_bullet(step) for step in record.plan.steps),
        "",
        "## Agent Summary",
        "",
        _quote(response or "No Agent summary was produced."),
        "",
        "## Changed Files",
        "",
        f"Total: {len(changes.files)} files, +{changes.additions} / -{changes.deletions}",
        "",
        "| Status | File | Planned | Binary | + | - |",
        "| --- | --- | --- | --- | ---: | ---: |",
    ]
    if changes.files:
        lines.extend(
            "| {status} | `{path}` | {planned} | {binary} | {additions} | {deletions} |".format(
                status=change.status,
                path=_table_text(change.path),
                planned="yes" if change.planned else "**no**",
                binary="yes" if change.binary else "no",
                additions=change.additions,
                deletions=change.deletions,
            )
            for change in changes.files
        )
    else:
        lines.append("| unchanged | *(none)* | yes | no | 0 | 0 |")

    lines.extend([
        "",
        "## Validation",
        "",
        f"Overall: **{validation.status}**" if validation else "Overall: **not run**",
        "",
        "| Status | Command | Exit | Duration |",
        "| --- | --- | ---: | ---: |",
    ])
    if validation and validation.commands:
        lines.extend(
            f"| {result.status} | `{_table_text(' '.join(result.argv))}` | "
            f"{result.exit_code if result.exit_code is not None else '-'} | "
            f"{result.duration_seconds:.3f}s |"
            for result in validation.commands
        )
    else:
        lines.append("| *(none)* | *(none)* | - | - |")

    lines.extend([
        "",
        "## Usage",
        "",
        f"- Prompt tokens: {metrics.prompt_tokens}",
        f"- Completion tokens: {metrics.completion_tokens}",
        f"- Total tokens: {metrics.total_tokens}",
        "- Estimated cost: " + (
            f"${metrics.estimated_cost_usd:.6f} USD"
            if metrics.estimated_cost_usd is not None
            else "unavailable"
        ),
        f"- Total duration: {metrics.duration_seconds:.3f}s",
        "",
        "## Failure",
        "",
    ])
    error_type = run.result.get("error_type")
    error = run.result.get("error")
    lines.append(
        f"- {error_type}: {error}"
        if error_type or error
        else "- No failure recorded."
    )

    risks = list(record.plan.risks)
    if changes.out_of_plan_files:
        risks.append("Out-of-plan files changed: " + ", ".join(changes.out_of_plan_files))
    binary_files = [change.path for change in changes.files if change.binary]
    if binary_files:
        risks.append("Binary changes require manual review: " + ", ".join(binary_files))
    if validation and validation.status != "passed":
        risks.append("One or more approved validation commands did not pass.")
    lines.extend(["", "## Risks", ""])
    lines.extend(_bullet(risk) for risk in risks or ["No additional risks recorded."])
    return "\n".join(lines) + "\n"


def _bullet(value: str) -> str:
    return f"- {value}"


def _quote(value: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in value.splitlines())


def _table_text(value: str) -> str:
    return value.replace("|", "\\|").replace("`", "\\`").replace("\n", " ")


def _atomic_write_report(path: Path, content: str, run_id: str) -> None:
    temporary = path.parent / f".report-{run_id}.tmp"
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
