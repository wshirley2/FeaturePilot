"""Local persistence for reviewable Plan versions."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..domain import Plan, PlanRecord, Task

_PLAN_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
_SHORT_PLAN_ID_PATTERN = re.compile(r"^[a-f0-9]{4,31}$")
_PLAN_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class PlanStore:
    """Save Plan records outside the target repository's source files."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def save_draft(
        self,
        plan: Plan,
        repository: Path,
        task: Task | None = None,
        name: str | None = None,
    ) -> PlanRecord:
        """Persist a new draft and assign its next version within the task."""

        repository_path = str(repository.resolve())
        repository_records = self.list(repository=repository)
        task_records = [record for record in repository_records if record.plan.task_id == plan.task_id]
        existing_versions = [record.version for record in task_records]
        plan_name = self._choose_name(plan, task_records, repository_records, name)
        record = PlanRecord(
            plan=plan,
            repository=repository_path,
            version=max(existing_versions, default=0) + 1,
            task=task,
            name=plan_name,
        )
        self._write(record)
        return record

    def list(self, repository: Path | None = None) -> list[PlanRecord]:
        """Return saved plans, newest first, optionally for one repository."""

        if not self.directory.is_dir():
            return []
        repository_path = str(repository.resolve()) if repository else None
        records: list[PlanRecord] = []
        for path in self.directory.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                record = PlanRecord.from_dict(data)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                raise ValueError(f"Could not read saved plan {path.name}: {error}") from error
            if repository_path is None or record.repository == repository_path:
                records.append(record)
        # ISO timestamps normally preserve creation order, but two drafts can
        # share the same timestamp on a fast filesystem or coarse clock.
        # Version is the deterministic tie-breaker for successive drafts.
        return sorted(records, key=lambda record: (record.created_at, record.version), reverse=True)

    def load(self, reference: str) -> PlanRecord:
        """Load one saved Plan by its name/version or full/short generated id."""

        path = self._resolve_path(reference)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ValueError(f"Saved plan not found: {reference}") from error
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            raise ValueError(f"Could not read saved plan {reference}: {error}") from error
        return PlanRecord.from_dict(data)

    def approve(self, reference: str) -> PlanRecord:
        record = self.load(reference)
        record.decide("approved")
        self._write(record)
        return record

    def reject(self, reference: str, reason: str) -> PlanRecord:
        record = self.load(reference)
        record.decide("rejected", reason)
        self._write(record)
        return record

    def _write(self, record: PlanRecord) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(record.to_dict(), ensure_ascii=False, indent=2)
        self._path_for(record.id).write_text(f"{payload}\n", encoding="utf-8")

    def _path_for(self, plan_id: str) -> Path:
        if not _PLAN_ID_PATTERN.fullmatch(plan_id):
            raise ValueError("Plan id must be a 32-character lowercase hexadecimal UUID")
        return self.directory / f"{plan_id}.json"

    def _resolve_path(self, reference: str) -> Path:
        if _PLAN_ID_PATTERN.fullmatch(reference):
            return self._path_for(reference)
        matches = [
            record
            for record in self.list()
            if record.reference == reference
            or (_SHORT_PLAN_ID_PATTERN.fullmatch(reference) and record.id.startswith(reference))
        ]
        if not matches:
            raise ValueError(f"Saved plan not found: {reference}")
        if len(matches) > 1:
            options = ", ".join(record.reference for record in matches)
            raise ValueError(f"Plan reference is ambiguous: {reference}; use one of: {options}")
        return self._path_for(matches[0].id)

    @staticmethod
    def _choose_name(
        plan: Plan,
        task_records: list[PlanRecord],
        repository_records: list[PlanRecord],
        requested_name: str | None,
    ) -> str:
        existing_name = task_records[0].name if task_records and task_records[0].name else None
        if requested_name:
            plan_name = PlanStore._normalize_name(requested_name)
            if existing_name and plan_name != existing_name:
                raise ValueError(f"Task already uses plan name {existing_name!r}; keep that name when regenerating")
        else:
            plan_name = existing_name or PlanStore._default_name(plan)

        conflicting_tasks = {
            record.plan.task_id
            for record in repository_records
            if record.name == plan_name and record.plan.task_id != plan.task_id
        }
        if conflicting_tasks:
            if requested_name:
                raise ValueError(f"Plan name {plan_name!r} is already used by another task in this repository")
            plan_name = f"{plan_name}-{plan.task_id[:8]}"
        return plan_name

    @staticmethod
    def _normalize_name(value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
        if not _PLAN_NAME_PATTERN.fullmatch(normalized):
            raise ValueError("Plan name must contain at least one English letter or number")
        return normalized

    @staticmethod
    def _default_name(plan: Plan) -> str:
        words = re.findall(r"[a-zA-Z0-9]+", plan.summary.lower())
        return "-".join(words[:4]) or f"task-{plan.task_id[:8]}"
