"""JSONL event log."""

from __future__ import annotations

import json
from pathlib import Path

from agentdeck.core.config import Workspace
from agentdeck.core.events import AgentEvent


class EventLog:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    @property
    def path(self) -> Path:
        return self.workspace.events_dir / "events.jsonl"

    def append(self, event: AgentEvent) -> None:
        self.workspace.events_dir.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")

    def tail(self, limit: int = 50) -> list[AgentEvent]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        events: list[AgentEvent] = []
        for line in lines[-limit:]:
            try:
                events.append(AgentEvent.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                continue
        return events

