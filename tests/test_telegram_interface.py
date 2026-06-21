import asyncio
import contextlib
import io
import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path

import agentdeck.interfaces.telegram as telegram_module
from agentdeck.cli import main
from agentdeck.core.config import Workspace
from agentdeck.core.events import AgentEvent, EventKind
from agentdeck.core.run_service import RunRequest, RunServiceResult
from agentdeck.interfaces.telegram import TelegramCommandHandler, TelegramJobQueue, config_from_env, split_message
from agentdeck.storage.approvals import ApprovalRegistry
from agentdeck.storage.agents import DEFAULT_ASSISTANT_TEMPLATE, AgentRegistry, role_template_for_agent
from agentdeck.storage.jobs import JobRegistry
from agentdeck.storage.progress import ProgressJournal
from agentdeck.storage.projects import ProjectRegistry
from agentdeck.storage.project_state import ProjectStateStore
from agentdeck.storage.session_state import SessionStateCard, SessionStateStore
from agentdeck.storage.sessions import SessionRegistry
from agentdeck.storage.tasks import TaskBoard


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
            self.assertIn("Current task: Phone control task", status)
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
            self.assertIn("No current task is selected", hint)
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
            self.assertIn("It will no longer be injected into task context.", disabled)

            context_without_memory = asyncio.run(handler.handle_text("/context", chat_id=42))[0]
            self.assertNotIn("Phone snapshot [project:proj]", context_without_memory)

            enabled = asyncio.run(handler.handle_text("/memory enable 1", chat_id=42))[0]
            self.assertIn("Memory enabled: Phone snapshot", enabled)
            self.assertIn("It can be retrieved into future task context again.", enabled)

            context_after_enable = asyncio.run(handler.handle_text("/context", chat_id=42))[0]
            self.assertIn("Phone snapshot [project:proj] pinned", context_after_enable)

            missing = asyncio.run(handler.handle_text("/context missing-task", chat_id=42))[0]
            self.assertIn("Task not found", missing)

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
            self.assertIn("Current task: Beta task", status)

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
                self.assertEqual(seen[0].task, task.task_id)
                self.assertEqual(seen[0].approval_mode, "bypass")
                self.assertEqual(seen[1].approval_mode, "bypass")
                self.assertIn("请继续推进当前任务", seen[0].prompt)
                self.assertIn("请继续推进当前任务", seen[1].prompt)

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
                    final_text="The task is sufficiently complete.\nAGENTDECK_AUTO_TASK_DONE",
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
                self.assertIn("mode: task", reply)
                first_job = re.search(r"Job started: (job-\S+)", reply).group(1)
                queue.wait(first_job, timeout=2)
                time.sleep(0.05)

                self.assertEqual(len(seen), 1)
                self.assertIn("AGENTDECK_AUTO_TASK_DONE", seen[0].prompt)
                self.assertTrue(any("Auto by task stopped: task judged complete." in text for _, text in sent))
                self.assertTrue(all("AGENTDECK_AUTO_TASK_DONE" not in text for _, text in sent))
                updated = TaskBoard(workspace).get(task.task_id)
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

    def test_telegram_daemon_status_without_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "telegram", "status"])
            self.assertEqual(code, 1)
            self.assertIn("telegram bot: stopped", stdout.getvalue())

    def _main(self, args: list[str]) -> str:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(args)
        self.assertEqual(code, 0)
        return stdout.getvalue()


if __name__ == "__main__":
    unittest.main()
