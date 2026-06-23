"""Project-local agent registry."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agentdeck.core.config import Workspace


ROLE_TEMPLATE_METADATA_KEY = "role_template"

ASSISTANT_AGENT_ID = "assistant"

DEFAULT_ASSISTANT_TEMPLATE = (
    "You are the user's AgentDeck assistant. Before a project or task is selected, "
    "help route the user to the right project, agent, and task. Prefer concise questions when information is missing. "
    "When useful, suggest exact AgentDeck or Telegram commands. You may help create projects, agents, tasks, and bot bindings, "
    "and help adopt or enter existing Codex/Kimi sessions by listing sessions with /sessions, selecting one with /use session, "
    "or scanning the old provider cwd with /session scan, then importing a numbered candidate with /session import. "
    "To enter a session, execute /use session through AGENTDECK_ACTION; do not merely describe or claim that a session was selected. "
    "Do not say a project, task, or session has been switched unless you include the matching AGENTDECK_ACTION line or the user has shown /current confirming it. "
    "Do not expose secrets such as bot tokens. Keep responses suitable for a phone chat. "
    "If you are confident that a safe AgentDeck Telegram control command should be executed, put it on its own final line as "
    "`AGENTDECK_ACTION: /command ...`. Only use safe routing and service-control commands such as /projects, /agents, /tasks, /status, "
    "/restart, /use project, /use agent, /use task, /use session, /project new, /agent new, /task new, /session scan, or /session import. "
    "Never request /run, /auto, /approve, /reject, /cancel, shell commands, destructive actions, or commands that reveal secrets."
)

DEFAULT_ROLE_TEMPLATES: dict[str, str] = {
    "owner": (
        "Coordinate the current task end to end. Keep project state, task handoffs, "
        "and durable memory current when important progress or decisions happen."
    ),
    "manager": (
        "Own direction and review. Keep the goal, constraints, and next step clear; "
        "record decisions and manager reviews; avoid doing broad executor work unless it is necessary to unblock the team."
    ),
    "planner": (
        "Break goals into small executable tasks, update project state, and call out sequencing, dependencies, and risks."
    ),
    "executor": (
        "Implement the next scoped step, verify the result, and record a concise handoff with completed work, evidence, blockers, and next steps."
    ),
    "developer": (
        "Make narrow code changes for the active task, follow existing project patterns, run relevant tests, and record a concise handoff."
    ),
    "tester": (
        "Verify behavior with focused tests or inspection, report evidence clearly, and avoid unrelated implementation changes."
    ),
    "reviewer": (
        "Review for correctness, regressions, missing tests, and task alignment; record requested changes or approval as a manager review."
    ),
}


@dataclass
class AgentRecord:
    """Saved defaults for one project agent."""

    agent_id: str
    title: str
    project_id: str = ""
    role: str = "owner"
    team_id: str = "default"
    adapter: str = "echo"
    project_dir: str = ""
    model: str = ""
    sandbox: str = ""
    approval_mode: str = "fail"
    codex_bin: str = "codex"
    kimi_bin: str = "kimi"
    resume_policy: str = "latest"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentRecord":
        return cls(
            agent_id=str(data["agent_id"]),
            title=str(data.get("title") or data["agent_id"]),
            project_id=str(data.get("project_id") or ""),
            role=str(data.get("role") or "owner"),
            team_id=str(data.get("team_id") or "default"),
            adapter=str(data.get("adapter") or "echo"),
            project_dir=str(data.get("project_dir") or ""),
            model=str(data.get("model") or ""),
            sandbox=str(data.get("sandbox") or ""),
            approval_mode=str(data.get("approval_mode") or "fail"),
            codex_bin=str(data.get("codex_bin") or "codex"),
            kimi_bin=str(data.get("kimi_bin") or "kimi"),
            resume_policy=str(data.get("resume_policy") or "latest"),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AgentRegistry:
    """JSON-backed index of project agents."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    @property
    def path(self) -> Path:
        return self.workspace.agents_dir / "registry.json"

    def upsert(
        self,
        *,
        agent_id: str,
        title: str | None = None,
        project_id: str = "",
        role: str = "owner",
        team_id: str = "default",
        adapter: str = "echo",
        project_dir: str | Path = ".",
        model: str = "",
        sandbox: str = "",
        approval_mode: str = "fail",
        codex_bin: str = "codex",
        kimi_bin: str = "kimi",
        resume_policy: str = "latest",
        replace: bool = False,
    ) -> AgentRecord:
        agent_id = _normalize_agent_id(agent_id)
        records = self._read()
        existing = records.get(agent_id)
        if existing is not None and not replace:
            raise ValueError(f"agent already exists: {agent_id}")

        now = time.time()
        created_at = existing.created_at if existing is not None else now
        record = AgentRecord(
            agent_id=agent_id,
            title=_clean_title(title or "") or _title_from_id(agent_id),
            project_id=_normalize_token(project_id) if project_id else "",
            role=_normalize_token(role or "owner"),
            team_id=_normalize_token(team_id or "default"),
            adapter=adapter,
            project_dir=str(Path(project_dir).expanduser().resolve()),
            model=model or "",
            sandbox=sandbox or "",
            approval_mode=approval_mode or "fail",
            codex_bin=codex_bin or "codex",
            kimi_bin=kimi_bin or "kimi",
            resume_policy=resume_policy or "latest",
            created_at=created_at,
            updated_at=now,
            metadata=dict(existing.metadata) if existing is not None else {},
        )
        records[agent_id] = record
        self._write(records)
        return record

    def get(self, agent_id: str) -> AgentRecord | None:
        return self._read().get(agent_id)

    def resolve(self, value: str) -> AgentRecord | None:
        records = self._read()
        normalized = _maybe_normalize_agent_id(value)
        if normalized in records:
            return records[normalized]
        matches = [record for record in records.values() if record.title == value]
        if not matches:
            return None
        return sorted(matches, key=lambda item: item.updated_at, reverse=True)[0]

    def list(
        self,
        *,
        project_id: str | None = None,
        team_id: str | None = None,
        role: str | None = None,
    ) -> list[AgentRecord]:
        records = list(self._read().values())
        if project_id:
            records = [record for record in records if record.project_id == _normalize_token(project_id)]
        if team_id:
            records = [record for record in records if record.team_id == _normalize_token(team_id)]
        if role:
            records = [record for record in records if record.role == _normalize_token(role)]
        return sorted(records, key=lambda item: (item.project_id, item.team_id, item.role, item.agent_id))

    def set_role_template(self, agent: str, template: str) -> AgentRecord:
        records = self._read()
        record = self.resolve(agent)
        if record is None:
            raise ValueError(f"agent not found: {agent}")
        clean = _clean_multiline(template)
        if clean:
            record.metadata[ROLE_TEMPLATE_METADATA_KEY] = clean
        else:
            record.metadata.pop(ROLE_TEMPLATE_METADATA_KEY, None)
        record.updated_at = time.time()
        records[record.agent_id] = record
        self._write(records)
        return record

    def _read(self) -> dict[str, AgentRecord]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        raw_records = data.get("agents", data)
        if not isinstance(raw_records, dict):
            return {}

        records: dict[str, AgentRecord] = {}
        for key, value in raw_records.items():
            if not isinstance(value, dict):
                continue
            try:
                record = AgentRecord.from_dict(value)
            except (KeyError, TypeError, ValueError):
                continue
            records[str(key)] = record
        return records

    def _write(self, records: dict[str, AgentRecord]) -> None:
        self.workspace.agents_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "agents": {key: record.to_dict() for key, record in sorted(records.items())},
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


def _normalize_agent_id(value: str) -> str:
    normalized = _maybe_normalize_agent_id(value)
    if not normalized:
        raise ValueError("agent id is empty")
    return normalized


def _maybe_normalize_agent_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip().lower()).strip(".-")


def _normalize_token(value: str) -> str:
    return _maybe_normalize_agent_id(value) or "default"


def _title_from_id(value: str) -> str:
    return " ".join(part.capitalize() for part in re.split(r"[-_.]+", value) if part) or value


def _clean_title(value: str) -> str:
    return " ".join(value.strip().split())


def _clean_multiline(value: str) -> str:
    lines = [" ".join(line.strip().split()) for line in value.splitlines()]
    return "\n".join(line for line in lines if line)


def role_template_for_agent(record: AgentRecord) -> str:
    explicit = str(record.metadata.get(ROLE_TEMPLATE_METADATA_KEY) or "").strip()
    if explicit:
        return explicit
    return DEFAULT_ROLE_TEMPLATES.get(record.role, "")
