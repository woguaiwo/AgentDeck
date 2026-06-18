from pathlib import Path

from agentdeck.core.config import Workspace
from agentdeck.storage.memory import MarkdownMemoryStore


def test_workspace_init_creates_expected_directories(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / ".agentdeck")
    workspace.ensure()

    assert workspace.config_path.exists()
    assert (workspace.memory_dir / "user").is_dir()
    assert (workspace.memory_dir / "projects").is_dir()
    assert (workspace.memory_dir / "teams").is_dir()


def test_memory_add_updates_index(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / ".agentdeck")
    store = MarkdownMemoryStore(workspace)
    entry = store.add("Project Rule", "Keep memory concise.")

    assert entry.path.exists()
    assert "Keep memory concise." in entry.path.read_text(encoding="utf-8")
    index = workspace.memory_dir / "projects" / "MEMORY.md"
    assert "Project Rule" in index.read_text(encoding="utf-8")

