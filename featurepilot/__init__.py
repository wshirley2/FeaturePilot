"""FeaturePilot: a repo-aware coding agent workspace."""

__version__ = "0.1.0"

from .application import FeaturePilotApplication
from .runtime_contracts import RuntimeMode, TaskRuntimeIdentity

__all__ = [
    "FeaturePilotApplication",
    "RuntimeMode",
    "TaskRuntimeIdentity",
    "__version__",
]
