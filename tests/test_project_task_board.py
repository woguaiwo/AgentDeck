import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from agentdeck.cli import main
from agentdeck.core.config import Workspace
from agentdeck.storage.projects import ProjectRegistry
from agentdeck.storage.sessions import SessionRegistry
from agentdeck.storage.tasks import TaskBoard


class ProjectTaskBoardTests(unittest.TestCase):
    def test_project_registry_normalizes_and_resolves_titles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()
            registry = ProjectRegistry(workspace)

            record = registry.upsert(
                project_id="Motion X",
                title="Motion-X",
                project_dir=tmpdir,
                team_id="Motion Team",
                default_agent_id="Owner",
            )

            self.assertEqual(record.project_id, "motion-x")
            self.assertEqual(record.team_id, "motion-team")
            self.assertEqual(record.default_agent_id, "owner")
            self.assertEqual(registry.resolve("Motion-X").project_id, "motion-x")

    def test_cli_project_task_run_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            workspace.ensure()
            project_dir = tmp / "motionx"
            project_dir.mkdir()

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "projects",
                        "create",
                        "motionx",
                        "--title",
                        "Motion-X",
                        "--cwd",
                        str(project_dir),
                        "--default-agent",
                        "owner",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertIn("Motion-X", stdout.getvalue())

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "projects", "list"])
            self.assertEqual(code, 0)
            self.assertIn("Motion-X\tmotionx", stdout.getvalue())

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "projects", "show", "Motion-X"])
            self.assertEqual(code, 0)
            shown_project = json.loads(stdout.getvalue())
            self.assertEqual(shown_project["project_id"], "motionx")
            self.assertEqual(shown_project["project_dir"], str(project_dir.resolve()))

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "agents",
                        "create",
                        "owner",
                        "--title",
                        "Motion-X Owner",
                        "--project",
                        "motionx",
                        "--adapter",
                        "echo",
                    ]
                )
            self.assertEqual(code, 0)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "agents", "show", "owner"])
            self.assertEqual(code, 0)
            shown_agent = json.loads(stdout.getvalue())
            self.assertEqual(shown_agent["project_id"], "motionx")
            self.assertEqual(shown_agent["team_id"], "motionx")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "tasks",
                        "create",
                        "Fix loader",
                        "--project",
                        "Motion-X",
                        "--priority",
                        "high",
                    ]
                )
            self.assertEqual(code, 0)
            match = re.search(r"\((task-[^)]+)\)", stdout.getvalue())
            assert match is not None
            task_id = match.group(1)

            task = TaskBoard(workspace).get(task_id)
            assert task is not None
            self.assertEqual(task.project_id, "motionx")
            self.assertEqual(task.agent_id, "owner")
            self.assertEqual(task.priority, "high")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "run", "first step", "--task", task_id])
            self.assertEqual(code, 0)
            first_match = re.search(r"session_id: (\S+)", stdout.getvalue())
            assert first_match is not None
            session_id = first_match.group(1)

            session = SessionRegistry(workspace).get(session_id)
            assert session is not None
            self.assertEqual(session.agent_id, "owner")
            self.assertEqual(session.project_dir, str(project_dir.resolve()))
            self.assertEqual(session.title, "Fix loader")

            task = TaskBoard(workspace).get(task_id)
            assert task is not None
            self.assertEqual(task.status, "doing")
            self.assertEqual(task.session_id, session_id)
            self.assertTrue(task.notes)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "run", "second step", "--task", task_id])
            self.assertEqual(code, 0)
            self.assertIn(f"session_id: {session_id}", stdout.getvalue())

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "tasks", "done", task_id, "verified"])
            self.assertEqual(code, 0)
            self.assertIn("status=done", stdout.getvalue())

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "tasks", "show", task_id])
            self.assertEqual(code, 0)
            shown_task = json.loads(stdout.getvalue())
            self.assertEqual(shown_task["status"], "done")
            self.assertEqual(shown_task["session_id"], session_id)


if __name__ == "__main__":
    unittest.main()
