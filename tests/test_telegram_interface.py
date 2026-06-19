import asyncio
import contextlib
import io
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path

from agentdeck.cli import main
from agentdeck.core.config import Workspace
from agentdeck.core.events import AgentEvent, EventKind
from agentdeck.core.run_service import RunRequest, RunServiceResult
from agentdeck.interfaces.telegram import TelegramCommandHandler, TelegramJobQueue, config_from_env, split_message
from agentdeck.storage.approvals import ApprovalRegistry
from agentdeck.storage.jobs import JobRegistry
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
            seen: list[tuple[str, str]] = []
            sent: list[tuple[int, str]] = []

            self._main(["--workspace", str(workspace.root), "projects", "create", "proj", "--title", "Project One", "--cwd", str(project)])
            self._main(["--workspace", str(workspace.root), "agents", "create", "owner", "--project", "proj", "--adapter", "echo"])
            task_out = self._main(["--workspace", str(workspace.root), "tasks", "create", "Named phone task", "--project", "proj"])
            task_id = re.search(r"\((task-[^)]+)\)", task_out).group(1)

            async def runner(workspace_arg: Workspace, request: RunRequest) -> RunServiceResult:
                seen.append((request.task or "", request.prompt))
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
            self.assertEqual(seen, [(task_id, "continue without ids")])

            latest = asyncio.run(handler.handle_text("/job", chat_id=42))[0]
            self.assertIn(f"Job: {job_id}", latest)
            self.assertIn("done: continue without ids", latest)

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

    def _main(self, args: list[str]) -> str:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(args)
        self.assertEqual(code, 0)
        return stdout.getvalue()


if __name__ == "__main__":
    unittest.main()
