"""CoreCoder - Minimal AI coding agent inspired by Claude Code's architecture."""

__version__ = "0.4.0"

from corecoder.agent import Agent
from corecoder.config import Config
from corecoder.events import EventSink, RuntimeEvent, RuntimeEventType
from corecoder.llm import LLM
from corecoder.permissions import (
    DenyPermissionPrompt,
    PermissionAction,
    PermissionDecision,
    PermissionEffect,
    PermissionGrantScope,
    PermissionManager,
    PermissionPolicy,
    PermissionPrompt,
    PermissionRequest,
)
from corecoder.runtime_control import CancellationToken, RuntimeLimits
from corecoder.tools import ALL_TOOLS

__all__ = [
    "ALL_TOOLS",
    "LLM",
    "Agent",
    "CancellationToken",
    "Config",
    "DenyPermissionPrompt",
    "EventSink",
    "PermissionAction",
    "PermissionDecision",
    "PermissionEffect",
    "PermissionGrantScope",
    "PermissionManager",
    "PermissionPolicy",
    "PermissionPrompt",
    "PermissionRequest",
    "RuntimeEvent",
    "RuntimeEventType",
    "RuntimeLimits",
    "__version__",
]
