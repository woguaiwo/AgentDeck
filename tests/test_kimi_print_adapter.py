import asyncio
import contextlib
import io
import json
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from agentdeck.adapters.kimi_print import KimiPrintAdapter, _events_from_stdout_line
from agentdeck.cli import main
from agentdeck.core.approvals import ApprovalMode
from agentdeck.core.cancel import CancellationToken
from agentdeck.core.config import Workspace
from agentdeck.core.events import EventKind
from agentdeck.core.runtime import AgentRuntime
from agentdeck.storage.sessions import SessionRegistry


class KimiPrintAdapterTests(unittest.TestCase):
    def test_kimi_print_adapter_maps_stream_json_and_resume_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            fake = tmp / "fake_kimi"
            fake.write_text(
                textwrap.dedent(
                    f"""\
                    #!{sys.executable}
                    import json

                    print(json.dumps({{
                        "role": "assistant",
                        "content": [
                            {{"type": "think", "think": "private reasoning"}},
                            {{"type": "text", "text": "final from kimi"}}
                        ]
                    }}), flush=True)
                    print("To resume this session: kimi -r 11111111-2222-3333-4444-555555555555", file=sys.stderr, flush=True)
                    """
                ),
                encoding="utf-8",
            )
            fake.chmod(0o755)

            workspace = Workspace(tmp / ".agentdeck")
            adapter = KimiPrintAdapter(kimi_bin=str(fake), cwd=tmp)
            runtime = AgentRuntime(workspace, adapter, agent_id="kimi-test")

            result = asyncio.run(runtime.run_prompt("hello"))

            kinds = [event.kind for event in result.events]
            self.assertIn(EventKind.STATUS, kinds)
            self.assertIn(EventKind.ASSISTANT_DELTA, kinds)
            self.assertEqual(result.final_text, "final from kimi")
            record = SessionRegistry(workspace).get(result.session_id)
            assert record is not None
            self.assertEqual(record.provider_session_id, "11111111-2222-3333-4444-555555555555")
            self.assertEqual(record.provider_session_kind, "kimi_session")

    def test_resume_command_uses_kimi_session_flag(self) -> None:
        adapter = KimiPrintAdapter(
            cwd=Path("/tmp/project"),
            resume="session-123",
            model="kimi-test-model",
            approval_mode=ApprovalMode.BYPASS,
        )

        command = adapter._build_command("hello")

        self.assertEqual(command[:4], ["kimi", "--print", "--output-format", "stream-json"])
        self.assertIn("--work-dir", command)
        self.assertIn("--session", command)
        self.assertIn("session-123", command)
        self.assertIn("--model", command)
        self.assertIn("kimi-test-model", command)
        self.assertIn("--yolo", command)
        self.assertEqual(command[-2:], ["--prompt", "hello"])

    def test_resume_hint_line_maps_to_status_event(self) -> None:
        events = _events_from_stdout_line(
            "To resume this session: kimi -r 11111111-2222-3333-4444-555555555555",
            agent_id="kimi-test",
            session_id="session",
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, EventKind.STATUS)
        self.assertEqual(events[0].payload["provider"], "kimi")
        self.assertEqual(events[0].payload["session_id"], "11111111-2222-3333-4444-555555555555")

    def test_cli_task_resume_forwards_kimi_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            project = tmp / "project"
            project.mkdir()
            args_log = tmp / "kimi_args.jsonl"
            fake = tmp / "fake_kimi"
            fake.write_text(
                textwrap.dedent(
                    f"""\
                    #!{sys.executable}
                    import json
                    import sys
                    from pathlib import Path

                    log = Path({str(args_log)!r})
                    with log.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(sys.argv[1:]) + "\\n")
                    print(json.dumps({{"role": "assistant", "content": [{{"type": "text", "text": "ok"}}]}}), flush=True)
                    print("To resume this session: kimi -r 11111111-2222-3333-4444-555555555555", file=sys.stderr, flush=True)
                    """
                ),
                encoding="utf-8",
            )
            fake.chmod(0o755)

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "--workspace",
                            str(workspace.root),
                            "projects",
                            "create",
                            "kimiproj",
                            "--cwd",
                            str(project),
                        ]
                    ),
                    0,
                )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "--workspace",
                            str(workspace.root),
                            "agents",
                            "create",
                            "owner",
                            "--project",
                            "kimiproj",
                            "--adapter",
                            "kimi",
                            "--kimi-bin",
                            str(fake),
                        ]
                    ),
                    0,
                )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    main(["--workspace", str(workspace.root), "tasks", "create", "Kimi resume", "--project", "kimiproj"]),
                    0,
                )
            task_id = stdout.getvalue().split("(", 1)[1].split(")", 1)[0]

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--workspace", str(workspace.root), "run", "first", "--task", task_id]), 0)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--workspace", str(workspace.root), "run", "second", "--task", task_id]), 0)

            calls = [json.loads(line) for line in args_log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(calls), 2)
            self.assertNotIn("--session", calls[0])
            self.assertIn("--session", calls[1])
            self.assertIn("11111111-2222-3333-4444-555555555555", calls[1])

    def test_cancellation_terminates_kimi_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            fake = tmp / "fake_kimi"
            fake.write_text(
                textwrap.dedent(
                    f"""\
                    #!{sys.executable}
                    import json
                    import time

                    print(json.dumps({{"role": "assistant", "content": [{{"type": "text", "text": "started"}}]}}), flush=True)
                    time.sleep(30)
                    """
                ),
                encoding="utf-8",
            )
            fake.chmod(0o755)

            async def run_cancelled() -> object:
                workspace = Workspace(tmp / ".agentdeck")
                adapter = KimiPrintAdapter(kimi_bin=str(fake), cwd=tmp)
                runtime = AgentRuntime(workspace, adapter, agent_id="kimi-test")
                cancellation = CancellationToken()
                task = asyncio.create_task(runtime.run_prompt("hello", cancellation=cancellation))
                await asyncio.sleep(0.2)
                cancellation.cancel("stop requested")
                return await asyncio.wait_for(task, timeout=5)

            started_at = time.time()
            result = asyncio.run(run_cancelled())

            self.assertLess(time.time() - started_at, 5)
            kinds = [event.kind for event in result.events]
            self.assertIn(EventKind.CANCELLED, kinds)


if __name__ == "__main__":
    unittest.main()
