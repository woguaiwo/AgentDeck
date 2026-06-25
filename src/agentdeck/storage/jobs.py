"""Background job registry."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agentdeck.core.config import Workspace


JOB_STATUSES = {"queued", "running", "cancel_requested", "cancelled", "done", "error", "interrupted"}


@dataclass
class JobRecord:
    job_id: str
    interface: str
    status: str = "queued"
    task_id: str = ""
    prompt: str = ""
    chat_id: int = 0
    session_id: str = ""
    final_text: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobRecord":
        return cls(
            job_id=str(data["job_id"]),
            interface=str(data.get("interface") or "unknown"),
            status=_validate_status(str(data.get("status") or "queued")),
            task_id=str(data.get("task_id") or ""),
            prompt=str(data.get("prompt") or ""),
            chat_id=int(data.get("chat_id") or 0),
            session_id=str(data.get("session_id") or ""),
            final_text=str(data.get("final_text") or ""),
            error=str(data.get("error") or ""),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class JobRegistry:
    """JSON-backed index of remote interface background jobs."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    @property
    def path(self) -> Path:
        return self.workspace.jobs_dir / "registry.json"

    def create(
        self,
        *,
        interface: str,
        chat_id: int,
        task_id: str,
        prompt: str,
        job_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> JobRecord:
        records = self._read()
        resolved_id = job_id or _new_job_id()
        while resolved_id in records:
            resolved_id = _new_job_id()
        now = time.time()
        record = JobRecord(
            job_id=resolved_id,
            interface=interface,
            chat_id=chat_id,
            task_id=task_id,
            prompt=prompt,
            created_at=now,
            updated_at=now,
            metadata=dict(metadata or {}),
        )
        records[record.job_id] = record
        self._write(records)
        return record

    def get(self, job_id: str) -> JobRecord | None:
        return self._read().get(job_id)

    def list(
        self,
        *,
        interface: str | None = None,
        chat_id: int | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[JobRecord]:
        records = list(self._read().values())
        if interface:
            records = [record for record in records if record.interface == interface]
        if chat_id is not None:
            records = [record for record in records if record.chat_id == chat_id]
        if status:
            clean_status = _validate_status(status)
            records = [record for record in records if record.status == clean_status]
        records = sorted(records, key=lambda item: item.updated_at, reverse=True)
        if limit is not None:
            records = records[:limit]
        return records

    def set_status(self, job_id: str, status: str, *, error: str = "") -> JobRecord | None:
        records = self._read()
        record = records.get(job_id)
        if record is None:
            return None
        record.status = _validate_status(status)
        record.updated_at = time.time()
        if error:
            record.error = error
        records[job_id] = record
        self._write(records)
        return record

    def update_metadata(self, job_id: str, metadata: dict[str, Any]) -> JobRecord | None:
        records = self._read()
        record = records.get(job_id)
        if record is None:
            return None
        record.metadata.update(dict(metadata))
        record.updated_at = time.time()
        records[job_id] = record
        self._write(records)
        return record

    def finish(
        self,
        job_id: str,
        *,
        status: str,
        session_id: str = "",
        final_text: str = "",
        error: str = "",
    ) -> JobRecord | None:
        records = self._read()
        record = records.get(job_id)
        if record is None:
            return None
        record.status = _validate_status(status)
        record.updated_at = time.time()
        record.session_id = session_id
        record.final_text = final_text
        record.error = error
        records[job_id] = record
        self._write(records)
        return record

    def cancel(self, job_id: str, *, reason: str) -> JobRecord | None:
        records = self._read()
        record = records.get(job_id)
        if record is None:
            return None
        if record.status == "queued":
            record.status = "cancelled"
            record.error = reason
        elif record.status == "running":
            record.status = "cancel_requested"
            record.error = reason
        elif record.status == "cancel_requested":
            record.error = record.error or reason
        else:
            return record
        record.updated_at = time.time()
        records[job_id] = record
        self._write(records)
        return record

    def mark_unfinished_interrupted(self, *, interface: str, reason: str) -> int:
        records = self._read()
        changed = 0
        now = time.time()
        for record in records.values():
            if record.interface != interface or record.status not in {"queued", "running", "cancel_requested"}:
                continue
            record.status = "interrupted"
            record.updated_at = now
            record.error = reason
            changed += 1
        if changed:
            self._write(records)
        return changed

    def _read(self) -> dict[str, JobRecord]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        raw_records = data.get("jobs", data)
        if not isinstance(raw_records, dict):
            return {}
        records: dict[str, JobRecord] = {}
        for key, value in raw_records.items():
            if not isinstance(value, dict):
                continue
            try:
                record = JobRecord.from_dict(value)
            except (KeyError, TypeError, ValueError):
                continue
            records[str(key)] = record
        return records

    def _write(self, records: dict[str, JobRecord]) -> None:
        self.workspace.jobs_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "jobs": {key: record.to_dict() for key, record in sorted(records.items())},
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


def _new_job_id() -> str:
    return f"job-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _validate_status(value: str) -> str:
    status = value.strip().lower().replace("-", "_")
    if status not in JOB_STATUSES:
        raise ValueError(f"unsupported job status: {value}")
    return status
