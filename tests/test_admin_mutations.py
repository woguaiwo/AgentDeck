import json
import tempfile
import unittest
from pathlib import Path

from agentdeck.core.config import Workspace
from agentdeck.core.events import AgentEvent, EventKind
from agentdeck.storage.admin import delete_project, rename_global_id, restore_project
from agentdeck.storage.agents import AgentRegistry
from agentdeck.storage.approvals import ApprovalRegistry
from agentdeck.storage.jobs import JobRegistry
from agentdeck.storage.memory import MarkdownMemoryStore
from agentdeck.storage.progress import ProgressJournal
from agentdeck.storage.projects import ProjectRegistry
from agentdeck.storage.project_state import ProjectStateStore
from agentdeck.storage.session_state import SessionStateCard, SessionStateStore
from agentdeck.storage.sessions import SessionRegistry
from agentdeck.storage.tasks import TaskBoard


class AdminMutationTests(unittest.TestCase):
    def test_project_rename_updates_global_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, task_id = _sample_workspace(Path(tmpdir))

            result = rename_global_id(workspace, entity="project", old_id="proj", new_id="renamed")

            self.assertEqual(result.changed["projects"], 1)
            self.assertIsNone(ProjectRegistry(workspace).get("proj"))
            self.assertIsNotNone(ProjectRegistry(workspace).get("renamed"))
            self.assertEqual(TaskBoard(workspace).get(task_id).project_id, "renamed")
            self.assertEqual(AgentRegistry(workspace).get("owner").project_id, "renamed")
            self.assertEqual(ApprovalRegistry(workspace).list()[0].project_id, "renamed")
            self.assertEqual(SessionStateStore(workspace).get("session-a").project_id, "renamed")
            self.assertEqual(ProjectStateStore(workspace).get("renamed").project_id, "renamed")
            self.assertTrue((workspace.memory_dir / "projects" / "renamed").exists())
            self.assertIn('"project_id": "renamed"', (workspace.journal_dir / "progress.jsonl").read_text(encoding="utf-8"))

    def test_task_agent_and_session_rename_update_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, task_id = _sample_workspace(Path(tmpdir))

            rename_global_id(workspace, entity="task", old_id=task_id, new_id="task-renamed")
            rename_global_id(workspace, entity="agent", old_id="owner", new_id="lead")
            rename_global_id(workspace, entity="session", old_id="session-a", new_id="session-b")

            task = TaskBoard(workspace).get("task-renamed")
            assert task is not None
            self.assertEqual(task.agent_id, "lead")
            self.assertEqual(task.session_id, "session-b")
            self.assertEqual(SessionRegistry(workspace).get("session-b").agent_id, "lead")
            self.assertEqual(JobRegistry(workspace).get("job-a").task_id, "task-renamed")
            self.assertEqual(JobRegistry(workspace).get("job-a").session_id, "session-b")
            approval = ApprovalRegistry(workspace).list()[0]
            self.assertEqual(approval.task_id, "task-renamed")
            self.assertEqual(approval.agent_id, "lead")
            self.assertEqual(approval.session_id, "session-b")
            card = SessionStateStore(workspace).get("session-b")
            assert card is not None
            self.assertEqual(card.task_id, "task-renamed")
            self.assertEqual(card.agent_id, "lead")
            self.assertTrue((workspace.memory_dir / "tasks" / "task-renamed").exists())
            self.assertTrue((workspace.memory_dir / "agents" / "lead").exists())

    def test_delete_project_archives_record_and_preserves_project_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, task_id = _sample_workspace(Path(tmpdir))

            result = delete_project(workspace, "proj")

            self.assertEqual(result.changed["projects"], 1)
            project = ProjectRegistry(workspace).get("proj")
            assert project is not None
            self.assertEqual(project.status, "archived")
            self.assertIn("archived_at", project.metadata)
            self.assertEqual(TaskBoard(workspace).get(task_id).project_id, "proj")
            self.assertEqual(AgentRegistry(workspace).get("owner").project_id, "proj")
            self.assertEqual(ApprovalRegistry(workspace).list()[0].project_id, "proj")
            self.assertEqual(SessionStateStore(workspace).get("session-a").project_id, "proj")
            self.assertIsNotNone(SessionRegistry(workspace).get("session-a"))

            restore = restore_project(workspace, "proj")

            self.assertEqual(restore.changed["projects"], 1)
            restored = ProjectRegistry(workspace).get("proj")
            assert restored is not None
            self.assertEqual(restored.status, "active")
            self.assertIn("restored_at", restored.metadata)


def _sample_workspace(root: Path) -> tuple[Workspace, str]:
    workspace = Workspace(root / ".agentdeck")
    workspace.ensure()
    project_dir = root / "project"
    project_dir.mkdir()
    ProjectRegistry(workspace).upsert(project_id="proj", title="Project", project_dir=project_dir, replace=True)
    AgentRegistry(workspace).upsert(
        agent_id="owner",
        title="Owner",
        project_id="proj",
        adapter="echo",
        project_dir=project_dir,
        replace=True,
    )
    task = TaskBoard(workspace).create(title="Task", project_id="proj", agent_id="owner", status="doing")
    SessionRegistry(workspace).upsert_start(
        session_id="session-a",
        agent_id="owner",
        adapter="echo",
        project_dir=project_dir,
        prompt="hello",
    )
    TaskBoard(workspace).attach_session(task.task_id, "session-a")
    JobRegistry(workspace).create(
        interface="web",
        chat_id=0,
        task_id=task.task_id,
        prompt="continue",
        job_id="job-a",
    )
    JobRegistry(workspace).finish("job-a", status="done", session_id="session-a", final_text="done")
    ApprovalRegistry(workspace).record_request(
        AgentEvent(EventKind.APPROVAL_REQUESTED, "owner", "session-a", text="Run command?"),
        adapter="codex",
        project_dir=project_dir,
        project_id="proj",
        task_id=task.task_id,
    )
    ProgressJournal(workspace).append(
        kind="handoff",
        summary="Progress",
        project_id="proj",
        task_id=task.task_id,
        session_id="session-a",
        agent_id="owner",
    )
    SessionStateStore(workspace).write(
        SessionStateCard(
            session_id="session-a",
            project_id="proj",
            task_id=task.task_id,
            agent_id="owner",
        )
    )
    ProjectStateStore(workspace).update("proj", goal="Goal")
    ProjectStateStore(workspace).add_decision("proj", "Decision")
    MarkdownMemoryStore(workspace).add("Project Memory", "project body", scope="project", owner="proj")
    MarkdownMemoryStore(workspace).add("Task Memory", "task body", scope="task", owner=task.task_id)
    MarkdownMemoryStore(workspace).add("Agent Memory", "agent body", scope="agent", owner="owner")
    (workspace.root / "telegram").mkdir(parents=True, exist_ok=True)
    (workspace.root / "telegram" / "state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "chats": {
                    "42": {
                        "current_project_id": "proj",
                        "current_task_id": task.task_id,
                        "current_agent_id": "owner",
                        "recent_project_ids": ["proj"],
                        "recent_task_ids": [task.task_id],
                        "recent_agent_ids": ["owner"],
                        "recent_session_ids": ["session-a"],
                        "auto": {"enabled": True, "task_id": task.task_id},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return workspace, task.task_id
