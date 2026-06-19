"""Task board storage."""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agentdeck.core.config import Workspace


TASK_STATUSES = {"todo", "doing", "blocked", "review", "done"}
TASK_PRIORITIES = {"low", "normal", "high", "urgent"}


@dataclass
class TaskRecord:
    """One task on the project board."""

    task_id: str
    title: str
    description: str = ""
    status: str = "todo"
    priority: str = "normal"
    project_id: str = ""
    agent_id: str = "owner"
    team_id: str = "default"
    session_id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    notes: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskRecord":
        notes = data.get("notes") or []
        if not isinstance(notes, list):
            notes = []
        return cls(
            task_id=str(data["task_id"]),
            title=str(data.get("title") or data["task_id"]),
            description=str(data.get("description") or ""),
            status=str(data.get("status") or "todo"),
            priority=str(data.get("priority") or "normal"),
            project_id=str(data.get("project_id") or ""),
            agent_id=str(data.get("agent_id") or "owner"),
            team_id=str(data.get("team_id") or "default"),
            session_id=str(data.get("session_id") or ""),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            notes=[dict(note) for note in notes if isinstance(note, dict)],
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TaskBoard:
    """JSON-backed task board."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    @property
    def path(self) -> Path:
        return self.workspace.board_dir / "tasks.json"

    def create(
        self,
        *,
        title: str,
        description: str = "",
        project_id: str = "",
        agent_id: str = "owner",
        team_id: str = "default",
        priority: str = "normal",
        status: str = "todo",
    ) -> TaskRecord:
        title = _clean_text(title)
        if not title:
            raise ValueError("task title is empty")
        status = _validate_status(status)
        priority = _validate_priority(priority)

        records = self._read()
        task_id = _new_task_id()
        while task_id in records:
            task_id = _new_task_id()

        now = time.time()
        record = TaskRecord(
            task_id=task_id,
            title=title,
            description=description,
            status=status,
            priority=priority,
            project_id=_normalize_token(project_id),
            agent_id=_normalize_token(agent_id or "owner"),
            team_id=_normalize_token(team_id or "default"),
            created_at=now,
            updated_at=now,
        )
        records[task_id] = record
        self._write(records)
        return record

    def get(self, task_id: str) -> TaskRecord | None:
        return self._read().get(task_id)

    def resolve(self, value: str) -> TaskRecord | None:
        records = self._read()
        if value in records:
            return records[value]
        normalized = _maybe_normalize_id(value)
        if normalized in records:
            return records[normalized]
        matches = [record for record in records.values() if record.title == value]
        if not matches:
            return None
        return sorted(matches, key=lambda item: item.updated_at, reverse=True)[0]

    def list(
        self,
        *,
        project_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
    ) -> list[TaskRecord]:
        records = list(self._read().values())
        if project_id:
            records = [record for record in records if record.project_id == _normalize_token(project_id)]
        if agent_id:
            records = [record for record in records if record.agent_id == _normalize_token(agent_id)]
        if status:
            records = [record for record in records if record.status == _validate_status(status)]
        return sorted(records, key=lambda item: (item.status == "done", -item.updated_at))

    def set_status(self, task: str, status: str, *, note: str = "") -> TaskRecord | None:
        records = self._read()
        record = self.resolve(task)
        if record is None:
            return None
        record.status = _validate_status(status)
        record.updated_at = time.time()
        if note:
            _append_note(record, note, kind=f"status:{record.status}")
        records[record.task_id] = record
        self._write(records)
        return record

    def add_note(self, task: str, note: str, *, kind: str = "note") -> TaskRecord | None:
        records = self._read()
        record = self.resolve(task)
        if record is None:
            return None
        _append_note(record, note, kind=kind)
        record.updated_at = time.time()
        records[record.task_id] = record
        self._write(records)
        return record

    def attach_session(self, task: str, session_id: str) -> TaskRecord | None:
        records = self._read()
        record = self.resolve(task)
        if record is None:
            return None
        record.session_id = session_id
        record.updated_at = time.time()
        records[record.task_id] = record
        self._write(records)
        return record

    def _read(self) -> dict[str, TaskRecord]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        raw_records = data.get("tasks", data)
        if not isinstance(raw_records, dict):
            return {}

        records: dict[str, TaskRecord] = {}
        for key, value in raw_records.items():
            if not isinstance(value, dict):
                continue
            try:
                record = TaskRecord.from_dict(value)
            except (KeyError, TypeError, ValueError):
                continue
            records[str(key)] = record
        return records

    def _write(self, records: dict[str, TaskRecord]) -> None:
        self.workspace.board_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "tasks": {key: record.to_dict() for key, record in sorted(records.items())},
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


def _append_note(record: TaskRecord, text: str, *, kind: str) -> None:
    clean = _clean_text(text)
    if not clean:
        return
    record.notes.append(
        {
            "kind": kind,
            "text": clean,
            "created_at": time.time(),
        }
    )


def _new_task_id() -> str:
    return f"task-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _validate_status(value: str) -> str:
    status = _normalize_token(value)
    if status not in TASK_STATUSES:
        raise ValueError(f"unsupported task status: {value}")
    return status


def _validate_priority(value: str) -> str:
    priority = _normalize_token(value)
    if priority not in TASK_PRIORITIES:
        raise ValueError(f"unsupported task priority: {value}")
    return priority


def _normalize_token(value: str) -> str:
    return _maybe_normalize_id(value) or "default"


def _maybe_normalize_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip().lower()).strip(".-")


def _clean_text(value: str) -> str:
    return " ".join(value.strip().split())
