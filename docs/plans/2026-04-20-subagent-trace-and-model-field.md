# Subagent Trace 观测 + TraceHook model 字段补齐

## 概述

两件事合一个 PR，因为单独做任何一件都不完整：

1. **Subagent trace**：为每个 spawn 构造一次性 TraceHook + 独立 llm_logs 文件，路径挂在父 scope 下，不写 sessions 目录。
2. **TraceHook model 字段**：当前 llm_logs 只有 `session_key` 没 `model`——就算 subagent 接上 trace，也分不清它用的是主 agent model 还是 subagent_model。两个补丁配对才能验证 [subagent 模型分层](./2026-04-20-subagent-model-config.md) 真在跑预期模型。

**核心设计决策：**

- **per-spawn 新造 TraceHook**：TraceHook `__slots__` 里存 per-iteration 状态，跨 spawn 共享会串。不复用 `self._trace_hook`。
- **独立日志文件**：`<scope>/llm_logs/subagent_<date>_<task_id>.jsonl`，每 spawn 一文件。并发 subagent 各写各的，无锁。
- **不创建 Session 对象**：`session_key` 只是 TraceHook 里的纯字符串 tag，不进 SessionManager、不 get_or_create、不落 sessions 目录。
- **model 走 AgentHookContext**：model 是 per-run 属性，从 context 传递比在 TraceHook 构造时传更干净——支持运行时 `/model` 切换场景。
- **layout 为 None 时 fallback**：无 channel_name 解析不出 layout（极少见）时降级到 `<workspace>/subagent_logs/`；cli 场景不特殊处理——nanobot workspace 结构把 cli 视为独立 channel 目录，和 discord 统一走 `layout.llm_logs_dir`。

## Non-Goals

- ❌ 写 subagent 专属 sessions 目录 / Session 对象（用户明确排除）
- ❌ 追加到主 agent 同一 jsonl（并发 append 会导致 JSON Lines 交织损坏）
- ❌ 新增 `LightTraceHook` 独立类（现有 TraceHook 本身就够轻，耦合错觉昨天被打脸）
- ❌ 为 cron / dream 等其他无 session 场景加 trace（本次只修 subagent，其他单独评估）
- ❌ 把 subagent trace 写到 session.jsonl 主日志里（TraceHook 只负责 llm_logs，不碰 session history）

## 文件变更地图

```
nanobot/
├── agent/
│   ├── hook.py               # [修改] AgentHookContext 加 model 字段；TraceHook._finalize_entry 写 model
│   ├── runner.py             # [修改] 构造 AgentHookContext 时传 model=spec.model
│   ├── subagent.py           # [修改] _run_subagent 构造 per-spawn TraceHook + log_path
│   ├── loop.py               # [修改] _set_tool_context 透传 layout 给 SpawnTool
│   └── tools/
│       └── spawn.py          # [修改] set_context 接受 layout；execute 透传到 manager.spawn
tests/
├── agent/
│   ├── test_trace_hook_model_field.py       # [新建] 验证 model 写入 log_entry
│   └── test_subagent_trace.py               # [新建] 验证 subagent 独立 log 文件 + 路径 + 并发隔离
docs/
└── plans/2026-04-20-subagent-trace-and-model-field.md  # 本文件
```

## 依赖关系

Task 1 → Task 2（Task 2 依赖 Task 1 加的 context.model 字段）
Task 3 → Task 4（Task 4 依赖 Task 3 确定的 manager.spawn 签名）
Task 2 与 Task 3/4 可并行（model 补丁独立于 subagent trace 装配）

建议顺序：1 → 2 → 3 → 4 → 5（文档）

## Task 1: AgentHookContext + runner 写 model

**文件：** `nanobot/agent/hook.py`、`nanobot/agent/runner.py`

### 1.1 AgentHookContext 加字段

`nanobot/agent/hook.py` 中 `AgentHookContext` dataclass（约 13-26 行），在 `stop_reason` 之后追加：

```python
@dataclass(slots=True)
class AgentHookContext:
    """Mutable per-iteration state exposed to runner hooks."""

    iteration: int
    messages: list[dict[str, Any]]
    response: LLMResponse | None = None
    usage: dict[str, int] = field(default_factory=dict)
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    tool_results: list[Any] = field(default_factory=list)
    tool_events: list[dict[str, str]] = field(default_factory=list)
    final_content: str | None = None
    stop_reason: str | None = None
    error: str | None = None
    model: str | None = None  # <-- 新增；TraceHook 用于写入 llm_logs
```

### 1.2 runner 构造 context 时填 model

`nanobot/agent/runner.py` 第 117 行左右：

```python
context = AgentHookContext(iteration=iteration, messages=messages)
```

改为：

```python
context = AgentHookContext(iteration=iteration, messages=messages, model=spec.model)
```

**注意：** 这是唯一的构造点（grep 确认），不要漏。

### 1.3 测试

**文件：** `tests/agent/test_trace_hook_model_field.py`（新建）

写两个测试：

1. `test_context_exposes_model`：
   - 构造 `AgentHookContext(iteration=0, messages=[], model="test-model")`
   - `assert ctx.model == "test-model"`
   - 同时验证默认值 `AgentHookContext(iteration=0, messages=[]).model is None`

2. `test_runner_populates_model_in_context`：
   - 构造最小 `AgentRunSpec`（model="anthropic/claude-haiku-4-5"）
   - 挂一个 spy hook，在 `before_iteration` 捕获 `context.model`
   - mock provider 立即返回 finished response（无 tool_calls）
   - `assert spy.captured_model == "anthropic/claude-haiku-4-5"`

**验证命令：**
```bash
cd /root/git_code/nanobot
PYTHONPATH=. .venv/bin/pytest tests/agent/test_trace_hook_model_field.py -v
```

**完成判据：** 2 个测试通过。

---

## Task 2: TraceHook 写入 model 字段

**文件：** `nanobot/agent/hook.py`

### 2.1 `_finalize_entry` 补 model

定位 `TraceHook` 中写 `log_entry` 的字典（约 109-133 行）。在 `"session_key"` 和 `"iteration"` 之间插入 `"model"` 键：

```python
log_entry = {
    "timestamp": datetime.utcnow().isoformat(),
    "session_key": self._session_key,
    "model": context.model,   # <-- 新增
    "iteration": context.iteration,
    # ... 其余字段保持 ...
}
```

**关键点：** `context.model` 可能是 `None`（Task 1 默认值），writer 必须容忍 None，不要改 `if context.model` 条件写入——**缺失 vs 显式 None** 都写 `null`，消费方 grep 时语义一致。

### 2.2 测试扩展

**文件：** `tests/agent/test_trace_hook_model_field.py`（追加）

3. `test_trace_hook_writes_model_to_log`：
   - 用 `tmp_path` 创建临时 log 文件
   - 构造 `TraceHook(log_path=tmp_path / "trace.jsonl")`
   - 设 `session_key`
   - 调 `before_iteration` → `after_iteration`，context.model 设为 "anthropic/claude-haiku-4-5"
   - 读 jsonl 文件第一行，`assert json.loads(line)["model"] == "anthropic/claude-haiku-4-5"`

4. `test_trace_hook_handles_none_model`：
   - 同上但 context.model = None
   - 读回 `assert log_entry["model"] is None`
   - **这是 regression 防线**：保证现有未传 model 的调用点（如果还有）不崩。

**验证命令：**
```bash
PYTHONPATH=. .venv/bin/pytest tests/agent/test_trace_hook_model_field.py -v
```

**完成判据：** 4 个测试全部通过。回归验证：已有 llm_logs 消费方（如果有）不破坏——主 agent 跑一条消息，检查 jsonl 新字段出现且无 parse 错误。

### 2.3 回归冒烟

跑一遍现有 trace 相关测试（如果有）：
```bash
PYTHONPATH=. .venv/bin/pytest tests/agent/ -k "trace or hook" -q
```

---

## Task 3: SubagentManager 构造 per-spawn TraceHook

**文件：** `nanobot/agent/subagent.py`

### 3.1 `spawn()` 方法签名扩展

当前签名（约 86-93 行）：

```python
async def spawn(
    self,
    task: str,
    label: str | None = None,
    origin_channel: str = "cli",
    origin_chat_id: str = "direct",
    session_key: str | None = None,
) -> str:
```

追加参数：

```python
async def spawn(
    self,
    task: str,
    label: str | None = None,
    origin_channel: str = "cli",
    origin_chat_id: str = "direct",
    session_key: str | None = None,
    log_dir: Path | None = None,  # <-- 新增
) -> str:
```

透传到 `_run_subagent`：

```python
bg_task = asyncio.create_task(
    self._run_subagent(task_id, task, display_label, origin, log_dir)
)
```

### 3.2 `_run_subagent` 签名 + 构造 TraceHook

当前签名：

```python
async def _run_subagent(
    self, task_id: str, task: str, label: str, origin: dict[str, str],
) -> None:
```

改为：

```python
async def _run_subagent(
    self, task_id: str, task: str, label: str, origin: dict[str, str],
    log_dir: Path | None = None,
) -> None:
```

### 3.3 构造 log_path + per-spawn TraceHook

在 `_run_subagent` 内部 `try:` 块顶部（ToolRegistry 构造之前）：

```python
from datetime import date
from nanobot.agent.hook import TraceHook

# 决定 log 路径
if log_dir is None:
    # layout 为 None 时的 fallback（极少见）；cli 场景 layout 不为 None
    resolved_log_dir = self.workspace / "subagent_logs"
else:
    resolved_log_dir = log_dir
resolved_log_dir.mkdir(parents=True, exist_ok=True)

today = date.today().isoformat()
log_path = resolved_log_dir / f"subagent_{today}_{task_id}.jsonl"

# per-spawn TraceHook（不复用 self._trace_hook）
subagent_trace = TraceHook(log_path=log_path)
subagent_trace.session_key = (
    f"subagent:{task_id}:{origin['channel']}:{origin['chat_id']}"
)
```

### 3.4 把 subagent_trace 挂进 hook 链

当前 `_compose_hook(task_id)` 调用（约 158 行）：

```python
hook=self._compose_hook(task_id),
```

**不修改 `_compose_hook` 本身**——它是 `SubagentManager` 的稳定接口。改为**局部组合**：

```python
from nanobot.agent.hook import CompositeHook

base_hook = self._compose_hook(task_id)
# subagent_trace 放最后：rewrite → logging → trace
# 确保 trace 记录的是最终 messages/rewritten args
composed_hook = CompositeHook([base_hook, subagent_trace])

result = await self.runner.run(AgentRunSpec(
    ...
    hook=composed_hook,
    ...
))
```

**顺序重要**：rewrite hook 先跑（改 args），logging hook 再跑（debug 打印），trace hook 最后跑（记录最终态）。这与主 agent loop 的 hook 顺序一致。

### 3.5 测试

**文件：** `tests/agent/test_subagent_trace.py`（新建）

1. `test_subagent_creates_log_file`：
   - mock provider 立即返回 finished
   - mock runner.run → 直接调用 hook 的 before_iteration/after_iteration（或真跑一轮）
   - 验证 `<log_dir>/subagent_<date>_<task_id>.jsonl` 文件存在
   - 验证文件第一行 `json.loads(...)["session_key"]` 以 `"subagent:"` 开头

2. `test_subagent_log_has_model_field`：
   - spec.model = "anthropic/claude-haiku-4-5"
   - 跑一轮，读回 log_entry
   - `assert entry["model"] == "anthropic/claude-haiku-4-5"`
   - **端到端验证 Task 1+2+3 联通**

3. `test_subagent_fallback_log_dir_when_layout_missing`：
   - log_dir=None 调 spawn
   - 验证文件落在 `<workspace>/subagent_logs/`（layout=None 的 fallback）

4. `test_concurrent_subagents_use_separate_files`：
   - 并发 spawn 两个，各自 task_id 不同
   - 验证两个独立 jsonl 文件，内容不混

**验证命令：**
```bash
PYTHONPATH=. .venv/bin/pytest tests/agent/test_subagent_trace.py -v
```

**完成判据：** 4 个测试通过。现有 `test_subagent_hook_composition.py`、`test_subagent_model_override.py` 不破坏。

---

## Task 4: SpawnTool + AgentLoop 透传 layout

**文件：** `nanobot/agent/tools/spawn.py`、`nanobot/agent/loop.py`

### 4.1 SpawnTool 接收 log_dir

`nanobot/agent/tools/spawn.py` 当前 `__init__`：

```python
def __init__(self, manager: "SubagentManager"):
    self._manager = manager
    self._origin_channel = "cli"
    self._origin_chat_id = "direct"
    self._session_key = "cli:direct"
```

追加：

```python
    self._log_dir: Path | None = None
```

并在 `set_context` 中扩展：

```python
def set_context(
    self, channel: str, chat_id: str,
    log_dir: Path | None = None,   # <-- 新增
) -> None:
    self._origin_channel = channel
    self._origin_chat_id = chat_id
    self._session_key = f"{channel}:{chat_id}"
    self._log_dir = log_dir
```

`execute` 透传：

```python
async def execute(self, task: str, label: str | None = None, **kwargs: Any) -> str:
    return await self._manager.spawn(
        task=task,
        label=label,
        origin_channel=self._origin_channel,
        origin_chat_id=self._origin_chat_id,
        session_key=self._session_key,
        log_dir=self._log_dir,   # <-- 新增
    )
```

### 4.2 AgentLoop `_set_tool_context` 传 layout

当前（约 407-418 行）：

```python
def _set_tool_context(
    self, channel: str, chat_id: str, message_id: str | None = None,
    session_metadata: dict | None = None,
) -> None:
    for name in ("message", "spawn", "cron"):
        if tool := self.tools.get(name):
            if hasattr(tool, "set_context"):
                if name == "message":
                    tool.set_context(channel, chat_id, message_id, session_metadata=session_metadata)
                else:
                    tool.set_context(channel, chat_id)
```

签名加 `layout`：

```python
def _set_tool_context(
    self, channel: str, chat_id: str, message_id: str | None = None,
    session_metadata: dict | None = None,
    layout: "WorkspaceLayout | None" = None,   # <-- 新增
) -> None:
    subagent_log_dir = layout.llm_logs_dir if layout else None
    for name in ("message", "spawn", "cron"):
        if tool := self.tools.get(name):
            if hasattr(tool, "set_context"):
                if name == "message":
                    tool.set_context(channel, chat_id, message_id, session_metadata=session_metadata)
                elif name == "spawn":
                    tool.set_context(channel, chat_id, log_dir=subagent_log_dir)
                else:
                    tool.set_context(channel, chat_id)
```

### 4.3 调用点更新

`_set_tool_context` 的两个调用点（659、713）都在 `_process_message` 里，此时 layout 已经被 `_resolve_layout(ctx)` 解析好了。

**第 659 行**：
```python
self._set_tool_context(ctx.channel, ctx.chat_id, ctx.message_id)
```
改为：
```python
self._set_tool_context(ctx.channel, ctx.chat_id, ctx.message_id, layout=layout)
```

**第 713 行**（在 `build_messages` 之前）：类似处理。需先 grep 确认这个调用点的 layout 变量是否在作用域里——如果不在，把 layout 构造提前或传下来。

### 4.4 测试

**文件：** `tests/agent/test_subagent_trace.py`（追加）

5. `test_spawn_tool_propagates_log_dir`：
   - 构造 SpawnTool，调用 `set_context("discord", "12345", log_dir=Path("/tmp/foo"))`
   - mock manager.spawn，capture 调用参数
   - `assert kwargs["log_dir"] == Path("/tmp/foo")`

6. `test_agentloop_sets_spawn_log_dir_from_layout`：
   - 构造 AgentLoop + 假 layout（用 WorkspaceLayout 构造器）
   - 调用 `_set_tool_context(..., layout=layout)`
   - 从 `loop.tools.get("spawn")._log_dir` 读回，`assert _log_dir == layout.llm_logs_dir`

**验证命令：**
```bash
PYTHONPATH=. .venv/bin/pytest tests/agent/test_subagent_trace.py -v
```

**完成判据：** 6 个测试全部通过。

### 4.5 集成回归

跑 loop 相关现有测试，确认 set_tool_context 签名扩展不破坏：
```bash
PYTHONPATH=. .venv/bin/pytest tests/agent/test_loop_rewrite_hook_injection.py tests/agent/test_subagent_hook_composition.py tests/agent/test_subagent_model_override.py -q
```

---

## Task 5: CHANGELOG + 示例

**文件：** `CHANGELOG.md`

在 `[Unreleased] Added` 追加：

```markdown
- TraceHook 日志新增 `model` 字段，每条 entry 记录发起 LLM 调用的模型名。便于验证多模型场景（如 subagent 模型分层）实际生效。
- Subagent 执行现在会生成独立的 llm_logs 文件：`<scope>/llm_logs/subagent_<date>_<task_id>.jsonl`。
  - 每次 spawn 新建 TraceHook 实例，不共享状态，并发安全。
  - 不创建 subagent 专属 session 或 sessions 目录——纯日志观测。
  - layout=None 时落到 `<workspace>/subagent_logs/`；cli 场景 layout 不为 None，统一走 `layout.llm_logs_dir`。
```

**文件：** `docs/plans/2026-04-20-subagent-trace-and-model-field.md`（本文件已存在，不动）

---

## 全局验证

### 手动烟测

```bash
# 确认 subagentModel 配置仍是 haiku（或你当前测试模型）
cat ~/workspace/nanobot_config/config.json | grep -i subagent

# 触发 nanobot 重启
touch ~/workspace/nanobot_config/config.json

# 在 Discord 里让 Evie spawn：
# > 让 subagent 读一下 /root/workspace/memory/MEMORY.md 前 20 行

# 看新生成的 subagent jsonl
LATEST=$(ls -t /root/workspace/discord/develop_*/llm_logs/subagent_*.jsonl | head -1)
echo "Latest trace: $LATEST"

# 验证 model 字段是否是 haiku
head -1 "$LATEST" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print('model:', d.get('model')); print('session_key:', d.get('session_key'))"

# 预期输出：
#   model: anthropic/claude-haiku-4-5
#   session_key: subagent:xxxxxx:discord:<chat_id>
```

如果 model 字段是 `anthropic/claude-haiku-4-5`——**Task 2+3 联动端到端通，[subagent 模型分层] feature 真正落地**。如果是主 agent model——说明 `_make_single_provider` 那条路径实际没生效，返回去查。

### 自动测试全绿判据

```bash
PYTHONPATH=. .venv/bin/pytest tests/agent/ tests/config/ 2>&1 | tail -3
```

预期：`pass` 数 = baseline(360) + 新增测试数(约 10) = 约 370，failed 数不变（main 原有 28 个不相关失败）。

## 提交策略

```
1. feat(hook): expose model field on AgentHookContext + runner fills it
2. feat(hook): trace hook writes model field to llm_logs
3. feat(subagent): per-spawn TraceHook + independent llm_logs file
4. feat(spawn): propagate layout.llm_logs_dir through tool to manager
5. docs: changelog for subagent trace and model field
```

五个原子 commit，每个带测试，不 squash。

## 边界复核

| 要点 | 处理 |
|---|---|
| 不写 sessions 目录 | 只 mkdir `log_path.parent`，不调 `layout.ensure_dirs()` |
| 不创建 Session 对象 | session_key 纯字符串给 TraceHook 用，不走 SessionManager |
| 并发安全 | per-spawn 新 TraceHook 实例 + 每 spawn 独立文件名（含 task_id） |
| layout=None 兼容 | layout=None 时 fallback 到 `<workspace>/subagent_logs/`；cli 场景 layout≠None |
| model 不可得时 | context.model=None，log_entry["model"]=null，消费方容忍 |
| 不破坏主 agent trace | 主 loop 的 `self._trace_hook` 完全没动 |

## 预期 diff 规模

- 生产代码：~40 行（hook 5 + runner 1 + subagent 20 + loop 5 + spawn 7）
- 测试代码：~150 行（10 个测试）
- 文档：CHANGELOG +6 行

总计约 190 行变更。比 subagent-model-config 那波（48 行生产 + 200 测试）大一圈，但大头在 subagent.py 里组装 TraceHook 和路径。
