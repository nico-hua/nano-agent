"""Persistent turn evidence and the structured changes proposed by the model."""

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator


@dataclass(frozen=True)
class MemoryEvent:
    event_id: int
    session_key: str
    messages: tuple[dict[str, Any], ...]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MemoryContent(_StrictModel):
    kind: Literal["preference", "fact", "decision"]
    scope: Literal["user", "project"]
    text: str
    message_offset: int

    @field_validator("text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Empty memory text")
        return value.strip()


class AddMemory(MemoryContent):
    op: Literal["add"]


class ReplaceMemory(MemoryContent):
    op: Literal["replace"]
    target_id: int


class RetractMemory(_StrictModel):
    op: Literal["retract"]
    target_id: int
    message_offset: int


class MemoryChanges(_StrictModel):
    operations: list[AddMemory | ReplaceMemory | RetractMemory]


@dataclass(frozen=True)
class ActiveMemory:
    scope: str
    id: int
    kind: str
    text: str
    session_key: str
    message_offset: int
    event_id: int
