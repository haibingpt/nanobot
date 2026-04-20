# Subagent 模型分层 — 独立 provider 配置

## 概述

通过 `agents.defaults` 新增四个字段，让 SubagentManager 绑定独立的 LLM provider，用更快/更便宜的模型跑后台 subagent，而主 agent 保持强模型。**全局配置，启动时绑死**，运行时不可变。

**设计决策：**
- **配置级（A 方案）**：启动一次，绑死整个进程生命周期。不支持每次 spawn 传 model。
- **复用 `_make_single_provider`**：subagent provider 走和 fallback 完全相同的构造路径，不引入新 provider 工厂。
- **可选覆盖 reasoning_effort / max_tokens**：快模型不需要 high effort，大 token 预算也浪费。留单独字段。
- **subagent_model 不配时 = 退回主 agent provider**：向后兼容，已有 config 行为零变化。

## Non-Goals（本版本不做）

- ❌ **Per-spawn model 覆盖**：主 agent 运行时挑模型，听起来灵活实际上它没那个 taste。需要再观察半年的使用数据。
- ❌ **Subagent fallback chain**：快模型挂了就让 subagent 失败，主 agent 可以重试。加 fallback 会把"subagent 便宜"这个前提稀释掉。
- ❌ **Per-named-subagent 配置**：没有 named subagent 概念之前不做。等 named agent 架构进来后统一设计。
- ❌ **运行时热切换 subagent_model**：gateway 监听 config 变更会整体重启，自然承接新配置。已跑的 subagent 进程都没了无需处理。

## 文件变更地图

```
nanobot/
├── config/
│   └── schema.py             # [修改] AgentDefaults 新增 4 个字段
├── agent/
│   ├── loop.py               # [修改] 构造 SubagentManager 前造 subagent provider，传入
│   └── subagent.py           # [修改] __init__ 接受 provider/model/reasoning_effort/max_tokens；runner 用独立 provider；run_spec 用新字段
tests/
├── config/
│   └── test_subagent_model_schema.py     # [新建] 验证字段默认值和序列化
├── agent/
│   └── test_subagent_model_override.py   # [新建] 验证 SubagentManager 使用独立 provider/model + AgentRunSpec 字段透传
└── test_make_provider_fallback.py        # [修改] 增加一个 case 验证 subagent provider 可独立构造
docs/
└── examples/
    └── config-subagent-model.json        # [新建] 示例配置片段
```

## Task 1: config schema 新增字段

**文件：** `nanobot/config/schema.py`

### 1.1 AgentDefaults 追加字段

在 `AgentDefaults` 类中，`fallback_models` 之后、`skills` 之前插入：

```python
# --- Subagent model override (optional) ---
# 若不配置，subagent 复用主 agent 的 provider 和 model（向后兼容）。
# 配置后，SubagentManager 会用独立 provider 跑后台任务，主 agent 不受影响。
subagent_model: str | None = None
subagent_reasoning_effort: str | None = None  # None = 让 provider 按模型默认行为
subagent_max_tokens: int | None = None        # None = 继承主 agent max_tokens
```

**注意：** 不要加 `subagent_fallback_models`、`subagent_temperature`、`subagent_provider_retry_mode` 等字段——YAGNI。

### 1.2 测试

**文件：** `tests/config/test_subagent_model_schema.py`（新建）

写三个测试：

1. `test_defaults_are_none`：`Config()` 构造后四个字段都是 `None`。
2. `test_round_trip_json`：从 `{"agents": {"defaults": {"subagent_model": "anthropic/claude-haiku-4-5", "subagent_max_tokens": 4096}}}` 构造 Config，再 `model_dump()` 输出，两个字段值正确保留。
3. `test_empty_string_is_valid`：pydantic 对 `None` 和缺省字段应视为同义（不做特殊校验，让 `None` 成为"未配置"的唯一语义）。

**验证命令：**
```bash
cd /root/git_code/nanobot
.venv/bin/pytest tests/config/test_subagent_model_schema.py -v
```

**完成判据：** 3 个测试通过。

---

## Task 2: SubagentManager 接受独立 provider + model + 覆盖字段

**文件：** `nanobot/agent/subagent.py`

### 2.1 `__init__` 签名扩展

当前签名（约第 44-66 行）：

```python
def __init__(
    self,
    provider: LLMProvider,
    workspace: Path,
    bus: MessageBus,
    max_tool_result_chars: int,
    model: str | None = None,
    web_config: "WebToolsConfig | None" = None,
    exec_config: "ExecToolConfig | None" = None,
    restrict_to_workspace: bool = False,
    extra_hooks: list[AgentHook] | None = None,
):
```

改为：

```python
def __init__(
    self,
    provider: LLMProvider,
    workspace: Path,
    bus: MessageBus,
    max_tool_result_chars: int,
    model: str | None = None,
    web_config: "WebToolsConfig | None" = None,
    exec_config: "ExecToolConfig | None" = None,
    restrict_to_workspace: bool = False,
    extra_hooks: list[AgentHook] | None = None,
    reasoning_effort: str | None = None,
    max_tokens: int | None = None,
):
```

方法体末尾 `self.runner = AgentRunner(provider)` 之前，保存新字段：

```python
self.reasoning_effort = reasoning_effort
self.max_tokens = max_tokens
```

**说明：** `provider` 参数已经存在，调用方负责在此传入独立的 subagent provider（见 Task 3）。**不在 SubagentManager 内部构造 provider**——保持单一职责。

### 2.2 `_run_subagent` 使用新字段

定位到 `_run_subagent` 中调用 `self.runner.run(AgentRunSpec(...))` 的地方（约第 170 行附近）。当前：

```python
result = await self.runner.run(AgentRunSpec(
    initial_messages=messages,
    tools=tools,
    model=self.model,
    max_iterations=15,
    max_tool_result_chars=self.max_tool_result_chars,
    hook=self._compose_hook(task_id),
    max_iterations_message="Task completed but no final response was generated.",
    error_message=None,
    fail_on_tool_error=True,
))
```

改为：

```python
result = await self.runner.run(AgentRunSpec(
    initial_messages=messages,
    tools=tools,
    model=self.model,
    max_iterations=15,
    max_tool_result_chars=self.max_tool_result_chars,
    hook=self._compose_hook(task_id),
    max_iterations_message="Task completed but no final response was generated.",
    error_message=None,
    fail_on_tool_error=True,
    reasoning_effort=self.reasoning_effort,
    max_tokens=self.max_tokens,
))
```

**注意 `AgentRunSpec` 已有这两个字段**（`runner.py:46-47`），不需要改 runner。

### 2.3 测试

**文件：** `tests/agent/test_subagent_model_override.py`（新建）

模仿 `test_subagent_hook_composition.py` 的 mock 风格。写四个测试：

1. `test_default_none_reasoning_and_max_tokens`：
   - 构造 `SubagentManager` 不传 `reasoning_effort` / `max_tokens`。
   - `assert mgr.reasoning_effort is None`
   - `assert mgr.max_tokens is None`

2. `test_accepts_reasoning_effort`：
   - 传 `reasoning_effort="low"`，`assert mgr.reasoning_effort == "low"`

3. `test_accepts_max_tokens`：
   - 传 `max_tokens=4096`，`assert mgr.max_tokens == 4096`

4. `test_run_spec_includes_reasoning_and_max_tokens`：
   - Mock `AgentRunner.run`，spawn 一个任务。
   - 从 mock 调用捕获 `AgentRunSpec`，`assert spec.reasoning_effort == "low"`、`assert spec.max_tokens == 4096`、`assert spec.model == "subagent-model"`。
   - 这是关键集成点——字段从 config 到 AgentRunSpec 的传导路径。

**验证命令：**
```bash
.venv/bin/pytest tests/agent/test_subagent_model_override.py -v
```

**完成判据：** 4 个测试通过。已有 `test_subagent_hook_composition.py` 仍然通过（不破坏回归）。

---

## Task 3: AgentLoop 接线 — 构造 subagent provider 并传入

**文件：** `nanobot/agent/loop.py`

### 3.1 在构造 SubagentManager 之前构造 subagent provider

定位到当前（约第 254-266 行）：

```python
self.subagents = SubagentManager(
    provider=provider,
    workspace=workspace,
    bus=bus,
    model=self.model,
    max_tool_result_chars=self.max_tool_result_chars,
    web_config=self.web_config,
    exec_config=exec_config,
    restrict_to_workspace=restrict_to_workspace,
    extra_hooks=_subagent_extra_hooks,
)
```

在这段之前插入 provider 构造逻辑：

```python
# Subagent 可选使用独立 provider + model（配置化模型分层）
# 不配置时复用主 agent 的 provider，行为零变化
subagent_provider = provider
subagent_model = self.model
subagent_reasoning = None
subagent_max_tokens = None

if self._config is not None:
    defaults = self._config.agents.defaults
    if defaults.subagent_model:
        from nanobot.nanobot import _make_single_provider
        try:
            subagent_provider = _make_single_provider(
                self._config, defaults.subagent_model
            )
            subagent_model = defaults.subagent_model
            subagent_reasoning = defaults.subagent_reasoning_effort
            subagent_max_tokens = defaults.subagent_max_tokens
            logger.info(
                "Subagent using independent provider: model={}, reasoning={}, max_tokens={}",
                subagent_model, subagent_reasoning, subagent_max_tokens,
            )
        except Exception as e:
            logger.warning(
                "Failed to build subagent provider for {}: {}. Falling back to main agent provider.",
                defaults.subagent_model, e,
            )
            # 保持默认值，即主 agent provider
```

然后把 `SubagentManager(...)` 调用改为：

```python
self.subagents = SubagentManager(
    provider=subagent_provider,
    workspace=workspace,
    bus=bus,
    model=subagent_model,
    max_tool_result_chars=self.max_tool_result_chars,
    web_config=self.web_config,
    exec_config=exec_config,
    restrict_to_workspace=restrict_to_workspace,
    extra_hooks=_subagent_extra_hooks,
    reasoning_effort=subagent_reasoning,
    max_tokens=subagent_max_tokens,
)
```

**关键点：**
- Provider 构造失败时 **warn + fallback 主 agent provider**，不让启动失败。这是防御性设计——配置文件手滑写错一个 model 字符串，主循环应该仍能运行。
- `_make_single_provider` 的 import 放在 if 分支内部避免循环 import（`nanobot.py` 已 import `AgentLoop`）。

### 3.2 测试

**文件：** `tests/agent/test_subagent_model_override.py`（追加，不新建）

追加两个 AgentLoop 集成测试：

5. `test_loop_no_subagent_model_uses_main_provider`：
   - 构造 `Config`，不设 `subagent_model`。
   - 构造 `AgentLoop`（mock provider）。
   - `assert loop.subagents.provider is loop.provider`
   - `assert loop.subagents.model == loop.model`

6. `test_loop_with_subagent_model_uses_independent_provider`：
   - 构造 `Config`，设 `subagent_model="anthropic/claude-haiku-4-5"`、`subagent_max_tokens=4096`。
   - Mock `_make_single_provider` 返回一个 fake provider。
   - 构造 `AgentLoop`。
   - `assert loop.subagents.provider is fake_subagent_provider`
   - `assert loop.subagents.model == "anthropic/claude-haiku-4-5"`
   - `assert loop.subagents.max_tokens == 4096`

**验证命令：**
```bash
.venv/bin/pytest tests/agent/test_subagent_model_override.py -v
```

**完成判据：** 6 个测试全部通过。

### 3.3 冒烟回归

```bash
.venv/bin/pytest tests/agent/ -v --timeout=60
```

**完成判据：** 所有 `tests/agent/` 下测试通过，尤其是 `test_subagent_hook_composition.py`、`test_loop_rewrite_hook_injection.py` 不退化。

---

## Task 4: `_make_single_provider` 可复用性验证

**文件：** `tests/test_make_provider_fallback.py`（修改）

这个文件已经有 `_make_single_provider` 的测试。追加一个 case 验证它能用任意 model string 独立构造，不依赖 `fallback_models` 列表上下文。

测试用例 `test_make_single_provider_for_subagent_model`：
- 构造 `Config`，主 `model="anthropic/claude-opus-4-5"`，不设 `fallback_models`。
- 直接调用 `_make_single_provider(config, "anthropic/claude-haiku-4-5")`。
- `assert` 返回 `AnthropicProvider` 实例，`default_model == "anthropic/claude-haiku-4-5"`。

**验证命令：**
```bash
.venv/bin/pytest tests/test_make_provider_fallback.py -v
```

**完成判据：** 新增测试通过，原有测试全部通过。

---

## Task 5: 示例配置 + README 片段

**文件：** `docs/examples/config-subagent-model.json`（新建）

```json
{
  "agents": {
    "defaults": {
      "model": "anthropic/claude-opus-4-5",
      "subagent_model": "anthropic/claude-haiku-4-5",
      "subagent_max_tokens": 4096,
      "_comment": "主 agent 用 Opus 思考，subagent 用 Haiku 跑日志/grep/读文件等窄任务，响应更快、成本更低。"
    }
  }
}
```

**文件：** 在 `docs/CHANGELOG.md` 或 `README.md`（项目实际维护的 changelog 位置）追加一段：

```markdown
### Subagent 模型分层 (2026-04-20)

新增 `agents.defaults.subagent_model` 配置，允许 subagent 使用独立的 LLM provider：

- `subagent_model`: subagent 使用的模型名（如 `"anthropic/claude-haiku-4-5"`）
- `subagent_reasoning_effort`: 可选 `low`/`medium`/`high`，不配则用 provider 默认
- `subagent_max_tokens`: 可选输出 token 上限，不配则继承主 agent

不配置时行为零变化。适用场景：主 agent 跑强模型做规划/写作，subagent 跑快模型做日志分析/文件读取/pattern matching。
```

**完成判据：** 文件存在，内容准确。

---

## 回归清单

全部任务完成后跑一遍：

```bash
cd /root/git_code/nanobot
.venv/bin/pytest tests/ -x --timeout=60 -q
```

预期：pass 数 = 原有 pass 数 + 新增测试数（约 +9）。无 regression。

## 手动烟测（可选，推荐）

改 `~/workspace/nanobot_config/config.json` 加 `subagent_model: "anthropic/claude-haiku-4-5"`（或你现有配置里的 k2p5），`touch` 触发重启，让主 agent spawn 一个 subagent：

```
> 让 subagent 帮我看看 nanobot 最近 5 分钟的日志
```

观察日志里 `Subagent using independent provider: model=...` 一行，并确认 subagent 返回时带 k2p5 响应特征（或看 journalctl 里的 API 调用目标 endpoint）。

## 边界复核（开工前）

四个你已经拍板的边界，在 plan 里落实的位置：

| 边界 | 决策 | 落实位置 |
|---|---|---|
| reasoning_effort 独立覆盖 | 单独字段，默认 None = provider 默认行为 | Task 1.1 / 2.1 / 2.2 |
| max_tokens 独立覆盖 | 单独字段，默认 None = 继承主 agent | Task 1.1 / 2.1 / 2.2 |
| subagent fallback chain | 不实现 | Non-Goals 明确排除 |
| 运行中 subagent 的热加载 | gateway 整体重启自然承接，无需代码 | Non-Goals 明确排除 |

## 提交策略

建议按 Task 切 commit，每个 Task 的 verify 命令跑过再提交：

1. `feat(config): add subagent_model/reasoning_effort/max_tokens fields`
2. `feat(subagent): accept reasoning_effort and max_tokens overrides`
3. `feat(loop): build independent provider for subagent when configured`
4. `test(provider): verify _make_single_provider works for subagent model`
5. `docs: add subagent model override example and changelog`
