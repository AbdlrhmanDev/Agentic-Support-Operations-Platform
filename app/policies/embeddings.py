"""Text embeddings for policy search.

`HashingEmbedder` is the default. It needs no model download or API key and
is deterministic, which suits a small policy corpus and repeatable evals.
A learned model is selected with EMBEDDING_PROVIDER; see `app/agent/llm/`.

Stored vectors and query vectors must come from the same embedder, so run
`python -m app.seed` again after changing the provider or model.
"""

import hashlib
import math
import re
from itertools import pairwise
from typing import Protocol

from app.db.models import EMBEDDING_DIM

_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "do",
        "for",
        "from",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "is",
        "it",
        "my",
        "of",
        "on",
        "or",
        "our",
        "that",
        "the",
        "their",
        "this",
        "to",
        "was",
        "we",
        "what",
        "when",
        "will",
        "with",
        "you",
        "your",
    ]
)


class EmbeddingError(Exception):
    """The embedding service could not produce a vector."""


class Embedder(Protocol):
    async def embed(self, text: str) -> list[float]: ...


def _stem(token: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(token) > len(suffix) + 2 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def tokenize(text: str) -> list[str]:
    return [_stem(t) for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS]


class HashingEmbedder:
    """Feature-hashed bag of words and bigrams, L2-normalised."""

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self._dim = dim

    async def embed(self, text: str) -> list[float]:
        tokens = tokenize(text)
        features = tokens + [f"{a}_{b}" for a, b in pairwise(tokens)]
        vector = [0.0] * self._dim
        for feature in features:
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self._dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            # pgvector cannot take the cosine of a zero vector.
            vector[0] = 1.0
            return vector
        return [v / norm for v in vector]
