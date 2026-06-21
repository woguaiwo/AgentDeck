"""Project-level state and decision log for manager/executor workflows."""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agentdeck.core.config import Workspace


@dataclass
class ProjectStateCard:
    """Compact direction-setting state shared by all project agents."""

    project_id: str
    goal: str = ""
    phase: str = ""
    current_focus: str = ""
    next_steps: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    active_artifacts: list[str] = field(default_factory=list)
    updated_by: str = ""
    updated_at: float = field(default_factory=time.time)
    schema_version: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectStateCard":
        return cls(
            project_id=str(data["project_id"]),
            goal=str(data.get("goal") or ""),
            phase=str(data.get("phase") or ""),
            current_focus=str(data.get("current_focus") or ""),
            next_steps=_string_list(data.get("next_steps")),
            constraints=_string_list(data.get("constraints")),
            blockers=_string_list(data.get("blockers")),
            active_artifacts=_string_list(data.get("active_artifacts")),
            updated_by=str(data.get("updated_by") or ""),
            updated_at=float(data.get("updated_at") or time.time()),
            schema_version=int(data.get("schema_version") or 1),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DecisionRecord:
    """One explicit project decision."""

    decision_id: str
    project_id: str
    decision: str
    reason: str = ""
    impact: str = ""
    alternatives: list[str] = field(default_factory=list)
    made_by: str = ""
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DecisionRecord":
        return cls(
            decision_id=str(data["decision_id"]),
            project_id=str(data["project_id"]),
            decision=str(data.get("decision") or ""),
            reason=str(data.get("reason") or ""),
            impact=str(data.get("impact") or ""),
            alternatives=_string_list(data.get("alternatives")),
            made_by=str(data.get("made_by") or ""),
            created_at=float(data.get("created_at") or time.time()),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProjectStateStore:
    """Project-level state store backed by compact JSON and JSONL files."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def state_path(self, project_id: str) -> Path:
        return self.workspace.project_state_dir / f"{_slug(project_id)}.json"

    def decisions_path(self, project_id: str) -> Path:
        return self.workspace.project_state_dir / f"{_slug(project_id)}-decisions.jsonl"

    def get(self, project_id: str) -> ProjectStateCard | None:
        path = self.state_path(project_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            return ProjectStateCard.from_dict(data)
        except (KeyError, TypeError, ValueError):
            return None

    def update(
        self,
        project_id: str,
        *,
        goal: str | None = None,
        phase: str | None = None,
        current_focus: str | None = None,
        next_steps: list[str] | None = None,
        constraints: list[str] | None = None,
        blockers: list[str] | None = None,
        active_artifacts: list[str] | None = None,
        updated_by: str = "",
    ) -> ProjectStateCard:
        self.workspace.ensure()
        card = self.get(project_id) or ProjectStateCard(project_id=_slug(project_id))
        if goal is not None:
            card.goal = _clean_text(goal)
        if phase is not None:
            card.phase = _clean_text(phase)
        if current_focus is not None:
            card.current_focus = _clean_text(current_focus)
        if next_steps is not None:
            card.next_steps = _clean_list(next_steps)
        if constraints is not None:
            card.constraints = _clean_list(constraints)
        if blockers is not None:
            card.blockers = _clean_list(blockers)
        if active_artifacts is not None:
            card.active_artifacts = _clean_list(active_artifacts)
        if updated_by:
            card.updated_by = _clean_text(updated_by)
        card.updated_at = time.time()
        path = self.state_path(card.project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(card.to_dict(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
        return card

    def add_decision(
        self,
        project_id: str,
        decision: str,
        *,
        reason: str = "",
        impact: str = "",
        alternatives: list[str] | None = None,
        made_by: str = "",
    ) -> DecisionRecord:
        self.workspace.ensure()
        clean_decision = _clean_text(decision)
        if not clean_decision:
            raise ValueError("decision is empty")
        normalized_project_id = _slug(project_id)
        record = DecisionRecord(
            decision_id=f"decision-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
            project_id=normalized_project_id,
            decision=clean_decision,
            reason=_clean_text(reason),
            impact=_clean_text(impact),
            alternatives=_clean_list(alternatives),
            made_by=_clean_text(made_by),
        )
        path = self.decisions_path(normalized_project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
        return record

    def decisions(self, project_id: str, *, limit: int = 10) -> list[DecisionRecord]:
        path = self.decisions_path(project_id)
        if not path.exists():
            return []
        records: list[DecisionRecord] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                try:
                    records.append(DecisionRecord.from_dict(data))
                except (KeyError, TypeError, ValueError):
                    continue
        return sorted(records, key=lambda item: item.created_at, reverse=True)[: max(limit, 0)]


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip().lower()).strip(".-") or "default"


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _clean_list(values: list[str] | None) -> list[str]:
    return [_clean_text(value) for value in values or [] if _clean_text(value)]


def _clean_text(value: str) -> str:
    return " ".join(str(value).strip().split())
