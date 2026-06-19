"""AgentDeck command line interface."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import sys
from pathlib import Path

from agentdeck.adapters.codex_exec import CodexExecAdapter
from agentdeck.adapters.echo import EchoAdapter
from agentdeck.adapters.kimi_print import KimiPrintAdapter
from agentdeck.adapters.base import AgentAdapter
from agentdeck.core.approvals import ApprovalMode
from agentdeck.core.config import Workspace
from agentdeck.core.runtime import AgentRuntime
from agentdeck.storage.event_log import EventLog
from agentdeck.storage.agents import AgentRecord, AgentRegistry
from agentdeck.storage.memory import MarkdownMemoryStore
from agentdeck.storage.projects import ProjectRecord, ProjectRegistry
from agentdeck.storage.sessions import SessionRecord, SessionRegistry
from agentdeck.storage.tasks import TASK_PRIORITIES, TASK_STATUSES, TaskBoard, TaskRecord


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentdeck", description="Remote control plane for AI agent teams")
    parser.add_argument("--workspace", help="Override .agentdeck workspace path")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create a project-local AgentDeck workspace")
    init.add_argument("path", nargs="?", default=".", help="Project directory")

    sub.add_parser("doctor", help="Print workspace diagnostics")

    run = sub.add_parser("run", help="Run a prompt through an adapter")
    run.add_argument("prompt")
    run.add_argument("--adapter", choices=["echo", "codex", "codex-exec", "kimi", "kimi-print"])
    run.add_argument("--project", help="Project id or title")
    run.add_argument("--task", help="Task id or title")
    run.add_argument("--agent")
    run.add_argument("--session", help="Resume an AgentDeck session id, agent id, or provider session id")
    run.add_argument("--title", help="Human-readable title for a new or resumed AgentDeck session")
    run.add_argument("--cwd", help="Project directory used by the wrapped agent")
    run.add_argument("--codex-bin", help="Codex executable path")
    run.add_argument("--kimi-bin", help="Kimi executable path")
    run.add_argument("--resume", help="Resume a provider session id or thread name")
    run.add_argument("--resume-last", action="store_true", help="Resume the provider's most recent session when supported")
    run.add_argument("--model", help="Model override for adapters that support it")
    run.add_argument("--sandbox", choices=["read-only", "workspace-write", "danger-full-access"])
    run.add_argument(
        "--approval-mode",
        choices=["fail", "record", "bypass"],
        help=(
            "How to handle backend approval requests. 'fail' stops on approval, "
            "'record' only logs requests, and 'bypass' asks the backend to skip approvals."
        ),
    )
    run.add_argument("--no-skip-git-check", action="store_true", help="Do not pass --skip-git-repo-check to Codex")
    run.add_argument("--extra-arg", action="append", default=[], help="Extra raw argument forwarded to the adapter")

    memory = sub.add_parser("memory", help="Manage markdown memory")
    mem_sub = memory.add_subparsers(dest="memory_command", required=True)
    mem_add = mem_sub.add_parser("add", help="Add a memory entry")
    mem_add.add_argument("title")
    mem_add.add_argument("content")
    mem_add.add_argument("--scope", default="project", choices=["user", "project", "team", "agent", "task"])
    mem_add.add_argument("--owner")
    mem_list = mem_sub.add_parser("list", help="List memory entries")
    mem_list.add_argument("--scope", default="project", choices=["user", "project", "team", "agent", "task"])
    mem_list.add_argument("--owner")

    events = sub.add_parser("events", help="Inspect event log")
    events.add_argument("--tail", type=int, default=20)

    sessions = sub.add_parser("sessions", help="Inspect session registry")
    sess_sub = sessions.add_subparsers(dest="sessions_command", required=True)
    sess_list = sess_sub.add_parser("list", help="List known sessions")
    sess_list.add_argument("--agent")
    sess_show = sess_sub.add_parser("show", help="Show one session as JSON")
    sess_show.add_argument("session")
    sess_rename = sess_sub.add_parser("rename", help="Rename one session")
    sess_rename.add_argument("session")
    sess_rename.add_argument("title")

    agents = sub.add_parser("agents", help="Manage project agents")
    agent_sub = agents.add_subparsers(dest="agents_command", required=True)
    agent_create = agent_sub.add_parser("create", help="Create or replace one agent")
    agent_create.add_argument("agent_id")
    agent_create.add_argument("--title", help="Human-readable agent name")
    agent_create.add_argument("--project", help="Project id this agent belongs to")
    agent_create.add_argument("--role", default="owner", help="Team role, e.g. owner, planner, developer, tester")
    agent_create.add_argument("--team", help="Team id")
    agent_create.add_argument("--adapter", default="echo", choices=["echo", "codex", "codex-exec", "kimi", "kimi-print"])
    agent_create.add_argument("--cwd", default=".", help="Project directory used by this agent")
    agent_create.add_argument("--model")
    agent_create.add_argument("--sandbox", choices=["read-only", "workspace-write", "danger-full-access"])
    agent_create.add_argument("--approval-mode", default="fail", choices=["fail", "record", "bypass"])
    agent_create.add_argument("--codex-bin", default="codex")
    agent_create.add_argument("--kimi-bin", default="kimi")
    agent_create.add_argument("--resume-policy", default="latest", choices=["latest", "new", "manual"])
    agent_create.add_argument("--replace", action="store_true")
    agent_list = agent_sub.add_parser("list", help="List project agents")
    agent_list.add_argument("--project")
    agent_list.add_argument("--team")
    agent_list.add_argument("--role")
    agent_show = agent_sub.add_parser("show", help="Show one agent as JSON")
    agent_show.add_argument("agent")

    projects = sub.add_parser("projects", help="Manage projects")
    project_sub = projects.add_subparsers(dest="projects_command", required=True)
    project_create = project_sub.add_parser("create", help="Create or replace one project")
    project_create.add_argument("project_id")
    project_create.add_argument("--title", help="Human-readable project name")
    project_create.add_argument("--cwd", default=".", help="Project directory")
    project_create.add_argument("--team", help="Team id; defaults to project id")
    project_create.add_argument("--default-agent", default="owner")
    project_create.add_argument("--status", default="active")
    project_create.add_argument("--replace", action="store_true")
    project_list = project_sub.add_parser("list", help="List projects")
    project_list.add_argument("--team")
    project_list.add_argument("--status")
    project_show = project_sub.add_parser("show", help="Show one project as JSON")
    project_show.add_argument("project")

    tasks = sub.add_parser("tasks", help="Manage task board")
    task_sub = tasks.add_subparsers(dest="tasks_command", required=True)
    task_create = task_sub.add_parser("create", help="Create a task")
    task_create.add_argument("title")
    task_create.add_argument("--description", default="")
    task_create.add_argument("--project")
    task_create.add_argument("--agent")
    task_create.add_argument("--team")
    task_create.add_argument("--priority", default="normal", choices=sorted(TASK_PRIORITIES))
    task_create.add_argument("--status", default="todo", choices=sorted(TASK_STATUSES))
    task_list = task_sub.add_parser("list", help="List tasks")
    task_list.add_argument("--project")
    task_list.add_argument("--agent")
    task_list.add_argument("--status", choices=sorted(TASK_STATUSES))
    task_show = task_sub.add_parser("show", help="Show one task as JSON")
    task_show.add_argument("task")
    task_note = task_sub.add_parser("note", help="Append a task note")
    task_note.add_argument("task")
    task_note.add_argument("note")
    task_start = task_sub.add_parser("start", help="Mark a task as doing")
    task_start.add_argument("task")
    task_done = task_sub.add_parser("done", help="Mark a task as done")
    task_done.add_argument("task")
    task_done.add_argument("note", nargs="?")
    task_review = task_sub.add_parser("review", help="Mark a task as ready for review")
    task_review.add_argument("task")
    task_review.add_argument("note", nargs="?")
    task_block = task_sub.add_parser("block", help="Mark a task as blocked")
    task_block.add_argument("task")
    task_block.add_argument("reason", nargs="?")
    task_status = task_sub.add_parser("status", help="Set a task status")
    task_status.add_argument("task")
    task_status.add_argument("status", choices=sorted(TASK_STATUSES))
    task_status.add_argument("note", nargs="?")

    return parser


def resolve_workspace(args: argparse.Namespace, cwd: str | Path | None = None) -> Workspace:
    if args.workspace:
        return Workspace(Path(args.workspace).expanduser().resolve())
    return Workspace.from_cwd(cwd)


async def _run_prompt(args: argparse.Namespace, workspace: Workspace) -> int:
    session_registry = SessionRegistry(workspace)
    agent_registry = AgentRegistry(workspace)
    project_registry = ProjectRegistry(workspace)
    task_board = TaskBoard(workspace)
    args = argparse.Namespace(**vars(args))

    task = task_board.resolve(args.task) if args.task else None
    if args.task and task is None:
        print(f"task not found: {args.task}", file=sys.stderr)
        return 2
    if task is not None:
        _apply_task_defaults(args, task)

    project = project_registry.resolve(args.project) if args.project else None
    if args.project and project is None:
        print(f"project not found: {args.project}", file=sys.stderr)
        return 2
    if project is not None:
        _apply_project_defaults(args, project)

    args.agent = args.agent or "default"
    saved_agent = agent_registry.resolve(args.agent) if args.agent else None
    if saved_agent is not None:
        _apply_agent_defaults(args, saved_agent, session_registry)

    session = session_registry.resolve(args.session) if args.session else None
    if args.session and session is None:
        print(f"session not found: {args.session}", file=sys.stderr)
        return 2

    session_id = None
    if session is not None:
        args.agent = session.agent_id
        args.adapter = args.adapter or session.adapter
        args.cwd = args.cwd or session.project_dir
        if args.adapter in {"codex", "codex-exec", "kimi", "kimi-print"} and not args.resume and not args.resume_last:
            if not session.provider_session_id:
                print(
                    f"session has no provider session id; pass --resume explicitly: {session.session_id}",
                    file=sys.stderr,
                )
                return 2
            args.resume = session.provider_session_id
        session_id = session.session_id

    args.adapter = args.adapter or "echo"
    args.codex_bin = args.codex_bin or "codex"
    args.kimi_bin = args.kimi_bin or "kimi"
    args.approval_mode = args.approval_mode or "fail"
    project_dir = Path(args.cwd or ".").expanduser().resolve()
    adapter = _build_adapter(args)
    if task is not None:
        task_board.set_status(task.task_id, "doing")
    runtime = AgentRuntime(
        workspace,
        adapter,
        agent_id=args.agent,
        project_dir=project_dir,
        session_registry=session_registry,
    )
    result = await runtime.run_prompt(args.prompt, session_id=session_id, title=args.title)
    if task is not None:
        refreshed = task_board.resolve(task.task_id)
        if refreshed is not None and not refreshed.session_id:
            task_board.attach_session(refreshed.task_id, result.session_id)
        task_board.add_note(
            task.task_id,
            f"Ran prompt with agent {args.agent}; session_id: {result.session_id}",
            kind="run",
        )
    print(result.final_text)
    print(f"session_id: {result.session_id}")
    return 0


def _build_adapter(args: argparse.Namespace) -> AgentAdapter:
    adapter_name = args.adapter or "echo"
    if adapter_name == "echo":
        return EchoAdapter()
    if adapter_name in {"codex", "codex-exec"}:
        return CodexExecAdapter(
            codex_bin=args.codex_bin,
            cwd=Path(args.cwd or ".").expanduser().resolve(),
            resume=args.resume,
            resume_last=args.resume_last,
            model=args.model,
            sandbox=args.sandbox,
            approval_mode=ApprovalMode.parse(args.approval_mode),
            skip_git_repo_check=not args.no_skip_git_check,
            extra_args=tuple(args.extra_arg or ()),
        )
    if adapter_name in {"kimi", "kimi-print"}:
        return KimiPrintAdapter(
            kimi_bin=args.kimi_bin,
            cwd=Path(args.cwd or ".").expanduser().resolve(),
            resume=args.resume,
            resume_last=args.resume_last,
            model=args.model,
            approval_mode=ApprovalMode.parse(args.approval_mode),
            extra_args=tuple(args.extra_arg or ()),
        )
    raise ValueError(f"unsupported adapter: {adapter_name}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(_normalize_argv(argv))

    if args.command == "init":
        workspace = resolve_workspace(args, cwd=args.path)
        workspace.ensure()
        print(f"initialized: {workspace.root}")
        return 0

    workspace = resolve_workspace(args)

    if args.command == "doctor":
        workspace.ensure()
        for key, value in workspace.doctor().items():
            print(f"{key}: {value}")
        return 0

    if args.command == "run":
        workspace.ensure()
        return asyncio.run(_run_prompt(args, workspace))

    if args.command == "memory":
        workspace.ensure()
        store = MarkdownMemoryStore(workspace)
        if args.memory_command == "add":
            entry = store.add(args.title, args.content, scope=args.scope, owner=args.owner)
            print(f"added: {entry.path}")
            return 0
        if args.memory_command == "list":
            paths = store.list(scope=args.scope, owner=args.owner)
            for path in paths:
                print(path)
            return 0

    if args.command == "events":
        workspace.ensure()
        for event in EventLog(workspace).tail(args.tail):
            print(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True))
        return 0

    if args.command == "sessions":
        workspace.ensure()
        registry = SessionRegistry(workspace)
        if args.sessions_command == "list":
            _print_sessions(registry.list(agent_id=args.agent))
            return 0
        if args.sessions_command == "show":
            record = registry.resolve(args.session)
            if record is None:
                print(f"session not found: {args.session}", file=sys.stderr)
                return 2
            print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.sessions_command == "rename":
            record = registry.rename(args.session, args.title)
            if record is None:
                print(f"session not found: {args.session}", file=sys.stderr)
                return 2
            print(f"renamed: {record.title} ({record.session_id})")
            return 0

    if args.command == "agents":
        workspace.ensure()
        registry = AgentRegistry(workspace)
        if args.agents_command == "create":
            project = ProjectRegistry(workspace).resolve(args.project) if args.project else None
            project_id = project.project_id if project is not None else (args.project or "")
            team_id = args.team
            project_dir = args.cwd
            if project is not None:
                team_id = args.team or project.team_id
                project_dir = args.cwd if args.cwd != "." else project.project_dir
            try:
                record = registry.upsert(
                    agent_id=args.agent_id,
                    title=args.title,
                    project_id=project_id,
                    role=args.role,
                    team_id=team_id,
                    adapter=args.adapter,
                    project_dir=project_dir,
                    model=args.model or "",
                    sandbox=args.sandbox or "",
                    approval_mode=args.approval_mode,
                    codex_bin=args.codex_bin,
                    kimi_bin=args.kimi_bin,
                    resume_policy=args.resume_policy,
                    replace=args.replace,
                )
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            print(f"agent: {record.title} ({record.agent_id})")
            return 0
        if args.agents_command == "list":
            _print_agents(registry.list(project_id=args.project, team_id=args.team, role=args.role))
            return 0
        if args.agents_command == "show":
            record = registry.resolve(args.agent)
            if record is None:
                print(f"agent not found: {args.agent}", file=sys.stderr)
                return 2
            print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
            return 0

    if args.command == "projects":
        workspace.ensure()
        registry = ProjectRegistry(workspace)
        if args.projects_command == "create":
            try:
                record = registry.upsert(
                    project_id=args.project_id,
                    title=args.title,
                    project_dir=args.cwd,
                    team_id=args.team or args.project_id,
                    default_agent_id=args.default_agent,
                    status=args.status,
                    replace=args.replace,
                )
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            print(f"project: {record.title} ({record.project_id})")
            return 0
        if args.projects_command == "list":
            _print_projects(registry.list(team_id=args.team, status=args.status))
            return 0
        if args.projects_command == "show":
            record = registry.resolve(args.project)
            if record is None:
                print(f"project not found: {args.project}", file=sys.stderr)
                return 2
            print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
            return 0

    if args.command == "tasks":
        workspace.ensure()
        board = TaskBoard(workspace)
        projects = ProjectRegistry(workspace)
        if args.tasks_command == "create":
            project = projects.resolve(args.project) if args.project else None
            if args.project and project is None:
                print(f"project not found: {args.project}", file=sys.stderr)
                return 2
            try:
                record = board.create(
                    title=args.title,
                    description=args.description,
                    project_id=project.project_id if project is not None else (args.project or ""),
                    agent_id=args.agent or (project.default_agent_id if project is not None else "owner"),
                    team_id=args.team or (project.team_id if project is not None else "default"),
                    priority=args.priority,
                    status=args.status,
                )
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            print(f"task: {record.title} ({record.task_id})")
            return 0
        if args.tasks_command == "list":
            _print_tasks(board.list(project_id=args.project, agent_id=args.agent, status=args.status))
            return 0
        if args.tasks_command == "show":
            record = board.resolve(args.task)
            if record is None:
                print(f"task not found: {args.task}", file=sys.stderr)
                return 2
            print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.tasks_command == "note":
            return _update_task_note(board, args.task, args.note)
        if args.tasks_command == "start":
            return _update_task_status(board, args.task, "doing")
        if args.tasks_command == "done":
            return _update_task_status(board, args.task, "done", note=args.note or "")
        if args.tasks_command == "review":
            return _update_task_status(board, args.task, "review", note=args.note or "")
        if args.tasks_command == "block":
            return _update_task_status(board, args.task, "blocked", note=args.reason or "")
        if args.tasks_command == "status":
            return _update_task_status(board, args.task, args.status, note=args.note or "")

    parser.error(f"unknown command: {args.command}")
    return 2


def _normalize_argv(argv: list[str] | None) -> list[str]:
    """Allow --workspace before or after the subcommand."""

    tokens = list(sys.argv[1:] if argv is None else argv)
    workspace: str | None = None
    normalized: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--workspace":
            if index + 1 >= len(tokens):
                normalized.append(token)
                index += 1
                continue
            workspace = tokens[index + 1]
            index += 2
            continue
        if token.startswith("--workspace="):
            workspace = token.split("=", 1)[1]
            index += 1
            continue
        normalized.append(token)
        index += 1
    if workspace:
        return ["--workspace", workspace, *normalized]
    return normalized


def _print_sessions(records: list[SessionRecord]) -> None:
    if not records:
        print("no sessions")
        return
    print("title\tsession_id\tagent\tadapter\tstatus\tupdated_at\tproject_dir")
    for record in records:
        print(
            "\t".join(
                [
                    record.title,
                    record.session_id,
                    record.agent_id,
                    record.adapter,
                    record.status,
                    _format_timestamp(record.updated_at),
                    record.project_dir,
                ]
            )
        )


def _apply_task_defaults(args: argparse.Namespace, task: TaskRecord) -> None:
    args.project = args.project or task.project_id or None
    args.agent = args.agent or task.agent_id
    args.session = args.session or task.session_id or None
    args.title = args.title or task.title


def _apply_project_defaults(args: argparse.Namespace, project: ProjectRecord) -> None:
    args.project = project.project_id
    args.cwd = args.cwd or project.project_dir
    args.agent = args.agent or project.default_agent_id


def _apply_agent_defaults(args: argparse.Namespace, agent: AgentRecord, sessions: SessionRegistry) -> None:
    args.agent = agent.agent_id
    args.project = args.project or agent.project_id or None
    args.adapter = args.adapter or agent.adapter
    args.cwd = args.cwd or agent.project_dir
    args.model = args.model or agent.model or None
    args.sandbox = args.sandbox or agent.sandbox or None
    args.approval_mode = args.approval_mode or agent.approval_mode
    args.codex_bin = args.codex_bin or agent.codex_bin
    args.kimi_bin = args.kimi_bin or agent.kimi_bin
    args.title = args.title or agent.title

    if args.session or args.resume or args.resume_last:
        return
    if agent.resume_policy != "latest":
        return

    adapter_name = args.adapter or agent.adapter
    latest = sessions.latest_for_agent(
        agent.agent_id,
        adapter=adapter_name,
        require_provider_session=adapter_name in {"codex", "codex-exec", "kimi", "kimi-print"},
    )
    if latest is not None:
        args.session = latest.session_id


def _print_projects(records: list[ProjectRecord]) -> None:
    if not records:
        print("no projects")
        return
    print("title\tproject_id\tteam\tdefault_agent\tstatus\tproject_dir")
    for record in records:
        print(
            "\t".join(
                [
                    record.title,
                    record.project_id,
                    record.team_id,
                    record.default_agent_id,
                    record.status,
                    record.project_dir,
                ]
            )
        )


def _print_agents(records: list[AgentRecord]) -> None:
    if not records:
        print("no agents")
        return
    print("title\tagent_id\tproject\trole\tteam\tadapter\tresume_policy\tproject_dir")
    for record in records:
        print(
            "\t".join(
                [
                    record.title,
                    record.agent_id,
                    record.project_id or "-",
                    record.role,
                    record.team_id,
                    record.adapter,
                    record.resume_policy,
                    record.project_dir,
                ]
            )
        )


def _print_tasks(records: list[TaskRecord]) -> None:
    if not records:
        print("no tasks")
        return
    print("title\ttask_id\tstatus\tpriority\tproject\tagent\tsession")
    for record in records:
        print(
            "\t".join(
                [
                    record.title,
                    record.task_id,
                    record.status,
                    record.priority,
                    record.project_id or "-",
                    record.agent_id,
                    record.session_id or "-",
                ]
            )
        )


def _update_task_status(board: TaskBoard, task: str, status: str, *, note: str = "") -> int:
    try:
        record = board.set_status(task, status, note=note)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if record is None:
        print(f"task not found: {task}", file=sys.stderr)
        return 2
    print(f"task: {record.title} ({record.task_id}) status={record.status}")
    return 0


def _update_task_note(board: TaskBoard, task: str, note: str) -> int:
    record = board.add_note(task, note)
    if record is None:
        print(f"task not found: {task}", file=sys.stderr)
        return 2
    print(f"task: {record.title} ({record.task_id}) notes={len(record.notes)}")
    return 0


def _format_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    raise SystemExit(main())
