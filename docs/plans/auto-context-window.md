# Plan: Auto Context Window Detection

> Spec: `docs/specs/auto-context-window.md`

## Phase 1: Lookup Table（零网络依赖，立即可用）

### Step 1.1: 重建 `cli/models.py`

重新实现 `get_model_context_limit(model, provider)`:

```python
_KNOWN_CONTEXT_WINDOWS = {
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-opus-4-5": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-opus-4": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-opus": 200_000,
    "claude-3-sonnet": 200_000,
    "claude-3-haiku": 200_000,
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "gpt-3.5-turbo": 16_385,
    "o1": 200_000,
    "o1-mini": 128_000,
    "o3": 200_000,
    "o3-mini": 200_000,
    "o4-mini": 200_000,
    "deepseek-chat": 65_536,
    "deepseek-reasoner": 65_536,
    "gemini-2.5-pro": 1_000_000,
    "gemini-2.5-flash": 1_000_000,
    "gemini-2.0-flash": 1_000_000,
    "qwen-max": 131_072,
    "qwen-plus": 131_072,
}
```

匹配逻辑：
1. 去掉 provider prefix（`anthropic/claude-sonnet-4-6` → `claude-sonnet-4-6`）
2. 去掉日期后缀（`claude-sonnet-4-6-20260301` → `claude-sonnet-4-6`）
3. 对 `_KNOWN_CONTEXT_WINDOWS` 做**最长前缀匹配**

**测试**: 验证各种 model name 格式（带 prefix、带日期后缀、简写）都能正确匹配。

### Step 1.2: 修改 `config/schema.py`

- `context_window_tokens` 默认值从 `65_536` 改为 `0`
- `0` 表示 "auto"（向后兼容：已有配置里的正整数仍作为显式 override）

### Step 1.3: 新增 `providers/context_window.py`

统一的 resolve 函数:

```python
async def resolve_context_window(
    provider: LLMProvider,
    model: str,
    configured_value: int,
) -> tuple[int, str]:
    """返回 (tokens, source)。source 为 "api" | "lookup" | "config" | "default"。"""
```

逻辑:
1. 如果 `configured_value > 0` → 直接返回 `(configured_value, "config")`
2. 尝试 `await provider.fetch_model_context_window(model)`（Phase 2 实现，Phase 1 返回 None）
3. 如果拿到 → 返回 `(value, "api")`
4. 尝试 `get_model_context_limit(model)` lookup table
5. 如果拿到 → 返回 `(value, "lookup")`
6. 返回 `(65_536, "default")`

### Step 1.4: 修改 `nanobot.py` 的 `from_config()`

在创建 `AgentLoop` 之前，调用 `resolve_context_window()` 获取实际值，传入 `AgentLoop` 构造函数。

注意：`from_config()` 目前是同步方法。两种选择：
- **方案 A**: 改为 `async def from_config()` — 影响调用链
- **方案 B**: 在同步上下文中只用 lookup table，API 动态获取放在 AgentLoop 启动后异步执行

**选择方案 B**：Phase 1 在同步路径中只做 lookup，Phase 2 再加异步 API 获取。

### Step 1.5: 修改 `cli/commands.py`

`cli/commands.py` 中 3 处创建 AgentLoop 的地方（gateway、CLI run、CLI chat），同样接入 resolve 逻辑。这些路径是 async 的，可以直接用 `await resolve_context_window()`。

### Step 1.6: 日志

在 resolve 完成后输出一条 info 日志：
```
Context window: 1,000,000 tokens (source: lookup, model: claude-sonnet-4-6)
```

---

## Phase 2: API 动态获取

### Step 2.1: `providers/base.py` 加接口

```python
class LLMProvider(ABC):
    async def fetch_model_context_window(self, model: str) -> int | None:
        """Query the provider API for model context window size. Returns None if unsupported."""
        return None
```

默认实现返回 None，子类按需 override。

### Step 2.2: `providers/anthropic_provider.py` 实现

```python
async def fetch_model_context_window(self, model: str) -> int | None:
    try:
        # 使用已初始化的 self._async_client
        response = await asyncio.wait_for(
            self._async_client.models.list(limit=100),
            timeout=5.0,
        )
        model_name = self._strip_prefix(model)
        for m in response.data:
            if m.id == model_name:
                return m.max_input_tokens
        return None
    except Exception:
        logger.debug("Failed to fetch context window from Anthropic API")
        return None
```

### Step 2.3: `providers/openai_compat_provider.py` 实现

```python
async def fetch_model_context_window(self, model: str) -> int | None:
    try:
        # 直接 HTTP 请求 /v1/models
        import httpx
        base = (self.api_base or "https://api.openai.com/v1").rstrip("/")
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with httpx.AsyncClient(timeout=5.0) as client:
            # 先尝试单个模型
            resp = await client.get(f"{base}/models/{model}", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("context_length") or data.get("context_window")
            # fallback: 列出所有模型
            resp = await client.get(f"{base}/models", headers=headers)
            if resp.status_code == 200:
                for m in resp.json().get("data", []):
                    if m.get("id") == model:
                        return m.get("context_length") or m.get("context_window")
        return None
    except Exception:
        logger.debug("Failed to fetch context window from OpenAI-compat API")
        return None
```

### Step 2.4: 接入 `resolve_context_window()`

Phase 1 的 resolve 函数中 step 2（API 调用）变为有效路径。在 `cli/commands.py` 的 async 启动路径中生效。

### Step 2.5: `nanobot.py` 的 `from_config()` 处理

`from_config()` 保持同步。如果 `configured_value == 0`：
- 同步路径先用 lookup table 得到初始值
- AgentLoop 启动后（第一次 process 之前），异步调用 API 更新值

在 `AgentLoop` 中加一个 `async def _maybe_update_context_window()` 方法，在首次 `process_message()` 时调一次。

---

## Phase 3: 测试

### `tests/test_auto_context_window.py`

```
test_lookup_with_prefix          # "anthropic/claude-sonnet-4-6" → 1M
test_lookup_with_date_suffix     # "claude-sonnet-4-6-20260301" → 1M
test_lookup_prefix_match         # "claude-3-opus-20240229" → 200k
test_lookup_unknown_model        # "some-random-model" → None
test_resolve_config_override     # configured_value=500000 → 直接返回
test_resolve_api_priority        # API 返回值优先于 lookup
test_resolve_fallback_chain      # API fail → lookup → default
test_strip_model_name            # 各种 prefix/suffix 格式的 strip 逻辑
```

---

## Rollout / 风险

- **向后兼容**：已有配置中 `contextWindowTokens` 为正整数的用户不受影响（直接使用配置值）
- **新安装**：默认值 0 → auto → lookup/API
- **API 失败**：有 5 秒超时 + lookup table fallback + 最终 65536 硬底线，不会阻塞启动
- **proxy 兼容性**：部分代理不支持 /models 端点，但有 lookup table 兜底
- **lookup table 过时**：新模型发布后 table 中没有 → 日志会输出 `source: default`，提醒用户手动配置或等更新

## Task Size

- Phase 1: ~200 行代码改动，1-2 小时
- Phase 2: ~150 行代码改动，1-2 小时
- Phase 3: ~100 行测试代码，30 分钟

总计约 450 行，预计半天完成。
