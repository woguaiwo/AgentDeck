"""Small runtime that connects adapters to storage."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from agentdeck.adapters.base import AgentAdapter
from agentdeck.core.config import Workspace
from agentdeck.core.events import AgentEvent, EventKind
from agentdeck.storage.event_log import EventLog


@dataclass
class RunResult:
    session_id: str
    final_text: str
    events: list[AgentEvent]


class AgentRuntime:
    """Run a prompt through an adapter and persist its event stream."""

    def __init__(self, workspace: Workspace, adapter: AgentAdapter, agent_id: str = "default") -> None:
        self.workspace = workspace
        self.adapter = adapter
        self.agent_id = agent_id
        self.event_log = EventLog(workspace)

    async def run_prompt(self, prompt: str, *, session_id: str | None = None) -> RunResult:
        self.workspace.ensure()
        sid = session_id or uuid.uuid4().hex[:12]
        events: list[AgentEvent] = []

        start = AgentEvent(EventKind.SESSION_STARTED, self.agent_id, sid, payload={"adapter": self.adapter.name})
        user = AgentEvent(EventKind.USER_MESSAGE, self.agent_id, sid, text=prompt)
        for event in [start, user]:
            self.event_log.append(event)
            events.append(event)

        final_text = ""
        async for event in self.adapter.send(prompt, agent_id=self.agent_id, session_id=sid, workspace=self.workspace):
            self.event_log.append(event)
            events.append(event)
            if event.kind == EventKind.ASSISTANT_FINAL:
                final_text = event.text

        idle = AgentEvent(EventKind.SESSION_IDLE, self.agent_id, sid)
        self.event_log.append(idle)
        events.append(idle)
        return RunResult(session_id=sid, final_text=final_text, events=events)

