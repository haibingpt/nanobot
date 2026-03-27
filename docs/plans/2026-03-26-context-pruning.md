# Context Pruning (softTrim / hardClear) Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在每次 LLM call 前对 context 中过大的 tool result 做轻量修剪（softTrim / hardClear），防止长对话 token 积累，不改磁盘、不改 session history。

**Architecture:** 新增独立 `ContextPruner` 类（`nanobot/agent/pruner.py`），在 `_run_agent_loop` 的每次 LLM call 前拦截 messages 做 transient 修剪；新增 Pydantic config schema；loop 初始化时按 config 决定是否启用 pruner。

**Tech Stack:** Python 3.11+, Pydantic v2, pytest + pytest-asyncio（已有）

---

## File Map

| 动作 | 文件 | 说明 |
|---|---|---|
| 新增 | `nanobot/agent/pruner.py` | ContextPruner 核心逻辑，~90 行 |
| 修改 | `nanobot/config/schema.py` | 新增 3 个 config 类 + AgentDefaults 字段，~45 行 |
| 修改 | `nanobot/agent/loop.py` | 初始化 pruner + while 循环里调用，~10 行 |
| 新增 | `tests/agent/test_context_pruner.py` | 10 个单元测试，~130 行 |

---

## Chunk 1: Config Schema

### Task 1: 新增 Pruning Config 类

**Files:**
- Modify: `nanobot/config/schema.py`

- [ ] **Step 1: 写失败测试**

在 `tests/config/test_config_migration.py` 末尾追加（或单独新建文件也可）：

```python
def test_context_pruning_config_defaults():
    from nanobot.config.schema import ContextPruningConfig
    cfg = ContextPruningConfig()
    assert cfg.enabled is False
    assert cfg.keep_last_assistants == 3
    assert cfg.min_prunable_tool_chars == 50_000
    assert cfg.soft_trim.max_chars == 4000
    assert cfg.hard_clear.enabled is True

def test_context_pruning_config_camel_case():
    from nanobot.config.schema import ContextPruningConfig
    cfg = ContextPruningConfig.model_validate({
        "enabled": True,
        "keepLastAssistants": 5,
        "softTrim": {"maxChars": 2000, "headChars": 800, "tailChars": 800},
        "hardClear": {"enabled": False},
    })
    assert cfg.enabled is True
    assert cfg.keep_last_assistants == 5
    assert cfg.soft_trim.max_chars == 2000
    assert cfg.hard_clear.enabled is False

def test_agent_defaults_has_context_pruning():
    from nanobot.config.schema import AgentDefaults
    defaults = AgentDefaults()
    assert hasattr(defaults, "context_pruning")
    assert defaults.context_pruning.enabled is False
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /root/git_code/nanobot && python3 -m pytest tests/config/test_config_migration.py -k "pruning" -v
```

期望：`ImportError` 或 `AttributeError`（类不存在）

- [ ] **Step 3: 在 schema.py 新增 config 类**

在 `ExecToolConfig` 之前插入：

```python
class SoftTrimConfig(Base):
    """softTrim：对超长 tool result 保留头尾，中间截断。"""
    max_chars: int = 4000       # content 超过此长度才 softTrim
    head_chars: int = 1500      # 保留头部字符数
    tail_chars: int = 1500      # 保留尾部字符数


class HardClearConfig(Base):
    """hardClear：对占比过大的 tool result 整块替换为 placeholder。"""
    enabled: bool = True
    placeholder: str = "[Old tool result content cleared]"
    # tool result chars / context_window_chars 超过此比例触发 hardClear
    ratio: float = 0.5


class ContextPruningConfig(Base):
    """每次 LLM call 前对 context 中过大的 tool result 做 transient 修剪。"""
    enabled: bool = False
    keep_last_assistants: int = 3       # 保护最近 N 条 assistant 之后的 tool results
    min_prunable_tool_chars: int = 50_000  # 总 tool chars 低于此值不触发
    soft_trim: SoftTrimConfig = Field(default_factory=SoftTrimConfig)
    hard_clear: HardClearConfig = Field(default_factory=HardClearConfig)
```

在 `AgentDefaults` 类末尾追加字段：

```python
    context_pruning: ContextPruningConfig = Field(default_factory=ContextPruningConfig)
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /root/git_code/nanobot && python3 -m pytest tests/config/test_config_migration.py -k "pruning" -v
```

期望：3 个测试 PASS

- [ ] **Step 5: Commit**

```bash
cd /root/git_code/nanobot
git add nanobot/config/schema.py tests/config/test_config_migration.py
git commit -m "feat(config): add ContextPruningConfig schema (softTrim/hardClear)"
```

---

## Chunk 2: ContextPruner 核心逻辑

### Task 2: 实现 pruner.py

**Files:**
- Create: `nanobot/agent/pruner.py`
- Create: `tests/agent/test_context_pruner.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/agent/test_context_pruner.py`：

```python
"""Tests for ContextPruner: softTrim / hardClear logic."""
from __future__ import annotations

import pytest
from nanobot.agent.pruner import ContextPruner
from nanobot.config.schema import ContextPruningConfig, SoftTrimConfig, HardClearConfig


def _make_cfg(**kwargs) -> ContextPruningConfig:
    return ContextPruningConfig(enabled=True, **kwargs)


def _tool_msg(content: str, tool_call_id: str = "t1") -> dict:
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
        soft_trim=SoftTrimConfig(max_chars=100, head_chars=20, tail_chars=20),
        hard_clear=HardClearConfig(enabled=False),
    )
    pruner = ContextPruner(cfg)
    long_content = "A" * 50 + "B" * 50 + "C" * 50   # 150 chars
    msgs = [_user_msg(), _assistant_msg(), _assistant_msg(), _assistant_msg(),
            _tool_msg(long_content)]  # 需要在保护边界之前
    # 插入足够多 assistant 让这条 tool 在保护边界之外
    msgs = [_user_msg(), _tool_msg(long_content), _assistant_msg(), _assistant_msg(), _assistant_msg()]
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
        hard_clear=HardClearConfig(enabled=True, ratio=0.1, placeholder="[cleared]"),
        soft_trim=SoftTrimConfig(max_chars=999999),  # 不触发 softTrim
    )
    pruner = ContextPruner(cfg)
    big_content = "X" * 10_000
    # context_window_chars=50_000, ratio=0.1 → 阈值 5000，内容 10000 > 5000
    msgs = [_user_msg(), _tool_msg(big_content), _assistant_msg(), _assistant_msg(), _assistant_msg()]
    result = pruner.prune(msgs, context_window_chars=50_000)
    assert result[1]["content"] == "[cleared]"


def test_hard_clear_disabled():
    """hard_clear.enabled=False → 不替换，只走 softTrim。"""
    cfg = _make_cfg(
        min_prunable_tool_chars=0,
        hard_clear=HardClearConfig(enabled=False),
        soft_trim=SoftTrimConfig(max_chars=999999),
    )
    pruner = ContextPruner(cfg)
    big_content = "X" * 10_000
    msgs = [_user_msg(), _tool_msg(big_content), _assistant_msg(), _assistant_msg(), _assistant_msg()]
    result = pruner.prune(msgs, context_window_chars=50_000)
    assert result[1]["content"] == big_content  # 没有被替换


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
    old_content = "0123456789"  # 10 chars > max_chars=5，应被 softTrim
    new_content = "ABCDEFGHIJ"  # 同上，但在保护边界内，不应被修剪

    msgs = [
        _user_msg(),
        _tool_msg(old_content, "old"),   # 在保护边界之前 → 修剪
        _assistant_msg("a1"),
        _tool_msg(new_content, "new"),   # 在保护边界之内 → 不修剪
        _assistant_msg("a2"),             # keep_last_assistants=2：保护这里及之后
    ]
    result = pruner.prune(msgs, context_window_chars=400_000)
    assert result[1]["content"] != old_content   # 被 softTrim
    assert result[3]["content"] == new_content   # 保护不动


# ─── image block 跳过 ────────────────────────────────────────────────────────

def test_skip_image_block_tool_result():
    """tool result content 为含 image block 的 list → 不修剪。"""
    cfg = _make_cfg(
        min_prunable_tool_chars=0,
        hard_clear=HardClearConfig(enabled=True, ratio=0.0),  # ratio=0 → 任何都应触发
        soft_trim=SoftTrimConfig(max_chars=0),
    )
    pruner = ContextPruner(cfg)
    image_content = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}]
    msgs = [_user_msg(), _tool_msg(image_content), _assistant_msg(), _assistant_msg(), _assistant_msg()]  # type: ignore
    result = pruner.prune(msgs, context_window_chars=400_000)
    assert result[1]["content"] == image_content  # 原样


# ─── enabled=False ───────────────────────────────────────────────────────────

def test_pruner_disabled_returns_original():
    """enabled=False → ContextPruner 直接返回原 messages（调用方应避免调用，但仍安全）。"""
    cfg = ContextPruningConfig(enabled=False)
    pruner = ContextPruner(cfg)
    msgs = [_user_msg(), _tool_msg("X" * 100_000)]
    result = pruner.prune(msgs, context_window_chars=1000)
    assert result == msgs
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /root/git_code/nanobot && python3 -m pytest tests/agent/test_context_pruner.py -v
```

期望：`ImportError: cannot import name 'ContextPruner'`

- [ ] **Step 3: 实现 pruner.py**

创建 `nanobot/agent/pruner.py`：

```python
"""Context pruner: transient softTrim / hardClear for tool results before each LLM call."""
from __future__ import annotations

from typing import Any

from loguru import logger

from nanobot.config.schema import ContextPruningConfig


def _is_image_content(content: Any) -> bool:
    """含 image block 的 list content → True，跳过修剪。"""
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") in ("image_url", "image")
        for block in content
    )


def _count_tool_chars(messages: list[dict]) -> int:
    """统计所有 tool result 的总字符数（仅 str content）。"""
    total = 0
    for msg in messages:
        if msg.get("role") == "tool":
            c = msg.get("content")
            if isinstance(c, str):
                total += len(c)
    return total


def _find_protection_boundary(messages: list[dict], keep_last_assistants: int) -> int:
    """返回保护边界 index：该 index（含）之后的 tool result 不修剪。

    从后往前数 keep_last_assistants 条 assistant message，取第一条的 index。
    找不到足够的 assistant → 返回 len(messages)（全部保护，不修剪任何内容）。
    """
    count = 0
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            count += 1
            if count == keep_last_assistants:
                return i
    return len(messages)  # 没有足够的 assistant → 全部保护


class ContextPruner:
    """在每次 LLM call 前对 context 中的 tool result 做 transient softTrim / hardClear。

    规则优先级（对每条 tool result 依次检查）：
    1. 跳过：在保护边界之后
    2. 跳过：content 为 list（含 image block）
    3. hardClear：content chars / context_window_chars > ratio → 替换 placeholder
    4. softTrim：content chars > max_chars → 保留 head + tail
    """

    def __init__(self, config: ContextPruningConfig):
        self.config = config

    def prune(self, messages: list[dict], context_window_chars: int) -> list[dict]:
        """返回修剪后的 messages（新 list，原 list 不变）。"""
        if not self.config.enabled:
            return messages

        # ── 总量 guard ────────────────────────────────────────────────────────
        total_tool_chars = _count_tool_chars(messages)
        if total_tool_chars < self.config.min_prunable_tool_chars:
            return messages

        # ── 保护边界 ──────────────────────────────────────────────────────────
        boundary = _find_protection_boundary(messages, self.config.keep_last_assistants)

        result = []
        pruned_count = 0

        for i, msg in enumerate(messages):
            if msg.get("role") != "tool" or i >= boundary:
                result.append(msg)
                continue

            content = msg.get("content")

            # 跳过 image block
            if _is_image_content(content):
                result.append(msg)
                continue

            if not isinstance(content, str) or not content:
                result.append(msg)
                continue

            # hardClear 优先
            new_content = self._maybe_hard_clear(content, context_window_chars)
            if new_content is not None:
                result.append({**msg, "content": new_content})
                pruned_count += 1
                continue

            # softTrim
            new_content = self._maybe_soft_trim(content)
            if new_content is not content:
                result.append({**msg, "content": new_content})
                pruned_count += 1
                continue

            result.append(msg)

        if pruned_count:
            logger.debug(
                "ContextPruner: pruned {} tool result(s) (total_tool_chars={}, boundary={})",
                pruned_count,
                total_tool_chars,
                boundary,
            )

        return result

    def _maybe_hard_clear(self, content: str, context_window_chars: int) -> str | None:
        """若 content 占比超阈值，返回 placeholder；否则 None。"""
        hc = self.config.hard_clear
        if not hc.enabled or context_window_chars <= 0:
            return None
        if len(content) / context_window_chars > hc.ratio:
            return hc.placeholder
        return None

    def _maybe_soft_trim(self, content: str) -> str:
        """若 content 超 max_chars，返回 head + '...' + tail；否则原样返回。"""
        st = self.config.soft_trim
        if len(content) <= st.max_chars:
            return content
        head = content[: st.head_chars]
        tail = content[-st.tail_chars :] if st.tail_chars > 0 else ""
        trimmed = len(content) - st.head_chars - st.tail_chars
        return f"{head}\n...[{trimmed} chars trimmed]...\n{tail}"
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /root/git_code/nanobot && python3 -m pytest tests/agent/test_context_pruner.py -v
```

期望：10 个测试全部 PASS

- [ ] **Step 5: Commit**

```bash
cd /root/git_code/nanobot
git add nanobot/agent/pruner.py tests/agent/test_context_pruner.py
git commit -m "feat(agent): add ContextPruner with softTrim/hardClear for tool results"
```

---

## Chunk 3: AgentLoop 集成

### Task 3: loop.py 集成 ContextPruner

**Files:**
- Modify: `nanobot/agent/loop.py`

- [ ] **Step 1: 写集成测试（验证 pruner 被调用）**

在 `tests/agent/test_context_pruner.py` 末尾追加：

```python
def test_pruner_not_called_when_disabled():
    """enabled=False 时 prune() 直接返回原 messages。"""
    from unittest.mock import patch, MagicMock
    cfg = ContextPruningConfig(enabled=False)
    pruner = ContextPruner(cfg)
    msgs = [{"role": "user", "content": "hi"}]
    with patch.object(pruner, "_maybe_hard_clear") as mock_hc:
        result = pruner.prune(msgs, context_window_chars=100000)
    mock_hc.assert_not_called()
    assert result == msgs
```

- [ ] **Step 2: 运行测试确认通过**

```bash
cd /root/git_code/nanobot && python3 -m pytest tests/agent/test_context_pruner.py -v
```

期望：11 个测试全部 PASS

- [ ] **Step 3: 修改 loop.py**

**a. `__init__` 顶部 import 区加入：**
```python
from nanobot.agent.pruner import ContextPruner
```

**b. `AgentLoop.__init__` 参数列表增加（在 `mcp_servers` 之后）：**
```python
context_pruning_config=None,  # ContextPruningConfig | None
```

**c. `__init__` 方法体末尾（`_register_default_tools` 调用之前）加入：**
```python
from nanobot.config.schema import ContextPruningConfig
_pruning_cfg = context_pruning_config or ContextPruningConfig()
self.pruner: ContextPruner | None = (
    ContextPruner(_pruning_cfg) if _pruning_cfg.enabled else None
)
```

**d. `_run_agent_loop` while 循环开头，`tool_defs = self.tools.get_definitions()` 之后插入：**
```python
# ── transient context pruning（每次 LLM call 前）────────────────────────────
if self.pruner:
    context_window_chars = self.context_window_tokens * 4
    messages = self.pruner.prune(messages, context_window_chars)
```

**e. `AgentLoop` 的调用方（`nanobot/cli/commands.py` 或 `nanobot/__main__.py`）找到构建 AgentLoop 的地方，传入 config：**

搜索 `AgentLoop(` 的位置，在构造调用里加上：
```python
context_pruning_config=config.agents.defaults.context_pruning,
```

- [ ] **Step 4: 运行全量工具测试确认无回归**

```bash
cd /root/git_code/nanobot && python3 -m pytest tests/agent/ tests/tools/ tests/config/ -v
```

期望：全部 PASS（包含之前 rtk 测试）

- [ ] **Step 5: Commit**

```bash
cd /root/git_code/nanobot
git add nanobot/agent/loop.py
git commit -m "feat(agent): integrate ContextPruner into AgentLoop._run_agent_loop"
```

---

## Chunk 4: 文档 + 配置示例 + 最终 Push

### Task 4: 更新 CLAUDE.md + push

**Files:**
- Modify: `nanobot/agent/CLAUDE.md`（若存在）或根目录 CLAUDE.md

- [ ] **Step 1: 检查 CLAUDE.md 现状**

```bash
cat /root/git_code/nanobot/CLAUDE.md
```

- [ ] **Step 2: 在 agent/ 目录结构说明里加入 pruner.py**

在 `nanobot/agent/` 的文件说明中追加：
```
- pruner.py     # ContextPruner: transient softTrim/hardClear for tool results
```

- [ ] **Step 3: push 所有 commits**

```bash
cd /root/git_code/nanobot && git push origin main
```

- [ ] **Step 4: 验证 config 可被加载且 pruning 默认关闭**

```bash
cd /root/git_code/nanobot && /root/git_code/nanobot/.venv/bin/python3 -c "
from nanobot.config.loader import load_config
cfg = load_config()
print('context_pruning.enabled:', cfg.agents.defaults.context_pruning.enabled)
print('soft_trim.max_chars:', cfg.agents.defaults.context_pruning.soft_trim.max_chars)
"
```

期望输出：
```
context_pruning.enabled: False
soft_trim.max_chars: 4000
```

- [ ] **Step 5: 最终 commit（若有 CLAUDE.md 改动）**

```bash
cd /root/git_code/nanobot
git add CLAUDE.md nanobot/agent/CLAUDE.md 2>/dev/null || true
git commit -m "docs: add ContextPruner to architecture docs" --allow-empty
git push origin main
```

---

## 验收标准

- [ ] `python3 -m pytest tests/agent/test_context_pruner.py -v` → 11 个 PASS
- [ ] `python3 -m pytest tests/ -v` → 无回归
- [ ] `load_config().agents.defaults.context_pruning.enabled` → `False`（默认关闭）
- [ ] 在 `~/.nanobot/config.json` 加入 `"agents": {"defaults": {"contextPruning": {"enabled": true}}}` 后，pruner 生效
- [ ] `git log --oneline -5` 显示 3 个新 commit
