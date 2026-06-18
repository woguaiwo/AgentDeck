import asyncio
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from agentdeck.adapters.codex_exec import CodexExecAdapter, _event_from_stdout_line
from agentdeck.core.config import Workspace
from agentdeck.core.events import EventKind
from agentdeck.core.runtime import AgentRuntime


class CodexExecAdapterTests(unittest.TestCase):
    def test_codex_exec_adapter_maps_jsonl_events_and_final_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            fake = tmp / "fake_codex"
            fake.write_text(
                textwrap.dedent(
                    f"""\
                    #!{sys.executable}
                    import json
                    import sys
                    from pathlib import Path

                    last = None
                    args = sys.argv[1:]
                    for i, arg in enumerate(args):
                        if arg in {{"--output-last-message", "-o"}} and i + 1 < len(args):
                            last = Path(args[i + 1])

                    print(json.dumps({{"type": "assistant_delta", "delta": "thinking"}}), flush=True)
                    print(json.dumps({{"type": "tool_call_started", "tool_name": "shell", "text": "run tests"}}), flush=True)
                    print(json.dumps({{"type": "tool_call_completed", "tool_name": "shell", "text": "ok"}}), flush=True)
                    if last is not None:
                        last.write_text("final from last message", encoding="utf-8")
                    """
                ),
                encoding="utf-8",
            )
            fake.chmod(0o755)

            workspace = Workspace(tmp / ".agentdeck")
            adapter = CodexExecAdapter(codex_bin=str(fake))
            runtime = AgentRuntime(workspace, adapter, agent_id="codex-test")

            result = asyncio.run(runtime.run_prompt("hello"))

            kinds = [event.kind for event in result.events]
            self.assertIn(EventKind.TOOL_STARTED, kinds)
            self.assertIn(EventKind.TOOL_FINISHED, kinds)
            self.assertEqual(result.final_text, "final from last message")

    def test_codex_item_completed_agent_message_extracts_text(self) -> None:
        event = _event_from_stdout_line(
            '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"OK"}}',
            agent_id="codex-test",
            session_id="session",
        )

        assert event is not None
        self.assertEqual(event.kind, EventKind.ASSISTANT_DELTA)
        self.assertEqual(event.text, "OK")

    def test_codex_lifecycle_events_are_status_not_assistant_text(self) -> None:
        event = _event_from_stdout_line(
            '{"type":"thread.started","thread_id":"abc"}',
            agent_id="codex-test",
            session_id="session",
        )

        assert event is not None
        self.assertEqual(event.kind, EventKind.STATUS)
