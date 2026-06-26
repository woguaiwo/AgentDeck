"""Workspace and configuration paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_CONFIG = """# AgentDeck platform configuration

[project]
name = ""

[runtime]
default_adapter = "echo"

[memory]
enabled = true
"""

DEFAULT_PROJECT_LOCAL_CONFIG = """# AgentDeck project-local integration hints
#
# This file is optional. AgentDeck platform state lives in the main workspace;
# keep only directory-specific adapter/TUI hints here.

[project]
id = ""

[tui]
profile = ""
command = []
"""

PROJECT_LOCAL_CONFIG_NAMES = (".agentdeck.toml", "agentdeck.toml")


@dataclass(frozen=True)
class Workspace:
    """AgentDeck control-plane workspace."""

    root: Path

    @classmethod
    def from_cwd(cls, cwd: str | Path | None = None) -> "Workspace":
        """Resolve the platform workspace, not a caller-directory workspace."""

        override = os.environ.get("AGENTDECK_WORKSPACE")
        if override:
            return cls(Path(override).expanduser().resolve())
        return cls(default_workspace_root())

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
    def agents_dir(self) -> Path:
        return self.root / "agents"

    @property
    def projects_dir(self) -> Path:
        return self.root / "projects"

    @property
    def approvals_dir(self) -> Path:
        return self.root / "approvals"

    @property
    def jobs_dir(self) -> Path:
        return self.root / "jobs"

    @property
    def journal_dir(self) -> Path:
        return self.root / "journal"

    @property
    def session_state_dir(self) -> Path:
        return self.root / "session-state"

    @property
    def project_state_dir(self) -> Path:
        return self.root / "project-state"

    @property
    def inbox_dir(self) -> Path:
        return self.root / "inbox"

    @property
    def errors_dir(self) -> Path:
        return self.root / "errors"

    @property
    def board_dir(self) -> Path:
        return self.root / "board"

    @property
    def focus_dir(self) -> Path:
        return self.root / "focus"

    @property
    def memory_dir(self) -> Path:
        return self.root / "memory"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for path in [
            self.events_dir,
            self.sessions_dir,
            self.agents_dir,
            self.projects_dir,
            self.approvals_dir,
            self.jobs_dir,
            self.journal_dir,
            self.session_state_dir,
            self.project_state_dir,
            self.inbox_dir,
            self.errors_dir,
            self.board_dir,
            self.focus_dir,
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
            "agents": str(self.agents_dir),
            "projects": str(self.projects_dir),
            "approvals": str(self.approvals_dir),
            "jobs": str(self.jobs_dir),
            "journal": str(self.journal_dir),
            "session_state": str(self.session_state_dir),
            "project_state": str(self.project_state_dir),
            "errors": str(self.errors_dir),
            "focus": str(self.focus_dir),
            "memory": str(self.memory_dir),
        }


def default_workspace_root() -> Path:
    """Return AgentDeck's platform workspace, independent of the caller cwd."""

    source_root = _find_agentdeck_source_root()
    if source_root is not None:
        return source_root / ".agentdeck"
    return Workspace.global_home().root


def project_local_config_path(project_dir: str | Path) -> Path:
    """Return the optional project-local AgentDeck integration config path."""

    return Path(project_dir).expanduser().resolve() / PROJECT_LOCAL_CONFIG_NAMES[0]


def find_project_local_config(cwd: str | Path | None = None) -> Path | None:
    """Find an optional project-local config without treating it as a workspace."""

    start = Path(cwd or os.getcwd()).expanduser().resolve()
    candidates = (start, *start.parents)
    for directory in candidates:
        for name in PROJECT_LOCAL_CONFIG_NAMES:
            path = directory / name
            if path.exists():
                return path
    return None


def _find_agentdeck_source_root() -> Path | None:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "pyproject.toml").exists() and (parent / "src" / "agentdeck").is_dir():
            return parent
    return None
