"""Plan draft and executable step storage."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agentdeck.core.config import Workspace


PLAN_STATUSES = {"draft", "ready", "running", "paused", "blocked", "done"}
STEP_STATUSES = {"pending", "running", "done", "blocked", "failed", "skipped"}


@dataclass
class PlanStep:
    step_id: str
    title: str
    body: str = ""
    status: str = "pending"
    report: str = ""
    result: str = ""
    decision: str = ""
    artifacts: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlanStep":
        artifacts = data.get("artifacts") or []
        if not isinstance(artifacts, list):
            artifacts = []
        return cls(
            step_id=str(data["step_id"]),
            title=str(data.get("title") or data["step_id"]),
            body=str(data.get("body") or ""),
            status=_validate_step_status(str(data.get("status") or "pending")),
            report=str(data.get("report") or ""),
            result=str(data.get("result") or ""),
            decision=str(data.get("decision") or ""),
            artifacts=[str(item) for item in artifacts],
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PlanRecord:
    plan_id: str
    title: str
    draft: str = ""
    status: str = "draft"
    project_id: str = ""
    focus_id: str = ""
    session_id: str = ""
    agent_id: str = ""
    directory: str = ""
    steps: list[PlanStep] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    notes: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlanRecord":
        steps = data.get("steps") or []
        notes = data.get("notes") or []
        if not isinstance(steps, list):
            steps = []
        if not isinstance(notes, list):
            notes = []
        return cls(
            plan_id=str(data["plan_id"]),
            title=str(data.get("title") or data["plan_id"]),
            draft=str(data.get("draft") or ""),
            status=_validate_plan_status(str(data.get("status") or "draft")),
            project_id=str(data.get("project_id") or ""),
            focus_id=str(data.get("focus_id") or ""),
            session_id=str(data.get("session_id") or ""),
            agent_id=str(data.get("agent_id") or ""),
            directory=str(data.get("directory") or ""),
            steps=[PlanStep.from_dict(item) for item in steps if isinstance(item, dict)],
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            notes=[dict(item) for item in notes if isinstance(item, dict)],
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["steps"] = [step.to_dict() for step in self.steps]
        return data


class PlanRegistry:
    """JSON-backed registry for user-readable plan drafts and step specs."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    @property
    def path(self) -> Path:
        return self.workspace.plans_dir / "registry.json"

    def create(
        self,
        *,
        title: str,
        draft: str = "",
        project_id: str = "",
        focus_id: str = "",
        session_id: str = "",
        agent_id: str = "",
        directory: str | Path = "",
        metadata: dict[str, Any] | None = None,
    ) -> PlanRecord:
        clean_title = _clean_line(title)
        if not clean_title:
            raise ValueError("plan title is empty")
        records = self._read()
        plan_id = _new_plan_id()
        while plan_id in records:
            plan_id = _new_plan_id()
        now = time.time()
        record = PlanRecord(
            plan_id=plan_id,
            title=clean_title,
            draft=_clean_multiline(draft),
            status="draft",
            project_id=_clean_token(project_id),
            focus_id=focus_id.strip(),
            session_id=session_id.strip(),
            agent_id=_clean_token(agent_id),
            directory=str(Path(directory).expanduser().resolve()) if str(directory or "").strip() else "",
            created_at=now,
            updated_at=now,
            metadata=dict(metadata or {}),
        )
        records[plan_id] = record
        self._write(records)
        return record

    def get(self, plan_id: str) -> PlanRecord | None:
        return self._read().get(plan_id)

    def resolve(self, value: str) -> PlanRecord | None:
        clean = value.strip()
        records = self._read()
        if clean in records:
            return records[clean]
        normalized = _maybe_normalize_id(clean)
        if normalized in records:
            return records[normalized]
        matches = [record for record in records.values() if record.title == clean]
        if not matches:
            return None
        return sorted(matches, key=lambda item: item.updated_at, reverse=True)[0]

    def list(
        self,
        *,
        project_id: str | None = None,
        focus_id: str | None = None,
        status: str | None = None,
    ) -> list[PlanRecord]:
        records = list(self._read().values())
        if project_id:
            records = [record for record in records if record.project_id == _clean_token(project_id)]
        if focus_id:
            records = [record for record in records if record.focus_id == focus_id.strip()]
        if status:
            records = [record for record in records if record.status == _validate_plan_status(status)]
        return sorted(records, key=lambda item: (item.status == "done", -item.updated_at))

    def set_draft(self, plan: str, draft: str, *, note: str = "") -> PlanRecord | None:
        records = self._read()
        record = self.resolve(plan)
        if record is None:
            return None
        record.draft = _clean_multiline(draft)
        record.status = "draft"
        if note:
            _append_note(record, note, kind="draft")
        record.updated_at = time.time()
        records[record.plan_id] = record
        self._write(records)
        return record

    def add_note(self, plan: str, note: str, *, kind: str = "note") -> PlanRecord | None:
        records = self._read()
        record = self.resolve(plan)
        if record is None:
            return None
        _append_note(record, note, kind=kind)
        record.updated_at = time.time()
        records[record.plan_id] = record
        self._write(records)
        return record

    def compile(self, plan: str) -> PlanRecord | None:
        records = self._read()
        record = self.resolve(plan)
        if record is None:
            return None
        steps = steps_from_draft(record.draft)
        if not steps:
            raise ValueError("plan draft has no extractable steps")
        now = time.time()
        record.steps = [
            PlanStep(
                step_id=f"step-{index:03d}",
                title=title,
                body=body,
                created_at=now,
                updated_at=now,
            )
            for index, (title, body) in enumerate(steps, 1)
        ]
        record.status = "ready"
        record.updated_at = now
        _append_note(record, f"Compiled {len(record.steps)} steps from draft.", kind="compile")
        records[record.plan_id] = record
        self._write(records)
        return record

    def set_status(self, plan: str, status: str, *, note: str = "") -> PlanRecord | None:
        records = self._read()
        record = self.resolve(plan)
        if record is None:
            return None
        record.status = _validate_plan_status(status)
        if note:
            _append_note(record, note, kind=f"status:{record.status}")
        record.updated_at = time.time()
        records[record.plan_id] = record
        self._write(records)
        return record

    def start_next_step(self, plan: str) -> tuple[PlanRecord | None, PlanStep | None]:
        records = self._read()
        record = self.resolve(plan)
        if record is None:
            return None, None
        for step in record.steps:
            if step.status in {"running"}:
                return record, step
        for step in record.steps:
            if step.status == "pending":
                step.status = "running"
                step.updated_at = time.time()
                record.status = "running"
                record.updated_at = step.updated_at
                records[record.plan_id] = record
                self._write(records)
                return record, step
        if all(step.status in {"done", "skipped"} for step in record.steps):
            record.status = "done"
            record.updated_at = time.time()
            records[record.plan_id] = record
            self._write(records)
        return record, None

    def update_step(
        self,
        plan: str,
        step: str,
        *,
        status: str,
        report: str = "",
        result: str = "",
        decision: str = "",
        artifacts: list[str] | None = None,
    ) -> PlanRecord | None:
        records = self._read()
        record = self.resolve(plan)
        if record is None:
            return None
        clean_status = _validate_step_status(status)
        found = False
        for item in record.steps:
            if item.step_id == step:
                item.status = clean_status
                if report:
                    item.report = _clean_multiline(report)
                if result:
                    item.result = _clean_multiline(result)
                if decision:
                    item.decision = _clean_multiline(decision)
                if artifacts is not None:
                    item.artifacts = [_clean_line(value) for value in artifacts if _clean_line(value)]
                item.updated_at = time.time()
                found = True
                break
        if not found:
            return None
        if any(item.status == "blocked" for item in record.steps):
            record.status = "blocked"
        elif all(item.status in {"done", "skipped"} for item in record.steps):
            record.status = "done"
        elif any(item.status == "running" for item in record.steps):
            record.status = "running"
        elif record.status == "draft":
            record.status = "ready"
        record.updated_at = time.time()
        records[record.plan_id] = record
        self._write(records)
        return record

    def current_step(self, plan: str) -> PlanStep | None:
        record = self.resolve(plan)
        if record is None:
            return None
        running = [step for step in record.steps if step.status == "running"]
        if running:
            return running[0]
        pending = [step for step in record.steps if step.status == "pending"]
        return pending[0] if pending else None

    def _read(self) -> dict[str, PlanRecord]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        raw = data.get("plans", data)
        if not isinstance(raw, dict):
            return {}
        records: dict[str, PlanRecord] = {}
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue
            try:
                records[str(key)] = PlanRecord.from_dict(value)
            except (KeyError, TypeError, ValueError):
                continue
        return records

    def _write(self, records: dict[str, PlanRecord]) -> None:
        self.workspace.plans_dir.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "plans": {key: record.to_dict() for key, record in sorted(records.items())}}
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


def steps_from_draft(draft: str) -> list[tuple[str, str]]:
    """Extract executable steps from a user-readable Markdown draft."""
    lines = draft.splitlines()
    steps: list[tuple[str, str]] = []
    current_title = ""
    current_body: list[str] = []

    def flush() -> None:
        nonlocal current_title, current_body
        title = _clean_line(current_title)
        body = _clean_multiline("\n".join(current_body))
        if title:
            steps.append((title, body))
        current_title = ""
        current_body = []

    for line in lines:
        stripped = line.strip()
        heading = re.match(r"^#{2,6}\s+(.+)$", stripped)
        checklist = re.match(r"^[-*]\s+\[[ xX]\]\s+(.+)$", stripped)
        numbered = re.match(r"^\d+[.)]\s+(.+)$", stripped)
        bullet_step = re.match(r"^[-*]\s+(?:step\s*)?(\d+[:.)]\s+.+)$", stripped, flags=re.IGNORECASE)
        matched_title = ""
        if heading and not stripped.startswith("# "):
            matched_title = heading.group(1)
        elif checklist:
            matched_title = checklist.group(1)
        elif numbered:
            matched_title = numbered.group(1)
        elif bullet_step:
            matched_title = bullet_step.group(1)
        if matched_title:
            flush()
            current_title = _strip_step_prefix(matched_title)
            continue
        if current_title:
            current_body.append(line)
    flush()
    if steps:
        return steps

    fallback = [_clean_line(line) for line in lines if _clean_line(line)]
    return [(line, "") for line in fallback[:20]]


def plan_progress(record: PlanRecord) -> tuple[int, int]:
    done = sum(1 for step in record.steps if step.status in {"done", "skipped"})
    return done, len(record.steps)


def _append_note(record: PlanRecord, note: str, *, kind: str) -> None:
    clean = _clean_multiline(note)
    if not clean:
        return
    record.notes.append({"kind": kind, "text": clean, "created_at": time.time()})
    record.notes = record.notes[-100:]


def _strip_step_prefix(value: str) -> str:
    return re.sub(r"^(?:step\s*)?\d+[:.)]\s*", "", value.strip(), flags=re.IGNORECASE).strip()


def _clean_multiline(value: str) -> str:
    lines = [line.rstrip() for line in str(value).strip().splitlines()]
    return "\n".join(lines).strip()


def _clean_line(value: str) -> str:
    return " ".join(str(value).strip().split())


def _clean_token(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value).strip().lower()).strip(".-")


def _maybe_normalize_id(value: str) -> str:
    clean = value.strip()
    return clean if clean.startswith("plan-") else _clean_token(clean)


def _validate_plan_status(value: str) -> str:
    clean = value.strip().lower()
    if clean not in PLAN_STATUSES:
        raise ValueError(f"unsupported plan status: {value}")
    return clean


def _validate_step_status(value: str) -> str:
    clean = value.strip().lower()
    if clean not in STEP_STATUSES:
        raise ValueError(f"unsupported plan step status: {value}")
    return clean


def _new_plan_id() -> str:
    return f"plan-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
