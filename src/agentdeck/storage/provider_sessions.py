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
