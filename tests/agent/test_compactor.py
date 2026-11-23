"""Focused tests for LLM-backed session summary compaction."""

from __future__ import annotations

import json
import unittest
from collections.abc import Awaitable, Callable, Sequence

from nanobot.agent import estimate_messages_tokens
from nanobot.providers import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    LLMProvider,
    LLMResponse,
    ToolCallRequest,
    ToolMessage,
)
from nanobot.session import Session, SessionCompactor
from nanobot.tools import Tool


def summary_json(text: str, index: int = 0) -> str:
    return json.dumps({
        "user_constraints": [], "confirmed_decisions": [{"text": text, "message_index": index}],
        "key_files": [], "unfinished_tasks": [],
    }, separators=(",", ":"))


class SummaryProvider(LLMProvider):
    def __init__(self, response: LLMResponse | Exception) -> None:
        self._response = response
        self.complete_calls: list[tuple[BaseMessage, ...]] = []
        self.schemas = []

    async def complete(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        *, response_schema=None,
    ) -> LLMResponse:
        del tools, max_tokens, temperature
        self.complete_calls.append(tuple(messages))
        self.schemas.append(response_schema)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    async def stream(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        raise AssertionError("SessionCompactor must not use streaming")


class SessionCompactorTest(unittest.IsolatedAsyncioTestCase):
    async def test_incomplete_or_refused_response_does_not_advance_the_summary(self) -> None:
        session = Session.create("test").with_messages((HumanMessage("Task."),))
        for reason in ("length", "max_tokens", "refusal", "content_filter"):
            with self.subTest(reason=reason):
                provider = SummaryProvider(LLMResponse(content=summary_json("A decision."), finish_reason=reason))
                result = await SessionCompactor(provider).compact_prefix(session, 1)
                self.assertIs(result, session)

    async def test_summary_requires_four_arrays_of_cited_items(self) -> None:
        provider = SummaryProvider(LLMResponse(content=summary_json("A decision.")))
        result = await SessionCompactor(provider).summarize([(0, HumanMessage("A decision."))])
        self.assertIsNotNone(result)
        schema, = provider.schemas
        sections = {"user_constraints", "confirmed_decisions", "key_files", "unfinished_tasks"}
        self.assertEqual(set(schema["required"]), sections)
        self.assertEqual(set(schema["properties"]), sections)
        self.assertFalse(schema["additionalProperties"])
        for field in schema["properties"].values():
            self.assertEqual(field["type"], "array")
            item = field["items"]
            self.assertEqual(set(item["required"]), {"text", "message_index"})
            self.assertEqual(item["properties"], {"text": {"type": "string"}, "message_index": {"type": "integer"}})
            self.assertFalse(item["additionalProperties"])

    async def test_does_not_compact_when_raw_history_fits_the_threshold(self) -> None:
        old_turn, recent_turn = _turns()
        session = Session.create("session-1").with_messages((*old_turn, *recent_turn))
        provider = SummaryProvider(LLMResponse(content="unused"))
        compactor = SessionCompactor(
            provider,
            token_threshold=estimate_messages_tokens(session.messages) + 1,
            recent_token_budget=1,
        )

        compacted = await compactor.compact(session)

        self.assertIs(compacted, session)
        self.assertEqual(provider.complete_calls, [])

    async def test_manual_compaction_uses_the_recent_budget_as_its_trigger(self) -> None:
        old_turn, recent_turn = _turns()
        session = Session.create("session-1").with_messages((*old_turn, *recent_turn))
        provider = SummaryProvider(LLMResponse(content=summary_json("The weather request was completed.", 3)))
        compactor = SessionCompactor(
            provider,
            token_threshold=estimate_messages_tokens(session.messages) + 1,
            recent_token_budget=estimate_messages_tokens(recent_turn),
        )

        automatic = await compactor.compact(session)
        manual = await compactor.compact_manually(session)

        self.assertIs(automatic, session)
        self.assertEqual(manual.summary, summary_json("The weather request was completed.", 3))
        self.assertEqual(manual.summary_until, len(old_turn))
        self.assertEqual(manual.messages[manual.summary_until :], recent_turn)
        self.assertEqual(len(provider.complete_calls), 1)

    async def test_generates_summary_without_removing_complete_tool_turns(self) -> None:
        old_turn, recent_turn = _turns()
        session = Session.create("session-1").with_messages((*old_turn, *recent_turn))
        provider = SummaryProvider(LLMResponse(content=summary_json("The weather request was completed.", 3)))
        compactor = SessionCompactor(
            provider,
            token_threshold=estimate_messages_tokens(recent_turn) + 1,
            recent_token_budget=estimate_messages_tokens(recent_turn),
        )

        compacted = await compactor.compact(session)

        self.assertEqual(compacted.messages, session.messages)
        self.assertEqual(compacted.summary, summary_json("The weather request was completed.", 3))
        self.assertEqual(compacted.summary_until, len(old_turn))
        self.assertEqual(compacted.messages[compacted.summary_until :], recent_turn)
        self.assertEqual(len(provider.complete_calls), 1)
        self.assertIn('"tool_call_id":"call-1"', provider.complete_calls[0][1].content)

    async def test_summary_failure_leaves_the_original_session_unchanged(self) -> None:
        old_turn, recent_turn = _turns()
        session = Session.create("session-1").with_messages((*old_turn, *recent_turn))
        provider = SummaryProvider(RuntimeError("provider unavailable"))
        compactor = SessionCompactor(
            provider,
            token_threshold=estimate_messages_tokens(recent_turn) + 1,
            recent_token_budget=estimate_messages_tokens(recent_turn),
        )

        compacted = await compactor.compact(session)

        self.assertIs(compacted, session)
        self.assertIsNone(session.summary)
        self.assertEqual(session.summary_until, 0)

    async def test_repeated_compaction_accepts_only_carried_or_new_source_indices(self) -> None:
        old, recent = _turns()
        session = Session.create("test").with_messages((*old, *recent, *recent)).with_summary(
            summary_json("Shanghai is sunny.", 3), len(old)
        )
        for index, accepted in ((3, True), (4, True), (1, False), (6, False)):
            with self.subTest(index=index):
                provider = SummaryProvider(LLMResponse(content=summary_json("Finding.", index)))
                compactor = SessionCompactor(provider, token_threshold=2, recent_token_budget=1)
                result = await compactor.compact(session)
                payload = json.loads(provider.complete_calls[0][1].content)
                self.assertEqual([m["message_index"] for m in payload["messages"]], [4, 5])
                self.assertEqual(result.summary_until, 6 if accepted else 4)
                if not accepted:
                    self.assertIs(result, session)

    async def test_legacy_summary_is_rebuilt_from_original_evidence(self) -> None:
        old, recent = _turns()
        session = Session.create("test").with_messages((*old, *recent, *recent)).with_summary(
            "An older summary without citations.", len(old)
        )
        provider = SummaryProvider(LLMResponse(content=summary_json("Shanghai is sunny.", 3)))
        result = await SessionCompactor(provider, token_threshold=2, recent_token_budget=1).compact(session)

        payload = json.loads(provider.complete_calls[0][1].content)
        self.assertIsNone(payload["previous_summary"])
        self.assertEqual([m["message_index"] for m in payload["messages"]], list(range(6)))
        self.assertEqual(result.summary_until, 6)
        self.assertEqual(result.messages, session.messages)


def _turns() -> tuple[tuple[BaseMessage, ...], tuple[BaseMessage, ...]]:
    request = ToolCallRequest(
        id="call-1",
        name="get_weather",
        arguments={"city": "Shanghai"},
    )
    old_turn = (
        HumanMessage(content="What is the weather in Shanghai?"),
        AIMessage(content="I will check.", tool_calls=(request,)),
        ToolMessage(content="Sunny", tool_call_id="call-1"),
        AIMessage(content="Shanghai is sunny."),
    )
    recent_turn = (
        HumanMessage(content="What should I pack?"),
        AIMessage(content="Bring sunscreen."),
    )
    return old_turn, recent_turn
