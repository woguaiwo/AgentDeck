import contextlib
import hashlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path

from agentdeck.cli import main
from agentdeck.core.config import Workspace
from agentdeck.storage.clones import CloneStore
from agentdeck.storage.experience import ExperienceStore
from agentdeck.storage.progress import ProgressJournal
from agentdeck.storage.project_state import ProjectStateStore
from agentdeck.storage.provider_sessions import read_provider_event_bundle
from agentdeck.storage.session_state import SessionStateCard, SessionStateStore
from agentdeck.storage.sessions import SessionRegistry


class CloneCapsuleTests(unittest.TestCase):
    def test_workers_clone_generates_redacted_rules_capsule_from_codex_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            workspace.ensure()
            home = tmp / "home"
            project = tmp / "project"
            project.mkdir()
            token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"

            codex_dir = home / ".codex"
            rollout = codex_dir / "sessions" / "2026" / "06" / "27" / "rollout.jsonl"
            rollout.parent.mkdir(parents=True)
            (codex_dir / "session_index.jsonl").parent.mkdir(parents=True, exist_ok=True)
            (codex_dir / "session_index.jsonl").write_text(
                json.dumps(
                    {
                        "id": "codex-thread-1",
                        "thread_name": "Clone source",
                        "updated_at": "2026-06-27T00:00:00Z",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            rollout.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-06-27T00:00:00Z",
                                "type": "session_meta",
                                "payload": {"id": "codex-thread-1", "cwd": str(project)},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "event_msg",
                                "payload": {"role": "user", "text": f"continue work with bot token {token}"},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "compacted",
                                "payload": {"message": f"Decision: use clone capsule. secret={token}"},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {"role": "assistant", "text": "Implemented provider reader."},
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            SessionRegistry(workspace).import_provider_session(
                provider_session_id="codex-thread-1",
                provider_session_kind="codex_thread",
                agent_id="developer",
                adapter="codex",
                project_dir=project,
                title="Clone source",
                session_id="worker-1",
                project_id="agentdeck",
                metadata={"provider": "codex"},
            )
            SessionStateStore(workspace).write(
                SessionStateCard(
                    session_id="worker-1",
                    objective="Build clone capsule",
                    current_state="Provider readers are ready",
                    next_step="Generate rules capsule",
                    project_id="agentdeck",
                    agent_id="developer",
                    decisions=["Use deterministic redaction before AI summarization"],
                )
            )
            ProjectStateStore(workspace).update(
                "agentdeck",
                goal="Build manager memory",
                constraints=["Assistant must not expose bot tokens"],
                active_artifacts=["src/agentdeck/storage/clones.py"],
            )
            ProgressJournal(workspace).append(
                kind="handoff",
                summary="Clone reader implemented",
                project_id="agentdeck",
                session_id="worker-1",
                agent_id="developer",
                completed=["Added provider reader"],
                next_steps=["Wire CLI"],
                decisions=["Keep rules strategy as v1 fallback"],
            )
            collection = ExperienceStore(workspace).create_collection(
                "Clone memory design",
                kind="decision_planning",
                purpose="Keep clone inheritance scoped to useful experience.",
                project_id="agentdeck",
                worker_id="worker-1",
                agent_id="developer",
            )
            ExperienceStore(workspace).record_event(
                collection.collection_id,
                purpose="Choose file-based clone capsule over provider session copying",
                result="Clone v1 exports a safe context bundle instead of mutating native provider files.",
                decisions=["Do not hard-copy provider internals"],
                tags=["clone", "memory"],
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "workers",
                        "clone",
                        "worker-1",
                        "--home",
                        str(home),
                        "--json",
                    ]
                )

            self.assertEqual(code, 0)
            capsule = json.loads(stdout.getvalue())
            serialized = json.dumps(capsule, ensure_ascii=False)
            self.assertEqual(capsule["source_session_id"], "worker-1")
            self.assertTrue(capsule["validation"]["ok"])
            self.assertIn("Build clone capsule", serialized)
            self.assertIn("Clone memory design", serialized)
            self.assertIn("file-based clone capsule", serialized)
            self.assertIn("[REDACTED_TELEGRAM_TOKEN]", serialized)
            self.assertNotIn(token, serialized)

            context = CloneStore(workspace).context_path(capsule["clone_id"]).read_text(encoding="utf-8")
            self.assertIn("Clone Context", context)
            self.assertIn("Experience Collections", context)
            self.assertNotIn(token, context)

    def test_workers_clone_ai_strategy_uses_ephemeral_summarizer_and_cleans_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            workspace.ensure()
            home = tmp / "home"
            project = tmp / "project"
            project.mkdir()
            _write_codex_fixture(home, project)
            SessionRegistry(workspace).import_provider_session(
                provider_session_id="codex-thread-1",
                provider_session_kind="codex_thread",
                agent_id="developer",
                adapter="codex",
                project_dir=project,
                title="Clone source",
                session_id="worker-ai",
                project_id="agentdeck",
                metadata={"provider": "codex"},
            )
            SessionStateStore(workspace).write(
                SessionStateCard(
                    session_id="worker-ai",
                    objective="Summarize clone context",
                    current_state="Rules capsule exists",
                    next_step="Use ephemeral summarizer",
                    project_id="agentdeck",
                    agent_id="developer",
                )
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "workers",
                        "clone",
                        "worker-ai",
                        "--home",
                        str(home),
                        "--strategy",
                        "ai",
                        "--summarizer-adapter",
                        "echo",
                        "--json",
                    ]
                )

            self.assertEqual(code, 0)
            capsule = json.loads(stdout.getvalue())
            self.assertEqual(capsule["strategy"], "ai")
            self.assertEqual(capsule["metadata"]["ai_summarizer"]["status"], "applied")
            self.assertTrue(capsule["validation"]["ok"])
            self.assertFalse((workspace.tmp_dir / "clone-runs").exists())

    def test_workers_clone_ai_keep_debug_and_cleanup_tmp_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            workspace.ensure()
            home = tmp / "home"
            project = tmp / "project"
            project.mkdir()
            _write_codex_fixture(home, project)
            SessionRegistry(workspace).import_provider_session(
                provider_session_id="codex-thread-1",
                provider_session_kind="codex_thread",
                agent_id="developer",
                adapter="codex",
                project_dir=project,
                title="Clone source",
                session_id="worker-debug",
                metadata={"provider": "codex"},
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "workers",
                        "clone",
                        "worker-debug",
                        "--home",
                        str(home),
                        "--strategy",
                        "ai",
                        "--summarizer-adapter",
                        "echo",
                        "--keep-debug",
                        "--json",
                    ]
                )

            self.assertEqual(code, 0)
            capsule = json.loads(stdout.getvalue())
            run_id = capsule["metadata"]["ai_summarizer"]["run_id"]
            run_dir = workspace.tmp_dir / "clone-runs" / run_id
            self.assertTrue((run_dir / "manifest.json").exists())
            self.assertTrue((run_dir / "sanitized_bundle.json").exists())
            time.sleep(0.01)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["--workspace", str(workspace.root), "clones", "cleanup", "--older-than", "0"])

            self.assertEqual(code, 0)
            self.assertIn("removed: 1", stdout.getvalue())
            self.assertFalse(run_dir.exists())

    def test_clones_spawn_prepares_new_worker_from_capsule(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = Workspace(tmp / ".agentdeck")
            workspace.ensure()
            home = tmp / "home"
            project = tmp / "project"
            project.mkdir()
            _write_codex_fixture(home, project)
            SessionRegistry(workspace).import_provider_session(
                provider_session_id="codex-thread-1",
                provider_session_kind="codex_thread",
                agent_id="developer",
                adapter="codex",
                project_dir=project,
                title="Clone source",
                session_id="worker-source",
                project_id="agentdeck",
                metadata={"provider": "codex"},
            )
            SessionStateStore(workspace).write(
                SessionStateCard(
                    session_id="worker-source",
                    objective="Carry over useful clone context",
                    current_state="Source worker found a stable plan",
                    next_step="Start the cloned worker",
                    project_id="agentdeck",
                    agent_id="developer",
                    decisions=["Use prepared sessions instead of copying provider history"],
                )
            )

            clone_stdout = io.StringIO()
            with contextlib.redirect_stdout(clone_stdout):
                self.assertEqual(
                    main(
                        [
                            "--workspace",
                            str(workspace.root),
                            "workers",
                            "clone",
                            "worker-source",
                            "--home",
                            str(home),
                            "--json",
                        ]
                    ),
                    0,
                )
            clone_id = json.loads(clone_stdout.getvalue())["clone_id"]

            spawn_stdout = io.StringIO()
            with contextlib.redirect_stdout(spawn_stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "clones",
                        "spawn",
                        clone_id,
                        "--agent",
                        "clone-worker",
                        "--session-id",
                        "clone-worker-session",
                        "--adapter",
                        "codex",
                        "--json",
                    ]
                )

            self.assertEqual(code, 0)
            result = json.loads(spawn_stdout.getvalue())
            self.assertEqual(result["agent_id"], "clone-worker")
            self.assertEqual(result["session_id"], "clone-worker-session")
            self.assertIn("agentdeck run --agent clone-worker", result["first_run"])

            session = SessionRegistry(workspace).get("clone-worker-session")
            self.assertIsNotNone(session)
            assert session is not None
            self.assertEqual(session.status, "prepared")
            self.assertEqual(session.provider_session_id, "")
            self.assertTrue(session.metadata["clone_prepared"])
            self.assertEqual(session.metadata["clone_id"], clone_id)

            card = SessionStateStore(workspace).get("clone-worker-session")
            self.assertIsNotNone(card)
            assert card is not None
            self.assertEqual(card.objective, "Carry over useful clone context")
            self.assertIn("Prepared from clone capsule", card.current_state)
            self.assertTrue(any("Use prepared sessions" in decision for decision in card.decisions))

    def test_kimi_provider_event_bundle_reads_context_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            home = tmp / "home"
            project = tmp / "project"
            project.mkdir()
            kimi_dir = home / ".kimi"
            kimi_dir.mkdir(parents=True)
            kimi_dir.joinpath("kimi.json").write_text(
                json.dumps({"work_dirs": [{"path": str(project), "last_session_id": "kimi-session-1"}]}),
                encoding="utf-8",
            )
            session_dir = kimi_dir / "sessions" / hashlib.md5(str(project).encode("utf-8")).hexdigest() / "kimi-session-1"
            session_dir.mkdir(parents=True)
            session_dir.joinpath("state.json").write_text(json.dumps({"custom_title": "Kimi clone source"}), encoding="utf-8")
            session_dir.joinpath("context.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({"role": "user", "content": [{"type": "text", "text": "What changed?"}]}),
                        json.dumps({"role": "_checkpoint", "content": "Current state was compacted."}),
                        json.dumps({"role": "assistant", "content": [{"type": "text", "text": "Provider events were read."}]}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            bundle = read_provider_event_bundle(
                provider="kimi",
                provider_session_id="kimi-session-1",
                project_dir=project,
                home=home,
            )

            assert bundle is not None
            self.assertEqual(bundle.title, "Kimi clone source")
            self.assertEqual(bundle.provider_session_id, "kimi-session-1")
            self.assertIn("Current state was compacted.", [event.text for event in bundle.events])


def _write_codex_fixture(home: Path, project: Path) -> None:
    codex_dir = home / ".codex"
    rollout = codex_dir / "sessions" / "2026" / "06" / "27" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    (codex_dir / "session_index.jsonl").parent.mkdir(parents=True, exist_ok=True)
    (codex_dir / "session_index.jsonl").write_text(
        json.dumps(
            {
                "id": "codex-thread-1",
                "thread_name": "Clone source",
                "updated_at": "2026-06-27T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    rollout.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-06-27T00:00:00Z",
                        "type": "session_meta",
                        "payload": {"id": "codex-thread-1", "cwd": str(project)},
                    }
                ),
                json.dumps({"type": "event_msg", "payload": {"role": "user", "text": "continue clone work"}}),
                json.dumps({"type": "compacted", "payload": {"message": "Current clone context was compacted."}}),
                json.dumps({"type": "response_item", "payload": {"role": "assistant", "text": "Rules capsule ready."}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
