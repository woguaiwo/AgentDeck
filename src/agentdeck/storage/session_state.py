"""Bounded session state cards for resume, auto mode, and handoffs."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agentdeck.core.config import Workspace
from agentdeck.storage.progress import ProgressEntry


@dataclass
class SessionStateCard:
    """Compact state for one provider-backed working session."""

    session_id: str
    objective: str = ""
    current_state: str = ""
    next_step: str = ""
    task_id: str = ""
    project_id: str = ""
    agent_id: str = ""
    blockers: list[str] = field(default_factory=list)
    verified_work: list[str] = field(default_factory=list)
    active_artifacts: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    source_progress_id: str = ""
    updated_at: float = field(default_factory=time.time)
    schema_version: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionStateCard":
        return cls(
            session_id=str(data["session_id"]),
            objective=str(data.get("objective") or ""),
            current_state=str(data.get("current_state") or ""),
            next_step=str(data.get("next_step") or ""),
            task_id=str(data.get("task_id") or ""),
            project_id=str(data.get("project_id") or ""),
            agent_id=str(data.get("agent_id") or ""),
            blockers=_string_list(data.get("blockers")),
            verified_work=_string_list(data.get("verified_work")),
            active_artifacts=_string_list(data.get("active_artifacts")),
            decisions=_string_list(data.get("decisions")),
            source_progress_id=str(data.get("source_progress_id") or ""),
            updated_at=float(data.get("updated_at") or time.time()),
            schema_version=int(data.get("schema_version") or 1),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SessionStateStore:
    """JSON file store for compact session state cards."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def path_for(self, session_id: str) -> Path:
        return self.workspace.session_state_dir / f"{_slug(session_id)}.json"

    def get(self, session_id: str) -> SessionStateCard | None:
        path = self.path_for(session_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            return SessionStateCard.from_dict(data)
        except (KeyError, TypeError, ValueError):
            return None

    def write(self, card: SessionStateCard) -> SessionStateCard:
        self.workspace.ensure()
        card.updated_at = time.time()
        path = self.path_for(card.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(card.to_dict(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
        return card

    def upsert_from_progress(self, entry: ProgressEntry, *, objective: str = "") -> SessionStateCard:
        if not entry.session_id:
            raise ValueError("progress entry has no session_id")
        card = self.get(entry.session_id) or SessionStateCard(session_id=entry.session_id)
        card.task_id = entry.task_id or card.task_id
        card.project_id = entry.project_id or card.project_id
        card.agent_id = entry.agent_id or card.agent_id
        card.objective = objective.strip() or card.objective
        card.current_state = _current_state_from_progress(entry) or card.current_state
        if entry.next_steps:
            card.next_step = entry.next_steps[0]
        if entry.blockers:
            card.blockers = entry.blockers
        card.verified_work = _merge_recent(card.verified_work, entry.verified + entry.completed)
        card.active_artifacts = _merge_recent(card.active_artifacts, entry.artifacts)
        card.decisions = _merge_recent(card.decisions, entry.decisions)
        card.source_progress_id = entry.entry_id
        return self.write(card)


def _current_state_from_progress(entry: ProgressEntry) -> str:
    if entry.completed:
        return "; ".join(entry.completed)
    return entry.summary


def _merge_recent(existing: list[str], incoming: list[str], *, limit: int = 20) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in existing + incoming:
        clean = " ".join(value.strip().split())
        if not clean or clean in seen:
            continue
        seen.add(clean)
        merged.append(clean)
    return merged[-limit:]


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", value.strip().lower()).strip("._") or "session"
