"""Tests for SubagentManager wall-clock timeout.

Verifies that a hung subagent gets cancelled after
``default_timeout_seconds`` (or per-spawn ``timeout_seconds``), and that the
failure is announced as ``status="timeout"`` rather than silently retained
or mislabelled as success.
See plan: docs/plans/2026-04-21-subagent-runtime-parity.md
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.subagent import SubagentManager


def _mk_manager(default_timeout_seconds: float = 900.0) -> SubagentManager:
    provider = MagicMock()
    provider.get_default_model.return_value = "main-model"
    bus = MagicMock()
    return SubagentManager(
        provider=provider,
        workspace=Path("/tmp/_subagent_timeout_test"),
        bus=bus,
        max_tool_result_chars=1024,
        default_timeout_seconds=default_timeout_seconds,
    )


@pytest.mark.asyncio
async def test_timeout_triggers_announce_with_timeout_status(tmp_path):
    """subagent 任务超过 timeout_seconds → _announce_result kind='timeout'."""
    mgr = _mk_manager()
    mgr.workspace = tmp_path
    mgr._announce_result = AsyncMock()

    async def _slow_inner(*args, **kwargs):
        await asyncio.sleep(5.0)  # 超过 0.1s timeout

    mgr._run_subagent_inner = _slow_inner  # type: ignore[assignment]

    await mgr._run_subagent(
        task_id="t1",
        task="slow task",
        label="t1",
        origin={"channel": "cli", "chat_id": "direct"},
        timeout_seconds=0.1,
    )

    # 必须调 announce_result 且 status="timeout"
    assert mgr._announce_result.await_count == 1
    call = mgr._announce_result.await_args
    assert call.args[5] == "timeout"
    assert "timed out" in call.args[3].lower()


@pytest.mark.asyncio
async def test_within_timeout_no_announce_by_outer(tmp_path):
    """按时完成的任务 → outer wrapper 不调 announce，inner 自己管."""
    mgr = _mk_manager()
    mgr.workspace = tmp_path
    mgr._announce_result = AsyncMock()

    async def _fast_inner(*args, **kwargs):
        # inner 应该自己在完成时调 announce，这里模拟 inner 正常路径
        await mgr._announce_result(
            kwargs.get("task_id", "t2"),
            "t2",
            "fast",
            "ok",
            {"channel": "cli", "chat_id": "direct"},
            "ok",
        )

    mgr._run_subagent_inner = _fast_inner  # type: ignore[assignment]

    await mgr._run_subagent(
        task_id="t2",
        task="fast task",
        label="t2",
        origin={"channel": "cli", "chat_id": "direct"},
        timeout_seconds=2.0,
    )

    # inner 调了一次，outer 不再补刀
    assert mgr._announce_result.await_count == 1
    assert mgr._announce_result.await_args.args[5] == "ok"


@pytest.mark.asyncio
async def test_default_timeout_path(tmp_path):
    """timeout_seconds=None → 走 self._default_timeout_seconds."""
    mgr = _mk_manager(default_timeout_seconds=0.1)
    mgr.workspace = tmp_path
    mgr._announce_result = AsyncMock()

    async def _slow_inner(*args, **kwargs):
        await asyncio.sleep(5.0)

    mgr._run_subagent_inner = _slow_inner  # type: ignore[assignment]

    # 直接调 spawn 走全路径（但不让它真正后台跑，手动 await task）
    task_id = await mgr.spawn(
        task="slow default",
        label="d1",
        # timeout_seconds 不传 → 走 default 0.1s
    )
    # spawn 返回的是 str，拿 _running_tasks[task_id] 里的真 task
    # 但 spawn 返回格式是 "Subagent [xxx] started (id: yyy). ..."
    # 所以需要直接从 _running_tasks 拿。
    assert len(mgr._running_tasks) == 1
    bg_task = next(iter(mgr._running_tasks.values()))
    await bg_task

    assert mgr._announce_result.await_count == 1
    assert mgr._announce_result.await_args.args[5] == "timeout"


@pytest.mark.asyncio
async def test_zero_timeout_seconds_disables_timeout(tmp_path):
    """timeout_seconds=0 → 禁用 timeout（外层不套 wait_for）."""
    mgr = _mk_manager(default_timeout_seconds=900.0)
    mgr.workspace = tmp_path
    mgr._announce_result = AsyncMock()

    call_count = {"n": 0}

    async def _medium_inner(*args, **kwargs):
        call_count["n"] += 1
        await asyncio.sleep(0.2)  # 0.2s，比 default 快远远小于 900s

    mgr._run_subagent_inner = _medium_inner  # type: ignore[assignment]

    await mgr._run_subagent(
        task_id="t4",
        task="no timeout",
        label="t4",
        origin={"channel": "cli", "chat_id": "direct"},
        timeout_seconds=0,  # 禁用
    )

    # inner 被完整跑完，没被 cancel
    assert call_count["n"] == 1
    assert mgr._announce_result.await_count == 0  # inner 未 mock 调 announce


@pytest.mark.asyncio
async def test_timeout_cleans_up_running_tasks(tmp_path):
    """超时后 task 从 _running_tasks 清理（依赖 spawn 里 done_callback）."""
    mgr = _mk_manager(default_timeout_seconds=0.1)
    mgr.workspace = tmp_path
    mgr._announce_result = AsyncMock()

    async def _slow_inner(*args, **kwargs):
        await asyncio.sleep(5.0)

    mgr._run_subagent_inner = _slow_inner  # type: ignore[assignment]

    await mgr.spawn(task="to-timeout", label="cleanup")
    assert len(mgr._running_tasks) == 1

    # 等后台 task 完成（包括 timeout 处理）
    bg_task = next(iter(mgr._running_tasks.values()))
    await bg_task
    # done_callback 触发清理（可能是下一个 event loop tick 才执行）
    await asyncio.sleep(0)

    assert len(mgr._running_tasks) == 0
