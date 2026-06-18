"""Workspace and configuration paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_CONFIG = """# AgentDeck project configuration

[project]
name = ""

[runtime]
default_adapter = "echo"

[memory]
enabled = true
"""


@dataclass(frozen=True)
class Workspace:
    """A project-local AgentDeck workspace."""

    root: Path

    @classmethod
    def from_cwd(cls, cwd: str | Path | None = None) -> "Workspace":
        base = Path(cwd or os.getcwd()).expanduser().resolve()
        override = os.environ.get("AGENTDECK_WORKSPACE")
        if override:
            return cls(Path(override).expanduser().resolve())
        return cls(base / ".agentdeck")

    @classmethod
    def global_home(cls) -> "Workspace":
        override = os.environ.get("AGENTDECK_HOME")
        if override:
            return cls(Path(override).expanduser().resolve())
        return cls(Path.home() / ".agentdeck")

    @property
    def config_path(self) -> Path:
        return self.root / "config.toml"

    @property
    def events_dir(self) -> Path:
        return self.root / "events"

    @property
    def sessions_dir(self) -> Path:
        return self.root / "sessions"

    @property
    def inbox_dir(self) -> Path:
        return self.root / "inbox"

    @property
    def board_dir(self) -> Path:
        return self.root / "board"

    @property
    def memory_dir(self) -> Path:
        return self.root / "memory"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for path in [
            self.events_dir,
            self.sessions_dir,
            self.inbox_dir,
            self.board_dir,
            self.memory_dir / "user",
            self.memory_dir / "projects",
            self.memory_dir / "teams",
            self.memory_dir / "agents",
            self.memory_dir / "tasks",
        ]:
            path.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists():
            self.config_path.write_text(DEFAULT_CONFIG, encoding="utf-8")

    def doctor(self) -> dict[str, str | bool]:
        return {
            "workspace": str(self.root),
            "exists": self.root.exists(),
            "config": str(self.config_path),
            "config_exists": self.config_path.exists(),
            "events": str(self.events_dir),
            "sessions": str(self.sessions_dir),
            "memory": str(self.memory_dir),
        }

