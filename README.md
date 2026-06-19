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
- Telegram long-polling interface for project/task/approval/run commands
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
python -m agentdeck tasks create "Summarize repository" --project motionx
python -m agentdeck run --project motionx "Summarize this repository"
python -m agentdeck run --task <task_id> "Continue"
python -m agentdeck projects list
python -m agentdeck agents list
python -m agentdeck tasks list
python -m agentdeck sessions list
python -m agentdeck approvals list
AGENTDECK_TELEGRAM_TOKEN="<bot-token>" python -m agentdeck telegram serve
python -m agentdeck run --adapter codex --cwd "$PWD" "Summarize this repository"
python -m agentdeck run --adapter kimi --cwd "$PWD" "Summarize this repository"
python -m agentdeck run --adapter codex --cwd "$PWD" --resume-last "Continue"
python -m agentdeck run --adapter codex --cwd "$PWD" --approval-mode record "Show me what approval is needed"
python -m agentdeck memory add "Project rule" "Keep shared memory concise."
python -m agentdeck memory list
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

Supported commands:

```text
/projects
/agents [project]
/tasks [project]
/task <task_id>
/run <task_id> <message>
/approvals [pending|approved|rejected]
/approval <approval_id>
/approve <approval_id> [note]
/reject <approval_id> [note]
```

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

## Agent Model

The current practical default is one `owner` agent per project. Agent records
already include `role` and `team_id`, so later teams can add planners,
developers, testers, reviewers, and managers without changing the storage
model.

## Project And Task Model

Projects represent source directories such as `Motion-X`, `ReID`, or `WHAM`.
Tasks represent units of work inside a project. `agentdeck run --task <task>`
uses the task's project and agent defaults, then attaches the resulting session
back to the task so later runs can continue the same work.
