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
  5. 移动 sessions/cli_direct.jsonl → cli/cli/sessions/{date}_direct_01.jsonl
  6. 清理空旧目录
"""

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore

DISCORD_API = "https://discord.com/api/v10"


def fetch_channel_map(token: str, guild_id: str) -> dict[str, str]:
    """chat_id → channel_name mapping from Discord API."""
    if httpx is None:
        print("ERROR: httpx not installed. Run: pip install httpx", file=sys.stderr)
        sys.exit(1)
    headers = {"Authorization": f"Bot {token}"}
    resp = httpx.get(f"{DISCORD_API}/guilds/{guild_id}/channels", headers=headers)
    resp.raise_for_status()
    return {str(ch["id"]): ch["name"] for ch in resp.json() if ch.get("name")}


def extract_created_date(jsonl_path: Path) -> str:
    """从 JSONL 第一行 metadata 提取 created_at 日期。"""
    try:
        with open(jsonl_path, encoding="utf-8") as f:
            first = f.readline().strip()
            if first:
                data = json.loads(first)
                if data.get("_type") == "metadata" and data.get("created_at"):
                    return datetime.fromisoformat(data["created_at"]).strftime("%Y-%m-%d")
    except Exception:
        pass
    return datetime.now().strftime("%Y-%m-%d")


def move_file(src: Path, dest: Path, dry_run: bool, workspace: Path) -> None:
    """Move a file with logging."""
    rel_src = src.relative_to(workspace) if src.is_relative_to(workspace) else src
    rel_dest = dest.relative_to(workspace) if dest.is_relative_to(workspace) else dest
    print(f"{'[DRY] ' if dry_run else ''}mv {rel_src} → {rel_dest}")
    if not dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))


def migrate(workspace: Path, channel_map: dict[str, str], dry_run: bool = False) -> None:
    # 1. people/ → discord/people/
    old_people = workspace / "people"
    new_people = workspace / "discord" / "people"
    if old_people.exists() and not new_people.exists():
        print(f"{'[DRY] ' if dry_run else ''}mv people/ → discord/people/")
        if not dry_run:
            new_people.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_people), str(new_people))

    # 2. sessions/discord_{id}.jsonl → discord/{name}/sessions/{date}_{id}_01.jsonl
    sessions_dir = workspace / "sessions"
    if sessions_dir.exists():
        for f in sorted(sessions_dir.glob("discord_*.jsonl")):
            chat_id = f.stem.replace("discord_", "")
            name = channel_map.get(chat_id, chat_id)
            created = extract_created_date(f)
            dest = workspace / "discord" / name / "sessions" / f"{created}_{chat_id}_01.jsonl"
            move_file(f, dest, dry_run, workspace)

    # 3. traces/discord_{id}.jsonl → discord/{name}/llm_logs/{date}_{id}_01.jsonl
    traces_dir = workspace / "traces"
    if traces_dir.exists():
        for f in sorted(traces_dir.glob("discord_*.jsonl")):
            chat_id = f.stem.replace("discord_", "")
            name = channel_map.get(chat_id, chat_id)
            # Try to match date from corresponding session file
            session_dest = workspace / "discord" / name / "sessions"
            created = datetime.now().strftime("%Y-%m-%d")
            if session_dest.exists():
                session_files = sorted(session_dest.glob(f"*_{chat_id}_*.jsonl"))
                if session_files:
                    # Extract date from session filename
                    created = session_files[0].stem.split("_")[0]
            dest = workspace / "discord" / name / "llm_logs" / f"{created}_{chat_id}_01.jsonl"
            move_file(f, dest, dry_run, workspace)

    # 4. CLI session
    if sessions_dir.exists():
        cli_session = sessions_dir / "cli_direct.jsonl"
        if cli_session.exists():
            created = extract_created_date(cli_session)
            dest = workspace / "cli" / "cli" / "sessions" / f"{created}_direct_01.jsonl"
            move_file(cli_session, dest, dry_run, workspace)

        cli_trace = workspace / "traces" / "cli_direct.jsonl"
        if cli_trace.exists():
            dest = workspace / "cli" / "cli" / "llm_logs" / f"{created}_direct_01.jsonl"
            move_file(cli_trace, dest, dry_run, workspace)

    # 5. Clean up empty directories
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
