"""Tests for SpawnTool log_dir propagation via AgentLoop._set_tool_context.

Task 4 of docs/plans/2026-04-20-subagent-trace-and-model-field.md
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.config.schema import ExecToolConfig
from nanobot.workspace.layout import WorkspaceLayout


def _mk_loop(workspace: Path) -> AgentLoop:
    bus = MagicMock()
    provider = MagicMock()
    provider.get_default_model.return_value = "main-model"
    provider.generation = MagicMock(max_tokens=4096)
    return AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        exec_config=ExecToolConfig(enable=False),
    )


def test_spawn_tool_propagates_log_dir():
    """SpawnTool.set_context 接受 log_dir 并透传到 manager.spawn。"""
    manager = MagicMock()
    tool = SpawnTool(manager=manager)
    tool.set_context("discord", "12345", log_dir=Path("/tmp/foo"))
    assert tool._log_dir == Path("/tmp/foo")


def test_agentloop_sets_spawn_log_dir_from_layout(tmp_path: Path):
    """AgentLoop._set_tool_context 把 layout.llm_logs_dir 传给 spawn 工具。"""
    loop = _mk_loop(tmp_path)
    layout = WorkspaceLayout(
        workspace=tmp_path,
        channel="discord",
        channel_name="develop",
        chat_id="12345",
        scope_id="12345",
    )
    layout.ensure_dirs()

    loop._set_tool_context("discord", "12345", layout=layout)

    spawn_tool = loop.tools.get("spawn")
    assert isinstance(spawn_tool, SpawnTool)
    assert spawn_tool._log_dir == layout.llm_logs_dir


def test_spawn_tool_log_dir_none_by_default():
    """未调用 set_context 时 log_dir 为 None。"""
    manager = MagicMock()
    tool = SpawnTool(manager=manager)
    assert tool._log_dir is None
