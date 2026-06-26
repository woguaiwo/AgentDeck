"""Focus registry for session-first AgentDeck work."""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agentdeck.core.config import Workspace


FOCUS_STATUSES = {"active", "paused", "review", "done", "blocked"}


@dataclass
class FocusRecord:
    """A mutable objective owned by one directory-bound agent/session."""

    focus_id: str
    title: str
    description: str = ""
    status: str = "active"
    project_id: str = ""
    agent_id: str = ""
    directory: str = ""
    session_id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    notes: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FocusRecord":
        notes = data.get("notes") or []
        if not isinstance(notes, list):
            notes = []
        return cls(
            focus_id=str(data["focus_id"]),
            title=str(data.get("title") or data["focus_id"]),
            description=str(data.get("description") or ""),
            status=str(data.get("status") or "active"),
            project_id=str(data.get("project_id") or ""),
            agent_id=str(data.get("agent_id") or ""),
            directory=str(data.get("directory") or ""),
            session_id=str(data.get("session_id") or ""),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            notes=[dict(note) for note in notes if isinstance(note, dict)],
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FocusRegistry:
    """JSON-backed focus registry.

    Focus is intentionally lighter than a task: it names what an agent/session is
    currently trying to do, while the agent stays bound to its working directory.
    """

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    @property
    def path(self) -> Path:
        return self.workspace.focus_dir / "registry.json"

    def create(
        self,
        *,
        title: str,
        description: str = "",
        project_id: str = "",
        agent_id: str = "",
        directory: str | Path = "",
        session_id: str = "",
        status: str = "active",
        metadata: dict[str, Any] | None = None,
    ) -> FocusRecord:
        title = _clean_text(title)
        if not title:
            raise ValueError("focus title is empty")
        status = _validate_status(status)

        records = self._read()
        focus_id = _new_focus_id()
        while focus_id in records:
            focus_id = _new_focus_id()

        now = time.time()
        record = FocusRecord(
            focus_id=focus_id,
            title=title,
            description=description.strip(),
            status=status,
            project_id=_normalize_token(project_id) if project_id else "",
            agent_id=_normalize_token(agent_id) if agent_id else "",
            directory=str(Path(directory).expanduser().resolve()) if str(directory or "").strip() else "",
            session_id=session_id.strip(),
            created_at=now,
            updated_at=now,
            metadata=dict(metadata or {}),
        )
        records[focus_id] = record
        self._write(records)
        return record

    def get(self, focus_id: str) -> FocusRecord | None:
        return self._read().get(focus_id)

    def resolve(self, value: str) -> FocusRecord | None:
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
        directory: str | Path | None = None,
        status: str | None = None,
    ) -> list[FocusRecord]:
        records = list(self._read().values())
        if project_id:
            records = [record for record in records if record.project_id == _normalize_token(project_id)]
        if agent_id:
            records = [record for record in records if record.agent_id == _normalize_token(agent_id)]
        if directory:
            resolved = str(Path(directory).expanduser().resolve())
            records = [record for record in records if record.directory == resolved]
        if status:
            records = [record for record in records if record.status == _validate_status(status)]
        return sorted(records, key=lambda item: (item.status == "done", -item.updated_at))

    def set_status(self, focus: str, status: str, *, note: str = "") -> FocusRecord | None:
        records = self._read()
        record = self.resolve(focus)
        if record is None:
            return None
        record.status = _validate_status(status)
        if note:
            _append_note(record, note, kind=f"status:{record.status}")
        record.updated_at = time.time()
        records[record.focus_id] = record
        self._write(records)
        return record

    def add_note(self, focus: str, note: str, *, kind: str = "note") -> FocusRecord | None:
        records = self._read()
        record = self.resolve(focus)
        if record is None:
            return None
        _append_note(record, note, kind=kind)
        record.updated_at = time.time()
        records[record.focus_id] = record
        self._write(records)
        return record

    def attach_session(self, focus: str, session_id: str) -> FocusRecord | None:
        records = self._read()
        record = self.resolve(focus)
        if record is None:
            return None
        record.session_id = session_id.strip()
        record.updated_at = time.time()
        records[record.focus_id] = record
        self._write(records)
        return record

    def _read(self) -> dict[str, FocusRecord]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        raw_records = data.get("focus", data)
        if not isinstance(raw_records, dict):
            return {}

        records: dict[str, FocusRecord] = {}
        for key, value in raw_records.items():
            if not isinstance(value, dict):
                continue
            try:
                record = FocusRecord.from_dict(value)
            except (KeyError, TypeError, ValueError):
                continue
            records[str(key)] = record
        return records

    def _write(self, records: dict[str, FocusRecord]) -> None:
        self.workspace.focus_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "focus": {key: record.to_dict() for key, record in sorted(records.items())},
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


def _append_note(record: FocusRecord, text: str, *, kind: str) -> None:
    clean = _clean_note_text(text)
    if not clean:
        return
    record.notes.append({"kind": kind, "text": clean, "created_at": time.time()})


def _new_focus_id() -> str:
    return f"focus-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _validate_status(value: str) -> str:
    status = _normalize_token(value)
    if status not in FOCUS_STATUSES:
        raise ValueError(f"unsupported focus status: {value}")
    return status


def _normalize_token(value: str) -> str:
    return _maybe_normalize_id(value) or "default"


def _maybe_normalize_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip().lower()).strip(".-")


def _clean_text(value: str) -> str:
    return " ".join(value.strip().split())


def _clean_note_text(value: str) -> str:
    lines = []
    for line in str(value).strip().splitlines():
        clean = " ".join(line.strip().split())
        if clean:
            lines.append(clean)
    return "\n".join(lines)
