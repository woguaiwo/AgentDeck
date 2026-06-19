"""Project registry for AgentDeck workspaces."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agentdeck.core.config import Workspace


@dataclass
class ProjectRecord:
    """One source project managed by AgentDeck."""

    project_id: str
    title: str
    project_dir: str
    team_id: str = "default"
    default_agent_id: str = "owner"
    status: str = "active"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectRecord":
        return cls(
            project_id=str(data["project_id"]),
            title=str(data.get("title") or data["project_id"]),
            project_dir=str(data.get("project_dir") or ""),
            team_id=str(data.get("team_id") or "default"),
            default_agent_id=str(data.get("default_agent_id") or "owner"),
            status=str(data.get("status") or "active"),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProjectRegistry:
    """JSON-backed project registry."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    @property
    def path(self) -> Path:
        return self.workspace.projects_dir / "registry.json"

    def upsert(
        self,
        *,
        project_id: str,
        title: str | None = None,
        project_dir: str | Path = ".",
        team_id: str = "default",
        default_agent_id: str = "owner",
        status: str = "active",
        replace: bool = False,
    ) -> ProjectRecord:
        project_id = _normalize_id(project_id)
        records = self._read()
        existing = records.get(project_id)
        if existing is not None and not replace:
            raise ValueError(f"project already exists: {project_id}")

        now = time.time()
        record = ProjectRecord(
            project_id=project_id,
            title=_clean_title(title or "") or _title_from_id(project_id),
            project_dir=str(Path(project_dir).expanduser().resolve()),
            team_id=_normalize_token(team_id or project_id),
            default_agent_id=_normalize_token(default_agent_id or "owner"),
            status=status or "active",
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
            metadata=dict(existing.metadata) if existing is not None else {},
        )
        records[project_id] = record
        self._write(records)
        return record

    def get(self, project_id: str) -> ProjectRecord | None:
        return self._read().get(_maybe_normalize_id(project_id))

    def resolve(self, value: str) -> ProjectRecord | None:
        records = self._read()
        normalized = _maybe_normalize_id(value)
        if normalized in records:
            return records[normalized]
        matches = [record for record in records.values() if record.title == value]
        if not matches:
            return None
        return sorted(matches, key=lambda item: item.updated_at, reverse=True)[0]

    def list(self, *, team_id: str | None = None, status: str | None = None) -> list[ProjectRecord]:
        records = list(self._read().values())
        if team_id:
            records = [record for record in records if record.team_id == _normalize_token(team_id)]
        if status:
            records = [record for record in records if record.status == status]
        return sorted(records, key=lambda item: item.project_id)

    def _read(self) -> dict[str, ProjectRecord]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        raw_records = data.get("projects", data)
        if not isinstance(raw_records, dict):
            return {}

        records: dict[str, ProjectRecord] = {}
        for key, value in raw_records.items():
            if not isinstance(value, dict):
                continue
            try:
                record = ProjectRecord.from_dict(value)
            except (KeyError, TypeError, ValueError):
                continue
            records[str(key)] = record
        return records

    def _write(self, records: dict[str, ProjectRecord]) -> None:
        self.workspace.projects_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "projects": {key: record.to_dict() for key, record in sorted(records.items())},
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


def _normalize_id(value: str) -> str:
    normalized = _maybe_normalize_id(value)
    if not normalized:
        raise ValueError("project id is empty")
    return normalized


def _maybe_normalize_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip().lower()).strip(".-")


def _normalize_token(value: str) -> str:
    return _maybe_normalize_id(value) or "default"


def _title_from_id(value: str) -> str:
    return " ".join(part.capitalize() for part in re.split(r"[-_.]+", value) if part) or value


def _clean_title(value: str) -> str:
    return " ".join(value.strip().split())
