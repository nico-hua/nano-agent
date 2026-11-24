"""Transactional memory updates against a real workspace database."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from nanobot.memory import MemoryStore
from nanobot.memory.models import MemoryChanges
from nanobot.providers import AIMessage, HumanMessage

from .fixtures import addition, seed


class MemoryStoreTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name)
        self.store = MemoryStore(self.workspace)

    def test_cross_session_correction_and_retraction_preserve_sources_after_restart(self):
        seed(self.store, "Use Redis.")
        event = self.store.append_event("other", (HumanMessage("Use local cache."),))
        change = addition("Use local cache.") | {"op": "replace", "target_id": 1}
        changes = MemoryChanges.model_validate({"operations": [change]})
        self.store.apply(event, changes)
        self.store.apply(event, changes)
        store = MemoryStore(self.workspace)
        self.assertIsNone(store.next_event())
        self.assertIn("Use local cache.", store.read(include_sources=True))
        self.assertIn("事件 2 · 消息 0（会话 other）", store.read(include_sources=True))
        self.assertNotIn("Redis", store.read())
        self.assertEqual(len(store.active_memories()), 1)
        event = store.append_event("other", (AIMessage("Still applicable?"), HumanMessage("Withdraw that decision.")))
        store.apply(event, MemoryChanges.model_validate({"operations": [
            {"op": "retract", "target_id": 2, "message_offset": 1}
        ]}))
        self.assertEqual(store.read(), "")
        with closing(sqlite3.connect(self.workspace / "memory" / "memory.sqlite3")) as db:
            rows = db.execute("""
                SELECT ended_message_offset, messages FROM memories
                JOIN memory_events ON memory_events.id = memories.ended_event_id ORDER BY memories.id
            """).fetchall()
        self.assertEqual([json.loads(snapshot)[offset]["content"] for offset, snapshot in rows],
                         ["Use local cache.", "Withdraw that decision."])

    def test_event_snapshot_identifies_original_even_after_session_index_reuse(self):
        seed(self.store, "Use Redis.")
        seed(self.store, "User also prefers Chinese.")
        output = self.store.read(include_sources=True)
        self.assertIn("事件 1 · 消息 0（会话 chat）", output)
        self.assertIn("事件 2 · 消息 0（会话 chat）", output)
        with closing(sqlite3.connect(self.workspace / "memory" / "memory.sqlite3")) as db:
            first = db.execute("SELECT messages FROM memory_events WHERE id = 1").fetchone()
        self.assertEqual(json.loads(first[0])[0]["content"], "Use Redis.")

    def test_credential_bearing_turn_leaves_no_durable_memory_event(self):
        event = self.store.append_event("chat", (
            HumanMessage("Set api_key=example-test-key"),
            AIMessage("I have saved example-test-key."),
        ))
        self.assertIsNone(event)
        self.assertIsNone(self.store.next_event())

    def test_bad_target_or_evidence_rolls_back_entire_event(self):
        event = self.store.append_event("chat", (HumanMessage("Confirmed."), AIMessage("Suggestion.")))
        for second in (addition("second", 1), addition("second", -1), addition("second", 2),
                       {"op": "retract", "target_id": 100, "message_offset": 0},
                       {"op": "retract", "target_id": 1, "message_offset": 0}):
            with self.subTest(second=second):
                changes = MemoryChanges.model_validate({"operations": [addition("first"), second]})
                with self.assertRaises(ValueError):
                    self.store.apply(event, changes)
                self.assertEqual(self.store.read(), "")
                self.assertEqual(self.store.next_event(), event)

    def test_conflicting_replacement_rolls_back_prior_writes_and_pending_status(self):
        seed(self.store, "Use Redis.")
        seed(self.store, "Use local cache.")
        event = self.store.append_event("new", (HumanMessage("Use local cache."),))
        changes = MemoryChanges.model_validate({"operations": [
            addition("Use another cache."),
            addition("Use local cache.") | {"op": "replace", "target_id": 1},
        ]})
        with self.assertRaises(ValueError):
            self.store.apply(event, changes)
        self.assertEqual([m.text for m in self.store.active_memories()], ["Use local cache.", "Use Redis."])
        self.assertEqual(self.store.next_event(), event)

    def test_duplicate_add_is_idempotent_and_prompt_budget_keeps_whole_items(self):
        seed(self.store, "Use Redis.")
        seed(self.store, "Use Redis.")
        self.assertEqual(len(self.store.active_memories()), 1)
        seed(self.store, "A" * 1000)
        self.assertIn("Use Redis.", self.store.read(token_budget=30))
        self.assertNotIn("AAAA", self.store.read(token_budget=30))
