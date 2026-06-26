import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from agentdeck.cli import main
from agentdeck.core.config import Workspace
from agentdeck.storage.agents import AgentRegistry, role_template_for_agent
from agentdeck.storage.directories import DirectoryRegistry
from agentdeck.storage.sessions import SessionRegistry
from agentdeck.storage.telegram_bots import TelegramBotRegistry, assistant_agent_id_for_bot


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
            self.assertIn("Coordinate the current task", role_template_for_agent(record))
            directory = DirectoryRegistry(workspace).resolve(tmpdir)
            assert directory is not None
            self.assertEqual(record.metadata["directory_id"], directory.directory_id)
            self.assertEqual(directory.metadata["agent_id"], "motion-x-owner")

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
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "agents",
                        "template",
                        "motionx",
                        "--prompt",
                        "Act as the project manager.",
                        "--prompt",
                        "Review executor handoffs before changing direction.",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertIn("agent template set: Motion-X Owner", stdout.getvalue())
            templated = AgentRegistry(workspace).get("motionx")
            assert templated is not None
            self.assertIn("Review executor handoffs", role_template_for_agent(templated))

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
                code = main(["--workspace", str(workspace.root), "agents", "template", "motionx", "--clear"])
            self.assertEqual(code, 0)
            cleared = AgentRegistry(workspace).get("motionx")
            assert cleared is not None
            self.assertNotIn("role_template", cleared.metadata)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "run", "second", "--agent", "motionx"])
            self.assertEqual(code, 0)
            self.assertIn(f"session_id: {first_session}", stdout.getvalue())

    def test_cli_assistant_setup_creates_default_router_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "assistant",
                        "setup",
                        "--adapter",
                        "echo",
                        "--cwd",
                        tmpdir,
                    ]
                )
            self.assertEqual(code, 0)
            self.assertIn("assistant: AgentDeck Assistant (assistant)", stdout.getvalue())

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "run", "help route me", "--agent", "assistant"])
            self.assertEqual(code, 0)
            run_output = stdout.getvalue()
            self.assertIn("Agent role guidance:", run_output)
            self.assertIn("You are the user's AgentDeck assistant", run_output)
            self.assertIn("help route me", run_output)

    def test_cli_assistant_setup_bots_creates_bot_specific_assistants(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            TelegramBotRegistry(workspace).upsert(
                bot_id="minsys-bot3",
                title="Minsys Bot 3",
                token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                allowed_chat_ids=[42],
            )
            stale_assistant_id = assistant_agent_id_for_bot("minsys-bot3")
            AgentRegistry(workspace).upsert(
                agent_id=stale_assistant_id,
                title="Minsys Bot 3 Assistant",
                adapter="echo",
                project_dir=tmpdir,
            )
            AgentRegistry(workspace).set_role_template(stale_assistant_id, "Old assistant prompt without session routing.")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "assistant",
                        "setup-bots",
                        "--adapter",
                        "echo",
                        "--cwd",
                        tmpdir,
                    ]
                )
            self.assertEqual(code, 0)
            assistant_id = assistant_agent_id_for_bot("minsys-bot3")
            self.assertIn(f"minsys-bot3) -> {assistant_id}", stdout.getvalue())

            bot = TelegramBotRegistry(workspace).get("minsys-bot3")
            assert bot is not None
            self.assertEqual(bot.assistant_agent_id, assistant_id)
            assistant = AgentRegistry(workspace).resolve(assistant_id)
            assert assistant is not None
            self.assertIn("You are the user's AgentDeck assistant", role_template_for_agent(assistant))
            self.assertIn("/use session", role_template_for_agent(assistant))
            self.assertIn("do not merely describe or claim", role_template_for_agent(assistant))

    def test_cli_assistant_refresh_updates_saved_assistant_templates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            assistant_id = assistant_agent_id_for_bot("minsys-bot3")
            TelegramBotRegistry(workspace).upsert(
                bot_id="minsys-bot3",
                title="Minsys Bot 3",
                token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                allowed_chat_ids=[42],
                assistant_agent_id=assistant_id,
            )
            registry = AgentRegistry(workspace)
            registry.upsert(agent_id=assistant_id, title="Minsys Bot 3 Assistant", adapter="echo", project_dir=tmpdir)
            registry.set_role_template(assistant_id, "Old assistant prompt.")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "assistant", "refresh"])

            self.assertEqual(code, 0)
            self.assertIn(f"refreshed Minsys Bot 3 Assistant ({assistant_id})", stdout.getvalue())
            refreshed = AgentRegistry(workspace).resolve(assistant_id)
            assert refreshed is not None
            self.assertIn("/use session", role_template_for_agent(refreshed))


if __name__ == "__main__":
    unittest.main()
