"""Built-in slash command handlers."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import TYPE_CHECKING

from nanobot import __version__
from nanobot.command.router import CommandContext, CommandRouter

if TYPE_CHECKING:
    from nanobot.bus.events import OutboundMessage
from nanobot.utils.helpers import build_status_content


async def cmd_stop(ctx: CommandContext) -> OutboundMessage:
    """Cancel all active tasks and subagents for the session."""
    loop = ctx.loop
    msg = ctx.msg
    tasks = loop._active_tasks.pop(msg.session_key, [])
    cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
    for t in tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    sub_cancelled = await loop.subagents.cancel_by_session(msg.session_key)
    total = cancelled + sub_cancelled
    content = f"Stopped {total} task(s)." if total else "No active task to stop."
    return ctx.make_response(content)


async def cmd_restart(ctx: CommandContext) -> OutboundMessage:
    """Restart the process in-place via os.execv."""
    msg = ctx.msg

    async def _do_restart():
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable, "-m", "nanobot"] + sys.argv[1:])

    asyncio.create_task(_do_restart())
    return ctx.make_response("Restarting...")


async def cmd_status(ctx: CommandContext) -> OutboundMessage:
    """Build an outbound status message for a session."""
    loop = ctx.loop
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    ctx_est = 0
    try:
        ctx_est, _ = loop.memory_consolidator.estimate_session_prompt_tokens(session)
    except Exception:
        pass
    if ctx_est <= 0:
        ctx_est = loop._last_usage.get("prompt_tokens", 0)
    return ctx.make_response(
        build_status_content(
            version=__version__, model=loop.model,
            start_time=loop._start_time, last_usage=loop._last_usage,
            context_window_tokens=loop.context_window_tokens,
            session_msg_count=len(session.get_history(max_messages=0)),
            context_tokens_estimate=ctx_est,
        ),
        metadata={"render_as": "text"},
    )


async def cmd_new(ctx: CommandContext) -> OutboundMessage:
    """Start a fresh session."""
    loop = ctx.loop
    session = ctx.session or (
        loop.sessions.get_or_create_from_layout(ctx.layout) if ctx.layout
        else loop.sessions.get_or_create(ctx.key)
    )
    snapshot = session.messages[session.last_consolidated:]
    if ctx.layout:
        # New layout path: preserve old file, create next sequence
        loop.sessions.new_session(ctx.layout)
    else:
        # Legacy path: clear in-place
        session.clear()
        loop.sessions.save(session)
        loop.sessions.invalidate(session.key)
    if snapshot:
        loop._schedule_background(loop.memory_consolidator.archive(snapshot))
    return ctx.make_response("New session started.")


async def cmd_tts(ctx: CommandContext) -> OutboundMessage:
    """Toggle TTS for the current session."""
    args = ctx.args.strip().lower()
    session = ctx.session or ctx.loop.sessions.get_or_create(ctx.key)

    if args == "on":
        session.metadata["tts"] = True
        ctx.loop.sessions.save(session)
        content = "🔊 TTS enabled for this session."
    elif args == "off":
        session.metadata.pop("tts", None)
        ctx.loop.sessions.save(session)
        content = "🔇 TTS disabled for this session."
    else:
        current = session.metadata.get("tts", False)
        content = f"🔊 TTS is **{'on' if current else 'off'}**. Use `/tts on` or `/tts off` to toggle."

    return ctx.make_response(content)


async def cmd_model(ctx: CommandContext) -> OutboundMessage:
    """切换或查看当前 LLM 模型。"""
    loop = ctx.loop
    args = (ctx.args or "").strip()

    # /model（无参数）→ 显示当前状态
    if not args:
        is_override = loop.model != loop._config_model
        lines = [
            f"🧠 Current model: `{loop.model}`",
            f"📋 Config default: `{loop._config_model}`",
        ]
        if is_override:
            lines.append("⚡ Status: **overridden** (use `reset` to restore)")
        else:
            lines.append("✅ Status: using config default")

        # fallback chain 状态
        from nanobot.providers.fallback import FallbackProvider
        if isinstance(loop._config_provider, FallbackProvider):
            fb_models = [m for _, m in loop._config_provider.fallbacks]
            lines.append(f"🔄 Fallback chain: {' → '.join(fb_models)}")
            if loop._config_provider._in_cooldown():
                lines.append("⚠️ Primary in cooldown — fallback active")

        return ctx.make_response("\n".join(lines))

    # /model reset → 恢复默认
    if args.lower() == "reset":
        model = loop.reset_model()
        return ctx.make_response(f"↩️ Model reset to default: `{model}`")

    # /model <model_name> → 切换
    try:
        model = loop.switch_model(args)
        return ctx.make_response(f"✅ Model switched to: `{model}`")
    except Exception as e:
        return ctx.make_response(f"❌ Failed to switch model: {e}")


async def cmd_dream(ctx: CommandContext) -> OutboundMessage:
    """Manually trigger a Dream consolidation run."""
    import time

    loop = ctx.loop
    msg = ctx.msg

    if not hasattr(loop, "dream") or loop.dream is None:
        return ctx.make_response("❌ Dream not available.")

    async def _run_dream():
        from nanobot.bus.events import OutboundMessage as _Out
        t0 = time.monotonic()
        try:
            did_work = await loop.dream.run()
            elapsed = time.monotonic() - t0
            if did_work:
                content = f"🌙 Dream completed in {elapsed:.1f}s."
            else:
                content = "🌙 Dream: nothing to process."
        except Exception as e:
            elapsed = time.monotonic() - t0
            content = f"❌ Dream failed after {elapsed:.1f}s: {e}"
        await loop.bus.publish_outbound(_Out(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
        ))

    asyncio.create_task(_run_dream())
    return ctx.make_response("🌙 Dreaming...")


def _extract_changed_files(diff: str) -> list[str]:
    """Extract changed file paths from a unified diff."""
    files: list[str] = []
    seen: set[str] = set()
    for line in diff.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        path = parts[3]
        if path.startswith("b/"):
            path = path[2:]
        if path in seen:
            continue
        seen.add(path)
        files.append(path)
    return files


def _format_changed_files(diff: str) -> str:
    files = _extract_changed_files(diff)
    if not files:
        return "No tracked memory files changed."
    return ", ".join(f"`{path}`" for path in files)


def _format_dream_log_content(commit, diff: str, *, requested_sha: str | None = None) -> str:
    files_line = _format_changed_files(diff)
    lines = [
        "## Dream Update",
        "",
        "Here is the selected Dream memory change." if requested_sha else "Here is the latest Dream memory change.",
        "",
        f"- Commit: `{commit.sha}`",
        f"- Time: {commit.timestamp}",
        f"- Changed files: {files_line}",
    ]
    if diff:
        lines.extend([
            "",
            f"Use `/dream-restore {commit.sha}` to undo this change.",
            "",
            "```diff",
            diff.rstrip(),
            "```",
        ])
    else:
        lines.extend([
            "",
            "Dream recorded this version, but there is no file diff to display.",
        ])
    return "\n".join(lines)


def _format_dream_restore_list(commits: list) -> str:
    lines = [
        "## Dream Restore",
        "",
        "Choose a Dream memory version to restore. Latest first:",
        "",
    ]
    for c in commits:
        lines.append(f"- `{c.sha}` {c.timestamp} - {c.message.splitlines()[0]}")
    lines.extend([
        "",
        "Preview a version with `/dream-log <sha>` before restoring it.",
        "Restore a version with `/dream-restore <sha>`.",
    ])
    return "\n".join(lines)


async def cmd_dream_log(ctx: CommandContext) -> OutboundMessage:
    """Show what the last Dream changed.

    Default: diff of the latest commit (HEAD~1 vs HEAD).
    With /dream-log <sha>: diff of that specific commit.
    """
    store = ctx.loop.memory_consolidator.store
    git = store.git

    if not git.is_initialized():
        if store.get_last_dream_cursor() == 0:
            msg = "Dream has not run yet. Run `/dream`, or wait for the next scheduled Dream cycle."
        else:
            msg = "Dream history is not available because memory versioning is not initialized."
        return ctx.make_response(msg, metadata={"render_as": "text"})

    args = ctx.args.strip()

    if args:
        sha = args.split()[0]
        result = git.show_commit_diff(sha)
        if not result:
            content = (
                f"Couldn't find Dream change `{sha}`.\n\n"
                "Use `/dream-restore` to list recent versions, "
                "or `/dream-log` to inspect the latest one."
            )
        else:
            commit, diff = result
            content = _format_dream_log_content(commit, diff, requested_sha=sha)
    else:
        commits = git.log(max_entries=1)
        result = git.show_commit_diff(commits[0].sha) if commits else None
        if result:
            commit, diff = result
            content = _format_dream_log_content(commit, diff)
        else:
            content = "Dream memory has no saved versions yet."

    return ctx.make_response(content, metadata={"render_as": "text"})


async def cmd_dream_restore(ctx: CommandContext) -> OutboundMessage:
    """Restore memory files from a previous dream commit.

    Usage:
        /dream-restore          — list recent commits
        /dream-restore <sha>    — revert a specific commit
    """
    store = ctx.loop.memory_consolidator.store
    git = store.git
    if not git.is_initialized():
        return ctx.make_response(
            "Dream history is not available because memory versioning is not initialized.",
        )

    args = ctx.args.strip()
    if not args:
        commits = git.log(max_entries=10)
        if not commits:
            content = "Dream memory has no saved versions to restore yet."
        else:
            content = _format_dream_restore_list(commits)
    else:
        sha = args.split()[0]
        result = git.show_commit_diff(sha)
        changed_files = _format_changed_files(result[1]) if result else "the tracked memory files"
        new_sha = git.revert(sha)
        if new_sha:
            content = (
                f"Restored Dream memory to the state before `{sha}`.\n\n"
                f"- New safety commit: `{new_sha}`\n"
                f"- Restored files: {changed_files}\n\n"
                f"Use `/dream-log {new_sha}` to inspect the restore diff."
            )
        else:
            content = (
                f"Couldn't restore Dream change `{sha}`.\n\n"
                "It may not exist, or it may be the first saved version with no earlier state to restore."
            )
    return ctx.make_response(content, metadata={"render_as": "text"})


async def cmd_help(ctx: CommandContext) -> OutboundMessage:
    """Return available slash commands."""
    content = build_help_text()
    return ctx.make_response(content, metadata={"render_as": "text"})


def build_help_text() -> str:
    """Build canonical help text shared across channels."""
    lines = [
        "🐈 nanobot commands:",
        "/new — Start a new conversation",
        "/stop — Stop the current task",
        "/restart — Restart the bot",
        "/status — Show bot status",
        "/dream — Manually trigger Dream consolidation",
        "/dream-log — Show what the last Dream changed",
        "/dream-restore — Revert memory to a previous state",
        "/tts — Toggle text-to-speech (on/off)",
        "/model — View or switch the LLM model",
        "/help — Show available commands",
    ]
    return "\n".join(lines)


def register_builtin_commands(router: CommandRouter) -> None:
    """Register the default set of slash commands."""
    router.priority("/stop", cmd_stop)
    router.priority("/restart", cmd_restart)
    router.priority("/status", cmd_status)
    router.exact("/new", cmd_new)
    router.exact("/status", cmd_status)
    router.exact("/dream", cmd_dream)
    router.exact("/dream-log", cmd_dream_log)
    router.prefix("/dream-log ", cmd_dream_log)
    router.exact("/dream-restore", cmd_dream_restore)
    router.prefix("/dream-restore ", cmd_dream_restore)
    router.prefix("/tts ", cmd_tts)
    router.exact("/tts", cmd_tts)
    router.prefix("/model ", cmd_model)
    router.exact("/model", cmd_model)
    router.exact("/help", cmd_help)
