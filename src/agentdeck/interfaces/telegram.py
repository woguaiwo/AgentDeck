"""Telegram interface for AgentDeck."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import shlex
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from agentdeck.adapters.capabilities import adapter_requires_provider_session
from agentdeck.core.cancel import CancellationToken
from agentdeck.core.config import Workspace
from agentdeck.core.error_daemon import (
    ErrorHandlingDaemon,
    create_error_incident_for_job,
    event_should_fail_job,
    first_error_event,
    format_error_decision_message,
)
from agentdeck.core.events import EventKind
from agentdeck.core.run_service import (
    RunConfigurationError,
    RunRequest,
    build_agentdeck_context,
    collect_relevant_memories,
    run_agent_prompt,
)
from agentdeck.storage.approvals import ApprovalRecord, ApprovalRegistry
from agentdeck.storage.agents import ASSISTANT_AGENT_ID, AgentRecord, AgentRegistry
from agentdeck.storage.jobs import JobRecord, JobRegistry
from agentdeck.storage.memory import MemoryDocument, MarkdownMemoryStore
from agentdeck.storage.progress import ProgressJournal, format_review
from agentdeck.storage.provider_sessions import ProviderSessionCandidate, scan_provider_sessions
from agentdeck.storage.projects import ProjectRecord, ProjectRegistry
from agentdeck.storage.project_state import ProjectStateStore
from agentdeck.storage.session_state import SessionStateStore
from agentdeck.storage.sessions import SessionRecord, SessionRegistry
from agentdeck.storage.tasks import TaskBoard, TaskRecord


MAX_TELEGRAM_MESSAGE = 3900
AUTO_CONTINUE_DELAY_SECONDS = 1.0
DEFAULT_AUTO_APPROVAL_MODE = "bypass"
HUMAN_AUTO_APPROVAL_MODE = "fail"
ASSISTANT_ACTION_PREFIX = "AGENTDECK_ACTION:"
MAX_ASSISTANT_ACTIONS = 3
DEFAULT_AUTO_PROMPT = (
    "请继续推进当前任务。要求：主动完成下一步；如果取得阶段性进展、"
    "做出重要决定或遇到阻塞，请用简短要点记录到项目日志或任务备注里；"
    "如果需要用户决策、权限或外部信息，请停止并明确说明。"
)
AUTO_TASK_DONE_MARKER = "AGENTDECK_AUTO_TASK_DONE"
DEFAULT_AUTO_TASK_PROMPT = (
    "请自动推进当前任务。每一轮主动完成下一步，并记录重要进展、决定、证据或阻塞。"
    "如果你判断当前 task 的范围和细节已经基本充分完成，请在回复最后单独一行输出 "
    f"{AUTO_TASK_DONE_MARKER}，并给出简短完成摘要；否则不要输出这个标记，继续说明下一步。"
)
ACTIVE_JOB_STATUSES = {"queued", "running", "cancel_requested"}
_RESTART_LOCK = threading.Lock()
_RESTART_SCHEDULED = False


@dataclass
class TelegramConfig:
    token: str
    allowed_chat_ids: set[int] = field(default_factory=set)
    poll_timeout: int = 30
    bot_id: str = ""
    assistant_agent_id: str = ""


class TelegramBotApi:
    """Small Telegram Bot API client using the standard library."""

    def __init__(self, token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"

    def get_updates(self, *, offset: int | None = None, timeout: int = 30) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        data = self._request("getUpdates", payload)
        result = data.get("result") or []
        return result if isinstance(result, list) else []

    def send_message(self, chat_id: int, text: str) -> None:
        for chunk in split_message(text):
            self._request("sendMessage", {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True})

    def send_video(self, chat_id: int, path: str | Path, caption: str = "") -> None:
        video_path = Path(path).expanduser()
        fields: dict[str, str | int | bool] = {"chat_id": chat_id, "supports_streaming": True}
        clean_caption = caption.strip()
        if clean_caption:
            fields["caption"] = clean_caption[:1000]
        self._request_multipart("sendVideo", fields, {"video": video_path})

    def _request(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(f"{self.base_url}/{method}", data=body)
        with urllib.request.urlopen(request, timeout=max(35, int(payload.get("timeout") or 0) + 5)) as response:
            data = json.loads(response.read().decode("utf-8"))
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data}")
        return data

    def _request_multipart(
        self,
        method: str,
        fields: dict[str, str | int | bool],
        files: dict[str, Path],
    ) -> dict[str, Any]:
        boundary = f"agentdeck-{uuid.uuid4().hex}"
        body = _build_multipart_body(boundary, fields, files)
        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data}")
        return data


class TelegramChatStateStore:
    """Persist small per-chat interface state."""

    def __init__(self, workspace: Workspace, *, scope: str = "") -> None:
        self.workspace = workspace
        self.scope = scope.strip()
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self.workspace.root / "telegram" / "state.json"

    def current_task(self, chat_id: int) -> str:
        with self._lock:
            data = self._read()
            return str(data.get(self._chat_key(chat_id), {}).get("current_task_id") or "")

    def set_current_task(self, chat_id: int, task_id: str) -> None:
        with self._lock:
            data = self._read()
            key = self._chat_key(chat_id)
            chat = dict(data.get(key) or {})
            chat["current_task_id"] = task_id
            data[key] = chat
            self._write(data)

    def current_session(self, chat_id: int) -> str:
        with self._lock:
            data = self._read()
            return str(data.get(self._chat_key(chat_id), {}).get("current_session_id") or "")

    def set_current_session(self, chat_id: int, session_id: str) -> None:
        with self._lock:
            data = self._read()
            key = self._chat_key(chat_id)
            chat = dict(data.get(key) or {})
            chat["current_session_id"] = session_id
            data[key] = chat
            self._write(data)

    def current_project(self, chat_id: int) -> str:
        with self._lock:
            data = self._read()
            return str(data.get(self._chat_key(chat_id), {}).get("current_project_id") or "")

    def set_current_project(self, chat_id: int, project_id: str) -> None:
        with self._lock:
            data = self._read()
            key = self._chat_key(chat_id)
            chat = dict(data.get(key) or {})
            chat["current_project_id"] = project_id
            data[key] = chat
            self._write(data)

    def current_agent(self, chat_id: int) -> str:
        with self._lock:
            data = self._read()
            return str(data.get(self._chat_key(chat_id), {}).get("current_agent_id") or "")

    def set_current_agent(self, chat_id: int, agent_id: str) -> None:
        with self._lock:
            data = self._read()
            key = self._chat_key(chat_id)
            chat = dict(data.get(key) or {})
            chat["current_agent_id"] = agent_id
            data[key] = chat
            self._write(data)

    def auto_state(self, chat_id: int) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            state = data.get(self._chat_key(chat_id), {}).get("auto") or {}
            return dict(state) if isinstance(state, dict) else {}

    def set_auto_state(
        self,
        chat_id: int,
        *,
        enabled: bool,
        task_id: str = "",
        prompt: str = DEFAULT_AUTO_PROMPT,
        until: float = 0.0,
        turns_started: int = 0,
        last_job_id: str = "",
        approval_mode: str = DEFAULT_AUTO_APPROVAL_MODE,
        mode: str = "loop",
    ) -> None:
        with self._lock:
            data = self._read()
            key = self._chat_key(chat_id)
            chat = dict(data.get(key) or {})
            chat["auto"] = {
                "enabled": enabled,
                "task_id": task_id,
                "prompt": prompt,
                "until": until,
                "turns_started": turns_started,
                "last_job_id": last_job_id,
                "approval_mode": _normalize_auto_approval_mode(approval_mode),
                "mode": _normalize_auto_mode(mode),
            }
            data[key] = chat
            self._write(data)

    def disable_auto(self, chat_id: int) -> None:
        with self._lock:
            data = self._read()
            key = self._chat_key(chat_id)
            chat = dict(data.get(key) or {})
            state = dict(chat.get("auto") or {})
            state["enabled"] = False
            chat["auto"] = state
            data[key] = chat
            self._write(data)

    def mark_auto_job(self, chat_id: int, *, job_id: str, task_id: str) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            key = self._chat_key(chat_id)
            chat = dict(data.get(key) or {})
            state = dict(chat.get("auto") or {})
            state["last_job_id"] = job_id
            state["task_id"] = task_id or str(state.get("task_id") or "")
            state["turns_started"] = int(state.get("turns_started") or 0) + 1
            chat["auto"] = state
            data[key] = chat
            self._write(data)
            return state

    def set_recent(self, chat_id: int, *, task_ids: list[str], job_ids: list[str]) -> None:
        with self._lock:
            data = self._read()
            key = self._chat_key(chat_id)
            chat = dict(data.get(key) or {})
            chat["recent_task_ids"] = task_ids
            chat["recent_job_ids"] = job_ids
            data[key] = chat
            self._write(data)

    def set_recent_projects(self, chat_id: int, project_ids: list[str]) -> None:
        with self._lock:
            data = self._read()
            key = self._chat_key(chat_id)
            chat = dict(data.get(key) or {})
            chat["recent_project_ids"] = project_ids
            data[key] = chat
            self._write(data)

    def set_recent_agents(self, chat_id: int, agent_ids: list[str]) -> None:
        with self._lock:
            data = self._read()
            key = self._chat_key(chat_id)
            chat = dict(data.get(key) or {})
            chat["recent_agent_ids"] = agent_ids
            data[key] = chat
            self._write(data)

    def set_recent_sessions(self, chat_id: int, session_ids: list[str]) -> None:
        with self._lock:
            data = self._read()
            key = self._chat_key(chat_id)
            chat = dict(data.get(key) or {})
            chat["recent_session_ids"] = session_ids
            data[key] = chat
            self._write(data)

    def set_recent_provider_sessions(self, chat_id: int, sessions: list[ProviderSessionCandidate]) -> None:
        with self._lock:
            data = self._read()
            key = self._chat_key(chat_id)
            chat = dict(data.get(key) or {})
            chat["recent_provider_sessions"] = [session.to_dict() for session in sessions]
            data[key] = chat
            self._write(data)

    def set_recent_approvals(self, chat_id: int, approval_ids: list[str]) -> None:
        with self._lock:
            data = self._read()
            key = self._chat_key(chat_id)
            chat = dict(data.get(key) or {})
            chat["recent_approval_ids"] = approval_ids
            data[key] = chat
            self._write(data)

    def set_recent_memories(self, chat_id: int, memory_paths: list[str]) -> None:
        with self._lock:
            data = self._read()
            key = self._chat_key(chat_id)
            chat = dict(data.get(key) or {})
            chat["recent_memory_paths"] = memory_paths
            data[key] = chat
            self._write(data)

    def set_recent_tasks(self, chat_id: int, task_ids: list[str]) -> None:
        with self._lock:
            data = self._read()
            key = self._chat_key(chat_id)
            chat = dict(data.get(key) or {})
            chat["recent_task_ids"] = task_ids
            data[key] = chat
            self._write(data)

    def set_recent_jobs(self, chat_id: int, job_ids: list[str]) -> None:
        with self._lock:
            data = self._read()
            key = self._chat_key(chat_id)
            chat = dict(data.get(key) or {})
            chat["recent_job_ids"] = job_ids
            data[key] = chat
            self._write(data)

    def recent_task_id(self, chat_id: int, index: int) -> str:
        with self._lock:
            data = self._read()
            task_ids = data.get(self._chat_key(chat_id), {}).get("recent_task_ids") or []
            if not isinstance(task_ids, list) or index < 1 or index > len(task_ids):
                return ""
            return str(task_ids[index - 1])

    def recent_project_id(self, chat_id: int, index: int) -> str:
        with self._lock:
            data = self._read()
            project_ids = data.get(self._chat_key(chat_id), {}).get("recent_project_ids") or []
            if not isinstance(project_ids, list) or index < 1 or index > len(project_ids):
                return ""
            return str(project_ids[index - 1])

    def recent_agent_id(self, chat_id: int, index: int) -> str:
        with self._lock:
            data = self._read()
            agent_ids = data.get(self._chat_key(chat_id), {}).get("recent_agent_ids") or []
            if not isinstance(agent_ids, list) or index < 1 or index > len(agent_ids):
                return ""
            return str(agent_ids[index - 1])

    def recent_job_id(self, chat_id: int, index: int) -> str:
        with self._lock:
            data = self._read()
            job_ids = data.get(self._chat_key(chat_id), {}).get("recent_job_ids") or []
            if not isinstance(job_ids, list) or index < 1 or index > len(job_ids):
                return ""
            return str(job_ids[index - 1])

    def recent_session_id(self, chat_id: int, index: int) -> str:
        with self._lock:
            data = self._read()
            session_ids = data.get(self._chat_key(chat_id), {}).get("recent_session_ids") or []
            if not isinstance(session_ids, list) or index < 1 or index > len(session_ids):
                return ""
            return str(session_ids[index - 1])

    def recent_provider_session(self, chat_id: int, index: int) -> ProviderSessionCandidate | None:
        with self._lock:
            data = self._read()
            sessions = data.get(self._chat_key(chat_id), {}).get("recent_provider_sessions") or []
            if not isinstance(sessions, list) or index < 1 or index > len(sessions):
                return None
            item = sessions[index - 1]
            if not isinstance(item, dict):
                return None
            try:
                return ProviderSessionCandidate(
                    provider=str(item["provider"]),
                    adapter=str(item["adapter"]),
                    provider_session_id=str(item["provider_session_id"]),
                    provider_session_kind=str(item["provider_session_kind"]),
                    project_dir=str(item["project_dir"]),
                    title=str(item.get("title") or ""),
                    updated_at=float(item.get("updated_at") or 0.0),
                    source_path=str(item.get("source_path") or ""),
                    metadata=dict(item.get("metadata") or {}),
                )
            except (KeyError, TypeError, ValueError):
                return None

    def recent_approval_id(self, chat_id: int, index: int) -> str:
        with self._lock:
            data = self._read()
            approval_ids = data.get(self._chat_key(chat_id), {}).get("recent_approval_ids") or []
            if not isinstance(approval_ids, list) or index < 1 or index > len(approval_ids):
                return ""
            return str(approval_ids[index - 1])

    def recent_memory_path(self, chat_id: int, index: int) -> str:
        with self._lock:
            data = self._read()
            memory_paths = data.get(self._chat_key(chat_id), {}).get("recent_memory_paths") or []
            if not isinstance(memory_paths, list) or index < 1 or index > len(memory_paths):
                return ""
            return str(memory_paths[index - 1])

    def _chat_key(self, chat_id: int) -> str:
        if not self.scope:
            return str(chat_id)
        return f"{self.scope}:{chat_id}"

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        chats = data.get("chats", data)
        if not isinstance(chats, dict):
            return {}
        return {str(key): dict(value) for key, value in chats.items() if isinstance(value, dict)}

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "chats": data}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


class TelegramCommandAuditLog:
    """Append-only audit log for received Telegram commands."""

    def __init__(self, workspace: Workspace, *, bot_id: str = "") -> None:
        self.workspace = workspace
        self.bot_id = bot_id.strip()
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self.workspace.root / "telegram" / "commands.jsonl"

    def append(
        self,
        *,
        chat_id: int,
        text: str,
        outcome: str,
        detail: str = "",
        reply_count: int = 0,
    ) -> None:
        clean = text.strip()
        command, _ = _split_command(clean)
        record = {
            "created_at": time.time(),
            "bot_id": self.bot_id,
            "chat_id": chat_id,
            "command": command or "text",
            "text_preview": _one_line(clean, 180),
            "outcome": outcome,
            "detail": _one_line(detail, 240),
            "reply_count": reply_count,
        }
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError:
            pass


class TelegramRestartNoticeStore:
    """Persist restart completion notices that the new daemon should send."""

    _LOCK = threading.Lock()

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    @property
    def path(self) -> Path:
        return self.workspace.root / "telegram" / "restart-pending.json"

    def add(self, *, bot_id: str, chat_id: int) -> None:
        record = {
            "bot_id": bot_id.strip(),
            "chat_id": chat_id,
            "created_at": time.time(),
        }
        with self._LOCK:
            records = self._read()
            records.append(record)
            self._write(records)

    def pop_for_bot(self, bot_id: str) -> list[dict[str, Any]]:
        clean_bot_id = bot_id.strip()
        with self._LOCK:
            records = self._read()
            matched = [record for record in records if str(record.get("bot_id") or "") == clean_bot_id]
            remaining = [record for record in records if str(record.get("bot_id") or "") != clean_bot_id]
            self._write(remaining)
        return matched

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        records = data.get("notices", data) if isinstance(data, dict) else data
        if not isinstance(records, list):
            return []
        return [dict(record) for record in records if isinstance(record, dict)]

    def _write(self, records: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not records:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            return
        payload = {"version": 1, "notices": records}
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


class TelegramUpdateOffsetStore:
    """Persist Telegram getUpdates offsets per bot."""

    _LOCK = threading.Lock()

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    @property
    def path(self) -> Path:
        return self.workspace.root / "telegram" / "offsets.json"

    def get(self, bot_id: str) -> int | None:
        with self._LOCK:
            value = self._read().get(bot_id.strip())
        return int(value) if isinstance(value, int) and value > 0 else None

    def set(self, bot_id: str, offset: int) -> None:
        if offset <= 0:
            return
        with self._LOCK:
            data = self._read()
            data[bot_id.strip()] = int(offset)
            self._write(data)

    def _read(self) -> dict[str, int]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        offsets = data.get("offsets", data) if isinstance(data, dict) else {}
        if not isinstance(offsets, dict):
            return {}
        clean: dict[str, int] = {}
        for key, value in offsets.items():
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                clean[str(key)] = parsed
        return clean

    def _write(self, offsets: dict[str, int]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "offsets": offsets}
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


class TelegramJobQueue:
    """Background runner for Telegram-triggered jobs."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        sender: Callable[[int, str], None],
        runner: Callable[[Workspace, RunRequest], Any] = run_agent_prompt,
        state_scope: str = "",
    ) -> None:
        self.workspace = workspace
        self.sender = sender
        self.runner = runner
        self.registry = JobRegistry(workspace)
        self.state_scope = state_scope.strip()
        self.chat_state = TelegramChatStateStore(workspace, scope=state_scope)
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._cancellations: dict[str, CancellationToken] = {}
        self._assistant_action_handler: Callable[[int, str], list[str]] | None = None
        self.registry.mark_unfinished_interrupted(
            interface="telegram",
            reason="AgentDeck restarted before this job finished.",
        )

    def set_assistant_action_handler(self, handler: Callable[[int, str], list[str]] | None) -> None:
        self._assistant_action_handler = handler

    def start(
        self,
        *,
        chat_id: int,
        task_id: str = "",
        prompt: str,
        metadata: dict[str, Any] | None = None,
    ) -> JobRecord:
        with self._lock:
            clean_metadata = dict(metadata or {})
            if self.state_scope:
                clean_metadata.setdefault("bot_id", self.state_scope)
            job = self.registry.create(
                interface="telegram",
                chat_id=chat_id,
                task_id=task_id,
                prompt=prompt,
                metadata=clean_metadata,
            )
            self._cancellations[job.job_id] = CancellationToken()
            thread = threading.Thread(target=self._run_job, args=(job.job_id,), daemon=True)
            self._threads[job.job_id] = thread
        thread.start()
        return job

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            return self.registry.get(job_id)

    def list(self, *, chat_id: int | None = None, limit: int = 10) -> list[JobRecord]:
        with self._lock:
            records = self.registry.list(interface="telegram", chat_id=chat_id)
        records = [record for record in records if self._record_matches_scope(record)]
        return records[:limit]

    def latest_for_chat(self, chat_id: int, *, statuses: set[str] | None = None) -> JobRecord | None:
        with self._lock:
            records = self.registry.list(interface="telegram", chat_id=chat_id)
        records = [record for record in records if self._record_matches_scope(record)]
        if statuses:
            records = [record for record in records if record.status in statuses]
        return records[0] if records else None

    def _record_matches_scope(self, record: JobRecord) -> bool:
        if not self.state_scope:
            return True
        return str((record.metadata or {}).get("bot_id") or "") == self.state_scope

    def wait(self, job_id: str, *, timeout: float | None = None) -> JobRecord | None:
        with self._lock:
            thread = self._threads.get(job_id)
        if thread is not None:
            thread.join(timeout)
        return self.get(job_id)

    def cancel(self, job_id: str) -> JobRecord | None:
        with self._lock:
            record = self.registry.cancel(
                job_id,
                reason="Cancellation requested from Telegram.",
            )
            token = self._cancellations.get(job_id)
            if token is not None:
                token.cancel("Cancellation requested from Telegram.")
            return record

    def _run_job(self, job_id: str) -> None:
        job = self._start_job(job_id)
        if job is None:
            return
        cancellation = self._get_cancellation(job_id)
        try:
            metadata = dict(job.metadata or {})
            result = asyncio.run(
                self.runner(
                    self.workspace,
                    RunRequest(
                        prompt=job.prompt,
                        task=job.task_id or None,
                        agent=str(metadata.get("agent_id") or "") or None,
                        session=str(metadata.get("session_id") or "") or None,
                        approval_mode=str(metadata.get("approval_mode") or "") or None,
                        cancellation=cancellation,
                    ),
                )
            )
        except RunConfigurationError as exc:
            finished = self._finish_job(job_id, status="error", error=str(exc))
            self._send(job.chat_id, f"Job failed: {job_id}\n{exc}")
            self._stop_auto_after_failure(finished or job, str(exc))
        except Exception as exc:  # keep the polling loop alive even if a background job crashes
            finished = self._finish_job(job_id, status="error", error=str(exc))
            self._send(job.chat_id, f"Job failed: {job_id}\n{exc}")
            self._stop_auto_after_failure(finished or job, str(exc))
        else:
            cancel_event = next((event for event in result.events if event.kind == EventKind.CANCELLED), None)
            error_event = first_error_event(result.events)
            cancel_was_requested = self._cancel_was_requested(job_id)
            if cancel_event is not None:
                finished = self._finish_job(
                    job_id,
                    status="cancelled",
                    session_id=result.session_id,
                    final_text=result.final_text,
                    error=cancel_event.text or "Run cancelled.",
                )
            elif event_should_fail_job(error_event):
                current = self.registry.get(job_id) or job
                assert error_event is not None
                incident = create_error_incident_for_job(
                    self.workspace,
                    job=current,
                    event=error_event,
                    adapter=result.adapter,
                )
                finished = self._finish_job(
                    job_id,
                    status="error",
                    session_id=result.session_id,
                    final_text=result.final_text,
                    error=error_event.text or "Backend adapter error.",
                )
                daemon = ErrorHandlingDaemon(
                    self.workspace,
                    notifier=lambda incident_arg, decision: self._send(
                        job.chat_id,
                        format_error_decision_message(incident_arg, decision),
                    ),
                )
                daemon.process_incident(incident)
                finished = self.registry.get(job_id) or finished
            else:
                if error_event is not None:
                    current = self.registry.get(job_id) or job
                    incident = create_error_incident_for_job(
                        self.workspace,
                        job=current,
                        event=error_event,
                        adapter=result.adapter,
                    )
                    ErrorHandlingDaemon(self.workspace).process_incident(incident)
                finished = self._finish_job(
                    job_id,
                    status="done",
                    session_id=result.session_id,
                    final_text=result.final_text,
                    error=(
                        "Cancellation was requested, but adapter-level process termination is not implemented yet."
                        if cancel_was_requested
                        else ""
                    ),
                )
            finished_job = finished or job
            if result.session_id and not _is_assistant_job(finished_job):
                self.chat_state.set_current_session(job.chat_id, result.session_id)
            self._send(job.chat_id, _format_job_completion(finished_job, result.approval_requested))
            if _is_assistant_job(finished_job):
                for reply in self._assistant_action_replies(finished_job.chat_id, finished_job.final_text):
                    self._send(finished_job.chat_id, reply)
            self._continue_auto_if_needed(finished or job, result.approval_requested)
        finally:
            with self._lock:
                self._cancellations.pop(job_id, None)

    def _start_job(self, job_id: str) -> JobRecord | None:
        with self._lock:
            current = self.registry.get(job_id)
            if current is None:
                return None
            if current.status == "cancelled":
                return None
            if current.status == "cancel_requested":
                self.registry.finish(
                    job_id,
                    status="cancelled",
                    error=current.error or "Cancelled before this job started running.",
                )
                return None
            return self.registry.set_status(job_id, "running")

    def _finish_job(
        self,
        job_id: str,
        *,
        status: str,
        session_id: str = "",
        final_text: str = "",
        error: str = "",
    ) -> JobRecord | None:
        with self._lock:
            return self.registry.finish(
                job_id,
                status=status,
                session_id=session_id,
                final_text=final_text,
                error=error,
            )

    def _cancel_was_requested(self, job_id: str) -> bool:
        with self._lock:
            current = self.registry.get(job_id)
            return current is not None and current.status == "cancel_requested"

    def _get_cancellation(self, job_id: str) -> CancellationToken | None:
        with self._lock:
            return self._cancellations.get(job_id)

    def _send(self, chat_id: int, text: str) -> None:
        try:
            self.sender(chat_id, text)
        except Exception:
            pass

    def _assistant_action_replies(self, chat_id: int, final_text: str) -> list[str]:
        if self._assistant_action_handler is None or not chat_id:
            return []
        try:
            replies = self._assistant_action_handler(chat_id, final_text)
        except Exception as exc:
            return [f"Assistant action handling failed: {_one_line(str(exc), 180)}"]
        if replies:
            return replies
        warning = _assistant_unverified_state_change_warning(final_text)
        return [warning] if warning else []

    def _continue_auto_if_needed(self, job: JobRecord, approval_requested: bool) -> None:
        if not job.chat_id or not job.task_id:
            return
        state = self.chat_state.auto_state(job.chat_id)
        if not _auto_enabled_for_job(state, job):
            return
        if approval_requested:
            self._send(
                job.chat_id,
                "Auto mode paused: approval is required. Use /approvals, then /approve <list #> or /reject <list #>.",
            )
            return
        if job.status != "done":
            self.chat_state.disable_auto(job.chat_id)
            self._send(job.chat_id, f"Auto mode stopped: job ended with status {job.status}.")
            return
        if job.error:
            self.chat_state.disable_auto(job.chat_id)
            self._send(job.chat_id, f"Auto mode stopped: {_one_line(job.error, 180)}")
            return
        if _auto_mode(state) == "task" and _auto_task_completed(job.final_text):
            self.chat_state.disable_auto(job.chat_id)
            TaskBoard(self.workspace).set_status(
                job.task_id,
                "review",
                note="Auto by task stopped after the agent reported task completion.",
            )
            self._send(
                job.chat_id,
                "\n".join(
                    [
                        "Auto by task stopped: task judged complete.",
                        "status: review",
                        "Use /tasks or /context to inspect before marking done.",
                    ]
                ),
            )
            return
        if _auto_expired(state):
            self.chat_state.disable_auto(job.chat_id)
            self._send(job.chat_id, "Auto mode ended: timer expired.")
            return
        prompt = str(state.get("prompt") or DEFAULT_AUTO_PROMPT).strip() or DEFAULT_AUTO_PROMPT
        metadata: dict[str, Any] = {
            "auto": True,
            "auto_parent_job_id": job.job_id,
            "approval_mode": _auto_approval_mode(state),
            "auto_mode": _auto_mode(state),
        }
        session_id = _safe_session_id_for_resume(self.workspace, job.session_id)
        if session_id:
            metadata["session_id"] = session_id

        def enqueue_next() -> None:
            current = self.chat_state.auto_state(job.chat_id)
            if not _auto_enabled_for_job(current, job) or _auto_expired(current):
                if _auto_expired(current):
                    self.chat_state.disable_auto(job.chat_id)
                    self._send(job.chat_id, "Auto mode ended: timer expired.")
                return
            next_job = self.start(chat_id=job.chat_id, task_id=job.task_id, prompt=prompt, metadata=metadata)
            self.chat_state.mark_auto_job(job.chat_id, job_id=next_job.job_id, task_id=job.task_id)
            self._send(
                job.chat_id,
                "\n".join(
                    [
                        f"Auto mode: next job started: {next_job.job_id}",
                        "Use /auto status or /auto end.",
                    ]
                ),
            )

        timer = threading.Timer(AUTO_CONTINUE_DELAY_SECONDS, enqueue_next)
        timer.daemon = True
        timer.start()

    def _stop_auto_after_failure(self, job: JobRecord, reason: str) -> None:
        if not job.chat_id or not job.task_id:
            return
        state = self.chat_state.auto_state(job.chat_id)
        if not _auto_enabled_for_job(state, job):
            return
        self.chat_state.disable_auto(job.chat_id)
        self._send(job.chat_id, f"Auto mode stopped: {_one_line(reason, 180)}")


class TelegramCommandHandler:
    """Parse Telegram messages into AgentDeck operations."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        runner: Callable[[Workspace, RunRequest], Any] = run_agent_prompt,
        job_queue: TelegramJobQueue | None = None,
        video_sender: Callable[[int, Path, str], None] | None = None,
        assistant_agent_id: str = ASSISTANT_AGENT_ID,
        bot_id: str = "",
        restart_callback: Callable[[], bool | None] | None = None,
    ) -> None:
        self.workspace = workspace
        self.runner = runner
        self.job_queue = job_queue
        self.video_sender = video_sender
        self.bot_id = bot_id.strip()
        self.assistant_agent_id = assistant_agent_id or ASSISTANT_AGENT_ID
        self.restart_callback = restart_callback
        self.chat_state = TelegramChatStateStore(workspace, scope=self.bot_id)
        if self.job_queue is not None:
            self.job_queue.set_assistant_action_handler(
                lambda chat_id, final_text: asyncio.run(self._execute_assistant_actions(chat_id, final_text))
            )

    async def handle_text(self, text: str, *, chat_id: int | None = None) -> list[str]:
        clean = text.strip()
        if not clean:
            return []
        command, rest = _split_command(clean)

        restart_intent = _natural_restart_intent(clean)
        if restart_intent is not None and not command.startswith("/"):
            return [self._restart(restart_intent, chat_id=chat_id)]

        if rest and not command.startswith("/") and self._should_route_bare_text_to_assistant(chat_id):
            return [await self._plain_text(clean, chat_id=chat_id)]

        if command in {"/start", "/help", "help"}:
            return [_help_text()]
        if command in {"/projects", "projects"}:
            return [self._projects(chat_id=chat_id)]
        if command in {"/project", "project"}:
            return [self._project(rest, chat_id=chat_id)]
        if command in {"/projectstate", "projectstate"}:
            return [self._project_state(rest, chat_id=chat_id)]
        if command in {"/decisions", "decisions"}:
            return [self._project_decisions(rest, chat_id=chat_id)]
        if command in {"/decide", "decide"}:
            return [self._decide(rest, chat_id=chat_id)]
        if command in {"/tasks", "tasks"}:
            return [self._tasks(rest, chat_id=chat_id)]
        if command in {"/task", "task"}:
            subcommand, subrest = _split_once(rest)
            if subcommand.lower() == "new":
                return [self._newtask(subrest, chat_id=chat_id)]
            return [self._task(rest, chat_id=chat_id)]
        if command in {"/newtask", "newtask"}:
            return [self._newtask(rest, chat_id=chat_id)]
        if command in {"/use", "use"}:
            return [self._use(rest, chat_id=chat_id)]
        if command in {"/assistant", "assistant", "/home", "home"}:
            return [self._assistant_mode(chat_id=chat_id)]
        if command in {"/current", "current"}:
            return [self._current(chat_id=chat_id)]
        if command in {"/status", "status"}:
            return [self._status(chat_id=chat_id)]
        if command in {"/restart", "restart", "/reload", "reload"}:
            return [self._restart(rest, chat_id=chat_id)]
        if command in {"/video", "video"}:
            return [self._video(rest, chat_id=chat_id)]
        if command in {"/send", "send"}:
            send_kind, send_tail = _split_once(rest)
            if send_kind.lower() == "video":
                return [self._video(send_tail, chat_id=chat_id)]
            return ["Usage: /send video <path> [caption]"]
        if command in {"/list", "list", "/recent", "recent"}:
            return [self._recent(chat_id=chat_id)]
        if command in {"/context", "context"}:
            return [self._context(rest, chat_id=chat_id)]
        if command in {"/memories", "memories"}:
            return [self._memories(rest, chat_id=chat_id)]
        if command in {"/memory", "memory"}:
            subcommand, subrest = _split_once(rest)
            if subcommand.lower() in {"disable", "off", "forget"}:
                return [self._set_memory_disabled(subrest, disabled=True, chat_id=chat_id)]
            if subcommand.lower() in {"enable", "on"}:
                return [self._set_memory_disabled(subrest, disabled=False, chat_id=chat_id)]
            return [self._memories(rest, chat_id=chat_id)]
        if command in {"/forget", "forget"}:
            return [self._set_memory_disabled(rest, disabled=True, chat_id=chat_id)]
        if command in {"/compact", "compact"}:
            return [self._compact(rest, chat_id=chat_id)]
        if command in {"/handoffs", "handoffs"}:
            return [self._handoffs(rest, chat_id=chat_id)]
        if command in {"/review", "review"}:
            return [self._review(rest, chat_id=chat_id)]
        if command in {"/reviews", "reviews"}:
            return [self._reviews(rest, chat_id=chat_id)]
        if command in {"/sessions", "sessions"}:
            return [self._sessions(rest, chat_id=chat_id)]
        if command in {"/session", "session"}:
            return [self._session(rest, chat_id=chat_id)]
        if command in {"/resume", "resume"}:
            resume_kind, resume_tail = _split_once(rest)
            if resume_kind.lower() == "job":
                return [await self._resume_job(resume_tail, chat_id=chat_id)]
            return [await self._resume(rest, chat_id=chat_id)]
        if command in {"/auto", "auto"}:
            return [self._auto(rest, chat_id=chat_id)]
        if command in {"/ta", "ta"}:
            subcommand, subrest = _split_once(rest)
            if subcommand.lower().split("@", 1)[0] == "auto":
                return [self._auto(subrest, chat_id=chat_id)]
        if command in {"/agents", "agents"}:
            return [self._agents(rest, chat_id=chat_id)]
        if command in {"/agent", "agent"}:
            return [self._agent(rest, chat_id=chat_id)]
        if command in {"/approvals", "approvals"}:
            return [self._approvals(rest, chat_id=chat_id)]
        if command in {"/approval", "approval"}:
            return [self._approval(rest, chat_id=chat_id)]
        if command in {"/approve", "approve"}:
            return [self._resolve_approval(rest, "approved", chat_id=chat_id)]
        if command in {"/reject", "reject"}:
            return [self._resolve_approval(rest, "rejected", chat_id=chat_id)]
        if command in {"/jobs", "jobs"}:
            return [self._jobs(chat_id=chat_id)]
        if command in {"/job", "job"}:
            job_kind, job_tail = _split_once(rest)
            if job_kind.lower() in {"resume", "rerun", "continue"}:
                return [await self._resume_job(job_tail, chat_id=chat_id)]
            return [self._job(rest, chat_id=chat_id)]
        if command in {"/cancel", "cancel"}:
            return [self._cancel(rest, chat_id=chat_id)]
        if command in {"/run", "run"}:
            return [await self._run(rest, chat_id=chat_id)]
        if not command.startswith("/"):
            return [await self._plain_text(clean, chat_id=chat_id)]
        return [f"Unknown command: {command}\n\n{_help_text()}"]

    def _should_route_bare_text_to_assistant(self, chat_id: int | None) -> bool:
        if chat_id is None:
            return False
        current_task_id = self.chat_state.current_task(chat_id)
        if current_task_id and self._resolve_task(current_task_id) is not None:
            return False
        current_session_id = self.chat_state.current_session(chat_id)
        if current_session_id and SessionRegistry(self.workspace).resolve(current_session_id) is not None:
            return False
        return self._assistant_agent() is not None

    async def _plain_text(self, text: str, *, chat_id: int | None) -> str:
        if chat_id is None:
            return "Plain text messages require a Telegram chat. Use /run <message>."
        current_task_id = self.chat_state.current_task(chat_id)
        task = self._resolve_task(current_task_id) if current_task_id else None
        if task is None:
            current_session_id = self.chat_state.current_session(chat_id)
            session = SessionRegistry(self.workspace).resolve(current_session_id) if current_session_id else None
            if session is not None:
                return await self._run_session_prompt(session, text, chat_id=chat_id)
            assistant = self._assistant_agent()
            if assistant is not None:
                return await self._run_assistant(text, assistant=assistant, chat_id=chat_id)
            return self._plain_text_setup_hint()
        return await self._run(text, chat_id=chat_id)

    def _assistant_agent(self) -> AgentRecord | None:
        registry = AgentRegistry(self.workspace)
        if self.assistant_agent_id:
            assistant = registry.resolve(self.assistant_agent_id)
            if assistant is not None:
                return assistant
        if self.assistant_agent_id != ASSISTANT_AGENT_ID:
            return registry.resolve(ASSISTANT_AGENT_ID)
        return None

    def _assistant_mode(self, *, chat_id: int | None) -> str:
        if chat_id is None:
            return "This command requires a Telegram chat."
        self.chat_state.set_current_task(chat_id, "")
        self.chat_state.set_current_session(chat_id, "")
        assistant = self._assistant_agent()
        lines = [
            "Assistant mode enabled.",
            "Current task/session cleared. Plain text messages will go to the assistant until you /use task <ref> or /use session <ref>.",
        ]
        if assistant is not None:
            lines.append(f"assistant: {assistant.title} ({assistant.agent_id})")
        else:
            lines.append("assistant: not configured")
            lines.append("Use CLI: agentdeck assistant setup --adapter <echo|codex|kimi>")
        return "\n".join(lines)

    async def _run_assistant(self, prompt: str, *, assistant: AgentRecord, chat_id: int) -> str:
        metadata = {"assistant": True, "agent_id": assistant.agent_id}
        if self.job_queue is not None:
            job = self.job_queue.start(chat_id=chat_id, task_id="", prompt=prompt, metadata=metadata)
            return "\n".join(
                [
                    f"Assistant job started: {job.job_id}",
                    f"agent: {assistant.title}",
                    "status: queued",
                    "Use /job to view the latest job",
                ]
            )
        try:
            result = await self.runner(self.workspace, RunRequest(prompt=prompt, agent=assistant.agent_id))
        except RunConfigurationError as exc:
            return str(exc)
        display_text = _strip_assistant_actions(result.final_text)
        lines = [display_text or "Assistant run finished without a final text response.", ""]
        action_replies = await self._execute_assistant_actions(chat_id, result.final_text)
        lines.extend(action_replies)
        if action_replies:
            lines.append("")
        lines.append(f"session: {result.session_id}")
        return "\n".join(lines)

    async def _execute_assistant_actions(self, chat_id: int, final_text: str) -> list[str]:
        actions = _assistant_actions_from_text(final_text)
        if not actions:
            return []
        replies: list[str] = []
        for action in actions:
            allowed, reason = _assistant_action_allowed(action)
            if not allowed:
                replies.append(
                    "\n".join(
                        [
                            f"Assistant action blocked: {action}",
                            f"reason: {reason}",
                        ]
                    )
                )
                continue
            command_replies = await self.handle_text(action, chat_id=chat_id)
            reply_text = "\n\n".join(command_replies).strip() or "No reply."
            replies.append(
                "\n".join(
                    [
                        f"Assistant action executed: {action}",
                        reply_text,
                    ]
                )
            )
        return replies

    def _plain_text_setup_hint(self) -> str:
        return "\n".join(
            [
                "No current task is selected, so this message was not sent to an agent.",
                "Use /tasks and /use task <ref>, or create one with /task new <title>.",
                "Or configure a default assistant with: agentdeck assistant setup --adapter <echo|codex|kimi>",
            ]
        )

    def _restart(self, rest: str = "", *, chat_id: int | None = None) -> str:
        if self.restart_callback is None:
            return "\n".join(
                [
                    "Restart is not available in this foreground handler.",
                    "If AgentDeck is running as a daemon, use CLI: agentdeck telegram restart",
                ]
            )
        mode = rest.strip().lower()
        force = mode in {"force", "--force", "now"}
        if mode and not force:
            return "Usage: /restart or /restart force"
        active_jobs = self._active_restart_jobs()
        if active_jobs and not force:
            lines = ["Restart not started: active Telegram jobs exist."]
            for job in active_jobs[:5]:
                task = f" task: {job.task_id}" if job.task_id else ""
                lines.append(f"- {job.job_id}  status: {job.status}{task}")
            if len(active_jobs) > 5:
                lines.append(f"- ... {len(active_jobs) - 5} more")
            lines.append("Wait for them to finish, use /cancel <job>, or send /restart force to interrupt them.")
            return "\n".join(lines)
        scheduled = self.restart_callback()
        if scheduled is False:
            return "AgentDeck restart is already scheduled."
        if chat_id is not None:
            TelegramRestartNoticeStore(self.workspace).add(bot_id=self.bot_id, chat_id=chat_id)
        lines = [
            "AgentDeck restart requested.",
            "The Telegram service will reload shortly and keep the same workspace.",
            "A completion notice will be sent after the new daemon starts.",
        ]
        if active_jobs:
            lines.append("Forced restart: active jobs will be interrupted and marked interrupted.")
        return "\n".join(lines)

    def _active_restart_jobs(self) -> list[JobRecord]:
        registry = self.job_queue.registry if self.job_queue is not None else JobRegistry(self.workspace)
        records: list[JobRecord] = []
        for status in ACTIVE_JOB_STATUSES:
            records.extend(registry.list(interface="telegram", status=status))
        return sorted(records, key=lambda item: item.updated_at, reverse=True)

    def _video(self, rest: str, *, chat_id: int | None) -> str:
        if chat_id is None:
            return "This command requires a Telegram chat."
        if self.video_sender is None:
            return "Video sending is not available in this Telegram handler."
        try:
            parts = shlex.split(rest.strip())
        except ValueError as exc:
            return f"Usage: /video <path> [caption]\nCould not parse path: {exc}"
        if not parts:
            return "Usage: /video <path> [caption]"
        path, error = self._resolve_media_path(parts[0], chat_id=chat_id)
        if error:
            return error
        assert path is not None
        if not _looks_like_video(path):
            return f"Not a recognized video file: {path.name}"
        caption = " ".join(parts[1:]).strip()
        try:
            self.video_sender(chat_id, path, caption)
        except Exception as exc:
            return f"Video send failed: {exc}"
        lines = [
            "Video sent.",
            f"file: {path.name}",
        ]
        if caption:
            lines.append(f"caption: {_one_line(caption, 220)}")
        return "\n".join(lines)

    def _resolve_media_path(self, value: str, *, chat_id: int | None) -> tuple[Path | None, str]:
        raw = value.strip()
        if not raw:
            return None, "Usage: /video <path> [caption]"
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            project = self._default_project(chat_id=chat_id)
            base = Path(project.project_dir).expanduser() if project is not None else Path.cwd()
            candidate = base / candidate
        resolved = candidate.resolve()
        if not resolved.exists():
            return None, f"File not found: {resolved}"
        if not resolved.is_file():
            return None, f"Not a file: {resolved}"
        return resolved, ""

    def _projects(self, *, chat_id: int | None = None) -> str:
        records = ProjectRegistry(self.workspace).list()
        if not records:
            return "No projects."
        if chat_id is not None:
            self.chat_state.set_recent_projects(chat_id, [record.project_id for record in records])
        current_project_id = self.chat_state.current_project(chat_id) if chat_id is not None else ""
        lines = ["Projects:"]
        for index, record in enumerate(records, 1):
            marker = " [current]" if record.project_id == current_project_id else ""
            lines.append(f"{index}. {record.title}{marker}")
            lines.append(f"   id: {record.project_id}")
            lines.append(f"   agent: {record.default_agent_id}  status: {record.status}")
        lines.append("")
        lines.append("Use /use project <list #> or /project new <id> <cwd> [title].")
        return "\n".join(lines)

    def _project(self, rest: str, *, chat_id: int | None) -> str:
        command, tail = _split_once(rest.strip())
        lowered = command.lower()
        if lowered in {"new", "create"}:
            return self._new_project(tail, chat_id=chat_id)
        if lowered in {"use", "select"}:
            if chat_id is None:
                return "This command requires a Telegram chat."
            return self._use_project(tail, chat_id=chat_id)
        project_ref = rest.strip()
        if not project_ref:
            if chat_id is not None:
                current = self.chat_state.current_project(chat_id)
                if current:
                    project_ref = current
            if not project_ref:
                return "Usage: /project <project_id or list #>, /project use <list #>, or /project new <id> <cwd> [title]"
        project = self._resolve_project(project_ref, chat_id=chat_id)
        if project is None:
            return f"Project not found: {project_ref}"
        lines = [
            project.title,
            f"id: {project.project_id}",
            f"cwd: {project.project_dir}",
            f"team: {project.team_id}",
            f"default agent: {project.default_agent_id}",
            f"status: {project.status}",
        ]
        lines.append("")
        lines.append("Use /project use <id or list #>, /agents, /tasks, or /task new <title>.")
        return "\n".join(lines)

    def _new_project(self, rest: str, *, chat_id: int | None) -> str:
        project_id, tail = _split_once(rest.strip())
        project_dir, title = _split_once(tail)
        if not project_id or not project_dir:
            return "Usage: /project new <project_id> <cwd> [title]"
        try:
            project = ProjectRegistry(self.workspace).upsert(
                project_id=project_id,
                title=title or None,
                project_dir=project_dir,
                team_id=project_id,
                default_agent_id="owner",
                replace=False,
            )
        except ValueError as exc:
            return str(exc)
        if chat_id is not None:
            self.chat_state.set_current_project(chat_id, project.project_id)
            self.chat_state.set_current_agent(chat_id, project.default_agent_id)
        return "\n".join(
            [
                "Project created and selected:",
                project.title,
                f"id: {project.project_id}",
                f"cwd: {project.project_dir}",
                f"default agent: {project.default_agent_id}",
                "Next: /agent new owner codex owner, or /task new <title>",
            ]
        )

    def _project_state(self, rest: str, *, chat_id: int | None) -> str:
        project, error = self._resolve_project_or_current(rest, chat_id=chat_id, usage="Usage: /projectstate [project]")
        if error:
            return error
        assert project is not None
        state = ProjectStateStore(self.workspace).get(project.project_id)
        if state is None:
            return f"No project state for: {project.title}\nUse CLI: agentdeck projects update-state {project.project_id} ..."
        lines = [f"Project state: {project.title}"]
        if state.goal:
            lines.append(f"goal: {_one_line(state.goal, 240)}")
        if state.phase:
            lines.append(f"phase: {_one_line(state.phase, 120)}")
        if state.current_focus:
            lines.append(f"focus: {_one_line(state.current_focus, 220)}")
        _append_compact_list(lines, "next", state.next_steps, max_items=4)
        _append_compact_list(lines, "constraints", state.constraints, max_items=4)
        _append_compact_list(lines, "blockers", state.blockers, max_items=3)
        _append_compact_list(lines, "artifacts", state.active_artifacts, max_items=4)
        lines.append("")
        lines.append("Use /decisions to see project decisions.")
        return "\n".join(lines)

    def _project_decisions(self, rest: str, *, chat_id: int | None) -> str:
        project, error = self._resolve_project_or_current(rest, chat_id=chat_id, usage="Usage: /decisions [project]")
        if error:
            return error
        assert project is not None
        decisions = ProjectStateStore(self.workspace).decisions(project.project_id, limit=5)
        if not decisions:
            return f"No decisions for project: {project.title}"
        lines = [f"Decisions: {project.title}"]
        for index, decision in enumerate(decisions, 1):
            lines.append(f"{index}. {_one_line(decision.decision, 220)}")
            if decision.reason:
                lines.append(f"   reason: {_one_line(decision.reason, 220)}")
            if decision.impact:
                lines.append(f"   impact: {_one_line(decision.impact, 220)}")
        return "\n".join(lines)

    def _decide(self, rest: str, *, chat_id: int | None) -> str:
        decision_text = rest.strip()
        if not decision_text:
            return "Usage: /decide <decision text>"
        project, error = self._resolve_project_or_current("", chat_id=chat_id, usage="Usage: /decide <decision text>")
        if error:
            return error
        assert project is not None
        try:
            decision = ProjectStateStore(self.workspace).add_decision(
                project.project_id,
                decision_text,
                made_by="telegram",
            )
        except ValueError as exc:
            return str(exc)
        return "\n".join(
            [
                "Decision recorded:",
                _one_line(decision.decision, 320),
                f"project: {project.title}",
                "Use /decisions to review.",
            ]
        )

    def _tasks(self, rest: str, *, chat_id: int | None) -> str:
        project_ref = rest.strip()
        project: ProjectRecord | None = None
        if project_ref:
            project = self._resolve_project(project_ref, chat_id=chat_id)
            if project is None:
                return f"Project not found: {project_ref}"
        elif chat_id is not None:
            project_id = self.chat_state.current_project(chat_id)
            project = self._resolve_project(project_id) if project_id else None
        records = TaskBoard(self.workspace).list(project_id=project.project_id if project is not None else None)
        if not records:
            return "No tasks."
        if chat_id is not None:
            self.chat_state.set_recent_tasks(chat_id, [record.task_id for record in records])
        heading = f"Tasks ({project.title}):" if project is not None else "Tasks:"
        current_task_id = self.chat_state.current_task(chat_id) if chat_id is not None else ""
        lines = [heading]
        for index, record in enumerate(records, 1):
            marker = " [current]" if record.task_id == current_task_id else ""
            lines.append(f"{index}. {record.title}{marker}")
            lines.append(f"   id: {record.task_id}")
            lines.append(f"   status: {record.status}  priority: {record.priority}")
            lines.append(f"   project: {record.project_id or '-'}  agent: {record.agent_id}")
        lines.append("")
        lines.append("Use /use task <list #>, /use <list #>, or /task new <title>.")
        return "\n".join(lines)

    def _task(self, rest: str, *, chat_id: int | None = None) -> str:
        task_id = rest.strip()
        if not task_id:
            return "Usage: /task <task_id>"
        record = self._resolve_task(task_id, chat_id=chat_id)
        if record is None:
            return f"Task not found: {task_id}"
        lines = [
            record.title,
            f"id: {record.task_id}",
            f"status: {record.status}",
            f"priority: {record.priority}",
            f"project: {record.project_id or '-'}",
            f"agent: {record.agent_id}",
            f"session: {record.session_id or '-'}",
        ]
        if record.description:
            lines.append(f"description: {record.description}")
        if record.notes:
            lines.append("latest notes:")
            for note in record.notes[-3:]:
                lines.append(f"- {note.get('text', '')}")
        return "\n".join(lines)

    def _newtask(self, rest: str, *, chat_id: int | None) -> str:
        title = rest.strip()
        if not title:
            return "Usage: /newtask <task title> or /task new <task title>"
        project = self._default_project(chat_id=chat_id)
        agent = self._default_agent(chat_id=chat_id, project=project)
        record = TaskBoard(self.workspace).create(
            title=title,
            project_id=project.project_id if project is not None else "",
            agent_id=agent.agent_id if agent is not None else (project.default_agent_id if project is not None else "owner"),
            team_id=project.team_id if project is not None else "default",
        )
        if chat_id is not None:
            self.chat_state.set_current_task(chat_id, record.task_id)
            self.chat_state.set_current_session(chat_id, "")
            if record.project_id:
                self.chat_state.set_current_project(chat_id, record.project_id)
            if record.agent_id:
                self.chat_state.set_current_agent(chat_id, record.agent_id)
        lines = [
            "Task created and selected:",
            record.title,
            f"id: {record.task_id}",
        ]
        if record.project_id:
            lines.append(f"project: {record.project_id}")
        lines.append(f"agent: {record.agent_id}")
        lines.append("Now you can send a plain message, or /run <message>.")
        return "\n".join(lines)

    def _use(self, rest: str, *, chat_id: int | None) -> str:
        if chat_id is None:
            return "This command requires a Telegram chat."
        kind, value = _split_once(rest.strip())
        lowered_kind = kind.lower()
        if lowered_kind in {"project", "proj"}:
            return self._use_project(value, chat_id=chat_id)
        if lowered_kind in {"agent", "role"}:
            return self._use_agent(value, chat_id=chat_id)
        if lowered_kind in {"session", "thread"}:
            return self._use_session(value, chat_id=chat_id)
        if lowered_kind in {"task", "todo"}:
            task_ref = value.strip()
        else:
            task_ref = rest.strip()
        if not task_ref:
            return "Usage: /use <task>, /use project <project>, /use agent <agent>, or /use session <session>"
        if task_ref.lower() in {"assistant", "home"}:
            return self._assistant_mode(chat_id=chat_id)
        task = self._resolve_task(task_ref, chat_id=chat_id)
        if task is None:
            return f"Task not found: {task_ref}"
        self.chat_state.set_current_task(chat_id, task.task_id)
        self.chat_state.set_current_session(chat_id, task.session_id or "")
        if task.project_id:
            self.chat_state.set_current_project(chat_id, task.project_id)
        if task.agent_id:
            self.chat_state.set_current_agent(chat_id, task.agent_id)
        return "\n".join(
            [
                "Current task set:",
                task.title,
                f"id: {task.task_id}",
                f"project: {task.project_id or '-'}",
                f"agent: {task.agent_id}",
                "Now you can send a plain message, or /run <message>.",
            ]
        )

    def _use_project(self, project_ref: str, *, chat_id: int) -> str:
        if not project_ref.strip():
            return "Usage: /use project <project_id, title, or number>"
        project = self._resolve_project(project_ref, chat_id=chat_id)
        if project is None:
            return f"Project not found: {project_ref}"
        self.chat_state.set_current_project(chat_id, project.project_id)
        self.chat_state.set_current_agent(chat_id, project.default_agent_id)
        self.chat_state.set_current_task(chat_id, "")
        self.chat_state.set_current_session(chat_id, "")
        return "\n".join(
            [
                "Current project set:",
                project.title,
                f"id: {project.project_id}",
                f"default agent: {project.default_agent_id}",
                "Use /tasks, /agents, /task new <title>, or /run after selecting a task.",
            ]
        )

    def _use_agent(self, agent_ref: str, *, chat_id: int) -> str:
        if not agent_ref.strip():
            return "Usage: /use agent <agent_id, title, or number>"
        agent = self._resolve_agent(agent_ref, chat_id=chat_id)
        if agent is None:
            return f"Agent not found: {agent_ref}"
        self.chat_state.set_current_agent(chat_id, agent.agent_id)
        if agent.project_id:
            self.chat_state.set_current_project(chat_id, agent.project_id)
        self.chat_state.set_current_task(chat_id, "")
        self.chat_state.set_current_session(chat_id, "")
        return "\n".join(
            [
                "Current agent set:",
                agent.title,
                f"id: {agent.agent_id}",
                f"role: {agent.role}",
                f"project: {agent.project_id or '-'}",
                f"adapter: {agent.adapter}",
            ]
        )

    def _current(self, *, chat_id: int | None) -> str:
        if chat_id is None:
            return "This command requires a Telegram chat."
        lines: list[str] = []
        project = self._resolve_project(self.chat_state.current_project(chat_id))
        agent = self._resolve_agent(self.chat_state.current_agent(chat_id))
        if project is None:
            lines.append("Current project: none")
        else:
            lines.append(f"Current project: {project.title} ({project.project_id})")
        if agent is None:
            lines.append("Current agent: none")
        else:
            lines.append(f"Current agent: {agent.title} ({agent.agent_id})")
        current_task_id = self.chat_state.current_task(chat_id)
        task = self._resolve_task(current_task_id) if current_task_id else None
        current_session_id = self.chat_state.current_session(chat_id)
        session = SessionRegistry(self.workspace).resolve(current_session_id) if current_session_id else None
        if task is None:
            lines.append("Current task: none")
            lines.append("Use /use task <task>, /tasks, or /task new <title>")
        else:
            lines.append("Current task:")
            lines.append(task.title)
            lines.append(f"id: {task.task_id}")
            lines.append(f"status: {task.status}")
        if session is None:
            lines.append("Current session: none")
        else:
            lines.append("Current session:")
            lines.append(session.title)
            lines.append(f"id: {session.session_id}")
            lines.append(f"agent: {session.agent_id}  adapter: {session.adapter}")
            lines.append("Plain text messages will resume this session.")
        if self.job_queue is not None:
            latest = self.job_queue.latest_for_chat(chat_id)
            if latest is not None:
                lines.append("")
                lines.append("Latest job:")
                lines.append(f"id: {latest.job_id}")
                lines.append(f"status: {latest.status}")
        return "\n".join(lines)

    def _status(self, *, chat_id: int | None) -> str:
        lines: list[str] = ["AgentDeck Status"]
        project = self._resolve_project(self.chat_state.current_project(chat_id)) if chat_id is not None else None
        agent = self._resolve_agent(self.chat_state.current_agent(chat_id)) if chat_id is not None else None
        lines.append(f"Project: {project.title if project is not None else '-'}")
        if project is not None:
            lines.append(f"Project id: {project.project_id}")
        lines.append(f"Agent: {agent.title if agent is not None else '-'}")
        if agent is not None:
            lines.append(f"Agent id: {agent.agent_id}  adapter: {agent.adapter}  role: {agent.role}")
        current_task_id = self.chat_state.current_task(chat_id) if chat_id is not None else ""
        current_task = self._resolve_task(current_task_id) if current_task_id else None
        current_session_id = self.chat_state.current_session(chat_id) if chat_id is not None else ""
        current_session = SessionRegistry(self.workspace).resolve(current_session_id) if current_session_id else None
        if current_task is None:
            lines.append("Current task: -")
        else:
            lines.append(f"Current task: {current_task.title}")
            lines.append(f"Task status: {current_task.status}  priority: {current_task.priority}")
        if current_session is None:
            lines.append("Current session: -")
        else:
            lines.append(f"Current session: {current_session.title}")
            lines.append(f"Session agent: {current_session.agent_id}  adapter: {current_session.adapter}")

        if self.job_queue is not None and chat_id is not None:
            latest = self.job_queue.latest_for_chat(chat_id)
            if latest is None:
                lines.append("Job: -")
            else:
                task = self._resolve_task(latest.task_id)
                task_title = task.title if task is not None else (latest.task_id or "-")
                lines.append(f"Job: {latest.status}  {latest.job_id}")
                lines.append(f"Job task: {task_title}")

        pending = ApprovalRegistry(self.workspace).list(status="pending")
        lines.append(f"Pending approvals: {len(pending)}")
        if chat_id is not None:
            auto_state = self.chat_state.auto_state(chat_id)
            if bool(auto_state.get("enabled")):
                lines.append(
                    "Auto: on"
                    f"  timer: {_format_auto_until(float(auto_state.get('until') or 0.0))}"
                    f"  approval: {_format_auto_approval_mode(_auto_approval_mode(auto_state))}"
                )
            else:
                lines.append("Auto: off")

        sessions = SessionRegistry(self.workspace).list()[:3]
        if sessions:
            lines.append("Recent sessions:")
            for index, session in enumerate(sessions, 1):
                lines.append(f"{index}. {session.title}  [{session.status}]")
        else:
            lines.append("Recent sessions: none")

        lines.append("")
        lines.append("Next:")
        lines.append("- /projects, /agents, /tasks")
        lines.append("- /use project <list #>, /use agent <list #>, /use task <list #>")
        lines.append("- /projectstate, /decisions, /context, /memories, /compact, /handoffs, /reviews")
        lines.append("- send plain text, /run <message>, /auto start")
        return "\n".join(lines)

    def _recent(self, *, chat_id: int | None) -> str:
        projects = ProjectRegistry(self.workspace).list()[:8]
        current_project_id = self.chat_state.current_project(chat_id) if chat_id is not None else ""
        scoped_project = self._resolve_project(current_project_id) if current_project_id else None
        agents = AgentRegistry(self.workspace).list(project_id=scoped_project.project_id if scoped_project is not None else None)[:8]
        tasks = TaskBoard(self.workspace).list()[:8]
        jobs = self.job_queue.list(chat_id=chat_id, limit=8) if self.job_queue is not None else []
        sessions = SessionRegistry(self.workspace).list()[:8]
        if chat_id is not None:
            self.chat_state.set_recent_projects(chat_id, [project.project_id for project in projects])
            self.chat_state.set_recent_agents(chat_id, [agent.agent_id for agent in agents])
            self.chat_state.set_recent(
                chat_id,
                task_ids=[task.task_id for task in tasks],
                job_ids=[job.job_id for job in jobs],
            )
            self.chat_state.set_recent_sessions(chat_id, [session.session_id for session in sessions])

        current_task_id = self.chat_state.current_task(chat_id) if chat_id is not None else ""
        lines: list[str] = ["Recent:"]
        if not projects:
            lines.append("Projects: none")
        else:
            lines.append("Projects:")
            for index, project in enumerate(projects, 1):
                marker = " [current]" if project.project_id == current_project_id else ""
                lines.append(f"{index}. {project.title}{marker}")
                lines.append(f"   id: {project.project_id}  agent: {project.default_agent_id}")

        current_agent_id = self.chat_state.current_agent(chat_id) if chat_id is not None else ""
        if not agents:
            lines.append("")
            lines.append("Agents: none")
        else:
            lines.append("")
            lines.append("Agents:")
            for index, agent in enumerate(agents, 1):
                marker = " [current]" if agent.agent_id == current_agent_id else ""
                lines.append(f"{index}. {agent.title}{marker}")
                lines.append(f"   id: {agent.agent_id}  adapter: {agent.adapter}  role: {agent.role}")

        if not tasks:
            lines.append("")
            lines.append("Tasks: none")
        else:
            lines.append("")
            lines.append("Tasks:")
            for index, task in enumerate(tasks, 1):
                marker = " [current]" if task.task_id == current_task_id else ""
                lines.append(f"{index}. {task.title}{marker}")
                lines.append(f"   status: {task.status}  project: {task.project_id or '-'}")

        if self.job_queue is not None:
            if not jobs:
                lines.append("")
                lines.append("Jobs: none")
            else:
                lines.append("")
                lines.append("Jobs:")
                for index, job in enumerate(jobs, 1):
                    task = self._resolve_task(job.task_id)
                    task_title = task.title if task is not None else job.task_id
                    lines.append(f"{index}. {job.status}  {task_title}")
                    if job.session_id:
                        lines.append(f"   session: {job.session_id}")

        if not sessions:
            lines.append("")
            lines.append("Sessions: none")
        else:
            lines.append("")
            lines.append("Sessions:")
            for index, session in enumerate(sessions, 1):
                lines.append(f"{index}. {session.title}")
                lines.append(f"   status: {session.status}  agent: {session.agent_id}")

        lines.append("")
        lines.append("Use /use project <list #>, /use agent <list #>, /use task <list #>, /run <list #> <message>, /job <list #>, or /resume <list #> <message>.")
        return "\n".join(lines)

    def _context(self, rest: str, *, chat_id: int | None) -> str:
        task, error = self._resolve_task_or_current(rest, chat_id=chat_id, usage="Usage: /context [task]")
        if error:
            return error
        assert task is not None
        context = build_agentdeck_context(
            self.workspace,
            task=task,
            session_id=task.session_id,
            max_chars=3400,
        )
        if not context:
            return f"No AgentDeck context for task: {task.title}"
        return context

    def _memories(self, rest: str, *, chat_id: int | None) -> str:
        task, error = self._resolve_task_or_current(rest, chat_id=chat_id, usage="Usage: /memories [task]")
        if error:
            return error
        assert task is not None
        memories = collect_relevant_memories(self.workspace, task)
        if chat_id is not None:
            self.chat_state.set_recent_memories(chat_id, [str(memory.path) for memory in memories])
        if not memories:
            return "\n".join(
                [
                    f"No durable memories for task: {task.title}",
                    "Use /compact [--pin] [title] to save the current structured task context.",
                ]
            )
        lines = [f"Durable memories: {task.title}"]
        for index, memory in enumerate(memories, 1):
            lines.extend(_format_memory_document(index, memory))
        lines.append("")
        lines.append("Use /compact [--pin] [title] to save a fresh task context snapshot.")
        return "\n".join(lines)

    def _set_memory_disabled(self, rest: str, *, disabled: bool, chat_id: int | None) -> str:
        ref = self._resolve_memory_ref(rest, chat_id=chat_id)
        if not ref:
            action = "disable" if disabled else "enable"
            return f"Usage: /memory {action} <memory #, id, title, or path>\nRun /memories first to use a list number."
        try:
            document = MarkdownMemoryStore(self.workspace).set_disabled(ref, disabled=disabled)
        except ValueError as exc:
            return str(exc)
        state = "disabled" if disabled else "enabled"
        lines = [
            f"Memory {state}: {document.title}",
            f"id: {document.memory_id}",
            f"scope: {document.scope}{':' + document.owner if document.owner else ''}",
        ]
        if document.pinned:
            lines.append("pinned: yes")
        if disabled:
            lines.append("It will no longer be injected into task context.")
        else:
            lines.append("It can be retrieved into future task context again.")
        return "\n".join(lines)

    def _resolve_memory_ref(self, rest: str, *, chat_id: int | None) -> str:
        ref = rest.strip()
        if chat_id is not None and ref.isdigit():
            mapped = self.chat_state.recent_memory_path(chat_id, int(ref))
            if mapped:
                return mapped
        return ref

    def _compact(self, rest: str, *, chat_id: int | None) -> str:
        task, error = self._resolve_task_or_current("", chat_id=chat_id, usage="Usage: /compact [--pin] [title]")
        if error:
            return error
        assert task is not None
        title, pinned = _parse_compact_options(rest, default_title=f"{task.title} context snapshot")
        context = build_agentdeck_context(
            self.workspace,
            task=task,
            session_id=task.session_id,
            max_chars=6000,
            include_memories=False,
        )
        if not context:
            return f"No task context to compact: {task.title}"
        owner = task.project_id
        content = "\n".join(
            [
                f"# {title}",
                "",
                "This memory was generated from structured AgentDeck state, not a raw chat transcript.",
                "",
                context,
            ]
        )
        try:
            entry = MarkdownMemoryStore(self.workspace).add(
                title,
                content,
                scope="project",
                owner=owner,
                memory_type="task-context",
                source="telegram-compact",
                pinned=pinned,
                tags=["agentdeck", "task-context", "telegram"],
            )
        except ValueError as exc:
            return str(exc)
        lines = [
            "Memory compacted:",
            f"title: {title}",
            f"id: {entry.memory_id}",
            f"scope: project",
        ]
        if pinned:
            lines.append("pinned: yes")
        if owner:
            lines.append(f"owner: {owner}")
        lines.append(f"path: {entry.path}")
        lines.append("")
        lines.append("Use /memories to inspect what will be retrieved into future runs.")
        return "\n".join(lines)

    def _handoffs(self, rest: str, *, chat_id: int | None) -> str:
        task, error = self._resolve_task_or_current(rest, chat_id=chat_id, usage="Usage: /handoffs [task]")
        if error:
            return error
        assert task is not None
        entries = ProgressJournal(self.workspace).list(kind="handoff", task_id=task.task_id, limit=5)
        if not entries:
            return f"No handoffs for task: {task.title}"
        lines = [f"Handoffs: {task.title}"]
        for index, entry in enumerate(entries, 1):
            lines.append(f"{index}. {_one_line(entry.summary, 220)}")
            if entry.next_steps:
                lines.append(f"   next: {_one_line(entry.next_steps[0], 220)}")
            if entry.blockers:
                lines.append(f"   blocker: {_one_line(entry.blockers[0], 220)}")
            if entry.decisions:
                lines.append(f"   decision: {_one_line(entry.decisions[0], 220)}")
        lines.append("")
        lines.append("Use /context to see what will be injected into the next run.")
        return "\n".join(lines)

    def _review(self, rest: str, *, chat_id: int | None) -> str:
        summary = rest.strip()
        if not summary:
            return "Usage: /review <manager review summary>"
        task, error = self._resolve_task_or_current("", chat_id=chat_id, usage="Usage: /review <manager review summary>")
        if error:
            return error
        assert task is not None
        reviewer = "manager"
        if chat_id is not None:
            reviewer = self.chat_state.current_agent(chat_id) or reviewer
        try:
            entry = ProgressJournal(self.workspace).append(
                kind="manager-review",
                summary=summary,
                project_id=task.project_id,
                task_id=task.task_id,
                session_id=task.session_id,
                agent_id=reviewer,
                metadata={"status": "noted", "reviewer": reviewer},
            )
        except ValueError as exc:
            return str(exc)
        TaskBoard(self.workspace).add_note(task.task_id, format_review(entry), kind="manager-review")
        if task.session_id:
            SessionStateStore(self.workspace).upsert_from_progress(entry, objective=task.description or task.title)
        return "\n".join(
            [
                f"Manager review recorded: {task.title}",
                f"id: {entry.entry_id}",
                "",
                "Use /reviews to inspect or /context to see the next-run context.",
            ]
        )

    def _reviews(self, rest: str, *, chat_id: int | None) -> str:
        task, error = self._resolve_task_or_current(rest, chat_id=chat_id, usage="Usage: /reviews [task]")
        if error:
            return error
        assert task is not None
        entries = ProgressJournal(self.workspace).list(kind="manager-review", task_id=task.task_id, limit=5)
        if not entries:
            return f"No manager reviews for task: {task.title}"
        lines = [f"Manager reviews: {task.title}"]
        for index, entry in enumerate(entries, 1):
            status = str(entry.metadata.get("status") or "").strip()
            prefix = f"{status}: " if status else ""
            lines.append(f"{index}. {prefix}{_one_line(entry.summary, 220)}")
            if entry.next_steps:
                lines.append(f"   next: {_one_line(entry.next_steps[0], 220)}")
            if entry.blockers:
                lines.append(f"   blocker: {_one_line(entry.blockers[0], 220)}")
            if entry.decisions:
                lines.append(f"   decision: {_one_line(entry.decisions[0], 220)}")
        lines.append("")
        lines.append("Use /context to see what will be injected into the next run.")
        return "\n".join(lines)

    def _resolve_task_or_current(
        self,
        rest: str,
        *,
        chat_id: int | None,
        usage: str,
    ) -> tuple[TaskRecord | None, str]:
        task_ref = rest.strip()
        if task_ref:
            task = self._resolve_task(task_ref, chat_id=chat_id)
            if task is None:
                return None, f"Task not found: {task_ref}"
            return task, ""
        if chat_id is None:
            return None, f"{usage}; Telegram chat required when no task is given."
        current_task_id = self.chat_state.current_task(chat_id)
        task = self._resolve_task(current_task_id) if current_task_id else None
        if task is None:
            return None, "No current task. Use /tasks and /use task <ref>, or pass a task id."
        return task, ""

    def _default_project(self, *, chat_id: int | None) -> ProjectRecord | None:
        if chat_id is not None:
            current_project_id = self.chat_state.current_project(chat_id)
            project = self._resolve_project(current_project_id) if current_project_id else None
            if project is not None:
                return project
            current_task_id = self.chat_state.current_task(chat_id)
            task = self._resolve_task(current_task_id) if current_task_id else None
            if task is not None and task.project_id:
                project = ProjectRegistry(self.workspace).resolve(task.project_id)
                if project is not None:
                    return project
        projects = ProjectRegistry(self.workspace).list(status="active")
        if len(projects) == 1:
            return projects[0]
        return None

    def _default_agent(self, *, chat_id: int | None, project: ProjectRecord | None = None) -> AgentRecord | None:
        if chat_id is not None:
            current_agent_id = self.chat_state.current_agent(chat_id)
            agent = self._resolve_agent(current_agent_id) if current_agent_id else None
            if agent is not None:
                return agent
        if project is not None:
            agent = AgentRegistry(self.workspace).resolve(project.default_agent_id)
            if agent is not None:
                return agent
        return None

    def _resolve_project(self, value: str, *, chat_id: int | None = None) -> ProjectRecord | None:
        clean = value.strip()
        if not clean:
            return None
        if chat_id is not None and clean.isdigit():
            mapped = self.chat_state.recent_project_id(chat_id, int(clean))
            if mapped:
                clean = mapped
        registry = ProjectRegistry(self.workspace)
        project = registry.resolve(clean)
        if project is not None:
            return project
        lowered = " ".join(clean.split()).lower()
        matches = [record for record in registry.list() if record.title.lower() == lowered]
        if len(matches) == 1:
            return matches[0]
        return None

    def _resolve_project_or_current(
        self,
        rest: str,
        *,
        chat_id: int | None,
        usage: str,
    ) -> tuple[ProjectRecord | None, str]:
        project_ref = rest.strip()
        if project_ref:
            project = self._resolve_project(project_ref, chat_id=chat_id)
            if project is None:
                return None, f"Project not found: {project_ref}"
            return project, ""
        project = self._default_project(chat_id=chat_id)
        if project is None:
            return None, f"No current project. Use /projects and /use project <ref>. {usage}"
        return project, ""

    def _resolve_agent(self, value: str, *, chat_id: int | None = None) -> AgentRecord | None:
        clean = value.strip()
        if not clean:
            return None
        if chat_id is not None and clean.isdigit():
            mapped = self.chat_state.recent_agent_id(chat_id, int(clean))
            if mapped:
                clean = mapped
        registry = AgentRegistry(self.workspace)
        agent = registry.resolve(clean)
        if agent is not None:
            return agent
        lowered = " ".join(clean.split()).lower()
        matches = [record for record in registry.list() if record.title.lower() == lowered]
        if len(matches) == 1:
            return matches[0]
        return None

    def _resolve_task(self, value: str, *, chat_id: int | None = None) -> TaskRecord | None:
        if chat_id is not None and value.strip().isdigit():
            mapped = self.chat_state.recent_task_id(chat_id, int(value.strip()))
            if mapped:
                value = mapped
        board = TaskBoard(self.workspace)
        task = board.resolve(value)
        if task is not None:
            return task
        clean = " ".join(value.strip().split()).lower()
        if not clean:
            return None
        matches = [record for record in board.list() if record.title.lower() == clean]
        if len(matches) == 1:
            return matches[0]
        return None

    def _sessions(self, rest: str, *, chat_id: int | None) -> str:
        agent_id = rest.strip() or None
        records = SessionRegistry(self.workspace).list(agent_id=agent_id)
        if not records:
            return "No sessions."
        records = records[:10]
        current_session_id = self.chat_state.current_session(chat_id) if chat_id is not None else ""
        if chat_id is not None:
            self.chat_state.set_recent_sessions(chat_id, [record.session_id for record in records])
        lines = ["Sessions:"]
        for index, record in enumerate(records, 1):
            marker = " [current]" if record.session_id == current_session_id else ""
            lines.append(f"{index}. {record.title}{marker}")
            lines.append(f"   status: {record.status}  agent: {record.agent_id}  adapter: {record.adapter}")
            task = self._task_for_session(record.session_id)
            if task is not None:
                lines.append(f"   task: {task.title}")
            if record.provider_session_id:
                lines.append(f"   provider: {record.provider_session_kind or 'session'}")
            if record.last_assistant_final:
                lines.append(f"   last: {_one_line(record.last_assistant_final, 120)}")
        lines.append("")
        lines.append("Use /session <list #>, /use session <list #>, or /resume <list #> <message>.")
        return "\n".join(lines)

    def _session(self, rest: str, *, chat_id: int | None) -> str:
        session_ref = rest.strip()
        if not session_ref:
            return (
                "Usage: /session <session_id, title, or number>, "
                "/session scan [codex|kimi] <old cwd>, or /session import <scan #>"
            )
        subcommand, subrest = _split_once(session_ref)
        lowered = subcommand.lower()
        if lowered == "scan":
            return self._session_scan(subrest, chat_id=chat_id)
        if lowered == "import":
            return self._session_import(subrest, chat_id=chat_id)
        if lowered in {"use", "select"}:
            return self._use_session(subrest, chat_id=chat_id)
        record = self._resolve_session(session_ref, chat_id=chat_id)
        if record is None:
            return f"Session not found: {session_ref}"
        lines = [
            record.title,
            f"id: {record.session_id}",
            f"status: {record.status}",
            f"agent: {record.agent_id}",
            f"adapter: {record.adapter}",
            f"project_dir: {record.project_dir}",
        ]
        task = self._task_for_session(record.session_id)
        if task is not None:
            lines.append(f"task: {task.title}")
        if record.provider_session_id:
            lines.append(f"provider_session: {record.provider_session_kind or 'provider_session'}")
        if record.last_user_message:
            lines.append(f"last user: {_one_line(record.last_user_message, 180)}")
        if record.last_assistant_final:
            lines.append(f"last reply: {_one_line(record.last_assistant_final, 240)}")
        lines.append("")
        lines.append("Use /session use <list #> to make plain text resume this session, or /resume <list #> <message> once.")
        return "\n".join(lines)

    def _session_scan(self, rest: str, *, chat_id: int | None) -> str:
        provider, scan_rest = _parse_provider_scan(rest)
        cwd = scan_rest.strip()
        if not cwd and chat_id is not None:
            current_project = self._resolve_project(self.chat_state.current_project(chat_id))
            if current_project is not None:
                cwd = current_project.project_dir
        if not cwd:
            return "Usage: /session scan [codex|kimi] <old provider cwd>"

        candidates = scan_provider_sessions(provider=provider, project_dir=cwd)[:10]
        if not candidates:
            provider_text = f" {provider}" if provider else ""
            return f"No{provider_text} provider sessions found for: {cwd}"
        if chat_id is not None:
            self.chat_state.set_recent_provider_sessions(chat_id, candidates)

        lines = [f"Provider sessions for: {cwd}"]
        for index, candidate in enumerate(candidates, 1):
            marker = " [last]" if bool(candidate.metadata.get("is_last_session")) else ""
            lines.append(f"{index}. {candidate.title}{marker}")
            lines.append(
                f"   provider: {candidate.provider}  id: {_short_id(candidate.provider_session_id)}  "
                f"updated: {_format_timestamp(candidate.updated_at)}"
            )
        lines.append("")
        lines.append("Use /session import <list #> [project <project>] [task <task>] [agent <agent>].")
        return "\n".join(lines)

    def _session_import(self, rest: str, *, chat_id: int | None) -> str:
        if chat_id is None:
            return "This command requires a Telegram chat."
        clean = rest.strip()
        if not clean:
            return "Usage: /session import <scan #> [project <project>] [task <task>] [agent <agent>]"
        ref, option_text = _split_once(clean)
        if not ref.isdigit():
            return "Usage: /session import <scan #> [project <project>] [task <task>] [agent <agent>]"
        candidate = self.chat_state.recent_provider_session(chat_id, int(ref))
        if candidate is None:
            return "Provider session not found in recent scan. Use /session scan <old cwd> first."

        options = _parse_session_import_options(option_text)
        project = self._resolve_project(options.get("project", ""))
        if project is None:
            current_project_id = self.chat_state.current_project(chat_id)
            project = self._resolve_project(current_project_id) if current_project_id else None
        task: TaskRecord | None = None
        if options.get("task"):
            task = self._resolve_task(options["task"], chat_id=chat_id)
            if task is None:
                return f"Task not found: {options['task']}"
            if project is None and task.project_id:
                project = self._resolve_project(task.project_id)
        if options.get("project") and project is None:
            return f"Project not found: {options['project']}"

        agent_id = options.get("agent") or self.chat_state.current_agent(chat_id)
        if not agent_id and project is not None:
            agent_id = project.default_agent_id
        agent_id = agent_id or "default"
        project_dir = options.get("cwd") or (project.project_dir if project is not None else candidate.project_dir)

        registry = SessionRegistry(self.workspace)
        record = registry.import_provider_session(
            provider_session_id=candidate.provider_session_id,
            provider_session_kind=candidate.provider_session_kind,
            agent_id=agent_id,
            adapter=candidate.adapter,
            project_dir=project_dir,
            title=options.get("title") or candidate.title,
            metadata={
                "provider": candidate.provider,
                "imported_by": "telegram",
                "source_path": candidate.source_path,
            },
        )
        if task is not None:
            TaskBoard(self.workspace).attach_session(task.task_id, record.session_id)
            self.chat_state.set_current_task(chat_id, task.task_id)
        if project is not None:
            self.chat_state.set_current_project(chat_id, project.project_id)
        self.chat_state.set_current_agent(chat_id, agent_id)
        self.chat_state.set_current_session(chat_id, record.session_id)
        self.chat_state.set_recent_sessions(chat_id, [record.session_id])

        lines = [
            "Provider session imported:",
            f"title: {record.title}",
            f"session: {record.session_id}",
            f"provider: {candidate.provider} {_short_id(candidate.provider_session_id)}",
            f"agent: {record.agent_id}",
            f"project_dir: {record.project_dir}",
        ]
        if project is not None:
            lines.append(f"project: {project.title}")
        if task is not None:
            lines.append(f"task: {task.title}")
        lines.append("")
        lines.append("Use /resume <message> or /resume 1 <message> to continue it.")
        return "\n".join(lines)

    async def _resume(self, rest: str, *, chat_id: int | None) -> str:
        if chat_id is None:
            return "This command requires a Telegram chat."
        session, prompt, error = self._resolve_resume_target(rest, chat_id=chat_id)
        if error:
            return error
        assert session is not None

        task = self._task_for_session(session.session_id)
        self._select_session(chat_id, session, task=task)
        metadata = {"session_id": session.session_id, "agent_id": session.agent_id}
        if self.job_queue is not None:
            job = self.job_queue.start(
                chat_id=chat_id,
                task_id=task.task_id if task is not None else "",
                prompt=prompt,
                metadata=metadata,
            )
            return "\n".join(
                [
                    f"Resume job started: {job.job_id}",
                    f"session: {session.title}",
                    f"status: queued",
                    "Use /job to view the latest job",
                ]
            )
        try:
            result = await self.runner(
                self.workspace,
                RunRequest(prompt=prompt, session=session.session_id, agent=session.agent_id),
            )
        except RunConfigurationError as exc:
            return str(exc)
        lines = [result.final_text or "Run finished without a final text response.", "", f"session: {result.session_id}"]
        return "\n".join(lines)

    def _resolve_session(self, value: str, *, chat_id: int | None = None) -> SessionRecord | None:
        clean = value.strip()
        if chat_id is not None and clean.isdigit():
            mapped = self.chat_state.recent_session_id(chat_id, int(clean))
            if mapped:
                clean = mapped
        return SessionRegistry(self.workspace).resolve(clean)

    def _resolve_resume_target(self, rest: str, *, chat_id: int) -> tuple[SessionRecord | None, str, str]:
        clean = rest.strip()
        if not clean:
            return None, "", "Usage: /resume <session number> <message>, or /resume <message> after /use"

        session_ref, maybe_prompt = _split_once(clean)
        if session_ref and maybe_prompt:
            session = self._resolve_session(session_ref, chat_id=chat_id)
            if session is not None:
                return session, maybe_prompt, ""

        current_task_id = self.chat_state.current_task(chat_id)
        current_task = self._resolve_task(current_task_id) if current_task_id else None
        if current_task is not None and current_task.session_id:
            session = SessionRegistry(self.workspace).resolve(current_task.session_id)
            if session is not None:
                return session, clean, ""

        current_session_id = self.chat_state.current_session(chat_id)
        current_session = SessionRegistry(self.workspace).resolve(current_session_id) if current_session_id else None
        if current_session is not None:
            return current_session, clean, ""

        if session_ref and not maybe_prompt and self._resolve_session(session_ref, chat_id=chat_id) is not None:
            return None, "", "Usage: /resume <session number> <message>"
        return None, "", "No current resumable session. Use /sessions, then /resume <list #> <message>."

    async def _run_session_prompt(self, session: SessionRecord, prompt: str, *, chat_id: int) -> str:
        task = self._task_for_session(session.session_id)
        self._select_session(chat_id, session, task=task)
        metadata = {"session_id": session.session_id, "agent_id": session.agent_id}
        if self.job_queue is not None:
            job = self.job_queue.start(
                chat_id=chat_id,
                task_id=task.task_id if task is not None else "",
                prompt=prompt,
                metadata=metadata,
            )
            return "\n".join(
                [
                    f"Session job started: {job.job_id}",
                    f"session: {session.title}",
                    f"agent: {session.agent_id}",
                    f"task: {task.title if task is not None else '-'}",
                    "status: queued",
                    "Use /job to view the latest job",
                ]
            )
        try:
            result = await self.runner(
                self.workspace,
                RunRequest(prompt=prompt, session=session.session_id, agent=session.agent_id),
            )
        except RunConfigurationError as exc:
            return str(exc)
        lines = [result.final_text or "Run finished without a final text response.", "", f"session: {result.session_id}"]
        return "\n".join(lines)

    def _use_session(self, session_ref: str, *, chat_id: int | None) -> str:
        if chat_id is None:
            return "This command requires a Telegram chat."
        if not session_ref.strip():
            return "Usage: /use session <session_id, title, or list #>"
        session = self._resolve_session(session_ref, chat_id=chat_id)
        if session is None:
            return f"Session not found: {session_ref}"
        task = self._task_for_session(session.session_id)
        project = self._select_session(chat_id, session, task=task)
        lines = [
            "Session selected.",
            "Plain text messages will resume this session.",
            f"session: {session.title}",
            f"id: {session.session_id}",
            f"agent: {session.agent_id}",
            f"adapter: {session.adapter}",
        ]
        if task is not None:
            lines.append(f"task: {task.title}")
            lines.append(f"task id: {task.task_id}")
        else:
            lines.append("task: -")
        if project is not None:
            lines.append(f"project: {project.title} ({project.project_id})")
        return "\n".join(lines)

    def _select_session(
        self,
        chat_id: int,
        session: SessionRecord,
        *,
        task: TaskRecord | None = None,
    ) -> ProjectRecord | None:
        self.chat_state.set_current_session(chat_id, session.session_id)
        project: ProjectRecord | None = None
        if task is not None:
            self.chat_state.set_current_task(chat_id, task.task_id)
            if task.project_id:
                project = ProjectRegistry(self.workspace).resolve(task.project_id)
                self.chat_state.set_current_project(chat_id, task.project_id)
            if task.agent_id:
                self.chat_state.set_current_agent(chat_id, task.agent_id)
            elif session.agent_id:
                self.chat_state.set_current_agent(chat_id, session.agent_id)
            return project

        self.chat_state.set_current_task(chat_id, "")
        if session.agent_id:
            self.chat_state.set_current_agent(chat_id, session.agent_id)
        project = self._project_for_session(session)
        if project is not None:
            self.chat_state.set_current_project(chat_id, project.project_id)
        return project

    async def _resume_job(self, rest: str, *, chat_id: int | None) -> str:
        if chat_id is None:
            return "This command requires a Telegram chat."
        if self.job_queue is None:
            return "Jobs are not enabled for this interface."
        job_ref, prompt = _split_once(rest.strip())
        if not job_ref:
            latest = self.job_queue.latest_for_chat(chat_id, statuses={"interrupted", "error", "cancelled"})
            if latest is None:
                return "Usage: /job resume <job_id or list #> [message]"
            job = latest
        else:
            job_id = self._resolve_job_id(job_ref, chat_id=chat_id)
            job = self.job_queue.get(job_id)
        if job is None:
            return f"Job not found: {job_ref}"
        if job.status not in {"interrupted", "error", "cancelled"}:
            return f"Job is {job.status}, not resumable: {job.job_id}"
        prompt = prompt.strip() or _interrupted_job_resume_prompt(job)
        metadata = dict(job.metadata or {})
        session_id = _safe_session_id_for_resume(self.workspace, job.session_id)
        if session_id:
            metadata["session_id"] = session_id
        elif "session_id" in metadata:
            metadata.pop("session_id", None)
        resume_task_id = job.task_id if job.task_id and TaskBoard(self.workspace).resolve(job.task_id) is not None else ""
        new_job = self.job_queue.start(chat_id=chat_id, task_id=resume_task_id, prompt=prompt, metadata=metadata)
        self.chat_state.set_recent(chat_id, task_ids=[resume_task_id] if resume_task_id else [], job_ids=[new_job.job_id, job.job_id])
        lines = [
            f"Interrupted job resumed: {job.job_id}",
            f"New job started: {new_job.job_id}",
            f"status: queued",
        ]
        if session_id:
            lines.append(f"session: {session_id}")
        elif resume_task_id:
            lines.append("session: not available; resumed from task context")
        else:
            lines.append("session: not available; rerunning the saved prompt context")
        lines.append("Use /job to view the latest job.")
        return "\n".join(lines)

    def _task_for_session(self, session_id: str) -> TaskRecord | None:
        if not session_id:
            return None
        matches = [task for task in TaskBoard(self.workspace).list() if task.session_id == session_id]
        if not matches:
            return None
        return matches[0]

    def _ensure_task_for_current_session(self, chat_id: int) -> tuple[TaskRecord | None, bool]:
        session_id = self.chat_state.current_session(chat_id)
        session = SessionRegistry(self.workspace).resolve(session_id) if session_id else None
        if session is None:
            return None, False
        existing = self._task_for_session(session.session_id)
        if existing is not None:
            self.chat_state.set_current_task(chat_id, existing.task_id)
            if existing.project_id:
                self.chat_state.set_current_project(chat_id, existing.project_id)
            if existing.agent_id:
                self.chat_state.set_current_agent(chat_id, existing.agent_id)
            return existing, False

        project = self._project_for_session(session)
        agent = AgentRegistry(self.workspace).resolve(session.agent_id) if session.agent_id else None
        project_id = project.project_id if project is not None else (agent.project_id if agent is not None else "")
        team_id = project.team_id if project is not None else (agent.team_id if agent is not None else "default")
        agent_id = session.agent_id or (project.default_agent_id if project is not None else "owner")
        title = session.title.strip() or f"Continue session {session.session_id}"
        description = (
            "Auto-created from the current Telegram session so /auto can track "
            "progress, handoffs, and follow-up runs."
        )
        task = TaskBoard(self.workspace).create(
            title=title,
            description=description,
            project_id=project_id,
            agent_id=agent_id,
            team_id=team_id,
        )
        attached = TaskBoard(self.workspace).attach_session(task.task_id, session.session_id)
        if attached is not None:
            task = attached
        self.chat_state.set_current_task(chat_id, task.task_id)
        self.chat_state.set_current_session(chat_id, session.session_id)
        if task.project_id:
            self.chat_state.set_current_project(chat_id, task.project_id)
        if task.agent_id:
            self.chat_state.set_current_agent(chat_id, task.agent_id)
        return task, True

    def _project_for_session(self, session: SessionRecord) -> ProjectRecord | None:
        if not session.project_dir:
            return None
        try:
            session_dir = str(Path(session.project_dir).expanduser().resolve())
        except OSError:
            session_dir = session.project_dir
        for project in ProjectRegistry(self.workspace).list():
            try:
                project_dir = str(Path(project.project_dir).expanduser().resolve())
            except OSError:
                project_dir = project.project_dir
            if project_dir == session_dir:
                return project
        return None

    def _agents(self, rest: str, *, chat_id: int | None) -> str:
        project_ref = rest.strip()
        project: ProjectRecord | None = None
        if project_ref:
            project = self._resolve_project(project_ref, chat_id=chat_id)
            if project is None:
                return f"Project not found: {project_ref}"
        elif chat_id is not None:
            current_project = self.chat_state.current_project(chat_id)
            project = self._resolve_project(current_project) if current_project else None
        records = AgentRegistry(self.workspace).list(project_id=project.project_id if project is not None else None)
        if not records:
            return "No agents."
        if chat_id is not None:
            self.chat_state.set_recent_agents(chat_id, [record.agent_id for record in records])
        current_agent_id = self.chat_state.current_agent(chat_id) if chat_id is not None else ""
        heading = f"Agents ({project.title}):" if project is not None else "Agents:"
        lines = [heading]
        for index, record in enumerate(records, 1):
            marker = " [current]" if record.agent_id == current_agent_id else ""
            lines.append(f"{index}. {record.title}{marker}")
            lines.append(f"   id: {record.agent_id}  role: {record.role}")
            lines.append(f"   project: {record.project_id or '-'}  adapter: {record.adapter}")
        lines.append("")
        lines.append("Use /use agent <list #> or /agent new <id> [adapter] [role] [title].")
        return "\n".join(lines)

    def _agent(self, rest: str, *, chat_id: int | None) -> str:
        command, tail = _split_once(rest.strip())
        lowered = command.lower()
        if lowered in {"new", "create"}:
            return self._new_agent(tail, chat_id=chat_id)
        if lowered in {"use", "select"}:
            if chat_id is None:
                return "This command requires a Telegram chat."
            return self._use_agent(tail, chat_id=chat_id)
        if lowered in {"template", "role-template"}:
            return self._agent_template(tail, chat_id=chat_id)
        agent_ref = rest.strip()
        if not agent_ref:
            if chat_id is not None:
                current = self.chat_state.current_agent(chat_id)
                if current:
                    agent_ref = current
            if not agent_ref:
                return "Usage: /agent <agent_id or list #>, /agent use <list #>, or /agent new <id> [adapter] [role] [title]"
        agent = self._resolve_agent(agent_ref, chat_id=chat_id)
        if agent is None:
            return f"Agent not found: {agent_ref}"
        lines = [
            agent.title,
            f"id: {agent.agent_id}",
            f"project: {agent.project_id or '-'}",
            f"role: {agent.role}",
            f"team: {agent.team_id}",
            f"adapter: {agent.adapter}",
            f"cwd: {agent.project_dir}",
            f"approval: {agent.approval_mode}",
            f"resume: {agent.resume_policy}",
        ]
        lines.append("")
        lines.append("Use /agent use <id or list #> to select it, or /agent template <prompt> to customize guidance.")
        return "\n".join(lines)

    def _agent_template(self, rest: str, *, chat_id: int | None) -> str:
        clean = rest.strip()
        if not clean:
            return "Usage: /agent template <prompt>, /agent template <agent> <prompt>, or /agent template clear [agent]"
        first, tail = _split_once(clean)
        registry = AgentRegistry(self.workspace)
        if first.lower() == "clear":
            agent_ref = tail.strip()
            if not agent_ref and chat_id is not None:
                agent_ref = self.chat_state.current_agent(chat_id)
            if not agent_ref:
                return "No current agent. Use /agents and /use agent <ref>, or pass an agent id."
            try:
                agent = registry.set_role_template(agent_ref, "")
            except ValueError as exc:
                return str(exc)
            return f"Agent template cleared: {agent.title}"

        agent = self._resolve_agent(first, chat_id=chat_id) if tail else None
        if agent is not None:
            prompt = tail
        else:
            prompt = clean
            if chat_id is None:
                return "Telegram chat required when no agent is given."
            current_agent = self.chat_state.current_agent(chat_id)
            agent = self._resolve_agent(current_agent) if current_agent else None
            if agent is None:
                return "No current agent. Use /agents and /use agent <ref>, or pass an agent id."
        try:
            updated = registry.set_role_template(agent.agent_id, prompt)
        except ValueError as exc:
            return str(exc)
        return "\n".join(
            [
                f"Agent template set: {updated.title}",
                str(updated.metadata.get("role_template") or ""),
                "",
                "Future task runs for this agent will include this guidance.",
            ]
        )

    def _new_agent(self, rest: str, *, chat_id: int | None) -> str:
        agent_id, tail = _split_once(rest.strip())
        if not agent_id:
            return "Usage: /agent new <agent_id> [adapter] [role] [title]"
        adapter, tail = _consume_optional_choice(tail, {"echo", "codex", "codex-exec", "kimi", "kimi-print"}, default="echo")
        role, title = _split_once(tail)
        if not role:
            role = agent_id
        project = self._default_project(chat_id=chat_id)
        project_dir = project.project_dir if project is not None else "."
        project_id = project.project_id if project is not None else ""
        team_id = project.team_id if project is not None else "default"
        try:
            agent = AgentRegistry(self.workspace).upsert(
                agent_id=agent_id,
                title=title or None,
                project_id=project_id,
                role=role,
                team_id=team_id,
                adapter=adapter,
                project_dir=project_dir,
                approval_mode="fail",
                replace=False,
            )
        except ValueError as exc:
            return str(exc)
        if chat_id is not None:
            self.chat_state.set_current_agent(chat_id, agent.agent_id)
            if agent.project_id:
                self.chat_state.set_current_project(chat_id, agent.project_id)
        return "\n".join(
            [
                "Agent created and selected:",
                agent.title,
                f"id: {agent.agent_id}",
                f"project: {agent.project_id or '-'}",
                f"role: {agent.role}",
                f"adapter: {agent.adapter}",
                "Next: /task new <title> or /tasks",
            ]
        )

    def _approvals(self, rest: str, *, chat_id: int | None) -> str:
        status = rest.strip() or "pending"
        try:
            records = ApprovalRegistry(self.workspace).list(status=status)
        except ValueError as exc:
            return str(exc)
        if not records:
            return f"No {status} approvals."
        if chat_id is not None:
            self.chat_state.set_recent_approvals(chat_id, [record.approval_id for record in records])
        lines = [f"Approvals ({status}):"]
        for index, record in enumerate(records, 1):
            lines.append(f"{index}. {record.title}")
            lines.append(f"   id: {record.approval_id}")
            lines.append(f"   task: {record.task_id or '-'}  agent: {record.agent_id}")
            lines.append(f"   provider: {record.provider or record.adapter or '-'}")
        lines.append("")
        lines.append("Use /approval <list #>, /approve <list #>, or /reject <list #>.")
        return "\n".join(lines)

    def _approval(self, rest: str, *, chat_id: int | None) -> str:
        approval_id = rest.strip()
        if not approval_id:
            return "Usage: /approval <approval_id>"
        record = self._resolve_approval_record(approval_id, chat_id=chat_id)
        if record is None:
            return f"Approval not found: {approval_id}"
        lines = [
            record.title,
            f"id: {record.approval_id}",
            f"status: {record.status}",
            f"project: {record.project_id or '-'}",
            f"task: {record.task_id or '-'}",
            f"agent: {record.agent_id}",
            f"session: {record.session_id or '-'}",
            f"request: {record.request_text}",
        ]
        if record.resolution_note:
            lines.append(f"resolution: {record.resolution_note}")
        return "\n".join(lines)

    def _resolve_approval(self, rest: str, status: str, *, chat_id: int | None) -> str:
        approval_id, note = _split_once(rest.strip())
        if not approval_id:
            verb = "approve" if status == "approved" else "reject"
            return f"Usage: /{verb} <approval_id> [note]"
        existing_approval = self._resolve_approval_record(approval_id, chat_id=chat_id)
        previous_status = existing_approval.status if existing_approval is not None else ""
        if existing_approval is not None:
            approval_id = existing_approval.approval_id
        registry = ApprovalRegistry(self.workspace)
        record = registry.resolve_request(approval_id, status=status, resolved_by="telegram", note=note)
        if record is None:
            return f"Approval not found: {approval_id}"
        if record.task_id:
            TaskBoard(self.workspace).add_note(
                record.task_id,
                f"Approval {record.status}: {record.approval_id}" + (f"; {note}" if note else ""),
                kind=f"approval:{record.status}",
            )
        lines = [f"Approval {record.status}: {record.title}", f"id: {record.approval_id}"]
        if (
            status == "approved"
            and previous_status == "pending"
            and self.job_queue is not None
            and chat_id is not None
            and record.task_id
        ):
            session_id = self._safe_session_id_for_resume(record.session_id)
            prompt = _approval_resume_prompt(record)
            metadata = {"approval_id": record.approval_id, "approval_mode": "bypass"}
            if session_id:
                metadata["session_id"] = session_id
            job = self.job_queue.start(
                chat_id=chat_id,
                task_id=record.task_id,
                prompt=prompt,
                metadata=metadata,
            )
            lines.extend(
                [
                    "",
                    f"Follow-up job started: {job.job_id}",
                    "approval_mode: bypass",
                    "Use /job to view the latest job",
                ]
            )
        return "\n".join(lines)

    def _resolve_approval_record(self, value: str, *, chat_id: int | None = None) -> ApprovalRecord | None:
        clean = value.strip()
        if chat_id is not None and clean.isdigit():
            mapped = self.chat_state.recent_approval_id(chat_id, int(clean))
            if mapped:
                clean = mapped
        return ApprovalRegistry(self.workspace).resolve(clean)

    def _safe_session_id_for_resume(self, session_id: str) -> str:
        return _safe_session_id_for_resume(self.workspace, session_id)

    def _auto(self, rest: str, *, chat_id: int | None) -> str:
        if chat_id is None:
            return "This command requires a Telegram chat."
        clean, approval_mode = _extract_auto_approval_mode(rest.strip())
        action, tail = _split_once(clean)
        action = action.lower()
        if not action or action in {"status", "show"}:
            return self._auto_status(chat_id)
        if action in {"end", "stop", "off", "disable"}:
            self.chat_state.disable_auto(chat_id)
            return "Auto mode disabled."
        if action == "prompt":
            prompt = tail.strip()
            if not prompt:
                return "Usage: /auto prompt <message>"
            state = self.chat_state.auto_state(chat_id)
            self.chat_state.set_auto_state(
                chat_id,
                enabled=bool(state.get("enabled")),
                task_id=str(state.get("task_id") or self.chat_state.current_task(chat_id)),
                prompt=prompt,
                until=float(state.get("until") or 0.0),
                turns_started=int(state.get("turns_started") or 0),
                last_job_id=str(state.get("last_job_id") or ""),
                approval_mode=_auto_approval_mode(state),
                mode=_auto_mode(state),
            )
            return f"Auto prompt updated:\n{prompt}"
        if action in {"task", "by-task", "bytask"}:
            return self._auto_start(tail, chat_id=chat_id, approval_mode=approval_mode, mode="task")
        if action == "start" or action == "on":
            return self._auto_start(tail, chat_id=chat_id, approval_mode=approval_mode, mode="loop")
        if _looks_like_float(action):
            return self._auto_start(clean, chat_id=chat_id, approval_mode=approval_mode, mode="loop")
        return "Usage: /auto start [hours], /auto task [hours], /auto <hours>, /auto -h start, /auto status, /auto prompt <message>, or /auto end"

    def _auto_start(self, rest: str, *, chat_id: int, approval_mode: str, mode: str) -> str:
        task_id = self.chat_state.current_task(chat_id)
        task = self._resolve_task(task_id) if task_id else None
        created_task = False
        if task is None:
            task, created_task = self._ensure_task_for_current_session(chat_id)
        if task is None:
            return "No current task. Use /use <task_id or title>, /use session <ref>, or /task new <title>, then /auto start."
        duration_text, prompt_override = _split_once(rest.strip())
        until = 0.0
        if duration_text:
            if not _looks_like_float(duration_text):
                prompt_override = rest.strip()
            else:
                hours = float(duration_text)
                if hours <= 0:
                    return "Auto duration must be positive hours, or omit it for no timer."
                until = time.time() + hours * 3600.0
        state = self.chat_state.auto_state(chat_id)
        normalized_mode = _normalize_auto_mode(mode)
        default_prompt = DEFAULT_AUTO_TASK_PROMPT if normalized_mode == "task" else DEFAULT_AUTO_PROMPT
        prompt = prompt_override.strip() or str(state.get("prompt") or default_prompt)
        if normalized_mode == "task" and not prompt_override.strip():
            prompt = DEFAULT_AUTO_TASK_PROMPT
        normalized_approval_mode = _normalize_auto_approval_mode(approval_mode)
        self.chat_state.set_auto_state(
            chat_id,
            enabled=True,
            task_id=task.task_id,
            prompt=prompt,
            until=until,
            turns_started=0,
            last_job_id="",
            approval_mode=normalized_approval_mode,
            mode=normalized_mode,
        )
        lines = [
            "Auto mode enabled.",
            f"task: {task.title}",
            f"timer: {_format_auto_until(until)}",
            f"approval: {_format_auto_approval_mode(normalized_approval_mode)}",
            f"mode: {normalized_mode}",
        ]
        if created_task:
            lines.insert(1, f"Created task from current session: {task.task_id}")
        if self.job_queue is None:
            lines.append("Jobs are not enabled for this interface.")
            return "\n".join(lines)

        active = self.job_queue.latest_for_chat(chat_id, statuses={"queued", "running", "cancel_requested"})
        if active is not None:
            lines.append(f"active job: {active.job_id}")
            lines.append("Auto will continue after the active job finishes.")
            return "\n".join(lines)

        metadata = {"auto": True, "approval_mode": normalized_approval_mode, "auto_mode": normalized_mode}
        session_id = self._safe_session_id_for_resume(task.session_id)
        if session_id:
            metadata["session_id"] = session_id
        job = self.job_queue.start(chat_id=chat_id, task_id=task.task_id, prompt=prompt, metadata=metadata)
        self.chat_state.mark_auto_job(chat_id, job_id=job.job_id, task_id=task.task_id)
        lines.append(f"Job started: {job.job_id}")
        lines.append("Use /auto status or /auto end.")
        return "\n".join(lines)

    def _auto_status(self, chat_id: int) -> str:
        state = self.chat_state.auto_state(chat_id)
        if not bool(state.get("enabled")):
            return "Auto mode: off\nUse /auto start after selecting a task with /use."
        task = self._resolve_task(str(state.get("task_id") or ""))
        lines = [
            "Auto mode: on",
            f"task: {task.title if task is not None else str(state.get('task_id') or '-')}",
            f"timer: {_format_auto_until(float(state.get('until') or 0.0))}",
            f"approval: {_format_auto_approval_mode(_auto_approval_mode(state))}",
            f"mode: {_auto_mode(state)}",
            f"turns started: {int(state.get('turns_started') or 0)}",
        ]
        if state.get("last_job_id"):
            lines.append(f"last auto job: {state['last_job_id']}")
        prompt = str(state.get("prompt") or DEFAULT_AUTO_PROMPT)
        lines.append(f"prompt: {_one_line(prompt, 220)}")
        lines.append("Use /auto end to stop.")
        return "\n".join(lines)

    async def _run(self, rest: str, *, chat_id: int | None = None) -> str:
        task, prompt, error = self._resolve_run_target(rest, chat_id=chat_id)
        if error:
            return error
        assert task is not None
        if chat_id is not None:
            self.chat_state.set_current_task(chat_id, task.task_id)
        if self.job_queue is not None and chat_id is not None:
            job = self.job_queue.start(chat_id=chat_id, task_id=task.task_id, prompt=prompt)
            return "\n".join(
                [
                    f"Job started: {job.job_id}",
                    f"task: {task.title}",
                    "status: queued",
                    "Use /job to view the latest job",
                ]
            )
        try:
            result = await self.runner(self.workspace, RunRequest(prompt=prompt, task=task.task_id))
        except RunConfigurationError as exc:
            return str(exc)
        lines = []
        if result.final_text:
            lines.append(result.final_text)
        else:
            lines.append("Run finished without a final text response.")
        lines.append("")
        lines.append(f"session: {result.session_id}")
        if result.approval_requested:
            lines.append("approval: required")
            if result.pending_approvals:
                lines.append(f"approval_id: {result.pending_approvals[0].approval_id}")
                lines.append(f"Use /approval {result.pending_approvals[0].approval_id}")
        return "\n".join(lines)

    def _resolve_run_target(self, rest: str, *, chat_id: int | None) -> tuple[TaskRecord | None, str, str]:
        clean = rest.strip()
        if not clean:
            return None, "", "Usage: /run <message> after /use <task>, or /run <task_id> <message>"
        task_ref, maybe_prompt = _split_once(clean)
        if task_ref and maybe_prompt:
            task = self._resolve_task(task_ref, chat_id=chat_id)
            if task is not None:
                return task, maybe_prompt, ""
        if chat_id is not None:
            current_task_id = self.chat_state.current_task(chat_id)
            if current_task_id:
                task = self._resolve_task(current_task_id)
                if task is not None:
                    return task, clean, ""
        if task_ref and not maybe_prompt and self._resolve_task(task_ref, chat_id=chat_id) is not None:
            return None, "", "Usage: /run <task_id> <message>"
        return None, "", "No current task. Use /use <task_id or exact task title>, then /run <message>."

    def _jobs(self, *, chat_id: int | None) -> str:
        if self.job_queue is None:
            return "Jobs are not enabled for this interface."
        records = self.job_queue.list(chat_id=chat_id)
        if not records:
            return "No jobs."
        if chat_id is not None:
            self.chat_state.set_recent_jobs(chat_id, [record.job_id for record in records])
        lines = ["Jobs:"]
        for index, job in enumerate(records, 1):
            lines.append(f"{index}. {job.job_id}")
            lines.append(f"   status: {job.status}  task: {job.task_id}")
            if job.session_id:
                lines.append(f"   session: {job.session_id}")
            if job.error:
                lines.append(f"   error: {_one_line(job.error, 120)}")
        return "\n".join(lines)

    def _job(self, rest: str, *, chat_id: int | None) -> str:
        if self.job_queue is None:
            return "Jobs are not enabled for this interface."
        job_id = rest.strip()
        if not job_id:
            if chat_id is None:
                return "Usage: /job <job_id>"
            latest = self.job_queue.latest_for_chat(chat_id)
            if latest is None:
                return "No jobs."
            job_id = latest.job_id
        else:
            job_id = self._resolve_job_id(job_id, chat_id=chat_id)
        job = self.job_queue.get(job_id)
        if job is None:
            return f"Job not found: {job_id}"
        lines = [
            f"Job: {job.job_id}",
            f"status: {job.status}",
            f"task: {job.task_id}",
        ]
        if job.session_id:
            lines.append(f"session: {job.session_id}")
        if job.error:
            lines.append(f"error: {job.error}")
        if job.final_text:
            lines.append("")
            lines.append(job.final_text)
        return "\n".join(lines)

    def _cancel(self, rest: str, *, chat_id: int | None) -> str:
        if self.job_queue is None:
            return "Jobs are not enabled for this interface."
        job_id = rest.strip()
        if not job_id:
            if chat_id is None:
                return "Usage: /cancel <job_id>"
            latest = self.job_queue.latest_for_chat(chat_id, statuses={"queued", "running", "cancel_requested"})
            if latest is None:
                return "No queued or running job to cancel."
            job_id = latest.job_id
        else:
            job_id = self._resolve_job_id(job_id, chat_id=chat_id)
        before = self.job_queue.get(job_id)
        if before is None:
            return f"Job not found: {job_id}"
        after = self.job_queue.cancel(job_id)
        if after is None:
            return f"Job not found: {job_id}"
        if before.status == "queued" and after.status == "cancelled":
            return f"Job cancelled: {job_id}"
        if after.status == "cancel_requested":
            return "\n".join(
                [
                    f"Cancel requested: {job_id}",
                    "status: cancel_requested",
                    "Note: AgentDeck will ask the running adapter to terminate.",
                ]
            )
        if after.status == "cancelled":
            return f"Job already cancelled: {job_id}"
        return f"Job is already {after.status}: {job_id}"

    def _resolve_job_id(self, value: str, *, chat_id: int | None) -> str:
        if chat_id is not None and value.strip().isdigit():
            mapped = self.chat_state.recent_job_id(chat_id, int(value.strip()))
            if mapped:
                return mapped
            if self.job_queue is not None:
                records = self.job_queue.list(chat_id=chat_id)
                index = int(value.strip())
                if 1 <= index <= len(records):
                    return records[index - 1].job_id
        return value.strip()


def request_process_restart(*, delay: float = 1.0) -> bool:
    """Schedule a same-argv process replacement so updated code is loaded."""

    global _RESTART_SCHEDULED
    with _RESTART_LOCK:
        if _RESTART_SCHEDULED:
            return False
        _RESTART_SCHEDULED = True

    command = [sys.executable, "-m", "agentdeck", *sys.argv[1:]]
    env = os.environ.copy()

    def restart_later() -> None:
        time.sleep(max(0.0, delay))
        os.execvpe(command[0], command, env)

    thread = threading.Thread(target=restart_later, name="agentdeck-restart", daemon=False)
    thread.start()
    return True


class TelegramServer:
    def __init__(
        self,
        workspace: Workspace,
        api: TelegramBotApi,
        config: TelegramConfig,
        *,
        restart_callback: Callable[[], bool | None] | None = request_process_restart,
    ) -> None:
        self.workspace = workspace
        self.api = api
        self.config = config
        self.command_audit = TelegramCommandAuditLog(workspace, bot_id=config.bot_id)
        self.offset_store = TelegramUpdateOffsetStore(workspace)
        self.job_queue = TelegramJobQueue(workspace, sender=api.send_message, state_scope=config.bot_id)
        self.handler = TelegramCommandHandler(
            workspace,
            job_queue=self.job_queue,
            video_sender=getattr(api, "send_video", None),
            assistant_agent_id=config.assistant_agent_id or ASSISTANT_AGENT_ID,
            bot_id=config.bot_id,
            restart_callback=restart_callback,
        )

    def serve_forever(self, *, once: bool = False) -> None:
        self._send_restart_notices()
        offset: int | None = self.offset_store.get(self.config.bot_id)
        while True:
            try:
                updates = self.api.get_updates(offset=offset, timeout=self.config.poll_timeout)
            except Exception as exc:  # keep the daemon alive through transient Bot API failures
                bot = f" bot={self.config.bot_id}" if self.config.bot_id else ""
                print(f"[agentdeck] telegram polling error{bot}: {exc}", flush=True)
                if once:
                    return
                time.sleep(5)
                continue
            for update in updates:
                try:
                    next_offset = int(update.get("update_id", 0)) + 1
                except (TypeError, ValueError):
                    continue
                offset = next_offset
                self.offset_store.set(self.config.bot_id, offset)
                # Ack before dispatch so restart/exec paths cannot replay side-effecting updates.
                self._handle_update(update)
            if once:
                return

    def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or update.get("edited_message") or {}
        if not isinstance(message, dict):
            return
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = message.get("text")
        if not isinstance(chat_id, int) or not isinstance(text, str):
            return
        if self.config.allowed_chat_ids and chat_id not in self.config.allowed_chat_ids:
            self.command_audit.append(
                chat_id=chat_id,
                text=text,
                outcome="ignored",
                detail="chat_id is not allowed",
            )
            return
        try:
            replies = asyncio.run(self.handler.handle_text(text, chat_id=chat_id))
        except Exception as exc:  # keep polling even if one command fails
            self.command_audit.append(
                chat_id=chat_id,
                text=text,
                outcome="error",
                detail=str(exc),
            )
            replies = [f"AgentDeck error: {exc}"]
        else:
            self.command_audit.append(
                chat_id=chat_id,
                text=text,
                outcome="handled",
                reply_count=len([reply for reply in replies if reply]),
            )
        for reply in replies:
            if reply:
                try:
                    self.api.send_message(chat_id, reply)
                except Exception as exc:
                    print(f"[agentdeck] telegram send error bot={self.config.bot_id}: {exc}", flush=True)

    def _send_restart_notices(self) -> None:
        notices = TelegramRestartNoticeStore(self.workspace).pop_for_bot(self.config.bot_id)
        if not notices:
            return
        for notice in notices:
            chat_id = notice.get("chat_id")
            if not isinstance(chat_id, int):
                continue
            try:
                self.api.send_message(
                    chat_id,
                    "\n".join(
                        [
                            "AgentDeck restarted.",
                            f"pid: {os.getpid()}",
                            f"bot: {self.config.bot_id or 'default'}",
                        ]
                    ),
                )
            except Exception as exc:
                print(f"[agentdeck] restart notice failed bot={self.config.bot_id}: {exc}", flush=True)


class TelegramMultiServer:
    """Run one long-polling worker per saved Telegram bot in a single daemon."""

    def __init__(
        self,
        workspace: Workspace,
        configs: list[TelegramConfig],
        *,
        api_factory: Callable[[str], TelegramBotApi] = TelegramBotApi,
    ) -> None:
        self.workspace = workspace
        self.configs = configs
        self.api_factory = api_factory

    def serve_forever(self, *, once: bool = False) -> None:
        servers = [
            TelegramServer(self.workspace, self.api_factory(config.token), config)
            for config in self.configs
            if config.token
        ]
        if not servers:
            return
        if len(servers) == 1:
            servers[0].serve_forever(once=once)
            return
        threads = [
            threading.Thread(target=server.serve_forever, kwargs={"once": once}, daemon=True)
            for server in servers
        ]
        for thread in threads:
            thread.start()
        if once:
            for thread in threads:
                thread.join()
            return
        while True:
            time.sleep(3600)


def config_from_env(
    token: str | None = None,
    allowed_chat_ids: list[str] | None = None,
    poll_timeout: int = 30,
    *,
    bot_id: str = "",
    assistant_agent_id: str = "",
) -> TelegramConfig:
    resolved_token = _normalize_bot_token(token or os.environ.get("AGENTDECK_TELEGRAM_TOKEN") or "")
    allowed = set(_parse_allowed_chat_ids(allowed_chat_ids or []))
    allowed.update(_parse_allowed_chat_ids((os.environ.get("AGENTDECK_TELEGRAM_ALLOWED_CHATS") or "").split(",")))
    return TelegramConfig(
        token=resolved_token,
        allowed_chat_ids=allowed,
        poll_timeout=poll_timeout,
        bot_id=bot_id or os.environ.get("AGENTDECK_TELEGRAM_BOT_ID", ""),
        assistant_agent_id=assistant_agent_id or os.environ.get("AGENTDECK_TELEGRAM_ASSISTANT_AGENT", ""),
    )


def split_message(text: str, *, limit: int = MAX_TELEGRAM_MESSAGE) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _split_command(text: str) -> tuple[str, str]:
    command, rest = _split_once(text)
    command = command.split("@", 1)[0].lower()
    return command, rest


def _split_once(text: str) -> tuple[str, str]:
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _consume_optional_choice(text: str, choices: set[str], *, default: str) -> tuple[str, str]:
    first, rest = _split_once(text)
    if first.lower() in choices:
        return first.lower(), rest
    return default, text.strip()


def _parse_provider_scan(rest: str) -> tuple[str | None, str]:
    first, remaining = _split_once(rest)
    lowered = first.lower()
    if lowered in {"codex", "kimi"}:
        return lowered, remaining
    return None, rest.strip()


def _parse_session_import_options(rest: str) -> dict[str, str]:
    tokens = rest.strip().split()
    options: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        key = tokens[index].lower()
        if key == "title":
            title = " ".join(tokens[index + 1 :]).strip()
            if title:
                options["title"] = title
            break
        if key in {"project", "task", "agent", "cwd"} and index + 1 < len(tokens):
            options[key] = tokens[index + 1]
            index += 2
            continue
        index += 1
    return options


def _short_id(value: str, *, keep: int = 8) -> str:
    clean = value.strip()
    if len(clean) <= keep:
        return clean
    return clean[:keep]


def _build_multipart_body(boundary: str, fields: dict[str, str | int | bool], files: dict[str, Path]) -> bytes:
    chunks: list[bytes] = []
    boundary_bytes = boundary.encode("ascii")
    for name, value in fields.items():
        chunks.extend(
            [
                b"--" + boundary_bytes + b"\r\n",
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    for name, path in files.items():
        filename = path.name
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        chunks.extend(
            [
                b"--" + boundary_bytes + b"\r\n",
                (
                    f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                    f"Content-Type: {content_type}\r\n\r\n"
                ).encode("utf-8"),
                path.read_bytes(),
                b"\r\n",
            ]
        )
    chunks.append(b"--" + boundary_bytes + b"--\r\n")
    return b"".join(chunks)


def _looks_like_video(path: Path) -> bool:
    content_type = mimetypes.guess_type(path.name)[0] or ""
    if content_type.startswith("video/"):
        return True
    return path.suffix.lower() in {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}


def _parse_allowed_chat_ids(values: list[str]) -> list[int]:
    chat_ids: list[int] = []
    for value in values:
        clean = str(value).strip()
        if not clean:
            continue
        try:
            chat_ids.append(int(clean))
        except ValueError:
            continue
    return chat_ids


def _normalize_bot_token(value: str) -> str:
    clean = value.strip()
    if "Token:" in clean:
        clean = clean.split("Token:", 1)[1].strip()
    return "".join(clean.split())


def _looks_like_float(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def _extract_auto_approval_mode(text: str) -> tuple[str, str]:
    tokens = text.split()
    kept: list[str] = []
    approval_mode = DEFAULT_AUTO_APPROVAL_MODE
    for token in tokens:
        lowered = token.lower()
        if lowered in {"-h", "--human", "--human-approval"}:
            approval_mode = HUMAN_AUTO_APPROVAL_MODE
            continue
        if lowered in {"--auto", "--auto-approval", "-a"}:
            approval_mode = DEFAULT_AUTO_APPROVAL_MODE
            continue
        kept.append(token)
    return " ".join(kept), approval_mode


def _normalize_auto_approval_mode(value: str) -> str:
    clean = (value or "").strip().lower()
    if clean in {"human", "manual", "fail", "ask"}:
        return HUMAN_AUTO_APPROVAL_MODE
    if clean in {"auto", "bypass", "approve", "approved"}:
        return DEFAULT_AUTO_APPROVAL_MODE
    return DEFAULT_AUTO_APPROVAL_MODE


def _auto_approval_mode(state: dict[str, Any]) -> str:
    return _normalize_auto_approval_mode(str(state.get("approval_mode") or DEFAULT_AUTO_APPROVAL_MODE))


def _normalize_auto_mode(value: str) -> str:
    clean = (value or "").strip().lower().replace("_", "-")
    if clean in {"task", "by-task", "bytask", "until-task-done"}:
        return "task"
    return "loop"


def _auto_mode(state: dict[str, Any]) -> str:
    return _normalize_auto_mode(str(state.get("mode") or state.get("auto_mode") or "loop"))


def _format_auto_approval_mode(value: str) -> str:
    return "human" if _normalize_auto_approval_mode(value) == HUMAN_AUTO_APPROVAL_MODE else "auto"


def _safe_session_id_for_resume(workspace: Workspace, session_id: str) -> str:
    session = SessionRegistry(workspace).get(session_id) if session_id else None
    if session is None:
        return ""
    if adapter_requires_provider_session(session.adapter) and not session.provider_session_id:
        return ""
    return session.session_id


def _auto_enabled_for_job(state: dict[str, Any], job: JobRecord) -> bool:
    if not bool(state.get("enabled")):
        return False
    task_id = str(state.get("task_id") or "")
    return bool(job.task_id) and task_id == job.task_id


def _auto_expired(state: dict[str, Any]) -> bool:
    until = float(state.get("until") or 0.0)
    return bool(until) and time.time() >= until


def _format_auto_until(until: float) -> str:
    if not until:
        return "none"
    remaining = max(0.0, until - time.time())
    if remaining < 60:
        return f"{remaining:.1f}s remaining"
    if remaining < 3600:
        return f"{remaining / 60:.1f}m remaining"
    return f"{remaining / 3600:.2f}h remaining"


def _format_timestamp(value: float) -> str:
    if not value:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


def _approval_resume_prompt(record: ApprovalRecord) -> str:
    request = _one_line(record.request_text, 300)
    lines = [
        "Approval was granted from Telegram.",
        "Continue the interrupted task now.",
        "Use the approved operation only if it is still necessary, and report the result clearly.",
    ]
    if request:
        lines.append(f"Approved request: {request}")
    return "\n".join(lines)


def _interrupted_job_resume_prompt(job: JobRecord) -> str:
    lines = [
        "请继续这个被中断的 AgentDeck job。",
        f"原 job: {job.job_id}",
    ]
    if job.task_id:
        lines.append(f"task: {job.task_id}")
    if job.prompt:
        lines.append("")
        lines.append("原始用户指令:")
        lines.append(job.prompt)
    lines.append("")
    lines.append("要求：根据当前项目和任务上下文继续推进；如果发现前一次运行可能已经部分完成，请先检查状态再继续。")
    return "\n".join(lines)


def _format_job_completion(job: JobRecord, approval_requested: bool) -> str:
    if job.status == "cancelled":
        heading = "Job cancelled"
    elif job.status == "error":
        heading = "Job failed"
    else:
        heading = "Job done"
    lines = [
        f"{heading}: {job.job_id}",
        f"task: {job.task_id or '-'}",
    ]
    if job.session_id:
        lines.append(f"session: {job.session_id}")
    if approval_requested:
        lines.append("approval: required")
        lines.append("Use /approvals")
    if job.error:
        lines.append(f"note: {_one_line(job.error, 180)}")
    lines.append("")
    if job.status == "cancelled":
        lines.append(_clean_job_final_text(job.final_text) or "Run cancelled.")
    else:
        lines.append(_clean_job_final_text(job.final_text) or "Run finished without a final text response.")
    return "\n".join(lines)


def _clean_job_final_text(text: str) -> str:
    return _strip_assistant_actions(_strip_auto_task_marker(text))


def _auto_task_completed(text: str) -> bool:
    return AUTO_TASK_DONE_MARKER in text


def _strip_auto_task_marker(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip() != AUTO_TASK_DONE_MARKER]
    return "\n".join(lines).strip()


def _is_assistant_job(job: JobRecord) -> bool:
    metadata = job.metadata or {}
    return bool(metadata.get("assistant")) or str(metadata.get("agent_id") or "") == ASSISTANT_AGENT_ID


def _assistant_actions_from_text(text: str) -> list[str]:
    actions: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        match = re.match(rf"^\s*(?:[-*]\s*)?{re.escape(ASSISTANT_ACTION_PREFIX)}\s*(/.+?)\s*$", line)
        if not match:
            continue
        action = " ".join(match.group(1).strip().split())
        if action and action not in seen:
            actions.append(action)
            seen.add(action)
        if len(actions) >= MAX_ASSISTANT_ACTIONS:
            break
    return actions


def _strip_assistant_actions(text: str) -> str:
    lines = [
        line
        for line in text.splitlines()
        if not re.match(rf"^\s*(?:[-*]\s*)?{re.escape(ASSISTANT_ACTION_PREFIX)}\s*/.+?\s*$", line)
    ]
    return "\n".join(lines).strip()


def _assistant_action_allowed(action: str) -> tuple[bool, str]:
    clean = action.strip()
    if not clean.startswith("/"):
        return False, "assistant actions must be explicit Telegram slash commands"
    command, rest = _split_command(clean)
    subcommand, _ = _split_once(rest.strip())
    lowered_sub = subcommand.lower()
    read_only = {
        "/projects",
        "/agents",
        "/tasks",
        "/sessions",
        "/current",
        "/status",
        "/list",
        "/recent",
        "/projectstate",
        "/decisions",
        "/assistant",
        "/home",
    }
    if command in read_only:
        return True, ""
    if command == "/restart":
        return (True, "") if not rest.strip() else (False, "/restart does not accept arguments")
    if command == "/use":
        return (True, "") if rest.strip() else (False, "/use requires a project, agent, or task reference")
    if command == "/project":
        if not rest.strip() or lowered_sub in {"new", "create", "use", "select"}:
            return True, ""
        return True, ""
    if command == "/task":
        return (True, "") if rest.strip() else (False, "/task requires a task reference or new task title")
    if command == "/newtask":
        return (True, "") if rest.strip() else (False, "/newtask requires a title")
    if command == "/session":
        if not rest.strip():
            return False, "/session requires a session reference, scan command, or import command"
        if lowered_sub in {"scan", "import"}:
            return True, ""
        return True, ""
    if command == "/agent":
        if not rest.strip():
            return True, ""
        if lowered_sub in {"template"}:
            return False, "/agent template is not in the assistant safe command whitelist"
        return True, ""
    return False, "command is not in the assistant safe command whitelist"


def _assistant_unverified_state_change_warning(text: str) -> str:
    clean = " ".join(_strip_assistant_actions(text).split())
    if not clean:
        return ""
    patterns = [
        r"(已|已经|currently|now)\s*.*(进入|切换|选中|选择|selected|switched|entered)\s*.*(session|任务|项目|agent|task|project)",
        r"(进入|切换到|选中|选择了)\s*.*(session|任务|项目|agent|task|project)",
    ]
    if not any(re.search(pattern, clean, flags=re.IGNORECASE) for pattern in patterns):
        return ""
    return "\n".join(
        [
            "State change was not verified.",
            "The assistant did not execute an AGENTDECK_ACTION, so AgentDeck may not have actually switched context.",
            "Use /current to confirm, or send /use session <list #> / /use task <list #> directly.",
        ]
    )


def _natural_restart_intent(text: str) -> str | None:
    clean = " ".join(text.strip().lower().split())
    if not clean or clean.startswith("/"):
        return None
    if "agentdeck" not in clean:
        return None
    if not any(word in clean for word in ("重启", "重载", "restart", "reload")):
        return None
    if any(word in clean for word in ("为什么", "原因", "怎么", "如何", "why", "how")):
        return None
    return "force" if any(word in clean for word in ("强制", "force", "forced")) else ""


def _format_memory_document(index: int, memory: MemoryDocument) -> list[str]:
    owner = f":{memory.owner}" if memory.owner else ""
    pin = "  pinned: yes" if memory.pinned else ""
    lines = [
        f"{index}. {_one_line(memory.title, 180)}",
        f"   scope: {memory.scope}{owner}  type: {memory.memory_type}{pin}",
    ]
    excerpt = _memory_excerpt(memory.content, max_chars=300)
    if excerpt:
        lines.append(f"   note: {excerpt}")
    return lines


def _parse_compact_options(rest: str, *, default_title: str) -> tuple[str, bool]:
    pinned = False
    title_tokens: list[str] = []
    for token in rest.split():
        lowered = token.lower()
        if lowered in {"--pin", "--pinned"}:
            pinned = True
            continue
        title_tokens.append(token)
    title = " ".join(title_tokens).strip() or default_title
    return title, pinned


def _memory_excerpt(value: str, *, max_chars: int) -> str:
    parts: list[str] = []
    for line in value.splitlines():
        clean = line.strip()
        if not clean or clean == "---":
            continue
        if clean.startswith("# "):
            continue
        parts.append(clean)
    return _one_line(" ".join(parts), max_chars)


def _one_line(value: str, max_chars: int) -> str:
    clean = " ".join(value.strip().split())
    if len(clean) <= max_chars:
        return clean
    return clean[: max_chars - 1].rstrip() + "..."


def _append_compact_list(lines: list[str], title: str, values: list[str], *, max_items: int) -> None:
    clean_values = [_one_line(value, 220) for value in values if _one_line(value, 220)]
    if not clean_values:
        return
    lines.append(f"{title}:")
    for value in clean_values[:max_items]:
        lines.append(f"- {value}")


def _help_text() -> str:
    return "\n".join(
        [
            "AgentDeck Telegram commands:",
            "/status",
            "/projects",
            "/project <project_id or list #>",
            "/project new <project_id> <cwd> [title]",
            "/projectstate [project]",
            "/decisions [project]",
            "/decide <decision text>",
            "/use project <project_id or list #>",
            "/agents [project]",
            "/agent <agent_id or list #>",
            "/agent new <agent_id> [adapter] [role] [title]",
            "/agent template <prompt>",
            "/agent template clear [agent]",
            "/use agent <agent_id or list #>",
            "/tasks [project]",
            "/task <task_id>",
            "/task new <task title>",
            "/newtask <task title>",
            "/use <task_id or exact task title>",
            "/use task <task_id or list #>",
            "/use session <session_id or list #>",
            "/assistant",
            "/current",
            "/restart",
            "/video <path> [caption]",
            "/list",
            "/context [task]",
            "/memories [task]",
            "/memory disable <memory #, id, title, or path>",
            "/memory enable <memory #, id, title, or path>",
            "/compact [--pin] [title]",
            "/handoffs [task]",
            "/review <manager review summary>",
            "/reviews [task]",
            "/sessions [agent]",
            "/session <session_id or list #>",
            "/session use <session_id or list #>",
            "/session scan [codex|kimi] <old cwd>",
            "/session import <scan #> [project <project>] [task <task>] [agent <agent>]",
            "/resume <session_id or list #> <message>",
            "/auto start [hours]",
            "/auto task [hours]",
            "/auto -h start [hours]",
            "/auto <hours>",
            "/auto status",
            "/auto prompt <message>",
            "/auto end",
            "plain text message  (after /use, or to assistant before /use)",
            "/run <task_id> <message>",
            "/run <message>  (after /use)",
            "/approvals [pending|approved|rejected]",
            "/approval <approval_id or list #>",
            "/approve <approval_id or list #> [note]",
            "/reject <approval_id or list #> [note]",
            "/jobs",
            "/job <job_id>",
            "/job <list #>",
            "/job resume <job_id or list #> [message]",
            "/cancel <job_id>",
            "/cancel <list #>",
        ]
    )
