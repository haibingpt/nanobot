"""Event types for the message bus."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class InboundMessage:
    """Message received from a chat channel."""

    channel: str  # telegram, discord, slack, whatsapp
    sender_id: str  # User identifier
    chat_id: str  # Chat/channel identifier
    content: str  # Message text
    timestamp: datetime = field(default_factory=datetime.now)
    media: list[str] = field(default_factory=list)  # Media URLs
    metadata: dict[str, Any] = field(default_factory=dict)  # Channel-specific data
    session_key_override: str | None = None  # Optional override for thread-scoped sessions

    @property
    def session_key(self) -> str:
        """Unique key for session identification."""
        return self.session_key_override or f"{self.channel}:{self.chat_id}"


@dataclass(slots=True)
class TurnContext:
    """Routing and identity context for a single turn — eliminates the
    channel/chat_id/message_id/channel_name/sender_name data clump."""

    channel: str
    chat_id: str
    message_id: str | None = None
    channel_name: str | None = None
    channel_scope_id: str | None = None  # parent channel ID for directory naming
    sender_name: str | None = None
    sender_id: str | None = None

    @classmethod
    def from_message(cls, msg: "InboundMessage") -> "TurnContext":
        return cls(
            channel=msg.channel,
            chat_id=msg.chat_id,
            message_id=msg.metadata.get("message_id"),
            channel_name=msg.metadata.get("channel_name"),
            channel_scope_id=msg.metadata.get("channel_scope_id"),
            sender_name=msg.metadata.get("sender_name"),
            sender_id=msg.sender_id,
        )


@dataclass
class OutboundMessage:
    """Message to send to a chat channel."""

    channel: str
    chat_id: str
    content: str
    reply_to: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


