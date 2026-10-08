"""OpenAI text embeddings for policy search."""

from functools import cached_property

import httpx2
import openai

from app.config import Settings
from app.db.models import EMBEDDING_DIM
from app.policies.embeddings import EmbeddingError


class OpenAIEmbedder:
    def __init__(self, settings: Settings, http_client: httpx2.AsyncClient | None = None) -> None:
        self._settings = settings
        self._http_client = http_client

    @cached_property
    def _client(self) -> openai.AsyncOpenAI:
        # Built on first use so the API can start without credentials.
        key = self._settings.openai_api_key
        return openai.AsyncOpenAI(
            api_key=key.get_secret_value() if key else None,
            timeout=self._settings.llm_timeout_seconds,
            http_client=self._http_client,
        )

    async def embed(self, text: str) -> list[float]:
        try:
            response = await self._client.embeddings.create(
                model=self._settings.embedding_model,
                input=text,
                # Shortened by the API to fit the vector column.
                dimensions=EMBEDDING_DIM,
                encoding_format="float",
            )
        except openai.OpenAIError as exc:
            raise EmbeddingError(f"Could not embed the text: {exc}") from exc
        vector = response.data[0].embedding
        if len(vector) != EMBEDDING_DIM:
            raise EmbeddingError(
                f"Expected {EMBEDDING_DIM} dimensions from the embedding model, got {len(vector)}."
            )
        return vector
