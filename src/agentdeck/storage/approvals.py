"""Approval request registry."""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agentdeck.core.config import Workspace
from agentdeck.core.events import AgentEvent, EventKind


APPROVAL_STATUSES = {"pending", "approved", "rejected"}


@dataclass
class ApprovalRecord:
    """One backend approval request."""

    approval_id: str
    title: str
    status: str = "pending"
    agent_id: str = "default"
    session_id: str = ""
    project_id: str = ""
    task_id: str = ""
    adapter: str = ""
    provider: str = ""
    project_dir: str = ""
    request_text: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    resolved_at: float = 0.0
    resolved_by: str = ""
    resolution_note: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApprovalRecord":
        return cls(
            approval_id=str(data["approval_id"]),
            title=str(data.get("title") or data["approval_id"]),
            status=str(data.get("status") or "pending"),
            agent_id=str(data.get("agent_id") or "default"),
            session_id=str(data.get("session_id") or ""),
            project_id=str(data.get("project_id") or ""),
            task_id=str(data.get("task_id") or ""),
            adapter=str(data.get("adapter") or ""),
            provider=str(data.get("provider") or ""),
            project_dir=str(data.get("project_dir") or ""),
            request_text=str(data.get("request_text") or ""),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            resolved_at=float(data.get("resolved_at") or 0.0),
            resolved_by=str(data.get("resolved_by") or ""),
            resolution_note=str(data.get("resolution_note") or ""),
            payload=dict(data.get("payload") or {}),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ApprovalRegistry:
    """JSON-backed approval queue."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    @property
    def path(self) -> Path:
        return self.workspace.approvals_dir / "registry.json"

    def record_request(
        self,
        event: AgentEvent,
        *,
        adapter: str,
        project_dir: str | Path,
        project_id: str = "",
        task_id: str = "",
    ) -> ApprovalRecord:
        if event.kind != EventKind.APPROVAL_REQUESTED:
            raise ValueError(f"cannot record non-approval event: {event.kind.value}")

        records = self._read()
        existing = self._find_by_event_id(records, event.event_id)
        if existing is not None:
            return existing

        now = time.time()
        provider = _provider_from_payload(event.payload) or adapter
        request_text = event.text or _extract_text(event.payload) or "Approval requested"
        title = _title_from_text(request_text)
        record = ApprovalRecord(
            approval_id=_new_approval_id(),
            title=title,
            status="pending",
            agent_id=event.agent_id,
            session_id=event.session_id,
            project_id=_normalize_token(project_id) if project_id else "",
            task_id=task_id,
            adapter=adapter,
            provider=provider,
            project_dir=str(Path(project_dir).expanduser().resolve()),
            request_text=request_text,
            created_at=now,
            updated_at=now,
            payload=event.payload,
            metadata={"source_event_id": event.event_id, "source_event_created_at": event.created_at},
        )
        records[record.approval_id] = record
        self._write(records)
        return record

    def get(self, approval_id: str) -> ApprovalRecord | None:
        return self._read().get(approval_id)

    def resolve(self, value: str) -> ApprovalRecord | None:
        records = self._read()
        if value in records:
            return records[value]
        matches = [record for record in records.values() if record.title == value]
        if not matches:
            return None
        return sorted(matches, key=lambda item: item.updated_at, reverse=True)[0]

    def list(
        self,
        *,
        status: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
    ) -> list[ApprovalRecord]:
        records = list(self._read().values())
        if status:
            checked_status = _validate_status(status)
            records = [record for record in records if record.status == checked_status]
        if project_id:
            records = [record for record in records if record.project_id == _normalize_token(project_id)]
        if task_id:
            records = [record for record in records if record.task_id == task_id]
        if agent_id:
            records = [record for record in records if record.agent_id == _normalize_token(agent_id)]
        return sorted(records, key=lambda item: (item.status != "pending", -item.updated_at))

    def resolve_request(
        self,
        approval: str,
        *,
        status: str,
        resolved_by: str = "cli",
        note: str = "",
    ) -> ApprovalRecord | None:
        records = self._read()
        record = self.resolve(approval)
        if record is None:
            return None
        record.status = _validate_status(status)
        now = time.time()
        record.updated_at = now
        record.resolved_at = now
        record.resolved_by = resolved_by
        record.resolution_note = note
        records[record.approval_id] = record
        self._write(records)
        return record

    def _find_by_event_id(self, records: dict[str, ApprovalRecord], event_id: str) -> ApprovalRecord | None:
        for record in records.values():
            if record.metadata.get("source_event_id") == event_id:
                return record
        return None

    def _read(self) -> dict[str, ApprovalRecord]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        raw_records = data.get("approvals", data)
        if not isinstance(raw_records, dict):
            return {}

        records: dict[str, ApprovalRecord] = {}
        for key, value in raw_records.items():
            if not isinstance(value, dict):
                continue
            try:
                record = ApprovalRecord.from_dict(value)
            except (KeyError, TypeError, ValueError):
                continue
            records[str(key)] = record
        return records

    def _write(self, records: dict[str, ApprovalRecord]) -> None:
        self.workspace.approvals_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "approvals": {key: record.to_dict() for key, record in sorted(records.items())},
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


def _new_approval_id() -> str:
    return f"apr-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _validate_status(value: str) -> str:
    status = _normalize_token(value)
    if status not in APPROVAL_STATUSES:
        raise ValueError(f"unsupported approval status: {value}")
    return status


def _title_from_text(text: str, *, max_chars: int = 72) -> str:
    clean = " ".join(text.strip().split()) or "Approval requested"
    if len(clean) <= max_chars:
        return clean
    return clean[: max_chars - 1].rstrip() + "..."


def _provider_from_payload(payload: dict[str, Any]) -> str:
    for key in ("provider", "backend", "source"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return _normalize_token(value)
    return ""


def _extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_extract_text(item) for item in value if _extract_text(item))
    if not isinstance(value, dict):
        return ""
    for key in ("text", "message", "summary", "command", "reason"):
        item = value.get(key)
        if isinstance(item, str) and item:
            return item
        if isinstance(item, (dict, list)):
            text = _extract_text(item)
            if text:
                return text
    return ""


def _normalize_token(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip().lower()).strip(".-") or "default"
