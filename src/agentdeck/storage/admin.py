"""Cross-registry administrative mutations."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentdeck.core.config import Workspace
from agentdeck.storage.agents import AgentRegistry
from agentdeck.storage.approvals import ApprovalRegistry
from agentdeck.storage.jobs import JobRegistry
from agentdeck.storage.projects import ProjectRegistry
from agentdeck.storage.sessions import SessionRegistry
from agentdeck.storage.tasks import TaskBoard


ENTITY_TYPES = {"project", "task", "agent", "session"}


@dataclass
class AdminMutationResult:
    action: str
    entity: str
    old_id: str = ""
    new_id: str = ""
    message: str = ""
    changed: dict[str, int] = field(default_factory=dict)

    def add(self, key: str, count: int) -> None:
        if count:
            self.changed[key] = self.changed.get(key, 0) + count


class AdminMutationError(ValueError):
    """Raised when an administrative mutation cannot be applied safely."""


def rename_global_id(workspace: Workspace, *, entity: str, old_id: str, new_id: str) -> AdminMutationResult:
    """Rename one AgentDeck id and update registry references globally."""

    entity = _entity(entity)
    old_id = old_id.strip()
    new_id = _normalize_id(new_id)
    if not old_id or not new_id:
        raise AdminMutationError("old_id and new_id are required")
    if old_id == new_id:
        raise AdminMutationError("old_id and new_id are the same")

    result = AdminMutationResult(action="rename", entity=entity, old_id=old_id, new_id=new_id)
    if entity == "project":
        _rename_project(workspace, old_id, new_id, result)
    elif entity == "task":
        _rename_task(workspace, old_id, new_id, result)
    elif entity == "agent":
        _rename_agent(workspace, old_id, new_id, result)
    elif entity == "session":
        _rename_session(workspace, old_id, new_id, result)
    _rewrite_telegram_state(workspace, entity=entity, old_id=old_id, new_id=new_id)
    result.add("telegram_state", 1)
    result.message = f"renamed {entity}: {old_id} -> {new_id}"
    return result


def delete_project(workspace: Workspace, project_id: str) -> AdminMutationResult:
    """Archive a project record without breaking child references.

    The public function name remains ``delete_project`` because the Web action is
    named delete, but the storage behavior is intentionally recoverable.
    """

    project_id = _normalize_id(project_id)
    if not project_id:
        raise AdminMutationError("project_id is required")
    registry = ProjectRegistry(workspace)
    projects = registry._read()
    if project_id not in projects:
        raise AdminMutationError(f"project not found: {project_id}")

    record = projects[project_id]
    record.status = "archived"
    record.updated_at = time.time()
    record.metadata["archived_at"] = record.updated_at
    registry._write(projects)
    result = AdminMutationResult(
        action="archive",
        entity="project",
        old_id=project_id,
        message=f"archived project: {project_id}",
    )
    result.add("projects", 1)
    return result


def restore_project(workspace: Workspace, project_id: str) -> AdminMutationResult:
    """Restore an archived project record."""

    project_id = _normalize_id(project_id)
    if not project_id:
        raise AdminMutationError("project_id is required")
    registry = ProjectRegistry(workspace)
    projects = registry._read()
    if project_id not in projects:
        raise AdminMutationError(f"project not found: {project_id}")

    record = projects[project_id]
    record.status = "active"
    record.updated_at = time.time()
    record.metadata["restored_at"] = record.updated_at
    projects[project_id] = record
    registry._write(projects)
    result = AdminMutationResult(
        action="restore",
        entity="project",
        old_id=project_id,
        message=f"restored project: {project_id}",
    )
    result.add("projects", 1)
    return result


def _rename_project(workspace: Workspace, old_id: str, new_id: str, result: AdminMutationResult) -> None:
    registry = ProjectRegistry(workspace)
    records = registry._read()
    if old_id not in records:
        raise AdminMutationError(f"project not found: {old_id}")
    if new_id in records:
        raise AdminMutationError(f"project already exists: {new_id}")
    record = records.pop(old_id)
    record.project_id = new_id
    record.updated_at = time.time()
    records[new_id] = record
    registry._write(records)
    result.add("projects", 1)
    result.add("tasks", _replace_task_field(workspace, "project_id", old_id, new_id))
    result.add("agents", _replace_agent_field(workspace, "project_id", old_id, new_id))
    result.add("approvals", _replace_approval_field(workspace, "project_id", old_id, new_id))
    result.add("progress", _rewrite_progress_journal(workspace, project_id=(old_id, new_id)))
    result.add("session_state", _rewrite_session_state_cards(workspace, project_id=(old_id, new_id)))
    result.add("project_state", _rename_project_state(workspace, old_id, new_id))
    result.add("memory", _rename_memory_owner(workspace, scope_dir="projects", old_id=old_id, new_id=new_id))


def _rename_task(workspace: Workspace, old_id: str, new_id: str, result: AdminMutationResult) -> None:
    board = TaskBoard(workspace)
    records = board._read()
    if old_id not in records:
        raise AdminMutationError(f"task not found: {old_id}")
    if new_id in records:
        raise AdminMutationError(f"task already exists: {new_id}")
    record = records.pop(old_id)
    record.task_id = new_id
    record.updated_at = time.time()
    records[new_id] = record
    board._write(records)
    result.add("tasks", 1)
    result.add("jobs", _replace_job_field(workspace, "task_id", old_id, new_id))
    result.add("approvals", _replace_approval_field(workspace, "task_id", old_id, new_id))
    result.add("progress", _rewrite_progress_journal(workspace, task_id=(old_id, new_id)))
    result.add("session_state", _rewrite_session_state_cards(workspace, task_id=(old_id, new_id)))
    result.add("memory", _rename_memory_owner(workspace, scope_dir="tasks", old_id=old_id, new_id=new_id))


def _rename_agent(workspace: Workspace, old_id: str, new_id: str, result: AdminMutationResult) -> None:
    registry = AgentRegistry(workspace)
    records = registry._read()
    if old_id not in records:
        raise AdminMutationError(f"agent not found: {old_id}")
    if new_id in records:
        raise AdminMutationError(f"agent already exists: {new_id}")
    record = records.pop(old_id)
    record.agent_id = new_id
    record.updated_at = time.time()
    records[new_id] = record
    registry._write(records)
    result.add("agents", 1)
    result.add("projects", _replace_project_field(workspace, "default_agent_id", old_id, new_id))
    result.add("tasks", _replace_task_field(workspace, "agent_id", old_id, new_id))
    result.add("sessions", _replace_session_field(workspace, "agent_id", old_id, new_id))
    result.add("approvals", _replace_approval_field(workspace, "agent_id", old_id, new_id))
    result.add("progress", _rewrite_progress_journal(workspace, agent_id=(old_id, new_id)))
    result.add("session_state", _rewrite_session_state_cards(workspace, agent_id=(old_id, new_id)))
    result.add("memory", _rename_memory_owner(workspace, scope_dir="agents", old_id=old_id, new_id=new_id))


def _rename_session(workspace: Workspace, old_id: str, new_id: str, result: AdminMutationResult) -> None:
    registry = SessionRegistry(workspace)
    records = registry._read()
    if old_id not in records:
        raise AdminMutationError(f"session not found: {old_id}")
    if new_id in records:
        raise AdminMutationError(f"session already exists: {new_id}")
    record = records.pop(old_id)
    record.session_id = new_id
    record.updated_at = time.time()
    records[new_id] = record
    registry._write(records)
    result.add("sessions", 1)
    result.add("tasks", _replace_task_field(workspace, "session_id", old_id, new_id))
    result.add("jobs", _replace_job_field(workspace, "session_id", old_id, new_id))
    result.add("approvals", _replace_approval_field(workspace, "session_id", old_id, new_id))
    result.add("progress", _rewrite_progress_journal(workspace, session_id=(old_id, new_id)))
    result.add("session_state", _rename_session_state_card(workspace, old_id, new_id))


def _replace_project_field(workspace: Workspace, field_name: str, old: str, new: str) -> int:
    registry = ProjectRegistry(workspace)
    records = registry._read()
    changed = 0
    for record in records.values():
        if getattr(record, field_name) == old:
            setattr(record, field_name, new)
            record.updated_at = time.time()
            changed += 1
    if changed:
        registry._write(records)
    return changed


def _replace_task_field(workspace: Workspace, field_name: str, old: str, new: str) -> int:
    board = TaskBoard(workspace)
    records = board._read()
    changed = 0
    for record in records.values():
        if getattr(record, field_name) == old:
            setattr(record, field_name, new)
            record.updated_at = time.time()
            changed += 1
    if changed:
        board._write(records)
    return changed


def _replace_agent_field(workspace: Workspace, field_name: str, old: str, new: str) -> int:
    registry = AgentRegistry(workspace)
    records = registry._read()
    changed = 0
    for record in records.values():
        if getattr(record, field_name) == old:
            setattr(record, field_name, new)
            record.updated_at = time.time()
            changed += 1
    if changed:
        registry._write(records)
    return changed


def _replace_session_field(workspace: Workspace, field_name: str, old: str, new: str) -> int:
    registry = SessionRegistry(workspace)
    records = registry._read()
    changed = 0
    for record in records.values():
        if getattr(record, field_name) == old:
            setattr(record, field_name, new)
            record.updated_at = time.time()
            changed += 1
    if changed:
        registry._write(records)
    return changed


def _replace_job_field(workspace: Workspace, field_name: str, old: str, new: str) -> int:
    registry = JobRegistry(workspace)
    records = registry._read()
    changed = 0
    for record in records.values():
        if getattr(record, field_name) == old:
            setattr(record, field_name, new)
            record.updated_at = time.time()
            changed += 1
    if changed:
        registry._write(records)
    return changed


def _replace_approval_field(workspace: Workspace, field_name: str, old: str, new: str) -> int:
    registry = ApprovalRegistry(workspace)
    records = registry._read()
    changed = 0
    for record in records.values():
        if getattr(record, field_name) == old:
            setattr(record, field_name, new)
            record.updated_at = time.time()
            changed += 1
    if changed:
        registry._write(records)
    return changed


def _rewrite_progress_journal(
    workspace: Workspace,
    *,
    project_id: tuple[str, str] | None = None,
    task_id: tuple[str, str] | None = None,
    session_id: tuple[str, str] | None = None,
    agent_id: tuple[str, str] | None = None,
) -> int:
    path = workspace.journal_dir / "progress.jsonl"
    if not path.exists():
        return 0
    changed = 0
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            lines.append(line)
            continue
        if isinstance(data, dict):
            changed += _replace_dict_field(data, "project_id", project_id)
            changed += _replace_dict_field(data, "task_id", task_id)
            changed += _replace_dict_field(data, "session_id", session_id)
            changed += _replace_dict_field(data, "agent_id", agent_id)
            lines.append(json.dumps(data, ensure_ascii=False, sort_keys=True))
        else:
            lines.append(line)
    if changed:
        _write_lines_atomic(path, lines)
    return changed


def _rewrite_session_state_cards(
    workspace: Workspace,
    *,
    project_id: tuple[str, str] | None = None,
    task_id: tuple[str, str] | None = None,
    agent_id: tuple[str, str] | None = None,
) -> int:
    if not workspace.session_state_dir.exists():
        return 0
    changed = 0
    for path in workspace.session_state_dir.glob("*.json"):
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        local = 0
        local += _replace_dict_field(data, "project_id", project_id)
        local += _replace_dict_field(data, "task_id", task_id)
        local += _replace_dict_field(data, "agent_id", agent_id)
        if local:
            data["updated_at"] = time.time()
            _write_json_atomic(path, data)
            changed += local
    return changed


def _rename_session_state_card(workspace: Workspace, old_id: str, new_id: str) -> int:
    old_path = _session_state_path(workspace, old_id)
    new_path = _session_state_path(workspace, new_id)
    changed = 0
    if old_path.exists():
        data = _read_json(old_path)
        if isinstance(data, dict):
            data["session_id"] = new_id
            data["updated_at"] = time.time()
            _write_json_atomic(new_path, data)
            if old_path != new_path:
                old_path.unlink()
            changed += 1
    changed += _rewrite_session_state_card_refs(workspace, old_id, new_id)
    return changed


def _rewrite_session_state_card_refs(workspace: Workspace, old_id: str, new_id: str) -> int:
    if not workspace.session_state_dir.exists():
        return 0
    changed = 0
    for path in workspace.session_state_dir.glob("*.json"):
        if path == _session_state_path(workspace, new_id):
            continue
        data = _read_json(path)
        if isinstance(data, dict) and data.get("session_id") == old_id:
            data["session_id"] = new_id
            data["updated_at"] = time.time()
            _write_json_atomic(path, data)
            changed += 1
    return changed


def _rename_project_state(workspace: Workspace, old_id: str, new_id: str) -> int:
    changed = 0
    old_state = workspace.project_state_dir / f"{_slug_dash(old_id)}.json"
    new_state = workspace.project_state_dir / f"{_slug_dash(new_id)}.json"
    if old_state.exists():
        data = _read_json(old_state)
        if isinstance(data, dict):
            data["project_id"] = new_id
            data["updated_at"] = time.time()
            _write_json_atomic(new_state, data)
            if old_state != new_state:
                old_state.unlink()
            changed += 1

    old_decisions = workspace.project_state_dir / f"{_slug_dash(old_id)}-decisions.jsonl"
    new_decisions = workspace.project_state_dir / f"{_slug_dash(new_id)}-decisions.jsonl"
    if old_decisions.exists():
        lines: list[str] = []
        for line in old_decisions.read_text(encoding="utf-8").splitlines():
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                lines.append(line)
                continue
            if isinstance(data, dict):
                if data.get("project_id") == old_id:
                    data["project_id"] = new_id
                    changed += 1
                lines.append(json.dumps(data, ensure_ascii=False, sort_keys=True))
            else:
                lines.append(line)
        _write_lines_atomic(new_decisions, lines, append=new_decisions.exists() and new_decisions != old_decisions)
        if old_decisions != new_decisions:
            old_decisions.unlink()
    return changed


def _rename_memory_owner(workspace: Workspace, *, scope_dir: str, old_id: str, new_id: str) -> int:
    base = workspace.memory_dir / scope_dir
    old_dir = base / _slug_underscore(old_id)
    new_dir = base / _slug_underscore(new_id)
    changed = 0
    if old_dir.exists():
        new_dir.parent.mkdir(parents=True, exist_ok=True)
        if not new_dir.exists():
            old_dir.rename(new_dir)
            changed += 1
        else:
            for path in old_dir.iterdir():
                target = _next_nonconflicting_path(new_dir / path.name)
                path.rename(target)
                changed += 1
            try:
                old_dir.rmdir()
            except OSError:
                pass
    if new_dir.exists():
        for path in new_dir.glob("*.md"):
            text = path.read_text(encoding="utf-8")
            updated = re.sub(rf"(?m)^owner:\s*{re.escape(old_id)}\s*$", f"owner: {new_id}", text)
            if updated != text:
                path.write_text(updated, encoding="utf-8")
                changed += 1
    return changed


def _rewrite_telegram_state(workspace: Workspace, *, entity: str, old_id: str, new_id: str) -> None:
    path = workspace.root / "telegram" / "state.json"
    data = _read_json(path)
    if not isinstance(data, dict):
        return
    chats = data.get("chats", data)
    if not isinstance(chats, dict):
        return
    key_map = {
        "project": ("current_project_id", "recent_project_ids"),
        "task": ("current_task_id", "recent_task_ids"),
        "agent": ("current_agent_id", "recent_agent_ids"),
        "session": ("current_session_id", "recent_session_ids"),
    }
    current_key, recent_key = key_map[entity]
    for chat in chats.values():
        if not isinstance(chat, dict):
            continue
        if chat.get(current_key) == old_id:
            chat[current_key] = new_id
        if isinstance(chat.get(recent_key), list):
            chat[recent_key] = _replace_list_values(chat[recent_key], old_id, new_id)
        if entity == "task":
            auto = chat.get("auto")
            if isinstance(auto, dict) and auto.get("task_id") == old_id:
                auto["task_id"] = new_id
        if entity == "project" and not new_id and chat.get("current_project_id") == old_id:
            chat["current_project_id"] = ""
    payload = {"version": int(data.get("version") or 1), "chats": chats}
    _write_json_atomic(path, payload)


def _replace_dict_field(data: dict[str, Any], key: str, pair: tuple[str, str] | None) -> int:
    if pair is None:
        return 0
    old, new = pair
    if data.get(key) == old:
        data[key] = new
        return 1
    return 0


def _replace_list_values(values: list[Any], old: str, new: str) -> list[Any]:
    replaced = [new if value == old else value for value in values]
    if new:
        deduped: list[Any] = []
        seen: set[Any] = set()
        for value in replaced:
            if value in seen:
                continue
            seen.add(value)
            deduped.append(value)
        return deduped
    return [value for value in replaced if value]


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _write_lines_atomic(path: Path, lines: list[str], *, append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if append and path.exists():
        existing = path.read_text(encoding="utf-8").splitlines()
        lines = existing + lines
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines).rstrip() + ("\n" if lines else ""), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _session_state_path(workspace: Workspace, session_id: str) -> Path:
    return workspace.session_state_dir / f"{_slug_underscore(session_id)}.json"


def _next_nonconflicting_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 10000):
        candidate = path.with_name(f"{stem}_{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise AdminMutationError(f"could not find non-conflicting path for: {path}")


def _entity(value: str) -> str:
    entity = value.strip().lower()
    if entity not in ENTITY_TYPES:
        raise AdminMutationError(f"unsupported entity: {value}")
    return entity


def _normalize_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip().lower()).strip(".-")


def _slug_dash(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip().lower()).strip(".-") or "default"


def _slug_underscore(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", value.strip().lower()).strip("._") or "default"
