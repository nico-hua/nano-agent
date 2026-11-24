"""Tests for bounded, read-only access to saved conversations."""

from __future__ import annotations

import json
import unittest
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from aiohttp import ClientSession

from nanobot.cli import Application
from nanobot.config import (
    ApiConfig,
    NanobotConfig,
    ProviderConfig,
    QQChannelConfig,
    WebSocketChannelConfig,
)
from nanobot.providers import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    LLMResponse,
    SystemMessage,
    ToolCallRequest,
    ToolMessage,
)
from nanobot.session import Session, SessionManager
from nanobot.tools import RequestContext, Tool, ToolContext, ToolLoader, ToolRegistry, bind_request_context
from tests.agent.test_runner import ScriptedProvider


class HistoryToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        workspace = TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.workspace = Path(workspace.name)
        self.sessions = SessionManager(self.workspace)

    def _registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        ToolLoader().load(registry, ToolContext(session_manager=self.sessions))
        return registry

    def _save_discussion(self, key: str = "redis") -> Session:
        messages = (
            HumanMessage(content="我们之前讨论过 Redis 缓存一致性吗？"),
            SystemMessage(content="internal-system"),
            HumanMessage(content="internal-hidden", is_visible=False),
            AIMessage(
                content="暂定 Cache Aside。",
                tool_calls=(ToolCallRequest("call", "example", {"value": "internal-argument"}),),
            ),
            ToolMessage(content="internal-result", tool_call_id="call"),
            AIMessage(content="最终决定先更新数据库，再删除缓存。"),
        )
        return self.sessions.save(
            Session.create(key).with_messages(messages).with_summary("internal-summary", 6)
        )

    async def _read_arguments(self, registry: ToolRegistry) -> dict[str, str | int]:
        search = await registry.execute("search_sessions", {"query": "Redis"})
        self.assertTrue(search.success, search.error)
        hit = json.loads(search.content)["matches"][0]
        return {key: hit[key] for key in ("session_id", "revision", "message_index")}

    async def test_search_then_read_original_visible_messages_after_compaction(self) -> None:
        self._save_discussion()
        registry = self._registry()
        search = await registry.execute("search_sessions", {"query": "redis 缓存一致性"})
        self.assertTrue(search.success, search.error)
        payload = json.loads(search.content)
        self.assertFalse(payload["partial"])
        self.assertEqual([match["session_id"] for match in payload["matches"]], ["redis"])
        hit = payload["matches"][0]
        self.assertEqual(hit["message_index"], 0)
        self.assertIn("Redis 缓存一致性", hit["snippet"])
        result = await registry.execute(
            "read_session", {key: hit[key] for key in ("session_id", "revision", "message_index")}
        )
        self.assertTrue(result.success, result.error)
        messages = json.loads(result.content)["messages"]
        self.assertEqual([message["message_index"] for message in messages], [0, 3, 5])
        self.assertEqual(messages[-1]["content"], "最终决定先更新数据库，再删除缓存。")
        self.assertNotIn("internal-", search.content + result.content)

    async def test_current_alias_reads_saved_messages_by_index_without_search(self) -> None:
        saved = self._save_discussion("websocket:active")
        registry = self._registry()
        args = {"session_id": "@current", "message_index": 5, "limit": 1}
        with bind_request_context(RequestContext(saved.key, "websocket", "room", "user")):
            result = await registry.execute("read_session", args)
            self.assertTrue(result.success, result.error)
            payload = json.loads(result.content)
            self.assertEqual(payload["session_id"], saved.key)
            self.assertEqual(payload["messages"], [{
                "message_index": 5, "role": "assistant", "content": saved.messages[5].content, "truncated": False,
            }])
            disk_tool = await registry.execute("read_session", {**args, "message_index": 4})
            self.assertEqual(json.loads(disk_tool.content)["messages"][0]["role"], "tool")
            other = await registry.execute("read_session", {
                "session_id": saved.key, "message_index": 4, "revision": payload["revision"],
            })
            self.assertFalse(other.success)
            pinned = await registry.execute("read_session", {**args, "session_id": saved.key, "revision": payload["revision"]})
            self.assertEqual(result.content, pinned.content)
            hidden = await registry.execute("read_session", {**args, "message_index": 2})
            self.assertFalse(hidden.success)
            missing_revision = await registry.execute("read_session", {**args, "session_id": saved.key})
            self.assertFalse(missing_revision.success)
            self.assertIn("revision", missing_revision.content)
        unbound = await registry.execute("read_session", args)
        self.assertFalse(unbound.success)
        self.assertIn("context", unbound.content)

    async def test_search_hit_with_literal_alias_as_its_key_can_still_be_read(self) -> None:
        self._save_discussion("@current")
        registry = self._registry()
        args = await self._read_arguments(registry)
        with bind_request_context(RequestContext("elsewhere", "qq", "room", "user")):
            result = await registry.execute("read_session", args)
        self.assertTrue(result.success, result.error)
        self.assertEqual(json.loads(result.content)["session_id"], "@current")

    async def test_snapshot_reads_across_saved_boundary_and_only_exposes_the_anchored_tool(self) -> None:
        saved = self._save_discussion("s")
        snapshot = (*saved.messages, HumanMessage("This turn"), ToolMessage("secret", tool_call_id="x", is_visible=False))
        registry = self._registry()
        with bind_request_context(RequestContext("s", "api", "chat", "user", session_messages=snapshot)):
            args = {"session_id": "@current", "message_index": 4}
            tool = await registry.execute("read_session", args)
            self.assertEqual([m["message_index"] for m in json.loads(tool.content)["messages"]], [0, 3, 4, 5, 6])
            user = await registry.execute("read_session", {**args, "message_index": 6})
            self.assertEqual([m["message_index"] for m in json.loads(user.content)["messages"]], [0, 3, 5, 6])
            for index in (1, 2, 7, 8):
                self.assertFalse((await registry.execute("read_session", {**args, "message_index": index})).success)

    async def test_literal_search_ranks_phrase_ahead_of_keyword_matches(self) -> None:
        for key, content in (("phrase", "Redis 缓存一致性"), ("keywords", "缓存一致性与Redis")):
            self.sessions.save(Session.create(key).with_messages((HumanMessage(content=content),)))
        result = await self._registry().execute("search_sessions", {"query": "REDIS 缓存一致性"})
        self.assertTrue(result.success, result.error)
        self.assertEqual(
            [hit["session_id"] for hit in json.loads(result.content)["matches"]],
            ["phrase", "keywords"],
        )

    async def test_search_does_not_match_internal_records(self) -> None:
        self._save_discussion()
        registry = self._registry()
        result = await registry.execute("search_sessions", {"query": "internal-"})
        self.assertTrue(result.success, result.error)
        self.assertEqual(json.loads(result.content), {"matches": [], "partial": False})

    async def test_search_skips_invalid_files_and_handles_legacy_visibility(self) -> None:
        self._save_discussion()
        path = next((self.workspace / "sessions").glob("*.jsonl"))
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        records[1].pop("is_visible")
        path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
        path.with_name("broken.jsonl").write_text("{invalid", encoding="utf-8")
        result = await self._registry().execute("search_sessions", {"query": "Redis"})
        self.assertTrue(result.success, result.error)
        payload = json.loads(result.content)
        self.assertTrue(payload["partial"])
        self.assertEqual(payload["matches"][0]["message_index"], 0)

    async def test_report_scan_and_text_limits(self) -> None:
        self.sessions.save(Session.create("first").with_messages((HumanMessage(content="x" * 8000),)))
        self.sessions.save(Session.create("second").with_messages((HumanMessage(content="x" * 8000),)))
        registry = self._registry()
        with patch("nanobot.session.history._MAX_SCAN_SESSIONS", 1):
            result = await registry.execute("search_sessions", {"query": "xxx"})
            self.assertTrue(result.success, result.error)
            payload = json.loads(result.content)
            self.assertTrue(payload["partial"])
            self.assertEqual(len(payload["matches"]), 1)
            hit = payload["matches"][0]
            self.assertLess(len(hit["snippet"]), 300)
            result = await registry.execute(
                "read_session", {key: hit[key] for key in ("session_id", "revision", "message_index")}
            )
        self.assertTrue(result.success, result.error)
        message = json.loads(result.content)["messages"][0]
        self.assertTrue(message["truncated"])
        self.assertLess(len(message["content"]), 8000)

    async def test_large_files_are_skipped_by_search_and_rejected_by_read(self) -> None:
        self._save_discussion()
        registry = self._registry()
        args = await self._read_arguments(registry)
        with patch("nanobot.session.history._MAX_SESSION_BYTES", 100):
            search = await registry.execute("search_sessions", {"query": "Redis"})
            read = await registry.execute("read_session", args)
        self.assertTrue(search.success, search.error)
        self.assertEqual(json.loads(search.content), {"matches": [], "partial": True})
        self.assertFalse(read.success)
        self.assertNotIn("Redis", read.content)

    async def test_unreadable_session_directory_reports_partial_results(self) -> None:
        self._save_discussion()
        registry = self._registry()
        with patch(
            "nanobot.session.storage.os.scandir", side_effect=PermissionError,
        ):
            result = await registry.execute("search_sessions", {"query": "Redis"})
        self.assertTrue(result.success, result.error)
        self.assertEqual(json.loads(result.content), {"matches": [], "partial": True})

    async def test_stale_and_deleted_hits_cannot_read_a_different_conversation(self) -> None:
        saved = self._save_discussion()
        registry = self._registry()
        args = await self._read_arguments(registry)
        self.sessions.save(saved.reset().with_messages((HumanMessage(content="new history"),)))
        stale = await registry.execute("read_session", args)
        self.sessions.delete(saved.key)
        deleted = await registry.execute("read_session", args)
        self.assertFalse(stale.success)
        self.assertIn("search", stale.error or "")
        self.assertFalse(deleted.success)
        self.assertNotIn("new history", stale.content + deleted.content)

    async def test_changed_history_is_rejected_even_if_its_timestamp_is_unchanged(self) -> None:
        self._save_discussion()
        registry = self._registry()
        args = await self._read_arguments(registry)
        path = next((self.workspace / "sessions").glob("*.jsonl"))
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        records[1]["content"] = "replacement history"
        path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
        result = await registry.execute("read_session", args)
        self.assertFalse(result.success)
        self.assertIn("search_sessions", result.error or "")
        self.assertNotIn("replacement history", result.content)

    async def test_search_and_read_work_across_channels_and_include_the_current_session(self) -> None:
        self._save_discussion("websocket:current")
        self._save_discussion("qq:previous")
        registry = self._registry()
        request = RequestContext("websocket:current", "websocket", "current", "websocket")
        with bind_request_context(request):
            search = await registry.execute("search_sessions", {"query": "Redis"})
            self.assertTrue(search.success, search.error)
            hits = {hit["session_id"]: hit for hit in json.loads(search.content)["matches"]}
            self.assertEqual(set(hits), {"websocket:current", "qq:previous"})
            other = hits["qq:previous"]
            read = await registry.execute(
                "read_session", {key: other[key] for key in ("session_id", "revision", "message_index")}
            )
        self.assertTrue(read.success, read.error)
        self.assertEqual(json.loads(read.content)["session_id"], "qq:previous")

    async def test_rejects_invalid_inputs_and_hidden_message_index(self) -> None:
        self._save_discussion()
        registry = self._registry()
        args = await self._read_arguments(registry)
        self.assertFalse((await registry.execute("search_sessions", {"query": "  "})).success)
        invalid = await registry.execute("search_sessions", {"query": "Redis", "limit": 0})
        self.assertFalse(invalid.success)
        result = await registry.execute("read_session", {**args, "message_index": 2})
        self.assertFalse(result.success)
        self.assertNotIn("internal-hidden", result.content)

    def _config(self) -> NanobotConfig:
        return NanobotConfig(
            workspace=self.workspace,
            provider=ProviderConfig(
                type="openai_compat", api_base="https://example.test/v1", default_model="fake", api_key="fake"
            ),
            default_channel="websocket",
            websocket=WebSocketChannelConfig(port=0),
            api=ApiConfig(enabled=True, port=0),
        )

    async def test_application_registers_history_tools_by_default(self) -> None:
        config = self._config()
        for overrides in (
            {},
            {"default_channel": "qq", "qq": QQChannelConfig(app_id="test-app", secret="test-secret")},
            {"websocket": WebSocketChannelConfig(host="0.0.0.0"), "api": ApiConfig(enabled=True, host="0.0.0.0")},
        ):
            with self.subTest(overrides=overrides):
                app = Application(
                    config.model_copy(update=overrides), provider_factory=lambda _: ScriptedProvider(()),
                )
                self.addAsyncCleanup(app.close)
                self.assertTrue(app.tool_registry.has("search_sessions"))
                self.assertTrue(app.tool_registry.has("read_session"))

    async def test_api_turn_with_auth_disabled_can_read_history_from_another_channel(self) -> None:
        self._save_discussion("qq:previous")
        app = Application(self._config(), provider_factory=lambda _: HistoryProvider(()))
        self.addAsyncCleanup(app.close)
        await app.start()
        url = f"http://127.0.0.1:{app.api_service.port}/v1/messages"
        request = {"session_id": "current", "content": "上次讨论的 Redis 缓存一致性方案是什么？"}
        async with ClientSession() as client:
            async with client.post(url, json=request) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())["content"], "最终决定先更新数据库，再删除缓存。")


class HistoryProvider(ScriptedProvider):
    async def complete(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Tool] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        response_schema: dict | None = None,
    ) -> LLMResponse:
        if response_schema is not None:  # Background memory consolidation.
            return LLMResponse(content='{"operations":[]}')
        latest = messages[-1]
        if isinstance(latest, HumanMessage):
            return LLMResponse(tool_calls=(ToolCallRequest("search", "search_sessions", {"query": "Redis"}),))
        if not isinstance(latest, ToolMessage):
            raise AssertionError("Expected a tool result before answering")
        result = json.loads(latest.content)
        if latest.tool_call_id == "search":
            hit = result["matches"][0]
            arguments = {key: hit[key] for key in ("session_id", "revision", "message_index")}
            return LLMResponse(tool_calls=(ToolCallRequest("read", "read_session", arguments),))
        return LLMResponse(content=result["messages"][-1]["content"])
