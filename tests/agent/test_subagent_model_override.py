"""Tests for SubagentManager model-tier override.

Verifies that reasoning_effort / max_tokens passed at construction time
propagate into AgentRunSpec when a subagent task is executed, and that
AgentLoop wires an independent provider when config.subagent_model is set.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.subagent import SubagentManager
from nanobot.config.schema import Config, ExecToolConfig


def _mk_manager(
    *,
    reasoning_effort: str | None = None,
    max_tokens: int | None = None,
    model: str | None = None,
) -> SubagentManager:
    provider = MagicMock()
    provider.get_default_model.return_value = "main-model"
    bus = MagicMock()
    return SubagentManager(
        provider=provider,
        workspace=Path("/root/workspace/tmp/_subagent_model_test"),
        bus=bus,
        max_tool_result_chars=1024,
        model=model,
        reasoning_effort=reasoning_effort,
        max_tokens=max_tokens,
    )


def test_default_none_reasoning_and_max_tokens():
    mgr = _mk_manager()
    assert mgr.reasoning_effort is None
    assert mgr.max_tokens is None


def test_accepts_reasoning_effort():
    mgr = _mk_manager(reasoning_effort="low")
    assert mgr.reasoning_effort == "low"


def test_accepts_max_tokens():
    mgr = _mk_manager(max_tokens=4096)
    assert mgr.max_tokens == 4096


@pytest.mark.asyncio
async def test_run_spec_includes_reasoning_and_max_tokens(tmp_path, monkeypatch):
    """关键集成点：config 字段 → SubagentManager → AgentRunSpec。"""
    mgr = _mk_manager(
        reasoning_effort="low",
        max_tokens=4096,
        model="subagent-model",
    )
    mgr.workspace = tmp_path

    captured: dict = {}

    async def _fake_run(spec):
        captured["spec"] = spec
        # 模拟 AgentRunResult 最小字段
        result = MagicMock()
        result.stop_reason = "completed"
        result.final_content = "done"
        result.tool_events = []
        result.error = None
        return result

    mgr.runner.run = _fake_run  # type: ignore[assignment]
    mgr._announce_result = AsyncMock()  # 避免 bus.publish_inbound 调用

    await mgr._run_subagent(
        task_id="t1",
        task="do something",
        label="t1",
        origin={"channel": "cli", "chat_id": "direct"},
    )

    spec = captured["spec"]
    assert spec.model == "subagent-model"
    assert spec.reasoning_effort == "low"
    assert spec.max_tokens == 4096


# ---------------------------------------------------------------------------
# AgentLoop wiring integration tests
# ---------------------------------------------------------------------------

def _mk_loop(workspace: Path, config: Config | None = None) -> AgentLoop:
    bus = MagicMock()
    provider = MagicMock()
    provider.get_default_model.return_value = "main-model"
    provider.generation = MagicMock(max_tokens=4096)
    return AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        exec_config=ExecToolConfig(enable=False),
        config=config,
    )


def test_loop_no_subagent_model_reuses_main_provider(tmp_path: Path):
    """未配置 subagent_model → subagents 复用主 agent provider / model。"""
    config = Config()
    loop = _mk_loop(tmp_path, config=config)
    assert loop.subagents.provider is loop.provider
    assert loop.subagents.model == loop.model
    assert loop.subagents.reasoning_effort is None
    assert loop.subagents.max_tokens is None


def test_loop_with_subagent_model_uses_independent_provider(tmp_path: Path):
    """配置 subagent_model → 用 _make_single_provider 构造独立 provider。"""
    config = Config()
    config.agents.defaults.subagent_model = "anthropic/claude-haiku-4-5"
    config.agents.defaults.subagent_reasoning_effort = "low"
    config.agents.defaults.subagent_max_tokens = 4096

    fake_subagent_provider = MagicMock(name="subagent_provider")
    fake_subagent_provider.get_default_model.return_value = "anthropic/claude-haiku-4-5"

    with patch(
        "nanobot.nanobot._make_single_provider",
        return_value=fake_subagent_provider,
    ) as mock_make:
        loop = _mk_loop(tmp_path, config=config)

    mock_make.assert_called_once()
    # positional args: (config, model)
    args, _ = mock_make.call_args
    assert args[1] == "anthropic/claude-haiku-4-5"

    assert loop.subagents.provider is fake_subagent_provider
    assert loop.subagents.model == "anthropic/claude-haiku-4-5"
    assert loop.subagents.reasoning_effort == "low"
    assert loop.subagents.max_tokens == 4096


def test_loop_subagent_provider_build_failure_falls_back(tmp_path: Path):
    """subagent provider 构造失败 → warn + 回退主 agent provider，不崩。"""
    config = Config()
    config.agents.defaults.subagent_model = "bogus/does-not-exist"

    with patch(
        "nanobot.nanobot._make_single_provider",
        side_effect=ValueError("no provider for bogus/does-not-exist"),
    ):
        loop = _mk_loop(tmp_path, config=config)

    # 回退到主 provider
    assert loop.subagents.provider is loop.provider
    assert loop.subagents.model == loop.model
