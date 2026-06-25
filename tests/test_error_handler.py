import tempfile
import unittest
from pathlib import Path

from agentdeck.core.config import Workspace
from agentdeck.core.error_daemon import ErrorHandlingDaemon
from agentdeck.core.events import AgentEvent, EventKind
from agentdeck.storage.errors import ErrorIncidentStore, decide_incident
from agentdeck.storage.jobs import JobRegistry


class ErrorHandlerTests(unittest.TestCase):
    def test_error_incident_store_records_unknown_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            job = JobRegistry(workspace).create(interface="telegram", chat_id=42, task_id="task-1", prompt="go")
            event = AgentEvent(
                EventKind.ERROR,
                "agent",
                "session-1",
                text="Unexpected backend failure",
                payload={"error_kind": "unknown"},
            )

            store = ErrorIncidentStore(workspace)
            incident = store.create_from_event(job=job, event=event, adapter="codex")
            decision = decide_incident(incident)
            store.append_decision(incident, decision)
            store.mark_resolved(incident.incident_id)

            unknowns = store.unknowns()
            self.assertEqual(len(unknowns), 1)
            self.assertEqual(unknowns[0].fingerprint, incident.fingerprint)
            self.assertEqual(unknowns[0].count, 1)
            self.assertEqual(store.get(incident.incident_id).status, "resolved")

    def test_error_daemon_pauses_telegram_auto_and_records_job_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            job = JobRegistry(workspace).create(
                interface="telegram",
                chat_id=42,
                task_id="task-1",
                prompt="go",
                metadata={"bot_id": "minsys-bot4"},
            )

            from agentdeck.interfaces.telegram import TelegramChatStateStore

            TelegramChatStateStore(workspace, scope="minsys-bot4").set_auto_state(
                42,
                enabled=True,
                task_id="task-1",
                prompt="continue",
            )
            event = AgentEvent(
                EventKind.ERROR,
                "agent",
                "session-1",
                text="Selected model is at capacity.",
                payload={"error_kind": "rate_limit", "hint": "Wait and retry."},
            )
            incident = ErrorIncidentStore(workspace).create_from_event(job=job, event=event, adapter="codex")

            count = ErrorHandlingDaemon(workspace).process_once()

            self.assertEqual(count, 1)
            self.assertFalse(TelegramChatStateStore(workspace, scope="minsys-bot4").auto_state(42).get("enabled"))
            updated = JobRegistry(workspace).get(job.job_id)
            assert updated is not None
            self.assertEqual(updated.metadata["error_decision"], "pause_auto")
            self.assertEqual(updated.metadata["error_kind"], "rate_limit")


if __name__ == "__main__":
    unittest.main()
