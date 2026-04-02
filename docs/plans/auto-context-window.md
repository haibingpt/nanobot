# Plan: Auto Context Window Detection

> Spec: `docs/specs/auto-context-window.md`

## Phase 1: Lookup Table + Resolve 函数

### Step 1.1: 重建 `cli/models.py`

重新实现 `get_model_context_limit(model, provider)`:

```python
import re

_KNOWN_CONTEXT_WINDOWS: dict[str, int] = {
    # Anthropic Claude 4.6
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    # Anthropic Claude 4.5
    "claude-opus-4-5": 200_000,
    "claude-sonnet-4-5": 200_000,
    # Anthropic Claude 4
    "claude-opus-4": 200_000,
    "claude-sonnet-4": 200_000,
    # Anthropic Claude 3.x
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
    "claude-3-opus": 200_000,
    "claude-3-sonnet": 200_000,
    "claude-3-haiku": 200_000,
    # OpenAI — 注意顺序：长 key 在前，短 key 在后（虽然算法按长度排序，但视觉上也保持一致）
    "gpt-4o-mini": 128_000,
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "gpt-3.5-turbo": 16_385,
    "o1-mini": 128_000,
    "o1": 200_000,
    "o3-mini": 200_000,
    "o3": 200_000,
    "o4-mini": 200_000,
    # DeepSeek
    "deepseek-chat": 65_536,
    "deepseek-reasoner": 65_536,
    # Gemini
    "gemini-2.5-pro": 1_000_000,
    "gemini-2.5-flash": 1_000_000,
    "gemini-2.0-flash": 1_000_000,
    # Qwen
    "qwen-max": 131_072,
    "qwen-plus": 131_072,
    # Llama
    "llama-3.3": 131_072,
    "llama-3.2": 131_072,
    "llama-3.1": 131_072,
    # Mistral
    "mistral-large": 128_000,
}

# 预排序：按 key 长度降序，确保最长前缀优先匹配
_SORTED_KEYS = sorted(_KNOWN_CONTEXT_WINDOWS.keys(), key=len, reverse=True)

_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")


def _normalize_model_name(model: str) -> str:
    """Strip provider prefix and date suffix from model name.

    Examples:
        "anthropic/claude-sonnet-4-6-20260301" → "claude-sonnet-4-6"
        "openrouter/openai/gpt-4o" → "gpt-4o"
        "claude-3-opus-20240229" → "claude-3-opus"
    """
    # Strip provider prefix: take last segment after "/"
    if "/" in model:
        model = model.rsplit("/", 1)[-1]
    # Strip date suffix: -YYYYMMDD
    model = _DATE_SUFFIX_RE.sub("", model)
    return model.lower()


def get_model_context_limit(model: str, provider: str = "auto") -> int | None:
    """Look up known context window for a model name. Returns None if unknown."""
    normalized = _normalize_model_name(model)
    for key in _SORTED_KEYS:
        if normalized.startswith(key):
            return _KNOWN_CONTEXT_WINDOWS[key]
    return None
```

其余函数（`get_all_models`, `find_model_info`, `get_model_suggestions`）保持返回空值不变。

### Step 1.2: 修改 `config/schema.py`

```python
# Before
context_window_tokens: int = 65_536

# After
context_window_tokens: int = 0  # 0 = auto-detect
```

### Step 1.3: 新增 `providers/context_window.py`

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from nanobot.cli.models import get_model_context_limit

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider

_HARD_DEFAULT = 65_536


async def resolve_context_window(
    provider: LLMProvider,
    model: str,
    configured_value: int,
) -> tuple[int, str]:
    """Resolve context window tokens with fallback chain.

    Priority: API > lookup table > user config > hard default.
    Returns (tokens, source) where source is "api" | "lookup" | "config" | "default".
    """
    # 1. Try API (async, with timeout)
    try:
        api_value = await provider.fetch_model_context_window(model)
        if api_value and api_value > 0:
            logger.info(
                "Context window: {:,} tokens (source: api, model: {})",
                api_value, model,
            )
            return api_value, "api"
    except Exception:
        logger.debug("API context window fetch failed for {}", model)

    # 2. Try lookup table
    lookup_value = get_model_context_limit(model)
    if lookup_value:
        logger.info(
            "Context window: {:,} tokens (source: lookup, model: {})",
            lookup_value, model,
        )
        return lookup_value, "lookup"

    # 3. Use user-configured value (if non-zero)
    if configured_value > 0:
        logger.info(
            "Context window: {:,} tokens (source: config, model: {})",
            configured_value, model,
        )
        return configured_value, "config"

    # 4. Hard default
    logger.warning(
        "Context window: {:,} tokens (source: default — consider setting contextWindowTokens in config, model: {})",
        _HARD_DEFAULT, model,
    )
    return _HARD_DEFAULT, "default"


def resolve_context_window_sync(
    model: str,
    configured_value: int,
) -> tuple[int, str]:
    """Synchronous resolve — lookup table + config only, no API call.

    Used by Nanobot.from_config() which is a sync method.
    """
    lookup_value = get_model_context_limit(model)
    if lookup_value:
        return lookup_value, "lookup"
    if configured_value > 0:
        return configured_value, "config"
    return _HARD_DEFAULT, "default"
```

### Step 1.4: 修改 `nanobot.py` 的 `from_config()`

在创建 AgentLoop 之前，用 `resolve_context_window_sync()` 获取值：

```python
from nanobot.providers.context_window import resolve_context_window_sync

# ... inside from_config():
ctx_tokens, ctx_source = resolve_context_window_sync(
    model=defaults.model,
    configured_value=defaults.context_window_tokens,
)
logger.info("Context window: {:,} tokens (source: {}, model: {})", ctx_tokens, ctx_source, defaults.model)

loop = AgentLoop(
    ...
    context_window_tokens=ctx_tokens,
    ...
)
```

### Step 1.5: 修改 `cli/commands.py`

3 处创建 AgentLoop 的地方，替换为 async resolve：

```python
from nanobot.providers.context_window import resolve_context_window

# 在每处创建 AgentLoop 之前:
ctx_tokens, ctx_source = await resolve_context_window(
    provider=provider,
    model=defaults.model,
    configured_value=defaults.context_window_tokens,
)
# 然后传入 AgentLoop(context_window_tokens=ctx_tokens, ...)
```

---

## Phase 2: API 动态获取

### Step 2.1: `providers/base.py` 加默认方法

```python
class LLMProvider(ABC):
    # ... existing code ...

    async def fetch_model_context_window(self, model: str) -> int | None:
        """Query the provider API for model context window size.

        Returns None if unsupported or model not found.
        Subclasses override for provider-specific implementations.
        """
        return None
```

不是 abstractmethod，提供默认 None 实现。

### Step 2.2: `providers/anthropic_provider.py` 实现

```python
async def fetch_model_context_window(self, model: str) -> int | None:
    """Fetch context window from Anthropic /v1/models endpoint."""
    import asyncio

    try:
        model_name = self._strip_prefix(model)
        response = await asyncio.wait_for(
            self._async_client.models.list(limit=1000),
            timeout=5.0,
        )
        for m in response.data:
            if m.id == model_name:
                return getattr(m, "max_input_tokens", None)
        # Model not found in list — proxy might be returning its own models
        logger.debug(
            "Model {} not found in /v1/models response ({} models listed)",
            model_name, len(response.data),
        )
        return None
    except Exception as e:
        logger.debug("Anthropic fetch_model_context_window failed: {}", e)
        return None
```

**关键**：严格匹配 `m.id == model_name`。如果代理返回的是自己的模型列表（如 kimi），匹配不上就返回 None，fallback 到 lookup table。

### Step 2.3: `providers/openai_compat_provider.py` 实现

使用已有依赖 `httpx`（项目已依赖 `httpx>=0.28.0`）：

```python
async def fetch_model_context_window(self, model: str) -> int | None:
    """Fetch context window from OpenAI-compatible /v1/models endpoint."""
    import httpx

    try:
        model_name = (model.split("/")[-1] if "/" in model else model)
        base = (self.api_base or "https://api.openai.com/v1").rstrip("/")
        headers = {"Authorization": f"Bearer {self.api_key}"}
        # 添加 extra_headers（如果有的话，部分代理需要）
        if hasattr(self, '_extra_headers') and self._extra_headers:
            headers.update(self._extra_headers)

        async with httpx.AsyncClient(timeout=5.0) as client:
            # 尝试 GET /models/{model}
            resp = await client.get(f"{base}/models/{model_name}", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                # 严格校验：返回的 id 必须匹配请求的 model
                returned_id = data.get("id", "")
                if returned_id == model_name or returned_id == model:
                    return data.get("context_length") or data.get("context_window")
                logger.debug(
                    "Model id mismatch: requested={}, returned={}",
                    model_name, returned_id,
                )

            # fallback: GET /models 列表
            resp = await client.get(f"{base}/models", headers=headers)
            if resp.status_code == 200:
                for m in resp.json().get("data", []):
                    mid = m.get("id", "")
                    if mid == model_name or mid == model:
                        return m.get("context_length") or m.get("context_window")

        return None
    except Exception as e:
        logger.debug("OpenAI-compat fetch_model_context_window failed: {}", e)
        return None
```

**安全**：httpx 的异常 repr 可能包含 URL，但不包含 Authorization header 值。logger.debug 级别确保生产环境默认不输出。

### Step 2.4: `providers/fallback.py` 处理

FallbackProvider 不 override `fetch_model_context_window`。resolve 函数调用时传入的是 primary provider（`cli/commands.py` 创建 fallback 前先 resolve，或从 FallbackProvider 中取 `.primary`）。

检查 `cli/commands.py` 中 FallbackProvider 的创建顺序：
- 如果 resolve 在 `_wrap_with_fallback()` 之前调用 → 传入的就是 primary，OK
- 如果 resolve 在之后调用 → 需要从 FallbackProvider 取 `.primary` 属性

**实现**：在 `nanobot.py` 和 `cli/commands.py` 中，确保 resolve 在 fallback wrap **之前**调用。

---

## Phase 3: 测试

### 新文件 `tests/test_auto_context_window.py`

```python
# --- _normalize_model_name tests ---
test_normalize_with_provider_prefix
    # "anthropic/claude-sonnet-4-6" → "claude-sonnet-4-6"

test_normalize_with_nested_prefix
    # "openrouter/anthropic/claude-sonnet-4-6" → "claude-sonnet-4-6"

test_normalize_with_date_suffix
    # "claude-sonnet-4-6-20260301" → "claude-sonnet-4-6"

test_normalize_with_prefix_and_date
    # "anthropic/claude-3-opus-20240229" → "claude-3-opus"

test_normalize_plain
    # "gpt-4o" → "gpt-4o"

# --- get_model_context_limit tests ---
test_lookup_exact_match
    # "claude-sonnet-4-6" → 1_000_000

test_lookup_with_prefix
    # "anthropic/claude-sonnet-4-6" → 1_000_000

test_lookup_with_date_suffix
    # "claude-3-opus-20240229" → 200_000

test_lookup_prefix_match_longest_wins
    # "gpt-4o-mini" → 128_000 (not gpt-4's 8_192)
    # "gpt-4o" → 128_000 (not gpt-4's 8_192)
    # "o1-mini" → 128_000 (not o1's 200_000)
    # "o3-mini" → 200_000 (not o3's 200_000 — same value but still tests matching)

test_lookup_unknown_model
    # "some-random-model" → None

# --- resolve_context_window tests ---
test_resolve_api_wins
    # Mock provider returns 500_000 from API → (500_000, "api")

test_resolve_api_fails_falls_to_lookup
    # Mock provider returns None → falls to lookup → e.g. (1_000_000, "lookup")

test_resolve_api_and_lookup_fail_falls_to_config
    # Unknown model + API fail + configured_value=300_000 → (300_000, "config")

test_resolve_all_fail_returns_default
    # Unknown model + API fail + configured_value=0 → (65_536, "default")

test_resolve_api_returns_zero_ignored
    # Mock provider returns 0 → treated as failure → falls to lookup

# --- resolve_context_window_sync tests ---
test_sync_resolve_lookup_hit
    # Known model → (value, "lookup")

test_sync_resolve_config_fallback
    # Unknown model + configured_value=200_000 → (200_000, "config")

test_sync_resolve_default
    # Unknown model + configured_value=0 → (65_536, "default")
```

---

## 实施顺序

| 步骤 | 文件 | 依赖 |
|---|---|---|
| 1 | `cli/models.py` — lookup table + normalize + 匹配 | 无 |
| 2 | `config/schema.py` — 默认值改 0 | 无 |
| 3 | `providers/base.py` — 加 `fetch_model_context_window` 默认方法 | 无 |
| 4 | `providers/context_window.py` — resolve 函数（async + sync） | 1, 3 |
| 5 | `providers/anthropic_provider.py` — override API 获取 | 3 |
| 6 | `providers/openai_compat_provider.py` — override API 获取 | 3 |
| 7 | `nanobot.py` — 接入 sync resolve | 4 |
| 8 | `cli/commands.py` — 3 处接入 async resolve | 4 |
| 9 | `tests/test_auto_context_window.py` — 全部测试 | 1, 4 |

步骤 1-4 可以一起提交（Phase 1），步骤 5-6 一起提交（Phase 2），步骤 7-9 一起提交（集成 + 测试）。

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| 代理返回错误 model 信息 | 严格 model id 匹配，不匹配则丢弃 |
| API 调用阻塞启动 | 5 秒超时硬上限 |
| lookup table 过时 | 日志输出 `source: default` 提醒，新模型通过 PR 更新 table |
| 配置默认值变更影响已有用户 | 已有 config 中的正整数仍作为 fallback 生效 |
| httpx error repr 泄露敏感信息 | 仅 debug 级别日志，Authorization header 不出现在 repr 中 |

## Task Size

- Phase 1 (lookup + resolve): ~150 行新代码 + ~20 行改动
- Phase 2 (API fetch): ~80 行新代码
- Phase 3 (tests): ~120 行
- 总计: ~370 行，预计 3-4 小时
