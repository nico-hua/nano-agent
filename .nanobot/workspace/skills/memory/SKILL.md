---
name: memory
description: Recall past conversations and explain how to view or correct workspace memory.
---

# Memory

## Recall a past conversation

When the user asks what was discussed or decided earlier, use `search_sessions` with distinctive terms from their question. For example, search for `Redis` when asked about a previous Redis caching decision. Use the matching session's `session_id`, `revision`, and `message_index` in `read_session` to read the surrounding messages before answering. If the session has changed, search again for a fresh match.

To look up a known message in the current conversation, use `read_session(session_id="@current", message_index=...)` and omit `revision`.

## Long-term memory

After a completed user turn, the runtime reviews the conversation for enduring preferences, stable facts, and confirmed decisions. When the user corrects or withdraws a fact, use the correction in the current conversation. Background consolidation handles updates to `memory/memory.sqlite3`.

Users can type `/memory` to see active entries and their sources. Each source identifies a memory event and a zero-based position within that event's saved messages. For `read_session`, use the message index returned by `search_sessions`.

The user maintains `AGENTS.md`, `SOUL.md`, and `USER.md` beside the runtime configuration. A request to remember something follows the SQLite consolidation flow above.
