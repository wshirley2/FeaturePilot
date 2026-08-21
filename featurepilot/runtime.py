"""Central assembly for FeaturePilot Chat and future Managed Runs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from corecoder.agent import Agent, ToolExecutor
from corecoder.config import Config
from corecoder.events import EventSink
from corecoder.llm import LLM, LiteLLM
from corecoder.permissions import DenyPermissionPrompt, PermissionManager, PermissionPrompt
from corecoder.tools import get_tool
from corecoder.tools.base import Tool

from .chat_executor import RepositoryToolExecutor
from .permissions import ChatPermissionPolicy
from .repository import RepositoryProfiler
from .repository.profiler import RepositoryProfile

ProviderFactory = Callable[[Config], object]
_CHAT_TOOL_NAMES = ("read_file", "glob", "grep", "edit_file", "write_file", "bash", "now")


@dataclass(frozen=True)
class RuntimeBootstrapInput:
    repository: Path
    event_sink: EventSink
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    tool_executor: ToolExecutor | None = None
    tools: list[Tool] | None = None
    system_context: str | None = None
    permission_mode: str | None = None
    permission_prompt: PermissionPrompt | None = None


@dataclass
class ChatRuntime:
    repository: Path
    config: Config
    agent: Agent
    tools: list[Tool]
    profile: RepositoryProfile | None
    profile_warning: str | None
    permission_mode: str = "repository reads allowed; writes and commands policy-gated"
    permission_manager: PermissionManager | None = None


class RuntimeBootstrap:
    """Build the complete runtime once so CLI commands do not duplicate it."""

    def __init__(
        self,
        *,
        provider_factory: ProviderFactory | None = None,
        profiler: RepositoryProfiler | None = None,
    ):
        self.provider_factory = provider_factory
        self.profiler = profiler or RepositoryProfiler()

    def build(self, inputs: RuntimeBootstrapInput) -> ChatRuntime:
        repository = inputs.repository.resolve()
        if not repository.is_dir():
            raise ValueError(f"Repository directory does not exist: {inputs.repository}")

        config = Config.from_env()
        if inputs.model:
            config.model = inputs.model
        if inputs.base_url:
            config.base_url = inputs.base_url
        if inputs.api_key:
            config.api_key = inputs.api_key

        if self.provider_factory is None and not config.api_key:
            raise ValueError(
                "No API key found. Set OPENAI_API_KEY, DEEPSEEK_API_KEY, or CORECODER_API_KEY."
            )

        profile = None
        profile_warning = None
        try:
            profile = self.profiler.profile(repository)
        except Exception as error:
            profile_warning = f"Repository profile unavailable: {error}"

        tools = (
            inputs.tools
            if inputs.tools is not None
            else [tool for name in _CHAT_TOOL_NAMES if (tool := get_tool(name)) is not None]
        )
        provider = self._build_provider(config)
        repository_context = _repository_summary(profile, profile_warning)
        system_context = "\n\n".join(
            part for part in (repository_context, inputs.system_context) if part
        )
        permission_manager = None
        tool_executor = inputs.tool_executor
        if tool_executor is None:
            permission_manager = PermissionManager(
                ChatPermissionPolicy(profile.validation_commands if profile else ()),
                inputs.permission_prompt or DenyPermissionPrompt(),
            )
            tool_executor = RepositoryToolExecutor(repository, permission_manager)
        agent = Agent(
            llm=provider,
            tools=tools,
            max_context_tokens=config.max_context_tokens,
            tool_executor=tool_executor,
            event_sink=inputs.event_sink,
            working_directory=str(repository),
            assistant_name="FeaturePilot",
            system_context=system_context,
        )
        return ChatRuntime(
            repository=repository,
            config=config,
            agent=agent,
            tools=tools,
            profile=profile,
            profile_warning=profile_warning,
            permission_mode=(
                inputs.permission_mode
                or "repository reads allowed; writes and commands policy-gated"
            ),
            permission_manager=permission_manager,
        )

    def _build_provider(self, config: Config):
        if self.provider_factory is not None:
            return self.provider_factory(config)
        provider_class = LiteLLM if config.provider == "litellm" else LLM
        return provider_class(
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )


def _repository_summary(profile: RepositoryProfile | None, warning: str | None) -> str:
    if profile is None:
        return warning or "Repository profile is unavailable; inspect files with tools as needed."

    def values(items: list[str]) -> str:
        return ", ".join(items[:8]) or "(none detected)"

    commands = [" ".join(command) for command in profile.validation_commands]
    return "\n".join([
        f"Repository root: {profile.root}",
        f"Language: {profile.language}",
        f"Frameworks: {values(profile.frameworks)}",
        f"Entrypoints: {values(profile.entrypoints)}",
        f"Tests: {values(profile.test_files)}",
        f"Validation commands: {values(commands)}",
        "This is a lightweight profile. Read detailed files only when needed.",
    ])
