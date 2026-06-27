import json
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path

from agentdeck.core.config import Workspace
from agentdeck.core.events import AgentEvent, EventKind
from agentdeck.interfaces.web import build_dashboard_snapshot, build_web_response, handle_web_action, render_dashboard_html
from agentdeck.storage.agents import AgentRegistry
from agentdeck.storage.approvals import ApprovalRegistry
from agentdeck.storage.directories import DirectoryRegistry
from agentdeck.storage.focus import FocusRegistry
from agentdeck.storage.jobs import JobRegistry
from agentdeck.storage.projects import ProjectRegistry
from agentdeck.storage.sessions import SessionRegistry
from agentdeck.storage.tasks import TaskBoard


class WebInterfaceTests(unittest.TestCase):
    def test_dashboard_snapshot_and_html_include_core_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = _sample_workspace(Path(tmpdir))

            snapshot = build_dashboard_snapshot(workspace)
            html = render_dashboard_html(snapshot)

            self.assertEqual(snapshot["counts"]["projects"], 1)
            self.assertEqual(snapshot["counts"]["directories"], 2)
            self.assertEqual(snapshot["counts"]["active_focus"], 1)
            self.assertEqual(snapshot["counts"]["workers"], 1)
            self.assertEqual(snapshot["counts"]["active_tasks"], 1)
            self.assertEqual(snapshot["counts"]["pending_approvals"], 1)
            self.assertEqual(snapshot["workers"][0]["identity"], "owner")
            self.assertEqual(snapshot["workers"][0]["focus_title"], "Web focus")
            self.assertIn("Project One", html)
            self.assertIn("Web focus", html)
            self.assertIn("Workers", html)
            self.assertIn("Directories", html)
            self.assertIn("Web task", html)
            self.assertIn("Run shell command?", html)
            self.assertIn("Archive Project", html)
            json.dumps(snapshot)

    def test_web_response_serves_dashboard_and_json_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = _sample_workspace(Path(tmpdir))
            health_response = build_web_response(workspace, "/api/health")
            overview_response = build_web_response(workspace, "/api/overview")
            html_response = build_web_response(workspace, "/")
            task_response = build_web_response(workspace, "/tasks/task-a")
            missing_response = build_web_response(workspace, "/missing")

            health = json.loads(health_response.body.decode("utf-8"))
            overview = json.loads(overview_response.body.decode("utf-8"))
            html = html_response.body.decode("utf-8")
            task_html = task_response.body.decode("utf-8")

            self.assertEqual(health_response.status, HTTPStatus.OK)
            self.assertEqual(overview_response.status, HTTPStatus.OK)
            self.assertEqual(html_response.status, HTTPStatus.OK)
            self.assertEqual(task_response.status, HTTPStatus.OK)
            self.assertEqual(missing_response.status, HTTPStatus.NOT_FOUND)
            self.assertTrue(health["ok"])
            self.assertEqual(overview["counts"]["projects"], 1)
            self.assertEqual(overview["counts"]["directories"], 2)
            self.assertEqual(overview["counts"]["workers"], 1)
            self.assertEqual(overview["focus"][0]["title"], "Web focus")
            self.assertEqual(overview["workers"][0]["session_agent_id"], "session-web")
            self.assertIn("AgentDeck", html)
            self.assertIn("Project One", html)
            self.assertIn("Web focus", html)
            self.assertIn("Dashboard", task_html)
            self.assertIn("Web task", task_html)

    def test_web_actions_create_rename_approval_cancel_and_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = _sample_workspace(Path(tmpdir))
            queue = _FakeQueue()

            create = handle_web_action(
                workspace,
                "/actions/projects/create",
                {"project_id": "extra", "title": "Extra", "cwd": str(Path(tmpdir) / "extra")},
                queue=queue,
            )
            rename = handle_web_action(
                workspace,
                "/actions/rename",
                {"entity": "project", "old_id": "extra", "new_id": "renamed"},
                queue=queue,
            )
            approval_id = ApprovalRegistry(workspace).list()[0].approval_id
            approve = handle_web_action(
                workspace,
                "/actions/approvals/resolve",
                {"approval_id": approval_id, "status": "approved", "note": "ok"},
                queue=queue,
            )
            cancel = handle_web_action(workspace, "/actions/jobs/cancel", {"job_id": "job-web"}, queue=queue)
            run = handle_web_action(workspace, "/actions/run", {"task_id": "", "assistant": "1", "prompt": "hello"}, queue=queue)
            auto = handle_web_action(workspace, "/actions/auto/start", {"task_id": "task-a", "mode": "task"}, queue=queue)
            archive = handle_web_action(workspace, "/actions/projects/archive", {"project_id": "proj"}, queue=queue)
            self.assertEqual(ProjectRegistry(workspace).get("proj").status, "archived")
            archived_html = render_dashboard_html(build_dashboard_snapshot(workspace))
            self.assertIn("Restore Project", archived_html)
            restore = handle_web_action(workspace, "/actions/projects/restore", {"project_id": "proj"}, queue=queue)

            self.assertEqual(create.status, HTTPStatus.SEE_OTHER)
            self.assertEqual(rename.status, HTTPStatus.SEE_OTHER)
            self.assertEqual(approve.status, HTTPStatus.SEE_OTHER)
            self.assertEqual(cancel.status, HTTPStatus.SEE_OTHER)
            self.assertEqual(run.status, HTTPStatus.SEE_OTHER)
            self.assertEqual(auto.status, HTTPStatus.SEE_OTHER)
            self.assertEqual(archive.status, HTTPStatus.SEE_OTHER)
            self.assertEqual(restore.status, HTTPStatus.SEE_OTHER)
            self.assertIsNotNone(ProjectRegistry(workspace).get("renamed"))
            self.assertEqual(ProjectRegistry(workspace).get("proj").status, "active")
            self.assertEqual(ApprovalRegistry(workspace).get(approval_id).status, "approved")
            self.assertEqual(queue.cancelled, ["job-web"])
            self.assertEqual(queue.started[-2]["agent_id"], "assistant")
            self.assertEqual(queue.auto[-1]["task_id"], "task-a")


def _sample_workspace(root: Path) -> Workspace:
    workspace = Workspace(root / ".agentdeck")
    workspace.ensure()
    project_dir = root / "project"
    child_dir = project_dir / "src"
    project_dir.mkdir()
    child_dir.mkdir()
    project = ProjectRegistry(workspace).upsert(
        project_id="proj",
        title="Project One",
        project_dir=project_dir,
        replace=True,
    )
    AgentRegistry(workspace).upsert(
        agent_id="owner",
        title="Owner",
        project_id=project.project_id,
        adapter="echo",
        project_dir=project_dir,
        replace=True,
    )
    DirectoryRegistry(workspace).upsert(path=child_dir, project_id=project.project_id, role="workspace")
    FocusRegistry(workspace).create(
        title="Web focus",
        description="Make web console show session-directory-focus routing.",
        project_id=project.project_id,
        agent_id="owner",
        directory=project_dir,
        session_id="session-web",
    )
    task = TaskBoard(workspace).create(title="Web task", project_id=project.project_id, status="doing")
    task.task_id = "task-a"
    records = TaskBoard(workspace)._read()
    records.pop(next(key for key in records if key != "task-a"), None)
    records["task-a"] = task
    TaskBoard(workspace)._write(records)
    SessionRegistry(workspace).upsert_start(
        session_id="session-web",
        agent_id="owner",
        adapter="echo",
        project_dir=project_dir,
        prompt="hello",
    )
    TaskBoard(workspace).attach_session(task.task_id, "session-web")
    JobRegistry(workspace).create(
        interface="telegram",
        chat_id=42,
        task_id=task.task_id,
        prompt="continue",
        job_id="job-web",
    )
    ApprovalRegistry(workspace).record_request(
        AgentEvent(EventKind.APPROVAL_REQUESTED, "owner", "session-web", text="Run shell command?"),
        adapter="codex",
        project_dir=project_dir,
        project_id=project.project_id,
        task_id=task.task_id,
    )
    return workspace


class _FakeQueue:
    def __init__(self) -> None:
        self.started: list[dict[str, str]] = []
        self.cancelled: list[str] = []
        self.auto: list[dict[str, object]] = []

    def start(self, **kwargs: str) -> str:
        self.started.append(dict(kwargs))
        return f"job-fake-{len(self.started)}"

    def cancel(self, job_id: str) -> bool:
        self.cancelled.append(job_id)
        return True

    def set_auto(self, **kwargs: object) -> None:
        self.auto.append(dict(kwargs))
