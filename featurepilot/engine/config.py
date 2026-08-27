"""Configuration - env vars and defaults."""

import os
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_CONTEXT_TOKENS = 128_000
_MODEL_CONTEXT_TOKENS = {
    "deepseek-v4-pro": 1_000_000,
    "deepseek-v4-flash": 1_000_000,
}


def default_context_tokens_for_model(model: str) -> int:
    """Return the known provider window, with a conservative generic fallback."""

    return _MODEL_CONTEXT_TOKENS.get(model, _DEFAULT_CONTEXT_TOKENS)


def _load_dotenv():
    """Load .env from cwd, walking up to home dir. No-op if python-dotenv missing."""
    try:
        from dotenv import load_dotenv
        # search cwd first, then parent dirs up to ~
        env_path = Path(".env")
        if not env_path.exists():
            cur = Path.cwd()
            home = Path.home()
            while cur != home and cur != cur.parent:
                candidate = cur / ".env"
                if candidate.exists():
                    env_path = candidate
                    break
                cur = cur.parent
        load_dotenv(env_path, override=False)
    except ImportError:
        pass  # python-dotenv not installed, silently skip


@dataclass
class Config:
    model: str = "gpt-5.5"
    api_key: str = ""
    base_url: str | None = None
    max_tokens: int = 4096
    temperature: float = 0.0
    max_context_tokens: int = _DEFAULT_CONTEXT_TOKENS
    provider: str = "openai"

    @classmethod
    def from_env(cls) -> "Config":
        # Load .env if present (won't override existing env vars). Tests and
        # embedding applications can opt out when they need strict isolation.
        load_dotenv = os.getenv("FEATUREPILOT_LOAD_DOTENV", "1").lower()
        if load_dotenv not in {"0", "false", "no", "off"}:
            _load_dotenv()
        # pick up common env vars automatically
        api_key = (
            os.getenv("FEATUREPILOT_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or os.getenv("DEEPSEEK_API_KEY")
            or ""
        )
        model = os.getenv("FEATUREPILOT_MODEL", "gpt-5.5")
        configured_context = os.getenv("FEATUREPILOT_MAX_CONTEXT")
        return cls(
            model=model,
            api_key=api_key,
            base_url=os.getenv("OPENAI_BASE_URL") or os.getenv("FEATUREPILOT_BASE_URL"),
            max_tokens=int(os.getenv("FEATUREPILOT_MAX_TOKENS", "4096")),
            temperature=float(os.getenv("FEATUREPILOT_TEMPERATURE", "0")),
            max_context_tokens=(
                int(configured_context)
                if configured_context is not None
                else default_context_tokens_for_model(model)
            ),
            provider=os.getenv("FEATUREPILOT_PROVIDER", "openai"),
        )
