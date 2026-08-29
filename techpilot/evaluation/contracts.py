"""Structured, versioned contracts for deterministic Runtime replay."""

from __future__ import annotations

import hashlib
import json
import platform
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class ReplayCategory(str, Enum):
    """Failure ownership used by reports instead of one blended score."""

    TOOL = "tool"
    SCHEDULING = "scheduling"
    CONTEXT = "context"
    PERSISTENCE = "persistence"
    CONTRACT = "contract"
    INSTRUCTION = "instruction"


class ReplayTrack(str, Enum):
    """A Fake Runtime replay must never be reported as model stability."""

    RUNTIME = "runtime"
    MODEL = "model"


class ReplayCaseOrigin(str, Enum):
    """Governance class for a case deck, independent from its behavior category."""

    CORE = "core"
    EXTENDED = "extended"
    HOLDOUT = "holdout"
    RUNNER_VALIDATION = "runner-validation"


class ExtendedCaseSource(str, Enum):
    """Only evidence-backed sources may add a public extended case."""

    REAL_DEFECT = "real-defect"
    NEW_CAPABILITY = "new-capability"


@dataclass(frozen=True)
class ExtendedCaseProvenance:
    """The auditable reason a public extended case may enter the suite."""

    source: ExtendedCaseSource
    evidence_id: str
    first_observed_commit: str
    pre_fix_failure: str
    rationale: str
    replaces_case_id: str | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("evidence_id", self.evidence_id),
            ("first_observed_commit", self.first_observed_commit),
            ("pre_fix_failure", self.pre_fix_failure),
            ("rationale", self.rationale),
        ):
            if not value.strip():
                raise ValueError(f"extended case provenance requires {label}")
        if self.replaces_case_id is not None and not self.replaces_case_id.strip():
            raise ValueError("replaces_case_id must be non-empty when provided")

    def to_dict(self) -> dict[str, str | None]:
        return {
            "source": self.source.value,
            "evidence_id": self.evidence_id,
            "first_observed_commit": self.first_observed_commit,
            "pre_fix_failure": self.pre_fix_failure,
            "rationale": self.rationale,
            "replaces_case_id": self.replaces_case_id,
        }


@dataclass(frozen=True)
class ReplayCase:
    """One deterministic, auditable Runtime behavior case."""

    id: str
    suite: str
    category: ReplayCategory
    scenario: str
    input: Mapping[str, Any]
    expected: Mapping[str, Any]
    track: ReplayTrack = ReplayTrack.RUNTIME
    origin: ReplayCaseOrigin = ReplayCaseOrigin.CORE
    provenance: ExtendedCaseProvenance | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if not self.id or self.id != self.id.lower():
            raise ValueError("replay case id must be non-empty lowercase text")
        if not self.suite:
            raise ValueError("replay case suite must not be empty")
        if self.track is not ReplayTrack.RUNTIME:
            raise ValueError("deterministic ReplayCase values must use the runtime track")
        if self.origin is ReplayCaseOrigin.EXTENDED and self.provenance is None:
            raise ValueError("extended ReplayCase values require evidence-backed provenance")
        if self.origin is ReplayCaseOrigin.EXTENDED and not self.suite.startswith("extended-"):
            raise ValueError("extended ReplayCase values must use an extended-* suite")
        if self.origin is not ReplayCaseOrigin.EXTENDED and self.provenance is not None:
            raise ValueError("only extended ReplayCase values may carry extended provenance")

    @property
    def fingerprint(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "suite": self.suite,
            "category": self.category.value,
            "scenario": self.scenario,
            "input": dict(self.input),
            "expected": dict(self.expected),
            "track": self.track.value,
            "origin": self.origin.value,
            "provenance": self.provenance.to_dict() if self.provenance is not None else None,
            "description": self.description,
        }


@dataclass(frozen=True)
class ReplayOutcome:
    """The result of one case, retaining the cause when it did not match."""

    case_id: str
    category: ReplayCategory
    passed: bool
    failure: str | None = None
    observed: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "category": self.category.value,
            "passed": self.passed,
            "failure": self.failure,
            "observed": dict(self.observed),
        }


@dataclass(frozen=True)
class ReplayReport:
    """A report with per-category numerators and denominators."""

    suite: str
    track: ReplayTrack
    cases: tuple[ReplayCase, ...]
    outcomes: tuple[ReplayOutcome, ...]
    git_commit: str
    git_dirty: bool

    def __post_init__(self) -> None:
        if len(self.cases) != len(self.outcomes):
            raise ValueError("replay report must have one outcome per case")
        case_ids = [case.id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("replay report case ids must be unique")

    @property
    def passed(self) -> int:
        return sum(outcome.passed for outcome in self.outcomes)

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def case_set_digest(self) -> str:
        return case_set_digest(self.cases)

    @property
    def category_results(self) -> dict[str, dict[str, int]]:
        results: dict[str, dict[str, int]] = {}
        for category in ReplayCategory:
            selected = [outcome for outcome in self.outcomes if outcome.category is category]
            if selected:
                results[category.value] = {
                    "passed": sum(outcome.passed for outcome in selected),
                    "total": len(selected),
                }
        return results

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "suite": self.suite,
            "track": self.track.value,
            "git_commit": self.git_commit,
            "git_dirty": self.git_dirty,
            "python": platform.python_version(),
            "case_set_digest": self.case_set_digest,
            "passed": self.passed,
            "total": self.total,
            "categories": self.category_results,
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
        }


@dataclass(frozen=True)
class BaselineReference:
    """The immutable facts needed to decide whether two reports are comparable."""

    suite: str
    track: ReplayTrack
    git_commit: str
    case_set_digest: str
    categories: Mapping[str, Mapping[str, int]]

    @classmethod
    def from_report(cls, report: ReplayReport) -> BaselineReference:
        return cls(
            suite=report.suite,
            track=report.track,
            git_commit=report.git_commit,
            case_set_digest=report.case_set_digest,
            categories=report.category_results,
        )

    @classmethod
    def from_path(cls, path: Path) -> BaselineReference:
        payload = json.loads(path.read_text(encoding="utf-8"))
        try:
            baseline = payload["baseline"]
            if not isinstance(baseline, Mapping) or baseline.get("kind") != "baseline-v0":
                raise ValueError("report is not a formal baseline-v0")
            if payload.get("git_dirty") is not False:
                raise ValueError("formal baseline report must have a clean Git worktree")
            if payload.get("passed") != payload.get("total"):
                raise ValueError("formal baseline report must have every case passing")
            track = ReplayTrack(payload["track"])
            categories = payload["categories"]
            if not isinstance(categories, Mapping):
                raise TypeError("categories must be a mapping")
            return cls(
                suite=str(payload["suite"]),
                track=track,
                git_commit=str(payload["git_commit"]),
                case_set_digest=str(payload["case_set_digest"]),
                categories=categories,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"invalid baseline report: {path}") from error

    def compare(self, candidate: ReplayReport) -> BaselineComparison:
        if candidate.suite != self.suite:
            return BaselineComparison(False, "suite_changed", self, candidate)
        if candidate.track is not self.track:
            return BaselineComparison(False, "track_changed", self, candidate)
        if candidate.case_set_digest != self.case_set_digest:
            return BaselineComparison(False, "case_set_changed", self, candidate)
        return BaselineComparison(True, None, self, candidate)


@dataclass(frozen=True)
class BaselineComparison:
    """A comparison that refuses to blend results from different case decks."""

    comparable: bool
    reason: str | None
    baseline: BaselineReference
    candidate: ReplayReport

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "comparable": self.comparable,
            "reason": self.reason,
            "baseline_commit": self.baseline.git_commit,
            "candidate_commit": self.candidate.git_commit,
            "baseline_case_set_digest": self.baseline.case_set_digest,
            "candidate_case_set_digest": self.candidate.case_set_digest,
        }
        if self.comparable:
            result["categories"] = {
                category: {
                    "baseline": dict(self.baseline.categories[category]),
                    "candidate": values,
                }
                for category, values in self.candidate.category_results.items()
            }
        return result


@dataclass(frozen=True)
class ModelEvaluationManifest:
    """Conditions for a real-model run; it intentionally contains no score."""

    provider: str
    model: str
    parameters: Mapping[str, Any]
    suite: str
    case_set_digest: str
    track: ReplayTrack = ReplayTrack.MODEL

    def __post_init__(self) -> None:
        if not self.provider or not self.model or not self.suite:
            raise ValueError("model evaluation manifest requires provider, model, and suite")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"track": self.track.value}


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def case_set_digest(cases: tuple[ReplayCase, ...]) -> str:
    """Return the stable content digest used to freeze one deterministic deck."""

    return _digest([case.to_dict() for case in cases])
