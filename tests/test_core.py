import os
import tempfile
import unittest
from pathlib import Path

from agentdeck.core.config import Workspace, default_workspace_root, find_project_local_config, project_local_config_path
from agentdeck.core.run_service import build_agentdeck_context
from agentdeck.storage.directories import DirectoryRegistry
from agentdeck.storage.focus import FocusRegistry
from agentdeck.storage.memory import MarkdownMemoryStore
from agentdeck.storage.projects import ProjectRegistry
from agentdeck.storage.sessions import SessionRegistry
from agentdeck.storage.telegram_bots import TelegramBotRegistry, current_server_id, redacted_token


class CoreStorageTests(unittest.TestCase):
    def test_default_workspace_is_platform_workspace_not_caller_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            old_workspace = os.environ.pop("AGENTDECK_WORKSPACE", None)
            try:
                workspace = Workspace.from_cwd(tmpdir)
                self.assertEqual(workspace.root, default_workspace_root())
                self.assertNotEqual(workspace.root, Path(tmpdir) / ".agentdeck")
            finally:
                if old_workspace is not None:
                    os.environ["AGENTDECK_WORKSPACE"] = old_workspace

    def test_workspace_env_override_still_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            override = Path(tmpdir) / "custom-agentdeck"
            old_workspace = os.environ.get("AGENTDECK_WORKSPACE")
            os.environ["AGENTDECK_WORKSPACE"] = str(override)
            try:
                self.assertEqual(Workspace.from_cwd("/").root, override.resolve())
            finally:
                if old_workspace is None:
                    os.environ.pop("AGENTDECK_WORKSPACE", None)
                else:
                    os.environ["AGENTDECK_WORKSPACE"] = old_workspace

    def test_project_local_config_is_separate_from_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir) / "project"
            nested = project / "src"
            nested.mkdir(parents=True)
            config_path = project_local_config_path(project)
            config_path.write_text("[project]\nid = \"demo\"\n", encoding="utf-8")

            self.assertEqual(config_path.name, ".agentdeck.toml")
            self.assertEqual(find_project_local_config(nested), config_path)

    def test_workspace_init_creates_expected_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()

            self.assertTrue(workspace.config_path.exists())
            self.assertTrue(workspace.agents_dir.is_dir())
            self.assertTrue(workspace.projects_dir.is_dir())
            self.assertTrue(workspace.directories_dir.is_dir())
            self.assertTrue(workspace.approvals_dir.is_dir())
            self.assertTrue(workspace.journal_dir.is_dir())
            self.assertTrue(workspace.session_state_dir.is_dir())
            self.assertTrue(workspace.project_state_dir.is_dir())
            self.assertTrue(workspace.errors_dir.is_dir())
            self.assertTrue(workspace.board_dir.is_dir())
            self.assertTrue(workspace.focus_dir.is_dir())
            self.assertTrue((workspace.memory_dir / "user").is_dir())
            self.assertTrue((workspace.memory_dir / "projects").is_dir())
            self.assertTrue((workspace.memory_dir / "teams").is_dir())

    def test_memory_add_updates_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            store = MarkdownMemoryStore(workspace)
            entry = store.add("Project Rule", "Keep memory concise.")

            self.assertTrue(entry.path.exists())
            self.assertIn("Keep memory concise.", entry.path.read_text(encoding="utf-8"))
            index = workspace.memory_dir / "projects" / "MEMORY.md"
            self.assertIn("Project Rule", index.read_text(encoding="utf-8"))

    def test_project_memory_can_be_scoped_by_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            store = MarkdownMemoryStore(workspace)
            entry = store.add(
                "Project Snapshot",
                "Use compact shared memory.",
                scope="project",
                owner="motionx",
                source="agentdeck-context",
            )

            self.assertEqual(entry.path.parent, workspace.memory_dir / "projects" / "motionx")
            self.assertIn("source: agentdeck-context", entry.path.read_text(encoding="utf-8"))
            index = workspace.memory_dir / "projects" / "motionx" / "MEMORY.md"
            self.assertIn("Project Snapshot", index.read_text(encoding="utf-8"))

            documents = store.list_documents(scope="project", owner="motionx")
            self.assertEqual(len(documents), 1)
            self.assertEqual(documents[0].title, "Project Snapshot")
            self.assertEqual(documents[0].owner, "motionx")
            self.assertIn("Use compact shared memory.", documents[0].content)

    def test_disabled_memory_documents_are_not_listed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            store = MarkdownMemoryStore(workspace)
            entry = store.add("Disabled Rule", "Ignore this memory.", owner="proj")
            text = entry.path.read_text(encoding="utf-8")
            entry.path.write_text(text.replace("disabled: false", "disabled: true"), encoding="utf-8")

            documents = store.list_documents(scope="project", owner="proj")
            self.assertEqual(documents, [])

            enabled = store.set_disabled(str(entry.path), disabled=False)
            self.assertFalse(enabled.disabled)
            self.assertEqual(store.list_documents(scope="project", owner="proj")[0].title, "Disabled Rule")

            disabled = store.set_disabled(enabled.memory_id, disabled=True)
            self.assertTrue(disabled.disabled)
            self.assertEqual(store.list_documents(scope="project", owner="proj"), [])

    def test_pinned_memory_documents_are_listed_before_recent_unpinned_documents(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            store = MarkdownMemoryStore(workspace)
            store.add("Pinned Rule", "Always include this.", owner="proj", pinned=True)
            store.add("Recent Note", "This was written later.", owner="proj")

            documents = store.list_documents(scope="project", owner="proj")
            self.assertEqual([document.title for document in documents], ["Pinned Rule", "Recent Note"])
            self.assertTrue(documents[0].pinned)
            self.assertIn("pinned: true", documents[0].path.read_text(encoding="utf-8"))

    def test_telegram_bot_registry_imports_toml_and_redacts_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            source = tmp / "bots.toml"
            source.write_text(
                """
[bots.minsys-bot3]
title = "Minsys Bot 3"
token = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ"
allowed_chat_ids = [42, 43]
""".strip(),
                encoding="utf-8",
            )

            registry = TelegramBotRegistry(workspace)
            imported = registry.import_file(source)
            self.assertEqual(len(imported), 1)
            record = registry.get("minsys-bot3")
            assert record is not None
            self.assertEqual(record.title, "Minsys Bot 3")
            self.assertEqual(record.allowed_chat_ids, [42, 43])
            self.assertEqual(record.server_id, current_server_id())
            self.assertEqual(redacted_token(record.token), "123456...WXYZ")
            self.assertNotIn(record.token, redacted_token(record.token))

    def test_telegram_bot_registry_imports_manager_style_headings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            source = tmp / "Manager.txt"
            source.write_text(
                """
Server: Minsys
    Agents:
        minsys-bot3:
            tmux session: TeleAgent
            Working Folder: /data/lyxie/TeleAgent
            Token: 123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ
""".strip(),
                encoding="utf-8",
            )

            registry = TelegramBotRegistry(workspace)
            imported = registry.import_file(source)
            self.assertEqual(len(imported), 1)
            record = registry.get("minsys-bot3")
            assert record is not None
            self.assertEqual(record.title, "minsys-bot3")
            self.assertEqual(record.server_id, current_server_id())
            self.assertEqual(record.metadata["source_server"], "Minsys")
            self.assertIsNone(registry.get("bot-1"))

    def test_focus_registry_tracks_directory_session_and_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            project_dir = tmp / "project"
            project_dir.mkdir()
            project = ProjectRegistry(workspace).upsert(project_id="proj", project_dir=project_dir)
            ProjectRegistry(workspace).add_directory(project.project_id, project_dir / "sub")

            focus = FocusRegistry(workspace).create(
                title="Explore model handoff",
                description="Understand how sessions can migrate.",
                project_id=project.project_id,
                agent_id="owner",
                directory=project_dir,
                session_id="session-a",
            )
            FocusRegistry(workspace).add_note(focus.focus_id, "Keep the session-first design.")

            resolved = FocusRegistry(workspace).resolve("Explore model handoff")
            assert resolved is not None
            self.assertEqual(resolved.directory, str(project_dir.resolve()))
            self.assertEqual(resolved.session_id, "session-a")

            context = build_agentdeck_context(workspace, task=None, focus=resolved, session_id="session-a")
            self.assertIn("Focus:", context)
            self.assertIn("Explore model handoff", context)
            self.assertIn(str(project_dir.resolve()), context)
            self.assertIn("Keep the session-first design.", context)

    def test_directory_registry_tracks_project_paths_and_hierarchy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            project_dir = tmp / "project"
            child_dir = project_dir / "src"
            child_dir.mkdir(parents=True)

            project = ProjectRegistry(workspace).upsert(project_id="proj", title="Project", project_dir=project_dir)
            child = DirectoryRegistry(workspace).upsert(
                path=child_dir,
                project_id=project.project_id,
                parent=project_dir,
                role="module",
            )
            ProjectRegistry(workspace).add_directory(project.project_id, child_dir)

            records = DirectoryRegistry(workspace).list(project_id="proj")
            paths = {record.path for record in records}
            self.assertIn(str(project_dir.resolve()), paths)
            self.assertIn(str(child_dir.resolve()), paths)
            self.assertEqual(DirectoryRegistry(workspace).resolve(child.directory_id).path, str(child_dir.resolve()))

            refreshed = ProjectRegistry(workspace).get("proj")
            assert refreshed is not None
            self.assertIn(str(child_dir.resolve()), refreshed.metadata["directories"])

    def test_session_tracks_current_focus_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            project_dir = tmp / "project"
            project_dir.mkdir()
            SessionRegistry(workspace).upsert_start(
                session_id="session-a",
                agent_id="owner",
                adapter="echo",
                project_dir=project_dir,
                prompt="start",
            )
            focus = FocusRegistry(workspace).create(
                title="Study adapter migration",
                description="Compare direct provider session import with context-pack migration.",
                agent_id="owner",
                directory=project_dir,
            )

            record = SessionRegistry(workspace).set_current_focus(
                "session-a",
                focus.focus_id,
                focus_text=focus.description,
                actor="test",
            )
            assert record is not None
            self.assertEqual(SessionRegistry(workspace).current_focus_id("session-a"), focus.focus_id)
            history = SessionRegistry(workspace).focus_history("session-a")
            self.assertEqual(history[-1]["to"], focus.focus_id)
            self.assertIn("context-pack migration", history[-1]["text"])


if __name__ == "__main__":
    unittest.main()
