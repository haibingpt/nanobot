# Subagent Runtime Parity — 把 subagent 的运行时配置对齐主 agent

## 概述

Subagent 现在跑得比主 agent 笨、比主 agent 慢、比主 agent 容易挂，不是因为模型差，而是因为 `SubagentManager._run_subagent` 在构造 `AgentRunSpec` 时**漏了三个关键参数**，外加**没有 wall-clock 超时保护**。主 loop 上这四项齐全，subagent 全缺。

本 plan 做三件事：

1. `concurrent_tools=True` 补上 —— 让 subagent 同一轮内的多个 tool_call 并发执行。
2. `pruner` 和 `context_window_tokens` 补上 —— 让 subagent 和主 loop 共享同一套 context pruning。
3. `_run_subagent` 外层套 `asyncio.wait_for`，加默认 15 分钟 wall-clock 硬超时，解僵死。

**`max_iterations` 复用主 agent 的值，不加 per-spawn override**：当前代码已是 `max_iterations=self.max_iterations`，从 `AgentLoop` 透传主 agent 预算（默认 40），无需改动。抬高单轮预算治标，补齐 pruner + concurrent_tools + timeout 才是治本。

**设计原则：**
- **Parity first**：subagent 和主 agent 走同一条 runner，配置能对齐就对齐。不对齐的项必须在代码里加注释说明理由。
- **最小接口改动**：`SubagentManager.__init__` 多接两个依赖（pruner、context_window_tokens），`spawn()` 多加两个可选参数（max_iterations、timeout）。不引入新类、不动 runner。
- **超时不擦屁股**：超时后 subagent 被 `cancel()`，返回 `stop_reason="timeout"`，不假装任务"完成"。

## 背景：2026-04-21 晚 khb-orchestrate pipeline 故障复盘

一条 `ljg-fetch | ljg-xray-paper && { card & rank & the-one | plain & wait } && roundtable` 管道，7 个 spawn 中 **4 个故障**：

| subagent | 观测到的 final_content | 实际 stop_reason（最可能） | 诱因 |
|---|---|---|---|
| `d19fb126` (xray 第 1 次) | 空（`"Task completed but no final response..."`) | `max_iterations` 或 `empty_final_response` | 日志采样不全，无法精确区分 |
| `dff6332b` (xray takeover) | 空（同上） | 同上 | 同上，另叠加 `/tmp/` 只读导致重试放大 |
| `ed98c769` (rank) | `"I completed the tool steps but couldn't produce a final answer."` | `empty_final_response` | 第二轮 LLM 返回 blank text |
| `bcf8edeb` (roundtable) | 无响应 | **无终止**（wall-clock 僵死） | `19:52:18` 之后 9 分钟零日志，直到外部消息唤醒主 agent |

**共同诱因都指向 subagent 配置对比主 loop 的残缺**。三个能跑完但产物空的故障各有直接触发，但背后是同一个放大器：context 线性膨胀 + 串行 tool 执行 + 无 wall-clock 兜底。

## Non-Goals（本版本不做）

- ❌ **改 runner.py 的 iteration 语义**：不动核心 loop，只动 subagent 构造参数。
- ❌ **per-subagent 独立 context budget**：等 `subagent_model` 独立 provider（已在 2026-04-20 plan 完成）稳定后再考虑。
- ❌ **把 wall-clock 超时推到 runner 内部**：超时是"外部强制 cancel 整个 task"的语义，属于 SubagentManager 的生命周期管理职责，runner 不应关心。
- ❌ **改 khb-orchestrate SKILL.md 的 5 分钟接管条款**：SKILL.md 那句是产品级约定（复用历史产物、换节点 id），和这里的"运行时强制超时"是两层——前者是主 agent 做的编排决策，后者是调度器保命。都要，都不冲突。
- ❌ **新增重试机制**：subagent 失败后的重试交给调用方（主 agent / orchestrate skill）决定，runner 层面不做。

## 文件变更地图

```
nanobot/
└── agent/
    ├── subagent.py           # [修改] __init__ 接受 pruner/context_window_tokens/default_timeout_seconds；
    │                         #        spawn() 接受 timeout_seconds 可选参数；
    │                         #        _run_subagent 构造 AgentRunSpec 补 concurrent_tools/pruner/context_window_tokens；
    │                         #        _run_subagent 外层套 asyncio.wait_for
    └── loop.py               # [修改] 构造 SubagentManager 时传 self._pruner 和 self.context_window_tokens
config/
└── schema.py                 # [修改] AgentDefaults 新增 subagent_timeout_seconds 字段（默认 900）
tests/
└── agent/
    ├── test_subagent_parity.py    # [新建] 验证 SubagentManager 把 pruner/concurrent_tools/context_window_tokens/max_iterations 透传到 AgentRunSpec
    └── test_subagent_timeout.py   # [新建] 验证超时后 task 被 cancel，_announce_result kind=timeout，清理 _running_tasks
docs/
└── plans/
    └── 2026-04-21-subagent-runtime-parity.md   # [本文档]
```

## Task 1: SubagentManager.__init__ 持有 pruner 和 context_window_tokens

**文件：** `nanobot/agent/subagent.py`

### 1.1 签名扩展

在现有 `__init__` 参数列表中追加（保持向后兼容，全部带默认值）：

```python
def __init__(
    self,
    provider: LLMProvider,
    bus: MessageBus,
    # ... 现有参数 ...
    max_iterations: int = 200,
    # --- 新增参数 ---
    pruner: "ContextPruner | None" = None,
    context_window_tokens: int | None = None,
    default_timeout_seconds: float = 900.0,
) -> None:
```

对应 `self._pruner = pruner`、`self._context_window_tokens = context_window_tokens`、`self._default_timeout_seconds = default_timeout_seconds`。

**向后兼容**：三个新参数全 `None` / 默认值时，行为与当前版本等价（pruner 不跑、context_window 走 runner 默认 128k、超时 15 分钟兜底）。

### 1.2 import ContextPruner

```python
from nanobot.agent.pruner import ContextPruner
```

放在文件顶部现有 import 块内。

## Task 2: spawn() 暴露 per-spawn timeout override

**文件：** `nanobot/agent/subagent.py`

### 2.1 签名扩展

```python
async def spawn(
    self,
    task: str,
    label: str | None = None,
    origin_channel: str = "cli",
    origin_chat_id: str = "direct",
    session_key: str | None = None,
    log_dir: Path | None = None,
    # --- 新增参数 ---
    timeout_seconds: float | None = None,    # None = 走 self._default_timeout_seconds
) -> str:
```

**`max_iterations` 不在 spawn 参数里**——复用 `self.max_iterations`（即主 agent 的值），不允许 per-spawn 覆盖。理由：决定 subagent 硬顶预算是系统级配置，不是每次调用的战术参数；真正的瓶颈是每轮的"有效信息密度"（靠 pruner + concurrent_tools 治）。

spawn 内部把 `timeout_seconds` 通过闭包绑进 `_run_subagent` 调用。

### 2.2 不改 SubagentTool schema

`nanobot/agent/tools/subagent.py` 里 LLM 可见的 `spawn` 工具**本版本不暴露** `timeout_seconds`——主 agent 不决定 subagent 墙钟预算，编排器（`khb-orchestrate` / Python caller）决定。

下个版本如果 `khb-orchestrate` 需要让 LLM 对重 skill 显式加时间预算（例如 roundtable 默认 30 分钟），再开放。

## Task 3: _run_subagent 构造 AgentRunSpec 补三参数

**文件：** `nanobot/agent/subagent.py`

### 3.1 当前代码（subagent.py:179-192）

```python
result = await self.runner.run(AgentRunSpec(
    initial_messages=messages,
    tools=tools,
    model=self.model,
    max_iterations=self.max_iterations,
    max_tool_result_chars=self.max_tool_result_chars,
    hook=composed_hook,
    max_iterations_message="Task completed but no final response was generated.",
    error_message=None,
    fail_on_tool_error=True,
    reasoning_effort=self.reasoning_effort,
    max_tokens=self.max_tokens,
))
```

### 3.2 改造后

```python
result = await self.runner.run(AgentRunSpec(
    initial_messages=messages,
    tools=tools,
    model=self.model,
    max_iterations=self.max_iterations,  # 直接复用主 agent 透传下来的值，不做 per-spawn override
    max_tool_result_chars=self.max_tool_result_chars,
    hook=composed_hook,
    max_iterations_message="Task completed but no final response was generated.",
    error_message=None,
    fail_on_tool_error=True,
    reasoning_effort=self.reasoning_effort,
    max_tokens=self.max_tokens,
    # --- 新增：与主 loop 对齐 ---
    concurrent_tools=True,
    pruner=self._pruner,
    context_window_tokens=self._context_window_tokens,
    session_key=session_key,  # 如果原本没传，顺手补上，pruner 需要
))
```

**注意事项：**
- `concurrent_tools=True` 是安全的：runner 内部已经按 batch 分组，同一 batch 内并发只对无副作用 tool（read_file / grep / web_fetch）生效，write_file / exec 这种由 runner 自动分到单独 batch。
- `pruner=self._pruner` 允许为 `None`（pruning 关闭时），runner 已处理。
- `context_window_tokens=None` → runner 走默认 128k，不引入新行为。

## Task 4: _run_subagent 外层加 asyncio.wait_for

**文件：** `nanobot/agent/subagent.py`

### 4.1 包装逻辑

将 `_run_subagent` 的主体用 `asyncio.wait_for` 包起来：

```python
async def _run_subagent(self, task_id: str, ...) -> None:
    timeout = timeout_override or self._default_timeout_seconds
    try:
        await asyncio.wait_for(
            self._run_subagent_inner(task_id, ...),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Subagent [{}] exceeded wall-clock timeout {:.0f}s, cancelled",
            task_id, timeout,
        )
        await self._announce_result(
            task_id, label, task,
            f"Subagent timed out after {timeout:.0f}s (no product delivered).",
            origin,
            "timeout",
        )
    finally:
        self._cleanup_task(task_id, session_key)
```

把现在 `_run_subagent` 里的全部执行逻辑抽到 `_run_subagent_inner`。

### 4.2 _announce_result 增加 "timeout" 语义

现在 `_announce_result` 的 `kind` 参数接受 `"success"`、`"error"`。新增 `"timeout"` 分支，消息格式化为明确的超时告警，而非伪装成完成。

### 4.3 Cancel 清理

`asyncio.wait_for` 超时会 `cancel()` 内部 coroutine，Python 侧本就会抛 `CancelledError` 到 `_run_subagent_inner` 里。确保 `_run_subagent_inner` 的 `try/except` 不会吞掉 `CancelledError`。现有代码里的 `except Exception` 需要改成 `except Exception:` + 单独 `except asyncio.CancelledError: raise`。

### 4.4 正在运行的 tool 怎么办

`asyncio.wait_for` 超时触发时，正在执行的 `_run_tool` 也会被 cancel。对于 `exec`（shell）tool，需要确保 subprocess 被 kill——当前 `ExecTool.run` 已有 `timeout` 参数会自动 kill subprocess。LLM 请求也会被 cancel（httpx 的 request context manager 保证）。

**保守做法**：本 plan 先信任现有 tool 层的 cancel 语义，不动 tool。如果线上观察到 orphan subprocess 再追加。

## Task 5: loop.py 注入 pruner 和 context_window_tokens

**文件：** `nanobot/agent/loop.py`

### 5.1 当前代码（loop.py:287-299）

```python
self.subagents = SubagentManager(
    provider=subagent_provider,
    bus=bus,
    # ...
    max_iterations=self.max_iterations,
)
```

### 5.2 改造后

```python
self.subagents = SubagentManager(
    provider=subagent_provider,
    bus=bus,
    # ... 现有参数 ...
    max_iterations=self.max_iterations,
    pruner=self._pruner,
    context_window_tokens=self.context_window_tokens,
    default_timeout_seconds=agent_defaults.subagent_timeout_seconds,
)
```

**注意：** `self._pruner` 和 `self.context_window_tokens` 在 AgentLoop.__init__ 里已经定义（loop.py:225 附近），直接用。

## Task 6: schema.py 新增 subagent_timeout_seconds

**文件：** `nanobot/config/schema.py`

在 `AgentDefaults` 里 `subagent_max_tokens` 之后追加：

```python
subagent_timeout_seconds: float = 900.0  # 15 分钟；0 或负值 = 不限制（不推荐）
```

配置示例（`~/workspace/nanobot_config/config.json`）：

```json
{
  "agents": {
    "defaults": {
      "subagent_timeout_seconds": 900.0
    }
  }
}
```

## Task 7: 测试

### 7.1 `tests/agent/test_subagent_parity.py`（新建）

验证 `SubagentManager._run_subagent` 构造的 `AgentRunSpec` 字段：

- `concurrent_tools is True`
- `pruner is self._pruner`（包括 `None` 情况）
- `context_window_tokens == self._context_window_tokens`
- `max_iterations == self.max_iterations`（构造 SubagentManager 时传多少就是多少，无 per-spawn override）

用法：mock `AgentRunner.run` 捕获 spec 参数，断言字段。

### 7.2 `tests/agent/test_subagent_timeout.py`（新建）

- **用例 1**：subagent 任务 sleep(2s)，`timeout_seconds=0.5` → 预期 `asyncio.TimeoutError` 被 SubagentManager 捕获，`_announce_result` 被调用 kind=`"timeout"`，task 从 `_running_tasks` 清理。
- **用例 2**：subagent 任务正常 500ms 完成，`timeout_seconds=2.0` → 预期 task 正常完成，不触发 timeout 分支。
- **用例 3**：`timeout_seconds=None` 且 `default_timeout_seconds=0.5`，任务 sleep(2s) → 预期超时（验证 default 路径）。

### 7.3 现有 SubagentManager 测试回归

跑 `pytest tests/agent/test_subagent*.py -v`，预期现有测试 pass（新增参数全有默认值）。

## 验证方式

### 本地验证

```bash
cd /root/git_code/nanobot
pip install -e .
pytest tests/agent/test_subagent_parity.py tests/agent/test_subagent_timeout.py -v
pytest tests/agent/ -v  # 现有 subagent 测试回归
```

### 线上验证

1. `touch ~/workspace/nanobot_config/config.json` 重启 gateway。
2. 重跑今天挂掉的 pipeline：
   ```
   ljg-fetch https://arxiv.org/abs/2604.02176 | ljg-xray-paper && { ljg-card & ljg-rank & ljg-the-one | ljg-plain & wait } && ljg-roundtable
   ```
3. 观察 `journalctl --user -u nanobot-gateway -f` 里：
   - 是否出现并发 tool 执行（同一 iteration 多个 `_run_tool` 的 log 时间接近）。
   - 是否出现 `pruner.prune` 相关日志。
   - 是否有 subagent 在 15 分钟内没产出就被 cancel（超时场景的预期路径）。
4. **成功标准**：7 个 subagent 中故障数 ≤ 1（允许单次 LLM 偶发 empty response，但不允许僵死或批量 max_iterations 耗尽）。

## 回滚策略

所有改动在 `subagent.py`、`loop.py`、`schema.py`，纯加法：

- 新 `__init__` 参数有默认值 → 旧 config / 旧 caller 无感。
- `AgentRunSpec` 新字段 → runner 已有默认，不传即老行为。
- `wait_for` 默认 15 分钟超时 → 比历史上"无限等"保守，但极少 subagent 任务真需要 >15 分钟。如出现误杀，单次 spawn 可传 `timeout_seconds=1800`。

回滚：`git revert` 单个 commit 即可。

## 执行顺序

1. Task 6（schema）→ Task 1（__init__）→ Task 5（loop.py 注入）—— 把依赖通路搭好，但不改 runtime 行为（新参数没人用）。
2. Task 3（AgentRunSpec 补参数）—— 一次提交，立刻生效，跑现有测试验证不破坏。
3. Task 4（wait_for 包装）+ Task 2（spawn 扩展）—— 一起提交，因为二者共用 `timeout_override` 参数通路。
4. Task 7（测试）—— 每步 task 完成后就写对应测试，不攒到最后。

每一步独立可验证、可 revert。

## 关键风险

- **风险 1：并发 tool 执行在某个批次里引发竞争**（比如两个 write_file 写同一路径）。
  - **缓解**：runner 的 `_partition_tool_batches` 当前已将有副作用的 tool 分到单独 batch。审查该函数的分批规则，确认 `write_file`、`edit_file`、`exec` 都在各自独立 batch。如果发现漏洞，单独修复。
- **风险 2：pruner 对 subagent 的 prompt 策略不匹配**。
  - **缓解**：pruner 只裁剪"工具结果"这类历史 context，保留 system + user + 最近 assistant。subagent 的系统消息和最初任务描述不会被裁。如线上发现 subagent 丢失关键上下文，加日志看 pruner 到底裁了啥。
- **风险 3：15 分钟超时对某些特别重的 skill 仍不够**。
  - **缓解**：per-spawn `timeout_seconds` 允许编排器覆盖。`khb-orchestrate` 后续版本里，对 `ljg-roundtable` / `ljg-xray-paper` 这类重 skill 默认传 `timeout_seconds=1800`。

## 附：`max_iterations` 的处理决策

2026-04-21 复盘时一度怀疑 `max_iterations=15` 硬编码导致 xray subagent 耗尽。实际 HEAD 的代码是 `max_iterations=self.max_iterations`，值由 `AgentLoop.__init__` 透传（默认 40），那个 15 是老分支或某次 WIP 残留，不在当前实现里。

即便如此，**本 plan 也不抬高也不暴露 `max_iterations`**，原因：

- 40 轮对绝大多数 skill 够用；真挂掉的那几个 subagent（`dff6332b`、`ed98c769`、`bcf8edeb`）根本原因不是轮数不够，是"每轮有效信息密度"随 context 膨胀跌到地板。
- 单纯加轮数 = 让 subagent 有更多机会在冗余 context 里打转，烧更多 token 办同一件事，放大问题不是解决问题。
- 保留 per-spawn override 会诱惑 LLM 动辄给 100+ 轮——这是最快把费用烧穿的方式。

真正的治本三件套 = `pruner`（每轮压干冗余）+ `concurrent_tools`（每轮墙钟时间压缩）+ `wait_for`（防止少数 LLM 请求卡死拖垮调度器）。这三件落地后，如果线上观察到 `max_iterations` 真的成了瓶颈，再单独开一个 plan 讨论分级预算（轻 skill 20 轮、重 skill 60 轮），由配置而非 caller 决定。
