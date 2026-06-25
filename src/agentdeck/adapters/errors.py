"""Common adapter error classification."""

from __future__ import annotations

from enum import Enum


class AdapterErrorKind(str, Enum):
    """Stable error classes surfaced by adapters."""

    AUTH_FAILED = "auth_failed"
    COMMAND_NOT_FOUND = "command_not_found"
    WORKING_DIRECTORY_NOT_FOUND = "working_directory_not_found"
    INVALID_RESUME_SESSION = "invalid_resume_session"
    MODEL_NOT_FOUND = "model_not_found"
    NETWORK_ERROR = "network_error"
    APPROVAL_REQUIRED = "approval_required"
    PROCESS_FAILED = "process_failed"
    RATE_LIMIT = "rate_limit"
    USAGE_WARNING = "usage_warning"
    UNKNOWN = "unknown"


_ERROR_HINTS = {
    AdapterErrorKind.AUTH_FAILED: "Check the backend login, token, or API key for this CLI.",
    AdapterErrorKind.COMMAND_NOT_FOUND: "Install the backend CLI or configure the adapter binary path.",
    AdapterErrorKind.WORKING_DIRECTORY_NOT_FOUND: "Create the project directory or update the project cwd.",
    AdapterErrorKind.INVALID_RESUME_SESSION: "Start a new session, or choose a resumable session from /sessions.",
    AdapterErrorKind.MODEL_NOT_FOUND: "Check the configured model name for this agent.",
    AdapterErrorKind.NETWORK_ERROR: "Check network access and retry after the backend service is reachable.",
    AdapterErrorKind.APPROVAL_REQUIRED: "Use human approval mode, or run auto mode only in an isolated workspace.",
    AdapterErrorKind.PROCESS_FAILED: "Open the job details or event log for the backend stderr.",
    AdapterErrorKind.RATE_LIMIT: "Wait and retry, or switch this agent to a cheaper/available model.",
    AdapterErrorKind.USAGE_WARNING: "Usage is still available, but the account or model limit is running low.",
    AdapterErrorKind.UNKNOWN: "Open the job details or event log for the raw backend output.",
}


def classify_adapter_error(text: str, *, return_code: int | None = None) -> dict[str, str | int]:
    """Classify backend stderr/stdout into a stable payload fragment."""

    clean = " ".join((text or "").strip().split())
    lowered = clean.lower()
    kind = AdapterErrorKind.UNKNOWN

    if any(marker in lowered for marker in ("unauthorized", "authentication", "auth failed", "invalid api key", "401")):
        kind = AdapterErrorKind.AUTH_FAILED
    elif (
        ("weekly limit" in lowered and any(marker in lowered for marker in ("left", "remaining", "less than")))
        or ("usage limit" in lowered and any(marker in lowered for marker in ("left", "remaining", "less than")))
    ):
        kind = AdapterErrorKind.USAGE_WARNING
    elif any(
        marker in lowered
        for marker in (
            "rate limit",
            "too many requests",
            "429",
            "quota exceeded",
            "at capacity",
            "model is busy",
            "temporarily unavailable",
            "server overloaded",
            "weekly limit reached",
            "usage limit reached",
        )
    ):
        kind = AdapterErrorKind.RATE_LIMIT
    elif "model" in lowered and any(marker in lowered for marker in ("not found", "unknown", "invalid")):
        kind = AdapterErrorKind.MODEL_NOT_FOUND
    elif any(marker in lowered for marker in ("invalid session", "session not found", "unknown session", "resume")):
        kind = AdapterErrorKind.INVALID_RESUME_SESSION
    elif any(marker in lowered for marker in ("approval", "permission denied", "requires confirmation")):
        kind = AdapterErrorKind.APPROVAL_REQUIRED
    elif any(
        marker in lowered
        for marker in (
            "connection",
            "network",
            "timeout",
            "timed out",
            "temporary failure",
            "dns",
            "http error 502",
            "http error 503",
            "http error 504",
        )
    ):
        kind = AdapterErrorKind.NETWORK_ERROR
    elif return_code is not None:
        kind = AdapterErrorKind.PROCESS_FAILED

    payload: dict[str, str | int] = {
        "error_kind": kind.value,
        "hint": _ERROR_HINTS[kind],
    }
    if return_code is not None:
        payload["return_code"] = return_code
    return payload


def command_not_found_payload(binary: str) -> dict[str, str]:
    """Payload for missing adapter executables."""

    return {
        "error_kind": AdapterErrorKind.COMMAND_NOT_FOUND.value,
        "hint": _ERROR_HINTS[AdapterErrorKind.COMMAND_NOT_FOUND],
        "binary": binary,
    }


def working_directory_not_found_payload(path: object) -> dict[str, str]:
    """Payload for missing adapter working directories."""

    return {
        "error_kind": AdapterErrorKind.WORKING_DIRECTORY_NOT_FOUND.value,
        "hint": _ERROR_HINTS[AdapterErrorKind.WORKING_DIRECTORY_NOT_FOUND],
        "cwd": str(path),
    }
