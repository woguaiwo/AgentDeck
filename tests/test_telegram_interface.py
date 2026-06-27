import asyncio
import contextlib
import hashlib
import io
import json
import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path

import agentdeck.interfaces.telegram as telegram_module
from agentdeck.cli import _telegram_configs_from_args, main
from agentdeck.core.config import Workspace
from agentdeck.core.events import AgentEvent, EventKind
from agentdeck.core.run_service import RunRequest, RunServiceResult
from agentdeck.interfaces.telegram import (
    TelegramChatStateStore,
    TelegramCommandHandler,
    TelegramConfig,
    TelegramJobQueue,
    TelegramRestartNoticeStore,
    TelegramServer,
    TelegramUpdateOffsetStore,
    config_from_env,
    split_message,
)
from agentdeck.storage.approvals import ApprovalRegistry
from agentdeck.storage.agents import DEFAULT_ASSISTANT_TEMPLATE, AgentRegistry, role_template_for_agent
from agentdeck.storage.directories import DirectoryRegistry
from agentdeck.storage.errors import ErrorIncidentStore
from agentdeck.storage.focus import FocusRegistry
from agentdeck.storage.jobs import JobRegistry
from agentdeck.storage.progress import ProgressJournal
from agentdeck.storage.projects import ProjectRegistry
from agentdeck.storage.project_state import ProjectStateStore
from agentdeck.storage.session_state import SessionStateCard, SessionStateStore
from agentdeck.storage.sessions import SessionRegistry
from agentdeck.storage.tasks import TaskBoard
from agentdeck.storage.telegram_bots import TelegramBotRegistry, assistant_agent_id_for_bot


class TelegramInterfaceTests(unittest.TestCase):
    def test_handler_lists_projects_tasks_and_runs_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            project = tmp / "project"
            project.mkdir()

            self._main(["--workspace", str(workspace.root), "projects", "create", "proj", "--title", "Project One", "--cwd", str(project)])
            self._main(["--workspace", str(workspace.root), "agents", "create", "owner", "--project", "proj", "--adapter", "echo"])
            task_out = self._main(["--workspace", str(workspace.root), "tasks", "create", "Telegram task", "--project", "proj"])
            task_id = re.search(r"\((task-[^)]+)\)", task_out).group(1)

            handler = TelegramCommandHandler(workspace)

            projects = asyncio.run(handler.handle_text("/projects"))[0]
            self.assertIn("Project One", projects)
            self.assertIn("id: proj", projects)

            tasks = asyncio.run(handler.handle_text("/tasks proj"))[0]
            self.assertIn("Telegram task", tasks)
            self.assertIn(task_id, tasks)

            task_detail = asyncio.run(handler.handle_text(f"/task {task_id}"))[0]
            self.assertIn("status: todo", task_detail)

            run_result = asyncio.run(handler.handle_text(f"/run {task_id} continue work"))[0]
            self.assertIn("Echo: continue work", run_result)
            self.assertIn("session:", run_result)

            task = TaskBoard(workspace).get(task_id)
            assert task is not None
            self.assertEqual(task.status, "doing")
            self.assertTrue(task.session_id)
            session = SessionRegistry(workspace).get(task.session_id)
            assert session is not None
            self.assertEqual(session.agent_id, "owner")

    def test_handler_creates_focus_and_routes_plain_text_to_focus(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            project = tmp / "project"
            project.mkdir()
            ProjectRegistry(workspace).upsert(project_id="proj", project_dir=project)
            AgentRegistry(workspace).upsert(agent_id="owner", project_id="proj", adapter="echo", project_dir=project)
            seen: list[RunRequest] = []

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                seen.append(request)
                return RunServiceResult(
                    session_id="session-focus",
                    final_text=f"done: {request.prompt}",
                    events=[],
                    agent_id=request.agent or "owner",
                    adapter="echo",
                    focus_id=request.focus or "",
                )

            handler = TelegramCommandHandler(workspace, runner=runner)

            created = asyncio.run(handler.handle_text("/focus new Explore handoff", chat_id=42))[0]
            self.assertIn("Focus created and selected", created)
            focus_id = TelegramChatStateStore(workspace).current_focus(42)
            self.assertTrue(focus_id)

            reply = asyncio.run(handler.handle_text("continue this", chat_id=42))[0]
            self.assertIn("done: continue this", reply)
            self.assertEqual(seen[-1].focus, focus_id)
            self.assertEqual(seen[-1].agent, "owner")

            natural_agent_text = "agent 和 session 应该是等价的，我不喜欢这部分过于复杂化"
            reply = asyncio.run(handler.handle_text(natural_agent_text, chat_id=42))[0]
            self.assertIn(f"done: {natural_agent_text}", reply)
            self.assertEqual(seen[-1].prompt, natural_agent_text)
            self.assertEqual(seen[-1].focus, focus_id)

    def test_handler_lists_uses_and_reports_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            project = tmp / "project"
            lab = project / "lab"
            lab.mkdir(parents=True)
            ProjectRegistry(workspace).upsert(project_id="proj", title="Project One", project_dir=project)
            ProjectRegistry(workspace).add_directory("proj", lab)
            AgentRegistry(workspace).upsert(
                agent_id="lab-owner",
                title="Lab Owner",
                project_id="proj",
                adapter="echo",
                project_dir=lab,
            )

            handler = TelegramCommandHandler(workspace)

            listed = asyncio.run(handler.handle_text("/directories proj", chat_id=42))[0]
            self.assertIn("Directories (Project One):", listed)
            self.assertIn(str(lab.resolve()), listed)

            selected = asyncio.run(handler.handle_text("/use directory 2", chat_id=42))[0]
            self.assertIn("Current directory set:", selected)
            self.assertIn("Lab Owner", selected)
            state = TelegramChatStateStore(workspace)
            directory = DirectoryRegistry(workspace).resolve(lab)
            assert directory is not None
            self.assertEqual(state.current_directory(42), directory.directory_id)
            self.assertEqual(state.current_project(42), "proj")
            self.assertEqual(state.current_agent(42), "lab-owner")

            current = asyncio.run(handler.handle_text("/current", chat_id=42))[0]
            self.assertIn("Current directory: lab", current)
            self.assertIn(str(lab.resolve()), current)

            status = asyncio.run(handler.handle_text("/status", chat_id=42))[0]
            self.assertIn("Directory: lab", status)
            self.assertIn("Directory path:", status)

    def test_auto_start_uses_current_focus_without_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            project = tmp / "project"
            project.mkdir()
            ProjectRegistry(workspace).upsert(project_id="proj", project_dir=project)
            AgentRegistry(workspace).upsert(agent_id="owner", project_id="proj", adapter="echo", project_dir=project)
            focus = FocusRegistry(workspace).create(
                title="Focus auto",
                project_id="proj",
                agent_id="owner",
                directory=project,
            )
            seen: list[RunRequest] = []

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                seen.append(request)
                return RunServiceResult(
                    session_id="session-auto-focus",
                    final_text="auto done",
                    events=[],
                    agent_id=request.agent or "owner",
                    adapter="echo",
                    focus_id=request.focus or "",
                )

            queue = TelegramJobQueue(workspace, sender=lambda chat_id, text: None, runner=runner)
            handler = TelegramCommandHandler(workspace, job_queue=queue)
            TelegramChatStateStore(workspace).set_current_focus(42, focus.focus_id)

            reply = asyncio.run(handler.handle_text("/auto start", chat_id=42))[0]
            self.assertIn("Auto mode enabled", reply)
            self.assertIn("focus: Focus auto", reply)
            job_id = re.search(r"Job started: (job-\S+)", reply).group(1)
            queue.wait(job_id, timeout=2)
            self.assertEqual(seen[-1].focus, focus.focus_id)
            job = JobRegistry(workspace).get(job_id)
            assert job is not None
            self.assertEqual(job.task_id, "")
            self.assertEqual(job.metadata.get("focus_id"), focus.focus_id)

    def test_handler_lists_and_resolves_approvals(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()
            event = AgentEvent(
                EventKind.APPROVAL_REQUESTED,
                "owner",
                "session-a",
                text="Allow command?",
                payload={"provider": "codex", "type": "approval_requested"},
            )
            approval = ApprovalRegistry(workspace).record_request(
                event,
                adapter="codex",
                project_dir=tmpdir,
                project_id="proj",
                task_id="task-a",
            )

            handler = TelegramCommandHandler(workspace)

            approvals = asyncio.run(handler.handle_text("/approvals"))[0]
            self.assertIn(approval.approval_id, approvals)
            detail = asyncio.run(handler.handle_text(f"/approval {approval.approval_id}"))[0]
            self.assertIn("Allow command?", detail)

            approved = asyncio.run(handler.handle_text(f"/approve {approval.approval_id} ok"))[0]
            self.assertIn("Approval approved", approved)
            resolved = ApprovalRegistry(workspace).get(approval.approval_id)
            assert resolved is not None
            self.assertEqual(resolved.status, "approved")
            self.assertEqual(resolved.resolved_by, "telegram")

    def test_status_sessions_resume_and_numbered_approvals(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()
            task = TaskBoard(workspace).create(title="Phone control task")
            SessionRegistry(workspace).upsert_start(
                session_id="session-phone",
                agent_id="owner",
                adapter="echo",
                project_dir=tmpdir,
                prompt="initial work",
                title="Phone session",
            )
            TaskBoard(workspace).attach_session(task.task_id, "session-phone")
            event = AgentEvent(
                EventKind.APPROVAL_REQUESTED,
                "owner",
                "session-phone",
                text="Allow command?",
                payload={"provider": "codex", "type": "approval_requested"},
            )
            approval = ApprovalRegistry(workspace).record_request(
                event,
                adapter="codex",
                project_dir=tmpdir,
                task_id=task.task_id,
            )
            seen: list[RunRequest] = []

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                seen.append(request)
                return RunServiceResult(
                    session_id=request.session or "session-new",
                    final_text=f"done: {request.prompt}",
                    events=[],
                    agent_id="owner",
                    adapter="echo",
                    task_id=request.task or "",
                )

            queue = TelegramJobQueue(workspace, sender=lambda chat_id, text: None, runner=runner)
            handler = TelegramCommandHandler(workspace, job_queue=queue)

            selected = asyncio.run(handler.handle_text(f"/use {task.task_id}", chat_id=42))[0]
            self.assertIn("Current task set", selected)

            status = asyncio.run(handler.handle_text("/status", chat_id=42))[0]
            self.assertIn("Legacy task: Phone control task", status)
            self.assertIn("Pending approvals: 1", status)
            self.assertIn("Phone session", status)

            sessions = asyncio.run(handler.handle_text("/sessions", chat_id=42))[0]
            self.assertIn("1. Phone session", sessions)

            resume = asyncio.run(handler.handle_text("/resume 1 continue session", chat_id=42))[0]
            resume_job = re.search(r"Resume job started: (job-\S+)", resume).group(1)
            queue.wait(resume_job, timeout=2)
            self.assertEqual(seen[-1].session, "session-phone")
            self.assertEqual(seen[-1].prompt, "continue session")

            approvals = asyncio.run(handler.handle_text("/approvals", chat_id=42))[0]
            self.assertIn("1. Allow command?", approvals)
            detail = asyncio.run(handler.handle_text("/approval 1", chat_id=42))[0]
            self.assertIn(approval.approval_id, detail)

            approved = asyncio.run(handler.handle_text("/approve 1 ok", chat_id=42))[0]
            self.assertIn("Approval approved", approved)
            self.assertIn("Follow-up job started", approved)
            followup_job = re.search(r"Follow-up job started: (job-\S+)", approved).group(1)
            queue.wait(followup_job, timeout=2)
            self.assertEqual(seen[-1].task, task.task_id)
            self.assertEqual(seen[-1].approval_mode, "bypass")
            self.assertIn("Approval was granted", seen[-1].prompt)
            runs_after_first_approval = len(seen)

            approved_again = asyncio.run(handler.handle_text("/approve 1 again", chat_id=42))[0]
            self.assertIn("Approval approved", approved_again)
            self.assertNotIn("Follow-up job started", approved_again)
            self.assertEqual(len(seen), runs_after_first_approval)

    def test_use_session_selects_linked_task_and_updates_phone_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            project_dir = tmp / "project"
            project_dir.mkdir()
            self._main(["--workspace", str(workspace.root), "projects", "create", "proj", "--title", "Project", "--cwd", str(project_dir)])
            self._main(["--workspace", str(workspace.root), "agents", "create", "owner", "--project", "proj", "--adapter", "echo"])
            task = TaskBoard(workspace).create(title="Renamed task", project_id="proj", agent_id="owner")
            session = SessionRegistry(workspace).upsert_start(
                session_id="session-linked",
                agent_id="owner",
                adapter="echo",
                project_dir=project_dir,
                prompt="old task title",
                title="Old session title",
            )
            TaskBoard(workspace).attach_session(task.task_id, session.session_id)
            seen: list[RunRequest] = []

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                seen.append(request)
                return RunServiceResult(
                    session_id=session.session_id,
                    final_text=f"done: {request.prompt}",
                    events=[],
                    agent_id="owner",
                    adapter="echo",
                    task_id=request.task or "",
                )

            queue = TelegramJobQueue(workspace, sender=lambda chat_id, text: None, runner=runner)
            handler = TelegramCommandHandler(workspace, job_queue=queue)

            sessions = asyncio.run(handler.handle_text("/sessions", chat_id=42))[0]
            self.assertIn("Session-agents:", sessions)
            self.assertIn("1. Old session title", sessions)
            self.assertIn("legacy task: Renamed task", sessions)

            workers = asyncio.run(handler.handle_text("/workers", chat_id=42))[0]
            self.assertIn("Session-agents:", workers)
            self.assertIn("identity: owner", workers)
            self.assertIn("legacy task: Renamed task", workers)

            selected = asyncio.run(handler.handle_text("/use session 1", chat_id=42))[0]
            self.assertIn("Session selected.", selected)
            self.assertIn("Plain text messages will resume this session.", selected)
            self.assertIn("task: Renamed task", selected)
            self.assertEqual(TelegramChatStateStore(workspace).current_session(42), session.session_id)
            self.assertEqual(TelegramChatStateStore(workspace).current_task(42), task.task_id)

            current = asyncio.run(handler.handle_text("/current", chat_id=42))[0]
            self.assertIn("Current session:", current)
            self.assertIn("Old session title", current)
            self.assertIn("Plain text messages will resume this session.", current)

            reply = asyncio.run(handler.handle_text("continue selected session", chat_id=42))[0]
            self.assertIn("Job started:", reply)
            job_id = re.search(r"Job started: (job-\S+)", reply).group(1)
            queue.wait(job_id, timeout=2)
            self.assertEqual(seen[-1].task, task.task_id)
            self.assertEqual(seen[-1].prompt, "continue selected session")

            sessions = asyncio.run(handler.handle_text("/sessions", chat_id=42))[0]
            self.assertIn("Old session title [current]", sessions)

    def test_use_unlinked_session_routes_plain_text_to_session_not_assistant(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()
            AgentRegistry(workspace).upsert(
                agent_id="assistant",
                title="Default Assistant",
                adapter="echo",
                role="manager",
                project_dir=tmpdir,
            )
            session = SessionRegistry(workspace).upsert_start(
                session_id="session-unlinked",
                agent_id="owner",
                adapter="echo",
                project_dir=tmpdir,
                prompt="adopt this thread",
                title="Imported provider session",
            )
            seen: list[RunRequest] = []

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                seen.append(request)
                return RunServiceResult(
                    session_id=request.session or "assistant-session",
                    final_text=f"done: {request.prompt}",
                    events=[],
                    agent_id=request.agent or "owner",
                    adapter="echo",
                    task_id=request.task or "",
                )

            queue = TelegramJobQueue(workspace, sender=lambda chat_id, text: None, runner=runner)
            handler = TelegramCommandHandler(workspace, job_queue=queue)

            asyncio.run(handler.handle_text("/sessions", chat_id=42))
            selected = asyncio.run(handler.handle_text("/session use 1", chat_id=42))[0]
            self.assertIn("Session selected.", selected)
            self.assertIn("task: -", selected)

            reply = asyncio.run(handler.handle_text("continue imported session", chat_id=42))[0]
            self.assertIn("Session job started:", reply)
            self.assertNotIn("Assistant job started", reply)
            job_id = re.search(r"Session job started: (job-\S+)", reply).group(1)
            queue.wait(job_id, timeout=2)
            self.assertEqual(seen[-1].session, session.session_id)
            self.assertEqual(seen[-1].agent, "owner")
            self.assertEqual(seen[-1].prompt, "continue imported session")

    def test_run_command_can_start_background_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()
            task = TaskBoard(workspace).create(title="Background task")
            started = threading.Event()
            release = threading.Event()
            sent: list[tuple[int, str]] = []

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                self.assertIs(workspace_arg, workspace)
                started.set()
                release.wait(timeout=2)
                return RunServiceResult(
                    session_id="session-bg",
                    final_text=f"done: {request.prompt}",
                    events=[],
                    agent_id="owner",
                    adapter="echo",
                    task_id=request.task or "",
                )

            queue = TelegramJobQueue(workspace, sender=lambda chat_id, text: sent.append((chat_id, text)), runner=runner)
            handler = TelegramCommandHandler(workspace, job_queue=queue)

            reply = asyncio.run(handler.handle_text(f"/run {task.task_id} background work", chat_id=42))[0]
            self.assertIn("Job started:", reply)
            job_id = re.search(r"Job started: (job-\S+)", reply).group(1)
            self.assertTrue(started.wait(timeout=1))

            jobs = asyncio.run(handler.handle_text("/jobs", chat_id=42))[0]
            self.assertIn(job_id, jobs)
            self.assertRegex(jobs, r"status: (queued|running)")

            release.set()
            job = queue.wait(job_id, timeout=2)
            assert job is not None
            self.assertEqual(job.status, "done")
            self.assertEqual(job.session_id, "session-bg")
            self.assertEqual(sent, [(42, f"Job done: {job_id}\ntask: {task.task_id}\nsession: session-bg\n\ndone: background work")])

            detail = asyncio.run(handler.handle_text(f"/job {job_id}", chat_id=42))[0]
            self.assertIn("status: done", detail)
            self.assertIn("done: background work", detail)

    def test_current_task_removes_need_to_copy_task_or_job_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            project = tmp / "project"
            project.mkdir()
            seen: list[tuple[str, str, str]] = []
            sent: list[tuple[int, str]] = []

            self._main(["--workspace", str(workspace.root), "projects", "create", "proj", "--title", "Project One", "--cwd", str(project)])
            self._main(["--workspace", str(workspace.root), "agents", "create", "owner", "--project", "proj", "--adapter", "echo"])
            task_out = self._main(["--workspace", str(workspace.root), "tasks", "create", "Named phone task", "--project", "proj"])
            task_id = re.search(r"\((task-[^)]+)\)", task_out).group(1)

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                seen.append((request.task or "", request.prompt, request.agent or ""))
                return RunServiceResult(
                    session_id="session-current",
                    final_text=f"done: {request.prompt}",
                    events=[],
                    agent_id="owner",
                    adapter="echo",
                    task_id=request.task or "",
                )

            queue = TelegramJobQueue(workspace, sender=lambda chat_id, text: sent.append((chat_id, text)), runner=runner)
            handler = TelegramCommandHandler(workspace, job_queue=queue)

            selected = asyncio.run(handler.handle_text("/use named phone task", chat_id=42))[0]
            self.assertIn("Current task set", selected)
            self.assertIn(task_id, selected)

            current = asyncio.run(handler.handle_text("/current", chat_id=42))[0]
            self.assertIn("Named phone task", current)

            reply = asyncio.run(handler.handle_text("/run continue without ids", chat_id=42))[0]
            self.assertIn("Job started:", reply)
            job_id = re.search(r"Job started: (job-\S+)", reply).group(1)
            job = queue.wait(job_id, timeout=2)
            assert job is not None
            self.assertEqual(seen, [(task_id, "continue without ids", "")])

            latest = asyncio.run(handler.handle_text("/job", chat_id=42))[0]
            self.assertIn(f"Job: {job_id}", latest)
            self.assertIn("done: continue without ids", latest)

    def test_plain_text_runs_current_task_or_shows_connection_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            project = tmp / "project"
            project.mkdir()
            seen: list[tuple[str, str, str]] = []

            self._main(["--workspace", str(workspace.root), "projects", "create", "proj", "--title", "Project One", "--cwd", str(project)])
            self._main(["--workspace", str(workspace.root), "agents", "create", "owner", "--project", "proj", "--adapter", "echo"])
            task_out = self._main(["--workspace", str(workspace.root), "tasks", "create", "Plain text task", "--project", "proj"])
            task_id = re.search(r"\((task-[^)]+)\)", task_out).group(1)

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                seen.append((request.task or "", request.prompt, request.agent or ""))
                return RunServiceResult(
                    session_id="session-plain",
                    final_text=f"done: {request.prompt}",
                    events=[],
                    agent_id="owner",
                    adapter="echo",
                    task_id=request.task or "",
                )

            queue = TelegramJobQueue(workspace, sender=lambda chat_id, text: None, runner=runner)
            handler = TelegramCommandHandler(workspace, job_queue=queue)

            hint = asyncio.run(handler.handle_text("continue before selecting", chat_id=42))[0]
            self.assertIn("No current focus is selected", hint)
            self.assertEqual(seen, [])

            AgentRegistry(workspace).upsert(
                agent_id="assistant",
                title="AgentDeck Assistant",
                role="manager",
                adapter="echo",
                project_dir=str(tmp),
                replace=False,
            )
            AgentRegistry(workspace).set_role_template("assistant", DEFAULT_ASSISTANT_TEMPLATE)
            assistant_reply = asyncio.run(handler.handle_text("help me choose a project", chat_id=42))[0]
            self.assertIn("Assistant job started:", assistant_reply)
            assistant_job = re.search(r"Assistant job started: (job-\S+)", assistant_reply).group(1)
            queue.wait(assistant_job, timeout=2)
            self.assertEqual(seen, [("", "help me choose a project", "assistant")])

            unknown = asyncio.run(handler.handle_text("/definitely_unknown", chat_id=42))[0]
            self.assertIn("Unknown command", unknown)
            self.assertEqual(seen, [("", "help me choose a project", "assistant")])

            asyncio.run(handler.handle_text(f"/use {task_id}", chat_id=42))
            reply = asyncio.run(handler.handle_text("continue as plain text", chat_id=42))[0]
            self.assertIn("Job started:", reply)
            job_id = re.search(r"Job started: (job-\S+)", reply).group(1)
            queue.wait(job_id, timeout=2)
            self.assertEqual(seen, [("", "help me choose a project", "assistant"), (task_id, "continue as plain text", "")])

    def test_assistant_can_execute_safe_marked_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            project = tmp / "project"
            project.mkdir()
            seen: list[tuple[str, str, str]] = []
            sent: list[tuple[int, str]] = []

            self._main(["--workspace", str(workspace.root), "projects", "create", "proj", "--title", "Project One", "--cwd", str(project)])
            AgentRegistry(workspace).upsert(
                agent_id="assistant",
                title="AgentDeck Assistant",
                role="manager",
                adapter="echo",
                project_dir=str(tmp),
                replace=False,
            )
            AgentRegistry(workspace).set_role_template("assistant", DEFAULT_ASSISTANT_TEMPLATE)

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                seen.append((request.task or "", request.prompt, request.agent or ""))
                return RunServiceResult(
                    session_id="session-assistant-action",
                    final_text="I can switch to that project.\nAGENTDECK_ACTION: /use project proj",
                    events=[],
                    agent_id="assistant",
                    adapter="echo",
                    task_id=request.task or "",
                )

            queue = TelegramJobQueue(workspace, sender=lambda chat_id, text: sent.append((chat_id, text)), runner=runner)
            handler = TelegramCommandHandler(workspace, job_queue=queue)

            reply = asyncio.run(handler.handle_text("switch to Project One", chat_id=42))[0]
            self.assertIn("Assistant job started:", reply)
            job_id = re.search(r"Assistant job started: (job-\S+)", reply).group(1)
            queue.wait(job_id, timeout=2)

            self.assertEqual(seen, [("", "switch to Project One", "assistant")])
            self.assertTrue(all("AGENTDECK_ACTION" not in text for _, text in sent))
            self.assertTrue(any("Assistant action executed: /use project proj" in text for _, text in sent))
            self.assertTrue(any("Current project set:" in text for _, text in sent))
            self.assertEqual(TelegramCommandHandler(workspace).chat_state.current_project(42), "proj")

    def test_assistant_can_select_session_with_safe_marked_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()
            session = SessionRegistry(workspace).upsert_start(
                session_id="session-selectable",
                agent_id="owner",
                adapter="echo",
                project_dir=tmpdir,
                prompt="existing work",
                title="Existing work",
            )
            sent: list[tuple[int, str]] = []

            AgentRegistry(workspace).upsert(
                agent_id="assistant",
                title="AgentDeck Assistant",
                role="manager",
                adapter="echo",
                project_dir=tmpdir,
                replace=False,
            )
            AgentRegistry(workspace).set_role_template("assistant", DEFAULT_ASSISTANT_TEMPLATE)

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                return RunServiceResult(
                    session_id="session-assistant-action",
                    final_text=(
                        "I will enter that session.\n"
                        "AGENTDECK_ACTION: /sessions\n"
                        "AGENTDECK_ACTION: /use session 1"
                    ),
                    events=[],
                    agent_id="assistant",
                    adapter="echo",
                    task_id=request.task or "",
                )

            queue = TelegramJobQueue(workspace, sender=lambda chat_id, text: sent.append((chat_id, text)), runner=runner)
            handler = TelegramCommandHandler(workspace, job_queue=queue)

            reply = asyncio.run(handler.handle_text("enter the existing work session", chat_id=42))[0]
            self.assertIn("Assistant job started:", reply)
            job_id = re.search(r"Assistant job started: (job-\S+)", reply).group(1)
            queue.wait(job_id, timeout=2)

            self.assertTrue(any("Assistant action executed: /use session 1" in text for _, text in sent))
            self.assertTrue(any("Session selected." in text for _, text in sent))
            self.assertTrue(any("Plain text messages will resume this session." in text for _, text in sent))
            self.assertEqual(TelegramChatStateStore(workspace).current_session(42), session.session_id)

    def test_assistant_claiming_switch_without_action_gets_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()
            sent: list[tuple[int, str]] = []

            AgentRegistry(workspace).upsert(
                agent_id="assistant",
                title="AgentDeck Assistant",
                role="manager",
                adapter="echo",
                project_dir=tmpdir,
                replace=False,
            )
            AgentRegistry(workspace).set_role_template("assistant", DEFAULT_ASSISTANT_TEMPLATE)

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                return RunServiceResult(
                    session_id="session-assistant-no-action",
                    final_text="已切换到 developer session d8416b8bce05，后续普通消息会进入它。",
                    events=[],
                    agent_id="assistant",
                    adapter="echo",
                    task_id=request.task or "",
                )

            queue = TelegramJobQueue(workspace, sender=lambda chat_id, text: sent.append((chat_id, text)), runner=runner)
            handler = TelegramCommandHandler(workspace, job_queue=queue)

            reply = asyncio.run(handler.handle_text("切换到 developer session", chat_id=42))[0]
            job_id = re.search(r"Assistant job started: (job-\S+)", reply).group(1)
            queue.wait(job_id, timeout=2)

            self.assertTrue(any("State change was not verified." in text for _, text in sent))
            self.assertEqual(TelegramChatStateStore(workspace).current_session(42), "")

    def test_assistant_can_scan_and_import_provider_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            project = tmp / "project"
            old_project = tmp / "old-project"
            home = tmp / "home"
            project.mkdir()
            old_project.mkdir()
            seen: list[tuple[str, str, str]] = []
            sent: list[tuple[int, str]] = []

            self._main(["--workspace", str(workspace.root), "projects", "create", "proj", "--title", "Project One", "--cwd", str(project)])
            self._main(["--workspace", str(workspace.root), "agents", "create", "owner", "--project", "proj", "--adapter", "kimi", "--cwd", str(project)])
            task_out = self._main(["--workspace", str(workspace.root), "tasks", "create", "Adopt old session", "--project", "proj"])
            task_id = re.search(r"\((task-[^)]+)\)", task_out).group(1)

            kimi_home = home / ".kimi"
            kimi_home.mkdir(parents=True)
            kimi_home.joinpath("kimi.json").write_text(
                json.dumps({"work_dirs": [{"path": str(old_project), "last_session_id": "kimi-session-1"}]}),
                encoding="utf-8",
            )
            session_dir = kimi_home / "sessions" / hashlib.md5(str(old_project).encode("utf-8")).hexdigest() / "kimi-session-1"
            session_dir.mkdir(parents=True)
            session_dir.joinpath("state.json").write_text(json.dumps({"custom_title": "Kimi old work"}), encoding="utf-8")

            AgentRegistry(workspace).upsert(
                agent_id="assistant",
                title="AgentDeck Assistant",
                role="manager",
                adapter="echo",
                project_dir=str(tmp),
                replace=False,
            )
            AgentRegistry(workspace).set_role_template("assistant", DEFAULT_ASSISTANT_TEMPLATE)

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                seen.append((request.task or "", request.prompt, request.agent or ""))
                return RunServiceResult(
                    session_id="session-assistant-import",
                    final_text=(
                        "I found the old Kimi session and will bind it.\n"
                        f"AGENTDECK_ACTION: /session scan kimi {old_project}\n"
                        f"AGENTDECK_ACTION: /session import 1 project proj task {task_id} agent owner"
                    ),
                    events=[],
                    agent_id="assistant",
                    adapter="echo",
                    task_id=request.task or "",
                )

            old_home = os.environ.get("HOME")
            os.environ["HOME"] = str(home)
            try:
                queue = TelegramJobQueue(workspace, sender=lambda chat_id, text: sent.append((chat_id, text)), runner=runner)
                handler = TelegramCommandHandler(workspace, job_queue=queue)
                reply = asyncio.run(handler.handle_text("接管旧 Kimi session", chat_id=42))[0]
                job_id = re.search(r"Assistant job started: (job-\S+)", reply).group(1)
                queue.wait(job_id, timeout=2)
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home

            self.assertEqual(seen, [("", "接管旧 Kimi session", "assistant")])
            self.assertTrue(any("Assistant action executed: /session scan" in text for _, text in sent))
            self.assertTrue(any("Assistant action executed: /session import 1" in text for _, text in sent))
            task = TaskBoard(workspace).get(task_id)
            assert task is not None
            session = SessionRegistry(workspace).get(task.session_id)
            assert session is not None
            self.assertEqual(session.provider_session_id, "kimi-session-1")
            self.assertEqual(session.provider_session_kind, "kimi_session")
            self.assertEqual(session.project_dir, str(project.resolve()))

    def test_assistant_blocks_unsafe_marked_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            seen: list[tuple[str, str, str]] = []
            sent: list[tuple[int, str]] = []

            AgentRegistry(workspace).upsert(
                agent_id="assistant",
                title="AgentDeck Assistant",
                role="manager",
                adapter="echo",
                project_dir=str(tmp),
                replace=False,
            )
            AgentRegistry(workspace).set_role_template("assistant", DEFAULT_ASSISTANT_TEMPLATE)

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                seen.append((request.task or "", request.prompt, request.agent or ""))
                return RunServiceResult(
                    session_id="session-assistant-blocked",
                    final_text="That needs a task first.\nAGENTDECK_ACTION: /run do work",
                    events=[],
                    agent_id="assistant",
                    adapter="echo",
                    task_id=request.task or "",
                )

            queue = TelegramJobQueue(workspace, sender=lambda chat_id, text: sent.append((chat_id, text)), runner=runner)
            handler = TelegramCommandHandler(workspace, job_queue=queue)

            reply = asyncio.run(handler.handle_text("run this for me", chat_id=42))[0]
            job_id = re.search(r"Assistant job started: (job-\S+)", reply).group(1)
            queue.wait(job_id, timeout=2)

            self.assertEqual(seen, [("", "run this for me", "assistant")])
            self.assertTrue(all("AGENTDECK_ACTION" not in text for _, text in sent))
            self.assertTrue(any("Assistant action blocked: /run do work" in text for _, text in sent))
            self.assertTrue(any("safe command whitelist" in text for _, text in sent))

    def test_restart_command_requires_restart_controller(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            handler = TelegramCommandHandler(workspace)

            reply = asyncio.run(handler.handle_text("/restart", chat_id=42))[0]

            self.assertIn("Restart is not available", reply)
            self.assertIn("agentdeck telegram restart", reply)

    def test_restart_command_invokes_restart_callback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            calls: list[str] = []
            handler = TelegramCommandHandler(workspace, restart_callback=lambda: calls.append("restart"))

            reply = asyncio.run(handler.handle_text("/restart@minsys_bot", chat_id=42))[0]

            self.assertEqual(calls, ["restart"])
            self.assertIn("AgentDeck restart requested", reply)
            self.assertIn("reload shortly", reply)
            self.assertIn("completion notice", reply)
            notices = TelegramRestartNoticeStore(workspace).pop_for_bot("")
            self.assertEqual(len(notices), 1)
            self.assertEqual(notices[0]["chat_id"], 42)

    def test_natural_language_restart_agentdeck_bypasses_assistant(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            calls: list[str] = []
            seen: list[RunRequest] = []

            AgentRegistry(workspace).upsert(
                agent_id="assistant",
                title="AgentDeck Assistant",
                role="manager",
                adapter="echo",
                project_dir=tmpdir,
                replace=False,
            )

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                seen.append(request)
                return RunServiceResult(
                    session_id="assistant-session",
                    final_text="assistant should not run",
                    events=[],
                    agent_id="assistant",
                    adapter="echo",
                )

            handler = TelegramCommandHandler(
                workspace,
                runner=runner,
                restart_callback=lambda: calls.append("restart"),
            )

            reply = asyncio.run(handler.handle_text("请重启一下agentdeck", chat_id=42))[0]

            self.assertEqual(calls, ["restart"])
            self.assertEqual(seen, [])
            self.assertIn("AgentDeck restart requested", reply)

    def test_telegram_server_sends_restart_completion_notice(self) -> None:
        class NoticeApi:
            def __init__(self) -> None:
                self.messages: list[tuple[int, str]] = []

            def get_updates(self, *, offset: int | None = None, timeout: int = 30) -> list[dict[str, object]]:
                return []

            def send_message(self, chat_id: int, text: str) -> None:
                self.messages.append((chat_id, text))

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            TelegramRestartNoticeStore(workspace).add(bot_id="minsys-bot4", chat_id=42)
            api = NoticeApi()
            server = TelegramServer(
                workspace,
                api,
                TelegramConfig(token="123456:ABC", bot_id="minsys-bot4", poll_timeout=0),
                restart_callback=None,
            )

            server.serve_forever(once=True)

            self.assertEqual(len(api.messages), 1)
            self.assertEqual(api.messages[0][0], 42)
            self.assertIn("AgentDeck restarted.", api.messages[0][1])
            self.assertIn("bot: minsys-bot4", api.messages[0][1])
            self.assertEqual(TelegramRestartNoticeStore(workspace).pop_for_bot("minsys-bot4"), [])

    def test_telegram_server_persists_update_offset_before_restart_replay(self) -> None:
        class ReplayApi:
            def __init__(self) -> None:
                self.offsets: list[int | None] = []
                self.messages: list[tuple[int, str]] = []

            def get_updates(self, *, offset: int | None = None, timeout: int = 30) -> list[dict[str, object]]:
                self.offsets.append(offset)
                if offset is None or offset <= 10:
                    return [
                        {
                            "update_id": 10,
                            "message": {
                                "chat": {"id": 42},
                                "text": "/restart",
                            },
                        }
                    ]
                return []

            def send_message(self, chat_id: int, text: str) -> None:
                self.messages.append((chat_id, text))

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            calls: list[str] = []

            first_api = ReplayApi()
            first = TelegramServer(
                workspace,
                first_api,
                TelegramConfig(token="123456:ABC", bot_id="minsys-bot4", poll_timeout=0),
                restart_callback=lambda: calls.append("restart"),
            )
            first.serve_forever(once=True)

            self.assertEqual(calls, ["restart"])
            self.assertEqual(TelegramUpdateOffsetStore(workspace).get("minsys-bot4"), 11)

            second_api = ReplayApi()
            second = TelegramServer(
                workspace,
                second_api,
                TelegramConfig(token="123456:ABC", bot_id="minsys-bot4", poll_timeout=0),
                restart_callback=lambda: calls.append("restart"),
            )
            second.serve_forever(once=True)

            self.assertEqual(calls, ["restart"])
            self.assertEqual(second_api.offsets, [11])
            self.assertTrue(any("AgentDeck restarted." in text for _, text in second_api.messages))

    def test_telegram_server_acks_update_before_dispatch(self) -> None:
        class OneUpdateApi:
            def get_updates(self, *, offset: int | None = None, timeout: int = 30) -> list[dict[str, object]]:
                return [
                    {
                        "update_id": 20,
                        "message": {
                            "chat": {"id": 42},
                            "text": "hello",
                        },
                    }
                ]

            def send_message(self, chat_id: int, text: str) -> None:
                pass

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            server = TelegramServer(
                workspace,
                OneUpdateApi(),
                TelegramConfig(token="123456:ABC", bot_id="minsys-bot4", poll_timeout=0),
                restart_callback=None,
            )
            seen_offsets: list[int | None] = []

            def handle(update: dict[str, object]) -> None:
                seen_offsets.append(TelegramUpdateOffsetStore(workspace).get("minsys-bot4"))

            server._handle_update = handle  # type: ignore[method-assign]

            server.serve_forever(once=True)

            self.assertEqual(seen_offsets, [21])

    def test_natural_language_restart_question_still_routes_to_assistant(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            calls: list[str] = []
            seen: list[RunRequest] = []

            AgentRegistry(workspace).upsert(
                agent_id="assistant",
                title="AgentDeck Assistant",
                role="manager",
                adapter="echo",
                project_dir=tmpdir,
                replace=False,
            )

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                seen.append(request)
                return RunServiceResult(
                    session_id="assistant-session",
                    final_text="because updates need reload",
                    events=[],
                    agent_id="assistant",
                    adapter="echo",
                )

            handler = TelegramCommandHandler(
                workspace,
                runner=runner,
                restart_callback=lambda: calls.append("restart"),
            )

            reply = asyncio.run(handler.handle_text("为什么要重启 agentdeck？", chat_id=42))[0]

            self.assertEqual(calls, [])
            self.assertEqual(len(seen), 1)
            self.assertIn("because updates need reload", reply)

    def test_restart_command_blocks_active_jobs_unless_forced(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()
            registry = JobRegistry(workspace)
            active = registry.create(interface="telegram", chat_id=42, task_id="task-a", prompt="work")
            registry.set_status(active.job_id, "running")
            calls: list[str] = []
            handler = TelegramCommandHandler(workspace, restart_callback=lambda: calls.append("restart"))

            blocked = asyncio.run(handler.handle_text("/restart", chat_id=42))[0]

            self.assertEqual(calls, [])
            self.assertIn("Restart not started", blocked)
            self.assertIn(active.job_id, blocked)
            self.assertIn("/restart force", blocked)

            forced = asyncio.run(handler.handle_text("/restart force", chat_id=42))[0]

            self.assertEqual(calls, ["restart"])
            self.assertIn("Forced restart", forced)

    def test_video_command_sends_project_relative_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            project_dir = tmp / "project"
            project_dir.mkdir()
            video = project_dir / "demo.mp4"
            video.write_bytes(b"fake mp4")
            ProjectRegistry(workspace).upsert(
                project_id="proj",
                title="Project One",
                project_dir=project_dir,
            )
            sent: list[tuple[int, Path, str]] = []
            handler = TelegramCommandHandler(
                workspace,
                video_sender=lambda chat_id, path, caption: sent.append((chat_id, path, caption)),
            )
            handler.chat_state.set_current_project(42, "proj")

            reply = asyncio.run(handler.handle_text('/video demo.mp4 "wide view"', chat_id=42))[0]

            self.assertEqual(sent, [(42, video.resolve(), "wide view")])
            self.assertIn("Video sent", reply)
            self.assertIn("demo.mp4", reply)
            self.assertIn("caption: wide view", reply)

    def test_telegram_server_injects_video_sender(self) -> None:
        class VideoApi:
            def __init__(self, video_path: Path) -> None:
                self.video_path = video_path
                self.messages: list[tuple[int, str]] = []
                self.videos: list[tuple[int, Path, str]] = []

            def get_updates(self, *, offset: int | None = None, timeout: int = 30) -> list[dict[str, object]]:
                return [
                    {
                        "update_id": 1,
                        "message": {
                            "chat": {"id": 42},
                            "text": "/video clip.mp4 caption",
                        },
                    }
                ]

            def send_message(self, chat_id: int, text: str) -> None:
                self.messages.append((chat_id, text))

            def send_video(self, chat_id: int, path: Path, caption: str = "") -> None:
                self.videos.append((chat_id, path, caption))

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            project_dir = tmp / "project"
            project_dir.mkdir()
            video = project_dir / "clip.mp4"
            video.write_bytes(b"fake mp4")
            ProjectRegistry(workspace).upsert(
                project_id="proj",
                title="Project One",
                project_dir=project_dir,
            )
            api = VideoApi(video)
            server = TelegramServer(
                workspace,
                api,
                TelegramConfig(token="123456:ABC", bot_id="minsys-bot3", poll_timeout=0),
                restart_callback=None,
            )

            server.serve_forever(once=True)

            self.assertEqual(api.videos, [(42, video.resolve(), "caption")])
            self.assertTrue(api.messages)
            self.assertIn("Video sent", api.messages[0][1])

    def test_telegram_polling_error_does_not_escape_server_loop(self) -> None:
        class FailingApi:
            def get_updates(self, *, offset: int | None = None, timeout: int = 30) -> list[dict[str, object]]:
                raise RuntimeError("HTTP Error 409: Conflict")

            def send_message(self, chat_id: int, text: str) -> None:
                raise AssertionError("send_message should not be called")

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            server = TelegramServer(
                workspace,
                FailingApi(),
                TelegramConfig(token="123456:ABC", bot_id="minsys-bot3", poll_timeout=0),
                restart_callback=None,
            )
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                server.serve_forever(once=True)

            self.assertIn("telegram polling error bot=minsys-bot3", stdout.getvalue())
            self.assertIn("409", stdout.getvalue())

    def test_telegram_send_error_does_not_escape_server_loop(self) -> None:
        class FailingSendApi:
            def __init__(self) -> None:
                self.calls = 0

            def get_updates(self, *, offset: int | None = None, timeout: int = 30) -> list[dict[str, object]]:
                self.calls += 1
                if self.calls > 1:
                    return []
                return [
                    {
                        "update_id": 1,
                        "message": {
                            "chat": {"id": 42},
                            "text": "hello",
                        },
                    }
                ]

            def send_message(self, chat_id: int, text: str) -> None:
                raise RuntimeError("HTTP Error 502: Bad Gateway")

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            server = TelegramServer(
                workspace,
                FailingSendApi(),
                TelegramConfig(token="123456:ABC", bot_id="minsys-bot5", poll_timeout=0),
                restart_callback=None,
            )
            # Avoid agent/registry setup; just ensure send failure is swallowed.
            server.handler.handle_text = lambda text, chat_id=None: ["reply"]  # type: ignore[method-assign]
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                server.serve_forever(once=True)

            self.assertIn("telegram send error bot=minsys-bot5", stdout.getvalue())
            self.assertIn("502", stdout.getvalue())

    def test_telegram_server_writes_command_audit_log(self) -> None:
        class StatusApi:
            def __init__(self) -> None:
                self.messages: list[tuple[int, str]] = []

            def get_updates(self, *, offset: int | None = None, timeout: int = 30) -> list[dict[str, object]]:
                return [
                    {
                        "update_id": 1,
                        "message": {
                            "chat": {"id": 42},
                            "text": "/status",
                        },
                    }
                ]

            def send_message(self, chat_id: int, text: str) -> None:
                self.messages.append((chat_id, text))

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            api = StatusApi()
            server = TelegramServer(
                workspace,
                api,
                TelegramConfig(token="123456:ABC", bot_id="minsys-bot1", poll_timeout=0),
                restart_callback=None,
            )

            server.serve_forever(once=True)

            audit_path = workspace.root / "telegram" / "commands.jsonl"
            records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["bot_id"], "minsys-bot1")
            self.assertEqual(records[0]["chat_id"], 42)
            self.assertEqual(records[0]["command"], "/status")
            self.assertEqual(records[0]["outcome"], "handled")
            self.assertGreaterEqual(records[0]["reply_count"], 1)

    def test_assistant_can_execute_restart_marked_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            calls: list[str] = []

            AgentRegistry(workspace).upsert(
                agent_id="assistant",
                title="AgentDeck Assistant",
                role="manager",
                adapter="echo",
                project_dir=str(tmp),
                replace=False,
            )
            AgentRegistry(workspace).set_role_template("assistant", DEFAULT_ASSISTANT_TEMPLATE)

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                return RunServiceResult(
                    session_id="session-assistant-restart",
                    final_text="I will reload AgentDeck now.\nAGENTDECK_ACTION: /restart",
                    events=[],
                    agent_id="assistant",
                    adapter="echo",
                    task_id=request.task or "",
                )

            handler = TelegramCommandHandler(
                workspace,
                runner=runner,
                restart_callback=lambda: calls.append("restart"),
            )

            reply = asyncio.run(handler.handle_text("更新后重启一下", chat_id=42))[0]

            self.assertEqual(calls, ["restart"])
            self.assertNotIn("AGENTDECK_ACTION", reply)
            self.assertIn("Assistant action executed: /restart", reply)
            self.assertIn("AgentDeck restart requested", reply)

    def test_bot_specific_assistant_and_assistant_mode_clear_current_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            project = tmp / "project"
            project.mkdir()
            seen: list[tuple[str, str, str]] = []

            self._main(["--workspace", str(workspace.root), "projects", "create", "proj", "--title", "Project One", "--cwd", str(project)])
            self._main(["--workspace", str(workspace.root), "agents", "create", "owner", "--project", "proj", "--adapter", "echo"])
            task_out = self._main(["--workspace", str(workspace.root), "tasks", "create", "Phone task", "--project", "proj"])
            task_id = re.search(r"\((task-[^)]+)\)", task_out).group(1)
            AgentRegistry(workspace).upsert(
                agent_id="assistant-minsys-bot3",
                title="Minsys Bot 3 Assistant",
                role="manager",
                adapter="echo",
                project_dir=str(tmp),
                replace=False,
            )
            AgentRegistry(workspace).set_role_template("assistant-minsys-bot3", DEFAULT_ASSISTANT_TEMPLATE)

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                seen.append((request.task or "", request.prompt, request.agent or ""))
                return RunServiceResult(
                    session_id="session-bot-assistant",
                    final_text=f"done: {request.prompt}",
                    events=[],
                    agent_id=request.agent or "owner",
                    adapter="echo",
                    task_id=request.task or "",
                )

            queue = TelegramJobQueue(workspace, sender=lambda chat_id, text: None, runner=runner)
            handler = TelegramCommandHandler(workspace, job_queue=queue, assistant_agent_id="assistant-minsys-bot3")

            asyncio.run(handler.handle_text(f"/use {task_id}", chat_id=42))
            task_reply = asyncio.run(handler.handle_text("continue work", chat_id=42))[0]
            task_job = re.search(r"Job started: (job-\S+)", task_reply).group(1)
            queue.wait(task_job, timeout=2)

            assistant_reply = asyncio.run(handler.handle_text("/assistant", chat_id=42))[0]
            self.assertIn("Assistant mode enabled.", assistant_reply)
            self.assertIn("assistant-minsys-bot3", assistant_reply)
            self.assertEqual(handler.chat_state.current_task(42), "")

            routed = asyncio.run(handler.handle_text("help route me", chat_id=42))[0]
            assistant_job = re.search(r"Assistant job started: (job-\S+)", routed).group(1)
            queue.wait(assistant_job, timeout=2)

            self.assertEqual(
                seen,
                [
                    (task_id, "continue work", ""),
                    ("", "help route me", "assistant-minsys-bot3"),
                ],
            )

    def test_context_and_handoffs_show_current_task_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()
            task = TaskBoard(workspace).create(
                title="Memory task",
                description="Keep the executor aligned",
                project_id="proj",
                agent_id="owner",
            )
            TaskBoard(workspace).attach_session(task.task_id, "session-memory")
            SessionStateStore(workspace).write(
                SessionStateCard(
                    session_id="session-memory",
                    task_id=task.task_id,
                    project_id="proj",
                    agent_id="owner",
                    current_state="State card exists",
                    next_step="Show it on Telegram",
                    decisions=["Use run_service context as the source of truth"],
                )
            )
            ProgressJournal(workspace).append(
                kind="handoff",
                summary="Context injection is implemented",
                task_id=task.task_id,
                session_id="session-memory",
                next_steps=["Add phone visibility"],
            )
            handler = TelegramCommandHandler(workspace)

            asyncio.run(handler.handle_text(f"/use {task.task_id}", chat_id=42))
            initial_context = asyncio.run(handler.handle_text("/context", chat_id=42))[0]
            self.assertIn("AgentDeck context:", initial_context)
            self.assertIn("State card exists", initial_context)
            self.assertIn("Context injection is implemented", initial_context)

            review = asyncio.run(handler.handle_text("/review Keep the next patch narrow", chat_id=42))[0]
            self.assertIn("Manager review recorded: Memory task", review)

            context = asyncio.run(handler.handle_text("/context", chat_id=42))[0]
            self.assertIn("AgentDeck context:", context)
            self.assertIn("Context injection is implemented", context)
            self.assertIn("Recent manager reviews:", context)
            self.assertIn("Keep the next patch narrow", context)

            handoffs = asyncio.run(handler.handle_text("/handoffs", chat_id=42))[0]
            self.assertIn("Handoffs: Memory task", handoffs)
            self.assertIn("Context injection is implemented", handoffs)
            self.assertIn("next: Add phone visibility", handoffs)
            self.assertNotIn("Keep the next patch narrow", handoffs)

            reviews = asyncio.run(handler.handle_text("/reviews", chat_id=42))[0]
            self.assertIn("Manager reviews: Memory task", reviews)
            self.assertIn("Keep the next patch narrow", reviews)

            empty_memories = asyncio.run(handler.handle_text("/memories", chat_id=42))[0]
            self.assertIn("No durable memories for task: Memory task", empty_memories)

            compact = asyncio.run(handler.handle_text("/compact --pin Phone snapshot", chat_id=42))[0]
            self.assertIn("Memory compacted:", compact)
            self.assertIn("title: Phone snapshot", compact)
            self.assertIn("pinned: yes", compact)
            self.assertIn("owner: proj", compact)
            path_match = re.search(r"path: (.+)", compact)
            assert path_match is not None
            memory_path = Path(path_match.group(1))
            memory_text = memory_path.read_text(encoding="utf-8")
            self.assertIn("source: telegram-compact", memory_text)
            self.assertIn("pinned: true", memory_text)
            self.assertIn("This memory was generated from structured AgentDeck state", memory_text)
            self.assertIn("Keep the next patch narrow", memory_text)

            memories = asyncio.run(handler.handle_text("/memories", chat_id=42))[0]
            self.assertIn("Durable memories: Memory task", memories)
            self.assertIn("Phone snapshot", memories)
            self.assertIn("scope: project:proj", memories)
            self.assertIn("pinned: yes", memories)

            context_with_memory = asyncio.run(handler.handle_text("/context", chat_id=42))[0]
            self.assertIn("Relevant durable memories:", context_with_memory)
            self.assertIn("Phone snapshot [project:proj] pinned", context_with_memory)

            disabled = asyncio.run(handler.handle_text("/memory disable 1", chat_id=42))[0]
            self.assertIn("Memory disabled: Phone snapshot", disabled)
            self.assertIn("It will no longer be injected into focus context.", disabled)

            context_without_memory = asyncio.run(handler.handle_text("/context", chat_id=42))[0]
            self.assertNotIn("Phone snapshot [project:proj]", context_without_memory)

            enabled = asyncio.run(handler.handle_text("/memory enable 1", chat_id=42))[0]
            self.assertIn("Memory enabled: Phone snapshot", enabled)
            self.assertIn("It can be retrieved into future focus context again.", enabled)

            context_after_enable = asyncio.run(handler.handle_text("/context", chat_id=42))[0]
            self.assertIn("Phone snapshot [project:proj] pinned", context_after_enable)

            missing = asyncio.run(handler.handle_text("/context missing-task", chat_id=42))[0]
            self.assertIn("No current focus", missing)

    def test_focus_handoffs_and_reviews_use_current_focus(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            project_dir = tmp / "project"
            project_dir.mkdir()
            ProjectRegistry(workspace).upsert(project_id="proj", project_dir=project_dir)
            AgentRegistry(workspace).upsert(agent_id="owner", project_id="proj", adapter="echo", project_dir=project_dir)
            SessionRegistry(workspace).upsert_start(
                session_id="session-focus-progress",
                agent_id="owner",
                adapter="echo",
                project_dir=project_dir,
                prompt="Work on focus progress.",
                project_id="proj",
            )
            focus = FocusRegistry(workspace).create(
                title="Focus progress",
                description="Keep phone progress tied to focus.",
                project_id="proj",
                agent_id="owner",
                directory=project_dir,
                session_id="session-focus-progress",
            )
            ProgressJournal(workspace).append(
                kind="handoff",
                summary="Focus handoff is recorded",
                project_id="proj",
                focus_id=focus.focus_id,
                session_id="session-focus-progress",
                next_steps=["Show focus progress on Telegram"],
            )
            handler = TelegramCommandHandler(workspace)

            selected = asyncio.run(handler.handle_text(f"/use focus {focus.focus_id}", chat_id=42))[0]
            self.assertIn("Focus selected.", selected)

            context = asyncio.run(handler.handle_text("/context", chat_id=42))[0]
            self.assertIn("Focus progress", context)
            self.assertIn("Focus handoff is recorded", context)

            review = asyncio.run(handler.handle_text("/review Keep the focus path narrow", chat_id=42))[0]
            self.assertIn("Manager review recorded: Focus progress", review)
            self.assertIn(f"focus: {focus.focus_id}", review)

            reviews = ProgressJournal(workspace).list(kind="manager-review", focus_id=focus.focus_id)
            self.assertEqual(len(reviews), 1)
            self.assertEqual(reviews[0].summary, "Keep the focus path narrow")
            self.assertEqual(reviews[0].task_id, "")

            card = SessionStateStore(workspace).get("session-focus-progress")
            assert card is not None
            self.assertEqual(card.focus_id, focus.focus_id)
            self.assertEqual(card.current_state, "Keep the focus path narrow")

            handoffs = asyncio.run(handler.handle_text("/handoffs", chat_id=42))[0]
            self.assertIn("Handoffs: Focus progress", handoffs)
            self.assertIn("Focus handoff is recorded", handoffs)
            self.assertIn("next: Show focus progress on Telegram", handoffs)

            listed_reviews = asyncio.run(handler.handle_text("/reviews", chat_id=42))[0]
            self.assertIn("Manager reviews: Focus progress", listed_reviews)
            self.assertIn("noted: Keep the focus path narrow", listed_reviews)

            updated_context = asyncio.run(handler.handle_text("/context", chat_id=42))[0]
            self.assertIn("Recent manager reviews:", updated_context)
            self.assertIn("Keep the focus path narrow", updated_context)

    def test_project_state_and_decisions_are_visible_from_telegram(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            project_dir = tmp / "project"
            project_dir.mkdir()
            self._main(["--workspace", str(workspace.root), "projects", "create", "proj", "--title", "Project One", "--cwd", str(project_dir)])
            ProjectStateStore(workspace).update(
                "proj",
                goal="Coordinate two agents",
                phase="memory",
                current_focus="Expose project direction",
                next_steps=["Use decisions in task context"],
                constraints=["Keep raw transcripts out of memory"],
            )
            ProjectStateStore(workspace).add_decision(
                "proj",
                "Use project state as the manager-owned direction",
                reason="Executors need stable guidance",
            )

            handler = TelegramCommandHandler(workspace)
            asyncio.run(handler.handle_text("/use project proj", chat_id=42))

            state = asyncio.run(handler.handle_text("/projectstate", chat_id=42))[0]
            self.assertIn("Project state: Project One", state)
            self.assertIn("goal: Coordinate two agents", state)
            self.assertIn("constraints:", state)

            decisions = asyncio.run(handler.handle_text("/decisions", chat_id=42))[0]
            self.assertIn("Decisions: Project One", decisions)
            self.assertIn("Use project state as the manager-owned direction", decisions)

            recorded = asyncio.run(handler.handle_text("/decide Keep executor tasks small", chat_id=42))[0]
            self.assertIn("Decision recorded", recorded)
            self.assertIn("Keep executor tasks small", recorded)

    def test_phone_console_selects_project_agent_and_task_by_number(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            alpha = tmp / "alpha"
            beta = tmp / "beta"
            alpha.mkdir()
            beta.mkdir()
            self._main(["--workspace", str(workspace.root), "projects", "create", "alpha", "--title", "Alpha", "--cwd", str(alpha)])
            self._main(["--workspace", str(workspace.root), "projects", "create", "beta", "--title", "Beta", "--cwd", str(beta), "--default-agent", "beta-owner"])
            self._main(["--workspace", str(workspace.root), "agents", "create", "beta-owner", "--title", "Beta Owner", "--project", "beta", "--adapter", "echo", "--cwd", str(beta)])
            task_out = self._main(["--workspace", str(workspace.root), "tasks", "create", "Beta task", "--project", "beta", "--agent", "beta-owner"])
            task_id = re.search(r"\((task-[^)]+)\)", task_out).group(1)

            handler = TelegramCommandHandler(workspace)

            projects = asyncio.run(handler.handle_text("/projects", chat_id=42))[0]
            self.assertIn("1. Alpha", projects)
            self.assertIn("2. Beta", projects)

            project_selected = asyncio.run(handler.handle_text("/use project 2", chat_id=42))[0]
            self.assertIn("Current project set", project_selected)
            self.assertIn("Beta", project_selected)

            agents = asyncio.run(handler.handle_text("/agents", chat_id=42))[0]
            self.assertIn("1. Beta Owner", agents)
            agent_selected = asyncio.run(handler.handle_text("/use agent 1", chat_id=42))[0]
            self.assertIn("Current agent set", agent_selected)
            self.assertIn("beta-owner", agent_selected)

            tasks = asyncio.run(handler.handle_text("/tasks", chat_id=42))[0]
            self.assertIn("Tasks (Beta):", tasks)
            self.assertIn("1. Beta task", tasks)
            task_selected = asyncio.run(handler.handle_text("/use task 1", chat_id=42))[0]
            self.assertIn("Current task set", task_selected)
            self.assertIn(task_id, task_selected)

            status = asyncio.run(handler.handle_text("/status", chat_id=42))[0]
            self.assertIn("Project: Beta", status)
            self.assertIn("Agent: Beta Owner", status)
            self.assertIn("Legacy task: Beta task", status)

    def test_phone_console_creates_project_agent_and_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            project_dir = tmp / "mobile-project"
            project_dir.mkdir()
            handler = TelegramCommandHandler(workspace)

            project_reply = asyncio.run(
                handler.handle_text(f"/project new mobileproj {project_dir} Mobile Project", chat_id=42)
            )[0]
            self.assertIn("Project created and selected", project_reply)
            project = ProjectRegistry(workspace).resolve("mobileproj")
            assert project is not None
            self.assertEqual(project.title, "Mobile Project")
            self.assertEqual(project.project_dir, str(project_dir.resolve()))

            agent_reply = asyncio.run(handler.handle_text("/agent new developer codex developer Developer Agent", chat_id=42))[0]
            self.assertIn("Agent created and selected", agent_reply)
            agent = AgentRegistry(workspace).resolve("developer")
            assert agent is not None
            self.assertEqual(agent.project_id, "mobileproj")
            self.assertEqual(agent.adapter, "codex")
            self.assertEqual(agent.role, "developer")
            self.assertEqual(agent.project_dir, str(project_dir.resolve()))

            template_reply = asyncio.run(
                handler.handle_text("/agent template Keep executor changes narrow and verified", chat_id=42)
            )[0]
            self.assertIn("Agent template set: Developer Agent", template_reply)
            templated = AgentRegistry(workspace).resolve("developer")
            assert templated is not None
            self.assertIn("Keep executor changes narrow", role_template_for_agent(templated))

            clear_reply = asyncio.run(handler.handle_text("/agent template clear", chat_id=42))[0]
            self.assertIn("Agent template cleared: Developer Agent", clear_reply)
            cleared = AgentRegistry(workspace).resolve("developer")
            assert cleared is not None
            self.assertNotIn("role_template", cleared.metadata)

            task_reply = asyncio.run(handler.handle_text("/task new Implement phone flow", chat_id=42))[0]
            self.assertIn("Task created and selected", task_reply)
            task_id = re.search(r"id: (task-\S+)", task_reply).group(1)
            task = TaskBoard(workspace).get(task_id)
            assert task is not None
            self.assertEqual(task.project_id, "mobileproj")
            self.assertEqual(task.agent_id, "developer")
            self.assertEqual(task.title, "Implement phone flow")

    def test_auto_mode_starts_followup_jobs_and_can_be_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()
            task = TaskBoard(workspace).create(title="Auto task")
            seen: list[RunRequest] = []
            second_started = threading.Event()
            release_second = threading.Event()

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                seen.append(request)
                if len(seen) == 2:
                    second_started.set()
                    release_second.wait(timeout=2)
                return RunServiceResult(
                    session_id=f"session-auto-{len(seen)}",
                    final_text=f"done: {request.prompt}",
                    events=[],
                    agent_id="owner",
                    adapter="echo",
                    task_id=request.task or "",
                )

            old_delay = telegram_module.AUTO_CONTINUE_DELAY_SECONDS
            telegram_module.AUTO_CONTINUE_DELAY_SECONDS = 0.01
            try:
                queue = TelegramJobQueue(workspace, sender=lambda chat_id, text: None, runner=runner)
                handler = TelegramCommandHandler(workspace, job_queue=queue)

                asyncio.run(handler.handle_text(f"/use {task.task_id}", chat_id=42))
                reply = asyncio.run(handler.handle_text("/auto start 1", chat_id=42))[0]
                first_job = re.search(r"Job started: (job-\S+)", reply).group(1)
                queue.wait(first_job, timeout=2)
                self.assertTrue(second_started.wait(timeout=2))
                self.assertIsNone(seen[0].task)
                self.assertTrue(seen[0].focus)
                self.assertEqual(seen[0].approval_mode, "bypass")
                self.assertEqual(seen[1].approval_mode, "bypass")
                self.assertIn("请继续推进当前 Focus", seen[0].prompt)
                self.assertIn("请继续推进当前 Focus", seen[1].prompt)

                status = asyncio.run(handler.handle_text("/auto status", chat_id=42))[0]
                self.assertIn("Auto mode: on", status)
                self.assertIn("Auto task", status)
                self.assertIn("approval: auto", status)

                stopped = asyncio.run(handler.handle_text("/auto end", chat_id=42))[0]
                self.assertIn("Auto mode disabled", stopped)
                release_second.set()
                jobs = queue.list(chat_id=42, limit=5)
                second_job = next(job for job in jobs if job.job_id != first_job)
                queue.wait(second_job.job_id, timeout=2)
                time.sleep(0.05)
                self.assertEqual(len(seen), 2)
            finally:
                telegram_module.AUTO_CONTINUE_DELAY_SECONDS = old_delay

    def test_auto_start_creates_task_for_current_unlinked_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            workspace.ensure()
            project_dir = tmp / "imu-generation"
            project_dir.mkdir()
            self._main(
                [
                    "--workspace",
                    str(workspace.root),
                    "projects",
                    "create",
                    "imu-generation",
                    "--title",
                    "IMU Generation",
                    "--cwd",
                    str(project_dir),
                ]
            )
            self._main(
                [
                    "--workspace",
                    str(workspace.root),
                    "agents",
                    "create",
                    "imu-generation-owner",
                    "--project",
                    "imu-generation",
                    "--adapter",
                    "echo",
                    "--cwd",
                    str(project_dir),
                ]
            )
            session = SessionRegistry(workspace).upsert_start(
                session_id="session-imu-existing",
                agent_id="imu-generation-owner",
                adapter="echo",
                project_dir=str(project_dir),
                prompt="existing work",
                title="IMU Generation Owner",
            )
            seen: list[RunRequest] = []

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                seen.append(request)
                return RunServiceResult(
                    session_id=session.session_id,
                    final_text="auto done",
                    events=[],
                    agent_id="imu-generation-owner",
                    adapter="echo",
                    task_id=request.task or "",
                )

            old_delay = telegram_module.AUTO_CONTINUE_DELAY_SECONDS
            telegram_module.AUTO_CONTINUE_DELAY_SECONDS = 10.0
            try:
                queue = TelegramJobQueue(workspace, sender=lambda chat_id, text: None, runner=runner)
                handler = TelegramCommandHandler(workspace, job_queue=queue)

                selected = asyncio.run(handler.handle_text(f"/use session {session.session_id}", chat_id=42))[0]
                self.assertIn("task: -", selected)

                reply = asyncio.run(handler.handle_text("/auto start", chat_id=42))[0]
                self.assertIn("Auto mode enabled.", reply)
                self.assertIn("Created focus from current session:", reply)
                self.assertIn("IMU Generation Owner", reply)
                job_id = re.search(r"Job started: (job-\S+)", reply).group(1)
                queue.wait(job_id, timeout=2)

                current_focus_id = TelegramChatStateStore(workspace).current_focus(42)
                focus = FocusRegistry(workspace).get(current_focus_id)
                assert focus is not None
                self.assertEqual(focus.session_id, session.session_id)
                self.assertEqual(focus.project_id, "imu-generation")
                self.assertEqual(focus.agent_id, "imu-generation-owner")
                self.assertIsNone(seen[0].task)
                self.assertTrue(seen[0].focus)
                self.assertEqual(seen[0].session, session.session_id)
                asyncio.run(handler.handle_text("/auto end", chat_id=42))
            finally:
                telegram_module.AUTO_CONTINUE_DELAY_SECONDS = old_delay

    def test_adapter_error_marks_telegram_job_failed_and_records_incident(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()
            task = TaskBoard(workspace).create(title="Capacity task")
            sent: list[tuple[int, str]] = []

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                return RunServiceResult(
                    session_id="session-capacity",
                    final_text="",
                    events=[
                        AgentEvent(
                            EventKind.ERROR,
                            "owner",
                            "session-capacity",
                            text="codex exec exited with status 1",
                            payload={
                                "error_kind": "rate_limit",
                                "hint": "Wait and retry, or switch this agent to a cheaper/available model.",
                                "stderr": "Selected model is at capacity.",
                            },
                        )
                    ],
                    agent_id="owner",
                    adapter="codex",
                    task_id=request.task or "",
                )

            queue = TelegramJobQueue(workspace, sender=lambda chat_id, text: sent.append((chat_id, text)), runner=runner)
            handler = TelegramCommandHandler(workspace, job_queue=queue)

            asyncio.run(handler.handle_text(f"/use {task.task_id}", chat_id=42))
            reply = asyncio.run(handler.handle_text("/run reproduce capacity", chat_id=42))[0]
            job_id = re.search(r"Job started: (job-\S+)", reply).group(1)
            job = queue.wait(job_id, timeout=2)

            assert job is not None
            self.assertEqual(job.status, "error")
            self.assertEqual(job.error, "codex exec exited with status 1")
            self.assertTrue(any("Job failed" in text for _, text in sent))
            self.assertTrue(any("AgentDeck error handler:" in text for _, text in sent))
            incidents = ErrorIncidentStore(workspace).list()
            self.assertEqual(len(incidents), 1)
            self.assertEqual(incidents[0].error_kind, "rate_limit")
            self.assertIn("Selected model is at capacity.", incidents[0].text)

    def test_auto_active_job_check_is_bot_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()
            task = TaskBoard(workspace).create(title="Bot scoped auto task")

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                return RunServiceResult(
                    session_id="session-bot1-auto",
                    final_text="approval needed",
                    events=[],
                    agent_id="owner",
                    adapter="echo",
                    task_id=request.task or "",
                    approval_requested=True,
                )

            queue = TelegramJobQueue(
                workspace,
                sender=lambda chat_id, text: None,
                runner=runner,
                state_scope="minsys-bot1",
            )
            handler = TelegramCommandHandler(workspace, job_queue=queue, bot_id="minsys-bot1")
            registry = JobRegistry(workspace)
            other_bot_job = registry.create(
                interface="telegram",
                chat_id=42,
                task_id=task.task_id,
                prompt="other bot is running",
                metadata={"bot_id": "minsys-bot3"},
            )
            registry.set_status(other_bot_job.job_id, "running")

            asyncio.run(handler.handle_text(f"/use {task.task_id}", chat_id=42))
            reply = asyncio.run(handler.handle_text("/auto start", chat_id=42))[0]
            job_id = re.search(r"Job started: (job-\S+)", reply).group(1)
            queue.wait(job_id, timeout=2)

            self.assertNotIn(f"active job: {other_bot_job.job_id}", reply)
            bot1_job = registry.get(job_id)
            assert bot1_job is not None
            self.assertEqual(bot1_job.metadata.get("bot_id"), "minsys-bot1")
            active = queue.latest_for_chat(42, statuses={"running"})
            self.assertIsNone(active)

    def test_auto_human_mode_uses_fail_approval_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()
            task = TaskBoard(workspace).create(title="Human auto task")
            seen: list[RunRequest] = []

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                seen.append(request)
                return RunServiceResult(
                    session_id="session-human-auto",
                    final_text="done",
                    events=[],
                    agent_id="owner",
                    adapter="echo",
                    task_id=request.task or "",
                )

            queue = TelegramJobQueue(workspace, sender=lambda chat_id, text: None, runner=runner)
            handler = TelegramCommandHandler(workspace, job_queue=queue)

            asyncio.run(handler.handle_text(f"/use {task.task_id}", chat_id=42))
            reply = asyncio.run(handler.handle_text("/auto -h start 0.0001", chat_id=42))[0]
            self.assertIn("approval: human", reply)
            first_job = re.search(r"Job started: (job-\S+)", reply).group(1)
            queue.wait(first_job, timeout=2)
            self.assertEqual(seen[0].approval_mode, "fail")

            status = asyncio.run(handler.handle_text("/auto status", chat_id=42))[0]
            self.assertIn("approval: human", status)

    def test_auto_task_mode_stops_when_agent_marks_task_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()
            task = TaskBoard(workspace).create(title="Auto by task")
            seen: list[RunRequest] = []
            sent: list[tuple[int, str]] = []

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                seen.append(request)
                return RunServiceResult(
                    session_id="session-auto-task",
                    final_text="The focus is sufficiently complete.\nAGENTDECK_AUTO_FOCUS_DONE",
                    events=[],
                    agent_id="owner",
                    adapter="echo",
                    task_id=request.task or "",
                )

            old_delay = telegram_module.AUTO_CONTINUE_DELAY_SECONDS
            telegram_module.AUTO_CONTINUE_DELAY_SECONDS = 0.01
            try:
                queue = TelegramJobQueue(workspace, sender=lambda chat_id, text: sent.append((chat_id, text)), runner=runner)
                handler = TelegramCommandHandler(workspace, job_queue=queue)

                asyncio.run(handler.handle_text(f"/use {task.task_id}", chat_id=42))
                reply = asyncio.run(handler.handle_text("/auto task", chat_id=42))[0]
                self.assertIn("mode: focus", reply)
                first_job = re.search(r"Job started: (job-\S+)", reply).group(1)
                queue.wait(first_job, timeout=2)
                time.sleep(0.05)

                self.assertEqual(len(seen), 1)
                self.assertIn("AGENTDECK_AUTO_FOCUS_DONE", seen[0].prompt)
                self.assertTrue(any("Auto by focus stopped: focus judged complete." in text for _, text in sent))
                self.assertTrue(all("AGENTDECK_AUTO_FOCUS_DONE" not in text for _, text in sent))
                current_focus_id = TelegramChatStateStore(workspace).current_focus(42)
                updated = FocusRegistry(workspace).get(current_focus_id)
                assert updated is not None
                self.assertEqual(updated.status, "review")
                status = asyncio.run(handler.handle_text("/auto status", chat_id=42))[0]
                self.assertIn("Auto mode: off", status)
            finally:
                telegram_module.AUTO_CONTINUE_DELAY_SECONDS = old_delay

    def test_recent_lists_allow_numeric_task_and_job_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()
            board = TaskBoard(workspace)
            board.create(title="Older task")
            newer = board.create(title="Newer task")
            seen: list[tuple[str, str]] = []

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                seen.append((request.task or "", request.prompt))
                return RunServiceResult(
                    session_id="session-numbered",
                    final_text=f"done: {request.prompt}",
                    events=[],
                    agent_id="owner",
                    adapter="echo",
                    task_id=request.task or "",
                )

            queue = TelegramJobQueue(workspace, sender=lambda chat_id, text: None, runner=runner)
            handler = TelegramCommandHandler(workspace, job_queue=queue)

            tasks = asyncio.run(handler.handle_text("/tasks", chat_id=42))[0]
            self.assertIn("1.", tasks)
            selected = asyncio.run(handler.handle_text("/use 1", chat_id=42))[0]
            self.assertIn("Current task set", selected)
            self.assertIn(newer.title, selected)

            run_reply = asyncio.run(handler.handle_text("/run 1 numeric work", chat_id=42))[0]
            job_id = re.search(r"Job started: (job-\S+)", run_reply).group(1)
            queue.wait(job_id, timeout=2)
            self.assertEqual(seen, [(newer.task_id, "numeric work")])

            jobs = asyncio.run(handler.handle_text("/jobs", chat_id=42))[0]
            self.assertIn("1.", jobs)
            latest = asyncio.run(handler.handle_text("/job 1", chat_id=42))[0]
            self.assertIn(f"Job: {job_id}", latest)

            queued = queue.registry.create(interface="telegram", chat_id=42, task_id=newer.task_id, prompt="queued work")
            jobs = asyncio.run(handler.handle_text("/jobs", chat_id=42))[0]
            self.assertIn(queued.job_id, jobs)
            cancel_reply = asyncio.run(handler.handle_text("/cancel 1", chat_id=42))[0]
            self.assertIn(f"Job cancelled: {queued.job_id}", cancel_reply)

    def test_newtask_names_and_selects_task_from_telegram(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            project = tmp / "project"
            project.mkdir()

            self._main(["--workspace", str(workspace.root), "projects", "create", "proj", "--title", "Project One", "--cwd", str(project)])
            handler = TelegramCommandHandler(workspace)

            created = asyncio.run(handler.handle_text("/newtask Mobile named task", chat_id=42))[0]
            self.assertIn("Task created and selected", created)
            self.assertIn("Mobile named task", created)
            task_id = re.search(r"id: (task-\S+)", created).group(1)

            task = TaskBoard(workspace).get(task_id)
            assert task is not None
            self.assertEqual(task.title, "Mobile named task")
            self.assertEqual(task.project_id, "proj")

            current = asyncio.run(handler.handle_text("/current", chat_id=42))[0]
            self.assertIn("Mobile named task", current)

    def test_jobs_are_persisted_and_unfinished_jobs_are_marked_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()
            registry = JobRegistry(workspace)
            stale = registry.create(interface="telegram", chat_id=42, task_id="task-a", prompt="old work")
            done = registry.create(interface="telegram", chat_id=42, task_id="task-b", prompt="done work")
            registry.finish(done.job_id, status="done", session_id="session-done", final_text="done text")

            queue = TelegramJobQueue(workspace, sender=lambda chat_id, text: None)
            handler = TelegramCommandHandler(workspace, job_queue=queue)

            stale_after_restart = queue.get(stale.job_id)
            assert stale_after_restart is not None
            self.assertEqual(stale_after_restart.status, "interrupted")
            self.assertIn("restarted", stale_after_restart.error)

            done_after_restart = queue.get(done.job_id)
            assert done_after_restart is not None
            self.assertEqual(done_after_restart.status, "done")
            self.assertEqual(done_after_restart.final_text, "done text")

            jobs = asyncio.run(handler.handle_text("/jobs", chat_id=42))[0]
            self.assertIn(stale.job_id, jobs)
            self.assertIn(done.job_id, jobs)
            self.assertIn("status: interrupted", jobs)

            detail = asyncio.run(handler.handle_text(f"/job {done.job_id}", chat_id=42))[0]
            self.assertIn("status: done", detail)
            self.assertIn("done text", detail)

    def test_interrupted_job_can_resume_from_saved_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            project = tmp / "project"
            project.mkdir()
            self._main(["--workspace", str(workspace.root), "projects", "create", "proj", "--cwd", str(project)])
            self._main(["--workspace", str(workspace.root), "agents", "create", "owner", "--project", "proj", "--adapter", "echo"])
            task_out = self._main(["--workspace", str(workspace.root), "tasks", "create", "Interrupted task", "--project", "proj"])
            task_id = re.search(r"\((task-[^)]+)\)", task_out).group(1)
            SessionRegistry(workspace).upsert_start(
                session_id="session-interrupted",
                agent_id="owner",
                adapter="echo",
                project_dir=project,
                prompt="old",
            )
            TaskBoard(workspace).attach_session(task_id, "session-interrupted")
            registry = JobRegistry(workspace)
            old = registry.create(interface="telegram", chat_id=42, task_id=task_id, prompt="old work", job_id="job-old")
            registry.finish(old.job_id, status="interrupted", session_id="session-interrupted", error="restarted")
            seen: list[RunRequest] = []

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                seen.append(request)
                return RunServiceResult(
                    session_id=request.session or "session-new",
                    final_text="continued",
                    events=[],
                    agent_id="owner",
                    adapter="echo",
                    task_id=request.task or "",
                )

            queue = TelegramJobQueue(workspace, sender=lambda chat_id, text: None, runner=runner)
            handler = TelegramCommandHandler(workspace, job_queue=queue)

            reply = asyncio.run(handler.handle_text("/job resume job-old keep going", chat_id=42))[0]
            new_job_id = re.search(r"New job started: (job-\S+)", reply).group(1)
            queue.wait(new_job_id, timeout=2)

            self.assertIn("session: session-interrupted", reply)
            self.assertEqual(seen[0].session, "session-interrupted")
            self.assertEqual(seen[0].prompt, "keep going")

    def test_interrupted_job_without_session_resumes_from_task_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()
            task = TaskBoard(workspace).create(title="Task without session", status="doing")
            registry = JobRegistry(workspace)
            old = registry.create(interface="telegram", chat_id=42, task_id=task.task_id, prompt="old work", job_id="job-old")
            registry.finish(old.job_id, status="interrupted", error="restarted")
            seen: list[RunRequest] = []

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                seen.append(request)
                return RunServiceResult(
                    session_id="session-new",
                    final_text="continued",
                    events=[],
                    agent_id="owner",
                    adapter="echo",
                    task_id=request.task or "",
                )

            queue = TelegramJobQueue(workspace, sender=lambda chat_id, text: None, runner=runner)
            handler = TelegramCommandHandler(workspace, job_queue=queue)

            reply = asyncio.run(handler.handle_text("/job resume 1", chat_id=42))[0]
            new_job_id = re.search(r"New job started: (job-\S+)", reply).group(1)
            queue.wait(new_job_id, timeout=2)

            self.assertIn("session: not available", reply)
            self.assertEqual(seen[0].session, None)
            self.assertEqual(seen[0].task, task.task_id)
            self.assertIn("old work", seen[0].prompt)

    def test_cancel_queued_job_marks_it_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()
            queue = TelegramJobQueue(workspace, sender=lambda chat_id, text: None)
            queued = queue.registry.create(interface="telegram", chat_id=42, task_id="task-a", prompt="queued work")
            handler = TelegramCommandHandler(workspace, job_queue=queue)

            reply = asyncio.run(handler.handle_text(f"/cancel {queued.job_id}", chat_id=42))[0]
            self.assertIn("Job cancelled", reply)

            record = queue.get(queued.job_id)
            assert record is not None
            self.assertEqual(record.status, "cancelled")

            detail = asyncio.run(handler.handle_text(f"/job {queued.job_id}", chat_id=42))[0]
            self.assertIn("status: cancelled", detail)

    def test_cancel_running_job_records_cancel_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()
            task = TaskBoard(workspace).create(title="Cancellable task")
            started = threading.Event()
            release = threading.Event()
            sent: list[tuple[int, str]] = []

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                started.set()
                release.wait(timeout=2)
                return RunServiceResult(
                    session_id="session-cancel",
                    final_text="finished after cancel request",
                    events=[],
                    agent_id="owner",
                    adapter="echo",
                    task_id=request.task or "",
                )

            queue = TelegramJobQueue(workspace, sender=lambda chat_id, text: sent.append((chat_id, text)), runner=runner)
            handler = TelegramCommandHandler(workspace, job_queue=queue)

            reply = asyncio.run(handler.handle_text(f"/run {task.task_id} cancellable work", chat_id=42))[0]
            job_id = re.search(r"Job started: (job-\S+)", reply).group(1)
            self.assertTrue(started.wait(timeout=1))

            cancel_reply = asyncio.run(handler.handle_text(f"/cancel {job_id}", chat_id=42))[0]
            self.assertIn("Cancel requested", cancel_reply)
            self.assertIn("status: cancel_requested", cancel_reply)

            requested = queue.get(job_id)
            assert requested is not None
            self.assertEqual(requested.status, "cancel_requested")

            release.set()
            finished = queue.wait(job_id, timeout=2)
            assert finished is not None
            self.assertEqual(finished.status, "done")
            self.assertIn("not implemented yet", finished.error)
            self.assertEqual(len(sent), 1)
            self.assertIn("note: Cancellation was requested", sent[0][1])

    def test_cancel_without_id_uses_latest_cancellable_job_for_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()
            task = TaskBoard(workspace).create(title="Task A")
            started = threading.Event()
            release = threading.Event()

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                started.set()
                release.wait(timeout=2)
                return RunServiceResult(
                    session_id="session-latest",
                    final_text="finished",
                    events=[],
                    agent_id="owner",
                    adapter="echo",
                    task_id=request.task or "",
                )

            queue = TelegramJobQueue(workspace, sender=lambda chat_id, text: None, runner=runner)
            handler = TelegramCommandHandler(workspace, job_queue=queue)

            reply = asyncio.run(handler.handle_text(f"/run {task.task_id} latest job", chat_id=42))[0]
            job_id = re.search(r"Job started: (job-\S+)", reply).group(1)
            self.assertTrue(started.wait(timeout=1))

            cancel_reply = asyncio.run(handler.handle_text("/cancel", chat_id=42))[0]
            self.assertIn(f"Cancel requested: {job_id}", cancel_reply)

            requested = queue.get(job_id)
            assert requested is not None
            self.assertEqual(requested.status, "cancel_requested")
            release.set()
            queue.wait(job_id, timeout=2)

    def test_cancelled_event_marks_job_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()
            task = TaskBoard(workspace).create(title="Cancelled task")
            started = threading.Event()
            sent: list[tuple[int, str]] = []

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                started.set()
                while request.cancellation is not None and not request.cancellation.is_cancelled():
                    await asyncio.sleep(0.01)
                return RunServiceResult(
                    session_id="session-cancelled",
                    final_text="",
                    events=[
                        AgentEvent(
                            EventKind.CANCELLED,
                            "owner",
                            "session-cancelled",
                            text="cancelled by test",
                        )
                    ],
                    agent_id="owner",
                    adapter="echo",
                    task_id=request.task or "",
                )

            queue = TelegramJobQueue(workspace, sender=lambda chat_id, text: sent.append((chat_id, text)), runner=runner)
            handler = TelegramCommandHandler(workspace, job_queue=queue)

            reply = asyncio.run(handler.handle_text(f"/run {task.task_id} cancellable work", chat_id=42))[0]
            job_id = re.search(r"Job started: (job-\S+)", reply).group(1)
            self.assertTrue(started.wait(timeout=1))

            asyncio.run(handler.handle_text(f"/cancel {job_id}", chat_id=42))
            finished = queue.wait(job_id, timeout=2)
            assert finished is not None
            self.assertEqual(finished.status, "cancelled")
            self.assertEqual(finished.error, "cancelled by test")
            self.assertEqual(len(sent), 1)
            self.assertIn(f"Job cancelled: {job_id}", sent[0][1])

    def test_message_split_and_env_config(self) -> None:
        chunks = split_message("a" * 5000, limit=1000)
        self.assertEqual(len(chunks), 5)
        self.assertTrue(all(len(chunk) <= 1000 for chunk in chunks))

        old_allowed = os.environ.pop("AGENTDECK_TELEGRAM_ALLOWED_CHATS", None)
        old_token = os.environ.pop("AGENTDECK_TELEGRAM_TOKEN", None)
        try:
            config = config_from_env(token="token", allowed_chat_ids=["1", "bad", "2"], poll_timeout=7)
            self.assertEqual(config.token, "token")
            self.assertEqual(config.allowed_chat_ids, {1, 2})
            self.assertEqual(config.poll_timeout, 7)

            config = config_from_env(token="    Token: 123456:ABC_def-GHI   ")
            self.assertEqual(config.token, "123456:ABC_def-GHI")
        finally:
            if old_allowed is not None:
                os.environ["AGENTDECK_TELEGRAM_ALLOWED_CHATS"] = old_allowed
            if old_token is not None:
                os.environ["AGENTDECK_TELEGRAM_TOKEN"] = old_token

    def test_bot_scoped_chat_state_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            state_a = TelegramChatStateStore(workspace, scope="minsys-bot1")
            state_b = TelegramChatStateStore(workspace, scope="minsys-bot3")

            state_a.set_current_task(42, "task-a")
            state_b.set_current_task(42, "task-b")

            self.assertEqual(state_a.current_task(42), "task-a")
            self.assertEqual(state_b.current_task(42), "task-b")
            self.assertEqual(TelegramChatStateStore(workspace).current_task(42), "")

    def test_telegram_start_defaults_to_current_server_saved_bots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            registry = TelegramBotRegistry(workspace)
            registry.upsert(
                bot_id="minsys-bot1",
                title="Minsys Bot 1",
                token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                assistant_agent_id=assistant_agent_id_for_bot("minsys-bot1"),
                allowed_chat_ids=[42],
            )
            registry.upsert(
                bot_id="remote-bot",
                title="Remote Bot",
                token="223456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                server_id="other-server",
            )

            args = type(
                "Args",
                (),
                {"bot": "", "token": None, "assistant_agent": "", "allowed_chat_id": [], "poll_timeout": 9},
            )()
            configs = _telegram_configs_from_args(args, workspace)

            self.assertEqual([config.bot_id for config in configs], ["minsys-bot1"])
            self.assertEqual(configs[0].assistant_agent_id, assistant_agent_id_for_bot("minsys-bot1"))
            self.assertEqual(configs[0].allowed_chat_ids, {42})
            self.assertEqual(configs[0].poll_timeout, 9)

    def test_telegram_daemon_status_without_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "telegram", "status"])
            self.assertEqual(code, 1)
            self.assertIn("telegram service: stopped", stdout.getvalue())

    def _main(self, args: list[str]) -> str:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(args)
        self.assertEqual(code, 0)
        return stdout.getvalue()


if __name__ == "__main__":
    unittest.main()
