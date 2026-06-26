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
from agentdeck.storage.agents import AgentRegistry
from agentdeck.storage.focus import FocusRegistry
from agentdeck.storage.memory import MarkdownMemoryStore
from agentdeck.storage.progress import ProgressJournal
from agentdeck.storage.projects import ProjectRegistry
from agentdeck.storage.project_state import ProjectStateStore
from agentdeck.storage.session_state import SessionStateCard, SessionStateStore
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

    def test_project_state_and_decision_cli_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            workspace.ensure()
            project_dir = tmp / "agentdeck"
            project_dir.mkdir()

            self._main(
                [
                    "--workspace",
                    str(workspace.root),
                    "projects",
                    "create",
                    "agentdeck",
                    "--title",
                    "AgentDeck",
                    "--cwd",
                    str(project_dir),
                    "--default-agent",
                    "developer",
                ]
            )
            state_out = self._main(
                [
                    "--workspace",
                    str(workspace.root),
                    "projects",
                    "update-state",
                    "AgentDeck",
                    "--goal",
                    "Coordinate manager and executor agents",
                    "--phase",
                    "memory",
                    "--focus",
                    "Implement project state",
                    "--next",
                    "Inject project state into prompts",
                    "--constraint",
                    "Do not share raw transcripts as memory",
                    "--artifact",
                    "src/agentdeck/storage/project_state.py",
                    "--by",
                    "manager",
                ]
            )
            self.assertIn("project_state: AgentDeck", state_out)

            decision_out = self._main(
                [
                    "--workspace",
                    str(workspace.root),
                    "projects",
                    "decide",
                    "agentdeck",
                    "Use project state as manager-controlled direction",
                    "--reason",
                    "Executors need stable project-level guidance",
                    "--impact",
                    "Task runs receive project decisions automatically",
                    "--by",
                    "manager",
                ]
            )
            self.assertIn("decision: decision-", decision_out)

            state = ProjectStateStore(workspace).get("agentdeck")
            assert state is not None
            self.assertEqual(state.goal, "Coordinate manager and executor agents")
            self.assertEqual(state.next_steps, ["Inject project state into prompts"])
            self.assertEqual(state.constraints, ["Do not share raw transcripts as memory"])

            decisions = ProjectStateStore(workspace).decisions("agentdeck")
            self.assertEqual(len(decisions), 1)
            self.assertEqual(decisions[0].decision, "Use project state as manager-controlled direction")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "projects", "state", "agentdeck"])
            self.assertEqual(code, 0)
            shown = json.loads(stdout.getvalue())
            self.assertEqual(shown["phase"], "memory")

            decisions_out = self._main(["--workspace", str(workspace.root), "projects", "decisions", "agentdeck"])
            self.assertIn("Use project state as manager-controlled direction", decisions_out)

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

            module_dir = project_dir / "module"
            module_dir.mkdir()
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "directories",
                        "add",
                        str(module_dir),
                        "--project",
                        "motionx",
                        "--parent",
                        str(project_dir),
                        "--role",
                        "module",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertIn("directory:", stdout.getvalue())

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "directories", "list", "--project", "motionx"])
            self.assertEqual(code, 0)
            listed_dirs = stdout.getvalue()
            self.assertIn(str(project_dir.resolve()), listed_dirs)
            self.assertIn(str(module_dir.resolve()), listed_dirs)
            directory_id = re.search(r"(dir-\S+)", listed_dirs).group(1)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "directories", "show", directory_id])
            self.assertEqual(code, 0)
            shown_directory = json.loads(stdout.getvalue())
            self.assertEqual(shown_directory["project_id"], "motionx")

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

    def test_task_handoff_updates_journal_task_note_and_session_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            workspace.ensure()
            project_dir = tmp / "agentdeck"
            project_dir.mkdir()

            self._main(
                [
                    "--workspace",
                    str(workspace.root),
                    "projects",
                    "create",
                    "agentdeck",
                    "--cwd",
                    str(project_dir),
                    "--default-agent",
                    "developer",
                ]
            )
            task_out = self._main(
                [
                    "--workspace",
                    str(workspace.root),
                    "tasks",
                    "create",
                    "Build memory layer",
                    "--description",
                    "Implement state cards and handoffs",
                    "--project",
                    "agentdeck",
                    "--agent",
                    "developer",
                ]
            )
            match = re.search(r"\((task-[^)]+)\)", task_out)
            assert match is not None
            task_id = match.group(1)
            TaskBoard(workspace).attach_session(task_id, "session-memory")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "tasks",
                        "handoff",
                        task_id,
                        "--summary",
                        "State card storage is in place",
                        "--completed",
                        "Added session-state JSON files",
                        "--verified",
                        "Ran focused tests",
                        "--next",
                        "Inject state cards into auto prompts",
                        "--decision",
                        "Keep handoffs as task notes plus journal entries",
                        "--artifact",
                        "src/agentdeck/storage/session_state.py",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertIn("handoff: progress-", stdout.getvalue())
            self.assertIn("session_state: session-memory", stdout.getvalue())

            task = TaskBoard(workspace).get(task_id)
            assert task is not None
            self.assertEqual(task.notes[-1]["kind"], "handoff")
            self.assertIn("State card storage is in place", task.notes[-1]["text"])
            self.assertIn("\nNext:\n", task.notes[-1]["text"])
            self.assertIn("- Inject state cards into auto prompts", task.notes[-1]["text"])

            entries = ProgressJournal(workspace).list(task_id=task_id)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].summary, "State card storage is in place")
            self.assertEqual(entries[0].next_steps, ["Inject state cards into auto prompts"])

            card = SessionStateStore(workspace).get("session-memory")
            assert card is not None
            self.assertEqual(card.objective, "Implement state cards and handoffs")
            self.assertEqual(card.current_state, "Added session-state JSON files")
            self.assertEqual(card.next_step, "Inject state cards into auto prompts")
            self.assertIn("Ran focused tests", card.verified_work)
            self.assertIn("src/agentdeck/storage/session_state.py", card.active_artifacts)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "sessions", "state", "session-memory"])
            self.assertEqual(code, 0)
            shown = json.loads(stdout.getvalue())
            self.assertEqual(shown["session_id"], "session-memory")
            self.assertEqual(shown["task_id"], task_id)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "tasks", "context", task_id])
            self.assertEqual(code, 0)
            context = stdout.getvalue()
            self.assertIn("AgentDeck context:", context)
            self.assertIn("State card storage is in place", context)
            self.assertIn("Recent handoffs:", context)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "tasks", "handoffs", task_id])
            self.assertEqual(code, 0)
            handoffs = stdout.getvalue()
            self.assertIn("handoffs for: Build memory layer", handoffs)
            self.assertIn("State card storage is in place", handoffs)

    def test_manager_review_updates_journal_task_note_session_state_and_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            workspace.ensure()
            project_dir = tmp / "agentdeck"
            project_dir.mkdir()

            self._main(
                [
                    "--workspace",
                    str(workspace.root),
                    "projects",
                    "create",
                    "agentdeck",
                    "--cwd",
                    str(project_dir),
                    "--default-agent",
                    "developer",
                ]
            )
            task_out = self._main(
                [
                    "--workspace",
                    str(workspace.root),
                    "tasks",
                    "create",
                    "Build review loop",
                    "--description",
                    "Let a manager steer executor work",
                    "--project",
                    "agentdeck",
                    "--agent",
                    "developer",
                ]
            )
            match = re.search(r"\((task-[^)]+)\)", task_out)
            assert match is not None
            task_id = match.group(1)
            TaskBoard(workspace).attach_session(task_id, "session-review")

            self._main(
                [
                    "--workspace",
                    str(workspace.root),
                    "tasks",
                    "handoff",
                    task_id,
                    "--summary",
                    "Executor added storage",
                    "--next",
                    "Ask manager to review",
                ]
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "tasks",
                        "manager-review",
                        task_id,
                        "--summary",
                        "Storage direction is acceptable",
                        "--status",
                        "approved",
                        "--next",
                        "Expose reviews on Telegram",
                        "--decision",
                        "Keep manager review as a progress journal kind",
                        "--reviewer",
                        "manager",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertIn("manager_review: progress-", stdout.getvalue())
            self.assertIn("session_state: session-review", stdout.getvalue())

            task = TaskBoard(workspace).get(task_id)
            assert task is not None
            self.assertEqual(task.notes[-1]["kind"], "manager-review")
            self.assertIn("Manager review: Storage direction is acceptable", task.notes[-1]["text"])
            self.assertIn("Status: approved", task.notes[-1]["text"])
            self.assertIn("- Expose reviews on Telegram", task.notes[-1]["text"])

            handoffs = ProgressJournal(workspace).list(kind="handoff", task_id=task_id)
            reviews = ProgressJournal(workspace).list(kind="manager-review", task_id=task_id)
            self.assertEqual(len(handoffs), 1)
            self.assertEqual(len(reviews), 1)
            self.assertEqual(reviews[0].summary, "Storage direction is acceptable")
            self.assertEqual(reviews[0].metadata["status"], "approved")

            card = SessionStateStore(workspace).get("session-review")
            assert card is not None
            self.assertEqual(card.current_state, "Storage direction is acceptable")
            self.assertEqual(card.next_step, "Expose reviews on Telegram")
            self.assertIn("Keep manager review as a progress journal kind", card.decisions)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "tasks", "reviews", task_id])
            self.assertEqual(code, 0)
            shown_reviews = stdout.getvalue()
            self.assertIn("manager reviews for: Build review loop", shown_reviews)
            self.assertIn("approved: Storage direction is acceptable", shown_reviews)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "tasks", "handoffs", task_id])
            self.assertEqual(code, 0)
            shown_handoffs = stdout.getvalue()
            self.assertIn("Executor added storage", shown_handoffs)
            self.assertNotIn("Storage direction is acceptable", shown_handoffs)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "tasks", "context", task_id])
            self.assertEqual(code, 0)
            context = stdout.getvalue()
            self.assertIn("Recent handoffs:", context)
            self.assertIn("Executor added storage", context)
            self.assertIn("Recent manager reviews:", context)
            self.assertIn("approved: Storage direction is acceptable", context)
            MarkdownMemoryStore(workspace).add(
                "Existing durable memory",
                "This older memory should not be copied into compact-task snapshots.",
                scope="project",
                owner="agentdeck",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "memory",
                        "compact-task",
                        task_id,
                        "--title",
                        "Review loop snapshot",
                    ]
                )
            self.assertEqual(code, 0)
            compact_out = stdout.getvalue()
            self.assertIn("memory: mem-", compact_out)
            self.assertIn("scope: project", compact_out)
            self.assertIn("owner: agentdeck", compact_out)
            path_match = re.search(r"path: (.+)", compact_out)
            assert path_match is not None
            memory_path = Path(path_match.group(1))
            memory_text = memory_path.read_text(encoding="utf-8")
            self.assertIn("This memory was generated from structured AgentDeck state", memory_text)
            self.assertIn("Recent manager reviews:", memory_text)
            self.assertIn("Storage direction is acceptable", memory_text)
            self.assertNotIn("This older memory should not be copied", memory_text)

            focus = FocusRegistry(workspace).create(
                title="Focus memory",
                description="Keep focus snapshots session-first.",
                project_id="agentdeck",
                agent_id="developer",
                directory=project_dir,
                session_id="session-a",
            )
            FocusRegistry(workspace).add_note(focus.focus_id, "Capture the current focus without copying older memory.")
            SessionRegistry(workspace).upsert_start(
                session_id="session-a",
                agent_id="developer",
                adapter="echo",
                project_dir=project_dir,
                prompt="Work on focus memory compaction.",
                project_id="agentdeck",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "memory",
                        "compact-focus",
                        focus.focus_id,
                        "--title",
                        "Focus snapshot",
                        "--pin",
                    ]
                )
            self.assertEqual(code, 0)
            focus_compact_out = stdout.getvalue()
            self.assertIn("memory: mem-", focus_compact_out)
            self.assertIn("scope: project", focus_compact_out)
            self.assertIn("owner: agentdeck", focus_compact_out)
            focus_path_match = re.search(r"path: (.+)", focus_compact_out)
            assert focus_path_match is not None
            focus_memory_path = Path(focus_path_match.group(1))
            focus_memory_text = focus_memory_path.read_text(encoding="utf-8")
            self.assertIn("This memory was generated from structured AgentDeck state", focus_memory_text)
            self.assertIn("Focus:", focus_memory_text)
            self.assertIn("Focus memory", focus_memory_text)
            self.assertIn("Keep focus snapshots session-first.", focus_memory_text)
            self.assertIn("Capture the current focus", focus_memory_text)
            self.assertNotIn("This older memory should not be copied", focus_memory_text)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "memory", "disable", str(memory_path)])
            self.assertEqual(code, 0)
            self.assertIn("memory disabled: Review loop snapshot", stdout.getvalue())

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "tasks", "context", task_id])
            self.assertEqual(code, 0)
            disabled_context = stdout.getvalue()
            self.assertNotIn("Review loop snapshot", disabled_context)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "memory", "enable", str(memory_path)])
            self.assertEqual(code, 0)
            self.assertIn("memory enabled: Review loop snapshot", stdout.getvalue())

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "tasks", "context", task_id])
            self.assertEqual(code, 0)
            enabled_context = stdout.getvalue()
            self.assertIn("Review loop snapshot", enabled_context)

    def test_run_injects_session_state_and_handoffs_but_keeps_user_prompt_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            workspace.ensure()
            project_dir = tmp / "agentdeck"
            project_dir.mkdir()

            self._main(
                [
                    "--workspace",
                    str(workspace.root),
                    "projects",
                    "create",
                    "agentdeck",
                    "--cwd",
                    str(project_dir),
                    "--default-agent",
                    "developer",
                ]
            )
            self._main(
                [
                    "--workspace",
                    str(workspace.root),
                    "agents",
                    "create",
                    "developer",
                    "--project",
                    "agentdeck",
                    "--adapter",
                    "echo",
                    "--role",
                    "developer",
                ]
            )
            task_out = self._main(
                [
                    "--workspace",
                    str(workspace.root),
                    "tasks",
                    "create",
                    "Inject context",
                    "--description",
                    "Make auto mode remember the current plan",
                    "--project",
                    "agentdeck",
                ]
            )
            match = re.search(r"\((task-[^)]+)\)", task_out)
            assert match is not None
            task_id = match.group(1)

            first_run = self._main(["--workspace", str(workspace.root), "run", "initial", "--task", task_id])
            first_match = re.search(r"session_id: (\S+)", first_run)
            assert first_match is not None
            session_id = first_match.group(1)

            SessionStateStore(workspace).write(
                SessionStateCard(
                    session_id=session_id,
                    task_id=task_id,
                    project_id="agentdeck",
                    agent_id="developer",
                    objective="Keep the manager and executor aligned",
                    current_state="State cards exist",
                    next_step="Inject context into run prompts",
                    verified_work=["Handoff CLI test passed"],
                    decisions=["Use bounded text injection before SQLite"],
                    active_artifacts=["src/agentdeck/core/run_service.py"],
                )
            )
            AgentRegistry(workspace).set_role_template(
                "developer",
                "Focus on the next scoped implementation step.\nWrite a handoff after verification.",
            )
            ProgressJournal(workspace).append(
                kind="handoff",
                summary="Executor finished state card storage",
                project_id="agentdeck",
                task_id=task_id,
                session_id=session_id,
                agent_id="developer",
                next_steps=["Wire state cards into auto"],
                decisions=["Use recent handoffs as prompt context"],
            )
            ProjectStateStore(workspace).update(
                "agentdeck",
                goal="Coordinate manager and executor agents",
                current_focus="Keep work aligned with project state",
                next_steps=["Have executor follow the project plan"],
                constraints=["Do not share raw transcripts as memory"],
            )
            ProjectStateStore(workspace).add_decision(
                "agentdeck",
                "Use project state as manager-controlled direction",
                reason="Executors need stable project-level guidance",
            )
            MarkdownMemoryStore(workspace).add(
                "Pinned manager operating rule",
                "Always keep executor patches narrow.",
                scope="project",
                owner="agentdeck",
                pinned=True,
            )
            MarkdownMemoryStore(workspace).add(
                "Recent manager operating note",
                "Keep executor patches narrow and verify with unittest.",
                scope="project",
                owner="agentdeck",
            )

            second_run = self._main(["--workspace", str(workspace.root), "run", "continue", "--task", task_id])

            self.assertIn("Echo: continue", second_run)
            self.assertIn("AgentDeck context:", second_run)
            self.assertIn("Project state:", second_run)
            self.assertIn("goal: Coordinate manager and executor agents", second_run)
            self.assertIn("Project decisions:", second_run)
            self.assertIn("Use project state as manager-controlled direction", second_run)
            self.assertIn("current state: State cards exist", second_run)
            self.assertIn("next step: Inject context into run prompts", second_run)
            self.assertIn("Agent role guidance:", second_run)
            self.assertIn("role: developer", second_run)
            self.assertIn("Focus on the next scoped implementation step.", second_run)
            self.assertIn("Relevant durable memories:", second_run)
            self.assertIn("Pinned manager operating rule [project:agentdeck] pinned", second_run)
            self.assertIn("Recent manager operating note [project:agentdeck]", second_run)
            self.assertLess(
                second_run.index("Pinned manager operating rule"),
                second_run.index("Recent manager operating note"),
            )
            self.assertIn("Keep executor patches narrow and verify with unittest.", second_run)
            self.assertIn("Executor finished state card storage", second_run)

            session = SessionRegistry(workspace).get(session_id)
            assert session is not None
            self.assertEqual(session.last_user_message, "continue")
            self.assertNotIn("AgentDeck context", session.last_user_message)
            self.assertNotIn("Relevant durable memories", session.last_user_message)

    def _main(self, args: list[str]) -> str:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(args)
        self.assertEqual(code, 0, stdout.getvalue())
        return stdout.getvalue()

if __name__ == "__main__":
    unittest.main()
