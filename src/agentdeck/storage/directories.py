"""Directory registry for AgentDeck projects."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agentdeck.core.config import Workspace


@dataclass
class DirectoryRecord:
    """One filesystem directory managed by AgentDeck."""

    directory_id: str
    path: str
    project_id: str = ""
    title: str = ""
    parent_directory_id: str = ""
    role: str = "workspace"
    status: str = "active"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DirectoryRecord":
        return cls(
            directory_id=str(data["directory_id"]),
            path=str(data.get("path") or ""),
            project_id=str(data.get("project_id") or ""),
            title=str(data.get("title") or ""),
            parent_directory_id=str(data.get("parent_directory_id") or ""),
            role=str(data.get("role") or "workspace"),
            status=str(data.get("status") or "active"),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DirectoryRegistry:
    """JSON-backed index of managed directories.

    Projects may contain many directories. Agents and sessions are still bound
    to concrete filesystem paths; this registry gives those paths stable ids and
    optional hierarchy metadata without changing provider cwd behavior.
    """

    _LOCK = threading.RLock()

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    @property
    def path(self) -> Path:
        return self.workspace.directories_dir / "registry.json"

    def upsert(
        self,
        *,
        path: str | Path,
        project_id: str = "",
        title: str = "",
        parent: str | Path = "",
        role: str = "workspace",
        status: str = "active",
        metadata: dict[str, Any] | None = None,
    ) -> DirectoryRecord:
        with self._LOCK:
            resolved_path = str(Path(path).expanduser().resolve())
            directory_id = directory_id_for_path(resolved_path)
            parent_directory_id = ""
            parent_text = str(parent or "").strip()
            if parent_text:
                parent_directory_id = (
                    parent_text
                    if parent_text.startswith("dir-")
                    else directory_id_for_path(Path(parent_text).expanduser().resolve())
                )

            records = self._read()
            existing = records.get(directory_id)
            now = time.time()
            record = DirectoryRecord(
                directory_id=directory_id,
                path=resolved_path,
                project_id=_normalize_token(project_id) if project_id else (existing.project_id if existing else ""),
                title=_clean_title(title) or (existing.title if existing else Path(resolved_path).name or resolved_path),
                parent_directory_id=parent_directory_id or (existing.parent_directory_id if existing else ""),
                role=_normalize_token(role or "workspace"),
                status=status or (existing.status if existing else "active"),
                created_at=existing.created_at if existing else now,
                updated_at=now,
                metadata=dict(existing.metadata) if existing else {},
            )
            if metadata:
                record.metadata.update(metadata)
            records[directory_id] = record
            self._write(records)
            return record

    def get(self, directory_id: str) -> DirectoryRecord | None:
        return self._read().get(directory_id.strip())

    def resolve(self, value: str | Path) -> DirectoryRecord | None:
        records = self._read()
        raw = str(value).strip()
        if raw in records:
            return records[raw]
        if raw:
            path_id = directory_id_for_path(Path(raw).expanduser().resolve())
            if path_id in records:
                return records[path_id]
        matches = [record for record in records.values() if record.title == raw]
        if not matches:
            return None
        return sorted(matches, key=lambda item: item.updated_at, reverse=True)[0]

    def list(
        self,
        *,
        project_id: str | None = None,
        parent_directory_id: str | None = None,
        status: str | None = None,
    ) -> list[DirectoryRecord]:
        records = list(self._read().values())
        if project_id:
            records = [record for record in records if record.project_id == _normalize_token(project_id)]
        if parent_directory_id is not None:
            records = [record for record in records if record.parent_directory_id == parent_directory_id]
        if status:
            records = [record for record in records if record.status == status]
        return sorted(records, key=lambda item: (item.project_id, item.path))

    def _read(self) -> dict[str, DirectoryRecord]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        raw_records = data.get("directories", data)
        if not isinstance(raw_records, dict):
            return {}

        records: dict[str, DirectoryRecord] = {}
        for key, value in raw_records.items():
            if not isinstance(value, dict):
                continue
            try:
                record = DirectoryRecord.from_dict(value)
            except (KeyError, TypeError, ValueError):
                continue
            records[str(key)] = record
        return records

    def _write(self, records: dict[str, DirectoryRecord]) -> None:
        self.workspace.directories_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "directories": {key: record.to_dict() for key, record in sorted(records.items())},
        }
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


def directory_id_for_path(path: str | Path) -> str:
    resolved = str(Path(path).expanduser().resolve())
    digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", Path(resolved).name.lower()).strip(".-") or "root"
    return f"dir-{slug}-{digest}"


def _normalize_token(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip().lower()).strip(".-") or "default"


def _clean_title(value: str) -> str:
    return " ".join(str(value).strip().split())
