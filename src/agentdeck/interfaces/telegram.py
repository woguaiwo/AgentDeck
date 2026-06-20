"""Telegram interface for AgentDeck."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from agentdeck.core.cancel import CancellationToken
from agentdeck.core.config import Workspace
from agentdeck.core.events import EventKind
from agentdeck.core.run_service import RunConfigurationError, RunRequest, run_agent_prompt
from agentdeck.storage.approvals import ApprovalRecord, ApprovalRegistry
from agentdeck.storage.jobs import JobRecord, JobRegistry
from agentdeck.storage.projects import ProjectRecord, ProjectRegistry
from agentdeck.storage.sessions import SessionRecord, SessionRegistry
from agentdeck.storage.tasks import TaskBoard, TaskRecord


MAX_TELEGRAM_MESSAGE = 3900
AUTO_CONTINUE_DELAY_SECONDS = 1.0
DEFAULT_AUTO_APPROVAL_MODE = "bypass"
HUMAN_AUTO_APPROVAL_MODE = "fail"
DEFAULT_AUTO_PROMPT = (
    "请继续推进当前任务。要求：主动完成下一步；如果取得阶段性进展、"
    "做出重要决定或遇到阻塞，请用简短要点记录到项目日志或任务备注里；"
    "如果需要用户决策、权限或外部信息，请停止并明确说明。"
)


@dataclass
class TelegramConfig:
    token: str
    allowed_chat_ids: set[int] = field(default_factory=set)
    poll_timeout: int = 30


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

    def _request(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(f"{self.base_url}/{method}", data=body)
        with urllib.request.urlopen(request, timeout=max(35, int(payload.get("timeout") or 0) + 5)) as response:
            data = json.loads(response.read().decode("utf-8"))
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data}")
        return data


class TelegramChatStateStore:
    """Persist small per-chat interface state."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self.workspace.root / "telegram" / "state.json"

    def current_task(self, chat_id: int) -> str:
        with self._lock:
            data = self._read()
            return str(data.get(str(chat_id), {}).get("current_task_id") or "")

    def set_current_task(self, chat_id: int, task_id: str) -> None:
        with self._lock:
            data = self._read()
            chat = dict(data.get(str(chat_id)) or {})
            chat["current_task_id"] = task_id
            data[str(chat_id)] = chat
            self._write(data)

    def auto_state(self, chat_id: int) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            state = data.get(str(chat_id), {}).get("auto") or {}
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
    ) -> None:
        with self._lock:
            data = self._read()
            chat = dict(data.get(str(chat_id)) or {})
            chat["auto"] = {
                "enabled": enabled,
                "task_id": task_id,
                "prompt": prompt,
                "until": until,
                "turns_started": turns_started,
                "last_job_id": last_job_id,
                "approval_mode": _normalize_auto_approval_mode(approval_mode),
            }
            data[str(chat_id)] = chat
            self._write(data)

    def disable_auto(self, chat_id: int) -> None:
        with self._lock:
            data = self._read()
            chat = dict(data.get(str(chat_id)) or {})
            state = dict(chat.get("auto") or {})
            state["enabled"] = False
            chat["auto"] = state
            data[str(chat_id)] = chat
            self._write(data)

    def mark_auto_job(self, chat_id: int, *, job_id: str, task_id: str) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            chat = dict(data.get(str(chat_id)) or {})
            state = dict(chat.get("auto") or {})
            state["last_job_id"] = job_id
            state["task_id"] = task_id or str(state.get("task_id") or "")
            state["turns_started"] = int(state.get("turns_started") or 0) + 1
            chat["auto"] = state
            data[str(chat_id)] = chat
            self._write(data)
            return state

    def set_recent(self, chat_id: int, *, task_ids: list[str], job_ids: list[str]) -> None:
        with self._lock:
            data = self._read()
            chat = dict(data.get(str(chat_id)) or {})
            chat["recent_task_ids"] = task_ids
            chat["recent_job_ids"] = job_ids
            data[str(chat_id)] = chat
            self._write(data)

    def set_recent_sessions(self, chat_id: int, session_ids: list[str]) -> None:
        with self._lock:
            data = self._read()
            chat = dict(data.get(str(chat_id)) or {})
            chat["recent_session_ids"] = session_ids
            data[str(chat_id)] = chat
            self._write(data)

    def set_recent_approvals(self, chat_id: int, approval_ids: list[str]) -> None:
        with self._lock:
            data = self._read()
            chat = dict(data.get(str(chat_id)) or {})
            chat["recent_approval_ids"] = approval_ids
            data[str(chat_id)] = chat
            self._write(data)

    def set_recent_tasks(self, chat_id: int, task_ids: list[str]) -> None:
        with self._lock:
            data = self._read()
            chat = dict(data.get(str(chat_id)) or {})
            chat["recent_task_ids"] = task_ids
            data[str(chat_id)] = chat
            self._write(data)

    def set_recent_jobs(self, chat_id: int, job_ids: list[str]) -> None:
        with self._lock:
            data = self._read()
            chat = dict(data.get(str(chat_id)) or {})
            chat["recent_job_ids"] = job_ids
            data[str(chat_id)] = chat
            self._write(data)

    def recent_task_id(self, chat_id: int, index: int) -> str:
        with self._lock:
            data = self._read()
            task_ids = data.get(str(chat_id), {}).get("recent_task_ids") or []
            if not isinstance(task_ids, list) or index < 1 or index > len(task_ids):
                return ""
            return str(task_ids[index - 1])

    def recent_job_id(self, chat_id: int, index: int) -> str:
        with self._lock:
            data = self._read()
            job_ids = data.get(str(chat_id), {}).get("recent_job_ids") or []
            if not isinstance(job_ids, list) or index < 1 or index > len(job_ids):
                return ""
            return str(job_ids[index - 1])

    def recent_session_id(self, chat_id: int, index: int) -> str:
        with self._lock:
            data = self._read()
            session_ids = data.get(str(chat_id), {}).get("recent_session_ids") or []
            if not isinstance(session_ids, list) or index < 1 or index > len(session_ids):
                return ""
            return str(session_ids[index - 1])

    def recent_approval_id(self, chat_id: int, index: int) -> str:
        with self._lock:
            data = self._read()
            approval_ids = data.get(str(chat_id), {}).get("recent_approval_ids") or []
            if not isinstance(approval_ids, list) or index < 1 or index > len(approval_ids):
                return ""
            return str(approval_ids[index - 1])

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


class TelegramJobQueue:
    """Background runner for Telegram-triggered jobs."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        sender: Callable[[int, str], None],
        runner: Callable[[Workspace, RunRequest], Any] = run_agent_prompt,
    ) -> None:
        self.workspace = workspace
        self.sender = sender
        self.runner = runner
        self.registry = JobRegistry(workspace)
        self.chat_state = TelegramChatStateStore(workspace)
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._cancellations: dict[str, CancellationToken] = {}
        self.registry.mark_unfinished_interrupted(
            interface="telegram",
            reason="AgentDeck restarted before this job finished.",
        )

    def start(
        self,
        *,
        chat_id: int,
        task_id: str = "",
        prompt: str,
        metadata: dict[str, Any] | None = None,
    ) -> JobRecord:
        with self._lock:
            job = self.registry.create(
                interface="telegram",
                chat_id=chat_id,
                task_id=task_id,
                prompt=prompt,
                metadata=metadata,
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
            return self.registry.list(interface="telegram", chat_id=chat_id, limit=limit)

    def latest_for_chat(self, chat_id: int, *, statuses: set[str] | None = None) -> JobRecord | None:
        with self._lock:
            records = self.registry.list(interface="telegram", chat_id=chat_id)
        if statuses:
            records = [record for record in records if record.status in statuses]
        return records[0] if records else None

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
            cancel_was_requested = self._cancel_was_requested(job_id)
            if cancel_event is not None:
                finished = self._finish_job(
                    job_id,
                    status="cancelled",
                    session_id=result.session_id,
                    final_text=result.final_text,
                    error=cancel_event.text or "Run cancelled.",
                )
            else:
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
            self._send(job.chat_id, _format_job_completion(finished or job, result.approval_requested))
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

    def _continue_auto_if_needed(self, job: JobRecord, approval_requested: bool) -> None:
        if not job.chat_id or not job.task_id:
            return
        state = self.chat_state.auto_state(job.chat_id)
        if not _auto_enabled_for_job(state, job):
            return
        if approval_requested:
            self._send(
                job.chat_id,
                "Auto mode paused: approval is required. Use /approvals, then /approve 1 or /reject 1.",
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
        if _auto_expired(state):
            self.chat_state.disable_auto(job.chat_id)
            self._send(job.chat_id, "Auto mode ended: timer expired.")
            return
        prompt = str(state.get("prompt") or DEFAULT_AUTO_PROMPT).strip() or DEFAULT_AUTO_PROMPT
        metadata: dict[str, Any] = {
            "auto": True,
            "auto_parent_job_id": job.job_id,
            "approval_mode": _auto_approval_mode(state),
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
    ) -> None:
        self.workspace = workspace
        self.runner = runner
        self.job_queue = job_queue
        self.chat_state = TelegramChatStateStore(workspace)

    async def handle_text(self, text: str, *, chat_id: int | None = None) -> list[str]:
        clean = text.strip()
        if not clean:
            return []
        command, rest = _split_command(clean)

        if command in {"/start", "/help", "help"}:
            return [_help_text()]
        if command in {"/projects", "projects"}:
            return [self._projects()]
        if command in {"/tasks", "tasks"}:
            return [self._tasks(rest, chat_id=chat_id)]
        if command in {"/task", "task"}:
            return [self._task(rest, chat_id=chat_id)]
        if command in {"/newtask", "newtask"}:
            return [self._newtask(rest, chat_id=chat_id)]
        if command in {"/use", "use"}:
            return [self._use(rest, chat_id=chat_id)]
        if command in {"/current", "current"}:
            return [self._current(chat_id=chat_id)]
        if command in {"/status", "status"}:
            return [self._status(chat_id=chat_id)]
        if command in {"/list", "list", "/recent", "recent"}:
            return [self._recent(chat_id=chat_id)]
        if command in {"/sessions", "sessions"}:
            return [self._sessions(rest, chat_id=chat_id)]
        if command in {"/session", "session"}:
            return [self._session(rest, chat_id=chat_id)]
        if command in {"/resume", "resume"}:
            return [await self._resume(rest, chat_id=chat_id)]
        if command in {"/auto", "auto"}:
            return [self._auto(rest, chat_id=chat_id)]
        if command in {"/ta", "ta"}:
            subcommand, subrest = _split_once(rest)
            if subcommand.lower().split("@", 1)[0] == "auto":
                return [self._auto(subrest, chat_id=chat_id)]
        if command in {"/agents", "agents"}:
            return [self._agents(rest)]
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
            return [self._job(rest, chat_id=chat_id)]
        if command in {"/cancel", "cancel"}:
            return [self._cancel(rest, chat_id=chat_id)]
        if command in {"/run", "run"}:
            return [await self._run(rest, chat_id=chat_id)]
        return [f"Unknown command: {command}\n\n{_help_text()}"]

    def _projects(self) -> str:
        records = ProjectRegistry(self.workspace).list()
        if not records:
            return "No projects."
        lines = ["Projects:"]
        for index, record in enumerate(records, 1):
            lines.append(f"{index}. {record.title}")
            lines.append(f"   id: {record.project_id}")
            lines.append(f"   agent: {record.default_agent_id}  status: {record.status}")
        return "\n".join(lines)

    def _tasks(self, rest: str, *, chat_id: int | None) -> str:
        project = rest.strip() or None
        records = TaskBoard(self.workspace).list(project_id=project)
        if not records:
            return "No tasks."
        if chat_id is not None:
            self.chat_state.set_recent_tasks(chat_id, [record.task_id for record in records])
        lines = ["Tasks:"]
        for index, record in enumerate(records, 1):
            lines.append(f"{index}. {record.title}")
            lines.append(f"   id: {record.task_id}")
            lines.append(f"   status: {record.status}  priority: {record.priority}")
            lines.append(f"   project: {record.project_id or '-'}  agent: {record.agent_id}")
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
            return "Usage: /newtask <task title>"
        project = self._default_project(chat_id=chat_id)
        record = TaskBoard(self.workspace).create(
            title=title,
            project_id=project.project_id if project is not None else "",
            agent_id=project.default_agent_id if project is not None else "owner",
            team_id=project.team_id if project is not None else "default",
        )
        if chat_id is not None:
            self.chat_state.set_current_task(chat_id, record.task_id)
        lines = [
            "Task created and selected:",
            record.title,
            f"id: {record.task_id}",
        ]
        if record.project_id:
            lines.append(f"project: {record.project_id}")
        lines.append("Now you can send /run <message>")
        return "\n".join(lines)

    def _use(self, rest: str, *, chat_id: int | None) -> str:
        if chat_id is None:
            return "This command requires a Telegram chat."
        task_ref = rest.strip()
        if not task_ref:
            return "Usage: /use <task_id or exact task title>"
        task = self._resolve_task(task_ref, chat_id=chat_id)
        if task is None:
            return f"Task not found: {task_ref}"
        self.chat_state.set_current_task(chat_id, task.task_id)
        return "\n".join(
            [
                "Current task set:",
                task.title,
                f"id: {task.task_id}",
                "Now you can send /run <message>",
            ]
        )

    def _current(self, *, chat_id: int | None) -> str:
        if chat_id is None:
            return "This command requires a Telegram chat."
        lines: list[str] = []
        current_task_id = self.chat_state.current_task(chat_id)
        task = self._resolve_task(current_task_id) if current_task_id else None
        if task is None:
            lines.append("Current task: none")
            lines.append("Use /use <task_id or task title>")
        else:
            lines.append("Current task:")
            lines.append(task.title)
            lines.append(f"id: {task.task_id}")
            lines.append(f"status: {task.status}")
        if self.job_queue is not None:
            latest = self.job_queue.latest_for_chat(chat_id)
            if latest is not None:
                lines.append("")
                lines.append("Latest job:")
                lines.append(f"id: {latest.job_id}")
                lines.append(f"status: {latest.status}")
        return "\n".join(lines)

    def _status(self, *, chat_id: int | None) -> str:
        lines: list[str] = ["Status:"]
        current_task_id = self.chat_state.current_task(chat_id) if chat_id is not None else ""
        current_task = self._resolve_task(current_task_id) if current_task_id else None
        if current_task is None:
            lines.append("Current task: none")
        else:
            lines.append(f"Current task: {current_task.title}")
            lines.append(f"Task status: {current_task.status}")

        if self.job_queue is not None and chat_id is not None:
            latest = self.job_queue.latest_for_chat(chat_id)
            if latest is None:
                lines.append("Latest job: none")
            else:
                task = self._resolve_task(latest.task_id)
                task_title = task.title if task is not None else (latest.task_id or "-")
                lines.append(f"Latest job: {latest.status}")
                lines.append(f"Job task: {task_title}")

        pending = ApprovalRegistry(self.workspace).list(status="pending")
        lines.append(f"Pending approvals: {len(pending)}")
        if chat_id is not None:
            auto_state = self.chat_state.auto_state(chat_id)
            if bool(auto_state.get("enabled")):
                lines.append(
                    "Auto: on  "
                    f"timer: {_format_auto_until(float(auto_state.get('until') or 0.0))}  "
                    f"approval: {_format_auto_approval_mode(_auto_approval_mode(auto_state))}"
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
        lines.append("Use /list, /sessions, /approvals, or /run <message>.")
        return "\n".join(lines)

    def _recent(self, *, chat_id: int | None) -> str:
        tasks = TaskBoard(self.workspace).list()[:8]
        jobs = self.job_queue.list(chat_id=chat_id, limit=8) if self.job_queue is not None else []
        sessions = SessionRegistry(self.workspace).list()[:8]
        if chat_id is not None:
            self.chat_state.set_recent(
                chat_id,
                task_ids=[task.task_id for task in tasks],
                job_ids=[job.job_id for job in jobs],
            )
            self.chat_state.set_recent_sessions(chat_id, [session.session_id for session in sessions])

        current_task_id = self.chat_state.current_task(chat_id) if chat_id is not None else ""
        lines: list[str] = ["Recent:"]
        if not tasks:
            lines.append("Tasks: none")
        else:
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
        lines.append("Use /use 1, /run 1 <message>, /job 1, /cancel 1, or /resume 1 <message>.")
        return "\n".join(lines)

    def _default_project(self, *, chat_id: int | None) -> ProjectRecord | None:
        if chat_id is not None:
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
        if chat_id is not None:
            self.chat_state.set_recent_sessions(chat_id, [record.session_id for record in records])
        lines = ["Sessions:"]
        for index, record in enumerate(records, 1):
            lines.append(f"{index}. {record.title}")
            lines.append(f"   status: {record.status}  agent: {record.agent_id}  adapter: {record.adapter}")
            if record.provider_session_id:
                lines.append(f"   provider: {record.provider_session_kind or 'session'}")
            if record.last_assistant_final:
                lines.append(f"   last: {_one_line(record.last_assistant_final, 120)}")
        lines.append("")
        lines.append("Use /session 1 or /resume 1 <message>.")
        return "\n".join(lines)

    def _session(self, rest: str, *, chat_id: int | None) -> str:
        session_ref = rest.strip()
        if not session_ref:
            return "Usage: /session <session_id, title, or number>"
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
        lines.append("Use /resume <message> if this is the current task session, or /resume 1 <message>.")
        return "\n".join(lines)

    async def _resume(self, rest: str, *, chat_id: int | None) -> str:
        if chat_id is None:
            return "This command requires a Telegram chat."
        session, prompt, error = self._resolve_resume_target(rest, chat_id=chat_id)
        if error:
            return error
        assert session is not None

        task = self._task_for_session(session.session_id)
        if task is not None:
            self.chat_state.set_current_task(chat_id, task.task_id)
        metadata = {"session_id": session.session_id}
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
            result = await self.runner(self.workspace, RunRequest(prompt=prompt, session=session.session_id))
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

        if session_ref and not maybe_prompt and self._resolve_session(session_ref, chat_id=chat_id) is not None:
            return None, "", "Usage: /resume <session number> <message>"
        return None, "", "No current resumable session. Use /sessions, then /resume 1 <message>."

    def _task_for_session(self, session_id: str) -> TaskRecord | None:
        if not session_id:
            return None
        matches = [task for task in TaskBoard(self.workspace).list() if task.session_id == session_id]
        if not matches:
            return None
        return matches[0]

    def _agents(self, rest: str) -> str:
        project = rest.strip() or None
        from agentdeck.storage.agents import AgentRegistry

        records = AgentRegistry(self.workspace).list(project_id=project)
        if not records:
            return "No agents."
        lines = ["Agents:"]
        for index, record in enumerate(records, 1):
            lines.append(f"{index}. {record.title}")
            lines.append(f"   id: {record.agent_id}  role: {record.role}")
            lines.append(f"   project: {record.project_id or '-'}  adapter: {record.adapter}")
        return "\n".join(lines)

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
        lines.append("Use /approval 1, /approve 1, or /reject 1.")
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
            )
            return f"Auto prompt updated:\n{prompt}"
        if action == "start" or action == "on":
            return self._auto_start(tail, chat_id=chat_id, approval_mode=approval_mode)
        if _looks_like_float(action):
            return self._auto_start(clean, chat_id=chat_id, approval_mode=approval_mode)
        return "Usage: /auto start [hours], /auto <hours>, /auto -h start, /auto status, /auto prompt <message>, or /auto end"

    def _auto_start(self, rest: str, *, chat_id: int, approval_mode: str) -> str:
        task_id = self.chat_state.current_task(chat_id)
        task = self._resolve_task(task_id) if task_id else None
        if task is None:
            return "No current task. Use /use <task_id or title>, then /auto start."
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
        prompt = prompt_override.strip() or str(state.get("prompt") or DEFAULT_AUTO_PROMPT)
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
        )
        lines = [
            "Auto mode enabled.",
            f"task: {task.title}",
            f"timer: {_format_auto_until(until)}",
            f"approval: {_format_auto_approval_mode(normalized_approval_mode)}",
        ]
        if self.job_queue is None:
            lines.append("Jobs are not enabled for this interface.")
            return "\n".join(lines)

        active = self.job_queue.latest_for_chat(chat_id, statuses={"queued", "running", "cancel_requested"})
        if active is not None:
            lines.append(f"active job: {active.job_id}")
            lines.append("Auto will continue after the active job finishes.")
            return "\n".join(lines)

        metadata = {"auto": True, "approval_mode": normalized_approval_mode}
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
        return value.strip()


class TelegramServer:
    def __init__(self, workspace: Workspace, api: TelegramBotApi, config: TelegramConfig) -> None:
        self.workspace = workspace
        self.api = api
        self.config = config
        self.job_queue = TelegramJobQueue(workspace, sender=api.send_message)
        self.handler = TelegramCommandHandler(workspace, job_queue=self.job_queue)

    def serve_forever(self, *, once: bool = False) -> None:
        offset: int | None = None
        while True:
            updates = self.api.get_updates(offset=offset, timeout=self.config.poll_timeout)
            for update in updates:
                offset = int(update.get("update_id", 0)) + 1
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
            return
        try:
            replies = asyncio.run(self.handler.handle_text(text, chat_id=chat_id))
        except Exception as exc:  # keep polling even if one command fails
            replies = [f"AgentDeck error: {exc}"]
        for reply in replies:
            if reply:
                self.api.send_message(chat_id, reply)


def config_from_env(token: str | None = None, allowed_chat_ids: list[str] | None = None, poll_timeout: int = 30) -> TelegramConfig:
    resolved_token = _normalize_bot_token(token or os.environ.get("AGENTDECK_TELEGRAM_TOKEN") or "")
    allowed = set(_parse_allowed_chat_ids(allowed_chat_ids or []))
    allowed.update(_parse_allowed_chat_ids((os.environ.get("AGENTDECK_TELEGRAM_ALLOWED_CHATS") or "").split(",")))
    return TelegramConfig(token=resolved_token, allowed_chat_ids=allowed, poll_timeout=poll_timeout)


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


def _format_auto_approval_mode(value: str) -> str:
    return "human" if _normalize_auto_approval_mode(value) == HUMAN_AUTO_APPROVAL_MODE else "auto"


def _safe_session_id_for_resume(workspace: Workspace, session_id: str) -> str:
    session = SessionRegistry(workspace).get(session_id) if session_id else None
    if session is None:
        return ""
    if session.adapter in {"codex", "codex-exec", "kimi", "kimi-print"} and not session.provider_session_id:
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


def _format_job_completion(job: JobRecord, approval_requested: bool) -> str:
    heading = "Job cancelled" if job.status == "cancelled" else "Job done"
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
        lines.append(job.final_text or "Run cancelled.")
    else:
        lines.append(job.final_text or "Run finished without a final text response.")
    return "\n".join(lines)


def _one_line(value: str, max_chars: int) -> str:
    clean = " ".join(value.strip().split())
    if len(clean) <= max_chars:
        return clean
    return clean[: max_chars - 1].rstrip() + "..."


def _help_text() -> str:
    return "\n".join(
        [
            "AgentDeck Telegram commands:",
            "/status",
            "/projects",
            "/agents [project]",
            "/tasks [project]",
            "/task <task_id>",
            "/newtask <task title>",
            "/use <task_id or exact task title>",
            "/current",
            "/list",
            "/sessions [agent]",
            "/session <session_id or 1>",
            "/resume <session_id or 1> <message>",
            "/auto start [hours]",
            "/auto -h start [hours]",
            "/auto <hours>",
            "/auto status",
            "/auto end",
            "/run <task_id> <message>",
            "/run <message>  (after /use)",
            "/approvals [pending|approved|rejected]",
            "/approval <approval_id or 1>",
            "/approve <approval_id or 1> [note]",
            "/reject <approval_id or 1> [note]",
            "/jobs",
            "/job <job_id>",
            "/job 1",
            "/cancel <job_id>",
            "/cancel 1",
        ]
    )
