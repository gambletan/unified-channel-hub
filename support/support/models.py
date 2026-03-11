"""Core data models for the support system."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
import uuid


class TicketStatus(str, Enum):
    OPEN = "open"
    ESCALATED = "escalated"
    ASSIGNED = "assigned"
    RESOLVED = "resolved"
    CLOSED = "closed"


class Priority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class AgentStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"


@dataclass
class Ticket:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    channel: str = ""
    chat_id: str = ""
    customer_id: str = ""
    customer_name: str | None = None
    subject: str | None = None
    status: TicketStatus = TicketStatus.OPEN
    priority: Priority = Priority.NORMAL
    assigned_agent_id: str | None = None
    language: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TicketMessage:
    id: int = 0
    ticket_id: str = ""
    role: str = "customer"  # customer | ai | agent
    sender_id: str | None = None
    sender_name: str | None = None
    content: str = ""
    channel: str | None = None
    from_id: str | None = None  # sender: customer_id, agent.id, or "ai:{model}"
    to_id: str | None = None    # receiver: customer_id, agent.id, or "ai:{model}"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Agent:
    id: str = ""
    name: str = ""
    email: str | None = None
    channel: str | None = None  # which IM the agent uses
    chat_id: str | None = None  # agent's chat_id on that channel
    status: AgentStatus = AgentStatus.OFFLINE
    max_concurrent: int = 5
    current_load: int = 0
    skills: list[str] = field(default_factory=list)  # e.g. ["billing", "tech"]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class KBArticle:
    id: int = 0
    title: str = ""
    content: str = ""
    category: str | None = None
    tags: list[str] = field(default_factory=list)
    source_path: str | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class CustomerBinding:
    """Maps a platform user ID to a channel-specific identity."""
    id: int = 0
    platform_user_id: str = ""  # Your platform's user ID
    channel: str = ""           # telegram, wechat, whatsapp, etc.
    chat_id: str = ""           # Channel-specific chat/user ID
    bound_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SatisfactionRating:
    id: int = 0
    ticket_id: str = ""
    rating: int = 0  # 1-5
    comment: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
