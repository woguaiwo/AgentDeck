"""Structured progress journal for handoffs and long-running work."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from agentdeck.core.config import Workspace


@dataclass
class ProgressEntry:
    """One durable progress record shared by agents and interfaces."""

    entry_id: str
    kind: str
    summary: str
    project_id: str = ""
    task_id: str = ""
    focus_id: str = ""
    session_id: str = ""
    agent_id: str = ""
    completed: list[str] = field(default_factory=list)
    verified: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProgressEntry":
        return cls(
            entry_id=str(data["entry_id"]),
            kind=str(data.get("kind") or "progress"),
            summary=str(data.get("summary") or ""),
            project_id=str(data.get("project_id") or ""),
            task_id=str(data.get("task_id") or ""),
            focus_id=str(data.get("focus_id") or ""),
            session_id=str(data.get("session_id") or ""),
            agent_id=str(data.get("agent_id") or ""),
            completed=_string_list(data.get("completed")),
            verified=_string_list(data.get("verified")),
            next_steps=_string_list(data.get("next_steps")),
            blockers=_string_list(data.get("blockers")),
            decisions=_string_list(data.get("decisions")),
            artifacts=_string_list(data.get("artifacts")),
            created_at=float(data.get("created_at") or time.time()),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProgressJournal:
    """Append-only project journal backed by JSONL."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    @property
    def path(self):
        return self.workspace.journal_dir / "progress.jsonl"

    def append(
        self,
        *,
        kind: str,
        summary: str,
        project_id: str = "",
        task_id: str = "",
        focus_id: str = "",
        session_id: str = "",
        agent_id: str = "",
        completed: list[str] | None = None,
        verified: list[str] | None = None,
        next_steps: list[str] | None = None,
        blockers: list[str] | None = None,
        decisions: list[str] | None = None,
        artifacts: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ProgressEntry:
        self.workspace.ensure()
        clean_summary = _clean_text(summary)
        if not clean_summary:
            raise ValueError("progress summary is empty")
        entry = ProgressEntry(
            entry_id=f"progress-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
            kind=_clean_token(kind) or "progress",
            summary=clean_summary,
            project_id=_clean_token(project_id),
            task_id=_clean_token(task_id),
            focus_id=_clean_token(focus_id),
            session_id=session_id.strip(),
            agent_id=_clean_token(agent_id),
            completed=_clean_list(completed),
            verified=_clean_list(verified),
            next_steps=_clean_list(next_steps),
            blockers=_clean_list(blockers),
            decisions=_clean_list(decisions),
            artifacts=_clean_list(artifacts),
            metadata=dict(metadata or {}),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
        return entry

    def list(
        self,
        *,
        kind: str | None = None,
        task_id: str | None = None,
        focus_id: str | None = None,
        session_id: str | None = None,
        limit: int = 20,
    ) -> list[ProgressEntry]:
        if not self.path.exists():
            return []
        entries: list[ProgressEntry] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                try:
                    entry = ProgressEntry.from_dict(data)
                except (KeyError, TypeError, ValueError):
                    continue
                if kind and entry.kind != kind:
                    continue
                if task_id and entry.task_id != task_id:
                    continue
                if focus_id and entry.focus_id != focus_id:
                    continue
                if session_id and entry.session_id != session_id:
                    continue
                entries.append(entry)
        return sorted(entries, key=lambda item: item.created_at, reverse=True)[: max(limit, 0)]


def format_handoff(entry: ProgressEntry) -> str:
    """Render one progress entry as a compact task note."""

    return _format_progress_note(entry, heading="Handoff")


def format_review(entry: ProgressEntry) -> str:
    """Render one manager review as a compact task note."""

    lines = [f"Manager review: {entry.summary}"]
    status = str(entry.metadata.get("status") or "").strip()
    reviewer = str(entry.metadata.get("reviewer") or "").strip()
    if status:
        lines.append(f"Status: {status}")
    if reviewer:
        lines.append(f"Reviewer: {reviewer}")
    _append_section(lines, "Next", entry.next_steps)
    _append_section(lines, "Blockers", entry.blockers)
    _append_section(lines, "Decisions", entry.decisions)
    _append_section(lines, "Artifacts", entry.artifacts)
    return "\n".join(lines)


def _format_progress_note(entry: ProgressEntry, *, heading: str) -> str:
    lines = [f"{heading}: {entry.summary}"]
    _append_section(lines, "Completed", entry.completed)
    _append_section(lines, "Verified", entry.verified)
    _append_section(lines, "Next", entry.next_steps)
    _append_section(lines, "Blockers", entry.blockers)
    _append_section(lines, "Decisions", entry.decisions)
    _append_section(lines, "Artifacts", entry.artifacts)
    return "\n".join(lines)


def _append_section(lines: list[str], title: str, values: list[str]) -> None:
    if not values:
        return
    lines.append(f"{title}:")
    for value in values:
        lines.append(f"- {value}")


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _clean_list(values: list[str] | None) -> list[str]:
    return [_clean_text(value) for value in values or [] if _clean_text(value)]


def _clean_text(value: str) -> str:
    return " ".join(str(value).strip().split())


def _clean_token(value: str) -> str:
    return str(value).strip()
