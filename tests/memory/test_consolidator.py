"""Structured extraction, ordered recovery, and request budgets."""

import json
import tempfile
import unittest

from nanobot.memory import MemoryConsolidator, MemoryEventConsumer, MemoryStore
from nanobot.providers import AIMessage, HumanMessage, LLMProvider, LLMResponse

from .fixtures import addition, seed


class ConsolidationProvider(LLMProvider):
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def complete(self, messages, *, response_schema):
        self.requests.append((json.loads(messages[-1].content), response_schema))
        return next(self.responses)

    async def stream(self, *args, **kwargs):
        raise AssertionError("Unexpected stream")


class MemoryConsolidatorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = MemoryStore(directory.name)

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

    async def test_oversized_candidates_leave_event_pending_without_model_call(self):
        seed(self.store, "A" * 16000)
        event = self.store.append_event("b", (HumanMessage("New question"),))
        provider = ConsolidationProvider([])
        await self.consume(provider, input_token_budget=2000)
        self.assertEqual(provider.requests, [])
        self.assertEqual(self.store.next_event(), event)
