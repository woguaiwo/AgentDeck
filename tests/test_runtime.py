import asyncio
from pathlib import Path

from agentdeck.adapters.echo import EchoAdapter
from agentdeck.core.config import Workspace
from agentdeck.core.runtime import AgentRuntime
from agentdeck.storage.event_log import EventLog


def test_echo_runtime_writes_events(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / ".agentdeck")
    runtime = AgentRuntime(workspace, EchoAdapter(), agent_id="test-agent")

    result = asyncio.run(runtime.run_prompt("hello"))

    assert result.final_text == "Echo: hello"
    events = EventLog(workspace).tail(20)
    assert [event.kind.value for event in events]
    assert any(event.text == "Echo: hello" for event in events)

