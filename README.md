# AgentDeck

AgentDeck is an AI agent control plane for running coding agents as persistent,
remotely managed project teams.

It is inspired by TeleAgent, but it does not treat AI CLIs as terminal screens
to scrape. AgentDeck separates the interface, runtime state, adapters, memory,
approvals, tasks, and project orchestration into explicit system components.

## Core Capabilities

AgentDeck is organized around five layers:

1. **AI Agent Harness**
   - Provides adapter interfaces for coding agents.
   - Currently supports Codex non-interactive runs with `codex exec --json` and
     Kimi non-interactive runs with `kimi --print --output-format stream-json`.
   - Keeps room for Claude Code, DeepSeek HTTP mode, and legacy TUI adapters.

2. **Project Control Plane**
   - Manages projects, agents, tasks, sessions, jobs, approvals, events, and
     workspace state.
   - Turns one-off terminal sessions into traceable project workflows.

3. **Multi-Agent Collaboration**
   - Models project roles such as owner, manager, planner, developer, tester,
     executor, and reviewer.
   - Supports task boards, handoff notes, manager reviews, decision logs,
     project state cards, and session state cards.

4. **Remote Interface And Navigation Assistant**
   - Exposes Telegram and local Web control surfaces, with CLI and future TUI
     interfaces sharing the same state model.
   - Lets each Telegram bot use its own assistant before a chat selects a task.
   - Allows the assistant to execute a narrow whitelist of safe routing commands.

5. **Autonomous Progress Engine**
   - Supports auto loop mode and auto-by-task mode.
   - Can keep background jobs moving after SSH disconnects.
   - Records progress and stops for explicit human input when needed.

In short:

```text
AgentDeck =
  AI Agent Harness
  + Project Control Plane
  + Multi-Agent Collaboration
  + Remote Control Interface
  + Autonomous Progress Engine
```

## Install

From the downloaded AgentDeck repository:

```bash
cd /path/to/AgentDeck
./install.sh
```

The installer performs an editable install with the current Python environment,
initializes the default platform workspace, and verifies the `agentdeck` command
when it is available on `PATH`.

If your Python scripts directory is not on `PATH`, the installer prints the
exact `export PATH=...` line. To let it append that line to your shell rc file:

```bash
./install.sh --shell-config
```

Useful variants:

```bash
./install.sh --run-tests
./install.sh --python /path/to/python
```

After installation:

```bash
agentdeck doctor
agentdeck web serve
agentdeck telegram start
agentdeck telegram status
agentdeck telegram restart
agentdeck telegram stop
```

For development without installing:

```bash
PYTHONPATH=src python -m agentdeck doctor
PYTHONPATH=src python -m unittest discover -s tests
```

## Workspace Model

AgentDeck is a platform-level control plane. By default, commands use the
workspace inside the AgentDeck install/source directory:

```text
<AgentDeck>/.agentdeck/
```

That workspace stores projects, agents, tasks, sessions, Telegram bot configs,
jobs, approvals, memory, handoffs, and decisions. The directory where you run
`agentdeck` is treated as a project working directory only when you register it
with `projects create --cwd ...` or pass it as an adapter `--cwd`.

Use `--workspace /path/to/.agentdeck` or `AGENTDECK_WORKSPACE=/path/to/.agentdeck`
only when you intentionally want a separate control-plane workspace. Project
directories may optionally contain a lightweight `.agentdeck.toml` file for
directory-specific integration hints such as future TUI profiles or special
adapter commands; this file is not the main AgentDeck state store.

## Quick Start

```bash
agentdeck init
agentdeck doctor
agentdeck projects create motionx --title "Motion-X" --cwd "$PWD" --default-agent owner
agentdeck agents create owner --title "Motion-X Owner" --project motionx --adapter codex --cwd "$PWD"
agentdeck tasks create "Summarize repository" --project motionx
agentdeck run --project motionx "Summarize this repository"
agentdeck run --task <task_id> "Continue"
agentdeck projects list
agentdeck tasks list
agentdeck sessions list
agentdeck approvals list
```

## Import Existing Provider Sessions

AgentDeck can adopt Codex/Kimi sessions that were created before AgentDeck
managed the project. First scan local provider state by the original provider
working directory:

```bash
agentdeck sessions scan --cwd /old/project/path
agentdeck sessions scan --provider codex --cwd /old/project/path
agentdeck sessions scan --provider kimi --cwd /old/project/path
```

Then bind the chosen provider session to an AgentDeck project, agent, and
optional task:

```bash
agentdeck sessions import \
  --provider codex \
  --provider-session <codex_thread_id> \
  --project <project_id> \
  --agent <agent_id> \
  --title "Imported Codex session"

agentdeck sessions import \
  --provider kimi \
  --provider-session <kimi_session_id> \
  --project <project_id> \
  --task <task_id>
```

After import, `agentdeck run --session <provider_session_id> "Continue"` resumes
the underlying provider session while recording the run in AgentDeck. If a
project moved directories, scan with the old provider cwd and import with the
new AgentDeck project or `--cwd`.

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
agentdeck approvals list
agentdeck approvals show <approval_id>
agentdeck approvals approve <approval_id> "approved by operator"
agentdeck approvals reject <approval_id> "too risky"
```

Approving a request records the decision for audit and later remote interfaces.
It does not implicitly rerun with bypassed permissions.

## Web Interface

The Web interface is a local browser control console backed by the same
workspace registries as CLI and Telegram. It has no external Python dependency:

```bash
agentdeck web serve
```

By default it listens on `127.0.0.1:8765`:

```text
http://127.0.0.1:8765
```

The Web console shows projects, tasks, agents, sessions, recent jobs, and
pending approvals, plus JSON endpoints for future frontends:

```text
/
/api/overview
/api/health
```

It also exposes guarded form actions for common operator work:

- Create a project.
- Archive and restore project records while preserving child tasks, agents,
  sessions, and memory links.
- Globally rename Project, Task, Agent, and Session ids.
- Run a prompt on a task or send a message to the AgentDeck assistant.
- Start/stop Web auto mode for a task.
- Approve/reject pending approval requests.
- Cancel queued or running Web jobs.

Global id rename updates AgentDeck registries and references across tasks,
agents, sessions, jobs, approvals, progress journal, session state cards,
project state, memory owner directories, and Telegram chat state.

To reach it from a phone or another machine without buying a domain, expose the
local port with a private network or temporary tunnel:

```bash
# Tailscale private access
agentdeck web serve --host 0.0.0.0 --port 8765

# Cloudflare Quick Tunnel for temporary public testing
cloudflared tunnel --url http://127.0.0.1:8765
```

Keep the default `127.0.0.1` binding unless a tunnel or private network is
handling access control.

## Telegram Interface

The Telegram interface uses Bot API long polling and requires no extra Python
dependency:

```bash
export AGENTDECK_TELEGRAM_TOKEN="<bot-token>"
export AGENTDECK_TELEGRAM_ALLOWED_CHATS="<chat-id>,<chat-id>"
agentdeck telegram serve
```

When saved bots exist for the current server, `telegram serve` and
`telegram start` serve all of them by default. Each bot uses its own assistant
before a chat selects a task. Pass `--bot <bot_id>` only when you want to debug
or run one bot.

`telegram serve` is foreground mode for debugging. To keep the Telegram
controller alive after disconnecting SSH, start it as a detached workspace
daemon:

```bash
agentdeck telegram start
agentdeck telegram status
agentdeck telegram stop
```

The daemon pid and log are stored under `.agentdeck/telegram/`. The daemon keeps
receiving phone commands and running queued jobs after the launching SSH session
disconnects. If the server itself restarts or the daemon crashes, start it
again; unfinished Telegram jobs are marked `interrupted`.
Use `agentdeck telegram restart` after updating AgentDeck to reload the daemon
without changing the saved bot configuration. Restart refuses to run while
Telegram jobs are queued or running; wait, cancel them, or use
`agentdeck telegram restart --force-jobs` when interruption is acceptable.

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
/use session <session_id or list #>
/assistant
/current
/status
/restart
/restart force
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
/session use <session_id or list #>
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
/job resume <job_id or list #> [message]
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
After a session is selected with `/use session <list #>` or
`/session use <list #>`, plain text messages resume that session even when the
session has no linked task. The success reply and `/current` both show the
selected session so phone users can see whether they are talking to the
assistant or a project agent. If no task or session is selected and a default
assistant exists, plain text messages are sent to that assistant so it can help
route the user to the right project, agent, task, or session. If no assistant
exists, the bot returns a setup hint instead of sending the text to an agent.
The bot continues receiving Telegram messages while the backend agent runs, then
sends the final result back to the chat when the job finishes. Job records are
stored under `.agentdeck/jobs/`; if AgentDeck restarts while a job is still
queued or running, that job is marked `interrupted`.

Interrupted jobs can be restarted from Telegram with
`/job resume <job_id or list #> [message]`, or simply `/job resume` for the
latest interrupted job in the chat. If the interrupted job already had a saved
session, AgentDeck resumes that session. If no safe session is available, it
starts a new job from the task context and the original prompt.

Create the default assistant with:

```bash
agentdeck assistant setup --adapter codex --cwd /data/lyxie/AgentDeck
agentdeck assistant setup-bots --adapter codex --cwd /data/lyxie/AgentDeck
agentdeck assistant refresh
agentdeck assistant show
```

The assistant is just an AgentDeck agent with a manager-style routing prompt.
It receives AgentDeck context and can suggest exact CLI or Telegram commands.
When the assistant is confident, it may place safe Telegram control commands on
their own final lines as `AGENTDECK_ACTION: /command ...`. AgentDeck strips
those marker lines from the user-visible reply and executes only a small
whitelist of routing commands, such as `/projects`, `/tasks`, `/status`,
`/use project`, `/use agent`, `/use task`, `/use session`, `/project new`,
`/agent new`, `/task new`, and `/restart`. `/restart` refuses to reload while
Telegram jobs are active, and assistant actions cannot force it. It will not
execute `/run`, `/auto`, approval, cancellation, shell, destructive, or
secret-revealing commands from assistant output. If an assistant claims that it
switched project, task, agent, or session without emitting an executable
`AGENTDECK_ACTION`, Telegram sends an extra warning telling the user to confirm
with `/current`. Use `agentdeck assistant refresh` after upgrading AgentDeck to
update saved assistant prompts to the latest routing rules.

Bot records are scoped to the server where they are imported or added. Use
`assistant setup-bots` to create and bind one assistant per saved bot on the
current server. Starting Telegram with `telegram start` serves all current
server bots and uses each bot's assistant before a task is selected, falling
back to the default `assistant` only when the bot has no assistant binding.

For phone use, `/status` is the main control panel. It shows the current
project, agent, task, latest job, auto mode, pending approvals, and recent
sessions. `/projects`, `/agents`, `/tasks`, `/jobs`, `/sessions`, and
`/approvals` store numbered lists for the current chat, so commands like
`/use project <list #>`, `/use agent <list #>`, `/use task <list #>`,
`/use session <list #>`, `/run <list #> <message>`, `/job <list #>`,
`/job resume <list #>`, `/cancel <list #>`, and `/resume <list #> <message>`
avoid copying long ids.

Projects, agents, and tasks can also be created from Telegram:

```text
/project new motionx /data/lyxie/Motion-X Motion-X
/agent new developer codex developer Motion-X Developer
/task new Fix data loading
```

After selecting a task once with `/use task <list #>` or creating one with
`/task new <title>`, you can send a plain text message to the current agent.
After selecting a session with `/use session <list #>`, plain text messages
resume that session. `/sessions` shows the linked task title under each session
when AgentDeck can find one, so a renamed task and an older session title remain
distinguishable. `/run <message>` is still supported. `/job` shows the latest job
in the chat, and `/cancel` cancels the latest queued or running job.
Use `/assistant` or `/use assistant` to clear the current task and route plain
text messages back to the assistant. Selecting a different project or agent with
`/use project ...` or `/use agent ...` also clears the current task/session, so
the next plain message goes through the assistant until you select or create a
task or select a session.

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

Saved Telegram bot configs are platform workspace-local operational config:

```bash
agentdeck telegram bots add minsys-bot3 \
  --token "<bot-token>" \
  --allowed-chat-id "<chat-id>"
agentdeck telegram bots import /data/lyxie/TeleAgent/Manager.txt
agentdeck assistant setup-bots --adapter codex --cwd /data/lyxie/AgentDeck
agentdeck telegram bots list
agentdeck telegram start
```

The registry is stored at `.agentdeck/telegram/bots.json`. Listing bots redacts
tokens; the file itself contains secrets and should not be committed.

`/cancel <job_id>` cancels queued jobs immediately. For running Codex/Kimi
print jobs, AgentDeck requests adapter-level process termination and records the
job as `cancelled` once the adapter stops. Adapters that do not support
cancellation yet may still finish normally after `cancel_requested`.

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
agentdeck memory compact-task <task_id> \
  --title "Loader fix context snapshot" \
  --pin
agentdeck memory list --scope project --owner <project_id>
agentdeck memory disable <memory_id_or_path>
agentdeck memory enable <memory_id_or_path>
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
agentdeck projects update-state motionx \
  --goal "Ship a stable loader fix" \
  --phase "implementation" \
  --focus "Keep executor work scoped" \
  --next "Run regression tests" \
  --constraint "Do not share raw transcripts as memory"
agentdeck projects decide motionx \
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
agentdeck tasks handoff <task_id> \
  --summary "State card storage is in place" \
  --completed "Added session-state JSON files" \
  --verified "Ran focused tests" \
  --next "Inject state cards into auto prompts" \
  --decision "Keep handoffs as task notes plus journal entries" \
  --artifact "src/agentdeck/storage/session_state.py"
```

Use `agentdeck sessions state <session_id>` to inspect the compact
state card used for resume and future auto-mode context. Use
`agentdeck tasks context <task_id>` to see the bounded context block
that will be injected into the next task run, and
`agentdeck tasks handoffs <task_id>` to inspect recent handoff
summaries.

`tasks manager-review` records the manager side of the loop. Handoffs are
executor reports; manager reviews are compact direction, approval, or requested
changes for the next execution run:

```bash
agentdeck tasks manager-review <task_id> \
  --summary "The storage shape is acceptable; keep the next patch narrow" \
  --status approved \
  --next "Add Telegram visibility for reviews"
agentdeck tasks reviews <task_id>
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
agentdeck agents template <agent_id> \
  --prompt "Act as manager: keep goals, constraints, reviews, and decisions current."
agentdeck agents template <agent_id> --clear
```

Telegram supports `/agent template <prompt>` for the current agent and
`/agent template clear` to restore the role default.

## Project And Task Model

Projects represent source directories such as `Motion-X`, `ReID`, or `WHAM`.
Tasks represent units of work inside a project. `agentdeck run --task <task>`
uses the task's project and agent defaults, then attaches the resulting session
back to the task so later runs can continue the same work.
