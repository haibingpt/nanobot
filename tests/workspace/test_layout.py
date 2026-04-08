import pytest
from pathlib import Path

from nanobot.workspace.layout import WorkspaceLayout, make_layout


class TestWorkspaceLayout:
    def test_channel_dir(self, tmp_path: Path):
        layout = WorkspaceLayout(workspace=tmp_path, channel="discord", channel_name="develop", chat_id="147xxx")
        assert layout.channel_dir == tmp_path / "discord"

    def test_scope_dir(self, tmp_path: Path):
        layout = WorkspaceLayout(workspace=tmp_path, channel="discord", channel_name="develop", chat_id="147xxx")
        assert layout.scope_dir == tmp_path / "discord" / "develop"

    def test_sessions_dir(self, tmp_path: Path):
        layout = WorkspaceLayout(workspace=tmp_path, channel="discord", channel_name="develop", chat_id="147xxx")
        assert layout.sessions_dir == tmp_path / "discord" / "develop" / "sessions"

    def test_llm_logs_dir(self, tmp_path: Path):
        layout = WorkspaceLayout(workspace=tmp_path, channel="discord", channel_name="develop", chat_id="147xxx")
        assert layout.llm_logs_dir == tmp_path / "discord" / "develop" / "llm_logs"

    def test_people_dir(self, tmp_path: Path):
        layout = WorkspaceLayout(workspace=tmp_path, channel="discord", channel_name="develop", chat_id="147xxx")
        assert layout.people_dir == tmp_path / "discord" / "people"

    def test_agents_md(self, tmp_path: Path):
        layout = WorkspaceLayout(workspace=tmp_path, channel="discord", channel_name="develop", chat_id="147xxx")
        assert layout.agents_md == tmp_path / "discord" / "develop" / "AGENTS.md"

    def test_session_path(self, tmp_path: Path):
        layout = WorkspaceLayout(workspace=tmp_path, channel="discord", channel_name="develop", chat_id="147xxx")
        assert layout.session_path("2026-04-01", 1) == (
            tmp_path / "discord" / "develop" / "sessions" / "2026-04-01_147xxx_01.jsonl"
        )

    def test_llm_log_path(self, tmp_path: Path):
        layout = WorkspaceLayout(workspace=tmp_path, channel="discord", channel_name="develop", chat_id="147xxx")
        assert layout.llm_log_path("2026-04-01", 1) == (
            tmp_path / "discord" / "develop" / "llm_logs" / "2026-04-01_147xxx_01.jsonl"
        )

    def test_next_session_seq_empty_dir(self, tmp_path: Path):
        layout = WorkspaceLayout(workspace=tmp_path, channel="discord", channel_name="develop", chat_id="147xxx")
        assert layout.next_session_seq("2026-04-01") == 1

    def test_next_session_seq_existing_files(self, tmp_path: Path):
        layout = WorkspaceLayout(workspace=tmp_path, channel="discord", channel_name="develop", chat_id="147xxx")
        layout.ensure_dirs()
        (layout.sessions_dir / "2026-04-01_147xxx_01.jsonl").touch()
        (layout.sessions_dir / "2026-04-01_147xxx_02.jsonl").touch()
        assert layout.next_session_seq("2026-04-01") == 3

    def test_next_session_seq_different_date_ignored(self, tmp_path: Path):
        layout = WorkspaceLayout(workspace=tmp_path, channel="discord", channel_name="develop", chat_id="147xxx")
        layout.ensure_dirs()
        (layout.sessions_dir / "2026-03-31_147xxx_01.jsonl").touch()
        assert layout.next_session_seq("2026-04-01") == 1

    def test_next_session_seq_different_chat_id_ignored(self, tmp_path: Path):
        layout = WorkspaceLayout(workspace=tmp_path, channel="discord", channel_name="develop", chat_id="147xxx")
        layout.ensure_dirs()
        (layout.sessions_dir / "2026-04-01_999yyy_01.jsonl").touch()
        assert layout.next_session_seq("2026-04-01") == 1

    def test_ensure_dirs_creates_both(self, tmp_path: Path):
        layout = WorkspaceLayout(workspace=tmp_path, channel="discord", channel_name="develop", chat_id="147xxx")
        layout.ensure_dirs()
        assert layout.sessions_dir.is_dir()
        assert layout.llm_logs_dir.is_dir()

    def test_frozen_immutable(self, tmp_path: Path):
        layout = WorkspaceLayout(workspace=tmp_path, channel="discord", channel_name="develop", chat_id="147xxx")
        with pytest.raises(AttributeError):
            layout.channel = "telegram"

    def test_current_session_path_no_files(self, tmp_path: Path):
        layout = WorkspaceLayout(workspace=tmp_path, channel="discord", channel_name="develop", chat_id="147xxx")
        assert layout.current_session_path("2026-04-01") is None

    def test_current_session_path_picks_highest_seq(self, tmp_path: Path):
        layout = WorkspaceLayout(workspace=tmp_path, channel="discord", channel_name="develop", chat_id="147xxx")
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

    def test_scope_id_in_dir_name(self, tmp_path: Path):
        layout = make_layout(tmp_path, "discord", "develop", "147xxx", scope_id="999aaa")
        assert layout.scope_dir == tmp_path / "discord" / "999aaa_develop"

    def test_no_scope_id_plain_dir_name(self, tmp_path: Path):
        layout = make_layout(tmp_path, "discord", "develop", "147xxx")
        assert layout.scope_dir == tmp_path / "discord" / "develop"

    def test_scope_id_same_as_channel_name_no_duplicate(self, tmp_path: Path):
        layout = make_layout(tmp_path, "discord", "cli", "direct", scope_id="cli")
        assert layout.scope_dir == tmp_path / "discord" / "cli"

    def test_different_guilds_same_channel_name(self, tmp_path: Path):
        l1 = make_layout(tmp_path, "discord", "general", "111", scope_id="guild1_ch1")
        l2 = make_layout(tmp_path, "discord", "general", "222", scope_id="guild2_ch1")
        assert l1.scope_dir != l2.scope_dir
