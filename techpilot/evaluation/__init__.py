"""Offline Runtime Replay and model-evaluation contracts."""

from .cases import (
    CORE_V0_CASE_COUNT,
    CORE_V0_CASE_SET_DIGEST,
    RUNNER_VALIDATION_CASE_COUNT,
    build_core_v0_cases,
    build_runner_validation_cases,
)
from .contracts import (
    BaselineComparison,
    BaselineReference,
    ExtendedCaseProvenance,
    ExtendedCaseSource,
    ModelEvaluationManifest,
    ReplayCase,
    ReplayCaseOrigin,
    ReplayCategory,
    ReplayReport,
    ReplayTrack,
    case_set_digest,
)
from .runner import ReplayRunner

__all__ = [
    "CORE_V0_CASE_COUNT",
    "CORE_V0_CASE_SET_DIGEST",
    "RUNNER_VALIDATION_CASE_COUNT",
    "BaselineComparison",
    "BaselineReference",
    "ExtendedCaseProvenance",
    "ExtendedCaseSource",
    "ModelEvaluationManifest",
    "ReplayCase",
    "ReplayCaseOrigin",
    "ReplayCategory",
    "ReplayReport",
    "ReplayRunner",
    "ReplayTrack",
    "build_core_v0_cases",
    "build_runner_validation_cases",
    "case_set_digest",
]
