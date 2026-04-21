"""Tests for model field propagation through AgentHookContext + TraceHook.

Task 1-2 of docs/plans/2026-04-20-subagent-trace-and-model-field.md
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.agent.hook import AgentHook, AgentHookContext, TraceHook
from nanobot.agent.runner import AgentRunSpec, AgentRunner
from nanobot.agent.tools.registry import ToolRegistry


class _ModelSpyHook(AgentHook):
    def __init__(self) -> None:
        self.captured_model: str | None = "UNSET"

    async def before_iteration(self, context: AgentHookContext) -> None:
        self.captured_model = context.model


def test_context_exposes_model():
    ctx = AgentHookContext(iteration=0, messages=[], model="test-model")
    assert ctx.model == "test-model"


def test_context_defaults_model_none():
    ctx = AgentHookContext(iteration=0, messages=[])
    assert ctx.model is None


@pytest.mark.asyncio
async def test_runner_populates_model_in_context():
    spy = _ModelSpyHook()
    provider = MagicMock()
    async def _fake_chat(**kwargs):
        return MagicMock(
            content="done",
            tool_calls=[],
            usage={},
            reasoning_content=None,
        )
    provider.chat_with_retry = _fake_chat
    runner = AgentRunner(provider)

    spec = AgentRunSpec(
        initial_messages=[{"role": "system", "content": "sys"}],
        tools=ToolRegistry(),
        model="anthropic/claude-haiku-4-5",
        max_iterations=10,
        max_tool_result_chars=1024,
        hook=spy,
    )
    await runner.run(spec)
    assert spy.captured_model == "anthropic/claude-haiku-4-5"


# ---------------------------------------------------------------------------
# TraceHook writes model field
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_hook_writes_model_to_log(tmp_path: Path):
    log_path = tmp_path / "trace.jsonl"
    hook = TraceHook(log_path=log_path)
    hook.session_key = "subagent:t1:cli:direct"

    ctx = AgentHookContext(
        iteration=0,
        messages=[{"role": "user", "content": "hi"}],
        model="anthropic/claude-haiku-4-5",
    )
    await hook.before_iteration(ctx)
    await hook.after_iteration(ctx)

    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["model"] == "anthropic/claude-haiku-4-5"
    assert entry["session_key"] == "subagent:t1:cli:direct"


@pytest.mark.asyncio
async def test_trace_hook_handles_none_model(tmp_path: Path):
    log_path = tmp_path / "trace.jsonl"
    hook = TraceHook(log_path=log_path)

    ctx = AgentHookContext(
        iteration=0,
        messages=[{"role": "user", "content": "hi"}],
        model=None,
    )
    await hook.before_iteration(ctx)
    await hook.after_iteration(ctx)

    entry = json.loads(log_path.read_text().strip().splitlines()[0])
    assert entry["model"] is None
