"""Kimi non-interactive adapter.

This adapter targets ``kimi --print --output-format stream-json``. The stream
is treated as the primary event source, with plain text fallbacks for Kimi's
resume hint line and older output shapes.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, ClassVar

from agentdeck.adapters.capabilities import KIMI_PRINT_CAPABILITIES, AdapterCapabilities
from agentdeck.adapters.errors import classify_adapter_error, command_not_found_payload, working_directory_not_found_payload
from agentdeck.core.approvals import ApprovalMode
from agentdeck.core.cancel import CancellationToken
from agentdeck.core.config import Workspace
from agentdeck.core.events import AgentEvent, EventKind


SUBPROCESS_STREAM_LIMIT = 8 * 1024 * 1024


_RESUME_RE = re.compile(r"\bTo resume this session:\s+kimi\s+-r\s+([0-9a-fA-F-]+)")


@dataclass(frozen=True)
class KimiPrintAdapter:
    """Run Kimi through its non-interactive print surface."""

    name: str = "kimi"
    capabilities: ClassVar[AdapterCapabilities] = KIMI_PRINT_CAPABILITIES
    kimi_bin: str = "kimi"
    cwd: Path | None = None
    resume: str | None = None
    resume_last: bool = False
    model: str | None = None
    approval_mode: ApprovalMode = ApprovalMode.FAIL
    extra_args: tuple[str, ...] = field(default_factory=tuple)

    async def send(
        self,
        prompt: str,
        *,
        agent_id: str,
        session_id: str,
        workspace: Workspace,
        cancellation: CancellationToken | None = None,
    ) -> AsyncIterator[AgentEvent]:
        run_cwd = (self.cwd or Path.cwd()).expanduser().resolve()
        if not run_cwd.is_dir():
            yield AgentEvent(
                EventKind.ERROR,
                agent_id,
                session_id,
                text=f"working directory not found: {run_cwd}",
                payload=working_directory_not_found_payload(run_cwd),
            )
            return

        command = self._build_command(prompt)
        assistant_chunks: list[str] = []
        emitted_final = ""
        approval_requested = False
        stop_stdout = False

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(run_cwd),
                stdin=subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=SUBPROCESS_STREAM_LIMIT,
            )
            cancel_task = asyncio.create_task(_terminate_on_cancel(process, cancellation))

            assert process.stdout is not None
            async for raw in process.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                for event in _events_from_stdout_line(line, agent_id=agent_id, session_id=session_id):
                    if event.kind == EventKind.ASSISTANT_DELTA and event.text:
                        assistant_chunks.append(event.text)
                    if event.kind == EventKind.ASSISTANT_FINAL:
                        emitted_final = event.text
                    yield event
                    if event.kind == EventKind.APPROVAL_REQUESTED:
                        approval_requested = True
                        if self.approval_mode == ApprovalMode.FAIL:
                            process.terminate()
                            stop_stdout = True
                            yield AgentEvent(
                                EventKind.ERROR,
                                agent_id,
                                session_id,
                                text=(
                                    "Backend requested approval, but this adapter cannot answer "
                                    "mid-run approval prompts yet."
                                ),
                                payload={
                                    **classify_adapter_error("approval requested"),
                                    "approval_mode": self.approval_mode.value,
                                    "approval_required": True,
                                    "hint": "Use --approval-mode record to only log requests, or --approval-mode bypass in an isolated environment.",
                                },
                            )
                            break
                if stop_stdout:
                    break

            assert process.stderr is not None
            stderr_text = (await process.stderr.read()).decode("utf-8", errors="replace").strip()
            return_code = await _wait_for_process_exit(process)
            cancel_requested = cancellation is not None and cancellation.is_cancelled()
            cancelled = cancel_requested or await _finish_cancel_task(cancel_task)

            if cancelled:
                yield AgentEvent(
                    EventKind.CANCELLED,
                    agent_id,
                    session_id,
                    text=(cancellation.reason if cancellation is not None else "Cancellation requested."),
                    payload={
                        "source": "kimi_print",
                        "return_code": return_code,
                        "command": _redacted_command(command),
                    },
                )
                return

            stderr = ""
            if stderr_text:
                stderr_lines: list[str] = []
                for line in stderr_text.splitlines():
                    if _RESUME_RE.search(line):
                        for event in _events_from_stdout_line(line, agent_id=agent_id, session_id=session_id):
                            yield event
                    else:
                        stderr_lines.append(line)
                stderr = "\n".join(stderr_lines).strip()

            if not approval_requested or self.approval_mode != ApprovalMode.FAIL:
                if not emitted_final and assistant_chunks:
                    final_text = "".join(assistant_chunks).strip()
                    if final_text:
                        emitted_final = final_text
                        yield AgentEvent(
                            EventKind.ASSISTANT_FINAL,
                            agent_id,
                            session_id,
                            text=final_text,
                            payload={"source": "kimi_assistant_delta_join"},
                        )

            approval_fail = approval_requested and self.approval_mode == ApprovalMode.FAIL
            if return_code != 0 and not approval_fail:
                yield AgentEvent(
                    EventKind.ERROR,
                    agent_id,
                    session_id,
                    text=f"kimi --print exited with status {return_code}",
                    payload={
                        **classify_adapter_error(stderr, return_code=return_code),
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
                    payload={
                        **classify_adapter_error(stderr),
                        "source": "kimi_stderr",
                        "nonfatal": True,
                    },
                )
        except FileNotFoundError as exc:
            yield AgentEvent(
                EventKind.ERROR,
                agent_id,
                session_id,
                text=f"kimi executable not found: {self.kimi_bin}",
                payload={**command_not_found_payload(self.kimi_bin), "error": str(exc)},
            )

    def _build_command(self, prompt: str) -> list[str]:
        command = [
            self.kimi_bin,
            "--print",
            "--output-format",
            "stream-json",
        ]
        if self.cwd is not None:
            command.extend(["--work-dir", str(self.cwd.expanduser().resolve())])
        if self.resume_last:
            command.append("--continue")
        elif self.resume:
            command.extend(["--session", self.resume])
        if self.model:
            command.extend(["--model", self.model])
        if self.approval_mode == ApprovalMode.BYPASS:
            command.append("--yolo")
        command.extend(self.extra_args)
        command.extend(["--prompt", prompt])
        return command


def _events_from_stdout_line(line: str, *, agent_id: str, session_id: str) -> list[AgentEvent]:
    resume_match = _RESUME_RE.search(line)
    if resume_match:
        provider_session_id = resume_match.group(1)
        return [
            AgentEvent(
                EventKind.STATUS,
                agent_id,
                session_id,
                text="session_started",
                payload={
                    "type": "session.started",
                    "provider": "kimi",
                    "session_id": provider_session_id,
                },
            )
        ]

    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return [
            AgentEvent(
                EventKind.ASSISTANT_DELTA,
                agent_id,
                session_id,
                text=line,
                payload={"source": "kimi_stdout_text"},
            )
        ]
    if not isinstance(payload, dict):
        return []

    events: list[AgentEvent] = []
    marker = _marker(payload)

    content = payload.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = str(part.get("type") or "").lower().replace(".", "_").replace("-", "_")
            text = _extract_text(part)
            if part_type in {"text", "assistant_text", "message"} and text:
                events.append(
                    AgentEvent(
                        EventKind.ASSISTANT_DELTA,
                        agent_id,
                        session_id,
                        text=text,
                        payload=_public_payload(payload, part=part),
                    )
                )
            elif part_type in {"tool_use", "tool_call", "function_call"}:
                events.append(
                    AgentEvent(
                        EventKind.TOOL_STARTED,
                        agent_id,
                        session_id,
                        text=text,
                        payload=_public_payload(payload, part=part),
                    )
                )
            elif part_type in {"tool_result", "function_result"}:
                events.append(
                    AgentEvent(
                        EventKind.TOOL_FINISHED,
                        agent_id,
                        session_id,
                        text=text,
                        payload=_public_payload(payload, part=part),
                    )
                )
            elif part_type in {"think", "thinking", "reasoning"}:
                events.append(AgentEvent(EventKind.STATUS, agent_id, session_id, text=part_type, payload={"source": "kimi_thinking"}))
        if events:
            return events

    text = _extract_text(payload)
    if "approval" in marker and ("request" in marker or "requested" in marker):
        return [AgentEvent(EventKind.APPROVAL_REQUESTED, agent_id, session_id, text=text, payload=payload)]
    if "tool" in marker and any(word in marker for word in ("finish", "finished", "complete", "completed", "result")):
        return [AgentEvent(EventKind.TOOL_FINISHED, agent_id, session_id, text=text, payload=payload)]
    if "tool" in marker and any(word in marker for word in ("start", "started", "call", "running")):
        return [AgentEvent(EventKind.TOOL_STARTED, agent_id, session_id, text=text, payload=payload)]
    if "error" in marker:
        return [AgentEvent(EventKind.ERROR, agent_id, session_id, text=text or "kimi error", payload=payload)]
    if any(word in marker for word in ("final", "completed", "message_complete")) and text:
        return [AgentEvent(EventKind.ASSISTANT_FINAL, agent_id, session_id, text=text, payload=payload)]
    if text and any(word in marker for word in ("assistant", "delta", "message", "output")):
        return [AgentEvent(EventKind.ASSISTANT_DELTA, agent_id, session_id, text=text, payload=payload)]
    return [AgentEvent(EventKind.STATUS, agent_id, session_id, text=marker or "kimi_event", payload=payload)]


def _marker(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("type", "kind", "event", "name", "role", "status"):
        value = payload.get(key)
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
    return ""


def _public_payload(payload: dict[str, Any], *, part: dict[str, Any] | None = None) -> dict[str, Any]:
    clean = _strip_private_reasoning(payload)
    if part is not None:
        clean["part"] = _strip_private_reasoning(part)
    clean["source"] = "kimi_stream_json"
    return clean


def _strip_private_reasoning(value: Any) -> Any:
    if isinstance(value, list):
        cleaned_items = [_strip_private_reasoning(item) for item in value]
        return [item for item in cleaned_items if item not in ({}, None, "")]
    if not isinstance(value, dict):
        return value

    part_type = str(value.get("type") or "").lower().replace(".", "_").replace("-", "_")
    if part_type in {"think", "thinking", "reasoning"}:
        return {}

    clean: dict[str, Any] = {}
    for key, item in value.items():
        normalized_key = key.lower().replace(".", "_").replace("-", "_")
        if normalized_key in {"think", "thinking", "reasoning", "encrypted"}:
            continue
        stripped = _strip_private_reasoning(item)
        if stripped in ({}, None, ""):
            continue
        clean[key] = stripped
    return clean


async def _terminate_on_cancel(process: asyncio.subprocess.Process, cancellation: CancellationToken | None) -> bool:
    if cancellation is None:
        return False
    while process.returncode is None:
        if cancellation.is_cancelled():
            try:
                process.terminate()
            except ProcessLookupError:
                return True
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
            return True
        await asyncio.sleep(0.1)
    return False


async def _wait_for_process_exit(process: asyncio.subprocess.Process) -> int:
    if process.returncode is not None:
        return process.returncode
    try:
        return await asyncio.wait_for(process.wait(), timeout=1.0)
    except asyncio.TimeoutError:
        reaped = _reap_returncode(process)
        if reaped is not None:
            return reaped

    try:
        process.terminate()
    except ProcessLookupError:
        reaped = _reap_returncode(process)
        return reaped if reaped is not None else 0

    try:
        return await asyncio.wait_for(process.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        reaped = _reap_returncode(process)
        if reaped is not None:
            return reaped
        try:
            process.kill()
        except ProcessLookupError:
            return process.returncode if process.returncode is not None else 0
        try:
            return await asyncio.wait_for(process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            reaped = _reap_returncode(process)
            return reaped if reaped is not None else -9


async def _finish_cancel_task(task: asyncio.Task[bool]) -> bool:
    if task.done():
        return await task
    task.cancel()
    try:
        return await task
    except asyncio.CancelledError:
        return False


def _reap_returncode(process: asyncio.subprocess.Process) -> int | None:
    if process.returncode is not None:
        return process.returncode
    try:
        pid, status = os.waitpid(process.pid, os.WNOHANG)
    except (AttributeError, ChildProcessError, OSError):
        return process.returncode
    if pid == 0:
        return process.returncode
    if hasattr(os, "waitstatus_to_exitcode"):
        return os.waitstatus_to_exitcode(status)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    return 1


def _redacted_command(command: list[str]) -> list[str]:
    redacted: list[str] = []
    skip_next = False
    for part in command:
        if skip_next:
            redacted.append("<redacted>")
            skip_next = False
            continue
        redacted.append(part)
        if part in {"--config", "--mcp-config"}:
            skip_next = True
    return redacted
