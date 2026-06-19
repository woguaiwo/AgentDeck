"""Debug adapter used for tests and local smoke runs."""

from __future__ import annotations

from typing import AsyncIterator

from agentdeck.core.cancel import CancellationToken
from agentdeck.core.config import Workspace
from agentdeck.core.events import AgentEvent, EventKind


class EchoAdapter:
    name = "echo"

    async def send(
        self,
        prompt: str,
        *,
        agent_id: str,
        session_id: str,
        workspace: Workspace,
        cancellation: CancellationToken | None = None,
    ) -> AsyncIterator[AgentEvent]:
        if cancellation is not None and cancellation.is_cancelled():
            yield AgentEvent(EventKind.CANCELLED, agent_id, session_id, text=cancellation.reason)
            return
        text = f"Echo: {prompt}"
        yield AgentEvent(EventKind.ASSISTANT_DELTA, agent_id, session_id, text=text)
        yield AgentEvent(EventKind.ASSISTANT_FINAL, agent_id, session_id, text=text)
