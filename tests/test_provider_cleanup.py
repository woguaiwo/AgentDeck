import contextlib
import http.server
import io
import json
import socketserver
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from agentdeck.cli import main
from agentdeck.core.config import Workspace
from agentdeck.storage.provider_cleanup import delete_provider_session
from agentdeck.storage.sessions import SessionRegistry


class ProviderCleanupTests(unittest.TestCase):
    def test_codex_provider_delete_uses_native_force_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            args_path = tmp / "args.txt"
            fake_codex = tmp / "codex"
            fake_codex.write_text(
                f"#!{sys.executable}\n"
                "import sys\n"
                f"from pathlib import Path\nPath({str(args_path)!r}).write_text('\\n'.join(sys.argv[1:]), encoding='utf-8')\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)

            result = delete_provider_session(
                provider="codex",
                provider_session_id="019ea0db-41bc-7d82-8a79-233b1576a49c",
                force=True,
                codex_bin=str(fake_codex),
            )

            self.assertTrue(result.ok)
            self.assertEqual(args_path.read_text(encoding="utf-8").splitlines(), [
                "delete",
                "019ea0db-41bc-7d82-8a79-233b1576a49c",
                "--force",
            ])

    def test_kimi_provider_delete_uses_web_api(self) -> None:
        deleted: list[str] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_DELETE(self) -> None:
                deleted.append(self.path)
                self.send_response(204)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = delete_provider_session(
                    provider="kimi",
                    provider_session_id="70df74dd-a5ce-4595-9fdd-44451ad59e7d",
                    force=True,
                    kimi_web_url=f"http://127.0.0.1:{server.server_address[1]}",
                )
            finally:
                server.shutdown()
                thread.join(timeout=2)

        self.assertTrue(result.ok)
        self.assertEqual(deleted, ["/api/sessions/70df74dd-a5ce-4595-9fdd-44451ad59e7d"])

    def test_workers_delete_provider_session_plans_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Workspace(Path(tmpdir) / ".agentdeck")
            workspace.ensure()
            SessionRegistry(workspace).import_provider_session(
                provider_session_id="codex-thread-1",
                provider_session_kind="codex_thread",
                agent_id="developer",
                adapter="codex",
                project_dir=tmpdir,
                title="Worker",
                session_id="worker-1",
                metadata={"provider": "codex"},
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "--workspace",
                        str(workspace.root),
                        "workers",
                        "delete-provider-session",
                        "worker-1",
                        "--json",
                    ]
                )

            self.assertEqual(code, 0)
            result = json.loads(stdout.getvalue())
            self.assertFalse(result["executed"])
            self.assertTrue(result["ok"])
            self.assertEqual(result["provider"], "codex")
            self.assertEqual(result["provider_session_id"], "codex-thread-1")
