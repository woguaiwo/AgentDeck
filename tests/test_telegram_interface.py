import asyncio
import contextlib
import io
import os
import re
import tempfile
import unittest
from pathlib import Path

from agentdeck.cli import main
from agentdeck.core.config import Workspace
from agentdeck.core.events import AgentEvent, EventKind
from agentdeck.interfaces.telegram import TelegramCommandHandler, config_from_env, split_message
from agentdeck.storage.approvals import ApprovalRegistry
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
