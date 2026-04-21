"""Tests for per-spawn subagent trace (independent llm_logs file).

Task 3 of docs/plans/2026-04-20-subagent-trace-and-model-field.md
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.subagent import SubagentManager


def _mk_manager(
    *,
    model: str | None = None,
    log_dir: Path | None = None,
) -> SubagentManager:
    provider = MagicMock()
    provider.get_default_model.return_value = "main-model"
    bus = MagicMock()
    return SubagentManager(
        provider=provider,
        workspace=Path("/root/workspace/tmp/_subagent_trace_test"),
        bus=bus,
        max_tool_result_chars=1024,
        model=model,
    )


@pytest.mark.asyncio
async def test_subagent_creates_log_file(tmp_path: Path):
    """验证 per-spawn 会生成独立的 subagent jsonl 文件。"""
    mgr = _mk_manager(model="subagent-model")
    log_dir = tmp_path / "llm_logs"

    async def _fake_run(spec):
        from nanobot.agent.hook import AgentHookContext
        ctx = AgentHookContext(iteration=0, messages=[], model=spec.model)
        if spec.hook:
            await spec.hook.before_iteration(ctx)
            ctx.final_content = "done"
            ctx.stop_reason = "completed"
            await spec.hook.after_iteration(ctx)
        result = MagicMock()
        result.stop_reason = "completed"
        result.final_content = "done"
        result.tool_events = []
        result.error = None
        return result

    mgr.runner.run = _fake_run
    mgr._announce_result = AsyncMock()

    await mgr._run_subagent(
        task_id="t1",
        task="do something",
        label="t1",
        origin={"channel": "cli", "chat_id": "direct"},
        log_dir=log_dir,
    )

    log_files = list(log_dir.glob("subagent_*_t1.jsonl"))
    assert len(log_files) == 1
    entry = json.loads(log_files[0].read_text().strip().splitlines()[0])
    assert entry["session_key"].startswith("subagent:t1:cli:direct")


@pytest.mark.asyncio
async def test_subagent_log_has_model_field(tmp_path: Path):
    """端到端：subagent trace 里能看到 model 字段。"""
    mgr = _mk_manager(model="anthropic/claude-haiku-4-5")
    log_dir = tmp_path / "llm_logs"

    async def _fake_run(spec):
        from nanobot.agent.hook import AgentHookContext
        ctx = AgentHookContext(iteration=0, messages=[], model=spec.model)
        if spec.hook:
            await spec.hook.before_iteration(ctx)
            ctx.final_content = "done"
            ctx.stop_reason = "completed"
            await spec.hook.after_iteration(ctx)
        result = MagicMock()
        result.stop_reason = "completed"
        result.final_content = "done"
        result.tool_events = []
        result.error = None
        return result

    mgr.runner.run = _fake_run
    mgr._announce_result = AsyncMock()

    await mgr._run_subagent(
        task_id="t2",
        task="do something",
        label="t2",
        origin={"channel": "discord", "chat_id": "123"},
        log_dir=log_dir,
    )

    log_file = next(log_dir.glob("subagent_*_t2.jsonl"))
    entry = json.loads(log_file.read_text().strip().splitlines()[0])
    assert entry["model"] == "anthropic/claude-haiku-4-5"


@pytest.mark.asyncio
async def test_subagent_fallback_log_dir_when_layout_missing(tmp_path: Path):
    """log_dir=None 时落到 workspace/subagent_logs。"""
    mgr = _mk_manager(model="subagent-model")
    mgr.workspace = tmp_path / "ws"

    async def _fake_run(spec):
        from nanobot.agent.hook import AgentHookContext
        ctx = AgentHookContext(iteration=0, messages=[], model=spec.model)
        if spec.hook:
            await spec.hook.before_iteration(ctx)
            ctx.final_content = "done"
            ctx.stop_reason = "completed"
            await spec.hook.after_iteration(ctx)
        result = MagicMock()
        result.stop_reason = "completed"
        result.final_content = "done"
        result.tool_events = []
        result.error = None
        return result

    mgr.runner.run = _fake_run
    mgr._announce_result = AsyncMock()

    await mgr._run_subagent(
        task_id="t3",
        task="do something",
        label="t3",
        origin={"channel": "cli", "chat_id": "direct"},
        log_dir=None,
    )

    log_files = list((mgr.workspace / "subagent_logs").glob("subagent_*_t3.jsonl"))
    assert len(log_files) == 1


@pytest.mark.asyncio
async def test_concurrent_subagents_use_separate_files(tmp_path: Path):
    """并发 spawn 两个 subagent，各写各的文件，不混。"""
    mgr = _mk_manager(model="subagent-model")
    log_dir = tmp_path / "llm_logs"

    async def _fake_run(spec):
        from nanobot.agent.hook import AgentHookContext
        ctx = AgentHookContext(iteration=0, messages=[], model=spec.model)
        if spec.hook:
            await spec.hook.before_iteration(ctx)
            ctx.final_content = "done"
            ctx.stop_reason = "completed"
            await spec.hook.after_iteration(ctx)
        result = MagicMock()
        result.stop_reason = "completed"
        result.final_content = "done"
        result.tool_events = []
        result.error = None
        return result

    mgr.runner.run = _fake_run
    mgr._announce_result = AsyncMock()

    await mgr._run_subagent(
        task_id="t4a",
        task="task a",
        label="t4a",
        origin={"channel": "discord", "chat_id": "111"},
        log_dir=log_dir,
    )
    await mgr._run_subagent(
        task_id="t4b",
        task="task b",
        label="t4b",
        origin={"channel": "discord", "chat_id": "222"},
        log_dir=log_dir,
    )

    files_a = list(log_dir.glob("subagent_*_t4a.jsonl"))
    files_b = list(log_dir.glob("subagent_*_t4b.jsonl"))
    assert len(files_a) == 1
    assert len(files_b) == 1
    assert files_a[0] != files_b[0]
