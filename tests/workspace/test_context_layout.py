"""Tests for ContextBuilder with WorkspaceLayout."""

from pathlib import Path

from nanobot.agent.context import ContextBuilder
from nanobot.workspace.layout import WorkspaceLayout


def _setup_workspace(tmp_path: Path) -> Path:
    (tmp_path / "SOUL.md").write_text("Root soul", encoding="utf-8")
    (tmp_path / "USER.md").write_text("Root user", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("Root agents", encoding="utf-8")
    (tmp_path / "TOOLS.md").write_text("Root tools", encoding="utf-8")
    return tmp_path


class TestPeopleOverrideFromLayout:
    def test_per_channel_people_overrides_root(self, tmp_path: Path):
        _setup_workspace(tmp_path)
        layout = WorkspaceLayout(workspace=tmp_path, channel="discord", channel_name="kids", chat_id="123")
        people_dir = layout.people_dir / "petch"
        people_dir.mkdir(parents=True)
        (people_dir / "SOUL.md").write_text("Petch soul override", encoding="utf-8")

        builder = ContextBuilder(tmp_path)
        path = builder._resolve_bootstrap_path("SOUL.md", "petch", layout=layout)
        assert "Petch soul override" in path.read_text()

    def test_fallback_to_root_when_no_override(self, tmp_path: Path):
        _setup_workspace(tmp_path)
        layout = WorkspaceLayout(workspace=tmp_path, channel="discord", channel_name="kids", chat_id="123")

        builder = ContextBuilder(tmp_path)
        path = builder._resolve_bootstrap_path("SOUL.md", "nobody", layout=layout)
        assert "Root soul" in path.read_text()




class TestChannelOverride:
    """Tests for per-channel override (replaces the old AGENT.md layering)."""

    def test_channel_override_takes_priority_over_root(self, tmp_path: Path):
        _setup_workspace(tmp_path)
        layout = WorkspaceLayout(workspace=tmp_path, channel="discord", channel_name="kids", chat_id="123")
        layout.scope_dir.mkdir(parents=True)
        (layout.scope_dir / "AGENTS.md").write_text("Kids channel agents", encoding="utf-8")

        builder = ContextBuilder(tmp_path)
        path = builder._resolve_bootstrap_path("AGENTS.md", sender_name=None, layout=layout)
        assert "Kids channel agents" in path.read_text()
        assert "Root agents" not in path.read_text()

    def test_channel_override_takes_priority_over_people(self, tmp_path: Path):
        """Channel > People: per-channel AGENTS.md beats people/{sender}/AGENTS.md"""
        _setup_workspace(tmp_path)
        layout = WorkspaceLayout(workspace=tmp_path, channel="discord", channel_name="kids", chat_id="123")
        layout.scope_dir.mkdir(parents=True)
        (layout.scope_dir / "AGENTS.md").write_text("Kids channel agents", encoding="utf-8")

        # People override exists but should not be used
        people_dir = layout.people_dir / "petch"
        people_dir.mkdir(parents=True)
        (people_dir / "AGENTS.md").write_text("Petch agents override", encoding="utf-8")

        builder = ContextBuilder(tmp_path)
        path = builder._resolve_bootstrap_path("AGENTS.md", sender_name="petch", layout=layout)
        assert "Kids channel agents" in path.read_text()
        assert "Petch agents override" not in path.read_text()

    def test_people_override_used_when_no_channel_override(self, tmp_path: Path):
        """People > Root: people/{sender}/AGENTS.md beats root AGENTS.md"""
        _setup_workspace(tmp_path)
        layout = WorkspaceLayout(workspace=tmp_path, channel="discord", channel_name="kids", chat_id="123")
        # No channel override

        people_dir = layout.people_dir / "petch"
        people_dir.mkdir(parents=True)
        (people_dir / "AGENTS.md").write_text("Petch agents override", encoding="utf-8")

        builder = ContextBuilder(tmp_path)
        path = builder._resolve_bootstrap_path("AGENTS.md", sender_name="petch", layout=layout)
        assert "Petch agents override" in path.read_text()
        assert "Root agents" not in path.read_text()

    def test_fallback_to_root_when_no_overrides(self, tmp_path: Path):
        _setup_workspace(tmp_path)
        layout = WorkspaceLayout(workspace=tmp_path, channel="discord", channel_name="kids", chat_id="123")

        builder = ContextBuilder(tmp_path)
        path = builder._resolve_bootstrap_path("AGENTS.md", sender_name=None, layout=layout)
        assert "Root agents" in path.read_text()

    def test_bootstrap_loads_resolved_paths(self, tmp_path: Path):
        """_load_bootstrap_files uses _resolve_bootstrap_path for each file."""
        _setup_workspace(tmp_path)
        layout = WorkspaceLayout(workspace=tmp_path, channel="discord", channel_name="kids", chat_id="123")
        layout.scope_dir.mkdir(parents=True)
        (layout.scope_dir / "AGENTS.md").write_text("Kids channel agents", encoding="utf-8")

        builder = ContextBuilder(tmp_path)
        bootstrap = builder._load_bootstrap_files(sender_name=None, layout=layout)
        # Should have channel override, not root
        assert "Kids channel agents" in bootstrap
        assert "Root agents" not in bootstrap
        # But other files should still come from root
        assert "Root soul" in bootstrap
        assert "Root user" in bootstrap
