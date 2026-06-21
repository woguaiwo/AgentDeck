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


@dataclass(frozen=True)
class MemoryDocument:
    path: Path
    title: str
    scope: MemoryScope
    owner: str
    memory_id: str
    memory_type: str
    source: str
    updated_at: str
    modified_at: float
    pinned: bool
    disabled: bool
    content: str
    metadata: dict[str, str]


class MarkdownMemoryStore:
    """Plain-file memory store designed for human inspection and git sync."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def scope_dir(self, scope: MemoryScope, owner: str | None = None) -> Path:
        base = self.workspace.memory_dir / SCOPE_DIRS[scope]
        if scope in {"project", "team", "agent", "task"} and owner:
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
        source: str = "manual",
        pinned: bool = False,
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
            f"source: {source}",
            f"signature: {signature}",
            f"created_at: {now}",
            f"updated_at: {now}",
            "ttl_days:",
            "disabled: false",
            f"pinned: {_frontmatter_bool(pinned)}",
            "tags: [" + ", ".join(tags or []) + "]",
            "---",
            "",
        ]
        path.write_text("\n".join(frontmatter) + body + "\n", encoding="utf-8")
        self._update_index(directory, title, path.name)
        return MemoryEntry(path=path, title=title, scope=scope, memory_id=memory_id)

    def list(self, *, scope: MemoryScope = "project", owner: str | None = None) -> list[Path]:
        directory = self.workspace.memory_dir / SCOPE_DIRS[scope]
        if scope in {"project", "team", "agent", "task"} and owner:
            directory = directory / _slug(owner)
        if not directory.exists():
            return []
        return sorted(path for path in directory.glob("*.md") if path.name != "MEMORY.md")

    def list_documents(
        self,
        *,
        scope: MemoryScope = "project",
        owner: str | None = None,
        limit: int = 10,
        include_disabled: bool = False,
    ) -> list[MemoryDocument]:
        documents: list[MemoryDocument] = []
        for path in self.list(scope=scope, owner=owner):
            document = _read_memory_document(
                path,
                fallback_scope=scope,
                fallback_owner=owner or "",
                include_disabled=include_disabled,
            )
            if document is None:
                continue
            documents.append(document)
        documents.sort(key=lambda item: (not item.pinned, -item.modified_at))
        return documents[: max(limit, 0)]

    def find_document(self, ref: str, *, scope: MemoryScope | None = None, owner: str | None = None) -> MemoryDocument | None:
        clean = ref.strip()
        if not clean:
            return None
        direct = Path(clean).expanduser()
        if direct.exists() and direct.is_file():
            return _read_memory_document(direct, fallback_scope=scope or "project", fallback_owner=owner or "", include_disabled=True)

        lowered = clean.lower()
        for path in self._candidate_paths(scope=scope, owner=owner):
            document = _read_memory_document(path, fallback_scope=scope or "project", fallback_owner=owner or "", include_disabled=True)
            if document is None:
                continue
            if clean in {document.memory_id, path.name, str(path)}:
                return document
            if lowered in {document.title.lower(), path.stem.lower()}:
                return document
        return None

    def set_disabled(
        self,
        ref: str,
        *,
        disabled: bool,
        scope: MemoryScope | None = None,
        owner: str | None = None,
    ) -> MemoryDocument:
        document = self.find_document(ref, scope=scope, owner=owner)
        if document is None:
            raise ValueError(f"memory not found: {ref}")
        _set_frontmatter_bool(document.path, "disabled", disabled)
        updated = _read_memory_document(
            document.path,
            fallback_scope=document.scope,
            fallback_owner=document.owner,
            include_disabled=True,
        )
        if updated is None:
            raise ValueError(f"memory is unreadable after update: {document.path}")
        return updated

    def _update_index(self, directory: Path, title: str, filename: str) -> None:
        index = directory / "MEMORY.md"
        text = index.read_text(encoding="utf-8") if index.exists() else "# Memory Index\n"
        if filename not in text:
            text = text.rstrip() + f"\n- [{title}]({filename})\n"
            index.write_text(text, encoding="utf-8")

    def _candidate_paths(self, *, scope: MemoryScope | None, owner: str | None) -> list[Path]:
        if scope is not None:
            return self.list(scope=scope, owner=owner)
        if not self.workspace.memory_dir.exists():
            return []
        return sorted(path for path in self.workspace.memory_dir.rglob("*.md") if path.name != "MEMORY.md")


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


def _read_memory_document(
    path: Path,
    *,
    fallback_scope: MemoryScope,
    fallback_owner: str,
    include_disabled: bool = False,
) -> MemoryDocument | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    metadata, body = _split_frontmatter(text)
    disabled = _metadata_bool(metadata.get("disabled"))
    if disabled and not include_disabled:
        return None
    body = body.strip()
    if not body:
        return None
    return MemoryDocument(
        path=path,
        title=metadata.get("name") or path.stem.replace("_", " "),
        scope=_metadata_scope(metadata.get("scope"), fallback_scope),
        owner=metadata.get("owner") or fallback_owner,
        memory_id=metadata.get("id") or path.stem,
        memory_type=metadata.get("type") or "memory",
        source=metadata.get("source") or "",
        updated_at=metadata.get("updated_at") or "",
        modified_at=_mtime(path),
        pinned=_metadata_bool(metadata.get("pinned")),
        disabled=disabled,
        content=body,
        metadata=metadata,
    )


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    metadata: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        clean_key = key.strip()
        if clean_key:
            metadata[clean_key] = value.strip()
    return metadata, parts[2].lstrip()


def _metadata_scope(value: str | None, fallback: MemoryScope) -> MemoryScope:
    if value in SCOPE_DIRS:
        return value  # type: ignore[return-value]
    return fallback


def _metadata_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"true", "yes", "1"}


def _frontmatter_bool(value: bool) -> str:
    return "true" if value else "false"


def _set_frontmatter_bool(path: Path, key: str, value: bool) -> None:
    text = path.read_text(encoding="utf-8")
    replacement = f"{key}: {_frontmatter_bool(value)}"
    if not text.startswith("---"):
        path.write_text(f"---\n{replacement}\n---\n\n{text.lstrip()}", encoding="utf-8")
        return
    parts = text.split("---", 2)
    if len(parts) < 3:
        path.write_text(f"---\n{replacement}\n---\n\n{text.lstrip()}", encoding="utf-8")
        return
    lines = parts[1].splitlines()
    found = False
    for index, line in enumerate(lines):
        if line.split(":", 1)[0].strip() == key:
            lines[index] = replacement
            found = True
            break
    if not found:
        lines.append(replacement)
    path.write_text("---\n" + "\n".join(lines) + "\n---" + parts[2], encoding="utf-8")


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
