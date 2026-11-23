"""Budget and evidence invariants at the Runner/Provider boundary."""

import asyncio
import json
import unittest
from tempfile import TemporaryDirectory

from nanobot.agent import (
    AgentLoop, AgentRunner, AgentRunSpec, ContextBuilder, ContextWindowExceededError,
    estimate_messages_tokens, estimate_tools_tokens,
)
from nanobot.providers import AIMessage, HumanMessage, LLMResponse, SystemMessage, TokenUsage, ToolCallRequest, ToolMessage
from nanobot.session import SessionCompactor, SessionManager
from nanobot.tools import Tool, ToolRegistry, ToolResult
from .test_compactor import SummaryProvider, summary_json
from .test_loop import _dispatch


class LargeResult(Tool):
    def __init__(self):
        super().__init__("read_data", "Read the test data.")

    async def execute(self, **arguments):
        return ToolResult(content="original data " * 1100 + "verified ending")


class BudgetProvider(SummaryProvider):
    def __init__(self, *, invalid_summary=False, tool_rounds=2):
        super().__init__(LLMResponse())
        self.requests = []
        self.inputs = []
        self.invalid_summary = invalid_summary
        self.tool_rounds = tool_rounds
        self.summarized_indices = set()
        self.summary_calls = 0
        self.fragments = {}

    async def complete(self, messages, tools=None, max_tokens=None, temperature=None):
        self.inputs.append(estimate_messages_tokens(messages) + estimate_tools_tokens(tools or ()))
        if max_tokens is not None:
            self.summary_calls += 1
            self.inputs[-1] += max_tokens
            records = json.loads(messages[1].content)["messages"]
            self.summarized_indices.update(row["message_index"] for row in records)
            for record in records:
                if "fragment" in record:
                    self.fragments.setdefault(record["message_index"], []).append(record["fragment"])
            index = records[-1]["message_index"]
            return LLMResponse(
                content=summary_json("Verified finding.", -1 if self.invalid_summary else index),
                usage=TokenUsage(2, 1, 3),
            )
        self.requests.append(tuple(messages))
        if len(self.requests) <= self.tool_rounds:
            return LLMResponse(tool_calls=(ToolCallRequest(str(len(self.requests)), "read_data", {}),))
        return LLMResponse(content="Done.", usage=TokenUsage(4, 1, 5))

    async def stream(self, messages, tools=None, max_tokens=None, temperature=None, on_delta=None):
        return await self.complete(messages, tools=tools)


class RunContextTest(unittest.IsolatedAsyncioTestCase):
    async def test_keeps_recent_message_at_the_compactor_budget_boundary(self):
        system = SystemMessage("Rules.")
        # Of the 1,192 tokens after the system prompt, 894 are reserved for recent messages.
        recent = HumanMessage("x" * 3556)
        provider = BudgetProvider(tool_rounds=0)
        runner = AgentRunner(SessionCompactor(provider, input_token_budget=1200))
        await runner.run(AgentRunSpec(
            messages=(system, HumanMessage("older " * 1000), recent),
            provider=provider, tool_registry=ToolRegistry(),
        ))
        self.assertEqual(provider.requests[0][-1], recent)
        self.assertEqual(provider.summarized_indices, {0})
        self.assertLessEqual(max(provider.inputs), 1200)

    async def test_keeps_recent_exchange_when_cutoff_falls_between_parallel_results(self):
        old_calls = (ToolCallRequest("a", "read", {}), ToolCallRequest("b", "read", {}))
        recent_call = ToolCallRequest("c", "read", {})
        prefix = (
            HumanMessage("Gather evidence."), AIMessage("", tool_calls=old_calls),
            ToolMessage("large " * 2000, tool_call_id="a"), ToolMessage("small", tool_call_id="b"),
        )
        suffix = (
            AIMessage("", tool_calls=(recent_call,)), ToolMessage("recent", tool_call_id="c"),
            HumanMessage("Only report the findings."),
        )
        provider = BudgetProvider(tool_rounds=0)
        runner = AgentRunner(SessionCompactor(provider, input_token_budget=1200))
        await runner.run(AgentRunSpec(
            messages=(*prefix, *suffix), provider=provider, tool_registry=ToolRegistry(),
        ))
        summary, *recent = provider.requests[0]
        self.assertIsInstance(summary, HumanMessage)
        self.assertEqual(recent, list(suffix))
        self.assertEqual(provider.summarized_indices, {0, 1, 2, 3})

    async def test_small_followup_batch_is_appended_to_the_compressed_request(self):
        class LargeThenSmall(LargeResult):
            def __init__(self):
                super().__init__()
                self.calls = 0

            async def execute(self, **arguments):
                self.calls += 1
                if self.calls == 1:
                    return await super().execute()
                return ToolResult(content="small result")

        system = SystemMessage("Workspace instructions.")
        task = HumanMessage("Read the data.")
        provider = BudgetProvider()
        runner = AgentRunner(SessionCompactor(provider, input_token_budget=1200))
        result = await runner.run(AgentRunSpec(
            messages=(system, task), provider=provider, tool_registry=ToolRegistry((LargeThenSmall(),)),
        ))

        first, compacted, following = provider.requests
        self.assertEqual(first, (system, task))
        self.assertIs(compacted[0], system)
        self.assertIsInstance(compacted[-1], HumanMessage)
        self.assertEqual(following[:-2], compacted)
        self.assertEqual(following[-2].tool_calls[0].id, following[-1].tool_call_id)
        self.assertEqual(following[-1].content, "small result")
        self.assertEqual(result.messages[:2], first)
        self.assertNotIn(compacted[-1], result.messages)

    async def test_first_call_summarizes_the_missing_prefix_and_next_turn_reuses_it(self):
        with TemporaryDirectory() as workspace:
            sessions = SessionManager(workspace)
            history = (
                HumanMessage("Original request."), AIMessage("First decision."),
                HumanMessage("Keep the data local."), AIMessage("Detailed evidence. " * 1200),
                HumanMessage("Recent question."), AIMessage("Recent answer."),
            )
            sessions.save(sessions.get_or_create("s").with_messages(history).with_summary(
                summary_json("First decision.", 1), 2,
            ))
            provider = BudgetProvider(tool_rounds=0)
            loop = AgentLoop(AgentRunner(SessionCompactor(provider, input_token_budget=1200)),
                             provider, ToolRegistry(), sessions, ContextBuilder(workspace))

            await _dispatch(loop, "Continue.", "test", "chat", "s")
            saved = SessionManager(workspace).get_or_create("s")
            self.assertEqual(saved.summary_until, 4)
            self.assertEqual(saved.messages[:len(history)], history)
            self.assertEqual(provider.summarized_indices, {2, 3})
            self.assertEqual(provider.requests[0][2:], (*history[4:], HumanMessage("Continue.")))
            system = SystemMessage(ContextBuilder(workspace).build_system_prompt())
            self.assertEqual(provider.requests[0][0], system)
            self.assertIsInstance(provider.requests[0][1], HumanMessage)
            self.assertIn(saved.summary, provider.requests[0][1].content)
            calls = provider.summary_calls

            await _dispatch(loop, "Next question.", "test", "chat", "s")
            self.assertEqual(provider.summary_calls, calls)
            self.assertEqual(provider.requests[1][0], system)
            self.assertEqual(provider.requests[1][1], provider.requests[0][1])
            self.assertEqual(provider.requests[1][2:], (*saved.messages[4:], HumanMessage("Next question.")))
            self.assertLessEqual(max(provider.inputs), 1200)

    async def test_first_call_with_invalid_summary_leaves_complete_history_for_retry(self):
        with TemporaryDirectory() as workspace:
            sessions = SessionManager(workspace)
            original = sessions.save(sessions.get_or_create("s").with_messages((
                HumanMessage("Long question " * 1000), AIMessage("Original answer."),
            )))
            provider = BudgetProvider(invalid_summary=True, tool_rounds=0)
            loop = AgentLoop(AgentRunner(SessionCompactor(provider, input_token_budget=1200)),
                             provider, ToolRegistry(), sessions, ContextBuilder(workspace))
            reply = await _dispatch(loop, "Continue.", "test", "chat", "s")
            self.assertIn("上下文窗口", reply.content)
            self.assertEqual(provider.requests, [])
            self.assertEqual(SessionManager(workspace).get_or_create("s"), original)

    async def test_cancelling_a_foreground_summary_keeps_the_saved_boundary(self):
        started = asyncio.Event()

        class WaitingProvider(BudgetProvider):
            async def complete(self, messages, tools=None, max_tokens=None, temperature=None):
                if max_tokens is None:
                    raise AssertionError("Expected a summary before the first task request")
                started.set()
                await asyncio.Event().wait()

        with TemporaryDirectory() as workspace:
            sessions = SessionManager(workspace)
            original = sessions.save(sessions.get_or_create("s").with_messages((
                HumanMessage("A constraint " * 1200), AIMessage("An answer."),
            )))
            provider = WaitingProvider()
            loop = AgentLoop(AgentRunner(SessionCompactor(provider, input_token_budget=1200)),
                             provider, ToolRegistry(), sessions, ContextBuilder(workspace))
            turn = asyncio.create_task(_dispatch(loop, "Continue.", "test", "chat", "s"))
            await asyncio.wait_for(started.wait(), timeout=2)
            await loop.close()
            with self.assertRaises(asyncio.CancelledError):
                await turn
            self.assertEqual(SessionManager(workspace).get_or_create("s"), original)

    async def test_every_iteration_and_summary_request_fits_and_history_stays_verifiable(self):
        with TemporaryDirectory() as workspace:
            sessions = SessionManager(workspace)
            # This old turn is already behind a durable summary. Transcript indices
            # in the next turn must still start after it.
            sessions.save(sessions.get_or_create("session").with_messages((
                HumanMessage("Old question."), AIMessage("Old answer."),
            )).with_summary("Saved summary.", 2))
            provider = BudgetProvider()
            loop = AgentLoop(
                AgentRunner(SessionCompactor(provider, input_token_budget=1200)), provider, ToolRegistry((LargeResult(),)), sessions,
                ContextBuilder(workspace),
            )

            reply = await _dispatch(loop, "Use the data.", "test", "chat", "session")

            self.assertEqual(reply.content, "Done.")
            self.assertEqual(len(provider.requests), 3)
            self.assertGreaterEqual(provider.summary_calls, 2)
            self.assertLessEqual(max(provider.inputs), 1200)
            saved = SessionManager(workspace).get_or_create("session")
            for turn, request in enumerate(provider.requests[1:]):
                self.assertEqual(request[0], SystemMessage(ContextBuilder(workspace).build_system_prompt()))
                self.assertIsInstance(request[-1], HumanMessage)
                summary = json.loads(request[-1].content.split("\n", 1)[1])
                index = summary["confirmed_decisions"][0]["message_index"]
                source = saved.messages[index]
                self.assertIsInstance(source, ToolMessage)
                self.assertTrue(source.content.endswith("verified ending"))
                serialized = "".join(provider.fragments[index])
                self.assertEqual(json.loads(serialized)["content"], source.content)
                self.assertEqual(source.tool_call_id, str(turn + 1))
                self.assertEqual(len(request), 2)
                self.assertNotIn(request[-1], saved.messages)
            self.assertEqual(saved.summary, "Saved summary.")

    async def test_streaming_preserves_injected_requests_and_counts_only_task_usage(self):
        provider = BudgetProvider()
        followups = [[HumanMessage("Additional constraint.")], [], []]

        async def inject():
            return followups.pop(0)

        result = await AgentRunner(SessionCompactor(provider, input_token_budget=1200)).run_stream(AgentRunSpec(
            messages=(HumanMessage("Task."),), provider=provider, tool_registry=ToolRegistry((LargeResult(),)),
            is_goal_mode=True, injection_callback=inject,
        ))

        self.assertEqual(result.content, "Done.")
        self.assertEqual(len(provider.requests[1]), 2)
        self.assertEqual(provider.requests[1][-1], HumanMessage("Additional constraint."))
        self.assertEqual(len(provider.requests[2]), 1)
        self.assertEqual(provider.summarized_indices, set(range(6)))
        self.assertEqual(result.token_usage, TokenUsage(4, 1, 5))
        self.assertLessEqual(max(provider.inputs), 1200)

    async def test_invalid_citation_stops_before_an_oversized_followup(self):
        provider = BudgetProvider(invalid_summary=True)
        with self.assertRaises(ContextWindowExceededError):
            await AgentRunner(SessionCompactor(provider, input_token_budget=1200)).run(AgentRunSpec(
                messages=(HumanMessage("Task."),), provider=provider,
                tool_registry=ToolRegistry((LargeResult(),)),
            ))
        self.assertEqual(len(provider.requests), 1)
        self.assertLessEqual(max(provider.inputs), 1200)
