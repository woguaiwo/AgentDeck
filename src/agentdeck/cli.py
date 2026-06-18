"""AgentDeck command line interface."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from agentdeck.adapters.echo import EchoAdapter
from agentdeck.core.config import Workspace
from agentdeck.core.runtime import AgentRuntime
from agentdeck.storage.event_log import EventLog
from agentdeck.storage.memory import MarkdownMemoryStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentdeck", description="Remote control plane for AI agent teams")
    parser.add_argument("--workspace", help="Override .agentdeck workspace path")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create a project-local AgentDeck workspace")
    init.add_argument("path", nargs="?", default=".", help="Project directory")

    sub.add_parser("doctor", help="Print workspace diagnostics")

    run = sub.add_parser("run", help="Run a prompt through an adapter")
    run.add_argument("prompt")
    run.add_argument("--adapter", default="echo", choices=["echo"])
    run.add_argument("--agent", default="default")

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

    return parser


def resolve_workspace(args: argparse.Namespace, cwd: str | Path | None = None) -> Workspace:
    if args.workspace:
        return Workspace(Path(args.workspace).expanduser().resolve())
    return Workspace.from_cwd(cwd)


async def _run_prompt(args: argparse.Namespace, workspace: Workspace) -> int:
    adapter = EchoAdapter()
    runtime = AgentRuntime(workspace, adapter, agent_id=args.agent)
    result = await runtime.run_prompt(args.prompt)
    print(result.final_text)
    print(f"session_id: {result.session_id}")
    return 0


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


if __name__ == "__main__":
    raise SystemExit(main())
