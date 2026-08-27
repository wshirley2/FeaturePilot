"""User-owned provider configuration and the first-run setup flow.

This module deliberately keeps credentials outside a repository and outside an
installed package.  It also keeps project ``.env`` values as the lowest
configuration source, rather than importing them into ``os.environ``.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path

from featurepilot.engine.config import Config, default_context_tokens_for_model

_CONFIG_FILENAME = "config.json"


@dataclass(frozen=True)
class UserConfig:
    provider: str
    base_url: str | None
    model: str
    api_key: str

    @classmethod
    def from_mapping(cls, data: object) -> UserConfig:
        if not isinstance(data, dict):
            raise TypeError("configuration must be a JSON object")
        required = ("provider", "base_url", "model", "api_key")
        if any(name not in data for name in required):
            raise ValueError("configuration is missing required fields")
        provider = data["provider"]
        base_url = data["base_url"]
        model = data["model"]
        api_key = data["api_key"]
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("configuration provider is invalid")
        if base_url is not None and not isinstance(base_url, str):
            raise ValueError("configuration base_url is invalid")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("configuration model is invalid")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("configuration api_key is invalid")
        return cls(provider.strip(), base_url.strip() if base_url else None, model.strip(), api_key)

    def to_mapping(self) -> dict[str, str | None]:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "api_key": self.api_key,
        }


@dataclass(frozen=True)
class UserConfigState:
    config: UserConfig | None
    problem: str | None = None

    @property
    def is_complete(self) -> bool:
        return self.config is not None


def user_config_directory() -> Path:
    """Return the OS-level configuration directory, never a package directory."""

    override = os.getenv("FEATUREPILOT_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        appdata = os.getenv("APPDATA")
        if appdata:
            return Path(appdata) / "FeaturePilot"
        return Path.home() / "AppData" / "Roaming" / "FeaturePilot"
    return Path.home() / ".config" / "featurepilot"


def user_config_path() -> Path:
    return user_config_directory() / _CONFIG_FILENAME


def load_user_config() -> UserConfigState:
    path = user_config_path()
    if not path.exists():
        return UserConfigState(None, "配置文件不存在")
    try:
        return UserConfigState(UserConfig.from_mapping(json.loads(path.read_text(encoding="utf-8"))))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        return UserConfigState(None, f"配置文件损坏或缺字段：{error}")


def save_user_config(config: UserConfig) -> Path:
    """Atomically save the local configuration without logging its contents."""

    directory = user_config_directory()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / _CONFIG_FILENAME
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, prefix=".config-", suffix=".tmp", delete=False
        ) as handle:
            json.dump(config.to_mapping(), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            temporary = Path(handle.name)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    if os.name != "nt":
        path.chmod(0o600)
    return path


def run_setup_wizard(*, repairing: bool = False, input_fn: Callable[[str], str] = input) -> UserConfig:
    """Collect provider settings without ever echoing the API key."""

    current = load_user_config().config
    title = "配置修复向导" if repairing else "首次配置向导"
    print(f"FeaturePilot {title}")
    print("配置仅保存到当前用户目录；API Key 不会显示在终端。")
    provider = input_fn(f"Provider [{current.provider if current else 'openai'}]: ").strip() or (current.provider if current else "openai")
    base_url = input_fn(f"Base URL [{(current.base_url or '') if current else ''}]: ").strip()
    model = input_fn(f"Model [{current.model if current else 'gpt-5.5'}]: ").strip() or (current.model if current else "gpt-5.5")
    api_key = getpass("API Key: ").strip()
    if not api_key and current is not None:
        api_key = current.api_key
    config = UserConfig(provider=provider, base_url=base_url or None, model=model, api_key=api_key)
    # Validate before changing an existing configuration.
    UserConfig.from_mapping(config.to_mapping())
    save_user_config(config)
    print(f"配置已保存到 {user_config_directory()}。")
    return config


def resolve_runtime_config(
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> Config:
    """Resolve CLI > environment > user configuration > project .env values."""

    project = _project_dotenv_values()
    user = load_user_config().config

    def source(*names: str) -> str | None:
        for name in names:
            if os.environ.get(name):
                return os.environ[name]
        if user is not None:
            user_value = {
                "provider": user.provider,
                "base_url": user.base_url,
                "model": user.model,
                "api_key": user.api_key,
            }
            field = {
                "FEATUREPILOT_PROVIDER": "provider",
                "OPENAI_BASE_URL": "base_url",
                "FEATUREPILOT_BASE_URL": "base_url",
                "FEATUREPILOT_MODEL": "model",
                "FEATUREPILOT_API_KEY": "api_key",
                "OPENAI_API_KEY": "api_key",
                "DEEPSEEK_API_KEY": "api_key",
            }.get(names[0])
            if field and user_value[field]:
                return user_value[field]
        for name in names:
            if project.get(name):
                return project[name]
        return None

    resolved_model = model or source("FEATUREPILOT_MODEL") or "gpt-5.5"
    resolved_api_key = api_key or source("FEATUREPILOT_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY") or ""
    resolved_base_url = base_url or source("OPENAI_BASE_URL", "FEATUREPILOT_BASE_URL")
    provider = source("FEATUREPILOT_PROVIDER") or "openai"
    max_context = os.getenv("FEATUREPILOT_MAX_CONTEXT") or project.get("FEATUREPILOT_MAX_CONTEXT")
    return Config(
        model=resolved_model,
        api_key=resolved_api_key,
        base_url=resolved_base_url,
        max_tokens=int(os.getenv("FEATUREPILOT_MAX_TOKENS") or project.get("FEATUREPILOT_MAX_TOKENS") or "4096"),
        temperature=float(os.getenv("FEATUREPILOT_TEMPERATURE") or project.get("FEATUREPILOT_TEMPERATURE") or "0"),
        max_context_tokens=int(max_context) if max_context else default_context_tokens_for_model(resolved_model),
        provider=provider,
    )


def _project_dotenv_values() -> dict[str, str]:
    if os.getenv("FEATUREPILOT_LOAD_DOTENV", "1").lower() in {"0", "false", "no", "off"}:
        return {}
    try:
        from dotenv import dotenv_values
    except ImportError:
        return {}
    current = Path.cwd()
    home = Path.home()
    while True:
        candidate = current / ".env"
        if candidate.exists():
            return {key: value for key, value in dotenv_values(candidate).items() if value is not None}
        if current == home or current == current.parent:
            return {}
        current = current.parent
