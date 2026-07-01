"""Local web console for AgentDeck."""

from __future__ import annotations

import html
import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from agentdeck.core.cancel import CancellationToken
from agentdeck.core.config import Workspace
from agentdeck.core.error_daemon import (
    ErrorHandlingDaemon,
    create_error_incident_for_job,
    event_should_fail_job,
    first_error_event,
    record_nonfatal_incident_for_job,
)
from agentdeck.core.run_service import RunConfigurationError, RunRequest, run_agent_prompt
from agentdeck.interfaces.telegram import AUTO_TASK_DONE_MARKER
from agentdeck.storage.admin import AdminMutationError, delete_project, rename_global_id, restore_project
from agentdeck.storage.agents import ASSISTANT_AGENT_ID, AgentRegistry
from agentdeck.storage.approvals import ApprovalRegistry
from agentdeck.storage.clones import CloneStore
from agentdeck.storage.directories import DirectoryRegistry
from agentdeck.storage.experience import ExperienceStore
from agentdeck.storage.focus import FocusRegistry
from agentdeck.storage.jobs import JobRegistry
from agentdeck.storage.projects import ProjectRegistry
from agentdeck.storage.sessions import SessionRegistry
from agentdeck.storage.tasks import TASK_STATUSES, TaskBoard
from agentdeck.storage.telegram_bots import current_server_id


DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8765
DEFAULT_WEB_AUTO_PROMPT = (
    "请继续推进当前任务。要求：主动完成下一步；如果取得阶段性进展、"
    "做出重要决定或遇到阻塞，请用简短要点记录到项目日志或任务备注里；"
    "如果需要用户决策、权限或外部信息，请停止并明确说明。"
)


@dataclass(frozen=True)
class DashboardLimits:
    projects: int = 12
    directories: int = 12
    focus: int = 18
    tasks: int = 18
    agents: int = 12
    workers: int = 12
    sessions: int = 12
    jobs: int = 12
    approvals: int = 12
    experience_collections: int = 12
    experience_events: int = 12
    clones: int = 12


@dataclass(frozen=True)
class WebResponse:
    status: int
    content_type: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


class WebJobQueue:
    """Small in-process background runner for Web-triggered jobs."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.registry = JobRegistry(workspace)
        self._lock = threading.Lock()
        self._tokens: dict[str, CancellationToken] = {}

    def start(
        self,
        *,
        prompt: str,
        task_id: str = "",
        agent_id: str = "",
        approval_mode: str = "fail",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        clean_prompt = prompt.strip()
        if not clean_prompt:
            raise ValueError("prompt is required")
        record = self.registry.create(
            interface="web",
            chat_id=0,
            task_id=task_id.strip(),
            prompt=clean_prompt,
            metadata={
                "agent_id": agent_id.strip(),
                "approval_mode": approval_mode.strip() or "fail",
                **dict(metadata or {}),
            },
        )
        token = CancellationToken()
        with self._lock:
            self._tokens[record.job_id] = token
        thread = threading.Thread(target=self._run_job, args=(record.job_id,), daemon=True)
        thread.start()
        return record.job_id

    def cancel(self, job_id: str) -> bool:
        record = self.registry.cancel(job_id, reason="Cancellation requested from Web.")
        if record is None:
            return False
        with self._lock:
            token = self._tokens.get(job_id)
        if token is not None:
            token.cancel("Cancellation requested from Web.")
        return True

    def set_auto(
        self,
        *,
        task_id: str,
        enabled: bool,
        prompt: str = DEFAULT_WEB_AUTO_PROMPT,
        mode: str = "loop",
        approval_mode: str = "bypass",
    ) -> None:
        states = _read_web_auto_states(self.workspace)
        if enabled:
            states[task_id] = {
                "enabled": True,
                "task_id": task_id,
                "prompt": prompt.strip() or DEFAULT_WEB_AUTO_PROMPT,
                "mode": "task" if mode == "task" else "loop",
                "approval_mode": approval_mode.strip() or "bypass",
                "updated_at": time.time(),
            }
        else:
            states.pop(task_id, None)
        _write_web_auto_states(self.workspace, states)

    def auto_state(self, task_id: str) -> dict[str, Any]:
        return dict(_read_web_auto_states(self.workspace).get(task_id) or {})

    def _run_job(self, job_id: str) -> None:
        job = self.registry.get(job_id)
        if job is None:
            return
        if job.status == "cancelled":
            return
        self.registry.set_status(job.job_id, "running")
        metadata = dict(job.metadata or {})
        with self._lock:
            token = self._tokens.get(job.job_id)
        try:
            result = asyncio.run(
                run_agent_prompt(
                    self.workspace,
                    RunRequest(
                        prompt=job.prompt,
                        task=job.task_id or None,
                        agent=str(metadata.get("agent_id") or "") or None,
                        approval_mode=str(metadata.get("approval_mode") or "fail"),
                        cancellation=token,
                    ),
                )
            )
            status = "done"
            if token is not None and token.is_cancelled():
                status = "cancelled"
            error_event = first_error_event(result.events)
            if status == "done" and event_should_fail_job(error_event):
                assert error_event is not None
                incident = create_error_incident_for_job(self.workspace, job=job, event=error_event, adapter=result.adapter)
                self.registry.finish(
                    job.job_id,
                    status="error",
                    session_id=result.session_id,
                    final_text=result.final_text,
                    error=error_event.text or "Backend adapter error.",
                )
                ErrorHandlingDaemon(self.workspace).process_incident(incident)
                return
            if error_event is not None:
                incident = create_error_incident_for_job(self.workspace, job=job, event=error_event, adapter=result.adapter)
                record_nonfatal_incident_for_job(self.workspace, incident)
            self.registry.finish(job.job_id, status=status, session_id=result.session_id, final_text=result.final_text)
            if status == "done" and not event_should_fail_job(error_event):
                self._continue_auto_if_needed(job, result.final_text, approval_requested=result.approval_requested)
        except RunConfigurationError as exc:
            self.registry.finish(job.job_id, status="error", error=str(exc))
            self.set_auto(task_id=job.task_id, enabled=False)
        except Exception as exc:  # pragma: no cover - defensive boundary for background thread
            self.registry.finish(job.job_id, status="error", error=str(exc))
            self.set_auto(task_id=job.task_id, enabled=False)
        finally:
            with self._lock:
                self._tokens.pop(job.job_id, None)

    def _continue_auto_if_needed(self, job: Any, final_text: str, *, approval_requested: bool) -> None:
        if not job.task_id or approval_requested:
            if job.task_id:
                self.set_auto(task_id=job.task_id, enabled=False)
            return
        state = self.auto_state(job.task_id)
        if not bool(state.get("enabled")):
            return
        if str(state.get("mode") or "loop") == "task" and AUTO_TASK_DONE_MARKER in final_text:
            TaskBoard(self.workspace).set_status(job.task_id, "review", note="Auto-by-task completed from Web.")
            self.set_auto(task_id=job.task_id, enabled=False)
            return

        def delayed_start() -> None:
            current = self.auto_state(job.task_id)
            if not bool(current.get("enabled")):
                return
            try:
                self.start(
                    prompt=str(current.get("prompt") or DEFAULT_WEB_AUTO_PROMPT),
                    task_id=job.task_id,
                    approval_mode=str(current.get("approval_mode") or "bypass"),
                    metadata={"auto": True, "auto_parent_job_id": job.job_id, "auto_mode": str(current.get("mode") or "loop")},
                )
            except Exception:
                self.set_auto(task_id=job.task_id, enabled=False)

        threading.Timer(1.0, delayed_start).start()


def build_dashboard_snapshot(workspace: Workspace, *, limits: DashboardLimits | None = None) -> dict[str, Any]:
    """Build a compact read-only snapshot for the browser console and JSON API."""

    limits = limits or DashboardLimits()
    projects = ProjectRegistry(workspace).list()
    directories = DirectoryRegistry(workspace).list()
    focus_records = FocusRegistry(workspace).list()
    tasks = TaskBoard(workspace).list()
    agents = AgentRegistry(workspace).list()
    sessions = SessionRegistry(workspace).list()
    jobs = JobRegistry(workspace).list(limit=max(limits.jobs, 1))
    approvals = ApprovalRegistry(workspace).list()
    experience_store = ExperienceStore(workspace)
    experience_collections = experience_store.list_collections()
    experience_events = experience_store.list_events(limit=max(limits.experience_events, 1))
    clones = CloneStore(workspace).list()

    task_counts = {status: 0 for status in sorted(TASK_STATUSES)}
    for task in tasks:
        task_counts[task.status] = task_counts.get(task.status, 0) + 1

    job_counts: dict[str, int] = {}
    for job in jobs:
        job_counts[job.status] = job_counts.get(job.status, 0) + 1

    pending_approvals = [approval for approval in approvals if approval.status == "pending"]
    active_jobs = [job for job in jobs if job.status in {"queued", "running", "cancel_requested"}]
    active_focus = [focus for focus in focus_records if focus.status not in {"done"}]
    active_tasks = [task for task in tasks if task.status != "done"]

    auto_states = _read_web_auto_states(workspace)
    return {
        "generated_at": time.time(),
        "generated_at_text": _format_timestamp(time.time()),
        "workspace": str(workspace.root),
        "server_id": current_server_id(),
        "counts": {
            "projects": len(projects),
            "active_projects": len([project for project in projects if project.status == "active"]),
            "directories": len(directories),
            "focus": len(focus_records),
            "active_focus": len(active_focus),
            "tasks": len(tasks),
            "active_tasks": len(active_tasks),
            "agents": len(agents),
            "workers": len(sessions),
            "sessions": len(sessions),
            "jobs": len(jobs),
            "active_jobs": len(active_jobs),
            "pending_approvals": len(pending_approvals),
            "experience_collections": len(experience_collections),
            "experience_events": len(experience_events),
            "clones": len(clones),
        },
        "task_counts": task_counts,
        "job_counts": job_counts,
        "projects": [_project_summary(project, tasks, focus_records, directories) for project in projects[: limits.projects]],
        "directories": [_directory_summary(directory) for directory in directories[: limits.directories]],
        "focus": [_focus_summary(focus) for focus in focus_records[: limits.focus]],
        "tasks": [_task_summary(task, auto_states=auto_states) for task in tasks[: limits.tasks]],
        "agents": [_agent_summary(agent) for agent in agents[: limits.agents]],
        "workers": [_worker_summary(session, focus_records, directories) for session in sessions[: limits.workers]],
        "sessions": [_session_summary(session) for session in sessions[: limits.sessions]],
        "jobs": [_job_summary(job) for job in jobs[: limits.jobs]],
        "approvals": [_approval_summary(approval) for approval in approvals[: limits.approvals]],
        "experience_collections": [
            _experience_collection_summary(collection, experience_store)
            for collection in experience_collections[: limits.experience_collections]
        ],
        "experience_events": [_experience_event_summary(event) for event in experience_events[: limits.experience_events]],
        "clones": [_clone_summary(clone) for clone in clones[: limits.clones]],
    }


def build_web_response(workspace: Workspace, path: str) -> WebResponse:
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    notice = _first(query, "notice")
    error = _first(query, "error")
    if parsed.path in {"", "/"}:
        return WebResponse(
            HTTPStatus.OK,
            "text/html; charset=utf-8",
            render_dashboard_html(build_dashboard_snapshot(workspace), notice=notice, error=error).encode("utf-8"),
        )
    if parsed.path.startswith("/tasks/"):
        task_id = parsed.path.removeprefix("/tasks/").strip("/")
        task = TaskBoard(workspace).resolve(task_id)
        if task is None:
            return _json_response({"error": "task not found"}, status=HTTPStatus.NOT_FOUND)
        return WebResponse(
            HTTPStatus.OK,
            "text/html; charset=utf-8",
            render_task_detail_html(workspace, task.to_dict(), notice=notice, error=error).encode("utf-8"),
        )
    if parsed.path == "/api/overview":
        return _json_response(build_dashboard_snapshot(workspace))
    if parsed.path == "/api/health":
        return _json_response(
            {
                "ok": True,
                "workspace": str(workspace.root),
                "server_id": current_server_id(),
                "generated_at": time.time(),
            }
        )
    return _json_response({"error": "not found"}, status=HTTPStatus.NOT_FOUND)


def handle_web_action(workspace: Workspace, path: str, form: dict[str, str], *, queue: WebJobQueue | None = None) -> WebResponse:
    queue = queue or WebJobQueue(workspace)
    try:
        if path == "/actions/projects/create":
            record = ProjectRegistry(workspace).upsert(
                project_id=form.get("project_id", ""),
                title=form.get("title") or None,
                project_dir=form.get("cwd") or ".",
                team_id=form.get("team") or form.get("project_id", ""),
                default_agent_id=form.get("default_agent") or "owner",
                replace=False,
            )
            return _redirect(f"created project {record.project_id}")
        if path in {"/actions/projects/archive", "/actions/projects/delete"}:
            result = delete_project(workspace, form.get("project_id", ""))
            return _redirect(result.message)
        if path == "/actions/projects/restore":
            result = restore_project(workspace, form.get("project_id", ""))
            return _redirect(result.message)
        if path == "/actions/rename":
            result = rename_global_id(
                workspace,
                entity=form.get("entity", ""),
                old_id=form.get("old_id", ""),
                new_id=form.get("new_id", ""),
            )
            return _redirect(result.message)
        if path == "/actions/approvals/resolve":
            approval = form.get("approval_id", "")
            status = form.get("status", "")
            if status not in {"approved", "rejected"}:
                raise ValueError("status must be approved or rejected")
            record = ApprovalRegistry(workspace).resolve_request(
                approval,
                status=status,
                resolved_by="web",
                note=form.get("note", ""),
            )
            if record is None:
                raise ValueError(f"approval not found: {approval}")
            return _redirect(f"{status} approval {record.approval_id}")
        if path == "/actions/jobs/cancel":
            if not queue.cancel(form.get("job_id", "")):
                raise ValueError("job not found")
            return _redirect(f"cancel requested for {form.get('job_id', '')}")
        if path == "/actions/run":
            agent_id = form.get("agent_id", "")
            if form.get("assistant") == "1" and not agent_id:
                agent_id = ASSISTANT_AGENT_ID
            job_id = queue.start(
                prompt=form.get("prompt", ""),
                task_id=form.get("task_id", ""),
                agent_id=agent_id,
                approval_mode=form.get("approval_mode", "fail"),
            )
            return _redirect(f"started job {job_id}")
        if path == "/actions/auto/start":
            task_id = form.get("task_id", "").strip()
            if not task_id:
                raise ValueError("task_id is required")
            mode = form.get("mode", "loop")
            prompt = form.get("prompt", "") or DEFAULT_WEB_AUTO_PROMPT
            queue.set_auto(task_id=task_id, enabled=True, prompt=prompt, mode=mode, approval_mode=form.get("approval_mode", "bypass"))
            job_id = queue.start(
                prompt=prompt,
                task_id=task_id,
                approval_mode=form.get("approval_mode", "bypass"),
                metadata={"auto": True, "auto_mode": mode},
            )
            return _redirect(f"auto started for {task_id}; job {job_id}")
        if path == "/actions/auto/stop":
            task_id = form.get("task_id", "").strip()
            if not task_id:
                raise ValueError("task_id is required")
            queue.set_auto(task_id=task_id, enabled=False)
            return _redirect(f"auto stopped for {task_id}")
    except (AdminMutationError, ValueError) as exc:
        return _redirect(str(exc), error=True)
    except Exception as exc:  # pragma: no cover - HTTP action guard
        return _redirect(f"action failed: {exc}", error=True)
    return _redirect("unknown action", error=True)


def make_web_handler(workspace: Workspace) -> type[BaseHTTPRequestHandler]:
    """Create an HTTP handler bound to one AgentDeck workspace."""

    queue = WebJobQueue(workspace)

    class AgentDeckWebHandler(BaseHTTPRequestHandler):
        server_version = "AgentDeckWeb/0.1"

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            self._send_response(build_web_response(workspace, self.path))

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            form = {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}
            self._send_response(handle_web_action(workspace, urlparse(self.path).path, form, queue=queue))

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_response(self, response: WebResponse) -> None:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Cache-Control", "no-store")
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            self.wfile.write(response.body)

    return AgentDeckWebHandler


def serve_web(workspace: Workspace, *, host: str = DEFAULT_WEB_HOST, port: int = DEFAULT_WEB_PORT) -> None:
    """Run the local web console until interrupted."""

    workspace.ensure()
    server = ThreadingHTTPServer((host, port), make_web_handler(workspace))
    address = server.server_address
    url = f"http://{address[0]}:{address[1]}"
    print(f"AgentDeck web console: {url}")
    print(f"workspace: {workspace.root}")
    try:
        server.serve_forever()
    finally:
        server.server_close()


def render_dashboard_html(snapshot: dict[str, Any], *, notice: str = "", error: str = "") -> str:
    counts = snapshot.get("counts", {})
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            '<meta http-equiv="refresh" content="15">',
            "<title>AgentDeck Console</title>",
            f"<style>{_DASHBOARD_CSS}</style>",
            "</head>",
            "<body>",
            '<header class="topbar">',
            "<div>",
            '<p class="eyebrow">AgentDeck</p>',
            "<h1>Control Console</h1>",
            "</div>",
            '<div class="topmeta">',
            f"<span>{_escape(snapshot.get('server_id', ''))}</span>",
            f"<span>{_escape(snapshot.get('generated_at_text', ''))}</span>",
            "</div>",
            "</header>",
            "<main>",
            '<section class="metrics" aria-label="Workspace summary">',
            _metric("Projects", counts.get("projects", 0), f"{counts.get('active_projects', 0)} active"),
            _metric("Focus", counts.get("active_focus", 0), f"{counts.get('focus', 0)} total"),
            _metric("Experience", counts.get("experience_collections", 0), f"{counts.get('experience_events', 0)} events"),
            _metric("Jobs", counts.get("active_jobs", 0), f"{counts.get('jobs', 0)} recent"),
            _metric("Approvals", counts.get("pending_approvals", 0), "pending"),
            "</section>",
            '<section class="workspace-line">',
            f"<strong>Workspace</strong><span>{_escape(snapshot.get('workspace', ''))}</span>",
            '<a href="/api/overview">JSON</a>',
            "</section>",
            _notice(notice, error),
            _actions_panel(),
            '<section class="board">',
            _panel("Projects", _project_cards(snapshot.get("projects", []))),
            _panel("Directories", _directory_cards(snapshot.get("directories", []))),
            _panel("Focus", _focus_cards(snapshot.get("focus", []))),
            _panel("Tasks", _task_cards(snapshot.get("tasks", []))),
            _panel("Agents", _agent_cards(snapshot.get("agents", []))),
            _panel("Workers", _worker_cards(snapshot.get("workers", []))),
            _panel("Experience Collections", _experience_collection_cards(snapshot.get("experience_collections", []))),
            _panel("Recent Experience", _experience_event_cards(snapshot.get("experience_events", []))),
            _panel("Clone Capsules", _clone_cards(snapshot.get("clones", []))),
            _panel("Jobs", _job_cards(snapshot.get("jobs", []))),
            _panel("Approvals", _approval_cards(snapshot.get("approvals", []))),
            "</section>",
            "</main>",
            "</body>",
            "</html>",
        ]
    )


def render_task_detail_html(workspace: Workspace, task: dict[str, Any], *, notice: str = "", error: str = "") -> str:
    task_id = str(task.get("task_id") or "")
    session = SessionRegistry(workspace).resolve(str(task.get("session_id") or "")) if task.get("session_id") else None
    jobs = [job for job in JobRegistry(workspace).list(limit=30) if job.task_id == task_id]
    approvals = ApprovalRegistry(workspace).list(task_id=task_id)
    notes = task.get("notes") if isinstance(task.get("notes"), list) else []
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            "<title>AgentDeck Task</title>",
            f"<style>{_DASHBOARD_CSS}</style>",
            "</head>",
            "<body>",
            '<header class="topbar">',
            "<div>",
            '<p class="eyebrow">AgentDeck Task</p>',
            f"<h1>{_escape(task.get('title') or task_id)}</h1>",
            "</div>",
            '<div class="topmeta"><a href="/">Dashboard</a></div>',
            "</header>",
            "<main>",
            _notice(notice, error),
            '<section class="board">',
            _panel(
                "Task",
                _card(
                    title=str(task.get("title") or task_id),
                    meta=[_badge(str(task.get("status") or "")), str(task.get("priority") or ""), f"id {task_id}"],
                    rows=[
                        ("project", task.get("project_id", "")),
                        ("agent", task.get("agent_id", "")),
                        ("session", task.get("session_id", "")),
                        ("desc", task.get("description", "")),
                    ],
                    actions=_task_actions({**task, "web_auto_enabled": bool(_read_web_auto_states(workspace).get(task_id))}),
                ),
            ),
            _panel(
                "Session",
                _session_cards([_session_summary(session)] if session is not None else []),
            ),
            _panel("Jobs", _job_cards([_job_summary(job) for job in jobs[:12]])),
            _panel("Approvals", _approval_cards([_approval_summary(approval) for approval in approvals[:12]])),
            _panel("Notes", _note_cards(notes)),
            "</section>",
            "</main>",
            "</body>",
            "</html>",
        ]
    )


def _project_summary(project: Any, tasks: list[Any], focus_records: list[Any], directories: list[Any]) -> dict[str, Any]:
    open_tasks = len([task for task in tasks if task.project_id == project.project_id and task.status != "done"])
    open_focus = len(
        [focus for focus in focus_records if focus.project_id == project.project_id and focus.status not in {"done"}]
    )
    directory_count = len([directory for directory in directories if directory.project_id == project.project_id])
    return {
        **project.to_dict(),
        "updated_at_text": _format_timestamp(project.updated_at),
        "open_tasks": open_tasks,
        "open_focus": open_focus,
        "directory_count": directory_count,
    }


def _directory_summary(directory: Any) -> dict[str, Any]:
    return {
        **directory.to_dict(),
        "updated_at_text": _format_timestamp(directory.updated_at),
    }


def _focus_summary(focus: Any) -> dict[str, Any]:
    latest_note = focus.notes[-1] if focus.notes else {}
    return {
        **focus.to_dict(),
        "updated_at_text": _format_timestamp(focus.updated_at),
        "description": _preview(focus.description, 220),
        "latest_note": _preview(str(latest_note.get("text") or ""), 180),
        "latest_note_kind": str(latest_note.get("kind") or ""),
    }


def _task_summary(task: Any, *, auto_states: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    latest_note = task.notes[-1] if task.notes else {}
    auto_state = dict((auto_states or {}).get(task.task_id) or {})
    return {
        **task.to_dict(),
        "updated_at_text": _format_timestamp(task.updated_at),
        "latest_note": _preview(str(latest_note.get("text") or ""), 180),
        "latest_note_kind": str(latest_note.get("kind") or ""),
        "web_auto_enabled": bool(auto_state.get("enabled")),
        "web_auto_mode": str(auto_state.get("mode") or ""),
    }


def _agent_summary(agent: Any) -> dict[str, Any]:
    return {
        **agent.to_dict(),
        "updated_at_text": _format_timestamp(agent.updated_at),
    }


def _session_summary(session: Any) -> dict[str, Any]:
    return {
        **session.to_dict(),
        "updated_at_text": _format_timestamp(session.updated_at),
        "last_user_message": _preview(session.last_user_message, 160),
        "last_assistant_final": _preview(session.last_assistant_final, 220),
    }


def _worker_summary(session: Any, focus_records: list[Any], directories: list[Any]) -> dict[str, Any]:
    focus = _focus_for_session(session, focus_records)
    directory = _directory_for_session_summary(session, directories)
    return {
        **_session_summary(session),
        "session_agent_id": session.session_id,
        "identity": session.agent_id,
        "focus_id": focus.focus_id if focus is not None else "",
        "focus_title": focus.title if focus is not None else "",
        "focus_text": _preview(focus.description, 180) if focus is not None else "",
        "directory_id": directory.directory_id if directory is not None else str(session.metadata.get("directory_id") or ""),
        "directory_title": directory.title if directory is not None else "",
        "directory_path": directory.path if directory is not None else session.project_dir,
    }


def _focus_for_session(session: Any, focus_records: list[Any]) -> Any | None:
    current_focus_id = str(session.metadata.get("current_focus_id") or "")
    if current_focus_id:
        for focus in focus_records:
            if focus.focus_id == current_focus_id:
                return focus
    matches = [focus for focus in focus_records if focus.session_id == session.session_id]
    if not matches:
        return None
    return sorted(matches, key=lambda item: item.updated_at, reverse=True)[0]


def _directory_for_session_summary(session: Any, directories: list[Any]) -> Any | None:
    directory_id = str(session.metadata.get("directory_id") or "")
    if directory_id:
        for directory in directories:
            if directory.directory_id == directory_id:
                return directory
    try:
        session_dir = str(Path(session.project_dir).expanduser().resolve())
    except OSError:
        session_dir = session.project_dir
    for directory in directories:
        if directory.path == session_dir:
            return directory
    return None


def _job_summary(job: Any) -> dict[str, Any]:
    return {
        **job.to_dict(),
        "updated_at_text": _format_timestamp(job.updated_at),
        "prompt": _preview(job.prompt, 180),
        "final_text": _preview(job.final_text, 220),
        "error": _preview(job.error, 180),
    }


def _approval_summary(approval: Any) -> dict[str, Any]:
    return {
        **approval.to_dict(),
        "updated_at_text": _format_timestamp(approval.updated_at),
        "request_text": _preview(approval.request_text, 220),
    }


def _experience_collection_summary(collection: Any, store: ExperienceStore) -> dict[str, Any]:
    return {
        **collection.to_dict(),
        "updated_at_text": _format_timestamp(collection.updated_at),
        "purpose": _preview(collection.purpose, 220),
        "event_count": len(store.list_events(collection=collection.collection_id, limit=0)),
    }


def _experience_event_summary(event: Any) -> dict[str, Any]:
    return {
        **event.to_dict(),
        "updated_at_text": _format_timestamp(event.updated_at),
        "purpose": _preview(event.purpose, 220),
        "result": _preview(event.result, 220),
        "analysis": _preview(event.analysis, 220),
        "decision": _preview(event.decisions[0], 220) if event.decisions else "",
    }


def _clone_summary(clone: Any) -> dict[str, Any]:
    validation = dict(clone.validation or {})
    return {
        "clone_id": clone.clone_id,
        "title": clone.title,
        "strategy": clone.strategy,
        "source_session_id": clone.source_session_id,
        "source_worker_id": clone.source_worker_id,
        "provider": clone.provider,
        "provider_session_kind": clone.provider_session_kind,
        "project_dir": clone.project_dir,
        "created_at": clone.created_at,
        "created_at_text": _format_timestamp(clone.created_at),
        "experience_collection_count": len(clone.experience_collections or []),
        "decision_count": len(clone.decisions or []),
        "progress_count": len(clone.progress or []),
        "validation_ok": bool(validation.get("ok")),
        "validation_findings": len(validation.get("findings") or []),
    }


def _read_web_auto_states(workspace: Workspace) -> dict[str, dict[str, Any]]:
    data = _read_json(_web_state_path(workspace))
    if not isinstance(data, dict):
        return {}
    states = data.get("auto") or {}
    if not isinstance(states, dict):
        return {}
    return {str(key): dict(value) for key, value in states.items() if isinstance(value, dict)}


def _write_web_auto_states(workspace: Workspace, states: dict[str, dict[str, Any]]) -> None:
    path = _web_state_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "auto": states}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _web_state_path(workspace: Workspace) -> Path:
    return workspace.root / "web" / "state.json"


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _json_response(data: dict[str, Any], *, status: int = HTTPStatus.OK) -> WebResponse:
    return WebResponse(
        status,
        "application/json; charset=utf-8",
        json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8"),
    )


def _redirect(message: str, *, error: bool = False) -> WebResponse:
    key = "error" if error else "notice"
    location = "/?" + urlencode({key: message})
    return WebResponse(
        HTTPStatus.SEE_OTHER,
        "text/plain; charset=utf-8",
        b"",
        headers={"Location": location},
    )


def _notice(notice: str, error: str) -> str:
    if error:
        return f'<section class="notice notice-error">{_escape(error)}</section>'
    if notice:
        return f'<section class="notice">{_escape(notice)}</section>'
    return ""


def _actions_panel() -> str:
    return (
        '<section class="actions-grid">'
        '<form class="action" method="post" action="/actions/projects/create">'
        "<h2>Add Project</h2>"
        '<input name="project_id" placeholder="project id" required>'
        '<input name="title" placeholder="title">'
        '<input name="cwd" placeholder="/path/to/project" required>'
        '<input name="default_agent" placeholder="default agent" value="owner">'
        '<button type="submit">Create</button>'
        "</form>"
        '<form class="action" method="post" action="/actions/rename">'
        "<h2>Rename ID</h2>"
        '<select name="entity">'
        '<option value="project">Project</option>'
        '<option value="task">Task</option>'
        '<option value="agent">Agent</option>'
        '<option value="session">Session</option>'
        "</select>"
        '<input name="old_id" placeholder="current id" required>'
        '<input name="new_id" placeholder="new id" required>'
        '<button type="submit">Rename Globally</button>'
        "</form>"
        '<form class="action" method="post" action="/actions/run">'
        "<h2>Assistant Chat</h2>"
        '<input type="hidden" name="assistant" value="1">'
        '<textarea name="prompt" rows="3" placeholder="Ask AgentDeck assistant..." required></textarea>'
        '<button type="submit">Send</button>'
        "</form>"
        "</section>"
    )


def _metric(label: str, value: Any, detail: str) -> str:
    return (
        '<article class="metric">'
        f"<span>{_escape(label)}</span>"
        f"<strong>{_escape(value)}</strong>"
        f"<small>{_escape(detail)}</small>"
        "</article>"
    )


def _panel(title: str, body: str) -> str:
    return (
        '<section class="panel">'
        f"<h2>{_escape(title)}</h2>"
        f"{body}"
        "</section>"
    )


def _project_cards(records: list[dict[str, Any]]) -> str:
    if not records:
        return _empty("No projects")
    return "".join(
        _card(
            title=str(record.get("title") or record.get("project_id") or ""),
            meta=[
                _badge(str(record.get("status") or "")),
                f"id {record.get('project_id') or ''}",
                f"focus {record.get('open_focus') or 0}",
                f"dirs {record.get('directory_count') or 0}",
            ],
            rows=[
                ("team", record.get("team_id", "")),
                ("agent", record.get("default_agent_id", "")),
                ("dir", record.get("project_dir", "")),
            ],
            actions=_project_action(record),
        )
        for record in records
    )


def _project_action(record: dict[str, Any]) -> str:
    project_id = _escape(record.get("project_id", ""))
    if record.get("status") == "archived":
        return (
            '<form method="post" action="/actions/projects/restore">'
            f'<input type="hidden" name="project_id" value="{project_id}">'
            '<button type="submit">Restore Project</button>'
            "</form>"
        )
    return (
        '<form method="post" action="/actions/projects/archive" onsubmit="return confirm(\'Archive this project? Child tasks, agents, sessions, and memory remain linked and restorable.\')">'
        f'<input type="hidden" name="project_id" value="{project_id}">'
        '<button class="danger" type="submit">Archive Project</button>'
        "</form>"
    )


def _directory_cards(records: list[dict[str, Any]]) -> str:
    if not records:
        return _empty("No directories")
    return "".join(
        _card(
            title=str(record.get("title") or record.get("directory_id") or ""),
            meta=[
                _badge(str(record.get("role") or "")),
                f"id {record.get('directory_id') or ''}",
                f"{record.get('status') or 'active'}",
            ],
            rows=[
                ("project", record.get("project_id", "")),
                ("parent", record.get("parent_directory_id", "")),
                ("path", record.get("path", "")),
            ],
        )
        for record in records
    )


def _focus_cards(records: list[dict[str, Any]]) -> str:
    if not records:
        return _empty("No focus records")
    return "".join(
        _card(
            title=str(record.get("title") or record.get("focus_id") or ""),
            meta=[
                _badge(str(record.get("status") or "")),
                f"id {record.get('focus_id') or ''}",
            ],
            rows=[
                ("project", record.get("project_id", "")),
                ("agent", record.get("agent_id", "")),
                ("session", record.get("session_id", "")),
                ("dir", record.get("directory", "")),
                ("text", record.get("description", "")),
                ("note", record.get("latest_note", "")),
            ],
        )
        for record in records
    )


def _task_cards(records: list[dict[str, Any]]) -> str:
    if not records:
        return _empty("No tasks")
    return "".join(
        _card(
            title=str(record.get("title") or record.get("task_id") or ""),
            title_html=_task_title_link(record),
            meta=[
                _badge(str(record.get("status") or "")),
                f"{record.get('priority') or 'normal'}",
                f"id {record.get('task_id') or ''}",
            ],
            rows=[
                ("project", record.get("project_id", "")),
                ("agent", record.get("agent_id", "")),
                ("session", record.get("session_id", "")),
                ("note", record.get("latest_note", "")),
            ],
            actions=_task_actions(record),
        )
        for record in records
    )


def _agent_cards(records: list[dict[str, Any]]) -> str:
    if not records:
        return _empty("No agents")
    return "".join(
        _card(
            title=str(record.get("title") or record.get("agent_id") or ""),
            meta=[
                _badge(str(record.get("role") or "")),
                f"id {record.get('agent_id') or ''}",
                f"{record.get('adapter') or 'echo'}",
            ],
            rows=[
                ("project", record.get("project_id", "")),
                ("team", record.get("team_id", "")),
                ("cwd", record.get("project_dir", "")),
                ("resume", record.get("resume_policy", "")),
            ],
        )
        for record in records
    )


def _session_cards(records: list[dict[str, Any]]) -> str:
    if not records:
        return _empty("No sessions")
    return "".join(
        _card(
            title=str(record.get("title") or record.get("session_id") or ""),
            meta=[
                _badge(str(record.get("status") or "")),
                f"id {record.get('session_id') or ''}",
                f"{record.get('adapter') or ''}",
            ],
            rows=[
                ("agent", record.get("agent_id", "")),
                ("provider", record.get("provider_session_id", "")),
                ("last", record.get("last_assistant_final", "")),
            ],
        )
        for record in records
    )


def _worker_cards(records: list[dict[str, Any]]) -> str:
    if not records:
        return _empty("No workers")
    return "".join(
        _card(
            title=str(record.get("title") or record.get("session_agent_id") or ""),
            meta=[
                _badge(str(record.get("status") or "")),
                f"id {record.get('session_agent_id') or ''}",
                f"{record.get('adapter') or ''}",
            ],
            rows=[
                ("identity", record.get("identity", "")),
                ("focus", record.get("focus_title", "")),
                ("directory", record.get("directory_title", "") or record.get("directory_path", "")),
                ("provider", record.get("provider_session_kind", "")),
                ("last", record.get("last_assistant_final", "")),
            ],
        )
        for record in records
    )


def _job_cards(records: list[dict[str, Any]]) -> str:
    if not records:
        return _empty("No jobs")
    return "".join(
        _card(
            title=str(record.get("job_id") or ""),
            meta=[
                _badge(str(record.get("status") or "")),
                f"{record.get('interface') or ''}",
                str(record.get("updated_at_text") or ""),
            ],
            rows=[
                ("task", record.get("task_id", "")),
                ("session", record.get("session_id", "")),
                ("prompt", record.get("prompt", "")),
                ("result", record.get("final_text", "") or record.get("error", "")),
            ],
            actions=_job_actions(record),
        )
        for record in records
    )


def _approval_cards(records: list[dict[str, Any]]) -> str:
    if not records:
        return _empty("No approvals")
    return "".join(
        _card(
            title=str(record.get("title") or record.get("approval_id") or ""),
            meta=[
                _badge(str(record.get("status") or "")),
                f"id {record.get('approval_id') or ''}",
                f"{record.get('adapter') or ''}",
            ],
            rows=[
                ("task", record.get("task_id", "")),
                ("agent", record.get("agent_id", "")),
                ("request", record.get("request_text", "")),
            ],
            actions=_approval_actions(record),
        )
        for record in records
    )


def _experience_collection_cards(records: list[dict[str, Any]]) -> str:
    if not records:
        return _empty("No experience collections")
    return "".join(
        _card(
            title=str(record.get("title") or record.get("collection_id") or ""),
            meta=[
                _badge(str(record.get("kind") or "")),
                f"id {record.get('collection_id') or ''}",
                f"events {record.get('event_count') or 0}",
            ],
            rows=[
                ("status", record.get("status", "")),
                ("project", record.get("project_id", "")),
                ("worker", record.get("worker_id", "")),
                ("focus", record.get("focus_id", "")),
                ("purpose", record.get("purpose", "")),
            ],
        )
        for record in records
    )


def _experience_event_cards(records: list[dict[str, Any]]) -> str:
    if not records:
        return _empty("No experience events")
    return "".join(
        _card(
            title=str(record.get("purpose") or record.get("event_id") or ""),
            meta=[
                _badge(str(record.get("status") or "")),
                f"{record.get('level') or ''}",
                f"id {record.get('event_id') or ''}",
            ],
            rows=[
                ("collection", record.get("collection_id", "")),
                ("kind", record.get("kind", "")),
                ("result", record.get("result", "")),
                ("decision", record.get("decision", "")),
                ("updated", record.get("updated_at_text", "")),
            ],
        )
        for record in records
    )


def _clone_cards(records: list[dict[str, Any]]) -> str:
    if not records:
        return _empty("No clone capsules")
    return "".join(
        _card(
            title=str(record.get("title") or record.get("clone_id") or ""),
            meta=[
                _badge(str(record.get("strategy") or "")),
                f"id {record.get('clone_id') or ''}",
                f"{record.get('provider') or ''}",
            ],
            rows=[
                ("source worker", record.get("source_worker_id", "")),
                ("source session", record.get("source_session_id", "")),
                ("provider kind", record.get("provider_session_kind", "")),
                ("experience", record.get("experience_collection_count", 0)),
                ("validation", "ok" if record.get("validation_ok") else f"{record.get('validation_findings') or 0} findings"),
                ("created", record.get("created_at_text", "")),
            ],
        )
        for record in records
    )


def _task_title_link(record: dict[str, Any]) -> str:
    task_id = str(record.get("task_id") or "")
    title = str(record.get("title") or task_id)
    if not task_id:
        return title
    return f'<a href="/tasks/{_escape(task_id)}">{_escape(title)}</a>'


def _note_cards(notes: list[Any]) -> str:
    if not notes:
        return _empty("No notes")
    rendered = []
    for note in reversed(notes[-20:]):
        if not isinstance(note, dict):
            continue
        rendered.append(
            _card(
                title=str(note.get("kind") or "note"),
                meta=[_format_timestamp(float(note.get("created_at") or 0.0))],
                rows=[("text", note.get("text", ""))],
            )
        )
    return "".join(rendered) or _empty("No notes")


def _task_actions(record: dict[str, Any]) -> str:
    task_id = _escape(record.get("task_id", ""))
    auto_enabled = bool(record.get("web_auto_enabled"))
    auto_label = "Stop Auto" if auto_enabled else "Start Auto"
    auto_action = "/actions/auto/stop" if auto_enabled else "/actions/auto/start"
    auto_fields = (
        f'<input type="hidden" name="task_id" value="{task_id}">'
        '<input type="hidden" name="mode" value="loop">'
        '<input type="hidden" name="approval_mode" value="bypass">'
        f'<button type="submit">{auto_label}</button>'
    )
    return (
        '<div class="inline-actions">'
        '<form method="post" action="/actions/run">'
        f'<input type="hidden" name="task_id" value="{task_id}">'
        '<textarea name="prompt" rows="2" placeholder="Run prompt on this task..." required></textarea>'
        '<button type="submit">Run</button>'
        "</form>"
        f'<form method="post" action="{auto_action}">'
        f"{auto_fields}"
        "</form>"
        '<form method="post" action="/actions/auto/start">'
        f'<input type="hidden" name="task_id" value="{task_id}">'
        '<input type="hidden" name="mode" value="task">'
        '<input type="hidden" name="approval_mode" value="bypass">'
        '<button type="submit">Auto By Task</button>'
        "</form>"
        "</div>"
    )


def _job_actions(record: dict[str, Any]) -> str:
    if record.get("status") not in {"queued", "running", "cancel_requested"}:
        return ""
    return (
        '<form class="inline-actions" method="post" action="/actions/jobs/cancel">'
        f'<input type="hidden" name="job_id" value="{_escape(record.get("job_id", ""))}">'
        '<button class="danger" type="submit">Cancel Job</button>'
        "</form>"
    )


def _approval_actions(record: dict[str, Any]) -> str:
    if record.get("status") != "pending":
        return ""
    approval_id = _escape(record.get("approval_id", ""))
    return (
        '<div class="inline-actions">'
        '<form method="post" action="/actions/approvals/resolve">'
        f'<input type="hidden" name="approval_id" value="{approval_id}">'
        '<input type="hidden" name="status" value="approved">'
        '<input name="note" placeholder="approval note">'
        '<button type="submit">Approve</button>'
        "</form>"
        '<form method="post" action="/actions/approvals/resolve">'
        f'<input type="hidden" name="approval_id" value="{approval_id}">'
        '<input type="hidden" name="status" value="rejected">'
        '<input name="note" placeholder="reject note">'
        '<button class="danger" type="submit">Reject</button>'
        "</form>"
        "</div>"
    )


def _card(*, title: str, meta: list[str], rows: list[tuple[str, Any]], actions: str = "", title_html: str = "") -> str:
    rendered_rows = "".join(
        f'<div class="row"><span>{_escape(label)}</span><p>{_escape(value)}</p></div>'
        for label, value in rows
        if str(value or "").strip()
    )
    rendered_meta = "".join(item if item.startswith("<") else f"<span>{_escape(item)}</span>" for item in meta if item)
    return (
        '<article class="record">'
        f"<h3>{title_html or _escape(title)}</h3>"
        f'<div class="meta">{rendered_meta}</div>'
        f"{rendered_rows}"
        f"{actions}"
        "</article>"
    )


def _badge(value: str) -> str:
    clean = _css_token(value or "unknown")
    return f'<span class="badge badge-{clean}">{_escape(value or "unknown")}</span>'


def _empty(text: str) -> str:
    return f'<p class="empty">{_escape(text)}</p>'


def _preview(value: str, limit: int) -> str:
    clean = " ".join(str(value or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: max(limit - 1, 0)].rstrip() + "..."


def _first(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or []
    return str(values[-1]) if values else ""


def _format_timestamp(value: float) -> str:
    if not value:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(value)))


def _escape(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _css_token(value: str) -> str:
    clean = "".join(char.lower() if char.isalnum() else "-" for char in str(value))
    return clean.strip("-") or "unknown"


_DASHBOARD_CSS = """
:root {
  color-scheme: light;
  --bg: #f6f7f8;
  --ink: #1d2329;
  --muted: #5d6873;
  --line: #d8dee4;
  --panel: #ffffff;
  --accent: #176d5d;
  --accent-soft: #dff3ee;
  --warn: #9a5b00;
  --warn-soft: #fff1cf;
  --bad: #a33b3b;
  --bad-soft: #fde2e2;
  --blue: #275c9a;
  --blue-soft: #e2edf9;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  letter-spacing: 0;
}
.topbar {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 20px;
  padding: 26px clamp(16px, 3vw, 36px) 18px;
  background: #ffffff;
  border-bottom: 1px solid var(--line);
}
.eyebrow {
  margin: 0 0 4px;
  color: var(--accent);
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
}
h1 {
  margin: 0;
  font-size: clamp(28px, 4vw, 44px);
  line-height: 1.05;
  font-weight: 760;
}
.topmeta {
  display: flex;
  flex-direction: column;
  gap: 4px;
  color: var(--muted);
  font-size: 13px;
  text-align: right;
  overflow-wrap: anywhere;
}
main {
  width: min(1440px, 100%);
  margin: 0 auto;
  padding: 18px clamp(12px, 2.4vw, 28px) 36px;
}
.metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}
.metric {
  min-height: 112px;
  padding: 16px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.metric span,
.metric small {
  display: block;
  color: var(--muted);
  font-size: 13px;
}
.metric strong {
  display: block;
  margin: 8px 0;
  font-size: 36px;
  line-height: 1;
}
.workspace-line {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  gap: 10px;
  align-items: center;
  margin: 12px 0;
  padding: 12px 14px;
  background: #ffffff;
  border: 1px solid var(--line);
  border-radius: 8px;
  color: var(--muted);
  font-size: 13px;
}
.workspace-line span { overflow-wrap: anywhere; }
.workspace-line a {
  color: var(--blue);
  text-decoration: none;
  font-weight: 700;
}
.notice {
  margin: 12px 0;
  padding: 12px 14px;
  background: var(--accent-soft);
  color: var(--accent);
  border: 1px solid #b8ddd4;
  border-radius: 8px;
  font-size: 14px;
}
.notice-error {
  background: var(--bad-soft);
  color: var(--bad);
  border-color: #f0bbbb;
}
.actions-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin: 12px 0 16px;
}
.action {
  padding: 14px;
  background: #ffffff;
  border: 1px solid var(--line);
  border-radius: 8px;
}
.action h2 {
  margin: 0 0 10px;
  font-size: 16px;
}
input,
select,
textarea {
  width: 100%;
  min-height: 38px;
  margin: 4px 0;
  padding: 8px 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #ffffff;
  color: var(--ink);
  font: inherit;
  font-size: 14px;
}
textarea {
  resize: vertical;
}
button {
  min-height: 36px;
  margin-top: 6px;
  padding: 7px 11px;
  border: 1px solid #0e5b4c;
  border-radius: 6px;
  background: var(--accent);
  color: #ffffff;
  font: inherit;
  font-size: 13px;
  font-weight: 700;
  cursor: pointer;
}
button.danger {
  border-color: #7f2d2d;
  background: var(--bad);
}
.inline-actions {
  display: grid;
  grid-template-columns: 1fr;
  gap: 8px;
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid #edf0f2;
}
.inline-actions form {
  min-width: 0;
}
.board {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}
.panel {
  min-width: 0;
}
.panel h2 {
  margin: 10px 2px 8px;
  font-size: 18px;
  line-height: 1.2;
}
.record {
  margin: 8px 0;
  padding: 14px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.record h3 {
  margin: 0;
  font-size: 15px;
  line-height: 1.25;
  overflow-wrap: anywhere;
}
.meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 9px 0;
}
.meta span,
.badge {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  padding: 3px 8px;
  border-radius: 999px;
  background: #eef1f4;
  color: #3e4750;
  font-size: 12px;
  line-height: 1.2;
  overflow-wrap: anywhere;
}
.badge-active,
.badge-doing,
.badge-done,
.badge-approved {
  background: var(--accent-soft);
  color: var(--accent);
}
.badge-running,
.badge-queued,
.badge-review,
.badge-pending {
  background: var(--warn-soft);
  color: var(--warn);
}
.badge-blocked,
.badge-error,
.badge-rejected,
.badge-cancelled,
.badge-interrupted {
  background: var(--bad-soft);
  color: var(--bad);
}
.row {
  display: grid;
  grid-template-columns: 76px minmax(0, 1fr);
  gap: 8px;
  padding: 6px 0;
  border-top: 1px solid #edf0f2;
}
.row span {
  color: var(--muted);
  font-size: 12px;
  text-transform: uppercase;
}
.row p {
  margin: 0;
  color: #263039;
  font-size: 13px;
  line-height: 1.4;
  overflow-wrap: anywhere;
}
.empty {
  margin: 8px 0;
  padding: 18px;
  background: #ffffff;
  border: 1px dashed var(--line);
  border-radius: 8px;
  color: var(--muted);
}
@media (max-width: 920px) {
  .metrics,
  .actions-grid,
  .board {
    grid-template-columns: 1fr;
  }
  .topbar {
    align-items: flex-start;
    flex-direction: column;
  }
  .topmeta {
    text-align: left;
  }
}
@media (max-width: 560px) {
  main {
    padding-left: 10px;
    padding-right: 10px;
  }
  .workspace-line {
    grid-template-columns: 1fr;
  }
  .row {
    grid-template-columns: 1fr;
  }
}
"""
