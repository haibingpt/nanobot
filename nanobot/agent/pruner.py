"""Context pruner: transient softTrim / hardClear for tool results before each LLM call.

不改磁盘，不改 session history，只影响当次发给 LLM 的 messages。

优先级（对每条 tool result 依次检查）：
  1. 跳过：在 keepLastAssistants 保护边界之后
  2. 跳过：content 为含 image block 的 list
  3. hardClear：content chars / context_window_chars > ratio → 替换 placeholder
  4. softTrim：content chars > max_chars → 保留 head + tail
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from nanobot.config.schema import ContextPruningConfig


# ─── helpers ─────────────────────────────────────────────────────────────────

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
    return len(messages)


# ─── ContextPruner ────────────────────────────────────────────────────────────

class ContextPruner:
    """在每次 LLM call 前对 context 中的 tool result 做 transient 修剪。"""

    def __init__(self, config: ContextPruningConfig):
        self.config = config

    def prune(self, messages: list[dict], context_window_chars: int) -> list[dict]:
        """返回修剪后的 messages（新 list，原 list 不变）。"""
        if not self.config.enabled:
            return messages

        # 总量 guard：tool result 总字符未超阈值 → 不修剪
        total_tool_chars = _count_tool_chars(messages)
        if total_tool_chars < self.config.min_prunable_tool_chars:
            return messages

        boundary = _find_protection_boundary(messages, self.config.keep_last_assistants)

        result = []
        pruned_count = 0

        for i, msg in enumerate(messages):
            # 非 tool message 或在保护边界内 → 原样
            if msg.get("role") != "tool" or i >= boundary:
                result.append(msg)
                continue

            content = msg.get("content")

            # 跳过 image block
            if _is_image_content(content):
                result.append(msg)
                continue

            # 跳过非字符串 content（None、空等）
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
            trimmed = self._maybe_soft_trim(content)
            if trimmed is not content:
                result.append({**msg, "content": trimmed})
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
        """若 content 超 max_chars，返回 head + trimmed note + tail；否则原样。"""
        st = self.config.soft_trim
        if len(content) <= st.max_chars:
            return content
        head = content[: st.head_chars]
        tail = content[-st.tail_chars:] if st.tail_chars > 0 else ""
        trimmed_len = len(content) - st.head_chars - st.tail_chars
        return f"{head}\n...[{trimmed_len} chars trimmed]...\n{tail}"
