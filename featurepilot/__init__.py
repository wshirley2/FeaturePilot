"""FeaturePilot: a repo-aware coding agent workspace."""

__version__ = "0.1.0"

from .application import FeaturePilotApplication
from .runtime_contracts import (
    RuntimeMode,
    RuntimeResultScope,
    RuntimeResultStatus,
    TaskRuntimeIdentity,
    TaskRuntimeResult,
)

__all__ = [
    "FeaturePilotApplication",
    "RuntimeMode",
    "RuntimeResultScope",
    "RuntimeResultStatus",
    "TaskRuntimeIdentity",
    "TaskRuntimeResult",
    "__version__",
]
