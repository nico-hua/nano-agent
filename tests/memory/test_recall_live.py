"""Opt-in Qwen evaluation on synthetic memories in a temporary workspace."""

import os
import tempfile
import unittest

from nanobot.config import load_file_config
from nanobot.memory import MemoryRecall, MemoryStore
from nanobot.providers import OpenAICompatEmbeddingProvider
from .fixtures import seed


@unittest.skipUnless(os.getenv("RUN_QWEN_LIVE_TESTS") == "1", "requires an explicit live Qwen run")
class QwenRecallLiveTest(unittest.IsolatedAsyncioTestCase):
    async def test_paraphrase_retrieves_the_confirmed_decision(self):
        settings = load_file_config().memory.embedding
        self.assertTrue(settings and settings.api_key, "Configure memory.embedding in nanobot.json")
        provider = OpenAICompatEmbeddingProvider(settings)
        self.addAsyncCleanup(provider.close)
        with tempfile.TemporaryDirectory() as workspace:
            store = MemoryStore(workspace)
            decision = "团队决定先写数据库，再删除 Redis 缓存，以免读取过期数据。"
            seed(store, decision)
            seed(store, "文档格式统一使用 Markdown。")
            seed(store, "应用默认使用深色主题。")
            recall = MemoryRecall(store, provider)
            await recall.index_missing()
            self.assertFalse(store.missing_embeddings(provider.model_key, 1))
            query = "怎样避免用户看到旧值？"
            self.assertEqual(store.search_keywords(query), [])
            result = await recall.recall(query)
            self.assertEqual(result.splitlines(), [f"- [decision] {decision}"])
            self.assertEqual(await recall.recall("火星的半径有多大？"), "")
