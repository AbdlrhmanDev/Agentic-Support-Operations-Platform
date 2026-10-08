"""Picks the model and embedding clients for the configured provider."""

from app.agent.llm.base import LLMClient
from app.agent.llm.routing import RoutingLLM
from app.config import Settings
from app.policies.embeddings import Embedder, HashingEmbedder


def _client(settings: Settings) -> LLMClient:
    # Imported here so only the chosen provider's SDK is loaded.
    if settings.llm_provider == "openai":
        from app.agent.llm.openai_client import OpenAILLM

        return OpenAILLM(settings)
    from app.agent.llm.anthropic_client import AnthropicLLM

    return AnthropicLLM(settings)


def build_llm(settings: Settings) -> LLMClient:
    primary = _client(settings)
    if settings.llm_light_model is None:
        return primary
    light = _client(settings.model_copy(update={"llm_model": settings.llm_light_model}))
    return RoutingLLM(primary, light, max_chars=settings.llm_light_max_chars)


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "openai":
        from app.agent.llm.openai_embeddings import OpenAIEmbedder

        return OpenAIEmbedder(settings)
    return HashingEmbedder()
