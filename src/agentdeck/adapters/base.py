"""Adapter interface for AI backends."""

from __future__ import annotations

from typing import AsyncIterator, Protocol

from agentdeck.core.cancel import CancellationToken
from agentdeck.core.config import Workspace
from agentdeck.core.events import AgentEvent


class AgentAdapter(Protocol):
    """Common interface implemented by Codex/Kimi/Claude/DeepSeek adapters."""

    name: str

    def send(
        self,
        prompt: str,
        *,
        agent_id: str,
        session_id: str,
        workspace: Workspace,
        cancellation: CancellationToken | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Send one user prompt and stream structured events."""
        ...
