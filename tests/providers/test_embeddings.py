"""OpenAI-compatible embeddings protocol and normalization."""

import json
import unittest

import httpx
from openai import AsyncOpenAI
from types import SimpleNamespace
from unittest.mock import AsyncMock

from nanobot.config import EmbeddingConfig
from nanobot.providers import OpenAICompatEmbeddingProvider, ProviderError


class EmbeddingProviderTest(unittest.IsolatedAsyncioTestCase):
    async def test_restores_input_order_and_rejects_malformed_batches(self):
        create = AsyncMock()
        client = SimpleNamespace(embeddings=SimpleNamespace(create=create))
        config = EmbeddingConfig(api_base="https://example.test/v1", api_key="test", model="text-embedding-v4")
        provider = OpenAICompatEmbeddingProvider(config, client=client)
        def item(index, vector):
            return SimpleNamespace(index=index, embedding=vector)
        create.return_value = SimpleNamespace(data=[item(1, [0., 2.]), item(0, [3., 0.])])
        self.assertEqual(await provider.embed(["a", "b"]), [(1., 0.), (0., 1.)])
        create.assert_awaited_once_with(model="text-embedding-v4", input=["a", "b"], encoding_format="float")
        for rows in ([item(0, [0., 0.])], [], [item(0, [float("nan")])], [item(1, [1.])]):
            with self.subTest(rows=rows):
                create.return_value = SimpleNamespace(data=rows)
                with self.assertRaises(ProviderError):
                    await provider.embed(["a"])


    async def test_sdk_posts_embeddings_using_the_configured_endpoint(self):
        def endpoint(request):
            self.assertEqual(request.url.path, "/compatible-mode/v1/embeddings")
            self.assertEqual(request.headers["Authorization"], "Bearer test-key")
            self.assertEqual(json.loads(request.content), {
                "model": "text-embedding-v4", "input": ["缓存"], "encoding_format": "float",
            })
            return httpx.Response(200, json={
                "object": "list", "model": "text-embedding-v4",
                "data": [{"object": "embedding", "index": 0, "embedding": [3., 4.]}],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            })

        config = EmbeddingConfig(api_key="test-key", model="text-embedding-v4",
                                 api_base="https://example.test/compatible-mode/v1")
        async with AsyncOpenAI(base_url=config.api_base, api_key=config.api_key,
                               http_client=httpx.AsyncClient(transport=httpx.MockTransport(endpoint))) as client:
            provider = OpenAICompatEmbeddingProvider(config, client=client)
            self.assertEqual(await provider.embed(["缓存"]), [(0.6, 0.8)])
