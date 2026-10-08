"""Application settings. Every business limit lives here, never in prompts or nodes."""

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # An empty value, as in `API_KEY=`, means unset rather than an empty string.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    database_url: str = "postgresql+psycopg://support:support@localhost:5434/support_ops"

    # Model. Set LLM_MODEL to one of the provider's models when changing LLM_PROVIDER.
    llm_provider: Literal["openai", "anthropic"] = "openai"
    llm_model: str = "gpt-6.1-sol"
    # Which values a model accepts depends on the model.
    llm_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] = "medium"
    llm_max_tokens: int = 16000
    llm_timeout_seconds: float = 120.0
    openai_api_key: SecretStr | None = None
    # Anthropic only: retry a declined request on a fallback model.
    llm_refusal_fallback: bool = True
    # Model routing. When a light model is set, tickets that are short and ask
    # for no action go to it; everything else goes to LLM_MODEL.
    llm_light_model: str | None = None
    llm_light_max_chars: int = Field(default=400, ge=1)

    # When set, every API route except /health needs this value in X-API-Key.
    api_key: SecretStr | None = None
    # Reviewer name -> personal key, as JSON. When set, an approval can only be
    # decided with one of these keys, and the reviewer recorded is the key's owner.
    reviewer_keys: dict[str, SecretStr] = Field(default_factory=dict)

    # Refund and replacement rules
    auto_refund_threshold: Decimal = Decimal("100.00")
    max_refund_amount: Decimal = Decimal("1000.00")
    refund_window_days: int = 30
    lost_shipment_days: int = 10
    auto_replacement_threshold: Decimal = Decimal("150.00")

    # Agent loop limits
    max_agent_steps: int = Field(default=12, ge=1)
    max_consecutive_tool_failures: int = Field(default=3, ge=1)
    tool_timeout_seconds: float = 10.0
    tool_max_retries: int = Field(default=2, ge=0)
    tool_retry_backoff_seconds: float = 0.2

    # Policy retrieval. Re-run the seed after changing the embedding provider or model.
    policy_top_k: int = Field(default=3, ge=1)
    embedding_provider: Literal["hashing", "openai"] = "hashing"
    embedding_model: str = "text-embedding-3-small"

    # Approval queue order: waiting past the SLA is urgent, a large amount is high.
    approval_sla_minutes: int = Field(default=60, ge=1)
    high_value_approval_amount: Decimal = Decimal("500.00")

    # Observability
    otel_exporter: Literal["none", "console"] = "none"

    # Lets a caller make tools fail on purpose. For evals and tests only.
    allow_fault_injection: bool = False

    @property
    def psycopg_dsn(self) -> str:
        """Connection string for libraries that talk to psycopg directly."""
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
