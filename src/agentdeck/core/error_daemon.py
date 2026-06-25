"""Background error handling daemon."""

from __future__ import annotations

import time
from collections.abc import Callable

from agentdeck.core.config import Workspace
from agentdeck.core.events import AgentEvent, EventKind
from agentdeck.storage.errors import ErrorDecisionRecord, ErrorIncidentRecord, ErrorIncidentStore, decide_incident
from agentdeck.storage.jobs import JobRecord, JobRegistry


Notifier = Callable[[ErrorIncidentRecord, ErrorDecisionRecord], None]


class ErrorHandlingDaemon:
    """Poll pending incidents and apply bounded decisions."""

    def __init__(self, workspace: Workspace, *, notifier: Notifier | None = None) -> None:
        self.workspace = workspace
        self.store = ErrorIncidentStore(workspace)
        self.jobs = JobRegistry(workspace)
        self.notifier = notifier

    def process_once(self, *, limit: int = 20) -> int:
        processed = 0
        for incident in reversed(self.store.list(status="pending", limit=limit)):
            self.process_incident(incident)
            processed += 1
        return processed

    def process_incident(self, incident: ErrorIncidentRecord) -> ErrorDecisionRecord:
        decision = decide_incident(incident)
        self.apply_decision(incident, decision)
        return decision

    def serve_forever(self, *, once: bool = False, poll_interval: float = 5.0) -> None:
        while True:
            self.process_once()
            if once:
                return
            time.sleep(max(0.5, poll_interval))

    def apply_decision(self, incident: ErrorIncidentRecord, decision: ErrorDecisionRecord) -> None:
        self.store.append_decision(incident, decision)
        self.store.mark_resolved(incident.incident_id)
        self.jobs.update_metadata(
            incident.job_id,
            {
                "error_decision": decision.action,
                "error_decision_reason": decision.reason,
                "error_incident_id": incident.incident_id,
                "error_kind": incident.error_kind,
            },
        )
        if decision.action in {"pause_auto", "need_user"}:
            _pause_auto(self.workspace, incident)
        if decision.notify_user and self.notifier is not None:
            self.notifier(incident, decision)


def _pause_auto(workspace: Workspace, incident: ErrorIncidentRecord) -> None:
    if incident.interface == "telegram" and incident.chat_id:
        try:
            from agentdeck.interfaces.telegram import TelegramChatStateStore

            bot_id = str(incident.job_metadata.get("bot_id") or "")
            TelegramChatStateStore(workspace, scope=bot_id).disable_auto(incident.chat_id)
        except Exception:
            return
    if incident.interface == "web" and incident.task_id:
        try:
            from agentdeck.interfaces.web import _read_web_auto_states, _write_web_auto_states

            states = _read_web_auto_states(workspace)
            states.pop(incident.task_id, None)
            _write_web_auto_states(workspace, states)
        except Exception:
            return


def format_error_decision_message(incident: ErrorIncidentRecord, decision: ErrorDecisionRecord) -> str:
    lines = [
        "AgentDeck error handler:",
        f"action: {decision.action}",
        f"kind: {incident.error_kind}",
    ]
    if incident.job_id:
        lines.append(f"job: {incident.job_id}")
    if decision.reason:
        lines.append(f"reason: {decision.reason}")
    if incident.hint:
        lines.append(f"hint: {incident.hint}")
    if incident.text:
        lines.append(f"error: {_one_line(incident.text, 240)}")
    return "\n".join(lines)


def first_error_event(events: list[AgentEvent]) -> AgentEvent | None:
    for event in events:
        if event.kind == EventKind.ERROR:
            return event
    return None


def create_error_incident_for_job(
    workspace: Workspace,
    *,
    job: JobRecord,
    event: AgentEvent,
    adapter: str = "",
) -> ErrorIncidentRecord:
    return ErrorIncidentStore(workspace).create_from_event(job=job, event=event, adapter=adapter)


def event_should_fail_job(event: AgentEvent | None) -> bool:
    if event is None:
        return False
    payload = dict(event.payload or {})
    if bool(payload.get("nonfatal")):
        return False
    if str(payload.get("error_kind") or "") == "usage_warning":
        return False
    return True


def _one_line(text: str, max_chars: int) -> str:
    clean = " ".join((text or "").split())
    if len(clean) <= max_chars:
        return clean
    return clean[: max_chars - 1].rstrip() + "..."
