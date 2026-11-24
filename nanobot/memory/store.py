"""Workspace-local SQLite storage for turn evidence and long-term memories."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from pathlib import Path

from ..providers import BaseMessage, HumanMessage, SystemMessage
from ..providers.messages import message_record
from ..session.tokens import estimate_message_tokens
from .models import (
    ActiveMemory,
    MemoryChanges,
    MemoryEvent,
    ReplaceMemory,
    RetractMemory,
)

logger = logging.getLogger(__name__)

# Exclude the whole turn so an assistant or tool echo cannot retain a detected secret.
_CREDENTIAL = re.compile(
    r"""(?i)\b(?:api[_-]?key|access[_-]?token|password|client[_-]?secret)[\\"']*\s*[:=]"""
    r"|\bBearer\s+[a-z0-9._~+/=-]+|\bsk-[a-z0-9_-]{20,}|-----BEGIN[ A-Z]*PRIVATE KEY-----"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_events (
    id INTEGER PRIMARY KEY,
    session_key TEXT NOT NULL,
    messages TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    processed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS pending_events ON memory_events(id) WHERE processed = 0;
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    scope TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    event_id INTEGER NOT NULL REFERENCES memory_events(id),
    message_offset INTEGER NOT NULL,
    supersedes INTEGER REFERENCES memories(id),
    ended_event_id INTEGER REFERENCES memory_events(id),
    ended_message_offset INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS active_content
    ON memories(scope, kind, text) WHERE status = 'active';
"""


class MemoryStore:
    """One database per user workspace, with short-lived connections."""

    def __init__(self, workspace: str | Path) -> None:
        self._path = Path(workspace).resolve() / "memory" / "memory.sqlite3"
        self._initialized = False

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self._path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            if not self._initialized:
                db.executescript(_SCHEMA)
                self._initialized = True
            with db:
                yield db

    def append_event(self, session_key: str, messages: Sequence[BaseMessage]) -> MemoryEvent | None:
        if not session_key.strip() or not messages:
            raise ValueError("A memory event requires a session and a non-empty turn")
        if any(isinstance(message, SystemMessage) for message in messages):
            raise ValueError("Memory events must not contain system messages")
        records = tuple(message_record(message) for message in messages if message.is_visible)
        snapshot = json.dumps(records, ensure_ascii=False)
        if _CREDENTIAL.search(snapshot):
            logger.debug("Skipping a credential-bearing memory turn")
            return None
        with self._connect() as db:
            row = db.execute(
                "INSERT INTO memory_events(session_key, messages) VALUES (?, ?) RETURNING id",
                (session_key, snapshot),
            ).fetchone()
        return MemoryEvent(row["id"], session_key, records)

    def next_event(self) -> MemoryEvent | None:
        if not self._path.exists():
            return None
        with self._connect() as db:
            row = db.execute(
                "SELECT id, session_key, messages FROM memory_events WHERE processed = 0 ORDER BY id LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return MemoryEvent(row["id"], row["session_key"], tuple(json.loads(row["messages"])))

    def active_memories(self) -> list[ActiveMemory]:
        if not self._path.exists():
            return []
        with self._connect() as db:
            rows = db.execute("""
                SELECT m.id, m.kind, m.text, e.session_key, m.message_offset, m.event_id
                FROM memories m JOIN memory_events e ON m.event_id = e.id
                WHERE m.status = 'active' ORDER BY m.id DESC
            """)
            return [ActiveMemory(**row) for row in rows]

    def read(self, *, include_sources: bool = False, token_budget: int | None = None) -> str:
        """Render current entries newest first, retaining whole lines within the budget."""
        try:
            memories = self.active_memories()
        except (OSError, sqlite3.Error):
            logger.warning("Unable to read workspace long-term memory")
            return ""
        lines = []
        for item in memories:
            line = f"- [{item.kind}] {item.text}"
            if include_sources:
                line += f" · 事件 {item.event_id} · 消息 {item.message_offset}（会话 {item.session_key}）"
            if token_budget is not None:
                cost = estimate_message_tokens(HumanMessage(line))
                if cost > token_budget:
                    continue
                token_budget -= cost
            lines.append(line)
        return "\n".join(lines)

    def apply(self, event: MemoryEvent, changes: MemoryChanges) -> None:
        """Commit all validated changes and acknowledge exactly one queued turn."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT processed FROM memory_events WHERE id = ?", (event.event_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown memory event")
            if row["processed"]:
                return
            head = db.execute("SELECT MIN(id) FROM memory_events WHERE processed = 0").fetchone()[0]
            if head != event.event_id:
                raise ValueError("Memory events must be applied in order")

            # Only IDs visible before this batch can be replaced or retracted.
            targets = {row[0] for row in db.execute("SELECT id FROM memories WHERE status = 'active'")}
            for change in changes.operations:
                offset = change.message_offset
                if not 0 <= offset < len(event.messages) or event.messages[offset]["role"] != "user":
                    raise ValueError("Expected evidence from a user message in this turn")
                if isinstance(change, (ReplaceMemory, RetractMemory)):
                    if change.target_id not in targets:
                        raise ValueError("Expected a distinct, active memory target")
                    targets.remove(change.target_id)
                    status = "retracted" if isinstance(change, RetractMemory) else "superseded"
                    db.execute("""
                        UPDATE memories SET status = ?, ended_event_id = ?, ended_message_offset = ?
                        WHERE id = ?
                    """, (status, event.event_id, offset, change.target_id))
                if not isinstance(change, RetractMemory):
                    inserted = db.execute("""
                        INSERT INTO memories(kind, scope, text, event_id, message_offset, supersedes)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(scope, kind, text) WHERE status = 'active' DO NOTHING
                    """, (change.kind, change.scope, change.text, event.event_id, offset,
                          change.target_id if isinstance(change, ReplaceMemory) else None))
                    if isinstance(change, ReplaceMemory) and not inserted.rowcount:
                        raise ValueError("Replacement duplicates another active memory")
            db.execute("UPDATE memory_events SET processed = 1 WHERE id = ?", (event.event_id,))
