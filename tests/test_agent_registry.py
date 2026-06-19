import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from agentdeck.cli import main
from agentdeck.core.config import Workspace
from agentdeck.storage.agents import AgentRegistry
from agentdeck.storage.sessions import SessionRegistry


class AgentRegistryTests(unittest.TestCase):
    def test_agent_registry_normalizes_ids_and_resolves_titles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()
            registry = AgentRegistry(workspace)

            record = registry.upsert(
                agent_id="Motion X Owner",
                title="Motion-X Owner",
                role="Owner",
                team_id="Motion X",
                adapter="echo",
                project_dir=tmpdir,
            )

            self.assertEqual(record.agent_id, "motion-x-owner")
            self.assertEqual(record.project_id, "")
            self.assertEqual(record.role, "owner")
            self.assertEqual(record.team_id, "motion-x")
            self.assertEqual(registry.resolve("Motion-X Owner").agent_id, "motion-x-owner")
            self.assertEqual(registry.list(team_id="motion-x")[0].title, "Motion-X Owner")

    def test_cli_agents_create_list_show_and_run_resume_latest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            workspace.ensure()
            project = tmp / "project"
            project.mkdir()

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "agents",
                        "create",
                        "motionx",
                        "--title",
                        "Motion-X Owner",
                        "--role",
                        "owner",
                        "--team",
                        "motionx",
                        "--adapter",
                        "echo",
                        "--cwd",
                        str(project),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertIn("Motion-X Owner", stdout.getvalue())

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "agents", "list"])
            self.assertEqual(code, 0)
            listed = stdout.getvalue()
            self.assertIn("title\tagent_id\tproject\trole\tteam", listed)
            self.assertIn("Motion-X Owner\tmotionx\t-\towner\tmotionx", listed)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "agents", "show", "Motion-X Owner"])
            self.assertEqual(code, 0)
            shown = json.loads(stdout.getvalue())
            self.assertEqual(shown["agent_id"], "motionx")
            self.assertEqual(shown["project_dir"], str(project.resolve()))

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "run", "first", "--agent", "motionx"])
            self.assertEqual(code, 0)
            first_match = re.search(r"session_id: (\S+)", stdout.getvalue())
            assert first_match is not None
            first_session = first_match.group(1)

            record = SessionRegistry(workspace).get(first_session)
            assert record is not None
            self.assertEqual(record.agent_id, "motionx")
            self.assertEqual(record.project_dir, str(project.resolve()))
            self.assertEqual(record.title, "Motion-X Owner")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "run", "second", "--agent", "motionx"])
            self.assertEqual(code, 0)
            self.assertIn(f"session_id: {first_session}", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
