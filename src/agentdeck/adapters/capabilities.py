"""Capability declarations for backend adapters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AdapterCapabilities:
    """Feature switches exposed by an adapter implementation."""

    streams_events: bool = False
    supports_resume: bool = False
    supports_resume_last: bool = False
    requires_provider_session: bool = False
    emits_provider_session_id: bool = False
    supports_approval_record: bool = False
    supports_approval_bypass: bool = False
    supports_mid_run_approval_response: bool = False
    supports_cancellation: bool = False
    supports_session_list: bool = False


ECHO_CAPABILITIES = AdapterCapabilities(streams_events=True, supports_cancellation=True)

CODEX_EXEC_CAPABILITIES = AdapterCapabilities(
    streams_events=True,
    supports_resume=True,
    supports_resume_last=True,
    requires_provider_session=True,
    emits_provider_session_id=True,
    supports_approval_record=True,
    supports_approval_bypass=True,
    supports_mid_run_approval_response=False,
    supports_cancellation=True,
)

KIMI_PRINT_CAPABILITIES = AdapterCapabilities(
    streams_events=True,
    supports_resume=True,
    supports_resume_last=True,
    requires_provider_session=True,
    emits_provider_session_id=True,
    supports_approval_record=False,
    supports_approval_bypass=True,
    supports_mid_run_approval_response=False,
    supports_cancellation=True,
)


_CAPABILITY_BY_ADAPTER = {
    "echo": ECHO_CAPABILITIES,
    "codex": CODEX_EXEC_CAPABILITIES,
    "codex-exec": CODEX_EXEC_CAPABILITIES,
    "codex_exec": CODEX_EXEC_CAPABILITIES,
    "kimi": KIMI_PRINT_CAPABILITIES,
    "kimi-print": KIMI_PRINT_CAPABILITIES,
    "kimi_print": KIMI_PRINT_CAPABILITIES,
}


def adapter_capabilities_for_name(adapter_name: str | None) -> AdapterCapabilities:
    """Return capabilities for a configured adapter name."""

    if not adapter_name:
        return AdapterCapabilities()
    normalized = adapter_name.strip().lower().replace("_", "-")
    return _CAPABILITY_BY_ADAPTER.get(normalized, AdapterCapabilities())


def adapter_requires_provider_session(adapter_name: str | None) -> bool:
    """Return whether an adapter needs a provider session id for resume."""

    return adapter_capabilities_for_name(adapter_name).requires_provider_session
