import tempfile
import unittest
from pathlib import Path

from agentdeck.core.config import Workspace
from agentdeck.storage.memory import MarkdownMemoryStore
from agentdeck.storage.telegram_bots import TelegramBotRegistry, redacted_token


class CoreStorageTests(unittest.TestCase):
    def test_workspace_init_creates_expected_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()

            self.assertTrue(workspace.config_path.exists())
            self.assertTrue(workspace.agents_dir.is_dir())
            self.assertTrue(workspace.projects_dir.is_dir())
            self.assertTrue(workspace.approvals_dir.is_dir())
            self.assertTrue(workspace.journal_dir.is_dir())
            self.assertTrue(workspace.session_state_dir.is_dir())
            self.assertTrue(workspace.project_state_dir.is_dir())
            self.assertTrue(workspace.board_dir.is_dir())
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
            self.assertEqual(redacted_token(record.token), "123456...WXYZ")
            self.assertNotIn(record.token, redacted_token(record.token))


if __name__ == "__main__":
    unittest.main()
