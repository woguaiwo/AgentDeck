import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from agentdeck.cli import main
from agentdeck.core.config import Workspace
from agentdeck.core.experience_organizer import ExperienceOrganizer
from agentdeck.storage.agents import AgentRegistry
from agentdeck.storage.experience import ExperienceStore
from agentdeck.storage.focus import FocusRegistry
from agentdeck.storage.progress import ProgressJournal
from agentdeck.storage.projects import ProjectRegistry
from agentdeck.storage.sessions import SessionRegistry


class ExperienceOrganizerTests(unittest.TestCase):
    def test_organizer_extracts_progress_entries_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            project_dir = tmp / "project"
            project_dir.mkdir()
            ProjectRegistry(workspace).upsert(project_id="proj", title="Project One", project_dir=project_dir)
            AgentRegistry(workspace).upsert(agent_id="owner", project_id="proj", adapter="echo", project_dir=project_dir)
            session = SessionRegistry(workspace).upsert_start(
                session_id="session-owner",
                agent_id="owner",
                adapter="echo",
                project_dir=project_dir,
                prompt="start",
                title="Owner session",
                project_id="proj",
            )
            focus = FocusRegistry(workspace).create(
                title="Clone memory design",
                project_id="proj",
                agent_id="owner",
                session_id=session.session_id,
                directory=project_dir,
            )
            ProgressJournal(workspace).append(
                kind="handoff",
                summary="Add Experience Collections above Event Graphs",
                project_id="proj",
                focus_id=focus.focus_id,
                session_id=session.session_id,
                agent_id="owner",
                completed=["Added collection/event/edge storage"],
                verified=["Unit tests passed"],
                next_steps=["Add organizer daemon"],
                decisions=["Use collection boundary for clone inheritance"],
                artifacts=["file:src/agentdeck/storage/experience.py"],
            )
            ProgressJournal(workspace).append(
                kind="manager-review",
                summary="Manual Telegram commands should stabilize schema before daemon extraction",
                project_id="proj",
                focus_id=focus.focus_id,
                session_id=session.session_id,
                agent_id="owner",
                completed=["Added /experience commands"],
                verified=["Telegram tests passed"],
                decisions=["Rules organizer writes via ExperienceStore"],
            )

            result = ExperienceOrganizer(workspace).process_once(limit=10)

            self.assertEqual(result.collections_created, 1)
            self.assertEqual(result.events_created, 2)
            self.assertEqual(result.edges_created, 1)
            store = ExperienceStore(workspace)
            collections = store.list_collections(focus_id=focus.focus_id)
            self.assertEqual(len(collections), 1)
            self.assertEqual(collections[0].title, "Clone memory design experience")
            events = store.list_events(collection=collections[0].collection_id, limit=10)
            self.assertEqual(len(events), 2)
            self.assertTrue(all(event.metadata.get("source_progress_id") for event in events))
            self.assertEqual(len(store.list_edges(relation="led_to")), 1)

            second = ExperienceOrganizer(workspace).process_once(limit=10)

            self.assertEqual(second.events_created, 0)
            self.assertEqual(second.skipped, 2)
            self.assertEqual(len(store.list_events(collection=collections[0].collection_id, limit=10)), 2)

    def test_experience_organize_cli_runs_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            ProgressJournal(workspace).append(
                kind="handoff",
                summary="Implement organizer daemon",
                project_id="proj",
                completed=["Added rules extractor"],
                verified=["Organizer tests passed"],
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "experience", "organize", "--limit", "5"])

            self.assertEqual(code, 0)
            self.assertIn("experience organizer: collections_created=1 events_created=1", stdout.getvalue())
            self.assertEqual(len(ExperienceStore(workspace).list_events(limit=10)), 1)


if __name__ == "__main__":
    unittest.main()
