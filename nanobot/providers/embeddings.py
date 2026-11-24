"""Provider interface and OpenAI-compatible adapter for text embeddings."""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from hashlib import sha256
from math import hypot, isfinite

from openai import AsyncOpenAI, OpenAIError

from ..config import EmbeddingConfig
from .base import ProviderError


class EmbeddingProvider(ABC):
    """Return unit vectors in input order, in the space identified by model_key."""

    model_key: str

    @abstractmethod
    async def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """Embed a batch or raise ProviderError if the batch cannot be used."""


class OpenAICompatEmbeddingProvider(EmbeddingProvider):
    def __init__(self, config: EmbeddingConfig, *, client: AsyncOpenAI | None = None) -> None:
        self.model = config.model
        identity = f"{config.type}\n{config.api_base.rstrip('/')}\n{config.model}"
        self.model_key = sha256(identity.encode()).hexdigest()
        self._client = client or AsyncOpenAI(
            api_key=config.api_key, base_url=config.api_base,
            timeout=config.request_timeout_seconds, max_retries=config.max_retries,
        )

    async def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        if not texts:
            return []
        try:
            response = await self._client.embeddings.create(
                model=self.model, input=list(texts), encoding_format="float",
            )
            rows = sorted(response.data, key=lambda row: row.index)
            if [row.index for row in rows] != list(range(len(texts))):
                raise ValueError("Invalid response indices")
            vectors = []
            for row in rows:
                length = hypot(*row.embedding)
                if not isfinite(length) or length == 0 or (vectors and len(row.embedding) != len(vectors[0])):
                    raise ValueError("Invalid vector")
                vectors.append(tuple(value / length for value in row.embedding))
            return vectors
        except (OpenAIError, ValueError, TypeError, AttributeError, OverflowError) as exc:
            raise ProviderError("Embedding request failed or returned invalid vectors") from exc

    async def close(self) -> None:
        await self._client.close()
