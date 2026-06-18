"""Markdown memory store with scoped directories."""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agentdeck.core.config import Workspace

MemoryScope = Literal["user", "project", "team", "agent", "task"]

SCOPE_DIRS: dict[MemoryScope, str] = {
    "user": "user",
    "project": "projects",
    "team": "teams",
    "agent": "agents",
    "task": "tasks",
}

SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(secret|token|api[_-]?key|password)\s*[:=]\s*['\"]?[^'\"\s]{12,}"),
)


@dataclass(frozen=True)
class MemoryEntry:
    path: Path
    title: str
    scope: MemoryScope
    memory_id: str


class MarkdownMemoryStore:
    """Plain-file memory store designed for human inspection and git sync."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def scope_dir(self, scope: MemoryScope, owner: str | None = None) -> Path:
        base = self.workspace.memory_dir / SCOPE_DIRS[scope]
        if scope in {"team", "agent", "task"} and owner:
            base = base / _slug(owner)
        base.mkdir(parents=True, exist_ok=True)
        index = base / "MEMORY.md"
        if not index.exists():
            index.write_text("# Memory Index\n", encoding="utf-8")
        return base

    def add(
        self,
        title: str,
        content: str,
        *,
        scope: MemoryScope = "project",
        owner: str | None = None,
        memory_type: str = "project",
        tags: list[str] | None = None,
    ) -> MemoryEntry:
        self.workspace.ensure()
        body = content.strip()
        if not body:
            raise ValueError("memory content is empty")
        if scope == "team" and _contains_secret(body):
            raise ValueError("team memory appears to contain a secret")

        directory = self.scope_dir(scope, owner)
        slug = _slug(title)
        memory_id = f"mem-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        path = _next_path(directory, slug)
        signature = hashlib.sha256(f"{body}|{memory_type}|{scope}".encode("utf-8")).hexdigest()
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        frontmatter = [
            "---",
            "schema_version: 1",
            f"id: {memory_id}",
            f"name: {title}",
            f"type: {memory_type}",
            f"scope: {scope}",
            f"owner: {owner or ''}",
            f"source: manual",
            f"signature: {signature}",
            f"created_at: {now}",
            f"updated_at: {now}",
            "ttl_days:",
            "disabled: false",
            "tags: [" + ", ".join(tags or []) + "]",
            "---",
            "",
        ]
        path.write_text("\n".join(frontmatter) + body + "\n", encoding="utf-8")
        self._update_index(directory, title, path.name)
        return MemoryEntry(path=path, title=title, scope=scope, memory_id=memory_id)

    def list(self, *, scope: MemoryScope = "project", owner: str | None = None) -> list[Path]:
        directory = self.scope_dir(scope, owner)
        return sorted(path for path in directory.glob("*.md") if path.name != "MEMORY.md")

    def _update_index(self, directory: Path, title: str, filename: str) -> None:
        index = directory / "MEMORY.md"
        text = index.read_text(encoding="utf-8") if index.exists() else "# Memory Index\n"
        if filename not in text:
            text = text.rstrip() + f"\n- [{title}]({filename})\n"
            index.write_text(text, encoding="utf-8")


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", value.strip().lower()).strip("._") or "memory"


def _next_path(directory: Path, slug: str) -> Path:
    candidate = directory / f"{slug}.md"
    if not candidate.exists():
        return candidate
    index = 2
    while True:
        candidate = directory / f"{slug}_{index}.md"
        if not candidate.exists():
            return candidate
        index += 1


def _contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)

