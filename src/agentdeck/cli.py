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
from agentdeck.adapters.base import AgentAdapter
from agentdeck.core.approvals import ApprovalMode
from agentdeck.core.config import Workspace
from agentdeck.core.runtime import AgentRuntime
from agentdeck.storage.event_log import EventLog
from agentdeck.storage.memory import MarkdownMemoryStore
from agentdeck.storage.sessions import SessionRecord, SessionRegistry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentdeck", description="Remote control plane for AI agent teams")
    parser.add_argument("--workspace", help="Override .agentdeck workspace path")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create a project-local AgentDeck workspace")
    init.add_argument("path", nargs="?", default=".", help="Project directory")

    sub.add_parser("doctor", help="Print workspace diagnostics")

    run = sub.add_parser("run", help="Run a prompt through an adapter")
    run.add_argument("prompt")
    run.add_argument("--adapter", choices=["echo", "codex", "codex-exec"])
    run.add_argument("--agent", default="default")
    run.add_argument("--session", help="Resume an AgentDeck session id, agent id, or provider session id")
    run.add_argument("--title", help="Human-readable title for a new or resumed AgentDeck session")
    run.add_argument("--cwd", help="Project directory used by the wrapped agent")
    run.add_argument("--codex-bin", default="codex", help="Codex executable path")
    run.add_argument("--resume", help="Resume a Codex session id or thread name")
    run.add_argument("--resume-last", action="store_true", help="Resume the most recent Codex session")
    run.add_argument("--model", help="Model override for adapters that support it")
    run.add_argument("--sandbox", choices=["read-only", "workspace-write", "danger-full-access"])
    run.add_argument(
        "--approval-mode",
        default="fail",
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

    return parser


def resolve_workspace(args: argparse.Namespace, cwd: str | Path | None = None) -> Workspace:
    if args.workspace:
        return Workspace(Path(args.workspace).expanduser().resolve())
    return Workspace.from_cwd(cwd)


async def _run_prompt(args: argparse.Namespace, workspace: Workspace) -> int:
    registry = SessionRegistry(workspace)
    session = registry.resolve(args.session) if args.session else None
    if args.session and session is None:
        print(f"session not found: {args.session}", file=sys.stderr)
        return 2

    args = argparse.Namespace(**vars(args))
    session_id = None
    if session is not None:
        args.agent = session.agent_id
        args.adapter = args.adapter or session.adapter
        args.cwd = args.cwd or session.project_dir
        if args.adapter in {"codex", "codex-exec"} and not args.resume and not args.resume_last:
            if not session.provider_session_id:
                print(
                    f"session has no provider session id; pass --resume explicitly: {session.session_id}",
                    file=sys.stderr,
                )
                return 2
            args.resume = session.provider_session_id
        session_id = session.session_id

    args.adapter = args.adapter or "echo"
    project_dir = Path(args.cwd or ".").expanduser().resolve()
    adapter = _build_adapter(args)
    runtime = AgentRuntime(workspace, adapter, agent_id=args.agent, project_dir=project_dir, session_registry=registry)
    result = await runtime.run_prompt(args.prompt, session_id=session_id, title=args.title)
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


def _format_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    raise SystemExit(main())
