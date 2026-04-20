# Changelog

本项目变更日志遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 规范，
并尽量遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Breaking

- 删除 `config.tools.exec.rtkEnabled` 与 `config.tools.exec.rtkVerbose` 字段。
- 旧 `config.json` 中如仍存在上述两个字段，会被 pydantic 直接拒绝，需要按下面的迁移路径改写。

### Added

- 新增 `config.tools.commandRewrite` 配置段，用于跨工具的命令参数改写：
  - `enabled: bool = false`
  - `verbose: bool = false`
  - `timeout: float = 5.0`
- 新增 `CommandRewriteHook`（`nanobot/agent/hooks/rewrite.py`），将原本耦合在 `ExecTool` 内部的 rtk 改写抽离为通用 hook。
- 主 agent loop 与 subagent 路径均受该 hook 覆盖。旧实现中 subagent 路径上 rtk 一直静默失效，本次一并修复。
- 新增 `agents.defaults.subagentModel` 配置，允许 subagent 使用独立的 LLM provider（模型分层）：
  - `subagentModel: str | None = None` — subagent 使用的模型名（如 `"anthropic/claude-haiku-4-5"`）；未配置则复用主 agent provider，行为零变化。
  - `subagentReasoningEffort: str | None = None` — 可选 `low`/`medium`/`high`，不配则用 provider 默认。
  - `subagentMaxTokens: int | None = None` — 可选输出 token 上限，不配则继承主 agent 行为。
  - Provider 构造失败时 warn + 回退主 agent provider，不阻断启动。
  - 适用场景：主 agent 跑强模型做规划/写作，subagent 跑快/便宜模型做日志分析、文件读取、pattern matching。

### Migration

从旧的 `rtkEnabled` / `rtkVerbose` 迁移到新的 `commandRewrite` 配置段：

```diff
 tools:
   exec:
     enable: true
-    rtkEnabled: true
-    rtkVerbose: true
+  commandRewrite:
+    enabled: true
+    verbose: true
```
