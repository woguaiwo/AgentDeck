import unittest
import json
from pathlib import Path

from agentdeck.adapters.codex_exec import _event_from_stdout_line
from agentdeck.adapters.kimi_print import _events_from_stdout_line
from agentdeck.core.events import EventKind


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "provider_events"


class AdapterEventFixtureTests(unittest.TestCase):
    def test_codex_fixture_maps_core_events(self) -> None:
        events = []
        for line in (FIXTURE_DIR / "codex_exec.jsonl").read_text(encoding="utf-8").splitlines():
            event = _event_from_stdout_line(line, agent_id="codex-agent", session_id="session")
            if event is not None:
                events.append(event)

        kinds = [event.kind for event in events]
        self.assertIn(EventKind.STATUS, kinds)
        self.assertIn(EventKind.TOOL_STARTED, kinds)
        self.assertIn(EventKind.TOOL_FINISHED, kinds)
        self.assertIn(EventKind.ASSISTANT_FINAL, kinds)
        self.assertEqual(events[0].payload["thread_id"], "thread-fixture-1")

    def test_kimi_fixture_maps_core_events_and_resume_hint(self) -> None:
        events = []
        for line in (FIXTURE_DIR / "kimi_stream.txt").read_text(encoding="utf-8").splitlines():
            events.extend(_events_from_stdout_line(line, agent_id="kimi-agent", session_id="session"))

        kinds = [event.kind for event in events]
        self.assertIn(EventKind.STATUS, kinds)
        self.assertIn(EventKind.ASSISTANT_DELTA, kinds)
        self.assertIn(EventKind.TOOL_STARTED, kinds)
        self.assertIn(EventKind.TOOL_FINISHED, kinds)
        self.assertIn(EventKind.ASSISTANT_FINAL, kinds)

        resume_events = [
            event
            for event in events
            if event.payload.get("provider") == "kimi" and event.payload.get("session_id")
        ]
        self.assertEqual(len(resume_events), 1)
        self.assertEqual(
            resume_events[0].payload["session_id"],
            "11111111-2222-3333-4444-555555555555",
        )

    def test_kimi_fixture_does_not_store_private_thinking_payloads(self) -> None:
        events = []
        for line in (FIXTURE_DIR / "kimi_stream.txt").read_text(encoding="utf-8").splitlines():
            events.extend(_events_from_stdout_line(line, agent_id="kimi-agent", session_id="session"))

        serialized = "\n".join(json.dumps(event.to_dict(), ensure_ascii=False) for event in events)

        self.assertNotIn("private reasoning", serialized)
        self.assertNotIn('"think":', serialized)


if __name__ == "__main__":
    unittest.main()
