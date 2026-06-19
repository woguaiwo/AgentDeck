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

    def test_task_auto_session_is_replaced_when_agent_adapter_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            workspace.ensure()
            project_dir = tmp / "teleagent"
            project_dir.mkdir()
            fake_codex = tmp / "fake_codex"
            fake_codex.write_text(
                textwrap.dedent(
                    f"""\
                    #!{sys.executable}
                    import json
                    import sys
                    from pathlib import Path

                    last = None
                    args = sys.argv[1:]
                    for index, arg in enumerate(args):
                        if arg == "--output-last-message" and index + 1 < len(args):
                            last = Path(args[index + 1])

                    print(json.dumps({{"type": "thread.started", "thread_id": "thread-from-fake"}}), flush=True)
                    if last is not None:
                        last.write_text("codex final", encoding="utf-8")
                    """
                ),
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)

            self._main(
                [
                    "--workspace",
                    str(workspace.root),
                    "projects",
                    "create",
                    "teleagent",
                    "--cwd",
                    str(project_dir),
                    "--default-agent",
                    "owner",
                ]
            )
            self._main(
                [
                    "--workspace",
                    str(workspace.root),
                    "agents",
                    "create",
                    "owner",
                    "--project",
                    "teleagent",
                    "--adapter",
                    "echo",
                ]
            )
            task_out = self._main(
                [
                    "--workspace",
                    str(workspace.root),
                    "tasks",
                    "create",
                    "Switch adapter smoke",
                    "--project",
                    "teleagent",
                ]
            )
            match = re.search(r"\((task-[^)]+)\)", task_out)
            assert match is not None
            task_id = match.group(1)

            first_run = self._main(["--workspace", str(workspace.root), "run", "echo step", "--task", task_id])
            first_match = re.search(r"session_id: (\S+)", first_run)
            assert first_match is not None
            echo_session_id = first_match.group(1)
            echo_session = SessionRegistry(workspace).get(echo_session_id)
            assert echo_session is not None
            self.assertEqual(echo_session.adapter, "echo")

            self._main(
                [
                    "--workspace",
                    str(workspace.root),
                    "agents",
                    "create",
                    "owner",
                    "--project",
                    "teleagent",
                    "--adapter",
                    "codex",
                    "--codex-bin",
                    str(fake_codex),
                    "--replace",
                ]
            )
            second_run = self._main(["--workspace", str(workspace.root), "run", "codex step", "--task", task_id])
            self.assertIn("codex final", second_run)
            second_match = re.search(r"session_id: (\S+)", second_run)
            assert second_match is not None
            codex_session_id = second_match.group(1)

            self.assertNotEqual(codex_session_id, echo_session_id)
            task = TaskBoard(workspace).get(task_id)
            assert task is not None
            self.assertEqual(task.session_id, codex_session_id)
            codex_session = SessionRegistry(workspace).get(codex_session_id)
            assert codex_session is not None
            self.assertEqual(codex_session.adapter, "codex")
            self.assertEqual(codex_session.provider_session_id, "thread-from-fake")

    def _main(self, args: list[str]) -> str:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(args)
        self.assertEqual(code, 0, stdout.getvalue())
        return stdout.getvalue()

if __name__ == "__main__":
    unittest.main()
