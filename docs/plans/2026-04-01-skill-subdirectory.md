# Spec: Skills 子目录管理

## 背景

`~/workspace/skills/` 下已有 47 个 skill，全部扁平堆在一级目录。随着 skill 数量增长，需要按类别分目录管理（`ljg/`, `coding/`, `invest/` 等），同时保持 skill name 扁平（不加前缀）。

## 目标

1. `SkillsLoader` 支持递归扫描子目录，发现所有含 `SKILL.md` 的目录
2. Skill name 保持为**目录名**（不含路径前缀），向后兼容
3. 同名冲突时先发现的赢（workspace > builtin，同源先遇到的优先）
4. `load_skill(name)` 能正确加载子目录下的 skill
5. 现有 config 中的 filter 规则无需任何修改

## 目录结构（目标）

```
skills/
  ljg/
    ljg-card/SKILL.md
    ljg-learn/SKILL.md
    ljg-plain/SKILL.md
    ljg-writes/SKILL.md
    ljg-explain-concept/SKILL.md
    ljg-skill-clip/SKILL.md
    ljg-skill-explain-words/SKILL.md
    ljg-skill-fetch/SKILL.md
    ljg-skill-rank/SKILL.md
    ljg-skill-roundtable/SKILL.md
    ljg-skill-skill-map/SKILL.md
    ljg-skill-the-one/SKILL.md
    ljg-skill-xray-article/SKILL.md
    ljg-skill-xray-book/SKILL.md
    ljg-skill-xray-paper/SKILL.md
    ljg-skill-xray-prompt/SKILL.md
    ljg-skill-xray-skill/SKILL.md
    ljg-invest/SKILL.md
    khb-skill-explain-chinese-words/SKILL.md
  coding/
    coding/SKILL.md
    brainstorming/SKILL.md
    systematic-debugging/SKILL.md
    test-driven-development/SKILL.md
    requesting-code-review/SKILL.md
    receiving-code-review/SKILL.md
    verification-before-completion/SKILL.md
    writing-plans/SKILL.md
    executing-plans/SKILL.md
    dispatching-parallel-agents/SKILL.md
    subagent-driven-development/SKILL.md
    skill-creator/SKILL.md
  invest/
    khb-investment-analyzer/SKILL.md
    khb-moat-seeker/SKILL.md
  petch/
    peppa-why/SKILL.md
    hanzi-stroke/SKILL.md
    ipad-monitor/SKILL.md
  research/
    interdisciplinary-research/SKILL.md
    news-aggregator-skill/SKILL.md
    notebooklm/SKILL.md
  tools/
    agent-browser/SKILL.md
    pdf/SKILL.md
    twitter-media/SKILL.md
    md-to-cloudflare/SKILL.md
    system-info/SKILL.md
    temp-ssh-grant/SKILL.md
    todo/SKILL.md
```

> `utils/` 目录不含 SKILL.md，不受影响。
> Builtin skills（clawhub, weather 等）不动，它们在 nanobot/skills/ 下。

## 技术方案

### 改动 1：`list_skills()` — 递归扫描

**现状：** 只扫描 `skills/` 直接子目录

```python
for skill_dir in self.workspace_skills.iterdir():
    if skill_dir.is_dir():
        skill_file = skill_dir / "SKILL.md"
        if skill_file.exists():
            skills.append(...)
```

**改为：** 用 `rglob("SKILL.md")` 找到所有 SKILL.md，skill name = SKILL.md 的父目录名

```python
if self.workspace_skills.exists():
    for skill_file in sorted(self.workspace_skills.rglob("SKILL.md")):
        name = skill_file.parent.name
        if not any(s["name"] == name for s in skills):
            skills.append({"name": name, "path": str(skill_file), "source": "workspace"})
```

关键点：
- `rglob` 递归查找，不限层数
- `sorted()` 保证扫描顺序确定性（字母序）
- `skill_file.parent.name` 取的是直接父目录名，不含路径前缀
- 去重逻辑不变：`not any(s["name"] == name for s in skills)`

Builtin skills 同理改为 `rglob`。

### 改动 2：`load_skill(name)` — 从缓存路径加载

**现状：** 硬编码路径 `self.workspace_skills / name / "SKILL.md"`

```python
workspace_skill = self.workspace_skills / name / "SKILL.md"
if workspace_skill.exists():
    return workspace_skill.read_text(encoding="utf-8")
```

**改为：** 先查 `list_skills()` 返回的路径映射，回退到旧逻辑

```python
def load_skill(self, name: str) -> str | None:
    # 先从已发现的 skill 列表查路径
    for s in self.list_skills(filter_unavailable=False):
        if s["name"] == name:
            path = Path(s["path"])
            if path.exists():
                return path.read_text(encoding="utf-8")
    return None
```

这样 `load_skill("ljg-card")` 即使 ljg-card 在 `skills/ljg/ljg-card/` 下也能找到。

**性能考虑：** `list_skills()` 会被多次调用。加一个 `_skills_cache`，首次扫描后缓存。

```python
def __init__(self, ...):
    ...
    self._skills_cache: list[dict[str, str]] | None = None

def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
    if self._skills_cache is None:
        self._skills_cache = self._discover_skills()
    skills = list(self._skills_cache)  # shallow copy
    if filter_unavailable:
        return [s for s in skills if self._check_requirements(self._get_skill_meta(s["name"]))]
    return skills

def _discover_skills(self) -> list[dict[str, str]]:
    """Scan workspace + builtin dirs for all skills."""
    skills = []
    # Workspace (recursive)
    if self.workspace_skills.exists():
        for skill_file in sorted(self.workspace_skills.rglob("SKILL.md")):
            name = skill_file.parent.name
            if not any(s["name"] == name for s in skills):
                skills.append({"name": name, "path": str(skill_file), "source": "workspace"})
    # Builtin (recursive)
    if self.builtin_skills and self.builtin_skills.exists():
        for skill_file in sorted(self.builtin_skills.rglob("SKILL.md")):
            name = skill_file.parent.name
            if not any(s["name"] == name for s in skills):
                skills.append({"name": name, "path": str(skill_file), "source": "builtin"})
    return skills
```

### 改动 3：移动文件

纯文件系统操作，用 `mv` 批量移动。不改任何 SKILL.md 内容。

### 不需要改的

- `config.json` 中的 skills filter 配置 — skill name 不变
- `ContextBuilder` — 只消费 skill name，不关心路径
- `filter_skill_names()` / `resolve_skill_filter()` — 纯 name 匹配
- Builtin skills 目录结构 — 不动

## 改动清单

| 文件 | 改动 |
|------|------|
| `nanobot/agent/skills.py` | 重构 `list_skills()` 为 `_discover_skills()` + 缓存；`load_skill()` 改用路径映射 |
| `tests/agent/test_skill_filtering.py` | 增加子目录扫描测试 |
| `~/workspace/skills/` | 创建分类目录，移动 skill 文件夹 |

预计改动：~30 行代码 + ~20 行测试 + 文件移动

## 测试计划

1. **单元测试：** 在 tmp_path 下创建多层 skill 目录，验证 `list_skills()` 能发现所有 skill
2. **单元测试：** 验证同名 skill 去重（先发现的赢）
3. **单元测试：** 验证 `load_skill()` 能加载子目录下的 skill
4. **单元测试：** 验证缓存生效（第二次调用不重新扫描）
5. **集成测试：** 验证 `build_skills_summary()` 包含子目录 skill
6. **回归测试：** 现有 794+ 测试全部通过

## 风险

- **极低：** `rglob` 在大目录树下性能问题 — workspace/skills 目录规模小（< 100），忽略不计
- **极低：** 同名冲突 — 现有 skill 名字全局唯一，且有去重保护
