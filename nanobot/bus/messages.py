"""Route-aware messages exchanged through the in-memory message bus."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class InboundMessage:
    """One user message waiting for agent processing."""

    channel: str
    chat_id: str
    sender_id: str
    session_id: str
    content: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate inbound routing and copy metadata into this message."""
        _validate_routing(
            self.channel,
            self.chat_id,
            self.sender_id,
            self.session_id,
        )
        if not isinstance(self.content, str):
            raise TypeError("InboundMessage content must be a string")
        _validate_metadata(self.metadata)
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True)
class OutboundMessage:
    """One final agent response ready for delivery to a channel."""

    channel: str
    chat_id: str
    sender_id: str
    session_id: str
    content: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate outbound routing and copy metadata into this message."""
        _validate_routing(
            self.channel,
            self.chat_id,
            self.sender_id,
            self.session_id,
        )
        if not isinstance(self.content, str):
            raise TypeError("OutboundMessage content must be a string")
        _validate_metadata(self.metadata)
        object.__setattr__(self, "metadata", dict(self.metadata))


def _validate_routing(
    channel: str,
    chat_id: str,
    sender_id: str,
    session_id: str,
) -> None:
    """Check routing field types and the required channel and chat identifiers."""
    for name, value in (
        ("channel", channel),
        ("chat_id", chat_id),
        ("sender_id", sender_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
    if not isinstance(session_id, str):
        raise TypeError("session_id must be a string")


def _validate_metadata(metadata: Mapping[str, Any]) -> None:
    """Require mapping-shaped metadata before making a per-message copy."""
    if not isinstance(metadata, Mapping):
        raise TypeError("message metadata must be a mapping")
