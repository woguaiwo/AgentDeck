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
python -m agentdeck run --adapter codex --cwd "$PWD" "Summarize this repository"
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

For development without installing:

```bash
PYTHONPATH=src python -m agentdeck doctor
PYTHONPATH=src python -m pytest
```

## Workspace Layout

```text
.agentdeck/
├── config.toml
├── events/
│   └── events.jsonl
├── sessions/
├── inbox/
├── board/
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
