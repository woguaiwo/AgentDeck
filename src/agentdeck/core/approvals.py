"""Approval policy primitives."""

from __future__ import annotations

from enum import Enum


class ApprovalMode(str, Enum):
    """How AgentDeck should handle backend approval requests."""

    FAIL = "fail"
    RECORD = "record"
    BYPASS = "bypass"

    @classmethod
    def parse(cls, value: str | "ApprovalMode") -> "ApprovalMode":
        if isinstance(value, cls):
            return value
        return cls(value.strip().lower())

