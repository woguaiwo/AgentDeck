"""Rule-based organizer for Experience Collections."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from agentdeck.core.config import Workspace
from agentdeck.storage.experience import COLLECTION_KINDS, ExperienceCollection, ExperienceEvent, ExperienceStore
from agentdeck.storage.focus import FocusRegistry
from agentdeck.storage.progress import ProgressEntry, ProgressJournal
from agentdeck.storage.projects import ProjectRegistry
from agentdeck.storage.sessions import SessionRegistry
from agentdeck.storage.tasks import TaskBoard


@dataclass
class ExperienceOrganizerResult:
    collections_created: int = 0
    events_created: int = 0
    edges_created: int = 0
    skipped: int = 0


class ExperienceOrganizer:
    """Extract candidate experience events from durable AgentDeck progress.

    This first organizer is intentionally deterministic: it consumes structured
    progress records, creates one event per unprocessed record, and stores the
    source progress id in event metadata for idempotency.
    """

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.store = ExperienceStore(workspace)
        self.journal = ProgressJournal(workspace)

    def process_once(
        self,
        *,
        limit: int = 50,
        collection: str = "",
        kind: str = "",
        dry_run: bool = False,
    ) -> ExperienceOrganizerResult:
        result = ExperienceOrganizerResult()
        processed_ids = self._processed_progress_ids()
        entries = list(reversed(self.journal.list(limit=max(limit, 1))))
        for entry in entries:
            if entry.entry_id in processed_ids:
                result.skipped += 1
                continue
            collection_record, created = self._collection_for_entry(entry, collection=collection, kind=kind, dry_run=dry_run)
            if collection_record is None:
                result.skipped += 1
                continue
            if created:
                result.collections_created += 1
            previous = self._latest_event(collection_record.collection_id)
            if dry_run:
                result.events_created += 1
                continue
            event = self._record_entry(collection_record, entry)
            result.events_created += 1
            if previous is not None and previous.event_id != event.event_id:
                try:
                    self.store.link_events(
                        previous.event_id,
                        event.event_id,
                        relation="led_to",
                        reason="Organized progress sequence",
                        metadata={"source": "experience_organizer", "strategy": "rules_v1"},
                    )
                    result.edges_created += 1
                except ValueError:
                    pass
        return result

    def serve_forever(
        self,
        *,
        once: bool = False,
        poll_interval: float = 30.0,
        limit: int = 50,
        collection: str = "",
        kind: str = "",
        dry_run: bool = False,
    ) -> None:
        while True:
            self.process_once(limit=limit, collection=collection, kind=kind, dry_run=dry_run)
            if once:
                return
            time.sleep(max(1.0, poll_interval))

    def _collection_for_entry(
        self,
        entry: ProgressEntry,
        *,
        collection: str,
        kind: str,
        dry_run: bool,
    ) -> tuple[ExperienceCollection | None, bool]:
        if collection:
            return self.store.resolve_collection(collection), False

        collection_kind = _choose_collection_kind(entry, override=kind)
        title = self._collection_title(entry, collection_kind)
        existing = self._resolve_scoped_collection(entry, title=title, kind=collection_kind)
        if existing is not None:
            return existing, False
        if dry_run:
            return ExperienceCollection(
                collection_id="dry-run",
                title=title,
                kind=collection_kind,
                purpose=_collection_purpose(entry, collection_kind),
                project_id=entry.project_id,
                worker_id=entry.session_id,
                agent_id=entry.agent_id,
                focus_id=entry.focus_id,
                metadata={"source": "experience_organizer", "strategy": "rules_v1"},
            ), True
        return (
            self.store.create_collection(
                title,
                kind=collection_kind,
                purpose=_collection_purpose(entry, collection_kind),
                project_id=entry.project_id,
                worker_id=entry.session_id,
                agent_id=entry.agent_id,
                focus_id=entry.focus_id,
                metadata={"source": "experience_organizer", "strategy": "rules_v1"},
            ),
            True,
        )

    def _resolve_scoped_collection(self, entry: ProgressEntry, *, title: str, kind: str) -> ExperienceCollection | None:
        candidates = self.store.list_collections(
            project_id=entry.project_id,
            worker_id=entry.session_id,
            agent_id=entry.agent_id,
            focus_id=entry.focus_id,
            kind=kind,
        )
        for record in candidates:
            if record.title == title:
                return record
        return self.store.resolve_collection(title)

    def _collection_title(self, entry: ProgressEntry, kind: str) -> str:
        if entry.focus_id:
            focus = FocusRegistry(self.workspace).resolve(entry.focus_id)
            if focus is not None:
                return f"{focus.title} experience"
        if entry.task_id:
            task = TaskBoard(self.workspace).resolve(entry.task_id)
            if task is not None:
                return f"{task.title} experience"
        if entry.session_id:
            session = SessionRegistry(self.workspace).resolve(entry.session_id)
            if session is not None:
                return f"{session.title} experience"
        if entry.project_id:
            project = ProjectRegistry(self.workspace).resolve(entry.project_id)
            project_title = project.title if project is not None else entry.project_id
            return f"{project_title} {kind.replace('_', ' ')} experience"
        return f"{kind.replace('_', ' ').title()} experience"

    def _record_entry(self, collection: ExperienceCollection, entry: ProgressEntry) -> ExperienceEvent:
        return self.store.record_event(
            collection.collection_id,
            purpose=entry.summary,
            context=f"Organized from AgentDeck progress entry {entry.entry_id} ({entry.kind}).",
            actions=entry.completed,
            result=_result_from_entry(entry),
            analysis=_analysis_from_entry(entry),
            decisions=entry.decisions,
            artifacts=_artifacts_from_entry(entry),
            tags=_tags_from_entry(entry),
            level="meso",
            kind=f"progress_{_clean_token(entry.kind) or 'entry'}",
            status="blocked" if entry.blockers else "done",
            focus_id=entry.focus_id,
            confidence="structured_progress",
            metadata={
                "source": "experience_organizer",
                "strategy": "rules_v1",
                "source_progress_id": entry.entry_id,
                "source_progress_kind": entry.kind,
            },
        )

    def _processed_progress_ids(self) -> set[str]:
        ids: set[str] = set()
        for event in self.store.list_events(limit=100000):
            source_id = str(event.metadata.get("source_progress_id") or "")
            if source_id:
                ids.add(source_id)
        return ids

    def _latest_event(self, collection_id: str) -> ExperienceEvent | None:
        events = self.store.list_events(collection=collection_id, limit=1)
        return events[0] if events else None


def _choose_collection_kind(entry: ProgressEntry, *, override: str = "") -> str:
    if override:
        clean = _clean_token(override)
        if clean not in COLLECTION_KINDS:
            raise ValueError(f"unsupported experience collection kind: {override}")
        return clean
    text = _entry_text(entry)
    if any(word in text for word in ("research", "explore", "探索", "实验", "hypothesis", "假设")):
        return "research_exploration"
    if any(word in text for word in ("decision", "plan", "design", "decide", "决策", "设计", "计划")):
        return "decision_planning"
    if entry.blockers or any(word in text for word in ("error", "restart", "daemon", "ops", "维护", "阻塞")):
        return "ops_maintenance"
    if any(word in text for word in ("test", "fix", "implement", "code", "cli", "telegram", "工程", "测试", "实现")):
        return "engineering_change"
    return "scratch"


def _collection_purpose(entry: ProgressEntry, kind: str) -> str:
    target = entry.focus_id or entry.task_id or entry.session_id or entry.project_id or "general work"
    return f"Auto-organized {kind.replace('_', ' ')} events for {target}."


def _result_from_entry(entry: ProgressEntry) -> str:
    if entry.verified:
        return "; ".join(entry.verified[:3])
    if entry.completed:
        return "; ".join(entry.completed[:3])
    if entry.blockers:
        return f"Blocked: {entry.blockers[0]}"
    return entry.summary


def _analysis_from_entry(entry: ProgressEntry) -> str:
    parts: list[str] = []
    if entry.next_steps:
        parts.append("Next: " + "; ".join(entry.next_steps[:3]))
    if entry.blockers:
        parts.append("Blockers: " + "; ".join(entry.blockers[:3]))
    return "\n".join(parts)


def _artifacts_from_entry(entry: ProgressEntry) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    for value in entry.artifacts:
        text = value.strip()
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


def _tags_from_entry(entry: ProgressEntry) -> list[str]:
    tags = ["progress", "auto_organized", _clean_token(entry.kind)]
    if entry.project_id:
        tags.append(f"project_{_clean_token(entry.project_id)}")
    if entry.focus_id:
        tags.append("focus")
    if entry.task_id:
        tags.append("task")
    return [tag for tag in tags if tag]


def _entry_text(entry: ProgressEntry) -> str:
    return " ".join(
        [
            entry.kind,
            entry.summary,
            " ".join(entry.completed),
            " ".join(entry.verified),
            " ".join(entry.next_steps),
            " ".join(entry.blockers),
            " ".join(entry.decisions),
            " ".join(entry.artifacts),
        ]
    ).casefold()


def _clean_token(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value).strip().lower()).strip("_.-")
