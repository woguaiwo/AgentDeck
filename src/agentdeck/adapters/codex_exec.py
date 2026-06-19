"""Codex non-interactive adapter.

This adapter intentionally targets ``codex exec --json`` instead of the Codex
interactive TUI. The JSONL stream is treated as the primary event source, and
``--output-last-message`` is used as a stable final-answer fallback.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from agentdeck.core.approvals import ApprovalMode
from agentdeck.core.config import Workspace
from agentdeck.core.events import AgentEvent, EventKind


@dataclass(frozen=True)
class CodexExecAdapter:
    """Run Codex through the non-interactive ``codex exec`` surface."""

    name: str = "codex"
    codex_bin: str = "codex"
    cwd: Path | None = None
    resume: str | None = None
    resume_last: bool = False
    model: str | None = None
    sandbox: str | None = None
    skip_git_repo_check: bool = True
    approval_mode: ApprovalMode = ApprovalMode.FAIL
    extra_args: tuple[str, ...] = field(default_factory=tuple)

    async def send(
        self,
        prompt: str,
        *,
        agent_id: str,
        session_id: str,
        workspace: Workspace,
    ) -> AsyncIterator[AgentEvent]:
        run_cwd = (self.cwd or Path.cwd()).expanduser().resolve()
        with tempfile.NamedTemporaryFile(
            prefix="agentdeck-codex-last-",
            suffix=".md",
            delete=False,
        ) as handle:
            last_message_path = Path(handle.name)

        command = self._build_command(prompt, last_message_path)
        assistant_chunks: list[str] = []
        emitted_final = ""
        approval_requested = False

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(run_cwd),
                stdin=subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            assert process.stdout is not None
            async for raw in process.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                event = _event_from_stdout_line(
                    line,
                    agent_id=agent_id,
                    session_id=session_id,
                )
                if event is None:
                    continue
                if event.kind == EventKind.ASSISTANT_DELTA and event.text:
                    assistant_chunks.append(event.text)
                if event.kind == EventKind.ASSISTANT_FINAL:
                    emitted_final = event.text
                yield event
                if event.kind == EventKind.APPROVAL_REQUESTED:
                    approval_requested = True
                    if self.approval_mode == ApprovalMode.FAIL:
                        process.terminate()
                        yield AgentEvent(
                            EventKind.ERROR,
                            agent_id,
                            session_id,
                            text=(
                                "Backend requested approval, but this adapter cannot answer "
                                "mid-run approval prompts yet."
                            ),
                            payload={
                                "approval_mode": self.approval_mode.value,
                                "approval_required": True,
                                "hint": "Use --approval-mode record to only log requests, or --approval-mode bypass in an isolated environment.",
                            },
                        )
                        break

            assert process.stderr is not None
            stderr = _filter_stderr((await process.stderr.read()).decode("utf-8", errors="replace"))
            return_code = await process.wait()

            if not approval_requested or self.approval_mode != ApprovalMode.FAIL:
                final_text = _read_last_message(last_message_path).strip()
                if final_text and final_text != emitted_final:
                    emitted_final = final_text
                    yield AgentEvent(
                        EventKind.ASSISTANT_FINAL,
                        agent_id,
                        session_id,
                        text=final_text,
                        payload={"source": "codex_output_last_message"},
                    )
                elif not emitted_final and assistant_chunks:
                    final_text = "".join(assistant_chunks).strip()
                    if final_text:
                        emitted_final = final_text
                        yield AgentEvent(
                            EventKind.ASSISTANT_FINAL,
                            agent_id,
                            session_id,
                            text=final_text,
                            payload={"source": "assistant_delta_join"},
                        )

            approval_fail = approval_requested and self.approval_mode == ApprovalMode.FAIL
            if return_code != 0 and not approval_fail:
                yield AgentEvent(
                    EventKind.ERROR,
                    agent_id,
                    session_id,
                    text=f"codex exec exited with status {return_code}",
                    payload={
                        "return_code": return_code,
                        "stderr": stderr[-8000:],
                        "command": _redacted_command(command),
                    },
                )
            elif stderr:
                yield AgentEvent(
                    EventKind.ERROR,
                    agent_id,
                    session_id,
                    text=stderr[-8000:],
                    payload={"source": "codex_stderr", "nonfatal": True},
                )
        except FileNotFoundError as exc:
            yield AgentEvent(
                EventKind.ERROR,
                agent_id,
                session_id,
                text=f"codex executable not found: {self.codex_bin}",
                payload={"error": str(exc)},
            )
        finally:
            try:
                last_message_path.unlink()
            except OSError:
                pass

    def _build_command(self, prompt: str, last_message_path: Path) -> list[str]:
        if self.resume or self.resume_last:
            command = [
                self.codex_bin,
                "exec",
                "resume",
                "--json",
                "--output-last-message",
                str(last_message_path),
            ]
            self._append_common_options(command, include_cd=False, include_sandbox=False)
            if self.resume_last:
                command.append("--last")
            elif self.resume:
                command.append(self.resume)
            command.append(prompt)
            return command

        command = [
            self.codex_bin,
            "exec",
            "--json",
            "--color",
            "never",
            "--output-last-message",
            str(last_message_path),
        ]
        self._append_common_options(command, include_cd=True, include_sandbox=True)
        command.append(prompt)
        return command

    def _append_common_options(self, command: list[str], *, include_cd: bool, include_sandbox: bool) -> None:
        if include_cd and self.cwd is not None:
            command.extend(["--cd", str(self.cwd.expanduser().resolve())])
        if self.skip_git_repo_check:
            command.append("--skip-git-repo-check")
        if self.model:
            command.extend(["--model", self.model])
        if self.approval_mode == ApprovalMode.BYPASS:
            command.append("--dangerously-bypass-approvals-and-sandbox")
        elif include_sandbox and self.sandbox:
            command.extend(["--sandbox", self.sandbox])
        command.extend(self.extra_args)


def _event_from_stdout_line(line: str, *, agent_id: str, session_id: str) -> AgentEvent | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return AgentEvent(
            EventKind.ASSISTANT_DELTA,
            agent_id,
            session_id,
            text=line,
            payload={"source": "codex_stdout_text"},
        )
    if not isinstance(payload, dict):
        return None

    marker = _marker(payload)
    text = _extract_text(payload)

    event_type = str(payload.get("type") or "").lower().replace(".", "_").replace("-", "_")
    item = payload.get("item")
    item_type = ""
    if isinstance(item, dict):
        item_type = str(item.get("type") or "").lower().replace(".", "_").replace("-", "_")

    if event_type in {"thread_started", "turn_started", "turn_completed"}:
        return AgentEvent(
            EventKind.STATUS,
            agent_id,
            session_id,
            text=event_type,
            payload=payload,
        )

    if event_type in {"item_started", "item_completed"} and item_type:
        if item_type in {"agent_message", "assistant_message", "message"} and text:
            return AgentEvent(EventKind.ASSISTANT_DELTA, agent_id, session_id, text=text, payload=payload)
        if "tool" in item_type and event_type == "item_started":
            return AgentEvent(EventKind.TOOL_STARTED, agent_id, session_id, text=text, payload=payload)
        if "tool" in item_type and event_type == "item_completed":
            return AgentEvent(EventKind.TOOL_FINISHED, agent_id, session_id, text=text, payload=payload)

    if "approval" in marker and ("request" in marker or "requested" in marker):
        return AgentEvent(EventKind.APPROVAL_REQUESTED, agent_id, session_id, text=text, payload=payload)
    if "tool" in marker and any(word in marker for word in ("finish", "finished", "complete", "completed", "result")):
        return AgentEvent(EventKind.TOOL_FINISHED, agent_id, session_id, text=text, payload=payload)
    if "tool" in marker and any(word in marker for word in ("start", "started", "call", "running")):
        return AgentEvent(EventKind.TOOL_STARTED, agent_id, session_id, text=text, payload=payload)
    if "error" in marker:
        return AgentEvent(EventKind.ERROR, agent_id, session_id, text=text or "codex error", payload=payload)
    if any(word in marker for word in ("final", "completed", "turn_complete", "message_complete")) and text:
        return AgentEvent(EventKind.ASSISTANT_FINAL, agent_id, session_id, text=text, payload=payload)
    if text and any(word in marker for word in ("assistant", "delta", "message", "output")):
        return AgentEvent(EventKind.ASSISTANT_DELTA, agent_id, session_id, text=text, payload=payload)
    return AgentEvent(EventKind.STATUS, agent_id, session_id, text=marker or "codex_event", payload=payload)


def _marker(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("type", "kind", "event", "name", "role", "status"):
        value = payload.get(key)
        if value is not None:
            parts.append(str(value))
    nested = payload.get("message")
    if isinstance(nested, dict):
        for key in ("type", "kind", "event", "role", "status"):
            value = nested.get(key)
            if value is not None:
                parts.append(str(value))
    item = payload.get("item")
    if isinstance(item, dict):
        for key in ("type", "kind", "event", "role", "status", "name"):
            value = item.get(key)
            if value is not None:
                parts.append(str(value))
    return " ".join(parts).lower().replace(".", "_").replace("-", "_")


def _extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_extract_text(item) for item in value)
    if not isinstance(value, dict):
        return ""

    for key in ("text", "delta", "content", "summary", "output"):
        item = value.get(key)
        if isinstance(item, str):
            return item
        if isinstance(item, list):
            text = "".join(_extract_text(part) for part in item)
            if text:
                return text

    message = value.get("message")
    if isinstance(message, (dict, list, str)):
        text = _extract_text(message)
        if text:
            return text
    item = value.get("item")
    if isinstance(item, (dict, list, str)):
        text = _extract_text(item)
        if text:
            return text
    return ""


def _read_last_message(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _filter_stderr(stderr: str) -> str:
    ignored = {
        "Reading additional input from stdin...",
    }
    lines = [line for line in stderr.splitlines() if line.strip() not in ignored]
    return "\n".join(lines).strip()


def _redacted_command(command: list[str]) -> list[str]:
    redacted: list[str] = []
    skip_next = False
    for part in command:
        if skip_next:
            redacted.append("<redacted>")
            skip_next = False
            continue
        redacted.append(part)
        if part in {"--config", "-c"}:
            skip_next = True
    return redacted
