"""Telegram interface for AgentDeck."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from agentdeck.core.config import Workspace
from agentdeck.core.run_service import RunConfigurationError, RunRequest, run_agent_prompt
from agentdeck.storage.approvals import ApprovalRegistry
from agentdeck.storage.projects import ProjectRegistry
from agentdeck.storage.tasks import TaskBoard


MAX_TELEGRAM_MESSAGE = 3900


@dataclass
class TelegramConfig:
    token: str
    allowed_chat_ids: set[int] = field(default_factory=set)
    poll_timeout: int = 30


@dataclass
class TelegramJob:
    job_id: str
    chat_id: int
    task_id: str
    prompt: str
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    session_id: str = ""
    final_text: str = ""
    error: str = ""


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


class TelegramJobQueue:
    """In-memory background runner for Telegram-triggered jobs."""

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
        self._lock = threading.Lock()
        self._jobs: dict[str, TelegramJob] = {}
        self._threads: dict[str, threading.Thread] = {}

    def start(self, *, chat_id: int, task_id: str, prompt: str) -> TelegramJob:
        job = TelegramJob(job_id=_new_job_id(), chat_id=chat_id, task_id=task_id, prompt=prompt)
        thread = threading.Thread(target=self._run_job, args=(job.job_id,), daemon=True)
        with self._lock:
            self._jobs[job.job_id] = job
            self._threads[job.job_id] = thread
        thread.start()
        return job

    def get(self, job_id: str) -> TelegramJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return _copy_job(job) if job is not None else None

    def list(self, *, limit: int = 10) -> list[TelegramJob]:
        with self._lock:
            jobs = [_copy_job(job) for job in self._jobs.values()]
        return sorted(jobs, key=lambda item: item.updated_at, reverse=True)[:limit]

    def wait(self, job_id: str, *, timeout: float | None = None) -> TelegramJob | None:
        with self._lock:
            thread = self._threads.get(job_id)
        if thread is not None:
            thread.join(timeout)
        return self.get(job_id)

    def _run_job(self, job_id: str) -> None:
        job = self._set_job_status(job_id, "running")
        if job is None:
            return
        try:
            result = asyncio.run(self.runner(self.workspace, RunRequest(prompt=job.prompt, task=job.task_id)))
        except RunConfigurationError as exc:
            self._finish_job(job_id, status="error", error=str(exc))
            self._send(job.chat_id, f"Job failed: {job_id}\n{exc}")
        except Exception as exc:  # keep the polling loop alive even if a background job crashes
            self._finish_job(job_id, status="error", error=str(exc))
            self._send(job.chat_id, f"Job failed: {job_id}\n{exc}")
        else:
            finished = self._finish_job(
                job_id,
                status="done",
                session_id=result.session_id,
                final_text=result.final_text,
            )
            self._send(job.chat_id, _format_job_completion(finished or job, result.approval_requested))

    def _set_job_status(self, job_id: str, status: str) -> TelegramJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            job.status = status
            job.updated_at = time.time()
            return _copy_job(job)

    def _finish_job(
        self,
        job_id: str,
        *,
        status: str,
        session_id: str = "",
        final_text: str = "",
        error: str = "",
    ) -> TelegramJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            job.status = status
            job.updated_at = time.time()
            job.session_id = session_id
            job.final_text = final_text
            job.error = error
            return _copy_job(job)

    def _send(self, chat_id: int, text: str) -> None:
        try:
            self.sender(chat_id, text)
        except Exception:
            pass


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
            return [self._tasks(rest)]
        if command in {"/task", "task"}:
            return [self._task(rest)]
        if command in {"/agents", "agents"}:
            return [self._agents(rest)]
        if command in {"/approvals", "approvals"}:
            return [self._approvals(rest)]
        if command in {"/approval", "approval"}:
            return [self._approval(rest)]
        if command in {"/approve", "approve"}:
            return [self._resolve_approval(rest, "approved")]
        if command in {"/reject", "reject"}:
            return [self._resolve_approval(rest, "rejected")]
        if command in {"/jobs", "jobs"}:
            return [self._jobs()]
        if command in {"/job", "job"}:
            return [self._job(rest)]
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

    def _tasks(self, rest: str) -> str:
        project = rest.strip() or None
        records = TaskBoard(self.workspace).list(project_id=project)
        if not records:
            return "No tasks."
        lines = ["Tasks:"]
        for index, record in enumerate(records, 1):
            lines.append(f"{index}. {record.title}")
            lines.append(f"   id: {record.task_id}")
            lines.append(f"   status: {record.status}  priority: {record.priority}")
            lines.append(f"   project: {record.project_id or '-'}  agent: {record.agent_id}")
        return "\n".join(lines)

    def _task(self, rest: str) -> str:
        task_id = rest.strip()
        if not task_id:
            return "Usage: /task <task_id>"
        record = TaskBoard(self.workspace).resolve(task_id)
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

    def _approvals(self, rest: str) -> str:
        status = rest.strip() or "pending"
        try:
            records = ApprovalRegistry(self.workspace).list(status=status)
        except ValueError as exc:
            return str(exc)
        if not records:
            return f"No {status} approvals."
        lines = [f"Approvals ({status}):"]
        for index, record in enumerate(records, 1):
            lines.append(f"{index}. {record.title}")
            lines.append(f"   id: {record.approval_id}")
            lines.append(f"   task: {record.task_id or '-'}  agent: {record.agent_id}")
            lines.append(f"   provider: {record.provider or record.adapter or '-'}")
        return "\n".join(lines)

    def _approval(self, rest: str) -> str:
        approval_id = rest.strip()
        if not approval_id:
            return "Usage: /approval <approval_id>"
        record = ApprovalRegistry(self.workspace).resolve(approval_id)
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

    def _resolve_approval(self, rest: str, status: str) -> str:
        approval_id, note = _split_once(rest.strip())
        if not approval_id:
            verb = "approve" if status == "approved" else "reject"
            return f"Usage: /{verb} <approval_id> [note]"
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
        return f"Approval {record.status}: {record.title}\nid: {record.approval_id}"

    async def _run(self, rest: str, *, chat_id: int | None = None) -> str:
        task_id, prompt = _split_once(rest.strip())
        if not task_id or not prompt:
            return "Usage: /run <task_id> <message>"
        if self.job_queue is not None and chat_id is not None:
            job = self.job_queue.start(chat_id=chat_id, task_id=task_id, prompt=prompt)
            return "\n".join(
                [
                    f"Job started: {job.job_id}",
                    f"task: {job.task_id}",
                    "status: queued",
                    f"Use /job {job.job_id}",
                ]
            )
        try:
            result = await self.runner(self.workspace, RunRequest(prompt=prompt, task=task_id))
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

    def _jobs(self) -> str:
        if self.job_queue is None:
            return "Jobs are not enabled for this interface."
        records = self.job_queue.list()
        if not records:
            return "No jobs."
        lines = ["Jobs:"]
        for index, job in enumerate(records, 1):
            lines.append(f"{index}. {job.job_id}")
            lines.append(f"   status: {job.status}  task: {job.task_id}")
            if job.session_id:
                lines.append(f"   session: {job.session_id}")
            if job.error:
                lines.append(f"   error: {_one_line(job.error, 120)}")
        return "\n".join(lines)

    def _job(self, rest: str) -> str:
        if self.job_queue is None:
            return "Jobs are not enabled for this interface."
        job_id = rest.strip()
        if not job_id:
            return "Usage: /job <job_id>"
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


def _new_job_id() -> str:
    return f"job-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _copy_job(job: TelegramJob | None) -> TelegramJob | None:
    if job is None:
        return None
    return TelegramJob(
        job_id=job.job_id,
        chat_id=job.chat_id,
        task_id=job.task_id,
        prompt=job.prompt,
        status=job.status,
        created_at=job.created_at,
        updated_at=job.updated_at,
        session_id=job.session_id,
        final_text=job.final_text,
        error=job.error,
    )


def _format_job_completion(job: TelegramJob, approval_requested: bool) -> str:
    lines = [
        f"Job done: {job.job_id}",
        f"task: {job.task_id}",
    ]
    if job.session_id:
        lines.append(f"session: {job.session_id}")
    if approval_requested:
        lines.append("approval: required")
        lines.append("Use /approvals")
    lines.append("")
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
            "/projects",
            "/agents [project]",
            "/tasks [project]",
            "/task <task_id>",
            "/run <task_id> <message>",
            "/approvals [pending|approved|rejected]",
            "/approval <approval_id>",
            "/approve <approval_id> [note]",
            "/reject <approval_id> [note]",
            "/jobs",
            "/job <job_id>",
        ]
    )
