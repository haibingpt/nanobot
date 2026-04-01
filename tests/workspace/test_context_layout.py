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

    def test_legacy_root_people_still_works(self, tmp_path: Path):
        _setup_workspace(tmp_path)
        # Legacy: workspace/people/alice/SOUL.md
        (tmp_path / "people" / "alice").mkdir(parents=True)
        (tmp_path / "people" / "alice" / "SOUL.md").write_text("Alice legacy soul", encoding="utf-8")

        builder = ContextBuilder(tmp_path)
        # Without layout, should find legacy
        path = builder._resolve_bootstrap_path("SOUL.md", "alice")
        assert "Alice legacy soul" in path.read_text()


class TestAgentMdLayer:
    def test_agent_md_appended_to_bootstrap(self, tmp_path: Path):
        _setup_workspace(tmp_path)
        layout = WorkspaceLayout(workspace=tmp_path, channel="discord", channel_name="kids", chat_id="123")
        layout.scope_dir.mkdir(parents=True)
        layout.agent_md.write_text("# Kids channel rules\nBe gentle.", encoding="utf-8")

        builder = ContextBuilder(tmp_path)
        bootstrap = builder._load_bootstrap_files(sender_name=None, layout=layout)
        assert "Root agents" in bootstrap
        assert "Kids channel rules" in bootstrap

    def test_no_agent_md_no_error(self, tmp_path: Path):
        _setup_workspace(tmp_path)
        layout = WorkspaceLayout(workspace=tmp_path, channel="discord", channel_name="develop", chat_id="123")

        builder = ContextBuilder(tmp_path)
        bootstrap = builder._load_bootstrap_files(sender_name=None, layout=layout)
        assert "Root agents" in bootstrap

    def test_without_layout_no_agent_md(self, tmp_path: Path):
        _setup_workspace(tmp_path)
        builder = ContextBuilder(tmp_path)
        bootstrap = builder._load_bootstrap_files(sender_name=None)
        assert "AGENT.md (channel)" not in bootstrap
