"""Structured events emitted by adapters and the runtime."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventKind(str, Enum):
    USER_MESSAGE = "user_message"
    STATUS = "status"
    SESSION_STARTED = "session_started"
    SESSION_IDLE = "session_idle"
    ASSISTANT_DELTA = "assistant_delta"
    ASSISTANT_FINAL = "assistant_final"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    MEMORY_UPDATED = "memory_updated"
    ERROR = "error"


@dataclass(frozen=True)
class AgentEvent:
    """One observable runtime event."""

    kind: EventKind
    agent_id: str
    session_id: str
    text: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind.value,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "text": self.text,
            "payload": self.payload,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentEvent":
        return cls(
            kind=EventKind(str(data["kind"])),
            agent_id=str(data["agent_id"]),
            session_id=str(data["session_id"]),
            text=str(data.get("text") or ""),
            payload=dict(data.get("payload") or {}),
            event_id=str(data.get("event_id") or uuid.uuid4().hex),
            created_at=float(data.get("created_at") or time.time()),
        )
