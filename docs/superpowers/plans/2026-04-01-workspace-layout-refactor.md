# Workspace Layout Refactor Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure workspace directories so sessions, llm_logs, and people are organized per-channel under a `discord/` hierarchy, with a `WorkspaceLayout` class owning all path calculations.

**Architecture:** Introduce `WorkspaceLayout` dataclass as the single source of truth for all workspace paths. Refactor `SessionManager` to accept layout instead of computing paths internally. `/new` preserves old session files (append-only archive). `TraceHook` and `ContextBuilder` delegate path resolution to `WorkspaceLayout`.

**Tech Stack:** Python 3.13, pytest, nanobot internals (no new dependencies)

**Spec:** `docs/superpowers/specs/2026-04-01-workspace-layout-refactor-design.md`

---

## Chunk 1: WorkspaceLayout + SessionManager refactor

### Task 1: Create WorkspaceLayout dataclass

**Files:**
- Create: `nanobot/workspace/__init__.py`
- Create: `nanobot/workspace/layout.py`
- Test: `tests/workspace/__init__.py`
- Test: `tests/workspace/test_layout.py`

- [ ] **Step 1: Write failing tests for WorkspaceLayout**

```python
# tests/workspace/__init__.py
# (empty)

# tests/workspace/test_layout.py
import pytest
from pathlib import Path

from nanobot.workspace.layout import WorkspaceLayout, make_layout


class TestWorkspaceLayout:
    def test_channel_dir(self, tmp_path: Path):
        layout = WorkspaceLayout(
            workspace=tmp_path, channel="discord",
            channel_name="develop", chat_id="147xxx",
        )
        assert layout.channel_dir == tmp_path / "discord"

    def test_scope_dir(self, tmp_path: Path):
        layout = WorkspaceLayout(
            workspace=tmp_path, channel="discord",
            channel_name="develop", chat_id="147xxx",
        )
        assert layout.scope_dir == tmp_path / "discord" / "develop"

    def test_sessions_dir(self, tmp_path: Path):
        layout = WorkspaceLayout(
            workspace=tmp_path, channel="discord",
            channel_name="develop", chat_id="147xxx",
        )
        assert layout.sessions_dir == tmp_path / "discord" / "develop" / "sessions"

    def test_llm_logs_dir(self, tmp_path: Path):
        layout = WorkspaceLayout(
            workspace=tmp_path, channel="discord",
            channel_name="develop", chat_id="147xxx",
        )
        assert layout.llm_logs_dir == tmp_path / "discord" / "develop" / "llm_logs"

    def test_people_dir(self, tmp_path: Path):
        layout = WorkspaceLayout(
            workspace=tmp_path, channel="discord",
            channel_name="develop", chat_id="147xxx",
        )
        assert layout.people_dir == tmp_path / "discord" / "people"

    def test_agent_md(self, tmp_path: Path):
        layout = WorkspaceLayout(
            workspace=tmp_path, channel="discord",
            channel_name="develop", chat_id="147xxx",
        )
        assert layout.agent_md == tmp_path / "discord" / "develop" / "AGENT.md"

    def test_session_path(self, tmp_path: Path):
        layout = WorkspaceLayout(
            workspace=tmp_path, channel="discord",
            channel_name="develop", chat_id="147xxx",
        )
        assert layout.session_path("2026-04-01", 1) == (
            tmp_path / "discord" / "develop" / "sessions" / "2026-04-01_147xxx_01.jsonl"
        )

    def test_llm_log_path(self, tmp_path: Path):
        layout = WorkspaceLayout(
            workspace=tmp_path, channel="discord",
            channel_name="develop", chat_id="147xxx",
        )
        assert layout.llm_log_path("2026-04-01", 1) == (
            tmp_path / "discord" / "develop" / "llm_logs" / "2026-04-01_147xxx_01.jsonl"
        )

    def test_next_session_seq_empty_dir(self, tmp_path: Path):
        layout = WorkspaceLayout(
            workspace=tmp_path, channel="discord",
            channel_name="develop", chat_id="147xxx",
        )
        assert layout.next_session_seq("2026-04-01") == 1

    def test_next_session_seq_existing_files(self, tmp_path: Path):
        layout = WorkspaceLayout(
            workspace=tmp_path, channel="discord",
            channel_name="develop", chat_id="147xxx",
        )
        layout.ensure_dirs()
        (layout.sessions_dir / "2026-04-01_147xxx_01.jsonl").touch()
        (layout.sessions_dir / "2026-04-01_147xxx_02.jsonl").touch()
        assert layout.next_session_seq("2026-04-01") == 3

    def test_next_session_seq_different_date_ignored(self, tmp_path: Path):
        layout = WorkspaceLayout(
            workspace=tmp_path, channel="discord",
            channel_name="develop", chat_id="147xxx",
        )
        layout.ensure_dirs()
        (layout.sessions_dir / "2026-03-31_147xxx_01.jsonl").touch()
        assert layout.next_session_seq("2026-04-01") == 1

    def test_next_session_seq_different_chat_id_ignored(self, tmp_path: Path):
        layout = WorkspaceLayout(
            workspace=tmp_path, channel="discord",
            channel_name="develop", chat_id="147xxx",
        )
        layout.ensure_dirs()
        (layout.sessions_dir / "2026-04-01_999yyy_01.jsonl").touch()
        assert layout.next_session_seq("2026-04-01") == 1

    def test_ensure_dirs_creates_both(self, tmp_path: Path):
        layout = WorkspaceLayout(
            workspace=tmp_path, channel="discord",
            channel_name="develop", chat_id="147xxx",
        )
        layout.ensure_dirs()
        assert layout.sessions_dir.is_dir()
        assert layout.llm_logs_dir.is_dir()

    def test_frozen_immutable(self, tmp_path: Path):
        layout = WorkspaceLayout(
            workspace=tmp_path, channel="discord",
            channel_name="develop", chat_id="147xxx",
        )
        with pytest.raises(AttributeError):
            layout.channel = "telegram"

    def test_current_session_path_no_files(self, tmp_path: Path):
        layout = WorkspaceLayout(
            workspace=tmp_path, channel="discord",
            channel_name="develop", chat_id="147xxx",
        )
        assert layout.current_session_path("2026-04-01") is None

    def test_current_session_path_picks_highest_seq(self, tmp_path: Path):
        layout = WorkspaceLayout(
            workspace=tmp_path, channel="discord",
            channel_name="develop", chat_id="147xxx",
        )
        layout.ensure_dirs()
        (layout.sessions_dir / "2026-04-01_147xxx_01.jsonl").touch()
        (layout.sessions_dir / "2026-04-01_147xxx_03.jsonl").touch()
        assert layout.current_session_path("2026-04-01") == (
            layout.sessions_dir / "2026-04-01_147xxx_03.jsonl"
        )


class TestMakeLayout:
    def test_fallback_channel_name_to_chat_id(self, tmp_path: Path):
        layout = make_layout(tmp_path, "discord", None, "147xxx")
        assert layout.channel_name == "147xxx"

    def test_explicit_channel_name(self, tmp_path: Path):
        layout = make_layout(tmp_path, "discord", "develop", "147xxx")
        assert layout.channel_name == "develop"

    def test_dm_prefix(self, tmp_path: Path):
        layout = make_layout(tmp_path, "discord", "dm-haibin", "900xxx")
        assert layout.scope_dir == tmp_path / "discord" / "dm-haibin"

    def test_cli_as_discord_channel(self, tmp_path: Path):
        layout = make_layout(tmp_path, "discord", "cli", "direct")
        assert layout.scope_dir == tmp_path / "discord" / "cli"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/git_code/nanobot && python -m pytest tests/workspace/test_layout.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nanobot.workspace'`

- [ ] **Step 3: Implement WorkspaceLayout**

```python
# nanobot/workspace/__init__.py
"""Workspace layout and path management."""

from nanobot.workspace.layout import WorkspaceLayout, make_layout

__all__ = ["WorkspaceLayout", "make_layout"]
```

```python
# nanobot/workspace/layout.py
"""Workspace directory layout — single source of truth for all workspace paths.

所有消费方（SessionManager、TraceHook、ContextBuilder）只问 layout，不自己拼路径。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class WorkspaceLayout:
    """每个 session 上下文的路径计算器。"""

    workspace: Path
    channel: str          # "discord"
    channel_name: str     # "develop", "dm-haibin", "cli"
    chat_id: str          # Discord channel/thread ID, or "direct" for CLI

    # --- 目录 ---

    @property
    def channel_dir(self) -> Path:
        return self.workspace / self.channel

    @property
    def scope_dir(self) -> Path:
        return self.channel_dir / self.channel_name

    @property
    def sessions_dir(self) -> Path:
        return self.scope_dir / "sessions"

    @property
    def llm_logs_dir(self) -> Path:
        return self.scope_dir / "llm_logs"

    @property
    def people_dir(self) -> Path:
        return self.channel_dir / "people"

    # --- 文件 ---

    @property
    def agent_md(self) -> Path:
        return self.scope_dir / "AGENT.md"

    def session_path(self, date: str, seq: int) -> Path:
        return self.sessions_dir / f"{date}_{self.chat_id}_{seq:02d}.jsonl"

    def llm_log_path(self, date: str, seq: int) -> Path:
        return self.llm_logs_dir / f"{date}_{self.chat_id}_{seq:02d}.jsonl"

    def current_session_path(self, date: str) -> Path | None:
        """当天最新（序号最大）的 session 文件，不存在返回 None。"""
        if not self.sessions_dir.exists():
            return None
        prefix = f"{date}_{self.chat_id}_"
        matches = sorted(self.sessions_dir.glob(f"{prefix}*.jsonl"))
        return matches[-1] if matches else None

    def next_session_seq(self, date: str) -> int:
        """当天下一个可用序号。"""
        if not self.sessions_dir.exists():
            return 1
        prefix = f"{date}_{self.chat_id}_"
        existing = []
        for p in self.sessions_dir.glob(f"{prefix}*.jsonl"):
            try:
                existing.append(int(p.stem.rsplit("_", 1)[-1]))
            except ValueError:
                continue
        return max(existing, default=0) + 1

    def ensure_dirs(self) -> None:
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.llm_logs_dir.mkdir(parents=True, exist_ok=True)


def make_layout(
    workspace: Path,
    channel: str,
    channel_name: str | None,
    chat_id: str,
) -> WorkspaceLayout:
    """构造 layout。channel_name 未知时 fallback 到 chat_id。"""
    return WorkspaceLayout(
        workspace=workspace,
        channel=channel,
        channel_name=channel_name or chat_id,
        chat_id=chat_id,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/git_code/nanobot && python -m pytest tests/workspace/test_layout.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
cd /root/git_code/nanobot
git add nanobot/workspace/__init__.py nanobot/workspace/layout.py \
        tests/workspace/__init__.py tests/workspace/test_layout.py
git commit -m "feat: add WorkspaceLayout dataclass for unified path calculations"
```

---

### Task 2: Refactor SessionManager to use WorkspaceLayout

**Files:**
- Modify: `nanobot/session/manager.py` (全文重构)
- Test: `tests/workspace/test_session_manager.py` (新增)
- Modify: `tests/agent/test_session_manager_history.py` (适配，Session 类不变)

- [ ] **Step 1: Write failing tests for new SessionManager**

```python
# tests/workspace/test_session_manager.py
"""Tests for SessionManager with WorkspaceLayout."""

import json
from datetime import date
from pathlib import Path

import pytest

from nanobot.session.manager import Session, SessionManager
from nanobot.workspace.layout import WorkspaceLayout


def _make_layout(tmp_path: Path, channel_name: str = "develop", chat_id: str = "147xxx") -> WorkspaceLayout:
    return WorkspaceLayout(
        workspace=tmp_path, channel="discord",
        channel_name=channel_name, chat_id=chat_id,
    )


class TestSessionManagerGetOrCreate:
    def test_creates_new_session(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        layout = _make_layout(tmp_path)
        session = mgr.get_or_create(layout)
        assert session.key == "discord:147xxx"
        assert session.messages == []

    def test_returns_cached_session(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        layout = _make_layout(tmp_path)
        s1 = mgr.get_or_create(layout)
        s1.add_message("user", "hello")
        s2 = mgr.get_or_create(layout)
        assert s2 is s1
        assert len(s2.messages) == 1


class TestSessionManagerSaveLoad:
    def test_save_creates_dated_file(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        layout = _make_layout(tmp_path)
        session = mgr.get_or_create(layout)
        session.add_message("user", "hello")
        mgr.save(session)
        today = date.today().isoformat()
        expected = layout.session_path(today, 1)
        assert expected.exists()

    def test_load_from_disk(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        layout = _make_layout(tmp_path)
        session = mgr.get_or_create(layout)
        session.add_message("user", "hello")
        mgr.save(session)

        # Fresh manager, no cache
        mgr2 = SessionManager(tmp_path)
        session2 = mgr2.get_or_create(layout)
        assert len(session2.messages) == 1
        assert session2.messages[0]["content"] == "hello"


class TestSessionManagerNew:
    def test_new_session_preserves_old_file(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        layout = _make_layout(tmp_path)
        session = mgr.get_or_create(layout)
        session.add_message("user", "old message")
        mgr.save(session)

        today = date.today().isoformat()
        old_path = layout.session_path(today, 1)
        assert old_path.exists()

        # Simulate /new
        snapshot = session.messages[session.last_consolidated:]
        mgr.new_session(layout)

        # 旧文件保留
        assert old_path.exists()
        with open(old_path, encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
        assert any('"old message"' in l for l in lines)

        # 新 session 是空的
        session2 = mgr.get_or_create(layout)
        assert session2.messages == []

        # 保存新 session 到 seq 02
        session2.add_message("user", "new message")
        mgr.save(session2)
        new_path = layout.session_path(today, 2)
        assert new_path.exists()


class TestSessionManagerLlmLogPath:
    def test_current_llm_log_path(self, tmp_path: Path):
        mgr = SessionManager(tmp_path)
        layout = _make_layout(tmp_path)
        session = mgr.get_or_create(layout)
        mgr.save(session)
        today = date.today().isoformat()
        assert mgr.current_llm_log_path(layout) == layout.llm_log_path(today, 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/git_code/nanobot && python -m pytest tests/workspace/test_session_manager.py -v`
Expected: FAIL — `SessionManager` doesn't accept `WorkspaceLayout` yet

- [ ] **Step 3: Refactor SessionManager**

Modify `nanobot/session/manager.py`:
- `SessionManager.__init__` 不再创建 `self.sessions_dir`，改为只存 `self.workspace`
- `get_or_create(layout: WorkspaceLayout)` 替代 `get_or_create(key: str)`
- 内部用 `layout.current_session_path(today)` 查当天最新文件
- `save(session)` 使用 session 上存储的 layout 信息写到正确路径
- 新增 `new_session(layout)` 方法：清空缓存中的 session，不删旧文件
- `_load` 改为从 layout 计算的路径读取
- Session dataclass 新增 `_file_path: Path | None` 内部字段追踪当前写入路径

完整改动太长，核心变更模式：

```python
class SessionManager:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self._cache: dict[str, Session] = {}

    @staticmethod
    def _session_key(layout: WorkspaceLayout) -> str:
        return f"{layout.channel}:{layout.chat_id}"

    def get_or_create(self, layout: WorkspaceLayout) -> Session:
        key = self._session_key(layout)
        if key in self._cache:
            return self._cache[key]
        session = self._load(layout)
        if session is None:
            layout.ensure_dirs()
            today = date.today().isoformat()
            seq = layout.next_session_seq(today)
            session = Session(key=key)
            session.metadata["_file_path"] = str(layout.session_path(today, seq))
        self._cache[key] = session
        return session

    def save(self, session: Session) -> None:
        path = Path(session.metadata["_file_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        # ... write JSONL (same format as before)

    def new_session(self, layout: WorkspaceLayout) -> Session:
        """Archive current session (keep file), create fresh session with next seq."""
        key = self._session_key(layout)
        old = self._cache.pop(key, None)
        snapshot = old.messages[old.last_consolidated:] if old else []

        layout.ensure_dirs()
        today = date.today().isoformat()
        seq = layout.next_session_seq(today)
        session = Session(key=key)
        session.metadata["_file_path"] = str(layout.session_path(today, seq))
        self._cache[key] = session
        return session  # caller archives snapshot separately

    def current_llm_log_path(self, layout: WorkspaceLayout) -> Path:
        """与当前 session 对应的 llm_log 文件路径。"""
        today = date.today().isoformat()
        path = layout.current_session_path(today)
        if path:
            # 从 session 文件名提取 seq
            seq = int(path.stem.rsplit("_", 1)[-1])
        else:
            seq = layout.next_session_seq(today)
        return layout.llm_log_path(today, seq)
```

**注意：** 同时保留旧的 `get_or_create(key: str)` 签名作为兼容层（内部判断参数类型），让未改造的调用点不会立即 break。后续 task 逐步迁移完后删除。

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/git_code/nanobot && python -m pytest tests/workspace/test_session_manager.py tests/agent/test_session_manager_history.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
cd /root/git_code/nanobot
git add nanobot/session/manager.py tests/workspace/test_session_manager.py
git commit -m "refactor: SessionManager accepts WorkspaceLayout, /new preserves old files"
```

---

## Chunk 2: TraceHook, ContextBuilder, AgentLoop integration

### Task 3: Refactor TraceHook to use WorkspaceLayout

**Files:**
- Modify: `nanobot/agent/trace.py`
- Test: `tests/workspace/test_trace_layout.py` (新增)

- [ ] **Step 1: Write failing test**

```python
# tests/workspace/test_trace_layout.py
import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

from nanobot.agent.trace import TraceHook
from nanobot.agent.hook import AgentHookContext
from nanobot.workspace.layout import WorkspaceLayout


def test_trace_writes_to_llm_logs_dir(tmp_path: Path):
    layout = WorkspaceLayout(
        workspace=tmp_path, channel="discord",
        channel_name="develop", chat_id="147xxx",
    )
    layout.ensure_dirs()

    hook = TraceHook(llm_logs_dir=layout.llm_logs_dir)
    hook.session_key = "discord:147xxx"
    hook.set_log_path(layout.llm_log_path("2026-04-01", 1))

    # Simulate before_iteration + after_iteration
    ctx = MagicMock(spec=AgentHookContext)
    ctx.messages = [{"role": "user", "content": "hello"}]
    ctx.iteration = 0
    ctx.response = MagicMock()
    ctx.response.content = "world"
    ctx.response.finish_reason = "stop"
    ctx.tool_calls = []
    ctx.usage = {"prompt_tokens": 10, "completion_tokens": 5}

    asyncio.run(hook.before_iteration(ctx))
    asyncio.run(hook.after_iteration(ctx))

    log_path = layout.llm_log_path("2026-04-01", 1)
    assert log_path.exists()
    entry = json.loads(log_path.read_text().strip())
    assert entry["session_key"] == "discord:147xxx"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/git_code/nanobot && python -m pytest tests/workspace/test_trace_layout.py -v`
Expected: FAIL — `TraceHook.__init__()` doesn't accept `llm_logs_dir`

- [ ] **Step 3: Modify TraceHook**

改动 `nanobot/agent/trace.py`：
- `__init__` 接受 `llm_logs_dir: Path` 替代 `traces_dir: Path`（保留旧参数名兼容）
- 新增 `set_log_path(path: Path)` 方法，让调用方指定精确的输出文件路径
- `after_iteration` 写入 `_log_path`（如果设置了）而非自动计算路径

```python
class TraceHook(AgentHook):
    __slots__ = ("_log_dir", "_log_path", "_session_key", "_call_t0", "_call_kwargs")

    def __init__(self, llm_logs_dir: Path | None = None, traces_dir: Path | None = None) -> None:
        # 兼容：旧调用用 traces_dir，新调用用 llm_logs_dir
        self._log_dir = llm_logs_dir or traces_dir or Path(".")
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._log_path: Path | None = None
        self._session_key: str = "unknown"
        self._call_t0: float = 0
        self._call_kwargs: dict = {}

    def set_log_path(self, path: Path) -> None:
        """设置精确的日志文件路径（与 session 文件一一对应）。"""
        self._log_path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    async def after_iteration(self, context: AgentHookContext) -> None:
        # ... build entry same as before ...
        if self._log_path:
            path = self._log_path
        else:
            path = self._log_dir / f"{safe_filename(self._session_key.replace(':', '_'))}.jsonl"
        # ... append line ...
```

- [ ] **Step 4: Run tests**

Run: `cd /root/git_code/nanobot && python -m pytest tests/workspace/test_trace_layout.py tests/agent/ -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add nanobot/agent/trace.py tests/workspace/test_trace_layout.py
git commit -m "refactor: TraceHook accepts llm_logs_dir and set_log_path"
```

---

### Task 4: Refactor ContextBuilder for per-channel people + AGENT.md layer

**Files:**
- Modify: `nanobot/agent/context.py`
- Test: `tests/workspace/test_context_layout.py` (新增)

- [ ] **Step 1: Write failing tests**

```python
# tests/workspace/test_context_layout.py
from pathlib import Path

from nanobot.agent.context import ContextBuilder
from nanobot.workspace.layout import WorkspaceLayout


def _setup_workspace(tmp_path: Path) -> Path:
    """创建 root bootstrap 文件。"""
    (tmp_path / "SOUL.md").write_text("Root soul", encoding="utf-8")
    (tmp_path / "USER.md").write_text("Root user", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("Root agents", encoding="utf-8")
    (tmp_path / "TOOLS.md").write_text("Root tools", encoding="utf-8")
    return tmp_path


class TestPeopleOverrideFromLayout:
    def test_per_channel_people_overrides_root(self, tmp_path: Path):
        _setup_workspace(tmp_path)
        layout = WorkspaceLayout(
            workspace=tmp_path, channel="discord",
            channel_name="kids", chat_id="123",
        )
        # 创建 discord/people/petch/SOUL.md
        people_dir = layout.people_dir / "petch"
        people_dir.mkdir(parents=True)
        (people_dir / "SOUL.md").write_text("Petch soul override", encoding="utf-8")

        builder = ContextBuilder(tmp_path)
        path = builder._resolve_bootstrap_path("SOUL.md", "petch", layout=layout)
        assert "Petch soul override" in path.read_text()

    def test_fallback_to_root_when_no_override(self, tmp_path: Path):
        _setup_workspace(tmp_path)
        layout = WorkspaceLayout(
            workspace=tmp_path, channel="discord",
            channel_name="kids", chat_id="123",
        )
        builder = ContextBuilder(tmp_path)
        path = builder._resolve_bootstrap_path("SOUL.md", "nobody", layout=layout)
        assert "Root soul" in path.read_text()


class TestAgentMdLayer:
    def test_agent_md_appended_to_bootstrap(self, tmp_path: Path):
        _setup_workspace(tmp_path)
        layout = WorkspaceLayout(
            workspace=tmp_path, channel="discord",
            channel_name="kids", chat_id="123",
        )
        layout.scope_dir.mkdir(parents=True)
        (layout.agent_md).write_text("# Kids channel rules\nBe gentle.", encoding="utf-8")

        builder = ContextBuilder(tmp_path)
        bootstrap = builder._load_bootstrap_files(sender_name=None, layout=layout)
        assert "Root agents" in bootstrap
        assert "Kids channel rules" in bootstrap

    def test_no_agent_md_no_error(self, tmp_path: Path):
        _setup_workspace(tmp_path)
        layout = WorkspaceLayout(
            workspace=tmp_path, channel="discord",
            channel_name="develop", chat_id="123",
        )
        builder = ContextBuilder(tmp_path)
        bootstrap = builder._load_bootstrap_files(sender_name=None, layout=layout)
        assert "Root agents" in bootstrap
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/git_code/nanobot && python -m pytest tests/workspace/test_context_layout.py -v`
Expected: FAIL — `_resolve_bootstrap_path` doesn't accept `layout`

- [ ] **Step 3: Modify ContextBuilder**

改动 `nanobot/agent/context.py`：

1. `_resolve_bootstrap_path` 新增 `layout: WorkspaceLayout | None = None` 参数：
   - 有 layout 时：先查 `layout.people_dir / {sender} / {file}`
   - 无 layout 时：保持旧逻辑（`workspace/people/...`）

2. `_load_bootstrap_files` 新增 `layout` 参数：
   - 加载完 root 四件套后，检查 `layout.agent_md`，有就 append 为 `## AGENT.md (channel)`

3. `build_system_prompt` 和 `build_messages` 新增 `layout` 参数透传

```python
def _resolve_bootstrap_path(
    self, filename: str, sender_name: str | None,
    layout: WorkspaceLayout | None = None,
) -> Path:
    if sender_name:
        # 新路径：discord/people/{sender}/{file}
        if layout:
            override = layout.people_dir / sender_name.lower() / filename
            if override.exists():
                return override
        # 旧路径 fallback
        override = self.workspace / "people" / sender_name.lower() / filename
        if override.exists():
            return override
    return self.workspace / filename

def _load_bootstrap_files(
    self, sender_name: str | None = None,
    layout: WorkspaceLayout | None = None,
) -> str:
    parts = []
    for filename in self.BOOTSTRAP_FILES:
        file_path = self._resolve_bootstrap_path(filename, sender_name, layout=layout)
        if file_path.exists():
            content = file_path.read_text(encoding="utf-8")
            parts.append(f"## {filename}\n\n{content}")

    # Layer: per-channel AGENT.md 追加
    if layout and layout.agent_md.exists():
        content = layout.agent_md.read_text(encoding="utf-8")
        parts.append(f"## AGENT.md (channel)\n\n{content}")

    return "\n\n".join(parts) if parts else ""
```

- [ ] **Step 4: Run tests**

Run: `cd /root/git_code/nanobot && python -m pytest tests/workspace/test_context_layout.py tests/agent/test_skill_filtering.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add nanobot/agent/context.py tests/workspace/test_context_layout.py
git commit -m "refactor: ContextBuilder resolves people + AGENT.md from WorkspaceLayout"
```

---

### Task 5: Wire WorkspaceLayout into AgentLoop

**Files:**
- Modify: `nanobot/agent/loop.py`
- Modify: `nanobot/command/builtin.py` (`cmd_new`)
- Modify: `nanobot/cli/commands.py` (TraceHook 初始化)

- [ ] **Step 1: Modify AgentLoop._process_message to construct layout**

在 `_process_message` 入口构造 `WorkspaceLayout`，传给 `sessions.get_or_create(layout)`、`context.build_messages(..., layout=layout)`、TraceHook。

核心改动位置（约 `loop.py:550-600`）：

```python
from nanobot.workspace.layout import make_layout

# 在 _process_message 中，ctx 构造之后：
layout = make_layout(
    workspace=self.workspace,
    channel=ctx.channel,
    channel_name=ctx.channel_name,
    chat_id=ctx.chat_id,
)

session = self.sessions.get_or_create(layout)
# ... 后续传 layout 给 context.build_messages
```

对 system message 路径（约 `loop.py:520`）做同样处理。

- [ ] **Step 2: Modify cmd_new to use new_session**

```python
# nanobot/command/builtin.py
async def cmd_new(ctx: CommandContext) -> OutboundMessage:
    loop = ctx.loop
    layout = ctx.layout  # 需要从 CommandContext 拿到 layout
    session = ctx.session or loop.sessions.get_or_create(layout)
    snapshot = session.messages[session.last_consolidated:]
    new_session = loop.sessions.new_session(layout)
    loop.sessions.save(new_session)
    if snapshot:
        loop._schedule_background(loop.memory_consolidator.archive_messages(snapshot))
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content="New session started.",
    )
```

需要给 `CommandContext` 添加 `layout: WorkspaceLayout | None = None` 字段。

- [ ] **Step 3: Update TraceHook initialization in cli/commands.py**

```python
# 约 cli/commands.py:651
# 旧：hooks.append(TraceHook(traces_dir=config.workspace_path / "traces"))
# 新：TraceHook 仍用旧模式初始化，AgentLoop 会在 _run_agent_loop 中调 set_log_path
hooks.append(TraceHook(traces_dir=config.workspace_path / "traces"))
# 注意：CLI 模式下仍需兼容旧路径，直到 layout 传入
```

- [ ] **Step 4: Run full test suite**

Run: `cd /root/git_code/nanobot && python -m pytest tests/ -v --timeout=30`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add nanobot/agent/loop.py nanobot/command/builtin.py nanobot/command/router.py nanobot/cli/commands.py
git commit -m "feat: wire WorkspaceLayout into AgentLoop, cmd_new, and TraceHook"
```

---

## Chunk 3: Discord adapter + Migration script

### Task 6: Ensure Discord adapter passes channel_name for DM

**Files:**
- Modify: `nanobot/channels/discord.py`

- [ ] **Step 1: Review current _resolve_channel_name for DM handling**

当前代码（约 `discord.py:420-448`）对 thread 递归取 parent name，但 DM channel（type=1）没有 `name` 字段。

- [ ] **Step 2: Add DM channel_name resolution**

```python
# 在 _resolve_channel_name 中，data 返回后：
channel_type = data.get("type")

# Thread → parent
if channel_type in (10, 11, 12) and data.get("parent_id"):
    name = await self._resolve_channel_name(data["parent_id"])
# DM → dm-{username}
elif channel_type == 1:
    recipients = data.get("recipients") or []
    if recipients:
        name = f"dm-{recipients[0].get('username', data.get('id', 'unknown'))}"
    else:
        name = f"dm-{channel_id}"
# Guild channel → name
else:
    name = data.get("name")
```

- [ ] **Step 3: Test manually**

发一条 DM 给 bot，观察 log 确认 `channel_name` 被正确设为 `dm-{username}`。

- [ ] **Step 4: Commit**

```bash
git add nanobot/channels/discord.py
git commit -m "feat: Discord adapter resolves DM channel_name as dm-{username}"
```

---

### Task 7: CLI mode layout — channel_name = "cli"

**Files:**
- Modify: `nanobot/agent/loop.py` (CLI fallback)
- Modify: `nanobot/cli/commands.py` (传递 channel_name 给 metadata)

- [ ] **Step 1: Add CLI channel_name fallback**

在 `AgentLoop._process_message` 中 `make_layout` 调用处：

```python
channel_name = ctx.channel_name
if not channel_name and ctx.channel == "cli":
    channel_name = "cli"
layout = make_layout(
    workspace=self.workspace,
    channel="discord",  # CLI 也归入 discord 目录
    channel_name=channel_name,
    chat_id=ctx.chat_id,
)
```

- [ ] **Step 2: Commit**

```bash
git add nanobot/agent/loop.py
git commit -m "feat: CLI mode uses discord/cli/ as workspace path"
```

---

### Task 8: Migration script

**Files:**
- Create: `scripts/migrate_workspace_layout.py`

- [ ] **Step 1: Write migration script**

```python
#!/usr/bin/env python3
"""一次性迁移脚本：将 workspace 旧目录结构迁移到新的 per-channel 布局。

用法：
  python scripts/migrate_workspace_layout.py /path/to/workspace --discord-token BOT_TOKEN --guild-id GUILD_ID
  python scripts/migrate_workspace_layout.py /path/to/workspace --discord-token BOT_TOKEN --guild-id GUILD_ID --dry-run

步骤：
  1. 从 Discord API 拉取 guild channels 映射
  2. 移动 people/ → discord/people/
  3. 移动 sessions/discord_{id}.jsonl → discord/{name}/sessions/{date}_{id}_01.jsonl
  4. 移动 traces/discord_{id}.jsonl → discord/{name}/llm_logs/{date}_{id}_01.jsonl
  5. 清理空旧目录
"""

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import httpx

DISCORD_API = "https://discord.com/api/v10"


def fetch_channel_map(token: str, guild_id: str) -> dict[str, str]:
    """chat_id → channel_name mapping from Discord API."""
    headers = {"Authorization": f"Bot {token}"}
    resp = httpx.get(f"{DISCORD_API}/guilds/{guild_id}/channels", headers=headers)
    resp.raise_for_status()
    return {str(ch["id"]): ch["name"] for ch in resp.json() if ch.get("name")}


def extract_created_date(jsonl_path: Path) -> str:
    """从 JSONL 第一行 metadata 提取 created_at 日期。"""
    with open(jsonl_path, encoding="utf-8") as f:
        first = f.readline().strip()
        if first:
            data = json.loads(first)
            if data.get("_type") == "metadata" and data.get("created_at"):
                return datetime.fromisoformat(data["created_at"]).strftime("%Y-%m-%d")
    return datetime.now().strftime("%Y-%m-%d")


def migrate(workspace: Path, channel_map: dict[str, str], dry_run: bool = False) -> None:
    # 1. people
    old_people = workspace / "people"
    new_people = workspace / "discord" / "people"
    if old_people.exists() and not new_people.exists():
        print(f"{'[DRY] ' if dry_run else ''}mv {old_people} → {new_people}")
        if not dry_run:
            new_people.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_people), str(new_people))

    # 2. sessions
    sessions_dir = workspace / "sessions"
    if sessions_dir.exists():
        for f in sessions_dir.glob("discord_*.jsonl"):
            chat_id = f.stem.replace("discord_", "")
            name = channel_map.get(chat_id, chat_id)
            created = extract_created_date(f)
            dest = workspace / "discord" / name / "sessions" / f"{created}_{chat_id}_01.jsonl"
            print(f"{'[DRY] ' if dry_run else ''}mv {f.name} → {dest.relative_to(workspace)}")
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(dest))

    # 3. traces → llm_logs
    traces_dir = workspace / "traces"
    if traces_dir.exists():
        for f in traces_dir.glob("discord_*.jsonl"):
            chat_id = f.stem.replace("discord_", "")
            name = channel_map.get(chat_id, chat_id)
            created = extract_created_date(
                workspace / "discord" / name / "sessions" / f.name
            ) if (workspace / "discord" / name / "sessions").exists() else datetime.now().strftime("%Y-%m-%d")
            dest = workspace / "discord" / name / "llm_logs" / f"{created}_{chat_id}_01.jsonl"
            print(f"{'[DRY] ' if dry_run else ''}mv {f.name} → {dest.relative_to(workspace)}")
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(dest))

    # 4. CLI session
    cli_session = sessions_dir / "cli_direct.jsonl" if sessions_dir.exists() else None
    if cli_session and cli_session.exists():
        created = extract_created_date(cli_session)
        dest = workspace / "discord" / "cli" / "sessions" / f"{created}_direct_01.jsonl"
        print(f"{'[DRY] ' if dry_run else ''}mv cli_direct.jsonl → {dest.relative_to(workspace)}")
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(cli_session), str(dest))

    # 5. 清理
    for d in [sessions_dir, traces_dir, old_people]:
        if d.exists() and not any(d.iterdir()):
            print(f"{'[DRY] ' if dry_run else ''}rmdir {d.relative_to(workspace)}")
            if not dry_run:
                d.rmdir()


def main():
    parser = argparse.ArgumentParser(description="Migrate workspace to per-channel layout")
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--discord-token", required=True)
    parser.add_argument("--guild-id", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.workspace.exists():
        print(f"Workspace not found: {args.workspace}", file=sys.stderr)
        sys.exit(1)

    print("Fetching Discord channel map...")
    channel_map = fetch_channel_map(args.discord_token, args.guild_id)
    print(f"Found {len(channel_map)} channels")

    if not args.dry_run:
        backup = args.workspace.parent / "workspace_backup.tar.gz"
        print(f"Creating backup: {backup}")
        import tarfile
        with tarfile.open(backup, "w:gz") as tar:
            tar.add(args.workspace, arcname="workspace")

    migrate(args.workspace, channel_map, dry_run=args.dry_run)
    print("Done!" if not args.dry_run else "Dry run complete. No files moved.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Test dry-run**

```bash
cd /root/git_code/nanobot
python scripts/migrate_workspace_layout.py /root/workspace \
  --discord-token "$(jq -r '.channels.discord.token' ~/.nanobot/config.json)" \
  --guild-id "1475645691358613558" \
  --dry-run
```

Expected: 打印所有计划移动的文件，不实际执行

- [ ] **Step 3: Commit**

```bash
git add scripts/migrate_workspace_layout.py
git commit -m "feat: add workspace layout migration script"
```

---

## Chunk 4: Cleanup + legacy compat removal

### Task 9: Remove legacy session path fallback

**Files:**
- Modify: `nanobot/session/manager.py` (移除 `_get_legacy_session_path` 和旧的 `_get_session_path`)
- Modify: `nanobot/config/paths.py` (移除 `get_legacy_sessions_dir` 如果不再有其他调用方)

- [ ] **Step 1: Search for remaining legacy path references**

```bash
grep -rn "legacy_sessions\|_get_session_path\|_get_legacy" nanobot/ --include="*.py"
```

- [ ] **Step 2: Remove dead code**

仅在确认迁移完成且所有调用点已迁到 layout 后执行。

- [ ] **Step 3: Run full test suite**

```bash
cd /root/git_code/nanobot && python -m pytest tests/ -v --timeout=30
```

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "chore: remove legacy session path fallback code"
```

---

### Task 10: End-to-end smoke test

- [ ] **Step 1: Run migration (real)**

```bash
python scripts/migrate_workspace_layout.py /root/workspace \
  --discord-token "$(jq -r '.channels.discord.token' ~/.nanobot/config.json)" \
  --guild-id "1475645691358613558"
```

- [ ] **Step 2: Restart nanobot**

```bash
touch ~/.nanobot/config.json
```

- [ ] **Step 3: Send a message in Discord, verify:**
- Session file created at `workspace/discord/{channel_name}/sessions/{date}_{id}_01.jsonl`
- LLM log created at `workspace/discord/{channel_name}/llm_logs/{date}_{id}_01.jsonl`
- Bot responds normally

- [ ] **Step 4: Run /new, send another message, verify:**
- Old session file preserved
- New file created with `_02` suffix
- Bot has clean context

- [ ] **Step 5: Send DM to bot, verify:**
- Session created at `workspace/discord/dm-{username}/sessions/...`

- [ ] **Step 6: Final commit**

```bash
git add -A
git commit -m "docs: mark workspace layout refactor as complete"
```
