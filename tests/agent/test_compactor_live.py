"""Opt-in DeepSeek compaction test using an in-memory conversation."""

import json
import os
import unittest
from urllib.parse import urlsplit

from nanobot.config import ProviderConfig
from nanobot.config.loader import load_file_config
from nanobot.providers import AIMessage, HumanMessage, ToolCallRequest, ToolMessage, create_default_provider_factory
from nanobot.session import Session, SessionCompactor
from nanobot.session.tokens import estimate_messages_tokens


@unittest.skipUnless(os.getenv("RUN_DEEPSEEK_LIVE_TESTS") == "1", "requires an explicit live DeepSeek run")
class DeepSeekCompactionLiveTest(unittest.IsolatedAsyncioTestCase):
    async def test_compacts_history_and_preserves_the_latest_turn(self):
        settings = load_file_config().provider
        self.assertEqual(urlsplit(settings.api_base).hostname, "api.deepseek.com")
        provider = create_default_provider_factory().create(ProviderConfig(**settings.model_dump()))

        history = (
            HumanMessage("先只写设计文档，不修改代码。请评估订单缓存的一致性方案。"),
            AIMessage("草案建议先删除缓存，再更新数据库。"),
            HumanMessage("修正并确认：先提交数据库事务，再删除 Redis 缓存。删除失败必须重试。"),
            AIMessage("", tool_calls=(ToolCallRequest("read-design", "read_file", {"path": "docs/cache-design.md"}),)),
            ToolMessage("已读取 docs/cache-design.md。文档里尚未定义缓存删除失败的重试次数。",
                        tool_call_id="read-design"),
            AIMessage("待办：在设计文档中补充重试次数和退避策略，等待用户评审。"),
        )
        recent = (HumanMessage("给这次评审起个标题。"), AIMessage("订单缓存一致性方案评审"))
        session = Session.create("synthetic-cache-review").with_messages((*history, *recent))
        compactor = SessionCompactor(provider, recent_token_budget=estimate_messages_tokens(recent))
        result = await compactor.compact_manually(session)

        self.assertEqual(result.summary_until, len(history))
        self.assertEqual(result.messages, session.messages)
        self.assertEqual(result.messages[result.summary_until:], recent)
        notes = json.loads(result.summary)
        self.assertEqual(set(notes), {"user_constraints", "confirmed_decisions", "key_files", "unfinished_tasks"})
        for section, items in notes.items():
            with self.subTest(section=section):
                self.assertTrue(items)
                for item in items:
                    self.assertIn(item["message_index"], range(len(history)))
                    self.assertTrue(item["text"].strip())
        print(json.dumps(notes, ensure_ascii=False, indent=2))
