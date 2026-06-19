"""Project-local session registry."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agentdeck.core.config import Workspace
from agentdeck.core.events import AgentEvent, EventKind


@dataclass
class SessionRecord:
    """Compact index of one AgentDeck session."""

    session_id: str
    agent_id: str
    adapter: str
    project_dir: str
    status: str = "running"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    provider_session_id: str = ""
    provider_session_kind: str = ""
    last_user_message: str = ""
    last_assistant_final: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionRecord":
        return cls(
            session_id=str(data["session_id"]),
            agent_id=str(data.get("agent_id") or "default"),
            adapter=str(data.get("adapter") or "unknown"),
            project_dir=str(data.get("project_dir") or ""),
            status=str(data.get("status") or "unknown"),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            provider_session_id=str(data.get("provider_session_id") or ""),
            provider_session_kind=str(data.get("provider_session_kind") or ""),
            last_user_message=str(data.get("last_user_message") or ""),
            last_assistant_final=str(data.get("last_assistant_final") or ""),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SessionRegistry:
    """JSON-backed session index.

    The event log remains the source of truth. This registry is a small,
    overwrite-friendly index for listing sessions and resuming provider threads.
    """

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    @property
    def path(self) -> Path:
        return self.workspace.sessions_dir / "registry.json"

    def upsert_start(
        self,
        *,
        session_id: str,
        agent_id: str,
        adapter: str,
        project_dir: str | Path,
        prompt: str,
    ) -> SessionRecord:
        records = self._read()
        existing = records.get(session_id)
        now = time.time()
        if existing is None:
            record = SessionRecord(
                session_id=session_id,
                agent_id=agent_id,
                adapter=adapter,
                project_dir=str(Path(project_dir).expanduser().resolve()),
                last_user_message=prompt,
                created_at=now,
                updated_at=now,
            )
        else:
            record = existing
            record.agent_id = agent_id
            record.adapter = adapter
            record.project_dir = str(Path(project_dir).expanduser().resolve())
            record.status = "running"
            record.updated_at = now
            record.last_user_message = prompt
        records[session_id] = record
        self._write(records)
        return record

    def update_from_event(self, event: AgentEvent) -> None:
        records = self._read()
        record = records.get(event.session_id)
        if record is None:
            return

        record.updated_at = event.created_at
        if event.kind == EventKind.USER_MESSAGE:
            record.last_user_message = event.text
        elif event.kind == EventKind.STATUS:
            self._update_status_event(record, event)
        elif event.kind == EventKind.ASSISTANT_FINAL:
            record.status = "idle"
            record.last_assistant_final = event.text
        elif event.kind == EventKind.APPROVAL_REQUESTED:
            record.status = "waiting_approval"
        elif event.kind == EventKind.ERROR:
            if not bool(event.payload.get("nonfatal")):
                record.status = "error"
            record.metadata["last_error"] = event.text
        elif event.kind == EventKind.SESSION_IDLE:
            if record.status not in {"error", "waiting_approval"}:
                record.status = "idle"

        records[event.session_id] = record
        self._write(records)

    def get(self, session_id: str) -> SessionRecord | None:
        return self._read().get(session_id)

    def resolve(self, value: str) -> SessionRecord | None:
        records = self._read()
        if value in records:
            return records[value]

        matches = [
            record
            for record in records.values()
            if record.agent_id == value or record.provider_session_id == value
        ]
        if not matches:
            return None
        return sorted(matches, key=lambda item: item.updated_at, reverse=True)[0]

    def list(self, *, agent_id: str | None = None) -> list[SessionRecord]:
        records = list(self._read().values())
        if agent_id:
            records = [record for record in records if record.agent_id == agent_id]
        return sorted(records, key=lambda item: item.updated_at, reverse=True)

    def _update_status_event(self, record: SessionRecord, event: AgentEvent) -> None:
        payload = event.payload
        event_type = str(payload.get("type") or event.text or "").lower().replace(".", "_").replace("-", "_")
        if event_type == "thread_started":
            thread_id = str(payload.get("thread_id") or payload.get("id") or "")
            if thread_id:
                record.provider_session_id = thread_id
                record.provider_session_kind = "codex_thread"
            record.metadata["provider_start_event"] = payload
        elif event_type in {"turn_started", "turn_completed"}:
            record.metadata["last_provider_status"] = payload

    def _read(self) -> dict[str, SessionRecord]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        raw_records = data.get("sessions", data)
        if not isinstance(raw_records, dict):
            return {}
        records: dict[str, SessionRecord] = {}
        for key, value in raw_records.items():
            if not isinstance(value, dict):
                continue
            try:
                record = SessionRecord.from_dict(value)
            except (KeyError, TypeError, ValueError):
                continue
            records[str(key)] = record
        return records

    def _write(self, records: dict[str, SessionRecord]) -> None:
        self.workspace.sessions_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "sessions": {key: record.to_dict() for key, record in sorted(records.items())},
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
