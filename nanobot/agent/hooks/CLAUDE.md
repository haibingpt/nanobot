# nanobot/agent/hooks/

横切关注点的接入层：在 runner 生命周期的缝隙里，做与具体工具解耦的事。

## 目录树

```
hooks/
├── __init__.py    # 公共导出（CommandRewriteHook ...）
└── rewrite.py     # CommandRewriteHook：拦截 exec 工具参数，调外部 rtk 等价改写
```

## 设计哲学

为什么是 hook，而不是把逻辑塞进工具内部？

- **横切关注点**：参数改写、脱敏、审计、限流——这些不属于任何一个工具的"业务"，而属于 runner 的执行边界。塞进工具就把全局策略钉死在局部实现里。
- **多工具复用**：一个 hook 可以同时关心 `exec` / `shell` / 未来的 `bash`，不必每个工具各写一份相同的预处理。
- **主 loop 与 subagent 统一接入**：`AgentLoop` 与 `SubagentManager` 共用同一套 `AgentHook` 协议，hook 一次注入，两条调用链都生效，行为保持一致。
- **零侵入回退**：hook 的实现内部 fail-safe，外部依赖（rtk 进程、网络 ...）出问题时透传原值，不污染 runner 状态。

## 扩展模板：三步加一个新 hook

1. **写 hook 类**：在本目录新增 `xxx.py`，继承 `nanobot.agent.hook.AgentHook`，按需实现生命周期方法：
   - `before_iteration` / `after_iteration`：迭代前后整体观察
   - `before_execute_tools`：在工具真正执行前改写 `context.tool_calls[*].arguments`
   - `on_stream` / `on_stream_end`：流式增量观察（需先 `wants_streaming()` 返回 True）
   - `finalize_content`：对最终输出做一次纯函数变换
   在 `__init__.py` 把新类加入 `__all__`。

2. **加 Config schema**：在 `nanobot/config/schema.py` 写一个 `XxxConfig`，至少含 `enabled: bool`，挂到顶层 config 上。

3. **在 loop 注入**：`nanobot/agent/loop.py` 的 `AgentLoop.__init__` 里读取 config，按 `enabled` 实例化 hook 追加进 `_extra_hooks`；若该 hook 也需作用于子 agent，构造 `SubagentManager` 时把它透传过去（参考 `CommandRewriteHook` 的接线方式）。

## 边界声明：hook 不该做的事

- **不持有业务状态**：hook 是横切的瞬时观察者。需要持久状态的功能写成工具或 service，不要藏在 hook 实例字段里。
- **不承载业务逻辑**：决策类逻辑（"该不该调用模型"、"用哪个 prompt"）属于 runner 或 agent 本身，不属于 hook。
- **不跨 hook 通信**：每个 hook 只看自己拿到的 `AgentHookContext`，不要通过全局变量或 import 互相喊话。需要协作就合并成一个 hook。
- **不抛异常打断 runner**：所有外部依赖必须 try/except 兜底，hook 失败应等价于 hook 不存在。
