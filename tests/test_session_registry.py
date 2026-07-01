import contextlib
import io
import json
import re
import sys
import tempfile
import threading
import textwrap
import unittest
from pathlib import Path

from agentdeck.cli import main
from agentdeck.core.config import Workspace
from agentdeck.core.events import AgentEvent, EventKind
from agentdeck.storage.directories import DirectoryRegistry
from agentdeck.storage.sessions import SessionRegistry


class SessionRegistryTests(unittest.TestCase):
    def test_concurrent_session_starts_do_not_collide_on_registry_tmp_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()
            project = Path(tmpdir) / "project"
            project.mkdir()
            start = threading.Barrier(12)
            errors: list[BaseException] = []

            def worker(index: int) -> None:
                try:
                    start.wait(timeout=2)
                    SessionRegistry(workspace).upsert_start(
                        session_id=f"session-{index}",
                        agent_id=f"agent-{index}",
                        adapter="echo",
                        project_dir=project,
                        prompt=f"prompt {index}",
                    )
                except BaseException as exc:  # pragma: no cover - surfaced by assertion below
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(index,)) for index in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            self.assertEqual(errors, [])
            records = SessionRegistry(workspace).list()
            self.assertEqual({record.session_id for record in records}, {f"session-{index}" for index in range(12)})

    def test_registry_captures_codex_thread_id_from_status_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()
            registry = SessionRegistry(workspace)

            registry.upsert_start(
                session_id="session-a",
                agent_id="agent-a",
                adapter="codex",
                project_dir=tmpdir,
                prompt="hello",
            )
            registry.update_from_event(
                AgentEvent(
                    EventKind.STATUS,
                    "agent-a",
                    "session-a",
                    text="thread_started",
                    payload={"type": "thread.started", "thread_id": "thread-123"},
                )
            )
            registry.update_from_event(
                AgentEvent(EventKind.ASSISTANT_FINAL, "agent-a", "session-a", text="done")
            )

            record = registry.get("session-a")

            assert record is not None
            self.assertEqual(record.provider_session_id, "thread-123")
            self.assertEqual(record.provider_session_kind, "codex_thread")
            self.assertEqual(record.title, "hello")
            self.assertEqual(record.last_assistant_final, "done")
            self.assertEqual(record.status, "idle")
            directory = DirectoryRegistry(workspace).resolve(tmpdir)
            assert directory is not None
            self.assertEqual(record.metadata["directory_id"], directory.directory_id)
            self.assertEqual(directory.metadata["last_session_id"], "session-a")

    def test_cli_sessions_list_rename_and_resolve_title(self) -> None:
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
                        "run",
                        "hello",
                        "--adapter",
                        "echo",
                        "--cwd",
                        str(project),
                        "--title",
                        "Build planner",
                    ]
                )
            self.assertEqual(code, 0)
            match = re.search(r"session_id: (\S+)", stdout.getvalue())
            assert match is not None
            session_id = match.group(1)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "sessions", "list"])
            self.assertEqual(code, 0)
            listed = stdout.getvalue()
            self.assertIn("title\tsession_agent_id\tidentity", listed)
            self.assertIn("directory_id", listed)
            self.assertIn("Build planner", listed)
            self.assertNotIn("provider_session_id", listed)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "workers", "list"])
            self.assertEqual(code, 0)
            workers = stdout.getvalue()
            self.assertIn("title\tsession_agent_id\tidentity", workers)
            self.assertIn("Build planner", workers)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "sessions",
                        "rename",
                        session_id,
                        "Planner review",
                    ]
                )
            self.assertEqual(code, 0)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "workers", "show", "Planner review"])
            self.assertEqual(code, 0)
            shown = json.loads(stdout.getvalue())
            self.assertEqual(shown["session_id"], session_id)
            self.assertEqual(shown["title"], "Planner review")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "workers", "rename", session_id, "Worker review"])
            self.assertEqual(code, 0)
            self.assertIn("renamed: Worker review", stdout.getvalue())
            self.assertTrue(shown["metadata"].get("directory_id"))

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "run",
                        "next",
                        "--session",
                        "Worker review",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertIn(f"session_id: {session_id}", stdout.getvalue())

    def test_cli_run_session_resumes_registered_codex_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            workspace.ensure()
            project = tmp / "project"
            project.mkdir()
            args_path = tmp / "codex_args.txt"
            fake = tmp / "fake_codex"
            fake.write_text(
                textwrap.dedent(
                    f"""\
                    #!{sys.executable}
                    import sys
                    from pathlib import Path

                    args = sys.argv[1:]
                    Path({str(args_path)!r}).write_text("\\n".join(args), encoding="utf-8")
                    last = None
                    for i, arg in enumerate(args):
                        if arg in {{"--output-last-message", "-o"}} and i + 1 < len(args):
                            last = Path(args[i + 1])
                    if last is not None:
                        last.write_text("resumed final", encoding="utf-8")
                    """
                ),
                encoding="utf-8",
            )
            fake.chmod(0o755)

            registry = SessionRegistry(workspace)
            registry.upsert_start(
                session_id="session-a",
                agent_id="agent-a",
                adapter="codex",
                project_dir=project,
                prompt="old",
            )
            registry.update_from_event(
                AgentEvent(
                    EventKind.STATUS,
                    "agent-a",
                    "session-a",
                    payload={"type": "thread.started", "thread_id": "thread-123"},
                )
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "run",
                        "next",
                        "--session",
                        "session-a",
                        "--codex-bin",
                        str(fake),
                    ]
                )

            self.assertEqual(code, 0)
            self.assertIn("resumed final", stdout.getvalue())
            args = args_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(args[:2], ["exec", "resume"])
            self.assertIn("thread-123", args)
            self.assertEqual(args[-1], "next")

    def test_cli_sessions_import_registers_external_codex_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            workspace.ensure()
            project = tmp / "project"
            project.mkdir()
            args_path = tmp / "codex_args.txt"
            fake = tmp / "fake_codex"
            fake.write_text(
                textwrap.dedent(
                    f"""\
                    #!{sys.executable}
                    import sys
                    from pathlib import Path

                    args = sys.argv[1:]
                    Path({str(args_path)!r}).write_text("\\n".join(args), encoding="utf-8")
                    for i, arg in enumerate(args):
                        if arg in {{"--output-last-message", "-o"}} and i + 1 < len(args):
                            Path(args[i + 1]).write_text("imported final", encoding="utf-8")
                    """
                ),
                encoding="utf-8",
            )
            fake.chmod(0o755)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "sessions",
                        "import",
                        "--provider",
                        "codex",
                        "--provider-session",
                        "thread-imported",
                        "--cwd",
                        str(project),
                        "--agent",
                        "agent-a",
                        "--title",
                        "Imported thread",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertIn("imported: Imported thread", stdout.getvalue())
            imported = SessionRegistry(workspace).resolve("thread-imported")
            assert imported is not None
            directory = DirectoryRegistry(workspace).resolve(project)
            assert directory is not None
            self.assertEqual(imported.metadata["directory_id"], directory.directory_id)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "run",
                        "continue",
                        "--session",
                        "thread-imported",
                        "--codex-bin",
                        str(fake),
                    ]
                )

            self.assertEqual(code, 0)
            self.assertIn("imported final", stdout.getvalue())
            args = args_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(args[:2], ["exec", "resume"])
            self.assertIn("thread-imported", args)

    def test_cli_sessions_scan_finds_codex_and_kimi_sessions_by_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            workspace.ensure()
            home = tmp / "home"
            project = tmp / "project"
            project.mkdir()

            codex_dir = home / ".codex"
            rollout = codex_dir / "sessions" / "2026" / "06" / "22" / "rollout.jsonl"
            rollout.parent.mkdir(parents=True)
            (codex_dir / "session_index.jsonl").parent.mkdir(parents=True, exist_ok=True)
            (codex_dir / "session_index.jsonl").write_text(
                json.dumps(
                    {
                        "id": "codex-thread-1",
                        "thread_name": "Codex old work",
                        "updated_at": "2026-06-22T00:00:00Z",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            rollout.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-06-21T00:00:00Z",
                        "type": "session_meta",
                        "payload": {"id": "codex-thread-1", "cwd": str(project)},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            kimi_dir = home / ".kimi"
            kimi_dir.mkdir(parents=True)
            kimi_dir.joinpath("kimi.json").write_text(
                json.dumps({"work_dirs": [{"path": str(project), "last_session_id": "kimi-session-1"}]}),
                encoding="utf-8",
            )
            import hashlib

            kimi_session = kimi_dir / "sessions" / hashlib.md5(str(project).encode("utf-8")).hexdigest() / "kimi-session-1"
            kimi_session.mkdir(parents=True)
            kimi_session.joinpath("state.json").write_text(json.dumps({"custom_title": "Kimi old work"}), encoding="utf-8")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "sessions",
                        "scan",
                        "--home",
                        str(home),
                        "--cwd",
                        str(project),
                        "--json",
                    ]
                )

            self.assertEqual(code, 0)
            found = json.loads(stdout.getvalue())
            ids = {item["provider_session_id"] for item in found}
            self.assertEqual(ids, {"codex-thread-1", "kimi-session-1"})

    def test_cli_workers_import_codex_index_adopts_visible_resume_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            workspace.ensure()
            home = tmp / "home"
            project = tmp / "project"
            project.mkdir()
            codex_dir = home / ".codex"
            rollout = codex_dir / "sessions" / "2026" / "06" / "30" / "rollout.jsonl"
            rollout.parent.mkdir(parents=True)
            (codex_dir / "session_index.jsonl").write_text(
                json.dumps(
                    {
                        "id": "official-thread-1",
                        "thread_name": "Official visible work",
                        "updated_at": "2026-06-30T00:00:00Z",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            rollout.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-06-30T00:00:00Z",
                        "type": "session_meta",
                        "payload": {"id": "official-thread-1", "cwd": str(project)},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "workers",
                        "scan-codex-index",
                        "--home",
                        str(home),
                        "--json",
                    ]
                )
            self.assertEqual(code, 0)
            visible = json.loads(stdout.getvalue())
            self.assertEqual(visible[0]["provider_session_id"], "official-thread-1")
            self.assertEqual(visible[0]["title"], "Official visible work")
            self.assertEqual(visible[0]["project_dir"], str(project))

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "workers",
                        "import-codex-index",
                        "Official visible work",
                        "--home",
                        str(home),
                        "--agent",
                        "agent-a",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertIn("provider_session_id: official-thread-1", stdout.getvalue())
            record = SessionRegistry(workspace).resolve("Official visible work")
            assert record is not None
            self.assertEqual(record.provider_session_id, "official-thread-1")
            self.assertEqual(record.project_dir, str(project))
            self.assertEqual(record.metadata["import_source"], "codex_session_index")

            args_path = tmp / "codex_args.txt"
            fake = tmp / "fake_codex"
            fake.write_text(
                textwrap.dedent(
                    f"""\
                    #!{sys.executable}
                    import sys
                    from pathlib import Path

                    args = sys.argv[1:]
                    Path({str(args_path)!r}).write_text("\\n".join(args), encoding="utf-8")
                    for i, arg in enumerate(args):
                        if arg in {{"--output-last-message", "-o"}} and i + 1 < len(args):
                            Path(args[i + 1]).write_text("official final", encoding="utf-8")
                    """
                ),
                encoding="utf-8",
            )
            fake.chmod(0o755)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "run",
                        "continue",
                        "--session",
                        "Official visible work",
                        "--codex-bin",
                        str(fake),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertIn("official final", stdout.getvalue())
            args = args_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(args[:2], ["exec", "resume"])
            self.assertIn("official-thread-1", args)

    def test_cli_run_session_refuses_codex_session_without_provider_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            workspace.ensure()
            project = tmp / "project"
            project.mkdir()
            fake = tmp / "fake_codex"
            fake.write_text(f"#!{sys.executable}\nraise SystemExit('should not run')\n", encoding="utf-8")
            fake.chmod(0o755)

            registry = SessionRegistry(workspace)
            registry.upsert_start(
                session_id="session-a",
                agent_id="agent-a",
                adapter="codex",
                project_dir=project,
                prompt="old",
            )

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "run",
                        "next",
                        "--session",
                        "session-a",
                        "--codex-bin",
                        str(fake),
                    ]
                )

            self.assertEqual(code, 2)
            self.assertIn("no provider session id", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
