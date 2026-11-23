"""JSONL persistence for complete Agent sessions."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import stat
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime
from pathlib import Path
from threading import Event, Lock
from typing import Any

from ..providers import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolCallRequest,
    ToolMessage,
)
from .goals import GoalState
from .models import Session, _validate_session_key

logger = logging.getLogger(__name__)
_MAX_HISTORY_DIRECTORY_ENTRIES = 1000


class JsonlSessionStorage:
    """Store each complete session in one atomically replaced JSONL file."""

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)
        self._history_file_lock = Lock()
        if self._directory.exists() and not self._directory.is_dir():
            raise ValueError("session storage directory must be a directory")

    def load(self, session_key: str) -> Session | None:
        """Return a session by key, or ``None`` when it has not been saved."""

        _validate_session_key(session_key)
        path = self._path_for(session_key)
        if not path.exists():
            return None
        return self._load_path(path)

    def read_history_session(self, session_key: str, *, max_bytes: int) -> tuple[Session, str] | None:
        """Return a bounded snapshot and the revision of exactly those bytes."""

        _validate_session_key(session_key)
        try:
            return self._load_history_path(self._path_for(session_key), max_bytes)
        except FileNotFoundError:
            return None

    def save(self, session: Session) -> None:
        """Atomically replace the file containing one complete session."""

        if not isinstance(session, Session):
            raise TypeError("session storage requires a Session")

        content = _serialize_session(session)
        self._directory.mkdir(parents=True, exist_ok=True)
        destination = self._path_for(session.key)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=self._directory,
                prefix=".session-",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(content)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            with self._history_file_lock:
                os.replace(temporary_path, destination)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise

    def delete(self, session_key: str) -> bool:
        """Delete one session file and report whether it existed."""

        _validate_session_key(session_key)
        path = self._path_for(session_key)
        try:
            with self._history_file_lock:
                path.unlink()
        except FileNotFoundError:
            return False
        return True

    def list_sessions(self) -> tuple[Session, ...]:
        """Load all saved sessions in stable key order."""

        sessions = (self._load_path(path) for path in self._directory.glob("*.jsonl"))
        return tuple(sorted(sessions, key=lambda session: session.key))

    def iter_history_sessions(
        self, *, max_files: int, max_bytes: int, cancelled: Event,
    ) -> Iterator[tuple[Session, str] | None]:
        """Load one bounded session at a time, yielding None for incomplete coverage."""

        files = 0
        try:
            with os.scandir(self._directory) as entries:
                for index, entry in enumerate(entries):
                    if cancelled.is_set():
                        return
                    if index == _MAX_HISTORY_DIRECTORY_ENTRIES:
                        yield None
                        return
                    if not entry.name.endswith(".jsonl"):
                        continue
                    if files == max_files:
                        yield None
                        return
                    files += 1
                    try:
                        snapshot = self._load_history_path(Path(entry.path), max_bytes)
                    except FileNotFoundError:
                        continue
                    except (OSError, TypeError, ValueError, RecursionError) as error:
                        logger.warning("Skipping an unreadable session (error_type=%s)", type(error).__name__)
                        yield None
                    else:
                        yield snapshot
        except FileNotFoundError:
            return
        except OSError as error:
            logger.warning("Could not enumerate session history (error_type=%s)", type(error).__name__)
            yield None

    def _path_for(self, session_key: str) -> Path:
        digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()
        return self._directory / f"{digest}.jsonl"

    def _load_history_path(self, path: Path, max_bytes: int) -> tuple[Session, str]:
        # On Windows a read handle can prevent an atomic replace or delete.
        with self._history_file_lock:
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError("Session history requires a regular session file")
            with path.open("rb") as file:
                content = file.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise ValueError("Session file exceeds the history read size limit")
        with io.StringIO(content.decode("utf-8"), newline=None) as file:
            session = _session_from_records(path, _parse_records(path, file))
        if path != self._path_for(session.key):
            raise ValueError("Session history key does not match its file")
        return session, hashlib.sha256(content).hexdigest()

    def _load_path(self, path: Path) -> Session:
        with path.open(encoding="utf-8") as file:
            records = _parse_records(path, file)
        return _session_from_records(path, records)


def _session_from_records(path: Path, records: list[dict[str, Any]]) -> Session:
    if not records or records[0].get("type") != "session":
        raise ValueError(f"Invalid session file: {path.name}")

    header = records[0]
    return Session(
        key=_required_text(header, "key"),
        created_at=_timestamp_from_record(header, "created_at"),
        updated_at=_timestamp_from_record(header, "updated_at"),
        system_prompt=_optional_system_prompt(header),
        last_request_at=_optional_timestamp_from_record(header, "last_request_at"),
        messages=tuple(_message_from_record(record) for record in records[1:]),
        summary=_optional_summary(header),
        summary_until=_optional_summary_until(header),
        goal_state=_optional_goal_state(header),
    )


def _serialize_session(session: Session) -> str:
    records = [
        {
            "type": "session",
            "key": session.key,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "system_prompt": session.system_prompt,
            "last_request_at": (
                session.last_request_at.isoformat()
                if session.last_request_at is not None
                else None
            ),
            "summary": session.summary,
            "summary_until": session.summary_until,
            "goal_state": (
                session.goal_state.to_dict()
                if session.goal_state is not None
                else None
            ),
        },
        *(_message_to_record(message) for message in session.messages),
    ]
    try:
        return "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in records
        )
    except (TypeError, ValueError) as exc:
        raise TypeError("session data must be JSON serializable") from exc


def _message_to_record(message: BaseMessage) -> dict[str, Any]:
    record: dict[str, Any] = {
        "type": "message",
        "role": message.role,
        "content": message.content,
        "is_visible": message.is_visible,
    }
    if isinstance(message, AIMessage):
        record["tool_calls"] = [
            {
                "id": tool_call.id,
                "name": tool_call.name,
                "arguments": dict(tool_call.arguments),
            }
            for tool_call in message.tool_calls
        ]
    elif isinstance(message, ToolMessage):
        record["tool_call_id"] = message.tool_call_id
    elif not isinstance(message, (SystemMessage, HumanMessage)):
        raise TypeError(f"Unsupported session message type: {type(message).__name__}")
    return record


def _parse_records(path: Path, lines: Iterable[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {path.name} at line {line_number}") from exc
        if not isinstance(record, dict):
            raise TypeError(f"Invalid record in {path.name} at line {line_number}")
        records.append(record)
    return records


def _message_from_record(record: Mapping[str, Any]) -> BaseMessage:
    if record.get("type") != "message":
        raise ValueError("Invalid session message record")

    content = _required_string(record, "content")
    role = _required_text(record, "role")
    is_visible = _optional_visibility(record)
    if role == "system":
        return SystemMessage(content=content, is_visible=is_visible)
    if role == "user":
        return HumanMessage(content=content, is_visible=is_visible)
    if role == "assistant":
        tool_calls = record.get("tool_calls", [])
        if not isinstance(tool_calls, list):
            raise ValueError("AI message tool_calls must be a list")
        return AIMessage(
            content=content,
            tool_calls=tuple(_tool_call_from_record(tool_call) for tool_call in tool_calls),
            is_visible=is_visible,
        )
    if role == "tool":
        return ToolMessage(
            content=content,
            tool_call_id=_required_text(record, "tool_call_id"),
            is_visible=is_visible,
        )
    raise ValueError(f"Unsupported session message role: {role}")


def _tool_call_from_record(record: Any) -> ToolCallRequest:
    if not isinstance(record, Mapping):
        raise TypeError("Tool call must be an object")
    arguments = record.get("arguments")
    if not isinstance(arguments, Mapping):
        raise TypeError("Tool call arguments must be an object")
    return ToolCallRequest(
        id=_required_text(record, "id"),
        name=_required_text(record, "name"),
        arguments=dict(arguments),
    )


def _required_text(record: Mapping[str, Any], name: str) -> str:
    value = _required_string(record, name)
    if not value.strip():
        raise ValueError(f"Session record {name} must be a non-empty string")
    return value


def _required_string(record: Mapping[str, Any], name: str) -> str:
    value = record.get(name)
    if not isinstance(value, str):
        raise TypeError(f"Session record {name} must be a string")
    return value


def _optional_summary(record: Mapping[str, Any]) -> str | None:
    value = record.get("summary")
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("Session record summary must be a string or null")
    if not value.strip():
        raise ValueError("Session record summary must not be blank")
    return value


def _optional_system_prompt(record: Mapping[str, Any]) -> str | None:
    value = record.get("system_prompt")
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("Session record system_prompt must be a string or null")
    if not value.strip():
        raise ValueError("Session record system_prompt must not be blank")
    return value


def _optional_visibility(record: Mapping[str, Any]) -> bool:
    value = record.get("is_visible", True)
    if not isinstance(value, bool):
        raise TypeError("Session record is_visible must be a boolean")
    return value


def _optional_summary_until(record: Mapping[str, Any]) -> int:
    value = record.get("summary_until", 0)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("Session record summary_until must be an integer")
    return value


def _optional_goal_state(record: Mapping[str, Any]) -> GoalState | None:
    value = record.get("goal_state")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("Session record goal_state must be an object or null")
    return GoalState.from_dict(value)


def _timestamp_from_record(record: Mapping[str, Any], name: str) -> datetime:
    value = _required_text(record, name)
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Session record {name} must be an ISO timestamp") from exc
    if timestamp.tzinfo is None:
        raise ValueError(f"Session record {name} must include a timezone")
    return timestamp


def _optional_timestamp_from_record(
    record: Mapping[str, Any],
    name: str,
) -> datetime | None:
    if record.get(name) is None:
        return None
    return _timestamp_from_record(record, name)
