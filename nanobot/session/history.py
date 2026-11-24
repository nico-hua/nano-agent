"""Search and project user-visible text from saved session messages."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Sequence
from datetime import datetime
from threading import Event

from ..providers import BaseMessage
from .manager import SessionManager
from .models import Session

logger = logging.getLogger(__name__)

_MAX_SCAN_SESSIONS = 100
_MAX_SESSION_BYTES = 2 * 1024 * 1024
_MAX_SNIPPET_CHARS = 240
_MAX_MESSAGE_CHARS = 1000
MAX_SESSION_ID_CHARS = 512


class SessionHistoryError(ValueError):
    """An expected failure safe to report to the caller."""


def search_sessions(
    sessions: SessionManager,
    query: str,
    limit: int,
    *,
    cancelled: Event,
) -> dict[str, object]:
    """Rank visible message matches without loading all sessions at once."""

    words = query.split()
    phrase = re.compile(re.escape(" ".join(words)), re.IGNORECASE)
    keywords = tuple(re.compile(re.escape(word), re.IGNORECASE) for word in words)
    matches: list[tuple[int, datetime, str, dict[str, object]]] = []
    partial = False
    for snapshot in sessions.iter_history_sessions(
        max_files=_MAX_SCAN_SESSIONS, max_bytes=_MAX_SESSION_BYTES, cancelled=cancelled,
    ):
        if snapshot is None:
            partial = True
            continue
        session, revision = snapshot
        if len(session.key) > MAX_SESSION_ID_CHARS:
            partial = True
            continue

        match = _best_match(session, phrase, keywords, cancelled)
        if match is not None:
            rank, index, snippet = match
            activity = session.last_request_at or session.updated_at
            matches.append((rank, activity, session.key, {
                "session_id": session.key,
                "revision": revision,
                "last_activity_at": activity.isoformat(),
                "message_index": index,
                "snippet": snippet,
            }))

    matches.sort(key=lambda match: (match[0], match[1], match[2]), reverse=True)
    return {"matches": [item[3] for item in matches[:limit]], "partial": partial}


def read_session(
    sessions: SessionManager,
    session_id: str,
    revision: str | None,
    message_index: int,
    limit: int,
    *,
    snapshot: Sequence[BaseMessage] | None = None,
) -> dict[str, object]:
    """Read nearby visible messages. A pinned revision always reads the saved file."""
    saved_revision = None
    if snapshot is None or revision is not None:
        try:
            saved = sessions.read_history_session(session_id, max_bytes=_MAX_SESSION_BYTES)
        except (OSError, TypeError, ValueError, RecursionError) as error:
            logger.warning("Could not load a session for history reading (error_type=%s)", type(error).__name__)
            raise SessionHistoryError("Session history could not be read.") from error
        if saved is None:
            if revision is None:
                raise SessionHistoryError("The current session has no saved history.")
            raise SessionHistoryError("Session was not found. Run search_sessions again.")
        session, saved_revision = saved
        if revision is not None and saved_revision != revision:
            raise SessionHistoryError("Session history changed. Run search_sessions again.")
        snapshot = session.messages

    tool_index = message_index if revision is None else None
    visible = list(_visible_messages(snapshot, tool_index=tool_index))
    anchor = next((position for position, (index, _) in enumerate(visible) if index == message_index), None)
    if anchor is None:
        raise SessionHistoryError("message_index does not identify a visible message.")

    start = max(0, min(anchor - limit // 2, len(visible) - limit))
    return {
        "session_id": session_id,
        "revision": saved_revision,
        "messages": [
            {
                "message_index": index,
                "role": message.role,
                "content": message.content[:_MAX_MESSAGE_CHARS],
                "truncated": len(message.content) > _MAX_MESSAGE_CHARS,
            }
            for index, message in visible[start : start + limit]
        ],
    }


def _visible_messages(
    messages: Sequence[BaseMessage], *, tool_index: int | None = None,
) -> Iterator[tuple[int, BaseMessage]]:
    """Yield visible conversation text and the tool result at an explicitly requested index."""
    for index, message in enumerate(messages):
        if not message.is_visible:
            continue
        if index == tool_index and message.role == "tool" or message.role in {"user", "assistant"} and message.content.strip():
            yield index, message


def _best_match(
    session: Session,
    phrase: re.Pattern[str],
    keywords: tuple[re.Pattern[str], ...],
    cancelled: Event,
) -> tuple[int, int, str] | None:
    """Prefer phrase matches over all-keyword matches, keeping the latest match of equal rank."""
    best = None
    for index, message in _visible_messages(session.messages):
        if cancelled.is_set():
            break
        text = " ".join(message.content.split())
        match = phrase.search(text)
        if match is not None:
            best = (2, index, _snippet(text, match.start()))
        elif len(keywords) > 1 and (best is None or best[0] == 1):
            first = keywords[0].search(text)
            if first is not None and all(keyword.search(text) for keyword in keywords[1:]):
                best = (1, index, _snippet(text, first.start()))
    return best


def _snippet(text: str, position: int) -> str:
    """Extract bounded context around a match and mark omitted text at either end."""
    start = max(0, min(position - 60, len(text) - _MAX_SNIPPET_CHARS))
    end = start + _MAX_SNIPPET_CHARS
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")
