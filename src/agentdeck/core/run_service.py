"""Reusable run orchestration for CLI and remote interfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agentdeck.adapters.base import AgentAdapter
from agentdeck.adapters.capabilities import adapter_requires_provider_session
from agentdeck.adapters.codex_exec import CodexExecAdapter
from agentdeck.adapters.echo import EchoAdapter
from agentdeck.adapters.kimi_print import KimiPrintAdapter
from agentdeck.core.approvals import ApprovalMode
from agentdeck.core.cancel import CancellationToken
from agentdeck.core.config import Workspace
from agentdeck.core.events import AgentEvent, EventKind
from agentdeck.core.runtime import AgentRuntime
from agentdeck.storage.approvals import ApprovalRecord, ApprovalRegistry
from agentdeck.storage.agents import AgentRecord, AgentRegistry, role_template_for_agent
from agentdeck.storage.memory import MemoryDocument, MemoryScope, MarkdownMemoryStore
from agentdeck.storage.progress import ProgressEntry, ProgressJournal
from agentdeck.storage.projects import ProjectRecord, ProjectRegistry
from agentdeck.storage.project_state import DecisionRecord, ProjectStateCard, ProjectStateStore
from agentdeck.storage.session_state import SessionStateCard, SessionStateStore
from agentdeck.storage.sessions import SessionRecord, SessionRegistry
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
    cancellation: CancellationToken | None = None


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
    explicit_session = bool(request.session)
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
        if explicit_session:
            raise RunConfigurationError(f"session not found: {request.session}")
        request.session = None

    session_id = None
    if session is not None:
        resume_problem = _session_resume_problem(session, request)
        if resume_problem:
            if explicit_session:
                raise RunConfigurationError(resume_problem)
            request.session = None
            session = None
        else:
            request.agent = session.agent_id
            request.adapter = request.adapter or session.adapter
            request.cwd = request.cwd or session.project_dir
            if _requires_provider_session(request.adapter) and not request.resume and not request.resume_last:
                request.resume = session.provider_session_id
            session_id = session.session_id

    request.adapter = request.adapter or "echo"
    request.codex_bin = request.codex_bin or "codex"
    request.kimi_bin = request.kimi_bin or "kimi"
    request.approval_mode = request.approval_mode or "fail"
    project_dir = Path(request.cwd or ".").expanduser().resolve()
    adapter = _build_adapter(request)

    active_task = task
    if task is not None:
        active_task = task_board.set_status(task.task_id, "doing") or task

    adapter_prompt = _inject_agentdeck_context(
        workspace,
        request.prompt,
        task=active_task,
        agent=saved_agent,
        session_id=session_id or (active_task.session_id if active_task is not None else ""),
    )

    runtime = AgentRuntime(
        workspace,
        adapter,
        agent_id=request.agent or "default",
        project_dir=project_dir,
        project_id=request.project or "",
        task_id=active_task.task_id if active_task is not None else "",
        session_registry=session_registry,
        approval_registry=approval_registry,
    )
    result = await runtime.run_prompt(
        adapter_prompt,
        display_prompt=request.prompt,
        session_id=session_id,
        title=request.title,
        cancellation=request.cancellation,
    )

    approval_requested = any(event.kind == EventKind.APPROVAL_REQUESTED for event in result.events)
    pending_approvals: list[ApprovalRecord] = []
    if active_task is not None:
        refreshed = task_board.resolve(active_task.task_id)
        if refreshed is not None and refreshed.session_id != result.session_id:
            task_board.attach_session(refreshed.task_id, result.session_id)
        task_board.add_note(
            active_task.task_id,
            f"Ran prompt with agent {request.agent}; session_id: {result.session_id}",
            kind="run",
        )
        if approval_requested:
            pending_approvals = approval_registry.list(status="pending", task_id=active_task.task_id)
            approval_text = f"Approval required for session {result.session_id}"
            if pending_approvals:
                approval_text += f"; approval_id: {pending_approvals[0].approval_id}"
            task_board.set_status(active_task.task_id, "blocked", note=approval_text)

    return RunServiceResult(
        session_id=result.session_id,
        final_text=result.final_text,
        events=result.events,
        agent_id=request.agent or "default",
        adapter=request.adapter,
        project_id=request.project or "",
        task_id=active_task.task_id if active_task is not None else "",
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


def _inject_agentdeck_context(
    workspace: Workspace,
    prompt: str,
    *,
    task: TaskRecord | None,
    agent: AgentRecord | None = None,
    session_id: str = "",
) -> str:
    context = build_agentdeck_context(workspace, task=task, agent=agent, session_id=session_id)
    if not context:
        return prompt
    return f"{prompt.rstrip()}\n\n---\n{context}"


def build_agentdeck_context(
    workspace: Workspace,
    *,
    task: TaskRecord | None,
    agent: AgentRecord | None = None,
    session_id: str = "",
    max_chars: int = 5000,
    include_memories: bool = True,
) -> str:
    state_id = session_id or (task.session_id if task is not None else "")
    project_id = task.project_id if task is not None else ""
    project_state = ProjectStateStore(workspace).get(project_id) if project_id else None
    project_decisions = ProjectStateStore(workspace).decisions(project_id, limit=5) if project_id else []
    state = SessionStateStore(workspace).get(state_id) if state_id else None
    active_agent = agent or (AgentRegistry(workspace).resolve(task.agent_id) if task is not None and task.agent_id else None)
    agent_role_template = role_template_for_agent(active_agent) if active_agent is not None else ""
    memories = collect_relevant_memories(workspace, task) if include_memories else []
    handoffs = ProgressJournal(workspace).list(
        kind="handoff",
        task_id=task.task_id if task is not None else None,
        session_id=None if task is not None else (state_id or None),
        limit=3,
    )
    reviews = ProgressJournal(workspace).list(
        kind="manager-review",
        task_id=task.task_id if task is not None else None,
        session_id=None if task is not None else (state_id or None),
        limit=3,
    )

    if (
        task is None
        and project_state is None
        and not project_decisions
        and state is None
        and not agent_role_template
        and not memories
        and not handoffs
        and not reviews
    ):
        return ""

    lines = [
        "AgentDeck context:",
        "Use this as compact project/task state. The user request is above this block.",
    ]
    if project_state is not None:
        _append_project_state_context(lines, project_state)
    if project_decisions:
        _append_project_decisions_context(lines, project_decisions)
    if task is not None:
        _append_task_context(lines, task)
    if active_agent is not None and agent_role_template:
        _append_agent_role_context(lines, active_agent, agent_role_template)
    if state is not None:
        _append_state_context(lines, state)
    if memories:
        _append_memory_context(lines, memories)
    if handoffs:
        _append_handoff_context(lines, handoffs)
    if reviews:
        _append_review_context(lines, reviews)
    return _limit_text("\n".join(lines), max_chars=max_chars)


def collect_relevant_memories(workspace: Workspace, task: TaskRecord | None) -> list[MemoryDocument]:
    if task is None:
        return []
    store = MarkdownMemoryStore(workspace)
    targets: list[tuple[MemoryScope, str | None, int]] = [("project", None, 2)]
    if task.project_id:
        targets.append(("project", task.project_id, 3))
    if task.team_id:
        targets.append(("team", task.team_id, 2))
    if task.agent_id:
        targets.append(("agent", task.agent_id, 2))
    targets.append(("task", task.task_id, 3))

    memories: list[MemoryDocument] = []
    seen: set[Path] = set()
    for scope, owner, limit in targets:
        for document in store.list_documents(scope=scope, owner=owner, limit=limit):
            resolved = document.path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            memories.append(document)
    memories.sort(key=lambda item: (not item.pinned, _memory_relevance_rank(item, task), -item.modified_at))
    return memories[:6]


def _memory_relevance_rank(memory: MemoryDocument, task: TaskRecord) -> int:
    if memory.scope == "task" and memory.owner == task.task_id:
        return 0
    if memory.scope == "project" and memory.owner == task.project_id:
        return 1
    if memory.scope == "team" and memory.owner == task.team_id:
        return 2
    if memory.scope == "agent" and memory.owner == task.agent_id:
        return 3
    if memory.scope == "project" and not memory.owner:
        return 4
    return 5


def _append_project_state_context(lines: list[str], state: ProjectStateCard) -> None:
    lines.append("")
    lines.append("Project state:")
    _append_optional_line(lines, "goal", state.goal, max_chars=360)
    _append_optional_line(lines, "phase", state.phase, max_chars=160)
    _append_optional_line(lines, "current focus", state.current_focus, max_chars=300)
    _append_list(lines, "next steps", state.next_steps, max_items=5)
    _append_list(lines, "constraints", state.constraints, max_items=5)
    _append_list(lines, "blockers", state.blockers, max_items=4)
    _append_list(lines, "active artifacts", state.active_artifacts, max_items=5)


def _append_project_decisions_context(lines: list[str], decisions: list[DecisionRecord]) -> None:
    lines.append("")
    lines.append("Project decisions:")
    for decision in decisions[:5]:
        lines.append(f"- {_one_line(decision.decision, 240)}")
        if decision.reason:
            lines.append(f"  reason: {_one_line(decision.reason, 220)}")
        if decision.impact:
            lines.append(f"  impact: {_one_line(decision.impact, 220)}")


def _append_task_context(lines: list[str], task: TaskRecord) -> None:
    lines.append("")
    lines.append("Task:")
    lines.append(f"- title: {_one_line(task.title, 160)}")
    lines.append(f"- id: {task.task_id}")
    lines.append(f"- status: {task.status}  priority: {task.priority}")
    if task.project_id:
        lines.append(f"- project: {task.project_id}")
    if task.agent_id:
        lines.append(f"- owner agent: {task.agent_id}")
    if task.description:
        lines.append(f"- objective: {_one_line(task.description, 360)}")


def _append_agent_role_context(lines: list[str], agent: AgentRecord, template: str) -> None:
    lines.append("")
    lines.append("Agent role guidance:")
    lines.append(f"- agent: {_one_line(agent.title, 160)} ({agent.agent_id})")
    lines.append(f"- role: {agent.role}")
    _append_list(lines, "guidance", template.splitlines(), max_items=6)


def _append_state_context(lines: list[str], state: SessionStateCard) -> None:
    lines.append("")
    lines.append("Session state card:")
    _append_optional_line(lines, "objective", state.objective, max_chars=360)
    _append_optional_line(lines, "current state", state.current_state, max_chars=360)
    _append_optional_line(lines, "next step", state.next_step, max_chars=240)
    _append_list(lines, "blockers", state.blockers, max_items=4)
    _append_list(lines, "verified work", state.verified_work, max_items=5)
    _append_list(lines, "decisions", state.decisions, max_items=5)
    _append_list(lines, "active artifacts", state.active_artifacts, max_items=5)


def _append_memory_context(lines: list[str], memories: list[MemoryDocument]) -> None:
    lines.append("")
    lines.append("Relevant durable memories:")
    for memory in memories:
        owner = f":{memory.owner}" if memory.owner else ""
        pin = " pinned" if memory.pinned else ""
        lines.append(f"- {memory.title} [{memory.scope}{owner}]{pin}")
        excerpt = _memory_excerpt(memory.content, max_chars=520)
        if excerpt:
            lines.append(f"  {excerpt}")


def _append_handoff_context(lines: list[str], handoffs: list[ProgressEntry]) -> None:
    lines.append("")
    lines.append("Recent handoffs:")
    for entry in handoffs:
        lines.append(f"- {entry.summary}")
        if entry.next_steps:
            lines.append(f"  next: {_one_line(entry.next_steps[0], 240)}")
        if entry.blockers:
            lines.append(f"  blocker: {_one_line(entry.blockers[0], 240)}")
        if entry.decisions:
            lines.append(f"  decision: {_one_line(entry.decisions[0], 240)}")


def _append_review_context(lines: list[str], reviews: list[ProgressEntry]) -> None:
    lines.append("")
    lines.append("Recent manager reviews:")
    for entry in reviews:
        status = str(entry.metadata.get("status") or "").strip()
        prefix = f"{status}: " if status else ""
        lines.append(f"- {prefix}{_one_line(entry.summary, 240)}")
        if entry.next_steps:
            lines.append(f"  next: {_one_line(entry.next_steps[0], 240)}")
        if entry.blockers:
            lines.append(f"  blocker: {_one_line(entry.blockers[0], 240)}")
        if entry.decisions:
            lines.append(f"  decision: {_one_line(entry.decisions[0], 240)}")


def _append_optional_line(lines: list[str], label: str, value: str, *, max_chars: int) -> None:
    clean = _one_line(value, max_chars)
    if clean:
        lines.append(f"- {label}: {clean}")


def _append_list(lines: list[str], label: str, values: list[str], *, max_items: int) -> None:
    clean_values = [_one_line(value, 220) for value in values if _one_line(value, 220)]
    if not clean_values:
        return
    lines.append(f"- {label}:")
    for value in clean_values[-max_items:]:
        lines.append(f"  - {value}")


def _one_line(value: str, max_chars: int) -> str:
    clean = " ".join(str(value).strip().split())
    if len(clean) <= max_chars:
        return clean
    return clean[: max_chars - 1].rstrip() + "..."


def _memory_excerpt(value: str, *, max_chars: int) -> str:
    lines = []
    for line in value.splitlines():
        clean = line.strip()
        if not clean or clean == "---":
            continue
        if clean.startswith("# "):
            continue
        lines.append(clean)
    return _one_line(" ".join(lines), max_chars)


def _limit_text(value: str, *, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 1].rstrip() + "..."


def _session_resume_problem(session: SessionRecord, request: RunRequest) -> str:
    requested_adapter = request.adapter
    if requested_adapter and requested_adapter != session.adapter:
        return (
            f"session adapter mismatch: {session.session_id} uses {session.adapter}, "
            f"but this run uses {requested_adapter}"
        )

    adapter_name = requested_adapter or session.adapter
    if (
        _requires_provider_session(adapter_name)
        and not request.resume
        and not request.resume_last
        and not session.provider_session_id
    ):
        return f"session has no provider session id; pass --resume explicitly: {session.session_id}"
    return ""


def _requires_provider_session(adapter_name: str | None) -> bool:
    return adapter_requires_provider_session(adapter_name)


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
        require_provider_session=_requires_provider_session(adapter_name),
    )
    if latest is not None:
        request.session = latest.session_id
