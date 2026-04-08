"""Tests for SubagentManager extra_hooks composition.

Verifies that cross-cutting hooks (like CommandRewriteHook) propagate into
the subagent runner via CompositeHook, so they operate on both the main
loop and spawned subagents.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.hook import AgentHook, AgentHookContext, CompositeHook
from nanobot.agent.subagent import SubagentManager, _SubagentHook


class _SpyHook(AgentHook):
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        self.calls.append(context.iteration)


def _mk_manager(extra_hooks=None) -> SubagentManager:
    provider = MagicMock()
    provider.get_default_model.return_value = "dummy-model"
    bus = MagicMock()
    return SubagentManager(
        provider=provider,
        workspace=Path("/root/workspace/tmp/_subagent_test"),
        bus=bus,
        max_tool_result_chars=1024,
        extra_hooks=extra_hooks,
    )


def test_subagent_default_no_extra_hooks():
    mgr = _mk_manager()
    assert mgr._extra_hooks == []


def test_subagent_accepts_extra_hooks():
    spy = _SpyHook()
    mgr = _mk_manager(extra_hooks=[spy])
    assert mgr._extra_hooks == [spy]


@pytest.mark.asyncio
async def test_compose_hook_runs_logging_and_extra():
    """_compose_hook 组合 _SubagentHook + extra_hooks 并正确 fan-out。"""
    spy = _SpyHook()
    mgr = _mk_manager(extra_hooks=[spy])
    composed = mgr._compose_hook("tid-1")
    assert isinstance(composed, CompositeHook)

    ctx = AgentHookContext(iteration=3, messages=[], tool_calls=[])
    await composed.before_execute_tools(ctx)
    assert spy.calls == [3]


def test_compose_hook_no_extras_returns_single_subagent_hook():
    """没有 extra hooks 时不包 CompositeHook，保持轻量。"""
    mgr = _mk_manager()
    composed = mgr._compose_hook("tid-2")
    assert isinstance(composed, _SubagentHook)
