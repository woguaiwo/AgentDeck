import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from agentdeck.cli import main
from agentdeck.core.config import Workspace
from agentdeck.storage.experience import ExperienceStore


class ExperienceStoreTests(unittest.TestCase):
    def test_experience_cli_records_collection_event_and_edge(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "experience",
                        "create-collection",
                        "IMU synthesis research",
                        "--kind",
                        "research_exploration",
                        "--purpose",
                        "Explain IMU synthesis failures",
                        "--project",
                        "agentdeck",
                        "--worker",
                        "worker-imu",
                    ]
                )
            self.assertEqual(code, 0)
            collection = ExperienceStore(workspace).list_collections(worker_id="worker-imu")[0]
            self.assertEqual(collection.kind, "research_exploration")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "experience",
                        "record",
                        collection.collection_id,
                        "--purpose",
                        "Check whether root orientation causes magnitude error",
                        "--action",
                        "Compared synthetic and ground-truth IMU peaks",
                        "--result",
                        "Root orientation alone does not explain the error",
                        "--decision",
                        "Keep investigating local high-frequency events",
                        "--artifact",
                        "report:outputs/imu/event_diagnostics.md",
                        "--tag",
                        "imu",
                        "--level",
                        "micro",
                    ]
                )
            self.assertEqual(code, 0)
            first_event = ExperienceStore(workspace).list_events(collection=collection.collection_id)[0]
            self.assertEqual(first_event.artifacts[0]["kind"], "report")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "experience",
                        "record",
                        collection.collection_id,
                        "--purpose",
                        "Test local impact explanation",
                        "--result",
                        "Evidence supports local patch impact as a factor",
                        "--parent",
                        first_event.event_id,
                    ]
                )
            self.assertEqual(code, 0)
            second_event = ExperienceStore(workspace).list_events(collection=collection.collection_id)[0]

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "experience",
                        "link",
                        first_event.event_id,
                        second_event.event_id,
                        "--relation",
                        "led_to",
                        "--reason",
                        "The first diagnostic narrowed the hypothesis space.",
                    ]
                )
            self.assertEqual(code, 0)
            edges = ExperienceStore(workspace).list_edges(event_id=first_event.event_id)
            self.assertEqual(edges[0].relation, "led_to")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "experience",
                        "events",
                        "--query",
                        "high-frequency",
                    ]
            )
            self.assertEqual(code, 0)
            self.assertIn("root orientation", stdout.getvalue())

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "experience", "show", first_event.event_id])
            self.assertEqual(code, 0)
            shown = json.loads(stdout.getvalue())
            self.assertEqual(shown["event_id"], first_event.event_id)
            self.assertTrue(shown["edges"])


if __name__ == "__main__":
    unittest.main()
