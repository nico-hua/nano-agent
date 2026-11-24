"""Structured extraction, ordered recovery, and request budgets."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from nanobot.memory import MemoryConsolidator, MemoryEventConsumer, MemoryStore
from nanobot.providers import AIMessage, HumanMessage, LLMProvider, LLMResponse, ToolMessage
from nanobot.providers.base import schema_instruction
from nanobot.session.tokens import estimate_message_tokens, estimate_messages_tokens

from .fixtures import addition, seed


class ConsolidationProvider(LLMProvider):
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []
        self.request_costs = []

    async def complete(self, messages, *, response_schema):
        self.requests.append((json.loads(messages[-1].content), response_schema))
        self.request_costs.append(estimate_messages_tokens(messages) + estimate_message_tokens(schema_instruction(response_schema)))
        return next(self.responses)

    async def stream(self, *args, **kwargs):
        raise AssertionError("Unexpected stream")


class MemoryConsolidatorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = MemoryStore(directory.name)
        self.database = Path(directory.name) / "memory" / "memory.sqlite3"

    async def consume(self, provider, **kwargs):
        consumer = MemoryEventConsumer(self.store, MemoryConsolidator(provider, self.store, **kwargs))
        consumer.wake()
        await consumer.wait()

    async def test_processes_one_turn_per_request_with_latest_active_candidates(self):
        self.store.append_event("a", (HumanMessage("Use Redis."),))
        self.store.append_event("b", (AIMessage("Which cache?"), AIMessage("Hidden", is_visible=False), HumanMessage("Switch to local cache.")))
        provider = ConsolidationProvider([LLMResponse(content=json.dumps({"operations": [op]})) for op in (
            addition("Use Redis."), addition("Use local cache.", 1) | {"op": "replace", "target_id": 1}
        )])
        await self.consume(provider)
        self.assertIsNone(self.store.next_event())
        self.assertNotIn("Redis", self.store.read())
        self.assertIn("事件 2 · 消息 1（会话 b）", self.store.read(include_sources=True))
        (first, _), (second, schema) = provider.requests
        self.assertEqual(first["active_memories"], [])
        self.assertEqual(second["active_memories"], [{"id": 1, "kind": "decision", "text": "Use Redis."}])
        self.assertEqual([m["message_offset"] for m in second["messages"]], [0, 1])
        self.assertIn("operations", schema["properties"])

    async def test_invalid_responses_remain_pending_until_explicit_empty_success(self):
        event = self.store.append_event("a", (HumanMessage("hello"),))
        empty = '{"operations":[]}'
        for response in (LLMResponse(content=""), LLMResponse(content="{}"), LLMResponse(error="failed"),
                         LLMResponse(content=empty, finish_reason="max_tokens"),
                         LLMResponse(content=empty, finish_reason="length"),
                         LLMResponse(content='{"operations":[{"op":"unknown"}]}')):
            await self.consume(ConsolidationProvider([response]))
            self.assertEqual(self.store.next_event(), event)
        await self.consume(ConsolidationProvider([LLMResponse(content=empty)]))
        self.assertIsNone(self.store.next_event())

    async def test_oversized_turn_and_candidates_preserve_evidence_and_queue_order(self):
        seed(self.store, "Use Redis.")
        seed(self.store, "A" * 16000)
        tool = ToolMessage("log " * 20000, tool_call_id="call")
        self.store.append_event("b", (HumanMessage("Check the cache"), tool, HumanMessage("Replace Redis with local cache.")))
        self.store.append_event("c", (HumanMessage("Use Python."),))
        provider = ConsolidationProvider([LLMResponse(content=json.dumps({"operations": [op]})) for op in (
            addition("Use local cache.", 2) | {"op": "replace", "target_id": 1}, addition("Use Python.")
        )])
        await self.consume(provider, input_token_budget=2000)
        self.assertIsNone(self.store.next_event())
        self.assertLessEqual(max(provider.request_costs), 2000)
        first, second = (request for request, _ in provider.requests)
        self.assertEqual([m["message_offset"] for m in first["messages"]], [0, 2])
        self.assertEqual([m["id"] for m in first["active_memories"]], [1])
        self.assertIn("Use local cache.", [m["text"] for m in second["active_memories"]])
        with closing(sqlite3.connect(self.database)) as db:
            snapshot, = db.execute("SELECT messages FROM memory_events WHERE session_key = 'b'").fetchone()
        self.assertEqual(json.loads(snapshot)[1]["content"], tool.content)

    async def test_oversized_user_message_preserves_both_ends_and_short_followup(self):
        text = "开头" + '"\\中文' * 10000 + "结尾"
        self.store.append_event("b", (HumanMessage(text), HumanMessage("Use Chinese.")))
        provider = ConsolidationProvider([LLMResponse(content='{"operations":[]}')])
        await self.consume(provider, input_token_budget=2000)
        self.assertIsNone(self.store.next_event())
        self.assertLessEqual(provider.request_costs[0], 2000)
        excerpt, followup = provider.requests[0][0]["messages"]
        self.assertEqual((excerpt["message_offset"], followup["message_offset"]), (0, 1))
        self.assertTrue(excerpt["content"].startswith("开头"))
        self.assertTrue(excerpt["content"].endswith("结尾"))
        self.assertTrue(excerpt["truncated"])
        self.assertEqual(followup["content"], "Use Chinese.")

    async def test_hidden_memory_target_cannot_be_replaced(self):
        seed(self.store, "A" * 16000)
        event = self.store.append_event("b", (HumanMessage("Replace the choice."),))
        op = addition("New choice.") | {"op": "replace", "target_id": 1}
        provider = ConsolidationProvider([LLMResponse(content=json.dumps({"operations": [op]}))])
        await self.consume(provider, input_token_budget=2000)
        self.assertEqual(provider.requests[0][0]["active_memories"], [])
        self.assertEqual(self.store.next_event(), event)

    async def test_omitted_user_message_is_not_valid_evidence(self):
        event = self.store.append_event("b", tuple(HumanMessage("x" * 120) for _ in range(100)))
        op = addition("An unsupported decision.", offset=0)
        provider = ConsolidationProvider([LLMResponse(content=json.dumps({"operations": [op]}))])
        await self.consume(provider, input_token_budget=2000)
        shown = provider.requests[0][0]["messages"]
        self.assertTrue(shown)
        self.assertNotIn(0, [item["message_offset"] for item in shown])
        self.assertEqual(self.store.next_event(), event)

    async def test_related_candidate_and_short_assistant_reply_survive_large_tool_output(self):
        seed(self.store, "Redis cache")
        seed(self.store, "Coffee")
        self.store.append_event("b", (
            AIMessage("Option two replaces Redis with local cache."),
            ToolMessage("log " * 20000, tool_call_id="call"),
            HumanMessage("For Redis use option two."),
        ))
        provider = ConsolidationProvider([LLMResponse(content='{"operations":[]}')])
        await self.consume(provider, input_token_budget=2000)
        payload = provider.requests[0][0]
        self.assertEqual([m["message_offset"] for m in payload["messages"]], [0, 2])
        self.assertEqual([m["id"] for m in payload["active_memories"]], [1, 2])

    async def test_unusable_fixed_budget_acknowledges_event_and_retains_snapshot(self):
        event = self.store.append_event("b", (HumanMessage("Hello"),))
        provider = ConsolidationProvider([])
        await self.consume(provider, input_token_budget=1)
        self.assertEqual(provider.requests, [])
        self.assertIsNone(self.store.next_event())
        self.assertEqual(self.store.active_memories(), [])
        with closing(sqlite3.connect(self.database)) as db:
            processed, snapshot = db.execute(
                "SELECT processed, messages FROM memory_events WHERE id = ?", (event.event_id,)
            ).fetchone()
        self.assertEqual(processed, 1)
        self.assertEqual(json.loads(snapshot), list(event.messages))

    async def test_skips_unusable_evidence_then_processes_next_event_in_same_pass(self):
        # Use the measured cost of a short request as the budget for both queued events.
        short = HumanMessage("Use Go.")
        self.store.append_event("baseline", (short,))
        probe = ConsolidationProvider([LLMResponse(content='{"operations":[]}')])
        await self.consume(probe)

        self.store.append_event("large", (HumanMessage("长" * 3000),))
        self.store.append_event("next", (short,))
        provider = ConsolidationProvider([LLMResponse(content=json.dumps({"operations": [addition(short.content)]}))])
        await self.consume(provider, input_token_budget=probe.request_costs[0])
        self.assertIsNone(self.store.next_event())
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual([m.session_key for m in self.store.active_memories()], ["next"])
