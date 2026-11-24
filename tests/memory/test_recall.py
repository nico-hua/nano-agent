"""Hybrid recall using real SQLite and deterministic embeddings."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from nanobot.memory import MemoryStore
from nanobot.memory.recall import MemoryRecall, keywords
from nanobot.memory.models import MemoryChanges
from nanobot.providers import EmbeddingProvider, HumanMessage, ProviderError
from .fixtures import addition, seed


class FakeEmbeddings(EmbeddingProvider):
    model_key = "fake-v1"

    def __init__(self):
        self.unavailable = False
        self.calls = []

    async def embed(self, texts):
        self.calls.append(list(texts))
        if self.unavailable:
            raise ProviderError("Unavailable")
        return [(1., 0.) if "缓存" in text or "旧值" in text else (0., 1.) for text in texts]


class RecallTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name)
        self.store = MemoryStore(self.workspace)
        self.provider = FakeEmbeddings()
        self.recall = MemoryRecall(self.store, self.provider)

    async def test_semantic_and_lexical_candidates_are_independent(self):
        seed(self.store, "先更新数据库再删除缓存")
        seed(self.store, "Implement in runner.py")
        self.assertEqual(self.store.search_keywords("缓存"), [1])
        self.assertEqual(self.store.search_keywords("怎样避免读到旧值"), [])
        await self.recall.index_missing()
        result = await self.recall.recall("runner.py 怎样避免读到旧值")
        self.assertIn("缓存", result)
        self.assertIn("runner.py", result)
        self.assertEqual(await self.recall.recall("谢谢"), "")
        self.provider.unavailable = True
        self.assertIn("runner.py", await self.recall.recall("runner.py"))

    async def test_replaced_vectors_are_excluded_before_backfill(self):
        seed(self.store, "Redis 缓存")
        await self.recall.index_missing()
        seed(self.store, "本地缓存", replace_id=1)
        self.assertEqual(await self.recall.recall("旧值"), "")
        self.assertIn("本地缓存", await self.recall.recall("缓存"))
        await self.recall.index_missing()
        self.assertIn("本地缓存", await self.recall.recall("旧值"))
        self.assertNotIn("Redis", await self.recall.recall("旧值"))
        count = len(self.provider.calls)
        await self.recall.index_missing()
        self.assertEqual(len(self.provider.calls), count)


    async def test_fts_backfills_existing_memories_and_prioritizes_full_filenames(self):
        seed(self.store, "runner.py 决策")
        seed(self.store, "runner 其他 py")
        with closing(sqlite3.connect(self.workspace / "memory/memory.sqlite3")) as db, db:
            db.execute("DROP TABLE memories_fts")
            db.execute("PRAGMA user_version = 0")
        reopened = MemoryStore(self.workspace)
        self.assertEqual(reopened.search_keywords("runner.py", limit=1), [1])
        self.assertEqual(reopened.search_keywords("决策"), [1])

    def test_search_terms_keep_whole_chinese_words_and_code_identifiers(self):
        terms = keywords("请问，如何了解数据库？Runner.py 与 read_file")
        for expected in ("了解", "数据库", "runner.py", "runner", "py", "read_file", "read", "file"):
            self.assertIn(expected, terms)
        self.assertTrue({"请问", "如何", "解数"}.isdisjoint(terms))
        self.assertEqual(keywords("谢谢！请继续。"), [])

    def test_upgrade_replaces_old_bigram_index_with_terms_from_active_memories(self):
        seed(self.store, "了解数据库")
        with closing(sqlite3.connect(self.workspace / "memory/memory.sqlite3")) as db, db:
            db.execute("UPDATE memories_fts SET terms = '解数 数据 据库' WHERE rowid = 1")
            db.execute("PRAGMA user_version = 1")
        reopened = MemoryStore(self.workspace)
        self.assertEqual(reopened.search_keywords("了解"), [1])
        with closing(sqlite3.connect(self.workspace / "memory/memory.sqlite3")) as db:
            terms = db.execute("SELECT terms FROM memories_fts WHERE rowid = 1").fetchone()[0].split()
        self.assertIn("数据库", terms)
        self.assertNotIn("解数", terms)

    async def test_indexing_batches_and_skips_unconfigured_or_incompatible_models(self):
        for i in range(11):
            seed(self.store, f"缓存策略 {i}")
        await self.recall.index_missing()
        self.assertEqual([len(batch) for batch in self.provider.calls], [10, 1])
        self.provider.model_key = "fake-v2"
        self.assertEqual(await self.recall.recall("旧值"), "")
        self.assertEqual(await MemoryRecall(self.store).recall("旧值"), "")
        self.provider.unavailable = True
        await self.recall.index_missing()
        self.assertTrue(self.store.missing_embeddings("fake-v2", 1))
        self.provider.unavailable = False
        await self.recall.index_missing()
        self.assertIn("缓存", await self.recall.recall("旧值"))


    async def test_preferences_share_budget_and_are_not_duplicated_on_a_keyword_hit(self):
        store = self.store
        text = "默认用中文回答"
        event = store.append_event("a", (HumanMessage(text),))
        store.apply(event, MemoryChanges.model_validate({"operations": [
            addition(text) | {"scope": "user", "kind": "preference"},
        ]}))
        seed(store, "Project runs on Redis")
        self.assertEqual(await self.recall.recall("谢谢"), f"- [preference] {text}")
        self.assertEqual((await self.recall.recall("中文")).count(text), 1)
        self.assertEqual(await self.recall.recall("Redis", token_budget=1), "")

    async def test_rollback_restores_search_results_after_failed_correction(self):
        seed(self.store, "Redis")
        event = self.store.append_event("a", (HumanMessage("Use Valkey"),))
        changes = MemoryChanges.model_validate({"operations": [
            addition("Valkey") | {"op": "replace", "target_id": 1},
            addition("invalid", 4),
        ]})
        with self.assertRaises(ValueError):
            self.store.apply(event, changes)
        self.assertEqual(self.store.search_keywords("Redis"), [1])
        self.assertEqual(self.store.search_keywords("Valkey"), [])
