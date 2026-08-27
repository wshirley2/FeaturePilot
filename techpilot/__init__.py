"""TechPilot: a repo-aware coding agent workspace."""

__version__ = "0.1.0"

from .runtime.application import TechPilotApplication
from .runtime.contracts import (
    RuntimeMode,
    RuntimeResultScope,
    RuntimeResultStatus,
    TaskRuntimeIdentity,
    TaskRuntimePaths,
    TaskRuntimeResult,
)

__all__ = [
    "RuntimeMode",
    "RuntimeResultScope",
    "RuntimeResultStatus",
    "TaskRuntimeIdentity",
    "TaskRuntimePaths",
    "TaskRuntimeResult",
    "TechPilotApplication",
    "__version__",
]
