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

from agentdeck.adapters.capabilities import adapter_requires_provider_session
from agentdeck.core.cancel import CancellationToken
from agentdeck.core.config import Workspace
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
from agentdeck.storage.projects import ProjectRecord, ProjectRegistry
from agentdeck.storage.project_state import ProjectStateStore
from agentdeck.storage.session_state import SessionStateStore
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
AUTO_TASK_DONE_MARKER = "AGENTDECK_AUTO_TASK_DONE"
DEFAULT_AUTO_TASK_PROMPT = (
    "请自动推进当前任务。每一轮主动完成下一步，并记录重要进展、决定、证据或阻塞。"
    "如果你判断当前 task 的范围和细节已经基本充分完成，请在回复最后单独一行输出 "
    f"{AUTO_TASK_DONE_MARKER}，并给出简短完成摘要；否则不要输出这个标记，继续说明下一步。"
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

    def current_project(self, chat_id: int) -> str:
        with self._lock:
            data = self._read()
            return str(data.get(str(chat_id), {}).get("current_project_id") or "")

    def set_current_project(self, chat_id: int, project_id: str) -> None:
        with self._lock:
            data = self._read()
            chat = dict(data.get(str(chat_id)) or {})
            chat["current_project_id"] = project_id
            data[str(chat_id)] = chat
            self._write(data)

    def current_agent(self, chat_id: int) -> str:
        with self._lock:
            data = self._read()
            return str(data.get(str(chat_id), {}).get("current_agent_id") or "")

    def set_current_agent(self, chat_id: int, agent_id: str) -> None:
        with self._lock:
            data = self._read()
            chat = dict(data.get(str(chat_id)) or {})
            chat["current_agent_id"] = agent_id
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
        mode: str = "loop",
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
                "mode": _normalize_auto_mode(mode),
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

    def set_recent_projects(self, chat_id: int, project_ids: list[str]) -> None:
        with self._lock:
            data = self._read()
            chat = dict(data.get(str(chat_id)) or {})
            chat["recent_project_ids"] = project_ids
            data[str(chat_id)] = chat
            self._write(data)

    def set_recent_agents(self, chat_id: int, agent_ids: list[str]) -> None:
        with self._lock:
            data = self._read()
            chat = dict(data.get(str(chat_id)) or {})
            chat["recent_agent_ids"] = agent_ids
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

    def set_recent_memories(self, chat_id: int, memory_paths: list[str]) -> None:
        with self._lock:
            data = self._read()
            chat = dict(data.get(str(chat_id)) or {})
            chat["recent_memory_paths"] = memory_paths
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

    def recent_project_id(self, chat_id: int, index: int) -> str:
        with self._lock:
            data = self._read()
            project_ids = data.get(str(chat_id), {}).get("recent_project_ids") or []
            if not isinstance(project_ids, list) or index < 1 or index > len(project_ids):
                return ""
            return str(project_ids[index - 1])

    def recent_agent_id(self, chat_id: int, index: int) -> str:
        with self._lock:
            data = self._read()
            agent_ids = data.get(str(chat_id), {}).get("recent_agent_ids") or []
            if not isinstance(agent_ids, list) or index < 1 or index > len(agent_ids):
                return ""
            return str(agent_ids[index - 1])

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

    def recent_memory_path(self, chat_id: int, index: int) -> str:
        with self._lock:
            data = self._read()
            memory_paths = data.get(str(chat_id), {}).get("recent_memory_paths") or []
            if not isinstance(memory_paths, list) or index < 1 or index > len(memory_paths):
                return ""
            return str(memory_paths[index - 1])

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
        if command in {"/current", "current"}:
            return [self._current(chat_id=chat_id)]
        if command in {"/status", "status"}:
            return [self._status(chat_id=chat_id)]
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
        return AgentRegistry(self.workspace).resolve(ASSISTANT_AGENT_ID) is not None

    async def _plain_text(self, text: str, *, chat_id: int | None) -> str:
        if chat_id is None:
            return "Plain text messages require a Telegram chat. Use /run <message>."
        current_task_id = self.chat_state.current_task(chat_id)
        task = self._resolve_task(current_task_id) if current_task_id else None
        if task is None:
            assistant = AgentRegistry(self.workspace).resolve(ASSISTANT_AGENT_ID)
            if assistant is not None:
                return await self._run_assistant(text, assistant=assistant, chat_id=chat_id)
            return self._plain_text_setup_hint()
        return await self._run(text, chat_id=chat_id)

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
        lines = [result.final_text or "Assistant run finished without a final text response.", ""]
        lines.append(f"session: {result.session_id}")
        return "\n".join(lines)

    def _plain_text_setup_hint(self) -> str:
        return "\n".join(
            [
                "No current task is selected, so this message was not sent to an agent.",
                "Use /tasks and /use task <ref>, or create one with /task new <title>.",
                "Or configure a default assistant with: agentdeck assistant setup --adapter <echo|codex|kimi>",
            ]
        )

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
        if lowered_kind in {"task", "todo"}:
            task_ref = value.strip()
        else:
            task_ref = rest.strip()
        if not task_ref:
            return "Usage: /use <task>, /use project <project>, or /use agent <agent>"
        task = self._resolve_task(task_ref, chat_id=chat_id)
        if task is None:
            return f"Task not found: {task_ref}"
        self.chat_state.set_current_task(chat_id, task.task_id)
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
        if task is None:
            lines.append("Current task: none")
            lines.append("Use /use task <task>, /tasks, or /task new <title>")
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
        if current_task is None:
            lines.append("Current task: -")
        else:
            lines.append(f"Current task: {current_task.title}")
            lines.append(f"Task status: {current_task.status}  priority: {current_task.priority}")

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
        lines.append("Use /session <list #> or /resume <list #> <message>.")
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
        lines.append("Use /resume <message> if this is the current task session, or /resume <list #> <message>.")
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
        return None, "", "No current resumable session. Use /sessions, then /resume <list #> <message>."

    def _task_for_session(self, session_id: str) -> TaskRecord | None:
        if not session_id:
            return None
        matches = [task for task in TaskBoard(self.workspace).list() if task.session_id == session_id]
        if not matches:
            return None
        return matches[0]

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


def _consume_optional_choice(text: str, choices: set[str], *, default: str) -> tuple[str, str]:
    first, rest = _split_once(text)
    if first.lower() in choices:
        return first.lower(), rest
    return default, text.strip()


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
        lines.append(_strip_auto_task_marker(job.final_text) or "Run cancelled.")
    else:
        lines.append(_strip_auto_task_marker(job.final_text) or "Run finished without a final text response.")
    return "\n".join(lines)


def _auto_task_completed(text: str) -> bool:
    return AUTO_TASK_DONE_MARKER in text


def _strip_auto_task_marker(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip() != AUTO_TASK_DONE_MARKER]
    return "\n".join(lines).strip()


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
            "/current",
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
            "/cancel <job_id>",
            "/cancel <list #>",
        ]
    )
