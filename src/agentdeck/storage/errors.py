"""Error incident storage and rule decisions."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from agentdeck.core.config import Workspace
from agentdeck.core.events import AgentEvent
from agentdeck.storage.jobs import JobRecord


INCIDENT_STATUSES = {"pending", "resolved"}
DECISION_ACTIONS = {"pass", "pause_auto", "need_user"}
UNKNOWN_ERROR_KINDS = {"unknown", "process_failed"}


@dataclass
class ErrorIncidentRecord:
    incident_id: str
    status: str = "pending"
    job_id: str = ""
    interface: str = ""
    chat_id: int = 0
    task_id: str = ""
    session_id: str = ""
    agent_id: str = ""
    adapter: str = ""
    error_kind: str = "unknown"
    hint: str = ""
    text: str = ""
    fingerprint: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    job_metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ErrorIncidentRecord":
        return cls(
            incident_id=str(data["incident_id"]),
            status=_validate_status(str(data.get("status") or "pending")),
            job_id=str(data.get("job_id") or ""),
            interface=str(data.get("interface") or ""),
            chat_id=int(data.get("chat_id") or 0),
            task_id=str(data.get("task_id") or ""),
            session_id=str(data.get("session_id") or ""),
            agent_id=str(data.get("agent_id") or ""),
            adapter=str(data.get("adapter") or ""),
            error_kind=str(data.get("error_kind") or "unknown"),
            hint=str(data.get("hint") or ""),
            text=str(data.get("text") or ""),
            fingerprint=str(data.get("fingerprint") or ""),
            payload=dict(data.get("payload") or {}),
            job_metadata=dict(data.get("job_metadata") or {}),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ErrorDecisionRecord:
    decision_id: str
    incident_id: str
    action: str
    reason: str = ""
    notify_user: bool = False
    record_unknown: bool = False
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ErrorDecisionRecord":
        return cls(
            decision_id=str(data["decision_id"]),
            incident_id=str(data["incident_id"]),
            action=_validate_action(str(data.get("action") or "need_user")),
            reason=str(data.get("reason") or ""),
            notify_user=bool(data.get("notify_user")),
            record_unknown=bool(data.get("record_unknown")),
            created_at=float(data.get("created_at") or time.time()),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UnknownErrorRecord:
    fingerprint: str
    error_kind: str
    text: str
    first_incident_id: str
    count: int = 1
    last_incident_id: str = ""
    last_seen_at: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UnknownErrorRecord":
        return cls(
            fingerprint=str(data["fingerprint"]),
            error_kind=str(data.get("error_kind") or "unknown"),
            text=str(data.get("text") or ""),
            first_incident_id=str(data.get("first_incident_id") or ""),
            count=int(data.get("count") or 1),
            last_incident_id=str(data.get("last_incident_id") or ""),
            last_seen_at=float(data.get("last_seen_at") or time.time()),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ErrorIncidentStore:
    """JSON-backed error incidents plus append-only decisions."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    @property
    def path(self):
        return self.workspace.errors_dir / "incidents.json"

    @property
    def decisions_path(self):
        return self.workspace.errors_dir / "decisions.jsonl"

    @property
    def unknown_path(self):
        return self.workspace.errors_dir / "unknown.json"

    def create_from_event(self, *, job: JobRecord, event: AgentEvent, adapter: str = "") -> ErrorIncidentRecord:
        payload = dict(event.payload or {})
        text = _incident_text(event, payload)
        fingerprint = error_fingerprint(text=text, error_kind=str(payload.get("error_kind") or "unknown"), adapter=adapter)
        record = ErrorIncidentRecord(
            incident_id=_new_incident_id(),
            job_id=job.job_id,
            interface=job.interface,
            chat_id=job.chat_id,
            task_id=job.task_id,
            session_id=event.session_id or job.session_id,
            agent_id=event.agent_id,
            adapter=adapter or str(payload.get("adapter") or ""),
            error_kind=str(payload.get("error_kind") or "unknown"),
            hint=str(payload.get("hint") or ""),
            text=text,
            fingerprint=fingerprint,
            payload=payload,
            job_metadata=dict(job.metadata or {}),
        )
        records = self._read()
        records[record.incident_id] = record
        self._write(records)
        return record

    def list(self, *, status: str | None = None, limit: int | None = None) -> list[ErrorIncidentRecord]:
        records = list(self._read().values())
        if status:
            clean = _validate_status(status)
            records = [record for record in records if record.status == clean]
        records = sorted(records, key=lambda item: item.updated_at, reverse=True)
        if limit is not None:
            records = records[:limit]
        return records

    def get(self, incident_id: str) -> ErrorIncidentRecord | None:
        return self._read().get(incident_id)

    def mark_resolved(self, incident_id: str) -> ErrorIncidentRecord | None:
        records = self._read()
        record = records.get(incident_id)
        if record is None:
            return None
        record.status = "resolved"
        record.updated_at = time.time()
        records[incident_id] = record
        self._write(records)
        return record

    def append_decision(self, incident: ErrorIncidentRecord, decision: ErrorDecisionRecord) -> None:
        self.workspace.errors_dir.mkdir(parents=True, exist_ok=True)
        with self.decisions_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(decision.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
        if decision.record_unknown:
            self.record_unknown(incident)

    def decisions(self, *, limit: int = 20) -> list[ErrorDecisionRecord]:
        if not self.decisions_path.exists():
            return []
        lines = self.decisions_path.read_text(encoding="utf-8", errors="replace").splitlines()
        records: list[ErrorDecisionRecord] = []
        for line in lines[-limit:]:
            try:
                records.append(ErrorDecisionRecord.from_dict(json.loads(line)))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return records

    def record_unknown(self, incident: ErrorIncidentRecord) -> UnknownErrorRecord:
        records = self._read_unknown()
        existing = records.get(incident.fingerprint)
        now = time.time()
        if existing is None:
            existing = UnknownErrorRecord(
                fingerprint=incident.fingerprint,
                error_kind=incident.error_kind,
                text=incident.text[:1000],
                first_incident_id=incident.incident_id,
                last_incident_id=incident.incident_id,
                last_seen_at=now,
            )
        else:
            existing.count += 1
            existing.last_incident_id = incident.incident_id
            existing.last_seen_at = now
        records[incident.fingerprint] = existing
        self._write_unknown(records)
        return existing

    def unknowns(self, *, limit: int = 20) -> list[UnknownErrorRecord]:
        records = sorted(self._read_unknown().values(), key=lambda item: item.last_seen_at, reverse=True)
        return records[:limit]

    def _read(self) -> dict[str, ErrorIncidentRecord]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        raw = data.get("incidents", data) if isinstance(data, dict) else {}
        if not isinstance(raw, dict):
            return {}
        records: dict[str, ErrorIncidentRecord] = {}
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue
            try:
                records[str(key)] = ErrorIncidentRecord.from_dict(value)
            except (KeyError, TypeError, ValueError):
                continue
        return records

    def _write(self, records: dict[str, ErrorIncidentRecord]) -> None:
        self.workspace.errors_dir.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "incidents": {key: value.to_dict() for key, value in sorted(records.items())}}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def _read_unknown(self) -> dict[str, UnknownErrorRecord]:
        if not self.unknown_path.exists():
            return {}
        try:
            data = json.loads(self.unknown_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        raw = data.get("unknowns", data) if isinstance(data, dict) else {}
        if not isinstance(raw, dict):
            return {}
        records: dict[str, UnknownErrorRecord] = {}
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue
            try:
                records[str(key)] = UnknownErrorRecord.from_dict(value)
            except (KeyError, TypeError, ValueError):
                continue
        return records

    def _write_unknown(self, records: dict[str, UnknownErrorRecord]) -> None:
        self.workspace.errors_dir.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "unknowns": {key: value.to_dict() for key, value in sorted(records.items())}}
        tmp = self.unknown_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.unknown_path)


def decide_incident(incident: ErrorIncidentRecord) -> ErrorDecisionRecord:
    kind = incident.error_kind
    if kind == "usage_warning":
        return _decision(incident, action="pass", reason="Usage warning only; keep the job result if one exists.")
    if kind in {"rate_limit", "network_error"}:
        return _decision(incident, action="pause_auto", notify_user=True, reason="Backend is temporarily unavailable or quota-limited.")
    if kind in {"approval_required", "auth_failed", "invalid_resume_session", "model_not_found", "working_directory_not_found", "command_not_found"}:
        return _decision(incident, action="need_user", notify_user=True, reason="The backend needs user action before continuing.")
    return _decision(
        incident,
        action="need_user",
        notify_user=True,
        record_unknown=kind in UNKNOWN_ERROR_KINDS,
        reason="Unknown or process-level adapter error; record it for future policy.",
    )


def error_fingerprint(*, text: str, error_kind: str, adapter: str = "") -> str:
    normalized = " ".join((text or "").lower().split())[:800]
    seed = f"{adapter}|{error_kind}|{normalized}"
    return hashlib.sha256(seed.encode("utf-8", errors="replace")).hexdigest()[:16]


def _incident_text(event: AgentEvent, payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for value in (event.text, payload.get("stderr"), payload.get("error")):
        text = str(value or "").strip()
        if text and text not in parts:
            parts.append(text)
    return "\n".join(parts)


def _decision(
    incident: ErrorIncidentRecord,
    *,
    action: str,
    reason: str,
    notify_user: bool = False,
    record_unknown: bool = False,
) -> ErrorDecisionRecord:
    return ErrorDecisionRecord(
        decision_id=f"decision-{uuid.uuid4().hex[:12]}",
        incident_id=incident.incident_id,
        action=_validate_action(action),
        reason=reason,
        notify_user=notify_user,
        record_unknown=record_unknown,
        metadata={"error_kind": incident.error_kind, "fingerprint": incident.fingerprint},
    )


def _new_incident_id() -> str:
    return f"incident-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _validate_status(value: str) -> str:
    status = value.strip().lower().replace("-", "_")
    if status not in INCIDENT_STATUSES:
        raise ValueError(f"unsupported incident status: {value}")
    return status


def _validate_action(value: str) -> str:
    action = value.strip().lower().replace("-", "_")
    if action not in DECISION_ACTIONS:
        raise ValueError(f"unsupported error decision action: {value}")
    return action
