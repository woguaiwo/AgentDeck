"""Small runtime that connects adapters to storage."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from agentdeck.adapters.base import AgentAdapter
from agentdeck.core.config import Workspace
from agentdeck.core.events import AgentEvent, EventKind
from agentdeck.storage.event_log import EventLog
from agentdeck.storage.sessions import SessionRegistry


@dataclass
class RunResult:
    session_id: str
    final_text: str
    events: list[AgentEvent]


class AgentRuntime:
    """Run a prompt through an adapter and persist its event stream."""

    def __init__(
        self,
        workspace: Workspace,
        adapter: AgentAdapter,
        agent_id: str = "default",
        *,
        project_dir: str | Path | None = None,
        session_registry: SessionRegistry | None = None,
    ) -> None:
        self.workspace = workspace
        self.adapter = adapter
        self.agent_id = agent_id
        self.event_log = EventLog(workspace)
        self.project_dir = Path(project_dir or Path.cwd()).expanduser().resolve()
        self.session_registry = session_registry or SessionRegistry(workspace)

    async def run_prompt(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        title: str | None = None,
    ) -> RunResult:
        self.workspace.ensure()
        sid = session_id or uuid.uuid4().hex[:12]
        events: list[AgentEvent] = []

        self.session_registry.upsert_start(
            session_id=sid,
            agent_id=self.agent_id,
            adapter=self.adapter.name,
            project_dir=self.project_dir,
            prompt=prompt,
            title=title,
        )
        start = AgentEvent(EventKind.SESSION_STARTED, self.agent_id, sid, payload={"adapter": self.adapter.name})
        user = AgentEvent(EventKind.USER_MESSAGE, self.agent_id, sid, text=prompt)
        for event in [start, user]:
            self.event_log.append(event)
            self.session_registry.update_from_event(event)
            events.append(event)

        final_text = ""
        async for event in self.adapter.send(prompt, agent_id=self.agent_id, session_id=sid, workspace=self.workspace):
            self.event_log.append(event)
            self.session_registry.update_from_event(event)
            events.append(event)
            if event.kind == EventKind.ASSISTANT_FINAL:
                final_text = event.text

        idle = AgentEvent(EventKind.SESSION_IDLE, self.agent_id, sid)
        self.event_log.append(idle)
        self.session_registry.update_from_event(idle)
        events.append(idle)
        return RunResult(session_id=sid, final_text=final_text, events=events)
