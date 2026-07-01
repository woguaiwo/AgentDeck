"""Clone capsules for transferring worker state across provider sessions."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agentdeck.adapters.codex_exec import CodexExecAdapter
from agentdeck.adapters.echo import EchoAdapter
from agentdeck.adapters.kimi_print import KimiPrintAdapter
from agentdeck.core.approvals import ApprovalMode
from agentdeck.core.config import Workspace
from agentdeck.core.events import EventKind
from agentdeck.storage.agents import AgentRegistry
from agentdeck.storage.experience import ExperienceStore
from agentdeck.storage.focus import FocusRegistry
from agentdeck.storage.progress import ProgressEntry, ProgressJournal
from agentdeck.storage.projects import ProjectRegistry
from agentdeck.storage.project_state import ProjectStateStore
from agentdeck.storage.provider_sessions import ProviderEvent, ProviderEventBundle, read_provider_event_bundle
from agentdeck.storage.session_state import SessionStateCard, SessionStateStore
from agentdeck.storage.sessions import SessionRecord, SessionRegistry
from agentdeck.storage.tasks import TaskBoard


SCHEMA_VERSION = 1


@dataclass
class CloneCapsule:
    clone_id: str
    source_session_id: str
    source_worker_id: str
    provider: str
    provider_session_id: str
    provider_session_kind: str
    project_dir: str
    title: str = ""
    strategy: str = "rules"
    created_at: float = field(default_factory=time.time)
    schema_version: int = SCHEMA_VERSION
    current: dict[str, Any] = field(default_factory=dict)
    constraints: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    progress: list[dict[str, Any]] = field(default_factory=list)
    recent_raw_turns: list[dict[str, Any]] = field(default_factory=list)
    provider_summary: list[str] = field(default_factory=list)
    experience_collections: list[dict[str, Any]] = field(default_factory=list)
    source_references: list[dict[str, Any]] = field(default_factory=list)
    validation: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CloneCapsule":
        return cls(
            clone_id=str(data["clone_id"]),
            source_session_id=str(data["source_session_id"]),
            source_worker_id=str(data.get("source_worker_id") or ""),
            provider=str(data.get("provider") or ""),
            provider_session_id=str(data.get("provider_session_id") or ""),
            provider_session_kind=str(data.get("provider_session_kind") or ""),
            project_dir=str(data.get("project_dir") or ""),
            title=str(data.get("title") or ""),
            strategy=str(data.get("strategy") or "rules"),
            created_at=float(data.get("created_at") or time.time()),
            schema_version=int(data.get("schema_version") or SCHEMA_VERSION),
            current=dict(data.get("current") or {}),
            constraints=_string_list(data.get("constraints")),
            decisions=_string_list(data.get("decisions")),
            progress=[dict(item) for item in data.get("progress") or [] if isinstance(item, dict)],
            recent_raw_turns=[dict(item) for item in data.get("recent_raw_turns") or [] if isinstance(item, dict)],
            provider_summary=_string_list(data.get("provider_summary")),
            experience_collections=[dict(item) for item in data.get("experience_collections") or [] if isinstance(item, dict)],
            source_references=[dict(item) for item in data.get("source_references") or [] if isinstance(item, dict)],
            validation=dict(data.get("validation") or {}),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CloneStore:
    """Filesystem store for clone capsules and rendered context."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def path_for(self, clone_id: str) -> Path:
        return self.workspace.clones_dir / _slug(clone_id)

    def capsule_path(self, clone_id: str) -> Path:
        return self.path_for(clone_id) / "clone_capsule.json"

    def context_path(self, clone_id: str) -> Path:
        return self.path_for(clone_id) / "clone_context.md"

    def sources_path(self, clone_id: str) -> Path:
        return self.path_for(clone_id) / "sources.json"

    def validation_path(self, clone_id: str) -> Path:
        return self.path_for(clone_id) / "validation.json"

    def write(self, capsule: CloneCapsule) -> CloneCapsule:
        self.workspace.ensure()
        root = self.path_for(capsule.clone_id)
        root.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(self.capsule_path(capsule.clone_id), capsule.to_dict())
        self.context_path(capsule.clone_id).write_text(render_clone_context(capsule), encoding="utf-8")
        _write_json_atomic(self.sources_path(capsule.clone_id), capsule.source_references)
        _write_json_atomic(self.validation_path(capsule.clone_id), capsule.validation)
        return capsule

    def get(self, clone_id: str) -> CloneCapsule | None:
        path = self.capsule_path(clone_id)
        if not path.exists():
            matches = [item for item in self.workspace.clones_dir.glob(f"{_slug(clone_id)}*") if item.is_dir()]
            if matches:
                path = matches[0] / "clone_capsule.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            return CloneCapsule.from_dict(data)
        except (KeyError, TypeError, ValueError):
            return None

    def list(self) -> list[CloneCapsule]:
        if not self.workspace.clones_dir.exists():
            return []
        capsules: list[CloneCapsule] = []
        for path in self.workspace.clones_dir.iterdir():
            if not path.is_dir():
                continue
            capsule = self.get(path.name)
            if capsule is not None:
                capsules.append(capsule)
        return sorted(capsules, key=lambda item: item.created_at, reverse=True)

    def cleanup_tmp(self, *, older_than_seconds: float = 86400.0) -> int:
        root = self.workspace.tmp_dir / "clone-runs"
        if not root.exists():
            return 0
        cutoff = time.time() - max(older_than_seconds, 0.0)
        removed = 0
        for path in root.iterdir():
            if not path.is_dir():
                continue
            manifest = path / "manifest.json"
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("agentdeck_temp_type") != "clone_summarizer":
                continue
            try:
                if path.stat().st_mtime > cutoff:
                    continue
                shutil.rmtree(path)
                removed += 1
            except OSError:
                continue
        _remove_empty_parents(root, stop_at=self.workspace.tmp_dir)
        return removed


def create_rules_clone_capsule(
    workspace: Workspace,
    session: str,
    *,
    home: str | Path | None = None,
    recent_turns: int = 12,
    max_provider_chars: int = 24000,
    collections: list[str] | None = None,
) -> CloneCapsule:
    """Build a deterministic clone capsule for one AgentDeck worker/session."""
    workspace.ensure()
    record = SessionRegistry(workspace).resolve(session)
    if record is None:
        raise ValueError(f"worker not found: {session}")
    if not record.provider_session_id:
        raise ValueError(f"worker has no provider session id: {record.session_id}")

    provider = _provider_for_record(record)
    bundle = read_provider_event_bundle(
        provider=provider,
        provider_session_id=record.provider_session_id,
        project_dir=record.project_dir,
        home=home,
    )

    card = SessionStateStore(workspace).get(record.session_id)
    current = _current_from_record(workspace, record, card)
    project_state, project_decisions = _project_memory(workspace, current.get("project_id", ""))
    if project_state:
        current["project_state"] = project_state

    progress_entries = _progress_for_record(workspace, record, card)
    provider_summary = _provider_summary(bundle, max_chars=max_provider_chars)
    recent = _recent_provider_turns(bundle, limit=recent_turns)
    experience_collections = _experience_collections_for_record(workspace, record, current, collections=collections or [])
    references = _source_references(workspace, record, card, progress_entries, bundle)

    decisions = _merge_strings(
        list(current.get("decisions") or []),
        [item.decision for item in project_decisions],
        [decision for entry in progress_entries for decision in entry.decisions],
    )
    constraints = _merge_strings(list(current.get("constraints") or []), list((project_state or {}).get("constraints") or []))

    capsule = CloneCapsule(
        clone_id=f"clone-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
        source_session_id=record.session_id,
        source_worker_id=record.agent_id,
        provider=provider,
        provider_session_id=record.provider_session_id,
        provider_session_kind=record.provider_session_kind,
        project_dir=record.project_dir,
        title=record.title,
        current=_redact_any(current),
        constraints=_redact_list(constraints),
        decisions=_redact_list(decisions),
        progress=[_progress_to_clone_dict(entry) for entry in progress_entries],
        recent_raw_turns=[_redact_any(item) for item in recent],
        provider_summary=_redact_list(provider_summary),
        experience_collections=[_redact_any(item) for item in experience_collections],
        source_references=references,
        validation={},
        metadata={
            "provider_events": len(bundle.events) if bundle is not None else 0,
            "home_override": bool(home),
            "rules_version": 1,
        },
    )
    capsule.validation = validate_clone_capsule(capsule)
    return CloneStore(workspace).write(capsule)


def create_ai_clone_capsule(
    workspace: Workspace,
    session: str,
    *,
    home: str | Path | None = None,
    recent_turns: int = 12,
    max_provider_chars: int = 24000,
    summarizer_adapter: str = "codex-exec",
    codex_bin: str = "codex",
    kimi_bin: str = "kimi",
    model: str | None = None,
    keep_debug: bool = False,
    collections: list[str] | None = None,
) -> CloneCapsule:
    """Try an ephemeral AI summarizer, falling back to the rules capsule."""
    capsule = create_rules_clone_capsule(
        workspace,
        session,
        home=home,
        recent_turns=recent_turns,
        max_provider_chars=max_provider_chars,
        collections=collections,
    )
    run_id = f"summarizer-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    run_dir = workspace.tmp_dir / "clone-runs" / run_id
    provider_home = run_dir / "provider-home"
    run_dir.mkdir(parents=True, exist_ok=True)
    provider_home.mkdir(parents=True, exist_ok=True)
    manifest = {
        "agentdeck_temp_type": "clone_summarizer",
        "run_id": run_id,
        "clone_id": capsule.clone_id,
        "created_at": time.time(),
        "strategy": "ai",
        "summarizer_adapter": summarizer_adapter,
    }
    _write_json_atomic(run_dir / "manifest.json", manifest)
    _write_json_atomic(run_dir / "sanitized_bundle.json", capsule.to_dict())
    prompt = _summarizer_prompt(capsule)
    (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

    status = "failed"
    error = ""
    raw_output = ""
    try:
        raw_output = asyncio.run(
            _run_ephemeral_summarizer(
                workspace,
                prompt,
                adapter_name=summarizer_adapter,
                run_id=run_id,
                cwd=Path(capsule.project_dir or "."),
                provider_home=provider_home,
                codex_bin=codex_bin,
                kimi_bin=kimi_bin,
                model=model,
            )
        )
        (run_dir / "raw_output.txt").write_text(redact_text(raw_output), encoding="utf-8")
        patch = _parse_summarizer_output(raw_output)
        if patch:
            capsule = _apply_summarizer_patch(capsule, patch)
            status = "applied"
        else:
            status = "fallback_rules"
            error = "summarizer output did not contain a JSON object"
    except Exception as exc:  # pragma: no cover - exercised through CLI fallback behavior
        status = "fallback_rules"
        error = str(exc)
    finally:
        if not keep_debug:
            shutil.rmtree(run_dir, ignore_errors=True)
            _remove_empty_parents(run_dir.parent, stop_at=workspace.tmp_dir)

    capsule.strategy = "ai"
    capsule.metadata["ai_summarizer"] = {
        "run_id": run_id,
        "adapter": summarizer_adapter,
        "status": status,
        "error": redact_text(error),
        "debug_kept": keep_debug,
    }
    capsule.validation = validate_clone_capsule(capsule)
    return CloneStore(workspace).write(capsule)


def validate_clone_capsule(capsule: CloneCapsule) -> dict[str, Any]:
    serialized = json.dumps(capsule.to_dict(), ensure_ascii=False, sort_keys=True)
    findings = detect_secrets(serialized)
    missing = []
    if not capsule.source_session_id:
        missing.append("source_session_id")
    if not capsule.provider_session_id:
        missing.append("provider_session_id")
    if not capsule.current.get("next_step") and not capsule.current.get("objective"):
        missing.append("current.objective_or_next_step")
    return {"ok": not findings and not missing, "secret_findings": findings, "missing": missing}


def spawn_worker_from_clone(
    workspace: Workspace,
    capsule: CloneCapsule,
    *,
    agent_id: str = "",
    session_id: str = "",
    title: str = "",
    project: str = "",
    project_dir: str = "",
    adapter: str = "",
    role: str = "developer",
    team_id: str = "default",
    model: str = "",
    sandbox: str = "",
    approval_mode: str = "fail",
    replace: bool = False,
) -> dict[str, str]:
    """Prepare a fresh AgentDeck worker from one clone capsule."""
    current = dict(capsule.current or {})
    project_record = ProjectRegistry(workspace).resolve(project) if project else None
    if project and project_record is None:
        raise ValueError(f"project not found: {project}")

    resolved_project_id = project_record.project_id if project_record is not None else str(current.get("project_id") or "")
    resolved_project_dir = project_dir or capsule.project_dir or (project_record.project_dir if project_record is not None else ".")
    resolved_title = title or f"{capsule.title or capsule.source_worker_id or capsule.clone_id} clone"
    resolved_agent_id = agent_id or f"{_slug(resolved_title)}-{capsule.clone_id[-6:]}"
    resolved_adapter = adapter or _adapter_from_provider(capsule.provider)
    resolved_session_id = session_id or f"{resolved_agent_id}-session"
    context_path = CloneStore(workspace).context_path(capsule.clone_id)

    agent_registry = AgentRegistry(workspace)
    agent = agent_registry.upsert(
        agent_id=resolved_agent_id,
        title=resolved_title,
        project_id=resolved_project_id,
        role=role,
        team_id=team_id,
        adapter=resolved_adapter,
        project_dir=resolved_project_dir,
        model=model,
        sandbox=sandbox,
        approval_mode=approval_mode,
        resume_policy="latest",
        replace=replace,
    )
    agent_registry.set_role_template(
        agent.agent_id,
        "\n".join(
            [
                f"You are a worker spawned from AgentDeck clone capsule {capsule.clone_id}.",
                "Use the injected session state card as inherited context; the native provider history is intentionally not copied.",
                f"Full rendered clone context is available at {context_path}.",
            ]
        ),
    )

    session = SessionRegistry(workspace).create_prepared_session(
        agent_id=agent.agent_id,
        adapter=resolved_adapter,
        project_dir=resolved_project_dir,
        title=resolved_title,
        session_id=resolved_session_id,
        project_id=resolved_project_id,
        replace=replace,
        metadata={
            "clone_prepared": True,
            "clone_id": capsule.clone_id,
            "clone_context_path": str(context_path),
            "source_session_id": capsule.source_session_id,
            "source_worker_id": capsule.source_worker_id,
            "source_provider": capsule.provider,
            "source_provider_session_id": capsule.provider_session_id,
        },
    )
    state = SessionStateCard(
        session_id=session.session_id,
        objective=str(current.get("objective") or capsule.title or resolved_title),
        current_state=_clone_spawn_current_state(capsule, context_path),
        next_step=str(current.get("next_step") or "Start a fresh provider session from this clone context."),
        task_id=str(current.get("task_id") or ""),
        focus_id=str(current.get("focus_id") or ""),
        project_id=resolved_project_id,
        agent_id=agent.agent_id,
        blockers=_string_list(current.get("blockers")),
        verified_work=_string_list(current.get("verified_work")),
        active_artifacts=_string_list(current.get("active_artifacts")),
        decisions=_merge_strings(_string_list(current.get("decisions")), list(capsule.decisions[:8])),
        metadata={
            "clone_id": capsule.clone_id,
            "clone_context_path": str(context_path),
            "source_session_id": capsule.source_session_id,
            "source_worker_id": capsule.source_worker_id,
        },
    )
    SessionStateStore(workspace).write(state)

    prompt = "Continue from the AgentDeck clone context. Confirm the inherited objective, then proceed with the next useful step."
    return {
        "agent_id": agent.agent_id,
        "session_id": session.session_id,
        "clone_id": capsule.clone_id,
        "clone_context_path": str(context_path),
        "first_run": f"agentdeck run --agent {agent.agent_id} {json.dumps(prompt, ensure_ascii=False)}",
    }


def render_clone_context(capsule: CloneCapsule) -> str:
    lines = [
        f"# Clone Context: {capsule.title or capsule.source_session_id}",
        "",
        f"- clone_id: {capsule.clone_id}",
        f"- source_session_id: {capsule.source_session_id}",
        f"- worker: {capsule.source_worker_id or '-'}",
        f"- provider: {capsule.provider}",
        f"- provider_session_id: {capsule.provider_session_id}",
        f"- project_dir: {capsule.project_dir}",
        "",
        "## Current",
    ]
    for key in ["objective", "current_state", "next_step", "task_id", "focus_id", "project_id"]:
        value = capsule.current.get(key)
        if value:
            lines.append(f"- {key}: {value}")
    _append_list_section(lines, "Constraints", capsule.constraints)
    _append_list_section(lines, "Decisions", capsule.decisions)
    _append_list_section(lines, "Provider Summary", capsule.provider_summary)
    if capsule.experience_collections:
        lines.extend(["", "## Experience Collections"])
        for collection in capsule.experience_collections:
            lines.append(
                f"- {collection.get('title') or collection.get('collection_id')}: "
                f"{collection.get('kind') or 'collection'}"
            )
            purpose = str(collection.get("purpose") or "").strip()
            if purpose:
                lines.append(f"  purpose: {purpose}")
            for event in list(collection.get("events") or [])[:5]:
                if not isinstance(event, dict):
                    continue
                result = str(event.get("result") or "").strip()
                suffix = f" -> {result}" if result else ""
                lines.append(f"  - {event.get('purpose')}{suffix}")
    if capsule.progress:
        lines.extend(["", "## Progress"])
        for item in capsule.progress[:10]:
            lines.append(f"- {item.get('kind')}: {item.get('summary')}")
            next_steps = item.get("next_steps") or []
            if next_steps:
                lines.append(f"  next: {next_steps[0]}")
    if capsule.recent_raw_turns:
        lines.extend(["", "## Recent Raw Turns"])
        for item in capsule.recent_raw_turns:
            text = str(item.get("text") or "").replace("\n", " ")
            lines.append(f"- {item.get('role') or item.get('kind')}: {text}")
    return "\n".join(lines).rstrip() + "\n"


def detect_secrets(text: str) -> list[str]:
    patterns = {
        "openai_key": r"sk-[A-Za-z0-9_\-]{20,}",
        "telegram_token": r"\b\d{6,12}:[A-Za-z0-9_\-]{25,}\b",
        "generic_secret_assignment": r"(?i)\b(api[_-]?key|token|secret|password|credential)\b[\"'\s:=]+[A-Za-z0-9_./+=\-]{16,}",
        "bearer_token": r"(?i)\bbearer\s+[A-Za-z0-9_./+=\-]{16,}",
    }
    findings = []
    for name, pattern in patterns.items():
        if re.search(pattern, text):
            findings.append(name)
    return findings


def redact_text(text: str) -> str:
    redacted = str(text)
    redacted = re.sub(r"sk-[A-Za-z0-9_\-]{20,}", "[REDACTED_OPENAI_KEY]", redacted)
    redacted = re.sub(r"\b\d{6,12}:[A-Za-z0-9_\-]{25,}\b", "[REDACTED_TELEGRAM_TOKEN]", redacted)
    redacted = re.sub(
        r"(?i)(\b(?:api[_-]?key|token|secret|password|credential)\b[\"'\s:=]+)[A-Za-z0-9_./+=\-]{16,}",
        r"\1[REDACTED_SECRET]",
        redacted,
    )
    redacted = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9_./+=\-]{16,}", "Bearer [REDACTED_SECRET]", redacted)
    return redacted


async def _run_ephemeral_summarizer(
    workspace: Workspace,
    prompt: str,
    *,
    adapter_name: str,
    run_id: str,
    cwd: Path,
    provider_home: Path,
    codex_bin: str,
    kimi_bin: str,
    model: str | None,
) -> str:
    adapter = _build_summarizer_adapter(
        adapter_name,
        cwd=cwd,
        provider_home=provider_home,
        codex_bin=codex_bin,
        kimi_bin=kimi_bin,
        model=model,
    )
    final_text = ""
    chunks: list[str] = []
    errors: list[str] = []
    async for event in adapter.send(
        prompt,
        agent_id="agentdeck-memory-summarizer",
        session_id=run_id,
        workspace=workspace,
    ):
        if event.kind == EventKind.ASSISTANT_DELTA and event.text:
            chunks.append(event.text)
        elif event.kind == EventKind.ASSISTANT_FINAL:
            final_text = event.text
        elif event.kind == EventKind.ERROR and event.text:
            errors.append(event.text)
    if final_text:
        return final_text
    if chunks:
        return "".join(chunks)
    if errors:
        raise RuntimeError("; ".join(errors)[-1000:])
    return ""


def _build_summarizer_adapter(
    adapter_name: str,
    *,
    cwd: Path,
    provider_home: Path,
    codex_bin: str,
    kimi_bin: str,
    model: str | None,
):
    name = adapter_name.strip().lower()
    if name == "echo":
        return EchoAdapter()
    env = _isolated_provider_env(provider_home)
    if name in {"codex", "codex-exec"}:
        return CodexExecAdapter(
            codex_bin=codex_bin,
            cwd=cwd,
            model=model,
            sandbox="read-only",
            approval_mode=ApprovalMode.FAIL,
            skip_git_repo_check=True,
            env=env,
        )
    if name in {"kimi", "kimi-print"}:
        return KimiPrintAdapter(
            kimi_bin=kimi_bin,
            cwd=cwd,
            model=model,
            approval_mode=ApprovalMode.FAIL,
            env=env,
        )
    raise ValueError(f"unsupported summarizer adapter: {adapter_name}")


def _isolated_provider_env(provider_home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["HOME"] = str(provider_home)
    env["CODEX_HOME"] = str(provider_home / ".codex")
    env["KIMI_HOME"] = str(provider_home / ".kimi")
    env["AGENTDECK_SUMMARIZER_HOME"] = str(provider_home)
    return env


def _summarizer_prompt(capsule: CloneCapsule) -> str:
    payload = json.dumps(capsule.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    return (
        "You are an AgentDeck memory summarizer. Read the sanitized clone capsule below and return only JSON.\n"
        "Do not include secrets. Do not invent completed work. Improve compactness and remove provider-only noise.\n"
        "Return a JSON object with optional keys: current, constraints, decisions, provider_summary, recent_raw_turns.\n"
        "Keep all values grounded in the input.\n\n"
        f"{payload}"
    )


def _parse_summarizer_output(raw_output: str) -> dict[str, Any]:
    text = raw_output.strip()
    if not text:
        return {}
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _apply_summarizer_patch(capsule: CloneCapsule, patch: dict[str, Any]) -> CloneCapsule:
    allowed = {"current", "constraints", "decisions", "provider_summary", "recent_raw_turns"}
    sanitized = _redact_any({key: patch[key] for key in allowed if key in patch})
    if isinstance(sanitized.get("current"), dict):
        capsule.current.update(dict(sanitized["current"]))
    if isinstance(sanitized.get("constraints"), list):
        capsule.constraints = _merge_strings(capsule.constraints, _string_list(sanitized["constraints"]))
    if isinstance(sanitized.get("decisions"), list):
        capsule.decisions = _merge_strings(capsule.decisions, _string_list(sanitized["decisions"]))
    if isinstance(sanitized.get("provider_summary"), list):
        capsule.provider_summary = _string_list(sanitized["provider_summary"])
    if isinstance(sanitized.get("recent_raw_turns"), list):
        capsule.recent_raw_turns = [dict(item) for item in sanitized["recent_raw_turns"] if isinstance(item, dict)]
    return capsule


def _current_from_record(workspace: Workspace, record: SessionRecord, card: SessionStateCard | None) -> dict[str, Any]:
    task_id = card.task_id if card is not None and card.task_id else _task_id_for_session(workspace, record.session_id)
    focus_id = card.focus_id if card is not None and card.focus_id else _focus_id_for_session(workspace, record.session_id)
    project_id = card.project_id if card is not None and card.project_id else str(record.metadata.get("project_id") or "")
    current = {
        "objective": card.objective if card is not None else "",
        "current_state": card.current_state if card is not None else record.last_assistant_final,
        "next_step": card.next_step if card is not None else "",
        "task_id": task_id,
        "focus_id": focus_id,
        "project_id": project_id,
        "agent_id": card.agent_id if card is not None and card.agent_id else record.agent_id,
        "status": record.status,
        "title": record.title,
        "last_user_message": record.last_user_message,
        "verified_work": card.verified_work if card is not None else [],
        "active_artifacts": card.active_artifacts if card is not None else [],
        "blockers": card.blockers if card is not None else [],
        "decisions": card.decisions if card is not None else [],
        "constraints": [],
    }
    task = TaskBoard(workspace).get(task_id) if task_id else None
    if task is not None:
        current["task"] = task.to_dict()
        current["objective"] = current["objective"] or task.description or task.title
        current["project_id"] = current["project_id"] or task.project_id
    focus = FocusRegistry(workspace).get(focus_id) if focus_id else None
    if focus is not None:
        current["focus"] = focus.to_dict()
        current["objective"] = current["objective"] or focus.description or focus.title
        current["project_id"] = current["project_id"] or focus.project_id
    return current


def _project_memory(workspace: Workspace, project_id: str) -> tuple[dict[str, Any] | None, list[Any]]:
    if not project_id:
        return None, []
    store = ProjectStateStore(workspace)
    state = store.get(project_id)
    decisions = store.decisions(project_id, limit=20)
    return (state.to_dict() if state is not None else None), decisions


def _progress_for_record(
    workspace: Workspace,
    record: SessionRecord,
    card: SessionStateCard | None,
) -> list[ProgressEntry]:
    journal = ProgressJournal(workspace)
    entries: list[ProgressEntry] = []
    entries.extend(journal.list(session_id=record.session_id, limit=20))
    task_id = card.task_id if card is not None else ""
    focus_id = card.focus_id if card is not None else ""
    if task_id:
        entries.extend(journal.list(task_id=task_id, limit=20))
    if focus_id:
        entries.extend(journal.list(focus_id=focus_id, limit=20))
    unique: dict[str, ProgressEntry] = {}
    for entry in entries:
        unique[entry.entry_id] = entry
    return sorted(unique.values(), key=lambda item: item.created_at, reverse=True)[:20]


def _experience_collections_for_record(
    workspace: Workspace,
    record: SessionRecord,
    current: dict[str, Any],
    *,
    collections: list[str],
) -> list[dict[str, Any]]:
    store = ExperienceStore(workspace)
    selected = []
    seen: set[str] = set()
    for value in collections:
        summary = store.collection_summary(value, event_limit=8)
        if summary and summary["collection_id"] not in seen:
            selected.append(summary)
            seen.add(summary["collection_id"])
    if collections:
        return selected

    candidates = []
    candidates.extend(store.list_collections(worker_id=record.session_id))
    candidates.extend(store.list_collections(worker_id=record.agent_id))
    if record.agent_id:
        candidates.extend(store.list_collections(agent_id=record.agent_id))
    focus_id = str(current.get("focus_id") or "")
    if focus_id:
        candidates.extend(store.list_collections(focus_id=focus_id))
    project_id = str(current.get("project_id") or record.metadata.get("project_id") or "")
    if project_id:
        candidates.extend(store.list_collections(project_id=project_id))

    for collection in sorted(candidates, key=lambda item: item.updated_at, reverse=True):
        if collection.collection_id in seen:
            continue
        summary = store.collection_summary(collection.collection_id, event_limit=8)
        if summary:
            selected.append(summary)
            seen.add(collection.collection_id)
        if len(selected) >= 5:
            break
    return selected


def _provider_summary(bundle: ProviderEventBundle | None, *, max_chars: int) -> list[str]:
    if bundle is None:
        return []
    summaries = [
        event.text
        for event in bundle.events
        if event.text and (event.kind in {"compacted", "turn_context"} or event.role in {"summary", "_checkpoint"})
    ]
    return _bounded_strings(_merge_strings(summaries), max_chars=max_chars)


def _recent_provider_turns(bundle: ProviderEventBundle | None, *, limit: int) -> list[dict[str, Any]]:
    if bundle is None:
        return []
    allowed_roles = {"user", "assistant", "system", "tool", "_checkpoint"}
    events = [event for event in bundle.events if event.text and (event.role in allowed_roles or event.kind in {"compacted"})]
    return [
        {
            "provider": event.provider,
            "kind": event.kind,
            "role": event.role,
            "text": _limit_text(event.text, 2000),
            "source": event.source,
        }
        for event in events[-max(limit, 0) :]
    ]


def _source_references(
    workspace: Workspace,
    record: SessionRecord,
    card: SessionStateCard | None,
    progress: list[ProgressEntry],
    bundle: ProviderEventBundle | None,
) -> list[dict[str, Any]]:
    references = [
        {"kind": "agentdeck_session", "path": str(SessionRegistry(workspace).path), "id": record.session_id},
    ]
    if card is not None:
        references.append(
            {"kind": "agentdeck_session_state", "path": str(SessionStateStore(workspace).path_for(record.session_id))}
        )
    if progress:
        references.append({"kind": "agentdeck_progress", "path": str(ProgressJournal(workspace).path), "count": len(progress)})
    if bundle is not None:
        references.append(
            {
                "kind": "provider_session",
                "provider": bundle.provider,
                "path": bundle.source_path,
                "event_count": len(bundle.events),
            }
        )
    return references


def _task_id_for_session(workspace: Workspace, session_id: str) -> str:
    matches = [task for task in TaskBoard(workspace).list() if task.session_id == session_id]
    return matches[0].task_id if matches else ""


def _focus_id_for_session(workspace: Workspace, session_id: str) -> str:
    matches = [focus for focus in FocusRegistry(workspace).list() if focus.session_id == session_id]
    return matches[0].focus_id if matches else ""


def _provider_for_record(record: SessionRecord) -> str:
    provider = str(record.metadata.get("provider") or "").strip().lower()
    if provider:
        return provider
    if record.adapter.startswith("codex") or record.provider_session_kind.startswith("codex"):
        return "codex"
    if record.adapter.startswith("kimi") or record.provider_session_kind.startswith("kimi"):
        return "kimi"
    return record.adapter


def _adapter_from_provider(provider: str) -> str:
    normalized = provider.strip().lower()
    if normalized == "codex":
        return "codex"
    if normalized == "kimi":
        return "kimi"
    return "echo"


def _clone_spawn_current_state(capsule: CloneCapsule, context_path: Path) -> str:
    current = dict(capsule.current or {})
    parts = [
        str(current.get("current_state") or "").strip(),
        f"Prepared from clone capsule {capsule.clone_id}; rendered context: {context_path}",
    ]
    provider_summary = [str(item).strip() for item in capsule.provider_summary[:3] if str(item).strip()]
    if provider_summary:
        parts.append("Provider summary: " + " / ".join(provider_summary))
    return " ".join(part for part in parts if part)


def _progress_to_clone_dict(entry: ProgressEntry) -> dict[str, Any]:
    return _redact_any(
        {
            "entry_id": entry.entry_id,
            "kind": entry.kind,
            "summary": entry.summary,
            "completed": entry.completed,
            "verified": entry.verified,
            "next_steps": entry.next_steps,
            "blockers": entry.blockers,
            "decisions": entry.decisions,
            "artifacts": entry.artifacts,
            "created_at": entry.created_at,
        }
    )


def _append_list_section(lines: list[str], title: str, values: list[str]) -> None:
    if not values:
        return
    lines.extend(["", f"## {title}"])
    for value in values:
        lines.append(f"- {value}")


def _bounded_strings(values: list[str], *, max_chars: int) -> list[str]:
    kept: list[str] = []
    used = 0
    for value in values:
        clean = _limit_text(value, max_chars)
        if not clean:
            continue
        if used + len(clean) > max_chars and kept:
            break
        kept.append(clean)
        used += len(clean)
    return kept


def _limit_text(value: str, limit: int) -> str:
    clean = " ".join(str(value).strip().split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def _merge_strings(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group:
            clean = _limit_text(value, 4000)
            if not clean or clean in seen:
                continue
            seen.add(clean)
            merged.append(clean)
    return merged


def _redact_any(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_redact_any(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_any(item) for key, item in value.items()}
    return value


def _redact_list(values: list[str]) -> list[str]:
    return [redact_text(value) for value in values if str(value).strip()]


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip().lower()).strip(".-") or "clone"


def _remove_empty_parents(path: Path, *, stop_at: Path) -> None:
    stop = stop_at.resolve()
    current = path
    while True:
        try:
            if current.resolve() == stop:
                break
            current.rmdir()
        except OSError:
            break
        current = current.parent
