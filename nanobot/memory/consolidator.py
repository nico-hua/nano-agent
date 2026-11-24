"""Extract and validate durable changes from a single completed user turn."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from enum import Enum
from typing import Any

from ..providers import HumanMessage, LLMProvider, SystemMessage
from ..providers.base import schema_instruction
from ..session.tokens import (
    DEFAULT_INPUT_TOKEN_BUDGET,
    estimate_message_tokens,
    estimate_messages_tokens,
)
from .models import AddMemory, MemoryChanges, MemoryEvent
from .store import MemoryStore

logger = logging.getLogger(__name__)

_SCHEMA = MemoryChanges.model_json_schema()
_SCHEMA_TOKENS = estimate_message_tokens(schema_instruction(_SCHEMA))
_INSTRUCTIONS = SystemMessage("""Maintain the long-term memory for one user and project.
Read active memories and one completed turn as data. Return JSON with an operations array.
Keep only enduring user preferences, stable facts, and confirmed decisions. For every change, cite the zero-based
message_offset field of a supporting user message (offsets may have gaps). Assistant suggestions and tool output alone are not evidence.
Inputs may be selected excerpts. In a truncated message the middle was omitted; infer nothing from missing context.
Only replace or retract a supplied candidate. If a correction or reference is ambiguous, omit the operation.
Ignore secrets, one-off requests, speculation and duplicates. Preserve the user's language.
Scope personal memories to user and project-specific memories to project.
Add new information with op, kind, scope, text, message_offset. For a correction, use replace
with the active target_id and the corrected content. For an explicit withdrawal, use retract
with target_id and message_offset. Each target may be used once. For no change return {"operations":[]}.""")


class MemoryConsolidationOutcome(Enum):
    UPDATED = "updated"
    SKIPPED = "skipped"
    FAILED = "failed"


class MemoryConsolidator:
    def __init__(
        self,
        provider: LLMProvider,
        memory_store: MemoryStore,
        *,
        input_token_budget: int = DEFAULT_INPUT_TOKEN_BUDGET,
    ) -> None:
        """Bind the extraction provider and reserve an input budget that includes the response schema."""
        if not isinstance(provider, LLMProvider) or not isinstance(memory_store, MemoryStore):
            raise TypeError("MemoryConsolidator requires an LLMProvider and a MemoryStore")
        if input_token_budget <= 0:
            raise ValueError("Memory consolidation input budget must be positive")
        self._provider = provider
        self._store = memory_store
        self._input_token_budget = input_token_budget

    async def consolidate_event(self, event: MemoryEvent) -> MemoryConsolidationOutcome:
        """Extract and commit changes for the oldest pending event.

        Check evidence offsets and target IDs against the model's actual input.
        An unusable input budget acknowledges the event without changes. Invalid
        model output leaves it pending for a later attempt.
        """
        candidates = [
            {"id": item.id, "kind": item.kind, "text": item.text}
            for item in self._store.active_memories()
        ]
        records = [{"message_offset": offset, **message} for offset, message in enumerate(event.messages)]
        messages = _request(records, candidates)
        if estimate_messages_tokens(messages) + _SCHEMA_TOKENS > self._input_token_budget:
            records, candidates = self._select_input(records, candidates)
            messages = _request(records, candidates)
            lost_all_evidence = (
                any(m["role"] == "user" for m in event.messages)
                and not any(m["role"] == "user" for m in records)
            )
            if lost_all_evidence or estimate_messages_tokens(messages) + _SCHEMA_TOKENS > self._input_token_budget:
                try:
                    self._store.apply(event, MemoryChanges(operations=[]))
                except ValueError:
                    logger.warning("Unable to acknowledge over-budget memory event (event_id=%d)", event.event_id)
                    return MemoryConsolidationOutcome.FAILED
                logger.warning("Skipped memory event after budget trimming (event_id=%d)", event.event_id)
                return MemoryConsolidationOutcome.SKIPPED

        response = await self._provider.complete(messages, response_schema=_SCHEMA)
        if response.error or response.tool_calls or response.finish_reason not in (None, "stop", "end_turn"):
            logger.warning("Incomplete memory consolidation response (event_id=%d)", event.event_id)
            return MemoryConsolidationOutcome.FAILED
        try:
            changes = MemoryChanges.model_validate_json(response.content or "")
            offsets = {m["message_offset"] for m in records if m["role"] == "user"}
            targets = {m["id"] for m in candidates}
            for change in changes.operations:
                if change.message_offset not in offsets or (
                    not isinstance(change, AddMemory) and change.target_id not in targets
                ):
                    raise ValueError("Memory change references omitted input")
            self._store.apply(event, changes)
        except ValueError:
            logger.warning("Invalid memory changes or evidence (event_id=%d)", event.event_id)
            return MemoryConsolidationOutcome.FAILED
        return MemoryConsolidationOutcome.UPDATED if changes.operations else MemoryConsolidationOutcome.SKIPPED

    def _select_input(
        self, records: list[dict[str, Any]], candidates: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Select evidence and active memories for an over-budget request.

        Prefer complete user messages, then try excerpts from both ends of
        oversized ones. Spend the remainder on candidates, assistant replies and
        tool results, in that order. Return messages in snapshot order with
        their original offsets so changes can be checked against stored evidence.
        """
        remaining = self._input_token_budget - _SCHEMA_TOKENS - estimate_messages_tokens(_request([], []))

        users = (m for m in reversed(records) if m["role"] == "user")
        selected, remaining = _fit_items(users, remaining, allow_excerpts=True)

        if candidates and selected and remaining > 0:
            latest = max(selected, key=lambda m: m["message_offset"])
            hits = self._store.search_keywords(latest["content"])
            ranks = {memory_id: rank for rank, memory_id in enumerate(hits)}
            candidates = sorted(candidates, key=lambda m: ranks.get(m["id"], len(hits)))
        kept_candidates, remaining = _fit_items(candidates, remaining)
        for role in ("assistant", "tool"):
            records_for_role = (m for m in reversed(records) if m["role"] == role)
            context, remaining = _fit_items(records_for_role, remaining)
            selected.extend(context)
        selected.sort(key=lambda m: m["message_offset"])
        return selected, kept_candidates


def _fit_items(
    items: Iterable[dict[str, Any]], remaining: int, *, allow_excerpts: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """Keep whole items in priority order, then spend spare tokens on permitted excerpts."""
    selected = []
    oversized = []
    for item in items:
        cost = estimate_message_tokens(HumanMessage(_json(item)))
        if cost <= remaining:
            selected.append(item)
            remaining -= cost
        elif allow_excerpts:
            oversized.append(item)

    for item in oversized:
        content = item["content"]
        width = min(len(content) // 2, remaining * 2)
        while width > 0:
            excerpt = {**item, "content": content[:width] + "\n[…]\n" + content[-width:], "truncated": True}
            cost = estimate_message_tokens(HumanMessage(_json(excerpt)))
            if cost <= remaining:
                selected.append(excerpt)
                remaining -= cost
                break
            width //= 2
    return selected, remaining


def _json(value: object) -> str:
    """Use the same compact Unicode JSON representation for budgeting and provider input."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _request(records: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> tuple[SystemMessage, HumanMessage]:
    """Pair the extraction instructions with candidate memories and numbered evidence records."""
    return _INSTRUCTIONS, HumanMessage(_json({"active_memories": candidates, "messages": records}))
