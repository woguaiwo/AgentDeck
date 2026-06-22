"""Local Telegram bot registry.

Bot tokens are platform workspace-local operational config. They should not be committed.
"""

from __future__ import annotations

import json
import re
import socket
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agentdeck.core.config import Workspace


TOKEN_PATTERN = re.compile(r"\b[0-9]{5,}:[A-Za-z0-9_-]{20,}\b")


@dataclass
class TelegramBotRecord:
    bot_id: str
    title: str
    token: str
    allowed_chat_ids: list[int] = field(default_factory=list)
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def server_id(self) -> str:
        return str(self.metadata.get("server_id") or "")

    @property
    def assistant_agent_id(self) -> str:
        return str(self.metadata.get("assistant_agent_id") or "")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TelegramBotRecord":
        return cls(
            bot_id=_normalize_bot_id(str(data["bot_id"])),
            title=str(data.get("title") or data["bot_id"]),
            token=str(data.get("token") or ""),
            allowed_chat_ids=_int_list(data.get("allowed_chat_ids")),
            source=str(data.get("source") or ""),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TelegramBotRegistry:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    @property
    def path(self) -> Path:
        return self.workspace.root / "telegram" / "bots.json"

    def upsert(
        self,
        *,
        bot_id: str,
        token: str,
        title: str = "",
        allowed_chat_ids: list[int] | None = None,
        source: str = "",
        metadata: dict[str, Any] | None = None,
        assistant_agent_id: str = "",
        server_id: str = "",
    ) -> TelegramBotRecord:
        clean_id = _normalize_bot_id(bot_id)
        clean_token = _normalize_token(token)
        if not clean_token:
            raise ValueError(f"missing Telegram token for bot: {bot_id}")
        records = self._read()
        existing = records.get(clean_id)
        clean_metadata = dict(existing.metadata) if existing is not None else {}
        clean_metadata.update(dict(metadata or {}))
        clean_metadata["server_id"] = str(clean_metadata.get("server_id") or server_id or current_server_id())
        if assistant_agent_id:
            clean_metadata["assistant_agent_id"] = assistant_agent_id
        record = TelegramBotRecord(
            bot_id=clean_id,
            title=title.strip() or clean_id,
            token=clean_token,
            allowed_chat_ids=sorted(set(allowed_chat_ids or [])),
            source=source,
            metadata=clean_metadata,
        )
        records[clean_id] = record
        self._write(records)
        return record

    def get(self, bot_id: str) -> TelegramBotRecord | None:
        return self._read().get(_normalize_bot_id(bot_id))

    def list(self, *, server_id: str | None = None) -> list[TelegramBotRecord]:
        records = list(self._read().values())
        if server_id:
            records = [record for record in records if (record.server_id or server_id) == server_id]
        return sorted(records, key=lambda item: item.bot_id)

    def assign_assistant(self, bot_id: str, assistant_agent_id: str, *, server_id: str = "") -> TelegramBotRecord:
        records = self._read()
        clean_id = _normalize_bot_id(bot_id)
        record = records.get(clean_id)
        if record is None:
            raise ValueError(f"telegram bot not found: {bot_id}")
        metadata = dict(record.metadata)
        metadata["server_id"] = metadata.get("server_id") or server_id or current_server_id()
        metadata["assistant_agent_id"] = assistant_agent_id
        record.metadata = metadata
        records[clean_id] = record
        self._write(records)
        return record

    def import_file(self, path: str | Path) -> list[TelegramBotRecord]:
        source = Path(path).expanduser().resolve()
        text = source.read_text(encoding="utf-8")
        records = _parse_toml_bots(text, source=str(source))
        if not records:
            records = _parse_loose_bots(text, source=str(source))
        imported: list[TelegramBotRecord] = []
        for record in records:
            imported.append(
                self.upsert(
                    bot_id=record.bot_id,
                    title=record.title,
                    token=record.token,
                    allowed_chat_ids=record.allowed_chat_ids,
                    source=record.source,
                    metadata=record.metadata,
                )
            )
        return imported

    def _read(self) -> dict[str, TelegramBotRecord]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        raw_records = data.get("bots", data) if isinstance(data, dict) else {}
        if not isinstance(raw_records, dict):
            return {}
        records: dict[str, TelegramBotRecord] = {}
        for key, value in raw_records.items():
            if not isinstance(value, dict):
                continue
            try:
                record = TelegramBotRecord.from_dict(value)
            except (KeyError, TypeError, ValueError):
                continue
            records[_normalize_bot_id(str(key))] = record
        return records

    def _write(self, records: dict[str, TelegramBotRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "bots": {key: value.to_dict() for key, value in sorted(records.items())}}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


def redacted_token(token: str) -> str:
    clean = _normalize_token(token)
    if len(clean) <= 10:
        return "***"
    return f"{clean[:6]}...{clean[-4:]}"


def _parse_toml_bots(text: str, *, source: str) -> list[TelegramBotRecord]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    bots_data = data.get("bots", data)
    if not isinstance(bots_data, dict):
        return []
    records: list[TelegramBotRecord] = []
    for key, value in bots_data.items():
        if not isinstance(value, dict):
            continue
        token = _normalize_token(str(value.get("token") or ""))
        if not token:
            continue
        records.append(
            TelegramBotRecord(
                bot_id=_normalize_bot_id(str(value.get("bot_id") or key)),
                title=str(value.get("title") or key),
                token=token,
                allowed_chat_ids=_int_list(value.get("allowed_chat_ids") or value.get("chat_ids") or value.get("chat_id")),
                source=source,
                metadata={
                    "server_id": str(value.get("server_id") or current_server_id()),
                    "assistant_agent_id": str(value.get("assistant_agent_id") or ""),
                },
            )
        )
    return records


def _parse_loose_bots(text: str, *, source: str) -> list[TelegramBotRecord]:
    records: list[TelegramBotRecord] = []
    current_name = ""
    current_chats: list[int] = []
    source_server = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            current_name = stripped.strip("[]").strip()
            current_chats = []
            continue
        key, sep, value = stripped.partition("=")
        if not sep:
            key, sep, value = stripped.partition(":")
        if sep:
            clean_key = key.strip().lower().replace("-", "_")
            clean_value = value.strip().strip("'\"")
            if clean_key == "server":
                source_server = clean_value
            elif clean_key in {"name", "bot", "bot_id"}:
                current_name = clean_value
            elif clean_key in {"chat_id", "chat_ids", "allowed_chat_ids", "allowed_chats"}:
                current_chats = _int_list(clean_value)
            elif not clean_value and _looks_like_loose_bot_heading(key):
                current_name = key.strip()
                current_chats = []
        token_match = TOKEN_PATTERN.search(stripped)
        if not token_match:
            continue
        token = token_match.group(0)
        bot_id = current_name or f"bot-{len(records) + 1}"
        records.append(
            TelegramBotRecord(
                bot_id=_normalize_bot_id(bot_id),
                title=bot_id,
                token=token,
                allowed_chat_ids=current_chats,
                source=source,
                metadata={"server_id": current_server_id(), "source_server": source_server},
            )
        )
    return records


def _looks_like_loose_bot_heading(value: str) -> bool:
    clean = value.strip()
    if not clean or " " in clean:
        return False
    lowered = clean.lower()
    if lowered in {"agents", "token", "working", "folder", "tmux", "session"}:
        return False
    return bool(re.search(r"(bot|agent|team)", lowered))


def current_server_id() -> str:
    return socket.gethostname().strip() or "unknown-server"


def assistant_agent_id_for_bot(bot_id: str) -> str:
    return f"assistant-{_normalize_bot_id(bot_id)}"


def _normalize_bot_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip().lower()).strip(".-") or "bot"


def _normalize_token(value: str) -> str:
    match = TOKEN_PATTERN.search(value.strip())
    return match.group(0) if match else value.strip()


def _int_list(value: object) -> list[int]:
    if isinstance(value, int):
        return [value]
    if isinstance(value, list):
        return sorted({int(item) for item in value if str(item).strip().lstrip("-").isdigit()})
    return sorted({int(item) for item in re.findall(r"-?\d+", str(value or ""))})
