# Plan — Migrate rtk rewrite from ExecTool to AgentHook

Date: 2026-04-08
Owner: haibin
Status: Ready for execution

---

## 1. 背景与动机

### 1.1 现状
`rtk rewrite` 是命令级 token 压缩器（节省 60–90%）。当前实现把 rewrite 逻辑内嵌在 `ExecTool`：

- `nanobot/config/schema.py` — `ExecToolConfig.rtk_enabled / rtk_verbose`
- `nanobot/agent/tools/shell.py` — `ExecTool.__init__(rtk_enabled, rtk_verbose)`、`_rtk_rewrite()`、`execute()` 入口处调用
- `nanobot/agent/loop.py:277-278` — 主 loop 构造 ExecTool 时透传
- `tests/tools/test_rtk_rewrite.py` — 6 个测试绑定在 ExecTool 上

### 1.2 架构问题
1. **职责错位**：命令改写是横切关注点（未来还会有 SQL 脱敏、路径归一化、敏感词拦截），不该长在单个工具的肚子里。
2. **覆盖死角**：`SubagentManager._run_subagent`（`nanobot/agent/subagent.py:122-129`）构造 ExecTool 时**没传 rtk 参数**，subagent 路径上 rtk 一直是关着的。这是一个隐藏 bug，当前方案结构决定了它会被遗忘。
3. **复用性为零**：新增一个改写器要再改一次 ExecTool 构造器、再加一组 config 字段。

### 1.3 迁移目标
利用 v0.1.5 新增的 `AgentHook.before_execute_tools` 钩子，把 rtk 从工具内部提升为 runner 级横切关注点：
- 一次接入，主 loop + subagent 同时生效
- 零工具耦合：`ExecTool` 完全不知道 rtk 存在
- 为后续同类改写器提供现成模板

---

## 2. 可行性验证（已完成）

| 验证点 | 结论 |
|---|---|
| hook 时机 | `runner.py:152` 在 `_execute_tools` 之前调用 `before_execute_tools` ✅ |
| 参数可变性 | `ToolCallRequest` 是普通 dataclass，`arguments: dict` 可原地修改；`context.tool_calls` 是浅拷贝列表，元素引用共享 → 改了就生效 ✅ |
| 错误隔离 | `CompositeHook._for_each_hook_safe` 已包 try/except，hook 抛异常不崩 runner ✅ |
| 主 loop 接入点 | `AgentLoop._extra_hooks` 现成通道（loop.py:210, 399-409） ✅ |
| subagent 接入点 | `SubagentManager._run_subagent` 当前传 `_SubagentHook(task_id)` 单 hook，需要改成 CompositeHook 组合 ⚠️ 需要小改 |

---

## 3. 设计决策

### 3.1 Hook 放哪
新建 `nanobot/agent/hooks/rewrite.py`——专门放"改写类" hook 的目录，为后续扩展铺路。
目录结构：
```
nanobot/agent/hooks/
├── __init__.py
├── CLAUDE.md          # 架构说明（新增）
└── rewrite.py         # 命令/参数改写 hook
```

### 3.2 Hook 设计
```python
class CommandRewriteHook(AgentHook):
    """
    横切改写工具调用参数。当前实现：rtk 命令压缩。
    未来可扩展：SQL 脱敏、路径归一化、敏感词拦截。
    """
    def __init__(
        self,
        *,
        enabled: bool = False,
        verbose: bool = False,
        timeout: float = 5.0,
        path_append: str = "",
    ) -> None: ...

    async def before_execute_tools(self, context: AgentHookContext) -> None: ...
```

**命名刀口**：不叫 `RtkHook`——那是实现细节绑定命名。叫 `CommandRewriteHook`，rtk 是它的内部策略。如果未来 rtk 被替换或多策略并存，接口不需要改。

**职责边界**：
- 只改写 `name == "exec"` 的 tool_call 的 `arguments["command"]`
- 其他工具完全忽略
- `rtk` 进程不可用时 fail-safe 返回原命令（不抛异常、不打 error 日志）

### 3.3 配置放哪
从 `ExecToolConfig` 迁出，新建独立配置块：

```python
# nanobot/config/schema.py
class CommandRewriteConfig(Base):
    """Command-level rewrite hook (e.g. rtk token compression)."""
    enabled: bool = False
    verbose: bool = False
    timeout: float = 5.0

class AgentConfig(Base):  # 或放到合适的父节点
    ...
    command_rewrite: CommandRewriteConfig = Field(default_factory=CommandRewriteConfig)
```

**兼容性**：根据 `<design_freedom>`，不考虑向后兼容。`ExecToolConfig.rtk_enabled/rtk_verbose` 直接删除。旧 config.json 带 `rtkEnabled: true` 会被 pydantic 报未知字段错误——在 CHANGELOG 中明确标注迁移路径。

### 3.4 注入点
**主 loop**（`nanobot/agent/loop.py`）：
- 构造 `AgentLoop` 时读取 `command_rewrite` 配置
- 若 `enabled`，创建 `CommandRewriteHook` 实例并 append 到 `self._extra_hooks`
- 现有 `_LoopHookChain(loop_hook, self._extra_hooks)` 自动包含它

**subagent**（`nanobot/agent/subagent.py`）：
- `SubagentManager.__init__` 新增参数 `extra_hooks: list[AgentHook] | None = None`
- `_run_subagent` 中把 `_SubagentHook(task_id)` 和 `extra_hooks` 组合成 `CompositeHook`
- 在 `AgentLoop._register_default_tools` 或更上层传入同一个 `CommandRewriteHook` 实例

**单例注意**：rtk hook 是无状态的（`_rewrite` 每次 spawn 新进程），可以安全共享实例。

### 3.5 废弃清理清单
| 文件 | 动作 |
|---|---|
| `nanobot/agent/tools/shell.py` | 删除 `rtk_enabled/rtk_verbose/_rtk_rewrite`、`execute()` 入口的 rtk 分支、`import os`（若 rtk 是唯一用途） |
| `nanobot/config/schema.py` | 删除 `ExecToolConfig.rtk_enabled/rtk_verbose`，新增 `CommandRewriteConfig` |
| `nanobot/agent/loop.py:271-279` | 删除 rtk 透传 |
| `tests/tools/test_rtk_rewrite.py` | 全文件删除 |
| `tests/agent/hooks/test_command_rewrite.py` | 新增 |

---

## 4. 品味自检

- **特殊情况消除**：旧方案在 exec 工具里分叉 `if rtk_enabled: command = rewrite(command)`。新方案无分叉——hook 要么被注册（走全流程）要么不被注册（完全不存在）。
- **缩进深度**：`before_execute_tools` 单层循环 + 单层 if 过滤，无嵌套。
- **函数规模**：`before_execute_tools` 预计 10 行、`_rewrite` 预计 15 行，均在 20 行红线内。
- **抽象必要性**：`CommandRewriteHook` 是未来同类需求的直接模板，不是为假想敌设计的过度抽象。
- **数据泥团**：`enabled/verbose/timeout` 三字段天然属于同一配置对象，用 `CommandRewriteConfig` 封装。

---

## 5. 任务分解（TDD）

### Task 1 — 新增 CommandRewriteConfig schema
**文件**：`nanobot/config/schema.py`

- 新增 `CommandRewriteConfig(Base)` 类，字段：`enabled: bool=False, verbose: bool=False, timeout: float=5.0`
- 挂到合适的父配置节点（需要先确认父节点；候选：根 `NanobotConfig`、`AgentConfig` 或紧邻 `exec` 的兄弟节点）
- **先写测试**：`tests/config/test_command_rewrite_config.py`
  - `test_defaults_all_off`
  - `test_camel_case_parsing`（`commandRewrite.enabled`）
  - `test_enabled_with_custom_timeout`
- 运行 `pytest tests/config/test_command_rewrite_config.py` 绿

**不删**任何旧 rtk 字段——本 task 纯增量，保持 CI 绿。

**Commit**: `feat(config): add CommandRewriteConfig schema`

---

### Task 2 — 实现 CommandRewriteHook
**文件**：`nanobot/agent/hooks/__init__.py`（新建）、`nanobot/agent/hooks/rewrite.py`（新建）

**先写测试**：`tests/agent/hooks/test_command_rewrite.py`
- `test_hook_disabled_skips_rewrite`：enabled=False 时不 spawn 进程
- `test_hook_rewrites_exec_command`：mock subprocess 返回 `rtk git status`，验证 `tc.arguments["command"]` 被改写
- `test_hook_ignores_non_exec_tool_calls`：对 `read_file` tool_call 不做任何事
- `test_hook_ignores_missing_command_arg`：`arguments` 无 `command` 键时静默跳过
- `test_hook_fail_safe_on_subprocess_error`：subprocess 抛异常时返回原命令、不抛给调用方
- `test_hook_fail_safe_on_nonzero_returncode`：returncode != 0 时返回原命令
- `test_hook_timeout_fail_safe`：subprocess 超时时返回原命令
- `test_hook_handles_multiple_exec_calls`：一轮多个 exec tool_call 全部改写
- `test_hook_verbose_logs_on_change`：verbose=True 且命令确实改变时打 debug 日志

**实现**：
```python
# nanobot/agent/hooks/rewrite.py
# ============================================================================
#  Command Rewrite Hook
#  横切工具参数改写层。当前策略：rtk 命令压缩。
# ============================================================================

class CommandRewriteHook(AgentHook):
    def __init__(self, *, enabled=False, verbose=False, timeout=5.0, path_append=""):
        self._enabled = enabled
        self._verbose = verbose
        self._timeout = timeout
        self._path_append = path_append

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        if not self._enabled:
            return
        for tc in context.tool_calls:
            if tc.name != "exec":
                continue
            cmd = tc.arguments.get("command")
            if not isinstance(cmd, str) or not cmd.strip():
                continue
            rewritten = await self._rewrite(cmd)
            if rewritten and rewritten != cmd:
                tc.arguments["command"] = rewritten
                if self._verbose:
                    logger.debug("rtk rewrite: {} → {}", cmd, rewritten)

    async def _rewrite(self, command: str) -> str:
        try:
            env = os.environ.copy()
            if self._path_append:
                env["PATH"] = env.get("PATH", "") + os.pathsep + self._path_append
            proc = await asyncio.create_subprocess_exec(
                "rtk", "rewrite", command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
            if proc.returncode == 0 and stdout:
                return stdout.decode().strip()
        except Exception as e:
            logger.debug("rtk rewrite failed (passthrough): {}", e)
        return command
```

测试绿 → **Commit**: `feat(hooks): add CommandRewriteHook for cross-cutting tool argument rewrite`

---

### Task 3 — 主 loop 接入
**文件**：`nanobot/agent/loop.py`

- `AgentLoop.__init__` 读取 `command_rewrite` 配置（从 `NanobotConfig` 或父节点传入）
- 若 `enabled`，创建 `CommandRewriteHook` 实例并 append 到 `self._extra_hooks`
- **保持**旧 `rtk_enabled/rtk_verbose` 透传到 ExecTool（本 task 不删，避免行为回归）

**先写测试**：`tests/agent/test_loop_rewrite_hook_injection.py`
- `test_command_rewrite_disabled_not_injected`：hook 不在 `_extra_hooks` 中
- `test_command_rewrite_enabled_injected`：hook 在 `_extra_hooks` 中，且 `_enabled=True`

**Commit**: `feat(loop): inject CommandRewriteHook into main agent loop`

---

### Task 4 — subagent 接入
**文件**：`nanobot/agent/subagent.py`、`nanobot/agent/loop.py`

- `SubagentManager.__init__` 新增参数 `extra_hooks: list[AgentHook] | None = None`
- `_run_subagent` 中：
  ```python
  hook = _SubagentHook(task_id)
  if self._extra_hooks:
      hook = CompositeHook([hook, *self._extra_hooks])
  ```
  （复用 `nanobot/agent/hook.py` 已有的 `CompositeHook`）
- `AgentLoop` 创建 `SubagentManager` 时传入同一份 `command_rewrite` hook 实例

**先写测试**：
- `tests/agent/test_subagent_hook_composition.py`
  - `test_subagent_default_no_extra_hooks`
  - `test_subagent_extra_hooks_composed`：传入一个间谍 hook，触发 `before_execute_tools` 时确认被调用
  - `test_subagent_command_rewritten`：端到端 mock rtk 验证 subagent 路径命令被改写

**Commit**: `feat(subagent): propagate extra hooks into subagent runner`

---

### Task 5 — 删除旧实现
**文件**：`nanobot/agent/tools/shell.py`、`nanobot/config/schema.py`、`nanobot/agent/loop.py`、`tests/tools/test_rtk_rewrite.py`

- 删除 `ExecTool.rtk_enabled/rtk_verbose/_rtk_rewrite`
- 删除 `ExecTool.execute()` 入口处 rtk 分支
- 删除 `ExecToolConfig.rtk_enabled/rtk_verbose`
- 删除 `loop.py:277-278` 的 rtk 透传
- 删除 `tests/tools/test_rtk_rewrite.py` 整个文件
- 审查 `shell.py` 的 `import os`——若 rtk 是唯一用途则一并清理

**验证**：
- 运行 `pytest` 全量，期望 1062-6=1056 测试通过（旧 rtk 测试 6 个被删）+ 新增测试全绿
- `grep -rn "rtk" nanobot/` 应仅剩文档/注释中的历史记录

**Commit**: `refactor(exec): remove rtk rewrite from ExecTool (moved to CommandRewriteHook)`

---

### Task 6 — 架构文档
**文件**：`nanobot/agent/hooks/CLAUDE.md`（新建）

内容要求：
- 目录树：`hooks/` 下每个 hook 一句话
- 设计哲学：为什么 hook > 工具内嵌
- 扩展模板：如何新增一个改写类 hook（三步：继承 `AgentHook`、实现 `before_execute_tools`、在 loop 注入）

同步更新 `nanobot/agent/CLAUDE.md`（如存在）：在目录树中新增 `hooks/` 子模块说明。

**Commit**: `docs(hooks): add architecture notes for cross-cutting hooks`

---

### Task 7 — CHANGELOG 与迁移说明
**文件**：`CHANGELOG.md` 或 `MIGRATION.md`

记录：
- Breaking: `ExecToolConfig.rtk_enabled/rtk_verbose` 删除
- 迁移：改为 `commandRewrite.enabled/verbose/timeout`
- 新收益：subagent 路径现已支持命令改写

**Commit**: `docs: document rtk→commandRewrite migration`

---

## 6. 验证清单（完成前必过）

- [ ] `pytest tests/` 全绿
- [ ] `pytest tests/agent/hooks/` 全绿（新增测试）
- [ ] `grep -rn "rtk_enabled\|rtk_verbose" nanobot/` 返回空
- [ ] 手动冒烟：改 `~/workspace/nanobot_config/config.json` 添加 `commandRewrite: {enabled: true, verbose: true}`，`touch` 触发重启，执行一条 `git status` 类命令，`journalctl --user -u nanobot-gateway -f` 看到 `rtk rewrite: git status → rtk git status`
- [ ] 手动冒烟：用 `spawn` 触发一个 subagent 执行 exec 命令，确认日志里 subagent 路径也触发了 rewrite（**这是旧方案无法验证的新能力**）
- [ ] `CLAUDE.md` 架构图同步更新

---

## 7. 风险与回退

| 风险 | 影响 | 缓解 |
|---|---|---|
| `rtk` 进程卡死 | 单次 tool_call 延迟 ≤ 5s（timeout 保护） | `CommandRewriteHook._timeout` 可配置，默认 5s |
| 幂等性问题 | checkpoint 恢复时 messages 里是原命令，重跑会再改写；若 rtk 不幂等则结果飘 | 与旧方案一致，**不回归**；文档标注"rtk 应保证幂等" |
| Hook schema 硬编码 `tc.name == "exec"` 和 `arguments["command"]` | ExecTool schema 变更会使 hook 静默失效 | 在 hook 内 log.debug 记录命中/未命中计数；future: ExecTool 变更需同步更新 hook |
| subagent 与主 loop 共享同一 hook 实例 | 无状态 hook 无并发问题；若未来加统计计数器需改 | 当前版本纯函数式，安全 |

**回退方案**：git revert Task 5 的 commit（删除动作独立），即可恢复旧内嵌实现；Tasks 1-4 是纯增量，保留不影响。

---

## 8. 不做的事（YAGNI 防线）

- ❌ 不为多策略改写器引入抽象基类（当前只有 rtk 一个策略）
- ❌ 不做 rewriter 注册中心/插件系统
- ❌ 不支持按命令 pattern 有选择性 rewrite（rtk 自己会判断）
- ❌ 不做 rewrite 前后 diff 审计日志（`verbose` 够用）
- ❌ 不做配置热重载（走现有 config 重启机制）

---

## 9. 执行选项

**计划已完成保存至 `docs/plans/2026-04-08-rtk-rewrite-hook-migration.md`。两种执行路径：**

1. **Subagent-Driven（推荐）** — 每个 Task 派一个全新 subagent，两阶段 review，快速迭代
2. **Inline Execution** — 在当前 session 批量执行，checkpoint 间 review

选哪个？
