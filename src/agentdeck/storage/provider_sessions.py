"""Discover existing provider sessions that can be imported into AgentDeck."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any


@dataclass
class ProviderSessionCandidate:
    provider: str
    adapter: str
    provider_session_id: str
    provider_session_kind: str
    project_dir: str
    title: str = ""
    updated_at: float = 0.0
    source_path: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProviderEvent:
    provider: str
    source: str
    kind: str
    role: str = ""
    text: str = ""
    timestamp: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProviderEventBundle:
    provider: str
    provider_session_id: str
    project_dir: str
    source_path: str = ""
    title: str = ""
    updated_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    events: list[ProviderEvent] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["events"] = [event.to_dict() for event in self.events]
        return data


def scan_provider_sessions(
    *,
    provider: str | None = None,
    project_dir: str | Path | None = None,
    home: str | Path | None = None,
) -> list[ProviderSessionCandidate]:
    """Find resumable provider sessions from local Codex/Kimi state."""
    base_home = Path(home).expanduser() if home is not None else Path.home()
    providers = [provider] if provider else ["codex", "kimi"]
    normalized_dir = _normalize_path(project_dir) if project_dir else ""

    candidates: list[ProviderSessionCandidate] = []
    if "codex" in providers:
        candidates.extend(_scan_codex(base_home / ".codex", project_dir=normalized_dir))
    if "kimi" in providers:
        candidates.extend(_scan_kimi(base_home / ".kimi", project_dir=normalized_dir))
    return sorted(candidates, key=lambda item: item.updated_at, reverse=True)


def scan_codex_index_sessions(
    *,
    project_dir: str | Path | None = None,
    home: str | Path | None = None,
) -> list[ProviderSessionCandidate]:
    """List sessions that Codex exposes through its resume index.

    Codex's index is the user-facing picker source, but it does not always carry
    a cwd. When a matching rollout exists, merge its session_meta cwd so the
    candidate can be imported as a directory-bound AgentDeck session-agent.
    """
    base_home = Path(home).expanduser() if home is not None else Path.home()
    codex_home = base_home / ".codex"
    index_path = codex_home / "session_index.jsonl"
    index = _read_codex_index(index_path)
    normalized_dir = _normalize_path(project_dir) if project_dir else ""
    rollout_by_id = {candidate.provider_session_id: candidate for candidate in _scan_codex(codex_home, project_dir="")}

    candidates: list[ProviderSessionCandidate] = []
    for session_id, item in index.items():
        rollout = rollout_by_id.get(session_id)
        project = rollout.project_dir if rollout is not None else ""
        if normalized_dir and project != normalized_dir:
            continue
        updated_at = _parse_time(str(item.get("updated_at") or ""))
        if not updated_at and rollout is not None:
            updated_at = rollout.updated_at
        title = str(item.get("thread_name") or "")
        if not title and rollout is not None:
            title = rollout.title
        metadata = dict(rollout.metadata) if rollout is not None else {}
        metadata.update(
            {
                "codex_indexed": True,
                "codex_index_path": str(index_path),
                "has_rollout": rollout is not None,
            }
        )
        candidates.append(
            ProviderSessionCandidate(
                provider="codex",
                adapter="codex",
                provider_session_id=session_id,
                provider_session_kind="codex_thread",
                project_dir=project,
                title=title or session_id,
                updated_at=updated_at,
                source_path=rollout.source_path if rollout is not None else str(index_path),
                metadata=metadata,
            )
        )
    return sorted(candidates, key=lambda item: item.updated_at, reverse=True)


def read_provider_event_bundle(
    *,
    provider: str,
    provider_session_id: str,
    project_dir: str | Path = "",
    home: str | Path | None = None,
) -> ProviderEventBundle | None:
    """Read a provider session into a provider-neutral event bundle."""
    base_home = Path(home).expanduser() if home is not None else Path.home()
    clean_provider = provider.strip().lower()
    if clean_provider in {"codex", "codex-exec"}:
        return _read_codex_event_bundle(
            base_home / ".codex",
            provider_session_id=provider_session_id,
            project_dir=_normalize_path(project_dir) if project_dir else "",
        )
    if clean_provider in {"kimi", "kimi-print"}:
        return _read_kimi_event_bundle(
            base_home / ".kimi",
            provider_session_id=provider_session_id,
            project_dir=_normalize_path(project_dir) if project_dir else "",
        )
    return None


def _scan_codex(codex_home: Path, *, project_dir: str = "") -> list[ProviderSessionCandidate]:
    index = _read_codex_index(codex_home / "session_index.jsonl")
    sessions_dir = codex_home / "sessions"
    if not sessions_dir.exists():
        return []

    by_id: dict[str, ProviderSessionCandidate] = {}
    for path in sessions_dir.rglob("*.jsonl"):
        meta = _read_first_json(path)
        if not meta or meta.get("type") != "session_meta":
            continue
        payload = meta.get("payload")
        if not isinstance(payload, dict):
            continue
        session_id = str(payload.get("id") or "")
        cwd = _normalize_path(str(payload.get("cwd") or ""))
        if not session_id or not cwd:
            continue
        if project_dir and cwd != project_dir:
            continue

        indexed = index.get(session_id, {})
        updated_at = _parse_time(str(indexed.get("updated_at") or "")) or _parse_time(
            str(payload.get("timestamp") or meta.get("timestamp") or "")
        )
        if not updated_at:
            updated_at = path.stat().st_mtime
        candidate = ProviderSessionCandidate(
            provider="codex",
            adapter="codex",
            provider_session_id=session_id,
            provider_session_kind="codex_thread",
            project_dir=cwd,
            title=str(indexed.get("thread_name") or Path(cwd).name or session_id),
            updated_at=updated_at,
            source_path=str(path),
            metadata={
                "originator": str(payload.get("originator") or ""),
                "cli_version": str(payload.get("cli_version") or ""),
            },
        )
        existing = by_id.get(session_id)
        if existing is None or candidate.updated_at >= existing.updated_at:
            by_id[session_id] = candidate
    return list(by_id.values())


def _read_codex_index(path: Path) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return index
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return index
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("id"):
            index[str(item["id"])] = item
    return index


def _read_codex_event_bundle(
    codex_home: Path,
    *,
    provider_session_id: str,
    project_dir: str = "",
) -> ProviderEventBundle | None:
    candidates = [
        candidate
        for candidate in _scan_codex(codex_home, project_dir=project_dir)
        if candidate.provider_session_id == provider_session_id
    ]
    if not candidates and project_dir:
        candidates = [
            candidate
            for candidate in _scan_codex(codex_home, project_dir="")
            if candidate.provider_session_id == provider_session_id
        ]
    if not candidates:
        return None
    candidate = sorted(candidates, key=lambda item: item.updated_at, reverse=True)[0]
    path = Path(candidate.source_path)
    events: list[ProviderEvent] = []
    for item in _iter_jsonl(path):
        event = _codex_provider_event(item, source=str(path))
        if event is not None:
            events.append(event)
    return ProviderEventBundle(
        provider="codex",
        provider_session_id=provider_session_id,
        project_dir=candidate.project_dir,
        source_path=candidate.source_path,
        title=candidate.title,
        updated_at=candidate.updated_at,
        metadata=dict(candidate.metadata),
        events=events,
    )


def _codex_provider_event(item: dict[str, Any], *, source: str) -> ProviderEvent | None:
    event_type = str(item.get("type") or "")
    payload = item.get("payload")
    payload_dict = payload if isinstance(payload, dict) else {}
    timestamp = _parse_time(str(item.get("timestamp") or payload_dict.get("timestamp") or ""))
    if event_type == "session_meta":
        return ProviderEvent(
            provider="codex",
            source=source,
            kind="session_meta",
            role="system",
            text=str(payload_dict.get("cwd") or ""),
            timestamp=timestamp,
            metadata={
                "id": str(payload_dict.get("id") or ""),
                "cwd": str(payload_dict.get("cwd") or ""),
                "cli_version": str(payload_dict.get("cli_version") or ""),
            },
        )
    if event_type == "compacted":
        text = _text_from_any(payload_dict.get("message") or payload_dict.get("replacement_history") or payload_dict)
        return ProviderEvent(
            provider="codex",
            source=source,
            kind="compacted",
            role="summary",
            text=text,
            timestamp=timestamp,
            metadata={"window_id": str(payload_dict.get("window_id") or "")},
        )
    if event_type == "turn_context":
        text = _text_from_any(payload_dict.get("summary") or payload_dict)
        return ProviderEvent(
            provider="codex",
            source=source,
            kind="turn_context",
            role="summary",
            text=text,
            timestamp=timestamp,
            metadata={},
        )
    if event_type in {"event_msg", "response_item"}:
        role = _role_from_payload(payload_dict)
        text = _text_from_any(payload_dict.get("text") or payload_dict.get("message") or payload_dict.get("item") or payload_dict)
        kind = str(payload_dict.get("type") or event_type)
        if not text:
            return None
        return ProviderEvent(
            provider="codex",
            source=source,
            kind=kind,
            role=role,
            text=text,
            timestamp=timestamp,
            metadata={},
        )
    return None


def _scan_kimi(kimi_home: Path, *, project_dir: str = "") -> list[ProviderSessionCandidate]:
    work_dirs = _read_kimi_work_dirs(kimi_home / "kimi.json")
    if project_dir and project_dir not in work_dirs:
        work_dirs[project_dir] = {"path": project_dir}

    candidates: list[ProviderSessionCandidate] = []
    seen: set[tuple[str, str]] = set()
    for work_dir, entry in work_dirs.items():
        if project_dir and _normalize_path(work_dir) != project_dir:
            continue
        session_root = kimi_home / "sessions" / _kimi_work_dir_hash(work_dir)
        if not session_root.exists():
            continue
        for session_dir in session_root.iterdir():
            if not session_dir.is_dir():
                continue
            session_id = session_dir.name
            key = (work_dir, session_id)
            if key in seen:
                continue
            seen.add(key)
            state = _read_json(session_dir / "state.json")
            title = ""
            archived = False
            if isinstance(state, dict):
                title = str(state.get("custom_title") or "")
                archived = bool(state.get("archived"))
            updated_at = _latest_mtime(session_dir)
            candidates.append(
                ProviderSessionCandidate(
                    provider="kimi",
                    adapter="kimi",
                    provider_session_id=session_id,
                    provider_session_kind="kimi_session",
                    project_dir=_normalize_path(work_dir),
                    title=title or Path(work_dir).name or session_id,
                    updated_at=updated_at,
                    source_path=str(session_dir),
                    metadata={
                        "work_dir_hash": session_root.name,
                        "archived": archived,
                        "is_last_session": session_id == str(entry.get("last_session_id") or ""),
                    },
                )
            )
    return candidates


def _read_kimi_event_bundle(
    kimi_home: Path,
    *,
    provider_session_id: str,
    project_dir: str = "",
) -> ProviderEventBundle | None:
    candidates = [
        candidate
        for candidate in _scan_kimi(kimi_home, project_dir=project_dir)
        if candidate.provider_session_id == provider_session_id
    ]
    if not candidates and project_dir:
        candidates = [
            candidate
            for candidate in _scan_kimi(kimi_home, project_dir="")
            if candidate.provider_session_id == provider_session_id
        ]
    if not candidates:
        return None
    candidate = sorted(candidates, key=lambda item: item.updated_at, reverse=True)[0]
    session_dir = Path(candidate.source_path)
    events: list[ProviderEvent] = []
    state = _read_json(session_dir / "state.json")
    if isinstance(state, dict):
        events.append(
            ProviderEvent(
                provider="kimi",
                source=str(session_dir / "state.json"),
                kind="state",
                role="system",
                text=str(state.get("custom_title") or ""),
                timestamp=(session_dir / "state.json").stat().st_mtime if (session_dir / "state.json").exists() else 0.0,
                metadata={key: state.get(key) for key in ["archived", "plan_mode", "version"] if key in state},
            )
        )
    context_files = sorted(session_dir.glob("context*.jsonl"), key=_kimi_context_sort_key)
    for path in context_files:
        for item in _iter_jsonl(path):
            event = _kimi_provider_event(item, source=str(path))
            if event is not None:
                events.append(event)
    return ProviderEventBundle(
        provider="kimi",
        provider_session_id=provider_session_id,
        project_dir=candidate.project_dir,
        source_path=candidate.source_path,
        title=candidate.title,
        updated_at=candidate.updated_at,
        metadata=dict(candidate.metadata),
        events=events,
    )


def _kimi_provider_event(item: dict[str, Any], *, source: str) -> ProviderEvent | None:
    role = str(item.get("role") or "")
    if not role:
        return None
    content = item.get("content")
    text = _text_from_any(content)
    if not text and role not in {"_checkpoint", "_system_prompt"}:
        return None
    kind = role.strip("_") or "message"
    return ProviderEvent(
        provider="kimi",
        source=source,
        kind=kind,
        role=role,
        text=text,
        timestamp=0.0,
        metadata={key: item.get(key) for key in ["name", "tool_call_id"] if key in item},
    )


def _read_kimi_work_dirs(path: Path) -> dict[str, dict[str, Any]]:
    data = _read_json(path)
    if not isinstance(data, dict):
        return {}
    raw = data.get("work_dirs")
    if not isinstance(raw, list):
        return {}
    work_dirs: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        path_value = _normalize_path(str(item.get("path") or ""))
        if path_value:
            work_dirs[path_value] = item
    return work_dirs


def _read_first_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            line = handle.readline()
    except OSError:
        return None
    if not line:
        return None
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _iter_jsonl(path: Path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    yield item
    except OSError:
        return


def _normalize_path(value: str | Path | None) -> str:
    if not value:
        return ""
    path = Path(value).expanduser()
    try:
        return str(path.resolve(strict=False))
    except OSError:
        return str(path.absolute())


def _parse_time(value: str) -> float:
    if not value:
        return 0.0
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def _latest_mtime(path: Path) -> float:
    latest = path.stat().st_mtime
    for child in path.iterdir():
        try:
            latest = max(latest, child.stat().st_mtime)
        except OSError:
            continue
    return latest


def _kimi_work_dir_hash(path: str) -> str:
    return hashlib.md5(path.encode("utf-8")).hexdigest()


def _kimi_context_sort_key(path: Path) -> tuple[int, str]:
    if path.name == "context.jsonl":
        return (0, path.name)
    stem = path.stem
    try:
        return (int(stem.rsplit("_", 1)[1]), path.name)
    except (IndexError, ValueError):
        return (9999, path.name)


def _role_from_payload(payload: dict[str, Any]) -> str:
    role = str(payload.get("role") or payload.get("type") or "")
    if role in {"user", "assistant", "system", "tool"}:
        return role
    item = payload.get("item")
    if isinstance(item, dict):
        item_role = str(item.get("role") or item.get("type") or "")
        if item_role:
            return item_role
    return role or "event"


def _text_from_any(value: Any, *, max_chars: int = 8000) -> str:
    parts: list[str] = []
    _collect_text(value, parts)
    text = "\n".join(part for part in parts if part.strip())
    text = text.strip()
    if len(text) > max_chars:
        return text[: max_chars - 3].rstrip() + "..."
    return text


def _collect_text(value: Any, parts: list[str]) -> None:
    if value is None:
        return
    if isinstance(value, str):
        if value.strip():
            parts.append(value.strip())
        return
    if isinstance(value, list):
        for item in value:
            _collect_text(item, parts)
        return
    if isinstance(value, dict):
        for key in ("text", "content", "message", "summary", "output"):
            if key in value:
                _collect_text(value.get(key), parts)
                return
        if value.get("type") in {"text", "input_text", "output_text"} and "value" in value:
            _collect_text(value.get("value"), parts)
            return
