"""Reusable run orchestration for CLI and remote interfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agentdeck.adapters.base import AgentAdapter
from agentdeck.adapters.codex_exec import CodexExecAdapter
from agentdeck.adapters.echo import EchoAdapter
from agentdeck.adapters.kimi_print import KimiPrintAdapter
from agentdeck.core.approvals import ApprovalMode
from agentdeck.core.config import Workspace
from agentdeck.core.events import AgentEvent, EventKind
from agentdeck.core.runtime import AgentRuntime
from agentdeck.storage.approvals import ApprovalRecord, ApprovalRegistry
from agentdeck.storage.agents import AgentRecord, AgentRegistry
from agentdeck.storage.projects import ProjectRecord, ProjectRegistry
from agentdeck.storage.sessions import SessionRegistry
from agentdeck.storage.tasks import TaskBoard, TaskRecord


class RunConfigurationError(ValueError):
    """Raised when a run request references missing project/task/session state."""


@dataclass
class RunRequest:
    prompt: str
    adapter: str | None = None
    project: str | None = None
    task: str | None = None
    agent: str | None = None
    session: str | None = None
    title: str | None = None
    cwd: str | Path | None = None
    codex_bin: str | None = None
    kimi_bin: str | None = None
    resume: str | None = None
    resume_last: bool = False
    model: str | None = None
    sandbox: str | None = None
    approval_mode: str | None = None
    no_skip_git_check: bool = False
    extra_args: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class RunServiceResult:
    session_id: str
    final_text: str
    events: list[AgentEvent]
    agent_id: str
    adapter: str
    project_id: str = ""
    task_id: str = ""
    approval_requested: bool = False
    pending_approvals: list[ApprovalRecord] = field(default_factory=list)


async def run_agent_prompt(workspace: Workspace, request: RunRequest) -> RunServiceResult:
    """Run a prompt using project/task/agent/session defaults."""

    workspace.ensure()
    session_registry = SessionRegistry(workspace)
    agent_registry = AgentRegistry(workspace)
    project_registry = ProjectRegistry(workspace)
    task_board = TaskBoard(workspace)
    approval_registry = ApprovalRegistry(workspace)

    task = task_board.resolve(request.task) if request.task else None
    if request.task and task is None:
        raise RunConfigurationError(f"task not found: {request.task}")
    if task is not None:
        _apply_task_defaults(request, task)

    project = project_registry.resolve(request.project) if request.project else None
    if request.project and project is None:
        raise RunConfigurationError(f"project not found: {request.project}")
    if project is not None:
        _apply_project_defaults(request, project)

    request.agent = request.agent or "default"
    saved_agent = agent_registry.resolve(request.agent) if request.agent else None
    if saved_agent is not None:
        _apply_agent_defaults(request, saved_agent, session_registry)

    session = session_registry.resolve(request.session) if request.session else None
    if request.session and session is None:
        raise RunConfigurationError(f"session not found: {request.session}")

    session_id = None
    if session is not None:
        request.agent = session.agent_id
        request.adapter = request.adapter or session.adapter
        request.cwd = request.cwd or session.project_dir
        if request.adapter in {"codex", "codex-exec", "kimi", "kimi-print"} and not request.resume and not request.resume_last:
            if not session.provider_session_id:
                raise RunConfigurationError(
                    f"session has no provider session id; pass --resume explicitly: {session.session_id}"
                )
            request.resume = session.provider_session_id
        session_id = session.session_id

    request.adapter = request.adapter or "echo"
    request.codex_bin = request.codex_bin or "codex"
    request.kimi_bin = request.kimi_bin or "kimi"
    request.approval_mode = request.approval_mode or "fail"
    project_dir = Path(request.cwd or ".").expanduser().resolve()
    adapter = _build_adapter(request)

    if task is not None:
        task_board.set_status(task.task_id, "doing")

    runtime = AgentRuntime(
        workspace,
        adapter,
        agent_id=request.agent or "default",
        project_dir=project_dir,
        project_id=request.project or "",
        task_id=task.task_id if task is not None else "",
        session_registry=session_registry,
        approval_registry=approval_registry,
    )
    result = await runtime.run_prompt(request.prompt, session_id=session_id, title=request.title)

    approval_requested = any(event.kind == EventKind.APPROVAL_REQUESTED for event in result.events)
    pending_approvals: list[ApprovalRecord] = []
    if task is not None:
        refreshed = task_board.resolve(task.task_id)
        if refreshed is not None and not refreshed.session_id:
            task_board.attach_session(refreshed.task_id, result.session_id)
        task_board.add_note(
            task.task_id,
            f"Ran prompt with agent {request.agent}; session_id: {result.session_id}",
            kind="run",
        )
        if approval_requested:
            pending_approvals = approval_registry.list(status="pending", task_id=task.task_id)
            approval_text = f"Approval required for session {result.session_id}"
            if pending_approvals:
                approval_text += f"; approval_id: {pending_approvals[0].approval_id}"
            task_board.set_status(task.task_id, "blocked", note=approval_text)

    return RunServiceResult(
        session_id=result.session_id,
        final_text=result.final_text,
        events=result.events,
        agent_id=request.agent or "default",
        adapter=request.adapter,
        project_id=request.project or "",
        task_id=task.task_id if task is not None else "",
        approval_requested=approval_requested,
        pending_approvals=pending_approvals,
    )


def _build_adapter(request: RunRequest) -> AgentAdapter:
    adapter_name = request.adapter or "echo"
    if adapter_name == "echo":
        return EchoAdapter()
    if adapter_name in {"codex", "codex-exec"}:
        return CodexExecAdapter(
            codex_bin=request.codex_bin or "codex",
            cwd=Path(request.cwd or ".").expanduser().resolve(),
            resume=request.resume,
            resume_last=request.resume_last,
            model=request.model,
            sandbox=request.sandbox,
            approval_mode=ApprovalMode.parse(request.approval_mode or "fail"),
            skip_git_repo_check=not request.no_skip_git_check,
            extra_args=tuple(request.extra_args or ()),
        )
    if adapter_name in {"kimi", "kimi-print"}:
        return KimiPrintAdapter(
            kimi_bin=request.kimi_bin or "kimi",
            cwd=Path(request.cwd or ".").expanduser().resolve(),
            resume=request.resume,
            resume_last=request.resume_last,
            model=request.model,
            approval_mode=ApprovalMode.parse(request.approval_mode or "fail"),
            extra_args=tuple(request.extra_args or ()),
        )
    raise RunConfigurationError(f"unsupported adapter: {adapter_name}")


def _apply_task_defaults(request: RunRequest, task: TaskRecord) -> None:
    request.project = request.project or task.project_id or None
    request.agent = request.agent or task.agent_id
    request.session = request.session or task.session_id or None
    request.title = request.title or task.title


def _apply_project_defaults(request: RunRequest, project: ProjectRecord) -> None:
    request.project = project.project_id
    request.cwd = request.cwd or project.project_dir
    request.agent = request.agent or project.default_agent_id


def _apply_agent_defaults(request: RunRequest, agent: AgentRecord, sessions: SessionRegistry) -> None:
    request.agent = agent.agent_id
    request.project = request.project or agent.project_id or None
    request.adapter = request.adapter or agent.adapter
    request.cwd = request.cwd or agent.project_dir
    request.model = request.model or agent.model or None
    request.sandbox = request.sandbox or agent.sandbox or None
    request.approval_mode = request.approval_mode or agent.approval_mode
    request.codex_bin = request.codex_bin or agent.codex_bin
    request.kimi_bin = request.kimi_bin or agent.kimi_bin
    request.title = request.title or agent.title

    if request.session or request.resume or request.resume_last:
        return
    if agent.resume_policy != "latest":
        return

    adapter_name = request.adapter or agent.adapter
    latest = sessions.latest_for_agent(
        agent.agent_id,
        adapter=adapter_name,
        require_provider_session=adapter_name in {"codex", "codex-exec", "kimi", "kimi-print"},
    )
    if latest is not None:
        request.session = latest.session_id
