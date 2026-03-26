"""Tests for ContextPruner: softTrim / hardClear logic."""
from __future__ import annotations

import pytest
from nanobot.agent.pruner import ContextPruner
from nanobot.config.schema import ContextPruningConfig, SoftTrimConfig, HardClearConfig


def _make_cfg(**kwargs) -> ContextPruningConfig:
    return ContextPruningConfig(enabled=True, **kwargs)


def _tool_msg(content, tool_call_id: str = "t1") -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "name": "exec", "content": content}


def _assistant_msg(content: str = "ok") -> dict:
    return {"role": "assistant", "content": content}


def _user_msg(content: str = "hi") -> dict:
    return {"role": "user", "content": content}


# ─── min_prunable_tool_chars guard ───────────────────────────────────────────

def test_no_prune_when_below_min_chars():
    """总 tool chars 未超 min_prunable_tool_chars → 原样返回。"""
    cfg = _make_cfg(min_prunable_tool_chars=1_000_000)
    pruner = ContextPruner(cfg)
    msgs = [_user_msg(), _assistant_msg(), _tool_msg("small")]
    result = pruner.prune(msgs, context_window_chars=400_000)
    assert result == msgs


# ─── softTrim ────────────────────────────────────────────────────────────────

def test_soft_trim_oversized_tool_result():
    """content 超过 soft_trim.max_chars → 保留 head + tail，中间插入省略号。"""
    cfg = _make_cfg(
        min_prunable_tool_chars=0,
        keep_last_assistants=2,
        soft_trim=SoftTrimConfig(max_chars=100, head_chars=20, tail_chars=20),
        hard_clear=HardClearConfig(enabled=False),
    )
    pruner = ContextPruner(cfg)
    long_content = "A" * 50 + "B" * 50 + "C" * 50  # 150 chars
    # 这条 tool result 在 keep_last_assistants=2 的保护边界之前
    msgs = [
        _user_msg(),
        _tool_msg(long_content, "old"),  # 边界之前 → 应被 softTrim
        _assistant_msg("a1"),
        _assistant_msg("a2"),             # 往后数 2 条 assistant → 边界在 a1 处（idx=2）
    ]
    result = pruner.prune(msgs, context_window_chars=400_000)
    pruned = result[1]["content"]
    assert pruned.startswith("A" * 20)
    assert pruned.endswith("C" * 20)
    assert "..." in pruned
    assert len(pruned) < 150


def test_no_soft_trim_when_under_max_chars():
    """content 未超 max_chars → 不修剪。"""
    cfg = _make_cfg(
        min_prunable_tool_chars=0,
        soft_trim=SoftTrimConfig(max_chars=10000, head_chars=1500, tail_chars=1500),
        hard_clear=HardClearConfig(enabled=False),
    )
    pruner = ContextPruner(cfg)
    msgs = [_user_msg(), _tool_msg("short"), _assistant_msg(), _assistant_msg(), _assistant_msg()]
    result = pruner.prune(msgs, context_window_chars=400_000)
    assert result[1]["content"] == "short"


# ─── hardClear ───────────────────────────────────────────────────────────────

def test_hard_clear_when_ratio_exceeded():
    """tool result chars / context_window_chars > ratio → 整块替换 placeholder。"""
    cfg = _make_cfg(
        min_prunable_tool_chars=0,
        keep_last_assistants=2,
        hard_clear=HardClearConfig(enabled=True, ratio=0.1, placeholder="[cleared]"),
        soft_trim=SoftTrimConfig(max_chars=999999),  # 不触发 softTrim
    )
    pruner = ContextPruner(cfg)
    big_content = "X" * 10_000
    # context_window_chars=50_000, ratio=0.1 → 阈值 5000，内容 10000 > 5000
    msgs = [
        _user_msg(),
        _tool_msg(big_content, "big"),  # 边界之前
        _assistant_msg("a1"),
        _assistant_msg("a2"),
    ]
    result = pruner.prune(msgs, context_window_chars=50_000)
    assert result[1]["content"] == "[cleared]"


def test_hard_clear_disabled():
    """hard_clear.enabled=False → 不替换，只走 softTrim。"""
    cfg = _make_cfg(
        min_prunable_tool_chars=0,
        keep_last_assistants=2,
        hard_clear=HardClearConfig(enabled=False),
        soft_trim=SoftTrimConfig(max_chars=999999),
    )
    pruner = ContextPruner(cfg)
    big_content = "X" * 10_000
    msgs = [
        _user_msg(),
        _tool_msg(big_content, "big"),
        _assistant_msg("a1"),
        _assistant_msg("a2"),
    ]
    result = pruner.prune(msgs, context_window_chars=50_000)
    assert result[1]["content"] == big_content


# ─── keepLastAssistants 保护边界 ──────────────────────────────────────────────

def test_keep_last_assistants_protects_recent_tool_results():
    """边界之后的 tool result 不被修剪，边界之前的才修剪。"""
    cfg = _make_cfg(
        min_prunable_tool_chars=0,
        keep_last_assistants=2,
        soft_trim=SoftTrimConfig(max_chars=5, head_chars=2, tail_chars=2),
        hard_clear=HardClearConfig(enabled=False),
    )
    pruner = ContextPruner(cfg)
    old_content = "0123456789"   # 10 chars > max_chars=5，在边界前 → 应被 softTrim
    new_content = "ABCDEFGHIJ"   # 同上，在边界后 → 不修剪

    msgs = [
        _user_msg(),
        _tool_msg(old_content, "old"),   # idx=1，边界之前
        _assistant_msg("a1"),             # idx=2，倒数第 2 条 assistant → 边界在这里
        _tool_msg(new_content, "new"),   # idx=3，边界之后 → 保护
        _assistant_msg("a2"),             # idx=4，倒数第 1 条 assistant
    ]
    result = pruner.prune(msgs, context_window_chars=400_000)
    assert result[1]["content"] != old_content   # 被 softTrim
    assert result[3]["content"] == new_content   # 保护不动


# ─── image block 跳过 ────────────────────────────────────────────────────────

def test_skip_image_block_tool_result():
    """tool result content 为含 image block 的 list → 不修剪。"""
    cfg = _make_cfg(
        min_prunable_tool_chars=0,
        keep_last_assistants=2,
        hard_clear=HardClearConfig(enabled=True, ratio=0.0),  # ratio=0 → 任何都触发
        soft_trim=SoftTrimConfig(max_chars=0),
    )
    pruner = ContextPruner(cfg)
    image_content = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}]
    msgs = [
        _user_msg(),
        _tool_msg(image_content, "img"),
        _assistant_msg("a1"),
        _assistant_msg("a2"),
    ]
    result = pruner.prune(msgs, context_window_chars=400_000)
    assert result[1]["content"] == image_content  # 原样


# ─── enabled=False ───────────────────────────────────────────────────────────

def test_pruner_disabled_returns_original():
    """enabled=False → ContextPruner 直接返回原 messages。"""
    cfg = ContextPruningConfig(enabled=False)
    pruner = ContextPruner(cfg)
    msgs = [_user_msg(), _tool_msg("X" * 100_000)]
    result = pruner.prune(msgs, context_window_chars=1000)
    assert result == msgs


# ─── integration: disabled skips internal logic ──────────────────────────────

def test_pruner_disabled_skips_internal_logic():
    """enabled=False 时不触发任何内部 pruning 逻辑。"""
    from unittest.mock import patch
    cfg = ContextPruningConfig(enabled=False)
    pruner = ContextPruner(cfg)
    msgs = [{"role": "user", "content": "hi"}]
    with patch.object(pruner, "_maybe_hard_clear") as mock_hc:
        result = pruner.prune(msgs, context_window_chars=100000)
    mock_hc.assert_not_called()
    assert result == msgs
