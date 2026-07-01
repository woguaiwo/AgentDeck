"""Experience collections and event graph storage."""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agentdeck.core.config import Workspace


COLLECTION_KINDS = {
    "research_exploration",
    "engineering_change",
    "ops_maintenance",
    "qa_support",
    "decision_planning",
    "scratch",
}

COLLECTION_STATUSES = {"active", "paused", "archived"}

EVENT_LEVELS = {"macro", "meso", "micro"}

EVENT_STATUSES = {"open", "done", "blocked", "superseded"}

EDGE_RELATIONS = {
    "led_to",
    "blocked_by",
    "resolved_by",
    "supports",
    "contradicts",
    "replaces",
    "depends_on",
    "produced",
    "verified_by",
    "contains",
    "related_to",
}


@dataclass
class ExperienceCollection:
    collection_id: str
    title: str
    kind: str
    purpose: str = ""
    status: str = "active"
    project_id: str = ""
    directory_id: str = ""
    worker_id: str = ""
    agent_id: str = ""
    focus_id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExperienceCollection":
        return cls(
            collection_id=str(data["collection_id"]),
            title=str(data.get("title") or data["collection_id"]),
            kind=_validate_collection_kind(str(data.get("kind") or "scratch")),
            purpose=str(data.get("purpose") or ""),
            status=_validate_collection_status(str(data.get("status") or "active")),
            project_id=str(data.get("project_id") or ""),
            directory_id=str(data.get("directory_id") or ""),
            worker_id=str(data.get("worker_id") or data.get("session_id") or ""),
            agent_id=str(data.get("agent_id") or ""),
            focus_id=str(data.get("focus_id") or ""),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExperienceEvent:
    event_id: str
    collection_id: str
    purpose: str
    context: str = ""
    actions: list[str] = field(default_factory=list)
    result: str = ""
    analysis: str = ""
    decisions: list[str] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    parent_event_id: str = ""
    sequence_index: int = 0
    level: str = "micro"
    kind: str = "event"
    status: str = "done"
    project_id: str = ""
    directory_id: str = ""
    worker_id: str = ""
    agent_id: str = ""
    focus_id: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    confidence: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExperienceEvent":
        return cls(
            event_id=str(data["event_id"]),
            collection_id=str(data["collection_id"]),
            purpose=str(data.get("purpose") or ""),
            context=str(data.get("context") or ""),
            actions=_string_list(data.get("actions")),
            result=str(data.get("result") or ""),
            analysis=str(data.get("analysis") or ""),
            decisions=_string_list(data.get("decisions")),
            artifacts=[dict(item) for item in data.get("artifacts") or [] if isinstance(item, dict)],
            tags=_normalize_tags(data.get("tags")),
            parent_event_id=str(data.get("parent_event_id") or ""),
            sequence_index=int(data.get("sequence_index") or 0),
            level=_validate_event_level(str(data.get("level") or "micro")),
            kind=_clean_token(str(data.get("kind") or "event")) or "event",
            status=_validate_event_status(str(data.get("status") or "done")),
            project_id=str(data.get("project_id") or ""),
            directory_id=str(data.get("directory_id") or ""),
            worker_id=str(data.get("worker_id") or data.get("session_id") or ""),
            agent_id=str(data.get("agent_id") or ""),
            focus_id=str(data.get("focus_id") or ""),
            started_at=float(data.get("started_at") or 0.0),
            ended_at=float(data.get("ended_at") or 0.0),
            confidence=str(data.get("confidence") or ""),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExperienceEdge:
    edge_id: str
    from_event_id: str
    to_event_id: str
    relation: str
    reason: str = ""
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExperienceEdge":
        return cls(
            edge_id=str(data["edge_id"]),
            from_event_id=str(data["from_event_id"]),
            to_event_id=str(data["to_event_id"]),
            relation=_validate_edge_relation(str(data.get("relation") or "related_to")),
            reason=str(data.get("reason") or ""),
            created_at=float(data.get("created_at") or time.time()),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ExperienceStore:
    """JSON-backed event graph store.

    The schema is intentionally close to a future SQLite layout: collections
    define retrieval scope, events hold transferable experience, and edges hold
    logical relationships between events.
    """

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    @property
    def collections_path(self) -> Path:
        return self.workspace.experience_dir / "collections.json"

    @property
    def events_path(self) -> Path:
        return self.workspace.experience_dir / "events.json"

    @property
    def edges_path(self) -> Path:
        return self.workspace.experience_dir / "edges.json"

    def create_collection(
        self,
        title: str,
        *,
        kind: str,
        purpose: str = "",
        project_id: str = "",
        directory_id: str = "",
        worker_id: str = "",
        agent_id: str = "",
        focus_id: str = "",
        status: str = "active",
        metadata: dict[str, Any] | None = None,
    ) -> ExperienceCollection:
        title = _clean_title(title)
        if not title:
            raise ValueError("experience collection title is empty")
        kind = _validate_collection_kind(kind)
        status = _validate_collection_status(status)
        collections = self._read_collections()
        collection_id = _new_id("xpcol")
        while collection_id in collections:
            collection_id = _new_id("xpcol")
        now = time.time()
        record = ExperienceCollection(
            collection_id=collection_id,
            title=title,
            kind=kind,
            purpose=_clean_multiline(purpose),
            status=status,
            project_id=_clean_token(project_id),
            directory_id=directory_id.strip(),
            worker_id=worker_id.strip(),
            agent_id=_clean_token(agent_id),
            focus_id=focus_id.strip(),
            created_at=now,
            updated_at=now,
            metadata=dict(metadata or {}),
        )
        collections[record.collection_id] = record
        self._write_collections(collections)
        return record

    def get_collection(self, collection_id: str) -> ExperienceCollection | None:
        return self._read_collections().get(collection_id)

    def resolve_collection(self, value: str) -> ExperienceCollection | None:
        value = value.strip()
        collections = self._read_collections()
        if value in collections:
            return collections[value]
        matches = [record for record in collections.values() if record.title == value]
        if not matches:
            normalized = _clean_token(value)
            matches = [record for record in collections.values() if _clean_token(record.title) == normalized]
        if not matches:
            return None
        return sorted(matches, key=lambda item: item.updated_at, reverse=True)[0]

    def list_collections(
        self,
        *,
        project_id: str = "",
        worker_id: str = "",
        agent_id: str = "",
        focus_id: str = "",
        kind: str = "",
        status: str = "",
    ) -> list[ExperienceCollection]:
        records = list(self._read_collections().values())
        if project_id:
            records = [record for record in records if record.project_id == _clean_token(project_id)]
        if worker_id:
            records = [record for record in records if record.worker_id == worker_id]
        if agent_id:
            records = [record for record in records if record.agent_id == _clean_token(agent_id)]
        if focus_id:
            records = [record for record in records if record.focus_id == focus_id]
        if kind:
            records = [record for record in records if record.kind == _validate_collection_kind(kind)]
        if status:
            records = [record for record in records if record.status == _validate_collection_status(status)]
        return sorted(records, key=lambda item: (item.status == "archived", -item.updated_at))

    def record_event(
        self,
        collection: str,
        *,
        purpose: str,
        context: str = "",
        actions: list[str] | None = None,
        result: str = "",
        analysis: str = "",
        decisions: list[str] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        tags: list[str] | None = None,
        parent_event_id: str = "",
        sequence_index: int = 0,
        level: str = "micro",
        kind: str = "event",
        status: str = "done",
        focus_id: str = "",
        started_at: float = 0.0,
        ended_at: float = 0.0,
        confidence: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ExperienceEvent:
        collection_record = self.resolve_collection(collection)
        if collection_record is None:
            raise ValueError(f"experience collection not found: {collection}")
        purpose = _clean_multiline(purpose)
        if not purpose:
            raise ValueError("experience event purpose is empty")
        if parent_event_id and self.get_event(parent_event_id) is None:
            raise ValueError(f"parent event not found: {parent_event_id}")
        events = self._read_events()
        event_id = _new_id("xpev")
        while event_id in events:
            event_id = _new_id("xpev")
        now = time.time()
        event = ExperienceEvent(
            event_id=event_id,
            collection_id=collection_record.collection_id,
            purpose=purpose,
            context=_clean_multiline(context),
            actions=_string_list(actions),
            result=_clean_multiline(result),
            analysis=_clean_multiline(analysis),
            decisions=_string_list(decisions),
            artifacts=list(artifacts or []),
            tags=_normalize_tags(tags),
            parent_event_id=parent_event_id.strip(),
            sequence_index=max(sequence_index, 0),
            level=_validate_event_level(level),
            kind=_clean_token(kind) or "event",
            status=_validate_event_status(status),
            project_id=collection_record.project_id,
            directory_id=collection_record.directory_id,
            worker_id=collection_record.worker_id,
            agent_id=collection_record.agent_id,
            focus_id=focus_id.strip() or collection_record.focus_id,
            started_at=started_at,
            ended_at=ended_at,
            confidence=confidence.strip(),
            created_at=now,
            updated_at=now,
            metadata=dict(metadata or {}),
        )
        events[event.event_id] = event
        collection_record.updated_at = now
        collections = self._read_collections()
        collections[collection_record.collection_id] = collection_record
        self._write_events(events)
        self._write_collections(collections)
        return event

    def get_event(self, event_id: str) -> ExperienceEvent | None:
        return self._read_events().get(event_id)

    def resolve_event(self, value: str) -> ExperienceEvent | None:
        value = value.strip()
        events = self._read_events()
        if value in events:
            return events[value]
        normalized = _clean_token(value)
        matches = [event for event in events.values() if _clean_token(event.purpose) == normalized]
        if not matches:
            return None
        return sorted(matches, key=lambda item: item.updated_at, reverse=True)[0]

    def list_events(
        self,
        *,
        collection: str = "",
        project_id: str = "",
        worker_id: str = "",
        agent_id: str = "",
        focus_id: str = "",
        kind: str = "",
        query: str = "",
        limit: int = 20,
    ) -> list[ExperienceEvent]:
        events = list(self._read_events().values())
        if collection:
            collection_record = self.resolve_collection(collection)
            if collection_record is None:
                return []
            events = [event for event in events if event.collection_id == collection_record.collection_id]
        if project_id:
            events = [event for event in events if event.project_id == _clean_token(project_id)]
        if worker_id:
            events = [event for event in events if event.worker_id == worker_id]
        if agent_id:
            events = [event for event in events if event.agent_id == _clean_token(agent_id)]
        if focus_id:
            events = [event for event in events if event.focus_id == focus_id]
        if kind:
            events = [event for event in events if event.kind == _clean_token(kind)]
        if query:
            needle = query.casefold()
            events = [event for event in events if needle in _event_search_text(event).casefold()]
        events = sorted(events, key=lambda item: (item.started_at or item.created_at, item.sequence_index), reverse=True)
        if limit > 0:
            events = events[:limit]
        return events

    def link_events(
        self,
        from_event: str,
        to_event: str,
        *,
        relation: str,
        reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ExperienceEdge:
        source = self.resolve_event(from_event)
        target = self.resolve_event(to_event)
        if source is None:
            raise ValueError(f"from event not found: {from_event}")
        if target is None:
            raise ValueError(f"to event not found: {to_event}")
        relation = _validate_edge_relation(relation)
        edges = self._read_edges()
        edge_id = _new_id("xpedge")
        while edge_id in edges:
            edge_id = _new_id("xpedge")
        edge = ExperienceEdge(
            edge_id=edge_id,
            from_event_id=source.event_id,
            to_event_id=target.event_id,
            relation=relation,
            reason=_clean_multiline(reason),
            metadata=dict(metadata or {}),
        )
        edges[edge.edge_id] = edge
        self._write_edges(edges)
        return edge

    def list_edges(self, *, event_id: str = "", relation: str = "") -> list[ExperienceEdge]:
        edges = list(self._read_edges().values())
        if event_id:
            event = self.resolve_event(event_id)
            if event is None:
                return []
            edges = [edge for edge in edges if edge.from_event_id == event.event_id or edge.to_event_id == event.event_id]
        if relation:
            edges = [edge for edge in edges if edge.relation == _validate_edge_relation(relation)]
        return sorted(edges, key=lambda item: item.created_at, reverse=True)

    def collection_summary(
        self,
        collection: str,
        *,
        event_limit: int = 8,
    ) -> dict[str, Any] | None:
        record = self.resolve_collection(collection)
        if record is None:
            return None
        events = self.list_events(collection=record.collection_id, limit=event_limit)
        return {
            "collection_id": record.collection_id,
            "title": record.title,
            "kind": record.kind,
            "purpose": record.purpose,
            "project_id": record.project_id,
            "worker_id": record.worker_id,
            "agent_id": record.agent_id,
            "focus_id": record.focus_id,
            "events": [
                {
                    "event_id": event.event_id,
                    "level": event.level,
                    "purpose": event.purpose,
                    "result": event.result,
                    "decisions": event.decisions[:5],
                    "artifacts": event.artifacts[:5],
                    "tags": event.tags[:8],
                }
                for event in events
            ],
        }

    def _read_collections(self) -> dict[str, ExperienceCollection]:
        data = _read_registry(self.collections_path)
        raw = data.get("collections", data)
        if not isinstance(raw, dict):
            return {}
        records = {}
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue
            try:
                records[str(key)] = ExperienceCollection.from_dict(value)
            except (KeyError, TypeError, ValueError):
                continue
        return records

    def _write_collections(self, records: dict[str, ExperienceCollection]) -> None:
        payload = {
            "version": 1,
            "collections": {key: record.to_dict() for key, record in sorted(records.items())},
        }
        _write_json_atomic(self.collections_path, payload)

    def _read_events(self) -> dict[str, ExperienceEvent]:
        data = _read_registry(self.events_path)
        raw = data.get("events", data)
        if not isinstance(raw, dict):
            return {}
        records = {}
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue
            try:
                records[str(key)] = ExperienceEvent.from_dict(value)
            except (KeyError, TypeError, ValueError):
                continue
        return records

    def _write_events(self, records: dict[str, ExperienceEvent]) -> None:
        payload = {
            "version": 1,
            "events": {key: record.to_dict() for key, record in sorted(records.items())},
        }
        _write_json_atomic(self.events_path, payload)

    def _read_edges(self) -> dict[str, ExperienceEdge]:
        data = _read_registry(self.edges_path)
        raw = data.get("edges", data)
        if not isinstance(raw, dict):
            return {}
        records = {}
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue
            try:
                records[str(key)] = ExperienceEdge.from_dict(value)
            except (KeyError, TypeError, ValueError):
                continue
        return records

    def _write_edges(self, records: dict[str, ExperienceEdge]) -> None:
        payload = {
            "version": 1,
            "edges": {key: record.to_dict() for key, record in sorted(records.items())},
        }
        _write_json_atomic(self.edges_path, payload)


def _read_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _validate_collection_kind(value: str) -> str:
    kind = _clean_token(value)
    if kind not in COLLECTION_KINDS:
        raise ValueError(f"unsupported experience collection kind: {value}")
    return kind


def _validate_collection_status(value: str) -> str:
    status = _clean_token(value)
    if status not in COLLECTION_STATUSES:
        raise ValueError(f"unsupported experience collection status: {value}")
    return status


def _validate_event_level(value: str) -> str:
    level = _clean_token(value)
    if level not in EVENT_LEVELS:
        raise ValueError(f"unsupported experience event level: {value}")
    return level


def _validate_event_status(value: str) -> str:
    status = _clean_token(value)
    if status not in EVENT_STATUSES:
        raise ValueError(f"unsupported experience event status: {value}")
    return status


def _validate_edge_relation(value: str) -> str:
    relation = _clean_token(value)
    if relation not in EDGE_RELATIONS:
        raise ValueError(f"unsupported experience edge relation: {value}")
    return relation


def _clean_token(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", value.strip().lower()).strip("_.-")


def _clean_title(value: str) -> str:
    return " ".join(value.strip().split())


def _clean_multiline(value: str) -> str:
    return "\n".join(line.rstrip() for line in str(value or "").strip().splitlines()).strip()


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [_clean_multiline(value)] if _clean_multiline(value) else []
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        text = _clean_multiline(str(item))
        if text:
            result.append(text)
    return result


def _normalize_tags(value: Any) -> list[str]:
    return list(dict.fromkeys(_clean_token(item) for item in _string_list(value) if _clean_token(item)))


def _event_search_text(event: ExperienceEvent) -> str:
    return "\n".join(
        [
            event.purpose,
            event.context,
            "\n".join(event.actions),
            event.result,
            event.analysis,
            "\n".join(event.decisions),
            " ".join(event.tags),
        ]
    )
