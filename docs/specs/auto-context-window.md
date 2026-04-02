# Spec: Auto Context Window Detection

## Problem

`contextWindowTokens` 是一个纯手动配置项（默认 65,536）。用户换模型后必须自己查并手动更新这个值，否则：

- **值太小**（如默认 65k 但实际模型支持 1M）→ consolidation 过早触发，白白浪费 LLM 调用做摘要，丢失可用上下文空间
- **值太大**（如配 1M 但模型只有 8k）→ consolidation 永远不触发，prompt 撑爆 context window 导致 API 报错

当前代码中 `cli/models.py` 的 `get_model_context_limit()` 已被清空（litellm 替换期间），永远返回 `None`。

## Goal

启动时自动获取当前模型的 context window 大小，消除手动维护 `contextWindowTokens` 的负担。

## Design

### 数据来源

| Provider 类型 | API 端点 | 返回字段 |
|---|---|---|
| Anthropic 原生 | `GET /v1/models` | `max_input_tokens` (int) |
| OpenAI 兼容 (含代理) | `GET /v1/models` 或 `GET /v1/models/{model}` | `context_length` 或 `context_window` (int) |
| Azure OpenAI | 不支持 models 端点 | 使用内置 lookup table |

Anthropic 官方 API 文档确认返回：`max_input_tokens` (number)

**⚠️ 代理场景注意**：部分代理（如 kimi）的 `/v1/models` 返回的是代理自己的模型信息（如 `kimi-for-coding, context_length: 262144`），而非实际转发的目标模型信息。API 获取时必须校验返回的 model id 与请求的 model name 匹配，不匹配则丢弃。

### 优先级

```
API 动态获取 > 内置 lookup table > 用户配置值 > 默认值 (65536)
```

用户配置值作为 fallback：当 API 和 lookup 都拿不到时，使用用户在 config 中填写的值。

### 内置 Lookup Table

作为 fallback（API 不可达、代理不支持 /models 端点、代理返回不匹配的 model id 等场景）：

```python
_KNOWN_CONTEXT_WINDOWS = {
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
    # OpenAI
    "gpt-4o-mini": 128_000,
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "gpt-3.5-turbo": 16_385,
    "o1": 200_000,
    "o1-mini": 128_000,
    "o3": 200_000,
    "o3-mini": 200_000,
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
    "llama-3.1": 131_072,
    "llama-3.2": 131_072,
    "llama-3.3": 131_072,
    # Mistral
    "mistral-large": 128_000,
}
```

Lookup 逻辑：
1. 去掉 provider prefix（`anthropic/claude-sonnet-4-6` → `claude-sonnet-4-6`）
2. 去掉日期后缀（正则 `-\d{8}$`，如 `claude-sonnet-4-6-20260301` → `claude-sonnet-4-6`）
3. 对 `_KNOWN_CONTEXT_WINDOWS` 做**最长前缀匹配**（按 key 长度降序排序，逐个检查 `startswith`）

### 配置项变化

- `contextWindowTokens` 保留，语义变为 **fallback**
- 默认值改为 `0`
- 值为 `0` 时：启动时动态获取（API → lookup → 硬编码 65536）
- 值为正整数时：作为 fallback（API 和 lookup 都失败时使用该值）
- **不支持字符串 "auto"**，保持字段类型为 `int`，避免破坏性变更

### 获取时机

- **启动时**：`cli/commands.py` 的 async 启动路径中，provider 创建后、AgentLoop 初始化前调用一次
- **SDK 路径**（`nanobot.py` 的 `from_config()`）：同步方法，仅使用 lookup table，不调 API
- **超时**：API 调用最多 5 秒，超时 fallback 到下一层
- **失败处理**：API 失败 → lookup table → 用户配置值 → 硬编码 65536
- **日志**：记录最终使用的值和来源（api / lookup / config / default）

### FallbackProvider 处理

当配置了 `fallback_models` 时，使用 **primary provider** 的 context window。FallbackProvider 只是链式切换 provider，上下文管理始终基于 primary。

### 不做的事

- 不做运行时动态切换（模型不会在运行中途变）
- 不缓存到磁盘（每次启动重新获取，保证最新）
- 不修改 consolidation 逻辑本身，只改输入的 `context_window_tokens` 值
- 不在 AgentLoop 首次 process 时异步更新（避免过度设计）

## Affected Files

| 文件 | 变更 |
|---|---|
| `nanobot/cli/models.py` | 重新实现 `get_model_context_limit()`，加入 lookup table + 前缀匹配 |
| `nanobot/providers/base.py` | 新增 `async def fetch_model_context_window(model) -> int \| None` 默认方法 |
| `nanobot/providers/anthropic_provider.py` | override：调 `/v1/models`，匹配 model id，取 `max_input_tokens` |
| `nanobot/providers/openai_compat_provider.py` | override：调 `/v1/models`，严格匹配 model id，取 `context_length` |
| `nanobot/providers/azure_openai_provider.py` | 不 override（继承默认 None） |
| `nanobot/providers/context_window.py` | 新文件：`resolve_context_window()` 统一 resolve 函数 |
| `nanobot/nanobot.py` | `from_config()` 中用同步 lookup resolve |
| `nanobot/cli/commands.py` | 3 处 AgentLoop 创建前用 `await resolve_context_window()` |
| `nanobot/config/schema.py` | `context_window_tokens` 默认值改为 `0` |
| `tests/test_auto_context_window.py` | 新文件：完整测试覆盖 |
