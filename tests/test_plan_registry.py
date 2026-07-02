import tempfile
import unittest
import contextlib
import io
from pathlib import Path

from agentdeck.cli import main
from agentdeck.core.config import Workspace
from agentdeck.storage.plans import PlanRegistry, plan_progress, steps_from_draft


class PlanRegistryTests(unittest.TestCase):
    def test_steps_from_readable_markdown_draft(self) -> None:
        steps = steps_from_draft(
            """
# Research plan

## Prepare dataset
Collect files and normalize metadata.

## Run ablation
Compare the baseline against two variants.
"""
        )

        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0][0], "Prepare dataset")
        self.assertIn("normalize metadata", steps[0][1])
        self.assertEqual(steps[1][0], "Run ablation")

    def test_registry_creates_compiles_and_updates_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()
            registry = PlanRegistry(workspace)

            record = registry.create(
                title="Long experiment",
                draft="- [ ] collect data\n- [ ] verify outputs",
                project_id="proj",
                focus_id="focus-1",
                session_id="session-1",
                agent_id="worker",
                directory=Path(tmpdir),
            )
            self.assertEqual(record.status, "draft")

            compiled = registry.compile(record.plan_id)
            assert compiled is not None
            self.assertEqual(compiled.status, "ready")
            self.assertEqual([step.title for step in compiled.steps], ["collect data", "verify outputs"])
            self.assertEqual(plan_progress(compiled), (0, 2))

            running, step = registry.start_next_step(compiled.plan_id)
            assert running is not None
            assert step is not None
            self.assertEqual(step.step_id, "step-001")
            self.assertEqual(running.status, "running")

            updated = registry.update_step(
                compiled.plan_id,
                "step-001",
                status="done",
                report="Collected files",
                result="dataset ready",
                decision="continue",
                artifacts=["/tmp/data"],
            )
            assert updated is not None
            self.assertEqual(plan_progress(updated), (1, 2))
            self.assertEqual(updated.steps[0].report, "Collected files")
            self.assertEqual(updated.steps[0].artifacts, ["/tmp/data"])

            updated = registry.update_step(compiled.plan_id, "step-002", status="done", report="Verified")
            assert updated is not None
            self.assertEqual(updated.status, "done")

    def test_cli_plan_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / ".agentdeck"
            draft = Path(tmpdir) / "draft.md"
            draft.write_text("## Step A\nDo A\n## Step B\nDo B\n", encoding="utf-8")

            out = self._main(
                [
                    "--workspace",
                    str(workspace),
                    "plans",
                    "new",
                    "CLI plan",
                    "--draft-file",
                    str(draft),
                    "--project",
                    "proj",
                ]
            )
            self.assertIn("plan: CLI plan", out)
            plan_id = out.split("(", 1)[1].split(")", 1)[0]

            compiled = self._main(["--workspace", str(workspace), "plans", "compile", plan_id])
            self.assertIn("plan compiled", compiled)
            self.assertIn("step-001", compiled)

            status = self._main(["--workspace", str(workspace), "plans", "status", plan_id])
            self.assertIn("steps: 0/2", status)

    def _main(self, args: list[str]) -> str:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(args)
        self.assertEqual(code, 0, stdout.getvalue())
        return stdout.getvalue()


if __name__ == "__main__":
    unittest.main()
