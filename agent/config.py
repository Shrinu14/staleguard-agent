"""
The one place where configuration and secrets are loaded.

How it works:
  - pydantic-settings reads values from environment variables and the .env
    file, then validates and type-converts them (e.g. "1.00" -> 1.0).
  - get_settings() is wrapped in lru_cache(maxsize=1), so .env is parsed
    exactly once. Every later call returns the same Settings object.
  - Secrets use SecretStr: printing or logging them shows '**********',
    so a key can't leak through a stray print or traceback.

Every other module does:
    from agent.config import get_settings
    settings = get_settings()
"""

import getpass
from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agent.lookups import ModelRole


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Provider keys (GEMINI_API_KEY, ...) sit in .env for Bifrost's use.
        # "ignore" keeps them out of this object: the agent never holds them.
        extra="ignore",
        env_ignore_empty=True,  # "KEY=" in .env counts as not set (None)
        frozen=True,  # settings can't be modified after loading
    )

    # Bifrost
    bifrost_base_url: str = "http://localhost:8080/v1"
    bifrost_vk_default: SecretStr | None = None
    bifrost_vk_local: SecretStr | None = None

    # Tavily
    tavily_api_key: SecretStr | None = None

    # Models (Bifrost "provider/model" ids)
    model_local: str = "ollama/qwen2.5-coder:7b"
    model_planner: str = ""
    model_editor: str = ""
    model_judge: str = ""
    # Comma-separated "provider/model" ids Bifrost tries if the primary fails.
    model_fallbacks: str = ""

    # Budget (per user, per calendar month UTC)
    user_budget_usd: float = Field(default=1.0, gt=0)
    staleguard_user: str = ""  # whose budget this is; empty = OS login name

    @model_validator(mode="before")
    @classmethod
    def _fill_empty_roles(cls, data: dict) -> dict:
        """Empty model roles fall back to the local model, so the project
        works with Ollama alone before any cloud provider is configured."""
        local = data.get("model_local") or "ollama/qwen2.5-coder:7b"
        for role in ("model_planner", "model_editor", "model_judge"):
            if not data.get(role):
                data[role] = local
        return data

    def model_for(self, role: ModelRole) -> str:
        """Return the model id configured for a role."""
        return getattr(self, f"model_{role.value}")

    @property
    def fallbacks(self) -> tuple[str, ...]:
        """MODEL_FALLBACKS as a clean tuple, e.g. ("groq/x", "ollama/y")."""
        return tuple(m.strip() for m in self.model_fallbacks.split(",") if m.strip())

    @property
    def user_id(self) -> str:
        return self.staleguard_user or getpass.getuser()

    def virtual_key(self, *, local_only: bool = False) -> SecretStr | None:
        """The Bifrost virtual key to authenticate with."""
        return self.bifrost_vk_local if local_only else self.bifrost_vk_default


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
