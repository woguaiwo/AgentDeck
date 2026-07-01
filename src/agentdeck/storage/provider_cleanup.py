"""Provider-native session cleanup helpers."""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ProviderDeleteResult:
    provider: str
    provider_session_id: str
    action: str
    ok: bool
    executed: bool = False
    command: list[str] = field(default_factory=list)
    status_code: int = 0
    message: str = ""
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def delete_provider_session(
    *,
    provider: str,
    provider_session_id: str,
    force: bool = False,
    home: str | Path | None = None,
    codex_bin: str = "codex",
    kimi_bin: str = "kimi",
    kimi_web_url: str = "",
    kimi_web_token: str = "",
    kimi_web_port: int = 0,
    timeout: float = 20.0,
) -> ProviderDeleteResult:
    clean_provider = provider.strip().lower()
    clean_session = provider_session_id.strip()
    if not clean_session:
        return ProviderDeleteResult(clean_provider, clean_session, "none", False, error="provider_session_id is empty")
    if clean_provider in {"codex", "codex-exec"}:
        return _delete_codex_session(
            clean_session,
            force=force,
            home=home,
            codex_bin=codex_bin,
            timeout=timeout,
        )
    if clean_provider in {"kimi", "kimi-print"}:
        return _delete_kimi_session(
            clean_session,
            force=force,
            home=home,
            kimi_bin=kimi_bin,
            web_url=kimi_web_url,
            web_token=kimi_web_token,
            web_port=kimi_web_port,
            timeout=timeout,
        )
    return ProviderDeleteResult(clean_provider, clean_session, "unsupported", False, error=f"unsupported provider: {provider}")


def _delete_codex_session(
    provider_session_id: str,
    *,
    force: bool,
    home: str | Path | None,
    codex_bin: str,
    timeout: float,
) -> ProviderDeleteResult:
    command = [codex_bin, "delete", provider_session_id, "--force"]
    env = _provider_env(home, provider="codex") if home else None
    result = ProviderDeleteResult(
        provider="codex",
        provider_session_id=provider_session_id,
        action="codex delete --force",
        ok=not force,
        executed=False,
        command=command,
        message="planned" if not force else "",
    )
    if not force:
        return result
    try:
        completed = subprocess.run(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        result.ok = False
        result.executed = True
        result.error = f"codex executable not found: {codex_bin}"
        result.metadata["exception"] = str(exc)
        return result
    except subprocess.TimeoutExpired:
        result.ok = False
        result.executed = True
        result.error = "codex delete timed out"
        return result
    result.executed = True
    result.status_code = completed.returncode
    result.ok = completed.returncode == 0
    result.message = _clean_output(completed.stdout)
    result.error = _clean_output(completed.stderr)
    return result


def _delete_kimi_session(
    provider_session_id: str,
    *,
    force: bool,
    home: str | Path | None,
    kimi_bin: str,
    web_url: str,
    web_token: str,
    web_port: int,
    timeout: float,
) -> ProviderDeleteResult:
    action = "kimi web api DELETE /api/sessions/{session_id}"
    result = ProviderDeleteResult(
        provider="kimi",
        provider_session_id=provider_session_id,
        action=action,
        ok=not force,
        executed=False,
        message="planned" if not force else "",
    )
    if not force:
        return result

    if web_url:
        return _delete_kimi_session_via_web(
            provider_session_id,
            base_url=web_url,
            token=web_token,
            result=result,
            timeout=timeout,
        )

    port = web_port or _free_local_port()
    token = web_token or secrets.token_urlsafe(24)
    command = [
        kimi_bin,
        "web",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--no-open",
        "--auth-token",
        token,
    ]
    result.command = [part if part != token else "[REDACTED_TOKEN]" for part in command]
    env = _provider_env(home, provider="kimi") if home else None
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        base_url = f"http://127.0.0.1:{port}"
        _wait_for_kimi_web(base_url, token=token, process=process, timeout=timeout)
        return _delete_kimi_session_via_web(
            provider_session_id,
            base_url=base_url,
            token=token,
            result=result,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        result.executed = True
        result.ok = False
        result.error = f"kimi executable not found: {kimi_bin}"
        result.metadata["exception"] = str(exc)
        return result
    except Exception as exc:
        result.executed = True
        result.ok = False
        result.error = _clean_output(str(exc))
        return result
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()


def _delete_kimi_session_via_web(
    provider_session_id: str,
    *,
    base_url: str,
    token: str,
    result: ProviderDeleteResult,
    timeout: float,
) -> ProviderDeleteResult:
    url = f"{base_url.rstrip('/')}/api/sessions/{provider_session_id}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url, method="DELETE", headers=headers)
    result.executed = True
    result.metadata["web_url"] = base_url.rstrip("/")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            result.status_code = int(response.status)
    except urllib.error.HTTPError as exc:
        result.status_code = int(exc.code)
        result.ok = False
        result.error = _http_error_detail(exc)
        return result
    except urllib.error.URLError as exc:
        result.ok = False
        result.error = _clean_output(str(exc.reason))
        return result
    result.ok = 200 <= result.status_code < 300
    result.message = _clean_output(body) if body else "deleted"
    return result


def _wait_for_kimi_web(base_url: str, *, token: str, process: subprocess.Popen[str], timeout: float) -> None:
    deadline = time.time() + max(timeout, 1.0)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"kimi web exited early with status {process.returncode}")
        request = urllib.request.Request(f"{base_url.rstrip('/')}/api/sessions?limit=1", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=1):
                return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("kimi web did not become ready")


def _provider_env(home: str | Path | None, *, provider: str) -> dict[str, str]:
    env = dict(os.environ)
    if home is None:
        return env
    base = Path(home).expanduser()
    env["HOME"] = str(base)
    if provider == "codex":
        env["CODEX_HOME"] = str(base / ".codex")
    if provider == "kimi":
        env["KIMI_HOME"] = str(base / ".kimi")
    return env


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except OSError:
        raw = ""
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and data.get("detail"):
            return _clean_output(str(data["detail"]))
        return _clean_output(raw)
    return f"HTTP {exc.code}: {exc.reason}"


def _clean_output(value: str, *, limit: int = 1000) -> str:
    clean = " ".join(str(value).strip().split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."
