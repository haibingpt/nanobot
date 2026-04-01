"""Tests for skill filtering by sender/channel."""

from pathlib import Path

import pytest
from nanobot.agent.context import ContextBuilder
from nanobot.agent.skills import SkillsLoader, filter_skill_names, resolve_skill_filter
from nanobot.config.schema import AgentDefaults, SkillsConfig, SkillsFilterConfig


class TestSkillsFilterConfig:
    def test_default_includes_all(self):
        cfg = SkillsFilterConfig()
        assert cfg.include == ["*"]
        assert cfg.exclude == []

    def test_custom_include_exclude(self):
        cfg = SkillsFilterConfig(include=["coding-*"], exclude=["ljg-*"])
        assert cfg.include == ["coding-*"]
        assert cfg.exclude == ["ljg-*"]


class TestSkillsConfig:
    def test_defaults_empty(self):
        cfg = SkillsConfig()
        assert cfg.senders == {}
        assert cfg.channels == {}
        assert cfg.include == ["*"]
        assert cfg.exclude == []

    def test_sender_config(self):
        cfg = SkillsConfig(senders={"petch": SkillsFilterConfig(exclude=["coding-*"])})
        assert "petch" in cfg.senders
        assert cfg.senders["petch"].exclude == ["coding-*"]

    def test_channel_config(self):
        cfg = SkillsConfig(channels={"develop": SkillsFilterConfig(include=["coding-*"])})
        assert cfg.channels["develop"].include == ["coding-*"]


class TestAgentDefaultsSkills:
    def test_has_skills_config(self):
        defaults = AgentDefaults()
        assert isinstance(defaults.skills, SkillsConfig)

    def test_from_dict(self):
        data = {
            "skills": {
                "exclude": ["peppa-*"],
                "senders": {
                    "petch": {"exclude": ["coding-*", "ljg-*"]}
                },
                "channels": {
                    "develop": {"include": ["coding-*"]}
                }
            }
        }
        defaults = AgentDefaults.model_validate(data)
        assert defaults.skills.exclude == ["peppa-*"]
        assert "petch" in defaults.skills.senders


class TestFilterSkillNames:
    def test_include_all_no_exclude(self):
        names = ["coding", "weather", "ljg-card"]
        assert filter_skill_names(names, ["*"], []) == ["coding", "weather", "ljg-card"]

    def test_exclude_glob(self):
        names = ["coding", "weather", "ljg-card", "ljg-learn"]
        result = filter_skill_names(names, ["*"], ["ljg-*"])
        assert result == ["coding", "weather"]

    def test_include_glob(self):
        names = ["coding", "coding-agent", "weather", "ljg-card"]
        result = filter_skill_names(names, ["coding*"], [])
        assert result == ["coding", "coding-agent"]

    def test_include_and_exclude(self):
        names = ["coding", "coding-agent", "weather"]
        result = filter_skill_names(names, ["*"], ["weather"])
        assert result == ["coding", "coding-agent"]

    def test_exact_match(self):
        names = ["coding", "weather"]
        result = filter_skill_names(names, ["coding"], [])
        assert result == ["coding"]

    def test_empty_input(self):
        assert filter_skill_names([], ["*"], ["ljg-*"]) == []

    def test_exclude_overrides_include(self):
        names = ["coding", "coding-agent"]
        result = filter_skill_names(names, ["coding*"], ["coding-agent"])
        assert result == ["coding"]


class TestResolveSkillFilter:
    def test_default_only(self):
        cfg = SkillsConfig(exclude=["peppa-*"])
        inc, exc = resolve_skill_filter(cfg)
        assert inc == ["*"]
        assert exc == ["peppa-*"]

    def test_sender_merges_with_default(self):
        cfg = SkillsConfig(
            exclude=["peppa-*"],
            senders={"petch": SkillsFilterConfig(exclude=["coding-*", "ljg-*"])}
        )
        inc, exc = resolve_skill_filter(cfg, sender_name="petch")
        assert inc == ["*"]
        assert set(exc) == {"peppa-*", "coding-*", "ljg-*"}

    def test_sender_include_replaces_default(self):
        cfg = SkillsConfig(
            senders={"petch": SkillsFilterConfig(include=["weather", "peppa-*"])}
        )
        inc, exc = resolve_skill_filter(cfg, sender_name="petch")
        assert inc == ["weather", "peppa-*"]

    def test_channel_overrides_everything(self):
        cfg = SkillsConfig(
            exclude=["peppa-*"],
            senders={"haibin": SkillsFilterConfig(exclude=["ljg-*"])},
            channels={"develop": SkillsFilterConfig(include=["coding-*"])}
        )
        inc, exc = resolve_skill_filter(cfg, sender_name="haibin", channel_name="develop")
        assert inc == ["coding-*"]
        assert exc == []

    def test_unknown_sender_uses_default(self):
        cfg = SkillsConfig(
            exclude=["peppa-*"],
            senders={"petch": SkillsFilterConfig(exclude=["coding-*"])}
        )
        inc, exc = resolve_skill_filter(cfg, sender_name="stranger")
        assert inc == ["*"]
        assert exc == ["peppa-*"]

    def test_unknown_channel_falls_to_sender(self):
        cfg = SkillsConfig(
            senders={"haibin": SkillsFilterConfig(exclude=["peppa-*"])},
            channels={"develop": SkillsFilterConfig(include=["coding-*"])}
        )
        inc, exc = resolve_skill_filter(cfg, sender_name="haibin", channel_name="general")
        assert inc == ["*"]
        assert exc == ["peppa-*"]

    def test_no_sender_no_channel(self):
        cfg = SkillsConfig()
        inc, exc = resolve_skill_filter(cfg)
        assert inc == ["*"]
        assert exc == []

    def test_case_insensitive_sender(self):
        cfg = SkillsConfig(
            senders={"petch": SkillsFilterConfig(exclude=["coding-*"])}
        )
        inc, exc = resolve_skill_filter(cfg, sender_name="Petch")
        assert "coding-*" in exc

    def test_channel_glob_pattern(self):
        cfg = SkillsConfig(
            channels={"*nanobot*": SkillsFilterConfig(include=["coding*"])}
        )
        inc, exc = resolve_skill_filter(cfg, channel_name="nanobot skill")
        assert inc == ["coding*"]

    def test_channel_glob_no_match(self):
        cfg = SkillsConfig(
            channels={"*nanobot*": SkillsFilterConfig(include=["coding*"])}
        )
        inc, exc = resolve_skill_filter(cfg, channel_name="general")
        assert inc == ["*"]  # falls back to default

    def test_channel_first_match_wins(self):
        cfg = SkillsConfig(
            channels={
                "develop*": SkillsFilterConfig(include=["coding*"]),
                "*nanobot*": SkillsFilterConfig(include=["weather"]),
            }
        )
        # "develop nanobot" matches both, first wins
        inc, exc = resolve_skill_filter(cfg, channel_name="develop nanobot")
        assert inc == ["coding*"]

    def test_sender_glob_pattern(self):
        cfg = SkillsConfig(
            senders={"pet*": SkillsFilterConfig(exclude=["coding-*"])}
        )
        inc, exc = resolve_skill_filter(cfg, sender_name="petch")
        assert "coding-*" in exc

    def test_channel_pipe_separator(self):
        cfg = SkillsConfig(
            channels={"*paper*|*book*|*article*": SkillsFilterConfig(include=["ljg-*"])}
        )
        inc, _ = resolve_skill_filter(cfg, channel_name="xray-paper")
        assert inc == ["ljg-*"]
        inc, _ = resolve_skill_filter(cfg, channel_name="read-book")
        assert inc == ["ljg-*"]
        inc, _ = resolve_skill_filter(cfg, channel_name="article-review")
        assert inc == ["ljg-*"]
        inc, _ = resolve_skill_filter(cfg, channel_name="general")
        assert inc == ["*"]  # no match, falls to default

    def test_sender_pipe_separator(self):
        cfg = SkillsConfig(
            senders={"petch|george": SkillsFilterConfig(exclude=["coding-*"])}
        )
        _, exc = resolve_skill_filter(cfg, sender_name="petch")
        assert "coding-*" in exc
        _, exc = resolve_skill_filter(cfg, sender_name="george")
        assert "coding-*" in exc
        _, exc = resolve_skill_filter(cfg, sender_name="haibin")
        assert "coding-*" not in exc


class TestSkillFilteringIntegration:
    """Integration: ContextBuilder respects skills filtering."""

    def _make_workspace(self, tmp_path: Path, skill_names: list[str]) -> Path:
        ws = tmp_path / "workspace"
        ws.mkdir()
        skills_dir = ws / "skills"
        skills_dir.mkdir()
        for name in skill_names:
            skill_dir = skills_dir / name
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: {name} skill\n---\n# {name}\nDo {name} stuff.\n"
            )
        (ws / "memory").mkdir()
        return ws

    def test_no_config_includes_all(self, tmp_path):
        ws = self._make_workspace(tmp_path, ["coding", "weather", "ljg-card"])
        ctx = ContextBuilder(ws)
        prompt = ctx.build_system_prompt()
        assert "coding" in prompt
        assert "weather" in prompt
        assert "ljg-card" in prompt

    def test_exclude_filters_skills(self, tmp_path):
        ws = self._make_workspace(tmp_path, ["coding", "weather", "ljg-card", "ljg-learn"])
        cfg = SkillsConfig(exclude=["ljg-*"])
        ctx = ContextBuilder(ws, skills_config=cfg)
        prompt = ctx.build_system_prompt()
        assert "coding" in prompt
        assert "weather" in prompt
        assert "ljg-card" not in prompt
        assert "ljg-learn" not in prompt

    def test_sender_exclude(self, tmp_path):
        ws = self._make_workspace(tmp_path, ["coding", "weather", "ljg-card"])
        cfg = SkillsConfig(
            senders={"petch": SkillsFilterConfig(exclude=["coding*", "ljg-*"])}
        )
        ctx = ContextBuilder(ws, skills_config=cfg)
        prompt = ctx.build_system_prompt(sender_name="petch")
        assert "weather" in prompt
        assert "ljg-card" not in prompt

    def test_channel_override(self, tmp_path):
        ws = self._make_workspace(tmp_path, ["coding", "weather", "ljg-card"])
        cfg = SkillsConfig(
            channels={"develop": SkillsFilterConfig(include=["coding*"])}
        )
        ctx = ContextBuilder(ws, skills_config=cfg)
        prompt = ctx.build_system_prompt(sender_name="haibin", channel_name="develop")
        assert "coding" in prompt
        assert "weather" not in prompt
        assert "ljg-card" not in prompt

    def test_sender_with_no_matching_channel_uses_sender(self, tmp_path):
        ws = self._make_workspace(tmp_path, ["coding", "weather", "ljg-card"])
        cfg = SkillsConfig(
            senders={"haibin": SkillsFilterConfig(exclude=["ljg-*"])},
            channels={"develop": SkillsFilterConfig(include=["coding*"])}
        )
        ctx = ContextBuilder(ws, skills_config=cfg)
        prompt = ctx.build_system_prompt(sender_name="haibin", channel_name="general")
        assert "coding" in prompt
        assert "weather" in prompt
        assert "ljg-card" not in prompt


class TestSkillsLoaderRecursive:
    """Test recursive skill discovery in subdirectories."""

    def _make_skill(self, path: Path, name: str, desc: str = ""):
        skill_dir = path / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {desc or name}\n---\n# {name}\n"
        )

    def _loader(self, ws: Path) -> SkillsLoader:
        """Create a loader with no builtin skills (isolated test)."""
        empty = ws / "_no_builtin"
        empty.mkdir(exist_ok=True)
        return SkillsLoader(ws, builtin_skills_dir=empty)

    def test_flat_skills(self, tmp_path):
        ws = tmp_path / "workspace"
        skills = ws / "skills"
        skills.mkdir(parents=True)
        self._make_skill(skills, "coding")
        self._make_skill(skills, "weather")

        loader = self._loader(ws)
        names = [s["name"] for s in loader.list_skills(filter_unavailable=False)]
        assert "coding" in names
        assert "weather" in names

    def test_nested_skills(self, tmp_path):
        ws = tmp_path / "workspace"
        skills = ws / "skills"
        self._make_skill(skills / "ljg", "ljg-card")
        self._make_skill(skills / "ljg", "ljg-learn")
        self._make_skill(skills / "coding", "coding")
        self._make_skill(skills, "weather")  # flat, still works

        loader = self._loader(ws)
        names = [s["name"] for s in loader.list_skills(filter_unavailable=False)]
        assert set(names) == {"ljg-card", "ljg-learn", "coding", "weather"}

    def test_load_skill_from_subdirectory(self, tmp_path):
        ws = tmp_path / "workspace"
        skills = ws / "skills"
        self._make_skill(skills / "petch", "peppa-why", "answer kids questions")

        loader = self._loader(ws)
        content = loader.load_skill("peppa-why")
        assert content is not None
        assert "peppa-why" in content

    def test_duplicate_name_first_wins(self, tmp_path):
        ws = tmp_path / "workspace"
        skills = ws / "skills"
        # "aaa" sorts before "zzz", so aaa/dupe wins
        dupe_a = skills / "aaa" / "dupe"
        dupe_a.mkdir(parents=True)
        (dupe_a / "SKILL.md").write_text("---\nname: dupe\n---\nFirst\n")
        dupe_z = skills / "zzz" / "dupe"
        dupe_z.mkdir(parents=True)
        (dupe_z / "SKILL.md").write_text("---\nname: dupe\n---\nSecond\n")

        loader = self._loader(ws)
        all_skills = loader.list_skills(filter_unavailable=False)
        dupe_skills = [s for s in all_skills if s["name"] == "dupe"]
        assert len(dupe_skills) == 1
        assert "aaa" in dupe_skills[0]["path"]

    def test_cache_reuse(self, tmp_path):
        ws = tmp_path / "workspace"
        skills = ws / "skills"
        self._make_skill(skills, "coding")

        loader = self._loader(ws)
        first = loader.list_skills(filter_unavailable=False)
        # Add a new skill after first scan
        self._make_skill(skills, "weather")
        second = loader.list_skills(filter_unavailable=False)
        # Cache means second call doesn't see the new skill
        assert len(first) == len(second)

    def test_nested_with_filtering(self, tmp_path):
        """Integration: subdirectory skills work with skill filtering."""
        ws = tmp_path / "workspace"
        skills = ws / "skills"
        (ws / "memory").mkdir(parents=True)
        self._make_skill(skills / "ljg", "ljg-card")
        self._make_skill(skills / "ljg", "ljg-learn")
        self._make_skill(skills / "coding", "coding")
        self._make_skill(skills, "weather")

        cfg = SkillsConfig(exclude=["ljg-*"])
        ctx = ContextBuilder(ws, skills_config=cfg)
        prompt = ctx.build_system_prompt()
        assert "coding" in prompt
        assert "weather" in prompt
        assert "ljg-card" not in prompt
        assert "ljg-learn" not in prompt
