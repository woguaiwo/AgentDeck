"""AgentDeck command line interface."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from agentdeck.core.config import DEFAULT_PROJECT_LOCAL_CONFIG, Workspace, find_project_local_config, project_local_config_path
from agentdeck.core.error_daemon import ErrorHandlingDaemon
from agentdeck.core.experience_organizer import ExperienceOrganizer, ExperienceOrganizerResult
from agentdeck.core.run_service import RunConfigurationError, RunRequest, build_agentdeck_context, run_agent_prompt
from agentdeck.interfaces.telegram import TelegramBotApi, TelegramMultiServer, TelegramServer, config_from_env
from agentdeck.storage.approvals import APPROVAL_STATUSES, ApprovalRecord, ApprovalRegistry
from agentdeck.storage.clones import (
    CloneCapsule,
    CloneStore,
    create_ai_clone_capsule,
    create_rules_clone_capsule,
    spawn_worker_from_clone,
)
from agentdeck.storage.event_log import EventLog
from agentdeck.storage.errors import ErrorIncidentStore
from agentdeck.storage.experience import (
    COLLECTION_KINDS,
    COLLECTION_STATUSES,
    EDGE_RELATIONS,
    EVENT_LEVELS,
    EVENT_STATUSES,
    ExperienceCollection,
    ExperienceEdge,
    ExperienceEvent,
    ExperienceStore,
)
from agentdeck.storage.agents import ASSISTANT_AGENT_ID, DEFAULT_ASSISTANT_TEMPLATE, AgentRecord, AgentRegistry
from agentdeck.storage.directories import DirectoryRecord, DirectoryRegistry
from agentdeck.storage.focus import FOCUS_STATUSES, FocusRecord, FocusRegistry
from agentdeck.storage.jobs import JobRecord, JobRegistry
from agentdeck.storage.memory import MarkdownMemoryStore
from agentdeck.storage.plans import PlanRecord, PlanRegistry, plan_progress
from agentdeck.storage.progress import ProgressJournal, format_handoff, format_review
from agentdeck.storage.provider_cleanup import ProviderDeleteResult, delete_provider_session
from agentdeck.storage.provider_sessions import ProviderSessionCandidate, scan_codex_index_sessions, scan_provider_sessions
from agentdeck.storage.projects import ProjectRecord, ProjectRegistry
from agentdeck.storage.project_state import ProjectStateStore
from agentdeck.storage.session_state import SessionStateStore
from agentdeck.storage.sessions import SessionRecord, SessionRegistry
from agentdeck.storage.tasks import TASK_PRIORITIES, TASK_STATUSES, TaskBoard, TaskRecord
from agentdeck.storage.telegram_bots import (
    TelegramBotRegistry,
    assistant_agent_id_for_bot,
    current_server_id,
    redacted_token,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentdeck", description="Remote control plane for AI agent teams")
    parser.add_argument("--workspace", help="Override the AgentDeck platform workspace path")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create or verify the AgentDeck platform workspace")
    init.add_argument("path", nargs="?", default=".", help="Project directory used only with --project-config")
    init.add_argument(
        "--project-config",
        action="store_true",
        help="Also create an optional project-local .agentdeck.toml integration config",
    )

    sub.add_parser("doctor", help="Print workspace diagnostics")

    run = sub.add_parser("run", help="Run a prompt through an adapter")
    run.add_argument("prompt")
    run.add_argument("--adapter", choices=["echo", "codex", "codex-exec", "kimi", "kimi-print"])
    run.add_argument("--project", help="Project id or title")
    run.add_argument("--task", help="Task id or title")
    run.add_argument("--focus", help="Focus id or title")
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

    assistant = sub.add_parser("assistant", help="Manage the default AgentDeck assistant")
    assistant_sub = assistant.add_subparsers(dest="assistant_command", required=True)
    assistant_setup = assistant_sub.add_parser("setup", help="Create or replace the default assistant agent")
    assistant_setup.add_argument("--agent", default=ASSISTANT_AGENT_ID)
    assistant_setup.add_argument("--title", default="AgentDeck Assistant")
    assistant_setup.add_argument("--adapter", default="echo", choices=["echo", "codex", "codex-exec", "kimi", "kimi-print"])
    assistant_setup.add_argument("--cwd", default=".", help="Working directory for the assistant")
    assistant_setup.add_argument("--model")
    assistant_setup.add_argument("--approval-mode", default="fail", choices=["fail", "record", "bypass"])
    assistant_setup.add_argument("--replace", action="store_true")
    assistant_show = assistant_sub.add_parser("show", help="Show the default assistant agent")
    assistant_show.add_argument("--agent", default=ASSISTANT_AGENT_ID)
    assistant_setup_bots = assistant_sub.add_parser("setup-bots", help="Create one assistant for each saved bot on this server")
    assistant_setup_bots.add_argument("--server", default=current_server_id(), help="Server id to assign. Defaults to this host.")
    assistant_setup_bots.add_argument("--all-servers", action="store_true", help="Assign assistants for all saved bots")
    assistant_setup_bots.add_argument("--adapter", default="echo", choices=["echo", "codex", "codex-exec", "kimi", "kimi-print"])
    assistant_setup_bots.add_argument("--cwd", default=".", help="Working directory for bot assistants")
    assistant_setup_bots.add_argument("--model")
    assistant_setup_bots.add_argument("--approval-mode", default="fail", choices=["fail", "record", "bypass"])
    assistant_setup_bots.add_argument("--replace", action="store_true")
    assistant_refresh = assistant_sub.add_parser("refresh", help="Refresh saved assistant routing templates")
    assistant_refresh.add_argument("--agent", action="append", default=[], help="Assistant agent id to refresh. Repeatable.")
    assistant_refresh.add_argument("--server", default=current_server_id(), help="Server id for bot assistants. Defaults to this host.")
    assistant_refresh.add_argument("--all-servers", action="store_true", help="Refresh bot assistants for all saved bots")

    memory = sub.add_parser("memory", help="Manage markdown memory")
    mem_sub = memory.add_subparsers(dest="memory_command", required=True)
    mem_add = mem_sub.add_parser("add", help="Add a memory entry")
    mem_add.add_argument("title")
    mem_add.add_argument("content")
    mem_add.add_argument("--scope", default="project", choices=["user", "project", "team", "agent", "task"])
    mem_add.add_argument("--owner")
    mem_add.add_argument("--pin", action="store_true", help="Prioritize this memory in retrieval")
    mem_list = mem_sub.add_parser("list", help="List memory entries")
    mem_list.add_argument("--scope", default="project", choices=["user", "project", "team", "agent", "task"])
    mem_list.add_argument("--owner")
    mem_disable = mem_sub.add_parser("disable", help="Soft-disable a memory entry")
    mem_disable.add_argument("memory")
    mem_disable.add_argument("--scope", choices=["user", "project", "team", "agent", "task"])
    mem_disable.add_argument("--owner")
    mem_enable = mem_sub.add_parser("enable", help="Re-enable a soft-disabled memory entry")
    mem_enable.add_argument("memory")
    mem_enable.add_argument("--scope", choices=["user", "project", "team", "agent", "task"])
    mem_enable.add_argument("--owner")
    mem_compact_task = mem_sub.add_parser("compact-task", help="Create a durable memory snapshot from one task context")
    mem_compact_task.add_argument("task")
    mem_compact_task.add_argument("--scope", default="project", choices=["project", "team", "agent", "task"])
    mem_compact_task.add_argument("--owner")
    mem_compact_task.add_argument("--title")
    mem_compact_task.add_argument("--max-chars", type=int, default=6000)
    mem_compact_task.add_argument("--pin", action="store_true", help="Prioritize this snapshot in retrieval")
    mem_compact_focus = mem_sub.add_parser("compact-focus", help="Create a durable memory snapshot from one focus context")
    mem_compact_focus.add_argument("focus")
    mem_compact_focus.add_argument("--session", help="Session id override; defaults to the focus session")
    mem_compact_focus.add_argument("--title")
    mem_compact_focus.add_argument("--max-chars", type=int, default=6000)
    mem_compact_focus.add_argument("--pin", action="store_true", help="Prioritize this snapshot in retrieval")

    experience = sub.add_parser("experience", help="Manage experience collections and event graphs")
    exp_sub = experience.add_subparsers(dest="experience_command", required=True)
    exp_collections = exp_sub.add_parser("collections", help="List experience collections")
    exp_collections.add_argument("--project")
    exp_collections.add_argument("--worker")
    exp_collections.add_argument("--agent")
    exp_collections.add_argument("--focus")
    exp_collections.add_argument("--kind", choices=sorted(COLLECTION_KINDS))
    exp_collections.add_argument("--status", choices=sorted(COLLECTION_STATUSES))
    exp_create_collection = exp_sub.add_parser("create-collection", help="Create an experience collection")
    exp_create_collection.add_argument("title")
    exp_create_collection.add_argument("--kind", required=True, choices=sorted(COLLECTION_KINDS))
    exp_create_collection.add_argument("--purpose", default="")
    exp_create_collection.add_argument("--project")
    exp_create_collection.add_argument("--worker")
    exp_create_collection.add_argument("--agent")
    exp_create_collection.add_argument("--focus")
    exp_create_collection.add_argument("--directory-id", default="")
    exp_create_collection.add_argument("--status", default="active", choices=sorted(COLLECTION_STATUSES))
    exp_show_collection = exp_sub.add_parser("show-collection", help="Show one experience collection")
    exp_show_collection.add_argument("collection")
    exp_record = exp_sub.add_parser("record", help="Record one experience event")
    exp_record.add_argument("collection")
    exp_record.add_argument("--purpose", required=True)
    exp_record.add_argument("--context", default="")
    exp_record.add_argument("--action", action="append", default=[])
    exp_record.add_argument("--result", default="")
    exp_record.add_argument("--analysis", default="")
    exp_record.add_argument("--decision", action="append", default=[])
    exp_record.add_argument("--artifact", action="append", default=[], help="Artifact path or kind:path")
    exp_record.add_argument("--tag", action="append", default=[])
    exp_record.add_argument("--parent")
    exp_record.add_argument("--sequence", type=int, default=0)
    exp_record.add_argument("--level", default="micro", choices=sorted(EVENT_LEVELS))
    exp_record.add_argument("--kind", default="event")
    exp_record.add_argument("--status", default="done", choices=sorted(EVENT_STATUSES))
    exp_record.add_argument("--focus")
    exp_record.add_argument("--confidence", default="")
    exp_events = exp_sub.add_parser("events", help="List/search experience events")
    exp_events.add_argument("--collection")
    exp_events.add_argument("--project")
    exp_events.add_argument("--worker")
    exp_events.add_argument("--agent")
    exp_events.add_argument("--focus")
    exp_events.add_argument("--kind")
    exp_events.add_argument("--query")
    exp_events.add_argument("--limit", type=int, default=20)
    exp_show = exp_sub.add_parser("show", help="Show one experience event")
    exp_show.add_argument("event")
    exp_link = exp_sub.add_parser("link", help="Link two experience events")
    exp_link.add_argument("from_event")
    exp_link.add_argument("to_event")
    exp_link.add_argument("--relation", required=True, choices=sorted(EDGE_RELATIONS))
    exp_link.add_argument("--reason", default="")
    exp_edges = exp_sub.add_parser("edges", help="List experience graph edges")
    exp_edges.add_argument("--event")
    exp_edges.add_argument("--relation", choices=sorted(EDGE_RELATIONS))
    exp_organize = exp_sub.add_parser("organize", help="Extract structured progress into experience events once")
    exp_organize.add_argument("--limit", type=int, default=50)
    exp_organize.add_argument("--collection", default="", help="Existing collection id/title to write into")
    exp_organize.add_argument("--kind", choices=sorted(COLLECTION_KINDS), default="", help="Kind for auto-created collections")
    exp_organize.add_argument("--dry-run", action="store_true")
    exp_serve = exp_sub.add_parser("serve", help="Run the experience organizer daemon in the foreground")
    exp_serve.add_argument("--once", action="store_true", help="Process pending progress once and exit")
    exp_serve.add_argument("--poll-interval", type=float, default=30.0)
    exp_serve.add_argument("--limit", type=int, default=50)
    exp_serve.add_argument("--collection", default="", help="Existing collection id/title to write into")
    exp_serve.add_argument("--kind", choices=sorted(COLLECTION_KINDS), default="", help="Kind for auto-created collections")
    exp_serve.add_argument("--dry-run", action="store_true")
    exp_start = exp_sub.add_parser("start", help="Start the experience organizer daemon as a detached process")
    exp_start.add_argument("--poll-interval", type=float, default=30.0)
    exp_start.add_argument("--limit", type=int, default=50)
    exp_start.add_argument("--collection", default="", help="Existing collection id/title to write into")
    exp_start.add_argument("--kind", choices=sorted(COLLECTION_KINDS), default="", help="Kind for auto-created collections")
    exp_start.add_argument("--dry-run", action="store_true")
    exp_stop = exp_sub.add_parser("stop", help="Stop detached experience organizer daemon")
    exp_stop.add_argument("--force", action="store_true", help="Send SIGKILL if SIGTERM does not stop the daemon")
    exp_restart = exp_sub.add_parser("restart", help="Restart detached experience organizer daemon")
    exp_restart.add_argument("--poll-interval", type=float, default=30.0)
    exp_restart.add_argument("--limit", type=int, default=50)
    exp_restart.add_argument("--collection", default="", help="Existing collection id/title to write into")
    exp_restart.add_argument("--kind", choices=sorted(COLLECTION_KINDS), default="", help="Kind for auto-created collections")
    exp_restart.add_argument("--dry-run", action="store_true")
    exp_restart.add_argument("--force", action="store_true", help="Send SIGKILL if SIGTERM does not stop the old daemon")
    exp_sub.add_parser("status", help="Show detached experience organizer daemon status")

    events = sub.add_parser("events", help="Inspect event log")
    events.add_argument("--tail", type=int, default=20)

    errors = sub.add_parser("errors", help="Handle backend error incidents")
    errors_sub = errors.add_subparsers(dest="errors_command", required=True)
    errors_serve = errors_sub.add_parser("serve", help="Run error-handling daemon in the foreground")
    errors_serve.add_argument("--once", action="store_true", help="Process pending incidents once and exit")
    errors_serve.add_argument("--poll-interval", type=float, default=5.0)
    errors_start = errors_sub.add_parser("start", help="Start error-handling daemon as a detached process")
    errors_start.add_argument("--poll-interval", type=float, default=5.0)
    errors_stop = errors_sub.add_parser("stop", help="Stop detached error-handling daemon")
    errors_stop.add_argument("--force", action="store_true", help="Send SIGKILL if SIGTERM does not stop the daemon")
    errors_restart = errors_sub.add_parser("restart", help="Restart detached error-handling daemon")
    errors_restart.add_argument("--poll-interval", type=float, default=5.0)
    errors_restart.add_argument("--force", action="store_true", help="Send SIGKILL if SIGTERM does not stop the old daemon")
    errors_sub.add_parser("status", help="Show detached error-handling daemon status")
    errors_list = errors_sub.add_parser("list", help="List error incidents")
    errors_list.add_argument("--status", choices=["pending", "resolved"])
    errors_list.add_argument("--limit", type=int, default=20)
    errors_decisions = errors_sub.add_parser("decisions", help="List recent error handler decisions")
    errors_decisions.add_argument("--limit", type=int, default=20)
    errors_unknowns = errors_sub.add_parser("unknowns", help="List unknown error fingerprints")
    errors_unknowns.add_argument("--limit", type=int, default=20)

    approvals = sub.add_parser("approvals", help="Manage backend approval requests")
    approval_sub = approvals.add_subparsers(dest="approvals_command", required=True)
    approval_list = approval_sub.add_parser("list", help="List approval requests")
    approval_list.add_argument("--status", choices=sorted(APPROVAL_STATUSES))
    approval_list.add_argument("--project")
    approval_list.add_argument("--task")
    approval_list.add_argument("--agent")
    approval_show = approval_sub.add_parser("show", help="Show one approval as JSON")
    approval_show.add_argument("approval")
    approval_approve = approval_sub.add_parser("approve", help="Mark an approval as approved")
    approval_approve.add_argument("approval")
    approval_approve.add_argument("note", nargs="?")
    approval_reject = approval_sub.add_parser("reject", help="Mark an approval as rejected")
    approval_reject.add_argument("approval")
    approval_reject.add_argument("note", nargs="?")

    plans = sub.add_parser("plans", help="Manage readable plan drafts and executable plan steps")
    plans_sub = plans.add_subparsers(dest="plans_command", required=True)
    plans_list = plans_sub.add_parser("list", help="List plans")
    plans_list.add_argument("--project")
    plans_list.add_argument("--focus")
    plans_list.add_argument("--status")
    plans_new = plans_sub.add_parser("new", help="Create a readable plan draft")
    plans_new.add_argument("title")
    plans_new.add_argument("--draft", default="")
    plans_new.add_argument("--draft-file")
    plans_new.add_argument("--project")
    plans_new.add_argument("--focus")
    plans_new.add_argument("--session")
    plans_new.add_argument("--agent")
    plans_new.add_argument("--cwd")
    plans_show = plans_sub.add_parser("show", help="Show one plan as JSON")
    plans_show.add_argument("plan")
    plans_draft = plans_sub.add_parser("draft", help="Print the user-readable plan draft")
    plans_draft.add_argument("plan")
    plans_set_draft = plans_sub.add_parser("set-draft", help="Replace the user-readable plan draft")
    plans_set_draft.add_argument("plan")
    plans_set_draft.add_argument("--text", default="")
    plans_set_draft.add_argument("--file")
    plans_note = plans_sub.add_parser("note", help="Append a discussion note to a plan")
    plans_note.add_argument("plan")
    plans_note.add_argument("note")
    plans_compile = plans_sub.add_parser("compile", help="Compile the readable draft into executable steps")
    plans_compile.add_argument("plan")
    plans_status = plans_sub.add_parser("status", help="Show plan step status")
    plans_status.add_argument("plan")
    plans_step = plans_sub.add_parser("step", help="Update one compiled plan step")
    plans_step.add_argument("plan")
    plans_step.add_argument("step")
    plans_step.add_argument("--status", required=True)
    plans_step.add_argument("--report", default="")
    plans_step.add_argument("--result", default="")
    plans_step.add_argument("--decision", default="")
    plans_step.add_argument("--artifact", action="append", default=[])

    sessions = sub.add_parser("sessions", help="Inspect session registry")
    sess_sub = sessions.add_subparsers(dest="sessions_command", required=True)
    sess_list = sess_sub.add_parser("list", help="List known sessions")
    sess_list.add_argument("--agent")
    sess_show = sess_sub.add_parser("show", help="Show one session as JSON")
    sess_show.add_argument("session")
    sess_state = sess_sub.add_parser("state", help="Show one session state card as JSON")
    sess_state.add_argument("session")
    sess_rename = sess_sub.add_parser("rename", help="Rename one session")
    sess_rename.add_argument("session")
    sess_rename.add_argument("title")
    sess_scan = sess_sub.add_parser("scan", help="Find provider sessions that can be imported")
    sess_scan.add_argument("--provider", choices=["codex", "kimi"])
    sess_scan.add_argument("--cwd", help="Only show sessions whose provider cwd matches this directory")
    sess_scan.add_argument("--home", help="Home directory containing .codex/.kimi")
    sess_scan.add_argument("--limit", type=int, default=20)
    sess_scan.add_argument("--json", action="store_true", help="Print raw JSON")
    sess_scan_codex = sess_sub.add_parser("scan-codex-index", help="List sessions visible to codex resume")
    sess_scan_codex.add_argument("--cwd", help="Only show indexed sessions whose rollout cwd matches this directory")
    sess_scan_codex.add_argument("--home", help="Home directory containing .codex")
    sess_scan_codex.add_argument("--limit", type=int, default=20)
    sess_scan_codex.add_argument("--json", action="store_true", help="Print raw JSON")
    sess_import = sess_sub.add_parser("import", help="Bind an existing provider session to AgentDeck")
    sess_import.add_argument("--provider", required=True, choices=["codex", "kimi"])
    sess_import.add_argument("--provider-session", required=True, help="Codex thread id or Kimi session id")
    sess_import.add_argument("--project", help="Project id or title")
    sess_import.add_argument("--task", help="Optional task id or title to attach to the imported session")
    sess_import.add_argument("--focus", help="Optional focus id or title to attach to the imported session")
    sess_import.add_argument("--agent", help="Agent id")
    sess_import.add_argument("--adapter", choices=["codex", "codex-exec", "kimi", "kimi-print"])
    sess_import.add_argument("--cwd", help="Provider working directory")
    sess_import.add_argument("--title", help="Human-readable title")
    sess_import.add_argument("--session-id", help="Explicit AgentDeck session id")
    sess_import.add_argument("--kind", help="Provider session kind override")
    sess_import_codex = sess_sub.add_parser("import-codex-index", help="Import a session visible to codex resume")
    sess_import_codex.add_argument("session", help="Codex session id or exact thread name")
    sess_import_codex.add_argument("--home", help="Home directory containing .codex")
    sess_import_codex.add_argument("--project", help="Project id or title")
    sess_import_codex.add_argument("--task", help="Optional task id or title to attach to the imported session")
    sess_import_codex.add_argument("--focus", help="Optional focus id or title to attach to the imported session")
    sess_import_codex.add_argument("--agent", help="Agent id")
    sess_import_codex.add_argument("--cwd", help="Provider working directory; required if Codex rollout has no cwd")
    sess_import_codex.add_argument("--title", help="Human-readable title")
    sess_import_codex.add_argument("--session-id", help="Explicit AgentDeck session id")
    sess_clone = sess_sub.add_parser("clone", help="Create a clone capsule from one session")
    sess_clone.add_argument("session")
    sess_clone.add_argument("--home", help="Home directory containing .codex/.kimi")
    sess_clone.add_argument("--strategy", default="rules", choices=["rules", "ai"], help="Clone generation strategy")
    sess_clone.add_argument("--recent-turns", type=int, default=12)
    sess_clone.add_argument("--max-provider-chars", type=int, default=24000)
    sess_clone.add_argument("--summarizer-adapter", default="codex-exec", choices=["echo", "codex", "codex-exec", "kimi", "kimi-print"])
    sess_clone.add_argument("--codex-bin", default="codex")
    sess_clone.add_argument("--kimi-bin", default="kimi")
    sess_clone.add_argument("--model")
    sess_clone.add_argument("--keep-debug", action="store_true", help="Keep ephemeral summarizer temp files for debugging")
    sess_clone.add_argument("--collection", action="append", default=[], help="Experience collection id/title to include. Repeatable.")
    sess_clone.add_argument("--json", action="store_true", help="Print raw capsule JSON")
    sess_delete_provider = sess_sub.add_parser("delete-provider-session", help="Delete the session's native provider session")
    sess_delete_provider.add_argument("session")
    _add_provider_delete_args(sess_delete_provider)

    workers = sub.add_parser("workers", help="Manage session-agent workers")
    worker_sub = workers.add_subparsers(dest="workers_command", required=True)
    worker_list = worker_sub.add_parser("list", help="List session-agent workers")
    worker_list.add_argument("--agent")
    worker_show = worker_sub.add_parser("show", help="Show one session-agent worker as JSON")
    worker_show.add_argument("session")
    worker_state = worker_sub.add_parser("state", help="Show one worker state card as JSON")
    worker_state.add_argument("session")
    worker_rename = worker_sub.add_parser("rename", help="Rename one session-agent worker")
    worker_rename.add_argument("session")
    worker_rename.add_argument("title")
    worker_scan = worker_sub.add_parser("scan", help="Find provider sessions that can be imported as workers")
    worker_scan.add_argument("--provider", choices=["codex", "kimi"])
    worker_scan.add_argument("--cwd", help="Only show sessions whose provider cwd matches this directory")
    worker_scan.add_argument("--home", help="Home directory containing .codex/.kimi")
    worker_scan.add_argument("--limit", type=int, default=20)
    worker_scan.add_argument("--json", action="store_true", help="Print raw JSON")
    worker_scan_codex = worker_sub.add_parser("scan-codex-index", help="List sessions visible to codex resume")
    worker_scan_codex.add_argument("--cwd", help="Only show indexed sessions whose rollout cwd matches this directory")
    worker_scan_codex.add_argument("--home", help="Home directory containing .codex")
    worker_scan_codex.add_argument("--limit", type=int, default=20)
    worker_scan_codex.add_argument("--json", action="store_true", help="Print raw JSON")
    worker_import = worker_sub.add_parser("import", help="Bind an existing provider session as a worker")
    worker_import.add_argument("--provider", required=True, choices=["codex", "kimi"])
    worker_import.add_argument("--provider-session", required=True, help="Codex thread id or Kimi session id")
    worker_import.add_argument("--project", help="Project id or title")
    worker_import.add_argument("--task", help="Optional legacy task id or title to attach to the imported worker")
    worker_import.add_argument("--focus", help="Optional focus id or title to attach to the imported worker")
    worker_import.add_argument("--agent", help="Identity id")
    worker_import.add_argument("--adapter", choices=["codex", "codex-exec", "kimi", "kimi-print"])
    worker_import.add_argument("--cwd", help="Provider working directory")
    worker_import.add_argument("--title", help="Human-readable title")
    worker_import.add_argument("--session-id", help="Explicit AgentDeck session-agent id")
    worker_import.add_argument("--kind", help="Provider session kind override")
    worker_import_codex = worker_sub.add_parser("import-codex-index", help="Import a session visible to codex resume")
    worker_import_codex.add_argument("session", help="Codex session id or exact thread name")
    worker_import_codex.add_argument("--home", help="Home directory containing .codex")
    worker_import_codex.add_argument("--project", help="Project id or title")
    worker_import_codex.add_argument("--task", help="Optional legacy task id or title to attach to the imported worker")
    worker_import_codex.add_argument("--focus", help="Optional focus id or title to attach to the imported worker")
    worker_import_codex.add_argument("--agent", help="Identity id")
    worker_import_codex.add_argument("--cwd", help="Provider working directory; required if Codex rollout has no cwd")
    worker_import_codex.add_argument("--title", help="Human-readable title")
    worker_import_codex.add_argument("--session-id", help="Explicit AgentDeck session-agent id")
    worker_clone = worker_sub.add_parser("clone", help="Create a clone capsule from one worker")
    worker_clone.add_argument("session")
    worker_clone.add_argument("--home", help="Home directory containing .codex/.kimi")
    worker_clone.add_argument("--strategy", default="rules", choices=["rules", "ai"], help="Clone generation strategy")
    worker_clone.add_argument("--recent-turns", type=int, default=12)
    worker_clone.add_argument("--max-provider-chars", type=int, default=24000)
    worker_clone.add_argument("--summarizer-adapter", default="codex-exec", choices=["echo", "codex", "codex-exec", "kimi", "kimi-print"])
    worker_clone.add_argument("--codex-bin", default="codex")
    worker_clone.add_argument("--kimi-bin", default="kimi")
    worker_clone.add_argument("--model")
    worker_clone.add_argument("--keep-debug", action="store_true", help="Keep ephemeral summarizer temp files for debugging")
    worker_clone.add_argument("--collection", action="append", default=[], help="Experience collection id/title to include. Repeatable.")
    worker_clone.add_argument("--json", action="store_true", help="Print raw capsule JSON")
    worker_delete_provider = worker_sub.add_parser("delete-provider-session", help="Delete the worker's native provider session")
    worker_delete_provider.add_argument("session")
    _add_provider_delete_args(worker_delete_provider)

    clones = sub.add_parser("clones", help="Inspect worker clone capsules")
    clones_sub = clones.add_subparsers(dest="clones_command", required=True)
    clones_sub.add_parser("list", help="List clone capsules")
    clones_show = clones_sub.add_parser("show", help="Show one clone capsule")
    clones_show.add_argument("clone")
    clones_show.add_argument("--context", action="store_true", help="Print rendered clone_context.md")
    clones_spawn = clones_sub.add_parser("spawn", help="Prepare a new worker from one clone capsule")
    clones_spawn.add_argument("clone")
    clones_spawn.add_argument("--agent", help="New worker identity id")
    clones_spawn.add_argument("--session-id", help="Explicit prepared session id")
    clones_spawn.add_argument("--title", help="Human-readable worker title")
    clones_spawn.add_argument("--project", help="Project id or title override")
    clones_spawn.add_argument("--cwd", help="Working directory override")
    clones_spawn.add_argument("--adapter", choices=["echo", "codex", "codex-exec", "kimi", "kimi-print"])
    clones_spawn.add_argument("--role", default="developer")
    clones_spawn.add_argument("--team", default="default")
    clones_spawn.add_argument("--model")
    clones_spawn.add_argument("--sandbox")
    clones_spawn.add_argument("--approval-mode", default="fail", choices=["fail", "record", "bypass"])
    clones_spawn.add_argument("--replace", action="store_true", help="Replace an existing prepared worker/agent record")
    clones_spawn.add_argument("--json", action="store_true", help="Print raw JSON")
    clones_delete_provider = clones_sub.add_parser(
        "delete-provider-session",
        help="Delete the source provider session recorded in a clone capsule",
    )
    clones_delete_provider.add_argument("clone")
    _add_provider_delete_args(clones_delete_provider)
    clones_cleanup = clones_sub.add_parser("cleanup", help="Clean stale clone temporary runs")
    clones_cleanup.add_argument("--older-than", type=float, default=86400.0, help="Minimum age in seconds")

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
    agent_template = agent_sub.add_parser("template", help="Set or clear one agent role template")
    agent_template.add_argument("agent")
    agent_template.add_argument("--prompt", action="append", default=[], help="Template guidance line. Can be repeated.")
    agent_template.add_argument("--clear", action="store_true", help="Clear custom template and use the default for the role")

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
    project_state = project_sub.add_parser("state", help="Show one project state card as JSON")
    project_state.add_argument("project")
    project_update_state = project_sub.add_parser("update-state", help="Create or update one project state card")
    project_update_state.add_argument("project")
    project_update_state.add_argument("--goal")
    project_update_state.add_argument("--phase")
    project_update_state.add_argument("--focus")
    project_update_state.add_argument("--next", dest="next_steps", action="append")
    project_update_state.add_argument("--constraint", dest="constraints", action="append")
    project_update_state.add_argument("--blocker", dest="blockers", action="append")
    project_update_state.add_argument("--artifact", dest="artifacts", action="append")
    project_update_state.add_argument("--by", dest="updated_by")
    project_decide = project_sub.add_parser("decide", help="Append one project decision")
    project_decide.add_argument("project")
    project_decide.add_argument("decision")
    project_decide.add_argument("--reason", default="")
    project_decide.add_argument("--impact", default="")
    project_decide.add_argument("--alternative", dest="alternatives", action="append", default=[])
    project_decide.add_argument("--by", dest="made_by", default="")
    project_decisions = project_sub.add_parser("decisions", help="List recent project decisions")
    project_decisions.add_argument("project")
    project_decisions.add_argument("--limit", type=int, default=10)
    project_add_dir = project_sub.add_parser("add-dir", help="Add a directory to one project")
    project_add_dir.add_argument("project")
    project_add_dir.add_argument("directory")
    project_remove_dir = project_sub.add_parser("remove-dir", help="Remove a directory from one project")
    project_remove_dir.add_argument("project")
    project_remove_dir.add_argument("directory")

    directories = sub.add_parser("directories", help="Manage directory registry")
    directory_sub = directories.add_subparsers(dest="directories_command", required=True)
    directory_list = directory_sub.add_parser("list", help="List managed directories")
    directory_list.add_argument("--project")
    directory_list.add_argument("--status")
    directory_add = directory_sub.add_parser("add", help="Add or update one managed directory")
    directory_add.add_argument("path")
    directory_add.add_argument("--project")
    directory_add.add_argument("--title", default="")
    directory_add.add_argument("--parent", default="")
    directory_add.add_argument("--role", default="workspace")
    directory_add.add_argument("--status", default="active")
    directory_show = directory_sub.add_parser("show", help="Show one directory as JSON")
    directory_show.add_argument("directory")

    focus = sub.add_parser("focus", help="Manage session-first focus records")
    focus_sub = focus.add_subparsers(dest="focus_command", required=True)
    focus_create = focus_sub.add_parser("create", help="Create a focus")
    focus_create.add_argument("title")
    focus_create.add_argument("--description", default="")
    focus_create.add_argument("--project")
    focus_create.add_argument("--agent")
    focus_create.add_argument("--cwd", help="Directory this focus runs in; defaults to agent or project directory")
    focus_create.add_argument("--session", help="Existing AgentDeck session id")
    focus_create.add_argument("--status", default="active", choices=sorted(FOCUS_STATUSES))
    focus_list = focus_sub.add_parser("list", help="List focus records")
    focus_list.add_argument("--project")
    focus_list.add_argument("--agent")
    focus_list.add_argument("--cwd")
    focus_list.add_argument("--status", choices=sorted(FOCUS_STATUSES))
    focus_show = focus_sub.add_parser("show", help="Show one focus as JSON")
    focus_show.add_argument("focus")
    focus_context = focus_sub.add_parser("context", help="Show the AgentDeck context injected for one focus")
    focus_context.add_argument("focus")
    focus_note = focus_sub.add_parser("note", help="Append a focus note")
    focus_note.add_argument("focus")
    focus_note.add_argument("note")
    focus_set = focus_sub.add_parser("set", help="Replace the focus paragraph text")
    focus_set.add_argument("focus")
    focus_set.add_argument("text")
    focus_status = focus_sub.add_parser("status", help="Set focus status")
    focus_status.add_argument("focus")
    focus_status.add_argument("status", choices=sorted(FOCUS_STATUSES))
    focus_status.add_argument("note", nargs="?")
    focus_attach = focus_sub.add_parser("attach-session", help="Attach a session to a focus")
    focus_attach.add_argument("focus")
    focus_attach.add_argument("session")
    focus_handoffs = focus_sub.add_parser("handoffs", help="List recent handoffs for one focus")
    focus_handoffs.add_argument("focus")
    focus_handoffs.add_argument("--limit", type=int, default=5)
    focus_reviews = focus_sub.add_parser("reviews", help="List recent manager reviews for one focus")
    focus_reviews.add_argument("focus")
    focus_reviews.add_argument("--limit", type=int, default=5)
    focus_handoff = focus_sub.add_parser("handoff", help="Append a structured focus handoff and update session state")
    focus_handoff.add_argument("focus")
    focus_handoff.add_argument("--summary", required=True)
    focus_handoff.add_argument("--completed", action="append", default=[])
    focus_handoff.add_argument("--verified", action="append", default=[])
    focus_handoff.add_argument("--next", dest="next_steps", action="append", default=[])
    focus_handoff.add_argument("--blocker", dest="blockers", action="append", default=[])
    focus_handoff.add_argument("--decision", dest="decisions", action="append", default=[])
    focus_handoff.add_argument("--artifact", dest="artifacts", action="append", default=[])
    focus_handoff.add_argument("--session", help="Session id; defaults to the focus session")
    focus_handoff.add_argument("--agent", help="Agent id; defaults to the focus agent")
    focus_manager_review = focus_sub.add_parser(
        "manager-review",
        help="Append a structured focus manager review and update session state",
    )
    focus_manager_review.add_argument("focus")
    focus_manager_review.add_argument("--summary", required=True)
    focus_manager_review.add_argument(
        "--status",
        choices=["noted", "approved", "changes-requested", "blocked"],
        default="noted",
    )
    focus_manager_review.add_argument("--next", dest="next_steps", action="append", default=[])
    focus_manager_review.add_argument("--blocker", dest="blockers", action="append", default=[])
    focus_manager_review.add_argument("--decision", dest="decisions", action="append", default=[])
    focus_manager_review.add_argument("--artifact", dest="artifacts", action="append", default=[])
    focus_manager_review.add_argument("--session", help="Session id; defaults to the focus session")
    focus_manager_review.add_argument("--reviewer", default="manager")

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
    task_context = task_sub.add_parser("context", help="Show the AgentDeck context injected for one task")
    task_context.add_argument("task")
    task_context.add_argument("--session", help="Session id; defaults to the task's attached session")
    task_handoffs = task_sub.add_parser("handoffs", help="List recent handoffs for one task")
    task_handoffs.add_argument("task")
    task_handoffs.add_argument("--limit", type=int, default=5)
    task_reviews = task_sub.add_parser("reviews", help="List recent manager reviews for one task")
    task_reviews.add_argument("task")
    task_reviews.add_argument("--limit", type=int, default=5)
    task_note = task_sub.add_parser("note", help="Append a task note")
    task_note.add_argument("task")
    task_note.add_argument("note")
    task_handoff = task_sub.add_parser("handoff", help="Append a structured handoff and update session state")
    task_handoff.add_argument("task")
    task_handoff.add_argument("--summary", required=True)
    task_handoff.add_argument("--completed", action="append", default=[])
    task_handoff.add_argument("--verified", action="append", default=[])
    task_handoff.add_argument("--next", dest="next_steps", action="append", default=[])
    task_handoff.add_argument("--blocker", dest="blockers", action="append", default=[])
    task_handoff.add_argument("--decision", dest="decisions", action="append", default=[])
    task_handoff.add_argument("--artifact", dest="artifacts", action="append", default=[])
    task_handoff.add_argument("--session", help="Session id; defaults to the task's attached session")
    task_handoff.add_argument("--agent", help="Agent id; defaults to the task owner")
    task_manager_review = task_sub.add_parser(
        "manager-review",
        help="Append a structured manager review and update session state",
    )
    task_manager_review.add_argument("task")
    task_manager_review.add_argument("--summary", required=True)
    task_manager_review.add_argument(
        "--status",
        choices=["noted", "approved", "changes-requested", "blocked"],
        default="noted",
    )
    task_manager_review.add_argument("--next", dest="next_steps", action="append", default=[])
    task_manager_review.add_argument("--blocker", dest="blockers", action="append", default=[])
    task_manager_review.add_argument("--decision", dest="decisions", action="append", default=[])
    task_manager_review.add_argument("--artifact", dest="artifacts", action="append", default=[])
    task_manager_review.add_argument("--session", help="Session id; defaults to the task's attached session")
    task_manager_review.add_argument("--reviewer", default="manager")
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

    telegram = sub.add_parser("telegram", help="Run Telegram remote interface")
    telegram_sub = telegram.add_subparsers(dest="telegram_command", required=True)
    telegram_serve = telegram_sub.add_parser("serve", help="Start Telegram long-polling service")
    telegram_serve.add_argument("--bot", help="Only serve one saved bot id from telegram bots registry")
    telegram_serve.add_argument("--assistant-agent", help="Assistant agent used before a focus or session is selected")
    telegram_serve.add_argument("--token", help="Telegram bot token; defaults to AGENTDECK_TELEGRAM_TOKEN")
    telegram_serve.add_argument(
        "--allowed-chat-id",
        action="append",
        default=[],
        help="Allowed Telegram chat id. Can be repeated. Defaults to AGENTDECK_TELEGRAM_ALLOWED_CHATS.",
    )
    telegram_serve.add_argument("--poll-timeout", type=int, default=30)
    telegram_serve.add_argument("--once", action="store_true", help="Process one polling response and exit")
    telegram_start = telegram_sub.add_parser("start", help="Start Telegram service as a detached background process")
    telegram_start.add_argument("--bot", help="Only serve one saved bot id from telegram bots registry")
    telegram_start.add_argument("--assistant-agent", help="Assistant agent used before a focus or session is selected")
    telegram_start.add_argument("--token", help="Telegram bot token; defaults to AGENTDECK_TELEGRAM_TOKEN")
    telegram_start.add_argument(
        "--allowed-chat-id",
        action="append",
        default=[],
        help="Allowed Telegram chat id. Can be repeated. Defaults to AGENTDECK_TELEGRAM_ALLOWED_CHATS.",
    )
    telegram_start.add_argument("--poll-timeout", type=int, default=30)
    telegram_stop = telegram_sub.add_parser("stop", help="Stop detached Telegram bot")
    telegram_stop.add_argument("--force", action="store_true", help="Send SIGKILL if SIGTERM does not stop the bot")
    telegram_restart = telegram_sub.add_parser("restart", help="Restart detached Telegram bot service")
    telegram_restart.add_argument("--bot", help="Only serve one saved bot id from telegram bots registry")
    telegram_restart.add_argument("--assistant-agent", help="Assistant agent used before a focus or session is selected")
    telegram_restart.add_argument("--token", help="Telegram bot token; defaults to AGENTDECK_TELEGRAM_TOKEN")
    telegram_restart.add_argument(
        "--allowed-chat-id",
        action="append",
        default=[],
        help="Allowed Telegram chat id. Can be repeated. Defaults to AGENTDECK_TELEGRAM_ALLOWED_CHATS.",
    )
    telegram_restart.add_argument("--poll-timeout", type=int, default=30)
    telegram_restart.add_argument("--force", action="store_true", help="Send SIGKILL if SIGTERM does not stop the old bot")
    telegram_restart.add_argument("--force-jobs", action="store_true", help="Restart even when Telegram jobs are queued or running")
    telegram_restart.add_argument(
        "--wait-idle",
        nargs="?",
        const=300.0,
        default=0.0,
        type=float,
        metavar="SECONDS",
        help="Wait up to SECONDS for a no-active-job gap before restarting. Defaults to 300 seconds when no value is given.",
    )
    telegram_restart.add_argument(
        "--idle-poll-interval",
        type=float,
        default=0.2,
        help="Polling interval in seconds for --wait-idle.",
    )
    telegram_sub.add_parser("status", help="Show detached Telegram bot status")
    telegram_bots = telegram_sub.add_parser("bots", help="Manage saved Telegram bot configs")
    telegram_bots_sub = telegram_bots.add_subparsers(dest="telegram_bots_command", required=True)
    telegram_bots_add = telegram_bots_sub.add_parser("add", help="Add or replace one saved Telegram bot")
    telegram_bots_add.add_argument("bot_id")
    telegram_bots_add.add_argument("--title", default="")
    telegram_bots_add.add_argument("--token", required=True)
    telegram_bots_add.add_argument("--allowed-chat-id", action="append", default=[])
    telegram_bots_add.add_argument("--assistant-agent", default="")
    telegram_bots_add.add_argument("--server", default=current_server_id())
    telegram_bots_import = telegram_bots_sub.add_parser("import", help="Import bot configs from TOML or loose text")
    telegram_bots_import.add_argument("path")
    telegram_bots_sub.add_parser("list", help="List saved Telegram bots")

    web = sub.add_parser("web", help="Run local browser control console")
    web_sub = web.add_subparsers(dest="web_command", required=True)
    web_serve = web_sub.add_parser("serve", help="Start local web dashboard")
    web_serve.add_argument("--host", default="127.0.0.1")
    web_serve.add_argument("--port", type=int, default=8765)

    return parser


def _add_provider_delete_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--force", action="store_true", help="Actually delete the native provider session")
    parser.add_argument("--home", help="Home directory containing .codex/.kimi")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--kimi-bin", default="kimi")
    parser.add_argument("--kimi-web-url", default="", help="Existing Kimi web base URL, e.g. http://127.0.0.1:5494")
    parser.add_argument("--kimi-web-token", default="", help="Bearer token for an existing Kimi web server")
    parser.add_argument("--kimi-web-port", type=int, default=0, help="Port for a temporary Kimi web server")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--json", action="store_true", help="Print raw JSON result")


def resolve_workspace(args: argparse.Namespace, cwd: str | Path | None = None) -> Workspace:
    if args.workspace:
        return Workspace(Path(args.workspace).expanduser().resolve())
    return Workspace.from_cwd(cwd)


async def _run_prompt(args: argparse.Namespace, workspace: Workspace) -> int:
    request = RunRequest(
        prompt=args.prompt,
        adapter=args.adapter,
        project=args.project,
        task=args.task,
        focus=args.focus,
        agent=args.agent,
        session=args.session,
        title=args.title,
        cwd=args.cwd,
        codex_bin=args.codex_bin,
        kimi_bin=args.kimi_bin,
        resume=args.resume,
        resume_last=args.resume_last,
        model=args.model,
        sandbox=args.sandbox,
        approval_mode=args.approval_mode,
        no_skip_git_check=args.no_skip_git_check,
        extra_args=tuple(args.extra_arg or ()),
    )
    try:
        result = await run_agent_prompt(workspace, request)
    except RunConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(result.final_text)
    print(f"session_id: {result.session_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(_normalize_argv(argv))

    if args.command == "init":
        workspace = resolve_workspace(args)
        workspace.ensure()
        print(f"initialized platform workspace: {workspace.root}")
        if args.project_config:
            config_path = project_local_config_path(args.path)
            if not config_path.exists():
                config_path.write_text(DEFAULT_PROJECT_LOCAL_CONFIG, encoding="utf-8")
            print(f"project config: {config_path}")
        return 0

    workspace = resolve_workspace(args)

    if args.command == "doctor":
        workspace.ensure()
        for key, value in workspace.doctor().items():
            print(f"{key}: {value}")
        local_config = find_project_local_config()
        print(f"project_local_config: {local_config or ''}")
        print(f"project_local_config_exists: {local_config is not None}")
        return 0

    if args.command == "run":
        workspace.ensure()
        return asyncio.run(_run_prompt(args, workspace))

    if args.command == "assistant":
        workspace.ensure()
        registry = AgentRegistry(workspace)
        if args.assistant_command == "setup":
            return _setup_assistant(registry, args)
        if args.assistant_command == "setup-bots":
            return _setup_bot_assistants(workspace, registry, args)
        if args.assistant_command == "refresh":
            return _refresh_assistant_templates(workspace, registry, args)
        if args.assistant_command == "show":
            record = registry.resolve(args.agent)
            if record is None:
                print(f"assistant not configured: {args.agent}", file=sys.stderr)
                return 2
            print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
            return 0

    if args.command == "memory":
        workspace.ensure()
        store = MarkdownMemoryStore(workspace)
        if args.memory_command == "add":
            entry = store.add(args.title, args.content, scope=args.scope, owner=args.owner, pinned=args.pin)
            print(f"added: {entry.path}")
            return 0
        if args.memory_command == "list":
            paths = store.list(scope=args.scope, owner=args.owner)
            for path in paths:
                print(path)
            return 0
        if args.memory_command == "disable":
            return _set_memory_disabled(store, args.memory, disabled=True, scope=args.scope, owner=args.owner)
        if args.memory_command == "enable":
            return _set_memory_disabled(store, args.memory, disabled=False, scope=args.scope, owner=args.owner)
        if args.memory_command == "compact-task":
            return _compact_task_memory(workspace, args)
        if args.memory_command == "compact-focus":
            return _compact_focus_memory(workspace, args)

    if args.command == "experience":
        workspace.ensure()
        if args.experience_command == "start":
            return _experience_start(args, workspace)
        if args.experience_command == "stop":
            return _experience_stop(args, workspace)
        if args.experience_command == "restart":
            stop_code = _experience_stop(argparse.Namespace(force=bool(getattr(args, "force", False))), workspace)
            if stop_code not in {0, 1}:
                return stop_code
            return _experience_start(args, workspace)
        if args.experience_command == "status":
            return _experience_status(workspace)
        return _handle_experience_command(workspace, args)

    if args.command == "events":
        workspace.ensure()
        for event in EventLog(workspace).tail(args.tail):
            print(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True))
        return 0

    if args.command == "errors":
        workspace.ensure()
        if args.errors_command == "serve":
            ErrorHandlingDaemon(workspace).serve_forever(once=args.once, poll_interval=args.poll_interval)
            return 0
        if args.errors_command == "start":
            return _errors_start(args, workspace)
        if args.errors_command == "stop":
            return _errors_stop(args, workspace)
        if args.errors_command == "restart":
            stop_code = _errors_stop(argparse.Namespace(force=bool(getattr(args, "force", False))), workspace)
            if stop_code not in {0, 1}:
                return stop_code
            return _errors_start(args, workspace)
        if args.errors_command == "status":
            return _errors_status(workspace)
        if args.errors_command == "list":
            return _errors_list(workspace, status=args.status, limit=args.limit)
        if args.errors_command == "decisions":
            return _errors_decisions(workspace, limit=args.limit)
        if args.errors_command == "unknowns":
            return _errors_unknowns(workspace, limit=args.limit)

    if args.command == "approvals":
        workspace.ensure()
        registry = ApprovalRegistry(workspace)
        if args.approvals_command == "list":
            _print_approvals(
                registry.list(
                    status=args.status,
                    project_id=args.project,
                    task_id=args.task,
                    agent_id=args.agent,
                )
            )
            return 0
        if args.approvals_command == "show":
            record = registry.resolve(args.approval)
            if record is None:
                print(f"approval not found: {args.approval}", file=sys.stderr)
                return 2
            print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.approvals_command == "approve":
            return _resolve_approval(registry, TaskBoard(workspace), args.approval, "approved", note=args.note or "")
        if args.approvals_command == "reject":
            return _resolve_approval(registry, TaskBoard(workspace), args.approval, "rejected", note=args.note or "")

    if args.command in {"sessions", "workers"}:
        return _handle_session_agent_command(workspace, args)

    if args.command == "plans":
        workspace.ensure()
        return _handle_plans_command(workspace, args)

    if args.command == "clones":
        workspace.ensure()
        return _handle_clones_command(workspace, args)

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
        if args.agents_command == "template":
            return _set_agent_template(registry, args.agent, prompts=args.prompt, clear=args.clear)

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
            ProjectRegistry(workspace).add_directory(record.project_id, record.project_dir)
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
        if args.projects_command == "state":
            return _print_project_state(workspace, registry, args.project)
        if args.projects_command == "update-state":
            return _update_project_state(workspace, registry, args)
        if args.projects_command == "decide":
            return _record_project_decision(workspace, registry, args)
        if args.projects_command == "decisions":
            return _print_project_decisions(workspace, registry, args.project, limit=args.limit)
        if args.projects_command == "add-dir":
            record = registry.add_directory(args.project, args.directory)
            if record is None:
                print(f"project not found: {args.project}", file=sys.stderr)
                return 2
            print(f"project: {record.title} ({record.project_id})")
            print(f"directories: {len(record.metadata.get('directories') or [])}")
            return 0
        if args.projects_command == "remove-dir":
            record = registry.remove_directory(args.project, args.directory)
            if record is None:
                print(f"project not found: {args.project}", file=sys.stderr)
                return 2
            print(f"project: {record.title} ({record.project_id})")
            print(f"directories: {len(record.metadata.get('directories') or [])}")
            return 0

    if args.command == "directories":
        workspace.ensure()
        registry = DirectoryRegistry(workspace)
        if args.directories_command == "list":
            _print_directories(registry.list(project_id=args.project, status=args.status))
            return 0
        if args.directories_command == "add":
            project = ProjectRegistry(workspace).resolve(args.project) if args.project else None
            if args.project and project is None:
                print(f"project not found: {args.project}", file=sys.stderr)
                return 2
            parent_id = ""
            if args.parent:
                parent = registry.resolve(args.parent)
                if parent is None:
                    parent = registry.upsert(path=args.parent, project_id=project.project_id if project else "")
                parent_id = parent.directory_id
            record = registry.upsert(
                path=args.path,
                project_id=project.project_id if project is not None else (args.project or ""),
                title=args.title,
                parent=args.parent,
                role=args.role,
                status=args.status,
                metadata={"source": "cli", "parent_directory_id": parent_id} if parent_id else {"source": "cli"},
            )
            if project is not None:
                ProjectRegistry(workspace).add_directory(project.project_id, record.path)
            print(f"directory: {record.title} ({record.directory_id})")
            print(f"path: {record.path}")
            return 0
        if args.directories_command == "show":
            record = registry.resolve(args.directory)
            if record is None:
                print(f"directory not found: {args.directory}", file=sys.stderr)
                return 2
            print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
            return 0

    if args.command == "focus":
        workspace.ensure()
        registry = FocusRegistry(workspace)
        if args.focus_command == "create":
            project = ProjectRegistry(workspace).resolve(args.project) if args.project else None
            if args.project and project is None:
                print(f"project not found: {args.project}", file=sys.stderr)
                return 2
            agent = AgentRegistry(workspace).resolve(args.agent) if args.agent else None
            if args.agent and agent is None:
                print(f"agent not found: {args.agent}", file=sys.stderr)
                return 2
            directory = args.cwd or (agent.project_dir if agent is not None else "") or (
                project.project_dir if project is not None else "."
            )
            try:
                record = registry.create(
                    title=args.title,
                    description=args.description,
                    project_id=project.project_id if project is not None else (args.project or ""),
                    agent_id=agent.agent_id if agent is not None else (args.agent or ""),
                    directory=directory,
                    session_id=args.session or "",
                    status=args.status,
                )
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            print(f"focus: {record.title} ({record.focus_id})")
            print(f"directory: {record.directory or '-'}")
            return 0
        if args.focus_command == "list":
            _print_focus(registry.list(project_id=args.project, agent_id=args.agent, directory=args.cwd, status=args.status))
            return 0
        if args.focus_command == "show":
            record = registry.resolve(args.focus)
            if record is None:
                print(f"focus not found: {args.focus}", file=sys.stderr)
                return 2
            print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.focus_command == "context":
            return _print_focus_context(workspace, registry, args.focus)
        if args.focus_command == "handoffs":
            return _print_focus_handoffs(workspace, registry, args.focus, limit=args.limit)
        if args.focus_command == "reviews":
            return _print_focus_reviews(workspace, registry, args.focus, limit=args.limit)
        if args.focus_command == "note":
            return _update_focus_note(registry, args.focus, args.note)
        if args.focus_command == "set":
            return _set_focus_text(workspace, registry, args.focus, args.text)
        if args.focus_command == "status":
            return _update_focus_status(registry, args.focus, args.status, note=args.note or "")
        if args.focus_command == "attach-session":
            record = registry.attach_session(args.focus, args.session)
            if record is None:
                print(f"focus not found: {args.focus}", file=sys.stderr)
                return 2
            SessionRegistry(workspace).set_current_focus(
                args.session,
                record.focus_id,
                focus_text=record.description or record.title,
                actor="cli",
            )
            print(f"focus: {record.title} ({record.focus_id}) session={record.session_id}")
            return 0
        if args.focus_command == "handoff":
            return _record_focus_handoff(workspace, registry, args)
        if args.focus_command == "manager-review":
            return _record_focus_manager_review(workspace, registry, args)

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
        if args.tasks_command == "context":
            return _print_task_context(workspace, board, args.task, session_id=args.session or "")
        if args.tasks_command == "handoffs":
            return _print_task_handoffs(workspace, board, args.task, limit=args.limit)
        if args.tasks_command == "reviews":
            return _print_task_reviews(workspace, board, args.task, limit=args.limit)
        if args.tasks_command == "note":
            return _update_task_note(board, args.task, args.note)
        if args.tasks_command == "handoff":
            return _record_task_handoff(workspace, board, args)
        if args.tasks_command == "manager-review":
            return _record_manager_review(workspace, board, args)
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

    if args.command == "telegram":
        workspace.ensure()
        if args.telegram_command == "serve":
            configs = _telegram_configs_from_args(args, workspace)
            if not configs:
                print("missing Telegram token; set AGENTDECK_TELEGRAM_TOKEN or pass --token", file=sys.stderr)
                return 2
            if len(configs) == 1:
                config = configs[0]
                TelegramServer(workspace, TelegramBotApi(config.token), config).serve_forever(once=args.once)
            else:
                TelegramMultiServer(workspace, configs).serve_forever(once=args.once)
            return 0
        if args.telegram_command == "bots":
            return _telegram_bots(args, workspace)
        if args.telegram_command == "start":
            return _telegram_start(args, workspace)
        if args.telegram_command == "stop":
            return _telegram_stop(args, workspace)
        if args.telegram_command == "restart":
            return _telegram_restart(args, workspace)
        if args.telegram_command == "status":
            return _telegram_status(workspace)

    if args.command == "web":
        workspace.ensure()
        if args.web_command == "serve":
            from agentdeck.interfaces.web import serve_web

            try:
                serve_web(workspace, host=args.host, port=args.port)
            except KeyboardInterrupt:
                print("web service stopped")
            return 0

    parser.error(f"unknown command: {args.command}")
    return 2


def _telegram_start(args: argparse.Namespace, workspace: Workspace) -> int:
    configs = _telegram_configs_from_args(args, workspace)
    if not configs:
        print("missing Telegram token; set AGENTDECK_TELEGRAM_TOKEN or pass --token", file=sys.stderr)
        return 2

    pid = _read_pid(_telegram_pid_path(workspace))
    if pid and _pid_alive(pid):
        print(f"telegram service already running: pid={pid}")
        print(f"log: {_telegram_log_path(workspace)}")
        return 0

    daemon_dir = workspace.root / "telegram"
    daemon_dir.mkdir(parents=True, exist_ok=True)
    log_path = _telegram_log_path(workspace)
    pid_path = _telegram_pid_path(workspace)
    env = os.environ.copy()
    command = [
        sys.executable,
        "-m",
        "agentdeck",
        "--workspace",
        str(workspace.root),
        "telegram",
        "serve",
        "--poll-timeout",
        str(args.poll_timeout),
    ]
    if len(configs) == 1:
        config = configs[0]
        env["AGENTDECK_TELEGRAM_TOKEN"] = config.token
        if config.bot_id:
            env["AGENTDECK_TELEGRAM_BOT_ID"] = config.bot_id
            command.extend(["--bot", config.bot_id])
        if config.assistant_agent_id:
            env["AGENTDECK_TELEGRAM_ASSISTANT_AGENT"] = config.assistant_agent_id
            command.extend(["--assistant-agent", config.assistant_agent_id])
        if config.allowed_chat_ids:
            env["AGENTDECK_TELEGRAM_ALLOWED_CHATS"] = ",".join(str(chat_id) for chat_id in sorted(config.allowed_chat_ids))
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            close_fds=True,
            start_new_session=True,
        )
    pid_path.write_text(str(process.pid), encoding="utf-8")
    bot_text = "all current-server bots" if len(configs) > 1 else (configs[0].bot_id or "single bot")
    print(f"telegram service started: pid={process.pid}")
    print(f"bots: {bot_text}")
    print(f"workspace: {workspace.root}")
    print(f"log: {log_path}")
    return 0


def _telegram_configs_from_args(args: argparse.Namespace, workspace: Workspace):
    bot_id = getattr(args, "bot", "") or ""
    token = getattr(args, "token", None)
    if bot_id:
        config = _telegram_config_from_args(args, workspace)
        return [config] if config.token else []
    if not token and _has_current_server_bots(workspace):
        return _telegram_configs_from_current_server_bots(args, workspace)
    config = _telegram_config_from_args(args, workspace)
    return [config] if config.token else []


def _telegram_config_from_args(args: argparse.Namespace, workspace: Workspace):
    token = getattr(args, "token", None)
    allowed = list(getattr(args, "allowed_chat_id", []) or [])
    assistant_agent_id = getattr(args, "assistant_agent", "") or ""
    bot_id = getattr(args, "bot", "") or ""
    if bot_id:
        bot = TelegramBotRegistry(workspace).get(bot_id)
        if bot is None:
            print(f"telegram bot not found: {bot_id}", file=sys.stderr)
            return config_from_env(token="", allowed_chat_ids=[], poll_timeout=args.poll_timeout)
        token = token or bot.token
        assistant_agent_id = assistant_agent_id or bot.assistant_agent_id
        if not allowed:
            allowed = [str(chat_id) for chat_id in bot.allowed_chat_ids]
    return config_from_env(
        token=token,
        allowed_chat_ids=allowed,
        poll_timeout=args.poll_timeout,
        bot_id=bot_id,
        assistant_agent_id=assistant_agent_id,
    )


def _telegram_configs_from_current_server_bots(args: argparse.Namespace, workspace: Workspace):
    configs = []
    for bot in TelegramBotRegistry(workspace).list(server_id=current_server_id()):
        allowed = [str(chat_id) for chat_id in bot.allowed_chat_ids]
        configs.append(
            config_from_env(
                token=bot.token,
                allowed_chat_ids=allowed,
                poll_timeout=args.poll_timeout,
                bot_id=bot.bot_id,
                assistant_agent_id=bot.assistant_agent_id or assistant_agent_id_for_bot(bot.bot_id),
            )
        )
    return [config for config in configs if config.token]


def _has_current_server_bots(workspace: Workspace) -> bool:
    return bool(TelegramBotRegistry(workspace).list(server_id=current_server_id()))


def _telegram_bots(args: argparse.Namespace, workspace: Workspace) -> int:
    registry = TelegramBotRegistry(workspace)
    if args.telegram_bots_command == "add":
        record = registry.upsert(
            bot_id=args.bot_id,
            title=args.title,
            token=args.token,
            allowed_chat_ids=[int(value) for value in args.allowed_chat_id if str(value).strip().lstrip("-").isdigit()],
            source="cli",
            assistant_agent_id=args.assistant_agent,
            server_id=args.server,
        )
        print(f"bot: {record.title} ({record.bot_id})")
        print(f"token: {redacted_token(record.token)}")
        print(f"server_id: {record.server_id}")
        if record.assistant_agent_id:
            print(f"assistant_agent_id: {record.assistant_agent_id}")
        if record.allowed_chat_ids:
            print("allowed_chat_ids: " + ", ".join(str(value) for value in record.allowed_chat_ids))
        return 0
    if args.telegram_bots_command == "import":
        records = registry.import_file(args.path)
        print(f"imported: {len(records)}")
        for record in records:
            assistant_text = f" assistant={record.assistant_agent_id}" if record.assistant_agent_id else ""
            print(f"- {record.title} ({record.bot_id}) token={redacted_token(record.token)} server={record.server_id}{assistant_text}")
        return 0
    if args.telegram_bots_command == "list":
        records = registry.list()
        if not records:
            print("no telegram bots")
            return 0
        for record in records:
            chats = ",".join(str(value) for value in record.allowed_chat_ids) or "-"
            assistant_text = record.assistant_agent_id or "-"
            print(f"{record.bot_id}\t{record.title}\t{redacted_token(record.token)}\tchats={chats}\tserver={record.server_id or '-'}\tassistant={assistant_text}")
        return 0
    return 2


def _telegram_stop(args: argparse.Namespace, workspace: Workspace) -> int:
    pid_path = _telegram_pid_path(workspace)
    pid = _read_pid(pid_path)
    if not pid:
        print("telegram service is not running")
        return 0
    if not _pid_alive(pid):
        _unlink_quietly(pid_path)
        print(f"telegram service is not running; removed stale pid {pid}")
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not _pid_alive(pid):
            _unlink_quietly(pid_path)
            print(f"telegram service stopped: pid={pid}")
            return 0
        time.sleep(0.1)
    if args.force:
        os.kill(pid, signal.SIGKILL)
        _unlink_quietly(pid_path)
        print(f"telegram service killed: pid={pid}")
        return 0
    print(f"telegram service did not stop after SIGTERM: pid={pid}", file=sys.stderr)
    print("rerun with: agentdeck telegram stop --force", file=sys.stderr)
    return 1


def _telegram_restart(args: argparse.Namespace, workspace: Workspace) -> int:
    pid_path = _telegram_pid_path(workspace)
    pid = _read_pid(pid_path)
    if pid and _pid_alive(pid):
        active_jobs = _active_telegram_jobs(workspace)
        if active_jobs and not bool(getattr(args, "force_jobs", False)):
            wait_idle = max(0.0, float(getattr(args, "wait_idle", 0.0) or 0.0))
            if wait_idle > 0.0 and _wait_for_telegram_idle(
                workspace,
                timeout=wait_idle,
                poll_interval=float(getattr(args, "idle_poll_interval", 0.2) or 0.2),
            ):
                active_jobs = []
            elif wait_idle > 0.0:
                active_jobs = _active_telegram_jobs(workspace)
            if not active_jobs:
                print("telegram restart: idle gap found")
            else:
                _print_telegram_restart_blocked(active_jobs)
                return 2
        stop_code = _telegram_stop(argparse.Namespace(force=bool(getattr(args, "force", False))), workspace)
        if stop_code != 0:
            return stop_code
    elif pid:
        _unlink_quietly(pid_path)
        print(f"removed stale telegram pid: {pid}")
    else:
        print("telegram service is not running; starting it")
    return _telegram_start(args, workspace)


def _print_telegram_restart_blocked(active_jobs: list[JobRecord]) -> None:
    print("telegram restart blocked: active Telegram jobs exist", file=sys.stderr)
    for job in active_jobs[:5]:
        task = f" task={job.task_id}" if job.task_id else ""
        print(f"- {job.job_id} status={job.status}{task}", file=sys.stderr)
    if len(active_jobs) > 5:
        print(f"- ... {len(active_jobs) - 5} more", file=sys.stderr)
    print(
        "wait for them to finish, cancel them, rerun with --wait-idle, or rerun with: agentdeck telegram restart --force-jobs",
        file=sys.stderr,
    )


def _wait_for_telegram_idle(workspace: Workspace, *, timeout: float, poll_interval: float = 0.2) -> bool:
    deadline = time.time() + max(0.0, timeout)
    interval = max(0.05, poll_interval)
    last_report = 0.0
    while True:
        active_jobs = _active_telegram_jobs(workspace)
        if not active_jobs:
            return True
        now = time.time()
        if now >= deadline:
            return False
        if now - last_report >= 10.0:
            print("telegram restart blocked: active Telegram jobs exist", file=sys.stderr)
            for job in active_jobs[:5]:
                task = f" task={job.task_id}" if job.task_id else ""
                print(f"- waiting: {job.job_id} status={job.status}{task}", file=sys.stderr)
            if len(active_jobs) > 5:
                print(f"- ... {len(active_jobs) - 5} more", file=sys.stderr)
            print(f"waiting up to {max(0.0, deadline - now):.1f}s for an idle gap...", file=sys.stderr)
            last_report = now
        time.sleep(min(interval, max(0.0, deadline - now)))


def _active_telegram_jobs(workspace: Workspace) -> list[JobRecord]:
    registry = JobRegistry(workspace)
    records: list[JobRecord] = []
    for status in {"queued", "running", "cancel_requested"}:
        records.extend(registry.list(interface="telegram", status=status))
    return sorted(records, key=lambda item: item.updated_at, reverse=True)


def _errors_start(args: argparse.Namespace, workspace: Workspace) -> int:
    pid = _read_pid(_errors_pid_path(workspace))
    if pid and _pid_alive(pid):
        print(f"error handler service already running: pid={pid}")
        print(f"log: {_errors_log_path(workspace)}")
        return 0
    workspace.errors_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "agentdeck",
        "--workspace",
        str(workspace.root),
        "errors",
        "serve",
        "--poll-interval",
        str(getattr(args, "poll_interval", 5.0)),
    ]
    with _errors_log_path(workspace).open("ab") as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
            close_fds=True,
            start_new_session=True,
        )
    _errors_pid_path(workspace).write_text(str(process.pid), encoding="utf-8")
    print(f"error handler service started: pid={process.pid}")
    print(f"workspace: {workspace.root}")
    print(f"log: {_errors_log_path(workspace)}")
    return 0


def _errors_stop(args: argparse.Namespace, workspace: Workspace) -> int:
    pid_path = _errors_pid_path(workspace)
    pid = _read_pid(pid_path)
    if not pid:
        print("error handler service is not running")
        return 1
    if not _pid_alive(pid):
        _unlink_quietly(pid_path)
        print(f"error handler service is not running; removed stale pid {pid}")
        return 1
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not _pid_alive(pid):
            _unlink_quietly(pid_path)
            print(f"error handler service stopped: pid={pid}")
            return 0
        time.sleep(0.1)
    if args.force:
        os.kill(pid, signal.SIGKILL)
        _unlink_quietly(pid_path)
        print(f"error handler service killed: pid={pid}")
        return 0
    print(f"error handler service did not stop after SIGTERM: pid={pid}", file=sys.stderr)
    return 2


def _errors_status(workspace: Workspace) -> int:
    pid = _read_pid(_errors_pid_path(workspace))
    if pid and _pid_alive(pid):
        print(f"error handler service: running pid={pid}")
        print(f"log: {_errors_log_path(workspace)}")
        return 0
    if pid:
        print(f"error handler service: stopped stale_pid={pid}")
        print(f"log: {_errors_log_path(workspace)}")
        return 1
    print("error handler service: stopped")
    print(f"log: {_errors_log_path(workspace)}")
    return 1


def _errors_list(workspace: Workspace, *, status: str | None, limit: int) -> int:
    records = ErrorIncidentStore(workspace).list(status=status, limit=limit)
    if not records:
        print("no error incidents")
        return 0
    print("status\tkind\taction\tincident_id\tjob\tsession")
    decisions = {record.incident_id: record for record in ErrorIncidentStore(workspace).decisions(limit=max(limit * 2, 20))}
    for incident in records:
        decision = decisions.get(incident.incident_id)
        action = decision.action if decision is not None else "-"
        print(f"{incident.status}\t{incident.error_kind}\t{action}\t{incident.incident_id}\t{incident.job_id or '-'}\t{incident.session_id or '-'}")
    return 0


def _errors_decisions(workspace: Workspace, *, limit: int) -> int:
    records = ErrorIncidentStore(workspace).decisions(limit=limit)
    if not records:
        print("no error decisions")
        return 0
    print("action\tincident_id\treason")
    for record in records:
        print(f"{record.action}\t{record.incident_id}\t{record.reason}")
    return 0


def _errors_unknowns(workspace: Workspace, *, limit: int) -> int:
    records = ErrorIncidentStore(workspace).unknowns(limit=limit)
    if not records:
        print("no unknown errors")
        return 0
    print("count\tkind\tfingerprint\tfirst_incident\tlast_incident")
    for record in records:
        print(f"{record.count}\t{record.error_kind}\t{record.fingerprint}\t{record.first_incident_id}\t{record.last_incident_id or '-'}")
    return 0


def _errors_pid_path(workspace: Workspace) -> Path:
    return workspace.errors_dir / "error-handler.pid"


def _errors_log_path(workspace: Workspace) -> Path:
    return workspace.errors_dir / "error-handler.log"


def _experience_start(args: argparse.Namespace, workspace: Workspace) -> int:
    pid = _read_pid(_experience_pid_path(workspace))
    if pid and _pid_alive(pid):
        print(f"experience organizer service already running: pid={pid}")
        print(f"log: {_experience_log_path(workspace)}")
        return 0
    _experience_daemon_dir(workspace).mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "agentdeck",
        "--workspace",
        str(workspace.root),
        "experience",
        "serve",
        "--poll-interval",
        str(getattr(args, "poll_interval", 30.0)),
        "--limit",
        str(getattr(args, "limit", 50)),
    ]
    collection = str(getattr(args, "collection", "") or "")
    if collection:
        command.extend(["--collection", collection])
    kind = str(getattr(args, "kind", "") or "")
    if kind:
        command.extend(["--kind", kind])
    if bool(getattr(args, "dry_run", False)):
        command.append("--dry-run")
    with _experience_log_path(workspace).open("ab") as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
            close_fds=True,
            start_new_session=True,
        )
    _experience_pid_path(workspace).write_text(str(process.pid), encoding="utf-8")
    print(f"experience organizer service started: pid={process.pid}")
    print(f"workspace: {workspace.root}")
    print(f"log: {_experience_log_path(workspace)}")
    return 0


def _experience_stop(args: argparse.Namespace, workspace: Workspace) -> int:
    pid_path = _experience_pid_path(workspace)
    pid = _read_pid(pid_path)
    if not pid:
        print("experience organizer service is not running")
        return 1
    if not _pid_alive(pid):
        _unlink_quietly(pid_path)
        print(f"experience organizer service is not running; removed stale pid {pid}")
        return 1
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not _pid_alive(pid):
            _unlink_quietly(pid_path)
            print(f"experience organizer service stopped: pid={pid}")
            return 0
        time.sleep(0.1)
    if args.force:
        os.kill(pid, signal.SIGKILL)
        _unlink_quietly(pid_path)
        print(f"experience organizer service killed: pid={pid}")
        return 0
    print(f"experience organizer service did not stop after SIGTERM: pid={pid}", file=sys.stderr)
    return 2


def _experience_status(workspace: Workspace) -> int:
    pid = _read_pid(_experience_pid_path(workspace))
    if pid and _pid_alive(pid):
        print(f"experience organizer service: running pid={pid}")
        print(f"log: {_experience_log_path(workspace)}")
        return 0
    if pid:
        print(f"experience organizer service: stopped stale_pid={pid}")
        print(f"log: {_experience_log_path(workspace)}")
        return 1
    print("experience organizer service: stopped")
    print(f"log: {_experience_log_path(workspace)}")
    return 1


def _experience_daemon_dir(workspace: Workspace) -> Path:
    return workspace.root / "experience"


def _experience_pid_path(workspace: Workspace) -> Path:
    return _experience_daemon_dir(workspace) / "experience-organizer.pid"


def _experience_log_path(workspace: Workspace) -> Path:
    return _experience_daemon_dir(workspace) / "experience-organizer.log"


def _telegram_status(workspace: Workspace) -> int:
    pid = _read_pid(_telegram_pid_path(workspace))
    log_path = _telegram_log_path(workspace)
    if pid and _pid_alive(pid):
        print(f"telegram service: running pid={pid}")
        print(f"log: {log_path}")
        return 0
    if pid:
        print(f"telegram service: stopped stale_pid={pid}")
        print(f"log: {log_path}")
        return 1
    print("telegram service: stopped")
    print(f"log: {log_path}")
    return 1


def _telegram_pid_path(workspace: Workspace) -> Path:
    return workspace.root / "telegram" / "agentdeck-telegram.pid"


def _telegram_log_path(workspace: Workspace) -> Path:
    return workspace.root / "telegram" / "agentdeck-telegram.log"


def _read_pid(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


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


def _handle_session_agent_command(workspace: Workspace, args: argparse.Namespace) -> int:
    workspace.ensure()
    command = getattr(args, "sessions_command", None) or getattr(args, "workers_command", "")
    registry = SessionRegistry(workspace)
    entity_label = "worker" if args.command == "workers" else "session"

    if command == "list":
        _print_sessions(registry.list(agent_id=args.agent))
        return 0
    if command == "show":
        record = registry.resolve(args.session)
        if record is None:
            print(f"{entity_label} not found: {args.session}", file=sys.stderr)
            return 2
        print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if command == "state":
        card = SessionStateStore(workspace).get(args.session)
        if card is None:
            print(f"{entity_label} state not found: {args.session}", file=sys.stderr)
            return 2
        print(json.dumps(card.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if command == "rename":
        record = registry.rename(args.session, args.title)
        if record is None:
            print(f"{entity_label} not found: {args.session}", file=sys.stderr)
            return 2
        print(f"renamed: {record.title} ({record.session_id})")
        return 0
    if command == "scan":
        candidates = scan_provider_sessions(provider=args.provider, project_dir=args.cwd, home=args.home)
        if args.limit and args.limit > 0:
            candidates = candidates[: args.limit]
        if args.json:
            print(json.dumps([item.to_dict() for item in candidates], ensure_ascii=False, indent=2, sort_keys=True))
        else:
            _print_provider_sessions(candidates)
        return 0
    if command == "scan-codex-index":
        candidates = scan_codex_index_sessions(project_dir=args.cwd, home=args.home)
        if args.limit and args.limit > 0:
            candidates = candidates[: args.limit]
        if args.json:
            print(json.dumps([item.to_dict() for item in candidates], ensure_ascii=False, indent=2, sort_keys=True))
        else:
            _print_provider_sessions(candidates)
        return 0
    if command == "import":
        project = ProjectRegistry(workspace).resolve(args.project) if args.project else None
        if args.project and project is None:
            print(f"project not found: {args.project}", file=sys.stderr)
            return 2
        project_dir = args.cwd or (project.project_dir if project is not None else ".")
        agent_id = args.agent or (project.default_agent_id if project is not None else "default")
        adapter = args.adapter or ("codex" if args.provider == "codex" else "kimi")
        kind = args.kind or ("codex_thread" if args.provider == "codex" else "kimi_session")
        try:
            record = registry.import_provider_session(
                provider_session_id=args.provider_session,
                provider_session_kind=kind,
                agent_id=agent_id,
                adapter=adapter,
                project_dir=project_dir,
                title=args.title or "",
                session_id=args.session_id,
                project_id=project.project_id if project is not None else "",
                metadata={"provider": args.provider, "imported_by": "cli", "entity": entity_label},
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.task:
            task = TaskBoard(workspace).attach_session(args.task, record.session_id)
            if task is None:
                print(f"task not found: {args.task}", file=sys.stderr)
                return 2
        if args.focus:
            focus = FocusRegistry(workspace).attach_session(args.focus, record.session_id)
            if focus is None:
                print(f"focus not found: {args.focus}", file=sys.stderr)
                return 2
        print(f"{'worker' if args.command == 'workers' else 'imported'}: {record.title} ({record.session_id})")
        print(f"provider_session_id: {record.provider_session_id}")
        return 0
    if command == "import-codex-index":
        candidates = scan_codex_index_sessions(home=args.home)
        matches = [
            candidate
            for candidate in candidates
            if candidate.provider_session_id == args.session or candidate.title == args.session
        ]
        if not matches:
            print(f"codex indexed session not found: {args.session}", file=sys.stderr)
            return 2
        candidate = matches[0]
        project = ProjectRegistry(workspace).resolve(args.project) if args.project else None
        if args.project and project is None:
            print(f"project not found: {args.project}", file=sys.stderr)
            return 2
        project_dir = args.cwd or candidate.project_dir or (project.project_dir if project is not None else "")
        if not project_dir:
            print(
                "cwd required: Codex index has no working directory for this session; pass --cwd /path/to/project",
                file=sys.stderr,
            )
            return 2
        agent_id = args.agent or (project.default_agent_id if project is not None else "default")
        metadata = dict(candidate.metadata)
        metadata.update(
            {
                "provider": "codex",
                "imported_by": "cli",
                "entity": entity_label,
                "import_source": "codex_session_index",
                "codex_interactive_resume": f"codex resume {candidate.provider_session_id} -C {project_dir}",
                "codex_background_resume": f"codex exec resume {candidate.provider_session_id} -C {project_dir} <prompt>",
            }
        )
        try:
            record = registry.import_provider_session(
                provider_session_id=candidate.provider_session_id,
                provider_session_kind="codex_thread",
                agent_id=agent_id,
                adapter="codex",
                project_dir=project_dir,
                title=args.title or candidate.title,
                session_id=args.session_id,
                project_id=project.project_id if project is not None else "",
                metadata=metadata,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.task:
            task = TaskBoard(workspace).attach_session(args.task, record.session_id)
            if task is None:
                print(f"task not found: {args.task}", file=sys.stderr)
                return 2
        if args.focus:
            focus = FocusRegistry(workspace).attach_session(args.focus, record.session_id)
            if focus is None:
                print(f"focus not found: {args.focus}", file=sys.stderr)
                return 2
        print(f"{'worker' if args.command == 'workers' else 'imported'}: {record.title} ({record.session_id})")
        print(f"provider_session_id: {record.provider_session_id}")
        print(f"interactive_resume: codex resume {record.provider_session_id} -C {record.project_dir}")
        print(f"background_resume: codex exec resume {record.provider_session_id} -C {record.project_dir} <prompt>")
        return 0
    if command == "clone":
        try:
            if args.strategy == "ai":
                capsule = create_ai_clone_capsule(
                    workspace,
                    args.session,
                    home=args.home,
                    recent_turns=args.recent_turns,
                    max_provider_chars=args.max_provider_chars,
                    summarizer_adapter=args.summarizer_adapter,
                    codex_bin=args.codex_bin,
                    kimi_bin=args.kimi_bin,
                    model=args.model,
                    keep_debug=args.keep_debug,
                    collections=args.collection,
                )
            else:
                capsule = create_rules_clone_capsule(
                    workspace,
                    args.session,
                    home=args.home,
                    recent_turns=args.recent_turns,
                    max_provider_chars=args.max_provider_chars,
                    collections=args.collection,
                )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(capsule.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(f"clone: {capsule.clone_id}")
            print(f"source_session_id: {capsule.source_session_id}")
            print(f"provider_session_id: {capsule.provider_session_id}")
            print(f"context: {CloneStore(workspace).context_path(capsule.clone_id)}")
            print(f"validation_ok: {str(bool(capsule.validation.get('ok'))).lower()}")
        return 0
    if command == "delete-provider-session":
        record = registry.resolve(args.session)
        if record is None:
            print(f"{entity_label} not found: {args.session}", file=sys.stderr)
            return 2
        if not record.provider_session_id:
            print(f"{entity_label} has no provider session id: {record.session_id}", file=sys.stderr)
            return 2
        result = _delete_native_provider_session(
            provider=_provider_for_session_record(record),
            provider_session_id=record.provider_session_id,
            args=args,
        )
        _print_provider_delete_result(result, json_output=args.json)
        return 0 if result.ok else 2
    return 0


def _handle_clones_command(workspace: Workspace, args: argparse.Namespace) -> int:
    store = CloneStore(workspace)
    if args.clones_command == "list":
        _print_clones(store.list())
        return 0
    if args.clones_command == "show":
        capsule = store.get(args.clone)
        if capsule is None:
            print(f"clone not found: {args.clone}", file=sys.stderr)
            return 2
        if args.context:
            path = store.context_path(capsule.clone_id)
            try:
                print(path.read_text(encoding="utf-8"), end="")
            except OSError as exc:
                print(str(exc), file=sys.stderr)
                return 1
        else:
            print(json.dumps(capsule.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.clones_command == "spawn":
        capsule = store.get(args.clone)
        if capsule is None:
            print(f"clone not found: {args.clone}", file=sys.stderr)
            return 2
        try:
            result = _spawn_worker_from_clone(workspace, capsule, args)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(f"worker: {result['agent_id']}")
            print(f"session: {result['session_id']}")
            print(f"clone: {result['clone_id']}")
            print(f"context: {result['clone_context_path']}")
            print(f"first_run: {result['first_run']}")
        return 0
    if args.clones_command == "delete-provider-session":
        capsule = store.get(args.clone)
        if capsule is None:
            print(f"clone not found: {args.clone}", file=sys.stderr)
            return 2
        if not capsule.provider_session_id:
            print(f"clone has no provider session id: {capsule.clone_id}", file=sys.stderr)
            return 2
        result = _delete_native_provider_session(
            provider=capsule.provider,
            provider_session_id=capsule.provider_session_id,
            args=args,
        )
        _print_provider_delete_result(result, json_output=args.json)
        return 0 if result.ok else 2
    if args.clones_command == "cleanup":
        removed = store.cleanup_tmp(older_than_seconds=args.older_than)
        print(f"removed: {removed}")
        return 0
    return 0


def _spawn_worker_from_clone(workspace: Workspace, capsule: CloneCapsule, args: argparse.Namespace) -> dict[str, str]:
    return spawn_worker_from_clone(
        workspace,
        capsule,
        agent_id=args.agent or "",
        session_id=args.session_id or "",
        title=args.title or "",
        project=args.project or "",
        project_dir=args.cwd or "",
        adapter=args.adapter or "",
        role=args.role,
        team_id=args.team,
        model=args.model or "",
        sandbox=args.sandbox or "",
        approval_mode=args.approval_mode,
        replace=args.replace,
    )


def _handle_experience_command(workspace: Workspace, args: argparse.Namespace) -> int:
    store = ExperienceStore(workspace)
    if args.experience_command == "collections":
        _print_experience_collections(
            store.list_collections(
                project_id=args.project or "",
                worker_id=args.worker or "",
                agent_id=args.agent or "",
                focus_id=args.focus or "",
                kind=args.kind or "",
                status=args.status or "",
            )
        )
        return 0
    if args.experience_command == "create-collection":
        try:
            record = store.create_collection(
                args.title,
                kind=args.kind,
                purpose=args.purpose,
                project_id=args.project or "",
                directory_id=args.directory_id or "",
                worker_id=args.worker or "",
                agent_id=args.agent or "",
                focus_id=args.focus or "",
                status=args.status,
                metadata={"source": "cli"},
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"collection: {record.title} ({record.collection_id})")
        print(f"kind: {record.kind}")
        if record.purpose:
            print(f"purpose: {record.purpose}")
        return 0
    if args.experience_command == "show-collection":
        record = store.resolve_collection(args.collection)
        if record is None:
            print(f"experience collection not found: {args.collection}", file=sys.stderr)
            return 2
        summary = store.collection_summary(record.collection_id, event_limit=20) or record.to_dict()
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.experience_command == "record":
        try:
            event = store.record_event(
                args.collection,
                purpose=args.purpose,
                context=args.context,
                actions=args.action,
                result=args.result,
                analysis=args.analysis,
                decisions=args.decision,
                artifacts=_parse_experience_artifacts(args.artifact),
                tags=args.tag,
                parent_event_id=args.parent or "",
                sequence_index=args.sequence,
                level=args.level,
                kind=args.kind,
                status=args.status,
                focus_id=args.focus or "",
                confidence=args.confidence,
                metadata={"source": "cli"},
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"event: {event.event_id}")
        print(f"collection: {event.collection_id}")
        print(f"purpose: {event.purpose}")
        if event.result:
            print(f"result: {event.result}")
        return 0
    if args.experience_command == "events":
        _print_experience_events(
            store.list_events(
                collection=args.collection or "",
                project_id=args.project or "",
                worker_id=args.worker or "",
                agent_id=args.agent or "",
                focus_id=args.focus or "",
                kind=args.kind or "",
                query=args.query or "",
                limit=args.limit,
            )
        )
        return 0
    if args.experience_command == "show":
        event = store.resolve_event(args.event)
        if event is None:
            print(f"experience event not found: {args.event}", file=sys.stderr)
            return 2
        payload = event.to_dict()
        payload["edges"] = [edge.to_dict() for edge in store.list_edges(event_id=event.event_id)]
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.experience_command == "link":
        try:
            edge = store.link_events(
                args.from_event,
                args.to_event,
                relation=args.relation,
                reason=args.reason,
                metadata={"source": "cli"},
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"edge: {edge.edge_id}")
        print(f"{edge.from_event_id} --{edge.relation}--> {edge.to_event_id}")
        if edge.reason:
            print(f"reason: {edge.reason}")
        return 0
    if args.experience_command == "edges":
        _print_experience_edges(store.list_edges(event_id=args.event or "", relation=args.relation or ""))
        return 0
    if args.experience_command == "organize":
        try:
            result = ExperienceOrganizer(workspace).process_once(
                limit=args.limit,
                collection=args.collection or "",
                kind=args.kind or "",
                dry_run=bool(args.dry_run),
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        _print_experience_organizer_result(result, dry_run=bool(args.dry_run))
        return 0
    if args.experience_command == "serve":
        try:
            if args.once:
                result = ExperienceOrganizer(workspace).process_once(
                    limit=args.limit,
                    collection=args.collection or "",
                    kind=args.kind or "",
                    dry_run=bool(args.dry_run),
                )
                _print_experience_organizer_result(result, dry_run=bool(args.dry_run))
            else:
                ExperienceOrganizer(workspace).serve_forever(
                    once=False,
                    poll_interval=args.poll_interval,
                    limit=args.limit,
                    collection=args.collection or "",
                    kind=args.kind or "",
                    dry_run=bool(args.dry_run),
                )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0
    return 2


def _handle_plans_command(workspace: Workspace, args: argparse.Namespace) -> int:
    registry = PlanRegistry(workspace)
    if args.plans_command == "list":
        _print_plans(registry.list(project_id=args.project, focus_id=args.focus, status=args.status))
        return 0
    if args.plans_command == "new":
        draft = _read_text_arg(args.draft, args.draft_file)
        focus = FocusRegistry(workspace).resolve(args.focus) if args.focus else None
        session = SessionRegistry(workspace).resolve(args.session) if args.session else None
        project = ProjectRegistry(workspace).resolve(args.project) if args.project else None
        try:
            record = registry.create(
                title=args.title,
                draft=draft,
                project_id=(focus.project_id if focus is not None else (project.project_id if project is not None else (args.project or ""))),
                focus_id=focus.focus_id if focus is not None else (args.focus or ""),
                session_id=(focus.session_id if focus is not None else (session.session_id if session is not None else (args.session or ""))),
                agent_id=(focus.agent_id if focus is not None else (session.agent_id if session is not None else (args.agent or ""))),
                directory=(focus.directory if focus is not None else (session.project_dir if session is not None else (args.cwd or ""))),
                metadata={"created_by": "cli"},
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"plan: {record.title} ({record.plan_id})")
        print("status: draft")
        return 0
    if args.plans_command == "show":
        record = registry.resolve(args.plan)
        if record is None:
            print(f"plan not found: {args.plan}", file=sys.stderr)
            return 2
        print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.plans_command == "draft":
        record = registry.resolve(args.plan)
        if record is None:
            print(f"plan not found: {args.plan}", file=sys.stderr)
            return 2
        print(record.draft)
        return 0
    if args.plans_command == "set-draft":
        draft = _read_text_arg(args.text, args.file)
        if not draft.strip():
            print("draft text is empty; pass --text or --file", file=sys.stderr)
            return 2
        record = registry.set_draft(args.plan, draft, note="Draft updated from CLI.")
        if record is None:
            print(f"plan not found: {args.plan}", file=sys.stderr)
            return 2
        print(f"plan draft updated: {record.title} ({record.plan_id})")
        return 0
    if args.plans_command == "note":
        record = registry.add_note(args.plan, args.note, kind="discussion")
        if record is None:
            print(f"plan not found: {args.plan}", file=sys.stderr)
            return 2
        print(f"plan note added: {record.title} ({record.plan_id})")
        return 0
    if args.plans_command == "compile":
        try:
            record = registry.compile(args.plan)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if record is None:
            print(f"plan not found: {args.plan}", file=sys.stderr)
            return 2
        print(f"plan compiled: {record.title} ({record.plan_id})")
        _print_plan_status(record)
        return 0
    if args.plans_command == "status":
        record = registry.resolve(args.plan)
        if record is None:
            print(f"plan not found: {args.plan}", file=sys.stderr)
            return 2
        _print_plan_status(record)
        return 0
    if args.plans_command == "step":
        try:
            record = registry.update_step(
                args.plan,
                args.step,
                status=args.status,
                report=args.report,
                result=args.result,
                decision=args.decision,
                artifacts=args.artifact,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if record is None:
            print(f"plan or step not found: {args.plan} {args.step}", file=sys.stderr)
            return 2
        _print_plan_status(record)
        return 0
    return 2


def _read_text_arg(text: str, path: str | None) -> str:
    if path:
        return Path(path).expanduser().read_text(encoding="utf-8")
    return text or ""


def _one_line(value: str, max_chars: int) -> str:
    text = " ".join(str(value).strip().split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "..."


def _parse_experience_artifacts(values: list[str]) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    for value in values or []:
        text = str(value).strip()
        if not text:
            continue
        kind = "file"
        path = text
        if ":" in text and not re.match(r"^[a-zA-Z]:[\\/]", text):
            maybe_kind, maybe_path = text.split(":", 1)
            if maybe_kind.strip() and maybe_path.strip():
                kind = maybe_kind.strip()
                path = maybe_path.strip()
        artifacts.append({"kind": kind, "path": path})
    return artifacts


def _delete_native_provider_session(
    *,
    provider: str,
    provider_session_id: str,
    args: argparse.Namespace,
) -> ProviderDeleteResult:
    return delete_provider_session(
        provider=provider,
        provider_session_id=provider_session_id,
        force=bool(getattr(args, "force", False)),
        home=getattr(args, "home", None),
        codex_bin=getattr(args, "codex_bin", "codex"),
        kimi_bin=getattr(args, "kimi_bin", "kimi"),
        kimi_web_url=getattr(args, "kimi_web_url", ""),
        kimi_web_token=getattr(args, "kimi_web_token", ""),
        kimi_web_port=int(getattr(args, "kimi_web_port", 0) or 0),
        timeout=float(getattr(args, "timeout", 20.0) or 20.0),
    )


def _provider_for_session_record(record: SessionRecord) -> str:
    provider = str(record.metadata.get("provider") or "").strip().lower()
    if provider:
        return provider
    if record.adapter.startswith("codex") or record.provider_session_kind.startswith("codex"):
        return "codex"
    if record.adapter.startswith("kimi") or record.provider_session_kind.startswith("kimi"):
        return "kimi"
    return record.adapter


def _print_provider_delete_result(result: ProviderDeleteResult, *, json_output: bool = False) -> None:
    if json_output:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return
    status = "deleted" if result.ok and result.executed else ("planned" if result.ok else "failed")
    print(f"provider_delete: {status}")
    print(f"provider: {result.provider}")
    print(f"provider_session_id: {result.provider_session_id}")
    print(f"action: {result.action}")
    if result.command:
        print(f"command: {' '.join(result.command)}")
    if result.message:
        print(f"message: {result.message}")
    if result.error:
        print(f"error: {result.error}")
    if not result.executed:
        print("rerun with --force to execute")


def _print_sessions(records: list[SessionRecord]) -> None:
    if not records:
        print("no session-agents")
        return
    print("title\tsession_agent_id\tidentity\tadapter\tstatus\tupdated_at\tdirectory_id\tproject_dir")
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
                    str(record.metadata.get("directory_id") or "-"),
                    record.project_dir,
                ]
            )
        )


def _print_provider_sessions(records: list[ProviderSessionCandidate]) -> None:
    if not records:
        print("no provider sessions")
        return
    print("title\tprovider\tprovider_session_id\tupdated_at\tproject_dir")
    for record in records:
        print(
            "\t".join(
                [
                    record.title,
                    record.provider,
                    record.provider_session_id,
                    _format_timestamp(record.updated_at),
                    record.project_dir,
                ]
            )
        )


def _print_clones(records: list[CloneCapsule]) -> None:
    if not records:
        print("no clones")
        return
    print("clone_id\ttitle\tsource_session_id\tprovider\tcreated_at\tvalidation")
    for record in records:
        print(
            "\t".join(
                [
                    record.clone_id,
                    record.title,
                    record.source_session_id,
                    record.provider,
                    _format_timestamp(record.created_at),
                    "ok" if bool(record.validation.get("ok")) else "needs-review",
                ]
            )
        )


def _print_experience_collections(records: list[ExperienceCollection]) -> None:
    if not records:
        print("no experience collections")
        return
    print("title\tcollection_id\tkind\tstatus\tproject\tworker\tfocus\tpurpose")
    for record in records:
        print(
            "\t".join(
                [
                    record.title,
                    record.collection_id,
                    record.kind,
                    record.status,
                    record.project_id or "-",
                    record.worker_id or record.agent_id or "-",
                    record.focus_id or "-",
                    record.purpose.replace("\n", " ") or "-",
                ]
            )
        )


def _print_experience_events(records: list[ExperienceEvent]) -> None:
    if not records:
        print("no experience events")
        return
    print("event_id\tlevel\tkind\tstatus\tcollection\tfocus\tpurpose\tresult")
    for record in records:
        print(
            "\t".join(
                [
                    record.event_id,
                    record.level,
                    record.kind,
                    record.status,
                    record.collection_id,
                    record.focus_id or "-",
                    record.purpose.replace("\n", " "),
                    record.result.replace("\n", " ") or "-",
                ]
            )
        )


def _print_experience_edges(records: list[ExperienceEdge]) -> None:
    if not records:
        print("no experience edges")
        return
    print("edge_id\tfrom\trelation\tto\treason")
    for record in records:
        print(
            "\t".join(
                [
                    record.edge_id,
                    record.from_event_id,
                    record.relation,
                    record.to_event_id,
                    record.reason.replace("\n", " ") or "-",
                ]
            )
        )


def _print_experience_organizer_result(result: ExperienceOrganizerResult, *, dry_run: bool = False) -> None:
    prefix = "dry-run " if dry_run else ""
    print(
        f"{prefix}experience organizer: "
        f"collections_created={result.collections_created} "
        f"events_created={result.events_created} "
        f"edges_created={result.edges_created} "
        f"skipped={result.skipped}"
    )


def _print_approvals(records: list[ApprovalRecord]) -> None:
    if not records:
        print("no approvals")
        return
    print("title\tapproval_id\tstatus\tproject\ttask\tagent\tprovider\tsession")
    for record in records:
        print(
            "\t".join(
                [
                    record.title,
                    record.approval_id,
                    record.status,
                    record.project_id or "-",
                    record.task_id or "-",
                    record.agent_id,
                    record.provider or record.adapter or "-",
                    record.session_id or "-",
                ]
            )
        )


def _print_projects(records: list[ProjectRecord]) -> None:
    if not records:
        print("no projects")
        return
    print("title\tproject_id\tteam\tdefault_agent\tstatus\tdirs\tprimary_dir")
    for record in records:
        print(
            "\t".join(
                [
                    record.title,
                    record.project_id,
                    record.team_id,
                    record.default_agent_id,
                    record.status,
                    str(len(record.metadata.get("directories") or ([record.project_dir] if record.project_dir else []))),
                    record.project_dir,
                ]
            )
        )


def _print_directories(records: list[DirectoryRecord]) -> None:
    if not records:
        print("no directories")
        return
    print("title\tdirectory_id\tproject\trole\tstatus\tparent\tpath")
    for record in records:
        print(
            "\t".join(
                [
                    record.title,
                    record.directory_id,
                    record.project_id or "-",
                    record.role,
                    record.status,
                    record.parent_directory_id or "-",
                    record.path,
                ]
            )
        )


def _print_project_state(workspace: Workspace, registry: ProjectRegistry, project: str) -> int:
    record = registry.resolve(project)
    if record is None:
        print(f"project not found: {project}", file=sys.stderr)
        return 2
    state = ProjectStateStore(workspace).get(record.project_id)
    if state is None:
        print(f"project state not found: {record.project_id}", file=sys.stderr)
        return 2
    print(json.dumps(state.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _update_project_state(workspace: Workspace, registry: ProjectRegistry, args: argparse.Namespace) -> int:
    record = registry.resolve(args.project)
    if record is None:
        print(f"project not found: {args.project}", file=sys.stderr)
        return 2
    state = ProjectStateStore(workspace).update(
        record.project_id,
        goal=args.goal,
        phase=args.phase,
        current_focus=args.focus,
        next_steps=args.next_steps,
        constraints=args.constraints,
        blockers=args.blockers,
        active_artifacts=args.artifacts,
        updated_by=args.updated_by or "",
    )
    print(f"project_state: {record.title} ({state.project_id})")
    if state.current_focus:
        print(f"focus: {state.current_focus}")
    if state.next_steps:
        print(f"next: {state.next_steps[0]}")
    return 0


def _record_project_decision(workspace: Workspace, registry: ProjectRegistry, args: argparse.Namespace) -> int:
    record = registry.resolve(args.project)
    if record is None:
        print(f"project not found: {args.project}", file=sys.stderr)
        return 2
    try:
        decision = ProjectStateStore(workspace).add_decision(
            record.project_id,
            args.decision,
            reason=args.reason,
            impact=args.impact,
            alternatives=args.alternatives,
            made_by=args.made_by,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"decision: {decision.decision_id}")
    print(f"project: {record.title} ({record.project_id})")
    print(f"text: {decision.decision}")
    return 0


def _print_project_decisions(workspace: Workspace, registry: ProjectRegistry, project: str, *, limit: int = 10) -> int:
    record = registry.resolve(project)
    if record is None:
        print(f"project not found: {project}", file=sys.stderr)
        return 2
    decisions = ProjectStateStore(workspace).decisions(record.project_id, limit=limit)
    if not decisions:
        print(f"no decisions for project: {record.title} ({record.project_id})")
        return 0
    print(f"decisions for: {record.title} ({record.project_id})")
    for decision in decisions:
        print(f"- {decision.decision}")
        if decision.reason:
            print(f"  reason: {decision.reason}")
        if decision.impact:
            print(f"  impact: {decision.impact}")
    return 0


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


def _print_focus(records: list[FocusRecord]) -> None:
    if not records:
        print("no focus")
        return
    print("title\tfocus_id\tstatus\tproject\tagent\tsession\tdirectory")
    for record in records:
        print(
            "\t".join(
                [
                    record.title,
                    record.focus_id,
                    record.status,
                    record.project_id or "-",
                    record.agent_id or "-",
                    record.session_id or "-",
                    record.directory or "-",
                ]
            )
        )


def _print_plans(records: list[PlanRecord]) -> None:
    if not records:
        print("no plans")
        return
    print("title\tplan_id\tstatus\tsteps\tproject\tfocus\tsession")
    for record in records:
        done, total = plan_progress(record)
        print(
            "\t".join(
                [
                    record.title,
                    record.plan_id,
                    record.status,
                    f"{done}/{total}",
                    record.project_id or "-",
                    record.focus_id or "-",
                    record.session_id or "-",
                ]
            )
        )


def _print_plan_status(record: PlanRecord) -> None:
    done, total = plan_progress(record)
    print(f"plan: {record.title} ({record.plan_id})")
    print(f"status: {record.status}")
    print(f"steps: {done}/{total}")
    for index, step in enumerate(record.steps, 1):
        print(f"{index}. {step.step_id} [{step.status}] {step.title}")
        if step.report:
            print(f"   report: {_one_line(step.report, 180)}")


def _setup_assistant(registry: AgentRegistry, args: argparse.Namespace) -> int:
    try:
        record = registry.upsert(
            agent_id=args.agent,
            title=args.title,
            project_id="",
            role="manager",
            team_id="agentdeck",
            adapter=args.adapter,
            project_dir=args.cwd,
            model=args.model or "",
            approval_mode=args.approval_mode,
            replace=args.replace,
        )
        record = registry.set_role_template(record.agent_id, DEFAULT_ASSISTANT_TEMPLATE)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"assistant: {record.title} ({record.agent_id})")
    print(f"adapter: {record.adapter}")
    print(f"cwd: {record.project_dir}")
    return 0


def _setup_bot_assistants(workspace: Workspace, registry: AgentRegistry, args: argparse.Namespace) -> int:
    bot_registry = TelegramBotRegistry(workspace)
    server_id = "" if args.all_servers else args.server
    bots = bot_registry.list(server_id=server_id or None)
    if not bots:
        target = "all servers" if args.all_servers else f"server {args.server}"
        print(f"no telegram bots for {target}")
        return 0
    print(f"server: {args.server if not args.all_servers else 'all'}")
    print(f"bots: {len(bots)}")
    for bot in bots:
        agent_id = bot.assistant_agent_id or assistant_agent_id_for_bot(bot.bot_id)
        try:
            existing = registry.resolve(agent_id)
            if existing is not None and not args.replace:
                record = existing
            else:
                record = registry.upsert(
                    agent_id=agent_id,
                    title=f"{bot.title} Assistant",
                    project_id="",
                    role="manager",
                    team_id="agentdeck",
                    adapter=args.adapter,
                    project_dir=args.cwd,
                    model=args.model or "",
                    approval_mode=args.approval_mode,
                    replace=args.replace,
                )
            record = registry.set_role_template(record.agent_id, DEFAULT_ASSISTANT_TEMPLATE)
            bot_registry.assign_assistant(bot.bot_id, record.agent_id, server_id=bot.server_id or args.server)
        except ValueError as exc:
            print(f"{bot.bot_id}: {exc}", file=sys.stderr)
            return 2
        print(f"- {bot.title} ({bot.bot_id}) -> {record.agent_id}")
    return 0


def _refresh_assistant_templates(workspace: Workspace, registry: AgentRegistry, args: argparse.Namespace) -> int:
    agent_ids = list(dict.fromkeys(args.agent or []))
    if not agent_ids and registry.resolve(ASSISTANT_AGENT_ID) is not None:
        agent_ids.append(ASSISTANT_AGENT_ID)

    bot_registry = TelegramBotRegistry(workspace)
    server_id = None if args.all_servers else args.server
    for bot in bot_registry.list(server_id=server_id):
        agent_ids.append(bot.assistant_agent_id or assistant_agent_id_for_bot(bot.bot_id))

    agent_ids = list(dict.fromkeys(agent_ids))
    if not agent_ids:
        print("no assistant agents found")
        return 0

    refreshed = 0
    for agent_id in agent_ids:
        record = registry.resolve(agent_id)
        if record is None:
            print(f"- {agent_id}: missing")
            continue
        registry.set_role_template(record.agent_id, DEFAULT_ASSISTANT_TEMPLATE)
        refreshed += 1
        print(f"- refreshed {record.title} ({record.agent_id})")
    print(f"refreshed: {refreshed}")
    return 0


def _set_agent_template(registry: AgentRegistry, agent: str, *, prompts: list[str], clear: bool) -> int:
    if clear:
        template = ""
    else:
        template = "\n".join(prompt for prompt in prompts if prompt.strip())
        if not template.strip():
            print("missing template prompt; pass --prompt or --clear", file=sys.stderr)
            return 2
    try:
        record = registry.set_role_template(agent, template)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if clear:
        print(f"agent template cleared: {record.title} ({record.agent_id})")
    else:
        print(f"agent template set: {record.title} ({record.agent_id})")
        print(str(record.metadata.get("role_template") or ""))
    return 0


def _set_memory_disabled(
    store: MarkdownMemoryStore,
    memory: str,
    *,
    disabled: bool,
    scope: str | None,
    owner: str | None,
) -> int:
    try:
        document = store.set_disabled(memory, disabled=disabled, scope=scope, owner=owner)  # type: ignore[arg-type]
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    state = "disabled" if disabled else "enabled"
    print(f"memory {state}: {document.title}")
    print(f"id: {document.memory_id}")
    print(f"path: {document.path}")
    return 0


def _compact_task_memory(workspace: Workspace, args: argparse.Namespace) -> int:
    board = TaskBoard(workspace)
    task = board.resolve(args.task)
    if task is None:
        print(f"task not found: {args.task}", file=sys.stderr)
        return 2
    context = build_agentdeck_context(
        workspace,
        task=task,
        session_id=task.session_id,
        max_chars=max(args.max_chars, 500),
        include_memories=False,
    )
    if not context:
        print(f"no task context to compact: {task.title} ({task.task_id})")
        return 0

    owner = args.owner or _default_memory_owner(args.scope, task)
    title = args.title or f"{task.title} context snapshot"
    content = "\n".join(
        [
            f"# {title}",
            "",
            "This memory was generated from structured AgentDeck state, not a raw chat transcript.",
            "",
            context,
        ]
    )
    try:
        entry = MarkdownMemoryStore(workspace).add(
            title,
            content,
            scope=args.scope,
            owner=owner,
            memory_type="task-context",
            source="agentdeck-context",
            pinned=args.pin,
            tags=["agentdeck", "task-context"],
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"memory: {entry.memory_id}")
    print(f"path: {entry.path}")
    print(f"scope: {args.scope}")
    if owner:
        print(f"owner: {owner}")
    return 0


def _compact_focus_memory(workspace: Workspace, args: argparse.Namespace) -> int:
    registry = FocusRegistry(workspace)
    focus = registry.resolve(args.focus)
    if focus is None:
        print(f"focus not found: {args.focus}", file=sys.stderr)
        return 2
    session_id = args.session or focus.session_id
    context = build_agentdeck_context(
        workspace,
        task=None,
        focus=focus,
        session_id=session_id,
        max_chars=max(args.max_chars, 500),
        include_memories=False,
    )
    if not context:
        print(f"no focus context to compact: {focus.title} ({focus.focus_id})")
        return 0

    owner = focus.project_id
    title = args.title or f"{focus.title} context snapshot"
    content = "\n".join(
        [
            f"# {title}",
            "",
            "This memory was generated from structured AgentDeck state, not a raw chat transcript.",
            "",
            context,
        ]
    )
    try:
        entry = MarkdownMemoryStore(workspace).add(
            title,
            content,
            scope="project",
            owner=owner,
            memory_type="focus-context",
            source="agentdeck-context",
            pinned=args.pin,
            tags=["agentdeck", "focus-context"],
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"memory: {entry.memory_id}")
    print(f"path: {entry.path}")
    print("scope: project")
    if owner:
        print(f"owner: {owner}")
    return 0


def _default_memory_owner(scope: str, task: TaskRecord) -> str:
    if scope == "project":
        return task.project_id
    if scope == "team":
        return task.team_id
    if scope == "agent":
        return task.agent_id
    if scope == "task":
        return task.task_id
    return ""


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


def _update_focus_status(registry: FocusRegistry, focus: str, status: str, *, note: str = "") -> int:
    try:
        record = registry.set_status(focus, status, note=note)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if record is None:
        print(f"focus not found: {focus}", file=sys.stderr)
        return 2
    print(f"focus: {record.title} ({record.focus_id}) status={record.status}")
    return 0


def _update_focus_note(registry: FocusRegistry, focus: str, note: str) -> int:
    record = registry.add_note(focus, note)
    if record is None:
        print(f"focus not found: {focus}", file=sys.stderr)
        return 2
    print(f"focus: {record.title} ({record.focus_id}) notes={len(record.notes)}")
    return 0


def _set_focus_text(workspace: Workspace, registry: FocusRegistry, focus: str, text: str) -> int:
    record = registry.set_text(focus, text, note="Focus text updated from CLI.")
    if record is None:
        print(f"focus not found: {focus}", file=sys.stderr)
        return 2
    if record.session_id:
        SessionRegistry(workspace).set_current_focus(
            record.session_id,
            record.focus_id,
            focus_text=record.description,
            actor="cli",
        )
    print(f"focus: {record.title} ({record.focus_id})")
    print(f"text: {record.description}")
    return 0


def _print_task_context(workspace: Workspace, board: TaskBoard, task: str, *, session_id: str = "") -> int:
    record = board.resolve(task)
    if record is None:
        print(f"task not found: {task}", file=sys.stderr)
        return 2
    context = build_agentdeck_context(
        workspace,
        task=record,
        session_id=session_id or record.session_id,
        max_chars=8000,
    )
    if not context:
        print(f"no AgentDeck context for task: {record.title} ({record.task_id})")
        return 0
    print(context)
    return 0


def _print_focus_context(workspace: Workspace, registry: FocusRegistry, focus: str) -> int:
    record = registry.resolve(focus)
    if record is None:
        print(f"focus not found: {focus}", file=sys.stderr)
        return 2
    context = build_agentdeck_context(
        workspace,
        task=None,
        focus=record,
        session_id=record.session_id,
        max_chars=8000,
    )
    if not context:
        print(f"no AgentDeck context for focus: {record.title} ({record.focus_id})")
        return 0
    print(context)
    return 0


def _print_focus_handoffs(workspace: Workspace, registry: FocusRegistry, focus: str, *, limit: int = 5) -> int:
    record = registry.resolve(focus)
    if record is None:
        print(f"focus not found: {focus}", file=sys.stderr)
        return 2
    entries = ProgressJournal(workspace).list(kind="handoff", focus_id=record.focus_id, limit=max(limit, 0))
    if not entries:
        print(f"no handoffs for focus: {record.title} ({record.focus_id})")
        return 0
    print(f"handoffs for focus: {record.title} ({record.focus_id})")
    for entry in entries:
        print(f"- {entry.summary}")
        if entry.next_steps:
            print(f"  next: {entry.next_steps[0]}")
        if entry.blockers:
            print(f"  blocker: {entry.blockers[0]}")
        if entry.decisions:
            print(f"  decision: {entry.decisions[0]}")
    return 0


def _print_focus_reviews(workspace: Workspace, registry: FocusRegistry, focus: str, *, limit: int = 5) -> int:
    record = registry.resolve(focus)
    if record is None:
        print(f"focus not found: {focus}", file=sys.stderr)
        return 2
    entries = ProgressJournal(workspace).list(kind="manager-review", focus_id=record.focus_id, limit=max(limit, 0))
    if not entries:
        print(f"no manager reviews for focus: {record.title} ({record.focus_id})")
        return 0
    print(f"manager reviews for focus: {record.title} ({record.focus_id})")
    for entry in entries:
        status = str(entry.metadata.get("status") or "").strip()
        prefix = f"{status}: " if status else ""
        print(f"- {prefix}{entry.summary}")
        if entry.next_steps:
            print(f"  next: {entry.next_steps[0]}")
        if entry.blockers:
            print(f"  blocker: {entry.blockers[0]}")
        if entry.decisions:
            print(f"  decision: {entry.decisions[0]}")
    return 0


def _print_task_handoffs(workspace: Workspace, board: TaskBoard, task: str, *, limit: int = 5) -> int:
    record = board.resolve(task)
    if record is None:
        print(f"task not found: {task}", file=sys.stderr)
        return 2
    entries = ProgressJournal(workspace).list(kind="handoff", task_id=record.task_id, limit=max(limit, 0))
    if not entries:
        print(f"no handoffs for task: {record.title} ({record.task_id})")
        return 0
    print(f"handoffs for: {record.title} ({record.task_id})")
    for entry in entries:
        print(f"- {entry.summary}")
        if entry.next_steps:
            print(f"  next: {entry.next_steps[0]}")
        if entry.blockers:
            print(f"  blocker: {entry.blockers[0]}")
        if entry.decisions:
            print(f"  decision: {entry.decisions[0]}")
    return 0


def _print_task_reviews(workspace: Workspace, board: TaskBoard, task: str, *, limit: int = 5) -> int:
    record = board.resolve(task)
    if record is None:
        print(f"task not found: {task}", file=sys.stderr)
        return 2
    entries = ProgressJournal(workspace).list(kind="manager-review", task_id=record.task_id, limit=max(limit, 0))
    if not entries:
        print(f"no manager reviews for task: {record.title} ({record.task_id})")
        return 0
    print(f"manager reviews for: {record.title} ({record.task_id})")
    for entry in entries:
        status = str(entry.metadata.get("status") or "").strip()
        prefix = f"{status}: " if status else ""
        print(f"- {prefix}{entry.summary}")
        if entry.next_steps:
            print(f"  next: {entry.next_steps[0]}")
        if entry.blockers:
            print(f"  blocker: {entry.blockers[0]}")
        if entry.decisions:
            print(f"  decision: {entry.decisions[0]}")
    return 0


def _record_task_handoff(workspace: Workspace, board: TaskBoard, args: argparse.Namespace) -> int:
    task = board.resolve(args.task)
    if task is None:
        print(f"task not found: {args.task}", file=sys.stderr)
        return 2
    session_id = (args.session or task.session_id or "").strip()
    agent_id = (args.agent or task.agent_id or "").strip()
    try:
        entry = ProgressJournal(workspace).append(
            kind="handoff",
            summary=args.summary,
            project_id=task.project_id,
            task_id=task.task_id,
            session_id=session_id,
            agent_id=agent_id,
            completed=args.completed,
            verified=args.verified,
            next_steps=args.next_steps,
            blockers=args.blockers,
            decisions=args.decisions,
            artifacts=args.artifacts,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    note_record = board.add_note(task.task_id, format_handoff(entry), kind="handoff")
    if session_id:
        objective = task.description or task.title
        SessionStateStore(workspace).upsert_from_progress(entry, objective=objective)

    notes_count = len(note_record.notes) if note_record is not None else 0
    print(f"handoff: {entry.entry_id}")
    print(f"task: {task.title} ({task.task_id}) notes={notes_count}")
    if session_id:
        print(f"session_state: {session_id}")
    return 0


def _record_focus_handoff(workspace: Workspace, registry: FocusRegistry, args: argparse.Namespace) -> int:
    focus = registry.resolve(args.focus)
    if focus is None:
        print(f"focus not found: {args.focus}", file=sys.stderr)
        return 2
    session_id = (args.session or focus.session_id or "").strip()
    agent_id = (args.agent or focus.agent_id or "").strip()
    try:
        entry = ProgressJournal(workspace).append(
            kind="handoff",
            summary=args.summary,
            project_id=focus.project_id,
            focus_id=focus.focus_id,
            session_id=session_id,
            agent_id=agent_id,
            completed=args.completed,
            verified=args.verified,
            next_steps=args.next_steps,
            blockers=args.blockers,
            decisions=args.decisions,
            artifacts=args.artifacts,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    note_record = registry.add_note(focus.focus_id, format_handoff(entry), kind="handoff")
    if session_id:
        objective = focus.description or focus.title
        SessionStateStore(workspace).upsert_from_progress(entry, objective=objective)

    notes_count = len(note_record.notes) if note_record is not None else 0
    print(f"handoff: {entry.entry_id}")
    print(f"focus: {focus.title} ({focus.focus_id}) notes={notes_count}")
    if session_id:
        print(f"session_state: {session_id}")
    return 0


def _record_manager_review(workspace: Workspace, board: TaskBoard, args: argparse.Namespace) -> int:
    task = board.resolve(args.task)
    if task is None:
        print(f"task not found: {args.task}", file=sys.stderr)
        return 2
    session_id = (args.session or task.session_id or "").strip()
    reviewer = (args.reviewer or "manager").strip()
    try:
        entry = ProgressJournal(workspace).append(
            kind="manager-review",
            summary=args.summary,
            project_id=task.project_id,
            task_id=task.task_id,
            session_id=session_id,
            agent_id=reviewer,
            next_steps=args.next_steps,
            blockers=args.blockers,
            decisions=args.decisions,
            artifacts=args.artifacts,
            metadata={"status": args.status, "reviewer": reviewer},
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    note_record = board.add_note(task.task_id, format_review(entry), kind="manager-review")
    if session_id:
        objective = task.description or task.title
        SessionStateStore(workspace).upsert_from_progress(entry, objective=objective)

    notes_count = len(note_record.notes) if note_record is not None else 0
    print(f"manager_review: {entry.entry_id}")
    print(f"task: {task.title} ({task.task_id}) notes={notes_count}")
    if session_id:
        print(f"session_state: {session_id}")
    return 0


def _record_focus_manager_review(workspace: Workspace, registry: FocusRegistry, args: argparse.Namespace) -> int:
    focus = registry.resolve(args.focus)
    if focus is None:
        print(f"focus not found: {args.focus}", file=sys.stderr)
        return 2
    session_id = (args.session or focus.session_id or "").strip()
    reviewer = (args.reviewer or "manager").strip()
    try:
        entry = ProgressJournal(workspace).append(
            kind="manager-review",
            summary=args.summary,
            project_id=focus.project_id,
            focus_id=focus.focus_id,
            session_id=session_id,
            agent_id=reviewer,
            next_steps=args.next_steps,
            blockers=args.blockers,
            decisions=args.decisions,
            artifacts=args.artifacts,
            metadata={"status": args.status, "reviewer": reviewer},
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    note_record = registry.add_note(focus.focus_id, format_review(entry), kind="manager-review")
    if session_id:
        objective = focus.description or focus.title
        SessionStateStore(workspace).upsert_from_progress(entry, objective=objective)

    notes_count = len(note_record.notes) if note_record is not None else 0
    print(f"manager_review: {entry.entry_id}")
    print(f"focus: {focus.title} ({focus.focus_id}) notes={notes_count}")
    if session_id:
        print(f"session_state: {session_id}")
    return 0


def _resolve_approval(
    registry: ApprovalRegistry,
    board: TaskBoard,
    approval: str,
    status: str,
    *,
    note: str = "",
) -> int:
    try:
        record = registry.resolve_request(approval, status=status, note=note)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if record is None:
        print(f"approval not found: {approval}", file=sys.stderr)
        return 2
    if record.task_id:
        task_note = f"Approval {record.status}: {record.approval_id}"
        if note:
            task_note += f"; {note}"
        board.add_note(record.task_id, task_note, kind=f"approval:{record.status}")
    print(f"approval: {record.title} ({record.approval_id}) status={record.status}")
    return 0


def _format_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    raise SystemExit(main())
