"""Tests for SubagentManager runtime parity with the main loop.

Verifies that ``concurrent_tools``, ``pruner`` and ``context_window_tokens``
propagate into ``AgentRunSpec`` so subagents enjoy the same context-governance
and tool-concurrency benefits as the main agent. See plan:
docs/plans/2026-04-21-subagent-runtime-parity.md
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.pruner import ContextPruner
from nanobot.agent.subagent import SubagentManager
from nanobot.config.schema import Config, ContextPruningConfig, ExecToolConfig


def _mk_manager(
    *,
    pruner: ContextPruner | None = None,
    context_window_tokens: int | None = None,
    default_timeout_seconds: float = 900.0,
) -> SubagentManager:
    provider = MagicMock()
    provider.get_default_model.return_value = "main-model"
    bus = MagicMock()
    return SubagentManager(
        provider=provider,
        workspace=Path("/tmp/_subagent_parity_test"),
        bus=bus,
        max_tool_result_chars=1024,
        pruner=pruner,
        context_window_tokens=context_window_tokens,
        default_timeout_seconds=default_timeout_seconds,
    )


def test_defaults_are_conservative():
    """未传 pruner/window/timeout → None/None/默认 900s."""
    mgr = _mk_manager()
    assert mgr._pruner is None
    assert mgr._context_window_tokens is None
    assert mgr._default_timeout_seconds == 900.0


def test_zero_timeout_becomes_none():
    """default_timeout_seconds=0 → 内部存 None，表示 disable."""
    mgr = _mk_manager(default_timeout_seconds=0)
    assert mgr._default_timeout_seconds is None


def test_negative_timeout_becomes_none():
    mgr = _mk_manager(default_timeout_seconds=-1.0)
    assert mgr._default_timeout_seconds is None


def test_custom_pruner_and_window_are_retained():
    pruner = ContextPruner(ContextPruningConfig(enabled=True))
    mgr = _mk_manager(pruner=pruner, context_window_tokens=32_000)
    assert mgr._pruner is pruner
    assert mgr._context_window_tokens == 32_000


@pytest.mark.asyncio
async def test_run_spec_has_concurrent_tools_and_pruner(tmp_path):
    """关键集成点：pruner/concurrent_tools/context_window_tokens 透传到 AgentRunSpec."""
    pruner = ContextPruner(ContextPruningConfig(enabled=True))
    mgr = _mk_manager(pruner=pruner, context_window_tokens=64_000)
    mgr.workspace = tmp_path

    captured: dict = {}

    async def _fake_run(spec):
        captured["spec"] = spec
        result = MagicMock()
        result.stop_reason = "completed"
        result.final_content = "done"
        result.tool_events = []
        result.error = None
        return result

    mgr.runner.run = _fake_run  # type: ignore[assignment]
    mgr._announce_result = AsyncMock()  # 避免 bus.publish_inbound

    await mgr._run_subagent_inner(
        task_id="p1",
        task="parity test",
        label="p1",
        origin={"channel": "cli", "chat_id": "direct"},
        session_key="sess-parity",
    )

    spec = captured["spec"]
    assert spec.concurrent_tools is True
    assert spec.pruner is pruner
    assert spec.context_window_tokens == 64_000
    assert spec.session_key == "sess-parity"


@pytest.mark.asyncio
async def test_run_spec_propagates_none_pruner(tmp_path):
    """pruner=None 时 spec.pruner 也为 None，不误注入."""
    mgr = _mk_manager(pruner=None, context_window_tokens=None)
    mgr.workspace = tmp_path

    captured: dict = {}

    async def _fake_run(spec):
        captured["spec"] = spec
        result = MagicMock()
        result.stop_reason = "completed"
        result.final_content = "done"
        result.tool_events = []
        result.error = None
        return result

    mgr.runner.run = _fake_run  # type: ignore[assignment]
    mgr._announce_result = AsyncMock()

    await mgr._run_subagent_inner(
        task_id="p2",
        task="no pruner",
        label="p2",
        origin={"channel": "cli", "chat_id": "direct"},
    )

    spec = captured["spec"]
    assert spec.pruner is None
    assert spec.context_window_tokens is None
    assert spec.concurrent_tools is True  # 这个是硬编码 True，不依赖 pruner


# ---------------------------------------------------------------------------
# AgentLoop wiring: pruner + context_window_tokens 从主 loop 注入 SubagentManager
# ---------------------------------------------------------------------------

def _mk_loop(
    workspace: Path,
    config: Config | None = None,
    context_pruning_config: ContextPruningConfig | None = None,
    context_window_tokens: int | None = None,
) -> AgentLoop:
    bus = MagicMock()
    provider = MagicMock()
    provider.get_default_model.return_value = "main-model"
    provider.generation = MagicMock(max_tokens=4096)
    kwargs = dict(
        bus=bus,
        provider=provider,
        workspace=workspace,
        exec_config=ExecToolConfig(enable=False),
        config=config,
    )
    if context_pruning_config is not None:
        kwargs["context_pruning_config"] = context_pruning_config
    if context_window_tokens is not None:
        kwargs["context_window_tokens"] = context_window_tokens
    return AgentLoop(**kwargs)


def test_loop_propagates_pruner_and_window_to_subagents(tmp_path: Path):
    """AgentLoop 构造 SubagentManager 时传入 _pruner 和 context_window_tokens."""
    config = Config()
    loop = _mk_loop(
        tmp_path,
        config=config,
        context_pruning_config=ContextPruningConfig(enabled=True),
        context_window_tokens=128_000,
    )

    # pruner 被启用时，loop 会造出 ContextPruner 实例
    assert loop._pruner is not None
    assert loop.subagents._pruner is loop._pruner
    assert loop.subagents._context_window_tokens == loop.context_window_tokens
    assert loop.context_window_tokens == 128_000


def test_loop_propagates_timeout_from_config(tmp_path: Path):
    """AgentLoop 读 subagent_timeout_seconds 透传到 SubagentManager."""
    config = Config()
    config.agents.defaults.subagent_timeout_seconds = 300.0
    loop = _mk_loop(tmp_path, config=config)

    assert loop.subagents._default_timeout_seconds == 300.0


def test_loop_default_timeout_is_900(tmp_path: Path):
    config = Config()
    loop = _mk_loop(tmp_path, config=config)
    assert loop.subagents._default_timeout_seconds == 900.0


def test_loop_pruner_disabled_remains_none(tmp_path: Path):
    """pruning 关闭时，subagent 也收到 None pruner."""
    config = Config()
    config.agents.defaults.context_pruning.enabled = False
    loop = _mk_loop(tmp_path, config=config)
    assert loop._pruner is None
    assert loop.subagents._pruner is None
