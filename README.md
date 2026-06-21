# AgentDeck

AgentDeck is a remote control plane for AI agent teams.

It is the next-generation architecture inspired by TeleAgent: instead of treating
AI CLIs as terminal screens to scrape, AgentDeck separates interfaces, runtime
state, model adapters, memory, approvals, and task orchestration.

## Goals

- Control multiple AI agents across projects from Telegram, CLI, and later a web UI.
- Support multiple backends through adapters: Codex, Claude Code, Kimi Code, DeepSeek, and legacy TUI wrappers.
- Keep shared memory explicit, scoped, searchable, and auditable.
- Make long-running work visible through sessions, task cards, event logs, approvals, and handoff notes.
- Allow cheap models to do routine work while stronger models plan, review, and arbitrate.

## Current Status

This repository starts with a minimal core:

- Workspace initialization under `.agentdeck/`
- Structured event model
- Adapter protocol
- Debug echo adapter
- Codex non-interactive adapter using `codex exec --json`
- Kimi non-interactive adapter using `kimi --print --output-format stream-json`
- Project registry for managing multiple source projects from one workspace
- Agent registry with project defaults, role, team, and resume policy
- Task board with project, agent, session, status, priority, and notes
- Session registry with human-readable titles and provider session ids
- Approval registry for backend approval requests and explicit decisions
- Telegram long-polling interface with persisted background run jobs
- Markdown memory store with `user`, `project`, `team`, `agent`, and `task` scopes
- JSONL event log
- CLI smoke path

The first real adapter targets should be:

1. `CodexExecAdapter` using `codex exec --json` / `codex exec resume`
2. `KimiPrintAdapter` using `kimi --print --output-format stream-json`
3. `ClaudePrintAdapter` using `claude --print --output-format stream-json`
4. `DeepSeekHttpAdapter` using `deepseek serve --http`
5. `LegacyTuiAdapter` as a fallback for existing TeleAgent behavior

## Quick Start

```bash
python -m agentdeck init
python -m agentdeck doctor
python -m agentdeck run "hello from AgentDeck"
python -m agentdeck projects create motionx --title "Motion-X" --cwd "$PWD" --default-agent owner
python -m agentdeck agents create owner --title "Motion-X Owner" --project motionx --adapter codex
python -m agentdeck agents template owner --prompt "Act as manager: keep direction clear and review executor handoffs."
python -m agentdeck tasks create "Summarize repository" --project motionx
python -m agentdeck run --project motionx "Summarize this repository"
python -m agentdeck run --task <task_id> "Continue"
python -m agentdeck projects list
python -m agentdeck agents list
python -m agentdeck tasks list
python -m agentdeck sessions list
python -m agentdeck approvals list
python -m agentdeck projects update-state motionx --goal "Ship the loader fix" --focus "Keep executor work aligned" --next "Run tests"
python -m agentdeck projects decide motionx "Use task handoffs as executor reports" --reason "Managers need compact review points"
python -m agentdeck projects decisions motionx
python -m agentdeck tasks handoff <task_id> --summary "Made progress" --completed "Changed storage" --next "Wire prompt injection"
python -m agentdeck tasks manager-review <task_id> --summary "Scope looks right" --next "Run regression tests"
python -m agentdeck tasks context <task_id>
python -m agentdeck tasks handoffs <task_id>
python -m agentdeck tasks reviews <task_id>
python -m agentdeck sessions state <session_id>
python -m agentdeck assistant setup --adapter codex --cwd "$PWD"
python -m agentdeck telegram bots import /path/to/bots.toml
python -m agentdeck telegram bots list
python -m agentdeck telegram start --bot minsys-bot3
AGENTDECK_TELEGRAM_TOKEN="<bot-token>" python -m agentdeck telegram serve
python -m agentdeck run --adapter codex --cwd "$PWD" "Summarize this repository"
python -m agentdeck run --adapter kimi --cwd "$PWD" "Summarize this repository"
python -m agentdeck run --adapter codex --cwd "$PWD" --resume-last "Continue"
python -m agentdeck run --adapter codex --cwd "$PWD" --approval-mode record "Show me what approval is needed"
python -m agentdeck memory add "Project rule" "Keep shared memory concise." --pin
python -m agentdeck memory compact-task <task_id> --title "Loader fix snapshot" --pin
python -m agentdeck memory list --scope project --owner motionx
```

## Codex Approval Modes

`CodexExecAdapter` is non-interactive, so AgentDeck cannot yet answer mid-run
approval prompts. The adapter exposes explicit modes instead of pretending the
approval loop is solved:

- `--approval-mode fail` is the default. If Codex asks for approval, AgentDeck
  records the request and stops the run with a clear error.
- `--approval-mode record` records approval events and lets Codex continue if
  Codex can proceed without an answer.
- `--approval-mode bypass` passes Codex
  `--dangerously-bypass-approvals-and-sandbox`. Use this only in an isolated,
  trusted environment.

When a backend requests approval, AgentDeck records it in the approval registry:

```bash
python -m agentdeck approvals list
python -m agentdeck approvals show <approval_id>
python -m agentdeck approvals approve <approval_id> "approved by operator"
python -m agentdeck approvals reject <approval_id> "too risky"
```

Approving a request records the decision for audit and later remote interfaces.
It does not implicitly rerun with bypassed permissions.

## Telegram Interface

The Telegram interface uses Bot API long polling and requires no extra Python
dependency:

```bash
export AGENTDECK_TELEGRAM_TOKEN="<bot-token>"
export AGENTDECK_TELEGRAM_ALLOWED_CHATS="<chat-id>,<chat-id>"
python -m agentdeck telegram serve
```

`telegram serve` is foreground mode for debugging. To keep the Telegram
controller alive after disconnecting SSH, start it as a detached workspace
daemon:

```bash
python -m agentdeck telegram start
python -m agentdeck telegram status
python -m agentdeck telegram stop
```

The daemon pid and log are stored under `.agentdeck/telegram/`. The daemon keeps
receiving phone commands and running queued jobs after the launching SSH session
disconnects. If the server itself restarts or the daemon crashes, start it
again; unfinished Telegram jobs are marked `interrupted`.

Supported commands:

```text
/projects
/project <project_id or list #>
/project new <project_id> <cwd> [title]
/projectstate [project]
/decisions [project]
/decide <decision text>
/use project <project_id or list #>
/agents [project]
/agent <agent_id or list #>
/agent new <agent_id> [adapter] [role] [title]
/use agent <agent_id or list #>
/tasks [project]
/task <task_id>
/task new <task title>
/newtask <task title>
/use <task_id or exact task title>
/use task <task_id or list #>
/current
/status
/list
/context [task]
/memories [task]
/memory disable <memory #, id, title, or path>
/memory enable <memory #, id, title, or path>
/compact [--pin] [title]
/handoffs [task]
/review <manager review summary>
/reviews [task]
/sessions [agent]
/session <session_id or list #>
/resume <session_id or list #> <message>
/auto start [hours]
/auto task [hours]
/auto -h start [hours]
/auto --human start [hours]
/auto <hours>
/auto status
/auto prompt <message>
/auto end
<plain text message>
/run <task_id> <message>
/run <list #> <message>
/run <message>
/jobs
/job <job_id>
/job <list #>
/job
/cancel <job_id>
/cancel <list #>
/cancel
/approvals [pending|approved|rejected]
/approval <approval_id or list #>
/approve <approval_id or list #> [note]
/reject <approval_id or list #> [note]
```

`/run` starts a background job and returns immediately with a job id. After a
task is selected, plain text messages are treated the same as `/run <message>`.
If no task is selected and a default assistant exists, plain text messages are
sent to that assistant so it can help route the user to the right project,
agent, and task. If no assistant exists, the bot returns a setup hint instead
of sending the text to an agent. The bot continues receiving Telegram messages
while the backend agent runs, then sends the final result back to the chat when
the job finishes. Job records are stored under `.agentdeck/jobs/`; if AgentDeck
restarts while a job is still queued or running, that job is marked
`interrupted`.

Create the default assistant with:

```bash
python -m agentdeck assistant setup --adapter codex --cwd /data/lyxie/AgentDeck
python -m agentdeck assistant show
```

The assistant is just an AgentDeck agent with a manager-style routing prompt.
It receives AgentDeck context and can suggest exact CLI or Telegram commands,
but it does not execute arbitrary control commands by itself.

For phone use, `/status` is the main control panel. It shows the current
project, agent, task, latest job, auto mode, pending approvals, and recent
sessions. `/projects`, `/agents`, `/tasks`, `/jobs`, `/sessions`, and
`/approvals` store numbered lists for the current chat, so commands like
`/use project <list #>`, `/use agent <list #>`, `/use task <list #>`,
`/run <list #> <message>`, `/job <list #>`, `/cancel <list #>`, and
`/resume <list #> <message>` avoid copying long ids.

Projects, agents, and tasks can also be created from Telegram:

```text
/project new motionx /data/lyxie/Motion-X Motion-X
/agent new developer codex developer Motion-X Developer
/task new Fix data loading
```

After selecting a task once with `/use task <list #>` or creating one with
`/task new <title>`, you can send a plain text message to the current agent.
`/run <message>` is still supported. `/job` shows the latest job in the chat,
and `/cancel` cancels the latest queued or running job.

Auto mode is a task-level job loop. After selecting a task with `/use`, send
`/auto start` to start one run immediately and then keep starting the next run
after each successful completion. `/auto 7.5` enables the same loop for 7.5
hours. `/auto end` stops future automatic jobs; it does not kill a job already
running, so use `/cancel` for that. The default auto prompt asks the agent to
continue useful work and record important progress in project logs or task
notes. Use `/auto prompt <message>` to replace that instruction.

Use `/auto task [hours]` when the loop should stop once the agent judges the
current task to be sufficiently complete. AgentDeck injects a completion marker
into that auto prompt; if the backend returns the marker, AgentDeck strips it
from the user-visible reply, stops auto mode, and moves the task to `review`.

Auto mode defaults to automatic approval: auto-created jobs run with
`approval_mode=bypass`, so they do not stop on backend approval prompts. Use
`/auto -h start`, `/auto --human start`, or `/auto -h 2` when the automatic loop
should stop and wait for a human approval decision instead.

Approval commands also support numbered selections after `/approvals`. When a
Telegram user sends `/approve <list #>` for a pending approval that belongs to a task,
AgentDeck records the approval and starts a follow-up background job with
`approval_mode=bypass`. This is a new run against the same task/session when it
is safely resumable; it is not an in-place continuation of a stopped provider
process.

Saved Telegram bot configs are workspace-local operational config:

```bash
python -m agentdeck telegram bots add minsys-bot3 \
  --token "<bot-token>" \
  --allowed-chat-id "<chat-id>"
python -m agentdeck telegram bots import /data/lyxie/TeleAgent/Manager.txt
python -m agentdeck telegram bots list
python -m agentdeck telegram start --bot minsys-bot3
```

The registry is stored at `.agentdeck/telegram/bots.json`. Listing bots redacts
tokens; the file itself contains secrets and should not be committed.

`/cancel <job_id>` cancels queued jobs immediately. For running Codex/Kimi
print jobs, AgentDeck requests adapter-level process termination and records the
job as `cancelled` once the adapter stops. Adapters that do not support
cancellation yet may still finish normally after `cancel_requested`.

For development without installing:

```bash
PYTHONPATH=src python -m agentdeck doctor
PYTHONPATH=src python -m pytest
```

## Workspace Layout

```text
.agentdeck/
├── config.toml
├── projects/
│   └── registry.json
├── approvals/
│   └── registry.json
├── jobs/
│   └── registry.json
├── journal/
│   └── progress.jsonl
├── session-state/
│   └── <session_id>.json
├── project-state/
│   ├── <project_id>.json
│   └── <project_id>-decisions.jsonl
├── telegram/
│   ├── bots.json
│   └── state.json
├── agents/
│   └── registry.json
├── events/
│   └── events.jsonl
├── sessions/
│   └── registry.json
├── inbox/
├── board/
│   └── tasks.json
└── memory/
    ├── user/
    ├── projects/
    ├── teams/
    ├── agents/
    └── tasks/
```

## Design Principle

Agents should not share raw chat transcripts as memory. They should share:

- concise durable facts
- task state
- handoff artifacts
- evidence
- structured events
- explicit approval decisions

Raw transcripts remain available for audit, but runtime prompts should receive
bounded, relevant memory only.

Use `memory compact-task <task_id>` to turn structured task context into a
durable Markdown memory snapshot. It uses project state, session state,
handoffs, and manager reviews; it does not copy raw chat transcripts or
recursively copy older durable memory snapshots:

```bash
python -m agentdeck memory compact-task <task_id> \
  --title "Loader fix context snapshot" \
  --pin
python -m agentdeck memory list --scope project --owner <project_id>
python -m agentdeck memory disable <memory_id_or_path>
python -m agentdeck memory enable <memory_id_or_path>
```

Telegram supports the same phone workflow with `/compact [--pin] [title]` for
the current task and `/memories` to inspect the durable memories that future
task runs will retrieve. Use `/compact --pin <title>` when a memory should be
prioritized in future retrieval. Pinned memories are injected before ordinary
recent memories; `disabled: true` memories are skipped. Use
`/memory disable 1` after `/memories` to soft-prune a noisy memory without
deleting its Markdown file, and `/memory enable 1` to restore it while the
recent list is still active.

## Handoffs And Session State

Project-level state is manager-owned direction for all agents in one project:

```bash
python -m agentdeck projects update-state motionx \
  --goal "Ship a stable loader fix" \
  --phase "implementation" \
  --focus "Keep executor work scoped" \
  --next "Run regression tests" \
  --constraint "Do not share raw transcripts as memory"
python -m agentdeck projects decide motionx \
  "Use task handoffs as executor reports" \
  --reason "Managers need compact review points"
```

Use `projects state <project>` and `projects decisions <project>` to inspect
the project state and decision log. Telegram supports `/projectstate`,
`/decisions`, and `/decide <decision text>` for phone control.

`tasks handoff` records compact progress for manager/executor workflows. It
writes a phone-readable handoff note back to the task, appends a structured
entry to `.agentdeck/journal/progress.jsonl`, and updates the attached session
state card when the task has a session:

```bash
python -m agentdeck tasks handoff <task_id> \
  --summary "State card storage is in place" \
  --completed "Added session-state JSON files" \
  --verified "Ran focused tests" \
  --next "Inject state cards into auto prompts" \
  --decision "Keep handoffs as task notes plus journal entries" \
  --artifact "src/agentdeck/storage/session_state.py"
```

Use `python -m agentdeck sessions state <session_id>` to inspect the compact
state card used for resume and future auto-mode context. Use
`python -m agentdeck tasks context <task_id>` to see the bounded context block
that will be injected into the next task run, and
`python -m agentdeck tasks handoffs <task_id>` to inspect recent handoff
summaries.

`tasks manager-review` records the manager side of the loop. Handoffs are
executor reports; manager reviews are compact direction, approval, or requested
changes for the next execution run:

```bash
python -m agentdeck tasks manager-review <task_id> \
  --summary "The storage shape is acceptable; keep the next patch narrow" \
  --status approved \
  --next "Add Telegram visibility for reviews"
python -m agentdeck tasks reviews <task_id>
```

When a run is attached to a task, AgentDeck appends a bounded context block to
the adapter prompt. The block includes project state, recent project decisions,
the task objective, the attached session state card, recent handoffs, and recent
manager reviews. It also retrieves recent durable Markdown memories from the
task's project, team, agent, and task scopes. The original user prompt remains
the prompt stored in the event log and session registry, so operational context
does not pollute user-visible history.
Telegram `/run`, plain text messages after `/use`, and auto-created jobs all
use this same run path. On Telegram, `/context` shows the current task's
injected context, `/handoffs` shows executor reports, and `/review` plus
`/reviews` records and lists manager feedback.

## Agent Model

The current practical default is one `owner` agent per project, but AgentDeck
now treats `role` as runtime guidance. Common roles such as `manager`,
`planner`, `executor`, `developer`, `tester`, `reviewer`, and `owner` have
default templates that are injected into task run context.

Custom templates can override the default for one agent:

```bash
python -m agentdeck agents template <agent_id> \
  --prompt "Act as manager: keep goals, constraints, reviews, and decisions current."
python -m agentdeck agents template <agent_id> --clear
```

Telegram supports `/agent template <prompt>` for the current agent and
`/agent template clear` to restore the role default.

## Project And Task Model

Projects represent source directories such as `Motion-X`, `ReID`, or `WHAM`.
Tasks represent units of work inside a project. `agentdeck run --task <task>`
uses the task's project and agent defaults, then attaches the resulting session
back to the task so later runs can continue the same work.
