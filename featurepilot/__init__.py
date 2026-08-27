"""FeaturePilot: a repo-aware coding agent workspace."""

__version__ = "0.1.0"

from .runtime.application import FeaturePilotApplication
from .runtime.contracts import (
    RuntimeMode,
    RuntimeResultScope,
    RuntimeResultStatus,
    TaskRuntimeIdentity,
    TaskRuntimePaths,
    TaskRuntimeResult,
)

__all__ = [
    "FeaturePilotApplication",
    "RuntimeMode",
    "RuntimeResultScope",
    "RuntimeResultStatus",
    "TaskRuntimeIdentity",
    "TaskRuntimePaths",
    "TaskRuntimeResult",
    "__version__",
]
