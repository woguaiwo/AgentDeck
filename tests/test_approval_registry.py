import contextlib
import io
import json
import re
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from agentdeck.cli import main
from agentdeck.core.config import Workspace
from agentdeck.core.events import AgentEvent, EventKind
from agentdeck.storage.approvals import ApprovalRegistry
from agentdeck.storage.sessions import SessionRegistry
from agentdeck.storage.tasks import TaskBoard


class ApprovalRegistryTests(unittest.TestCase):
    def test_registry_records_and_resolves_approval_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()
            registry = ApprovalRegistry(workspace)
            event = AgentEvent(
                EventKind.APPROVAL_REQUESTED,
                "agent-a",
                "session-a",
                text="Allow shell command?",
                payload={"type": "approval_requested", "provider": "codex", "command": "pytest"},
            )

            record = registry.record_request(
                event,
                adapter="codex",
                project_dir=tmpdir,
                project_id="Project A",
                task_id="task-a",
            )
            duplicate = registry.record_request(
                event,
                adapter="codex",
                project_dir=tmpdir,
                project_id="Project A",
                task_id="task-a",
            )

            self.assertEqual(record.approval_id, duplicate.approval_id)
            self.assertEqual(record.status, "pending")
            self.assertEqual(record.project_id, "project-a")
            self.assertEqual(record.task_id, "task-a")
            self.assertEqual(record.provider, "codex")

            resolved = registry.resolve_request(record.approval_id, status="approved", note="ok")
            assert resolved is not None
            self.assertEqual(resolved.status, "approved")
            self.assertEqual(resolved.resolution_note, "ok")

    def test_cli_run_task_records_pending_approval_and_blocks_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            project = tmp / "project"
            project.mkdir()
            fake = tmp / "fake_codex"
            fake.write_text(
                textwrap.dedent(
                    f"""\
                    #!{sys.executable}
                    import json
                    import time

                    print(json.dumps({{"type": "thread.started", "thread_id": "thread-approval"}}), flush=True)
                    print(json.dumps({{"type": "approval_requested", "text": "Allow shell command?"}}), flush=True)
                    time.sleep(5)
                    """
                ),
                encoding="utf-8",
            )
            fake.chmod(0o755)

            self._main(["--workspace", str(workspace.root), "projects", "create", "proj", "--cwd", str(project)])
            self._main(
                [
                    "--workspace",
                    str(workspace.root),
                    "agents",
                    "create",
                    "owner",
                    "--project",
                    "proj",
                    "--adapter",
                    "codex",
                    "--codex-bin",
                    str(fake),
                ]
            )
            task_out = self._main(["--workspace", str(workspace.root), "tasks", "create", "Needs approval", "--project", "proj"])
            task_id = re.search(r"\((task-[^)]+)\)", task_out).group(1)

            run_out = self._main(["--workspace", str(workspace.root), "run", "please run command", "--task", task_id])
            session_id = re.search(r"session_id: (\S+)", run_out).group(1)

            approvals = ApprovalRegistry(workspace).list(status="pending", task_id=task_id)
            self.assertEqual(len(approvals), 1)
            approval = approvals[0]
            self.assertEqual(approval.session_id, session_id)
            self.assertEqual(approval.project_id, "proj")
            self.assertEqual(approval.agent_id, "owner")
            self.assertIn("Allow shell command", approval.request_text)

            session = SessionRegistry(workspace).get(session_id)
            assert session is not None
            self.assertEqual(session.status, "waiting_approval")

            task = TaskBoard(workspace).get(task_id)
            assert task is not None
            self.assertEqual(task.status, "blocked")
            self.assertIn(approval.approval_id, task.notes[-1]["text"])

            listed = self._main(["--workspace", str(workspace.root), "approvals", "list", "--status", "pending"])
            self.assertIn(approval.approval_id, listed)
            shown = self._main(["--workspace", str(workspace.root), "approvals", "show", approval.approval_id])
            self.assertEqual(json.loads(shown)["approval_id"], approval.approval_id)

            approved = self._main(["--workspace", str(workspace.root), "approvals", "approve", approval.approval_id, "approved for test"])
            self.assertIn("status=approved", approved)
            resolved = ApprovalRegistry(workspace).get(approval.approval_id)
            assert resolved is not None
            self.assertEqual(resolved.status, "approved")

            task = TaskBoard(workspace).get(task_id)
            assert task is not None
            self.assertIn("Approval approved", task.notes[-1]["text"])

    def _main(self, args: list[str]) -> str:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(args)
        self.assertEqual(code, 0)
        return stdout.getvalue()


if __name__ == "__main__":
    unittest.main()
