"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import platform
from pathlib import Path
from typing import Any

from nanobot.utils.helpers import current_time_str

from nanobot.agent.memory import MemoryStore
from nanobot.utils.prompt_templates import render_template
from nanobot.agent.skills import SkillsLoader
from nanobot.utils.helpers import build_assistant_message, detect_image_mime
from nanobot.workspace.layout import WorkspaceLayout


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md"]
    _RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"

    def __init__(self, workspace: Path, timezone: str | None = None, skills_config=None):
        self.workspace = workspace
        self.timezone = timezone
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace)
        self.skills_config = skills_config  # SkillsConfig | None

    def build_system_prompt(
        self, skill_names: list[str] | None = None,
        sender_name: str | None = None,
        channel_name: str | None = None,
        layout: WorkspaceLayout | None = None,
    ) -> str:
        """Build the system prompt from identity, bootstrap files, memory, and skills."""
        parts = [self._get_identity()]

        bootstrap = self._load_bootstrap_files(sender_name=sender_name, layout=layout)
        if bootstrap:
            parts.append(bootstrap)

        memory = self.memory.get_memory_context()
        if memory:
            parts.append(f"# Memory\n\n{memory}")

        # Apply skill filtering if config provided
        allowed_skills = set(skill_names) if skill_names else None
        if self.skills_config and channel_name:
            from nanobot.agent.skills import filter_skill_names, resolve_skill_filter
            include, exclude = resolve_skill_filter(self.skills_config, sender_name, channel_name)
            all_skills = [s["name"] for s in self.skills.list_skills(filter_unavailable=True)]
            allowed_skills = set(filter_skill_names(all_skills, include, exclude))

        always_skills = self.skills.get_always_skills(allowed_skills)
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        if skill_names:
            skill_content = self.skills.load_skills_for_context(skill_names)
            if skill_content:
                parts.append(f"# Loaded Skills\n\n{skill_content}")

        skills_summary = self.skills.build_skills_summary()
        if skills_summary:
            parts.append(render_template("agent/skills_section.md", skills_summary=skills_summary))

        # SOUL anchor at the end for identity reinforcement
        soul = self._load_soul_anchor(sender_name=sender_name, layout=layout)
        if soul:
            parts.append(f"# Identity Anchor\n\n{soul}")

        return "\n\n---\n\n".join(parts)

    def _get_identity(self) -> str:
        """Get the core identity section."""
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        return render_template(
            "agent/identity.md",
            workspace_path=workspace_path,
            runtime=runtime,
            platform_policy=render_template("agent/platform_policy.md", system=system),
        )

    @staticmethod
    def _build_runtime_context(
        channel: str | None, chat_id: str | None, timezone: str | None = None,
        channel_name: str | None = None, sender_name: str | None = None,
    ) -> str:
        """Build untrusted runtime metadata block for injection before the user message."""
        lines = [f"Current Time: {current_time_str(timezone)}"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
        if channel_name:
            lines.append(f"Channel Name: {channel_name}")
        if sender_name:
            lines.append(f"Sender: {sender_name}")
        return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines)

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n\n{right}" if left else right

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [item if isinstance(item, dict) else {"type": "text", "text": str(item)} for item in value]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    def _resolve_bootstrap_path(
        self, filename: str, sender_name: str | None,
        layout: WorkspaceLayout | None = None,
    ) -> Path:
        """Resolve bootstrap file: per-channel people override > root people override > root file."""
        if sender_name:
            # New: per-channel people directory
            if layout:
                override = layout.people_dir / sender_name.lower() / filename
                if override.exists():
                    return override
            # Legacy: root people directory
            override = self.workspace / "people" / sender_name.lower() / filename
            if override.exists():
                return override
        return self.workspace / filename

    def _load_soul_anchor(
        self, sender_name: str | None = None,
        layout: WorkspaceLayout | None = None,
    ) -> str | None:
        """Load SOUL.md for end-of-prompt identity reinforcement."""
        soul_path = self._resolve_bootstrap_path("SOUL.md", sender_name, layout=layout)
        if soul_path.exists():
            return soul_path.read_text(encoding="utf-8").strip()
        return None

    def _load_bootstrap_files(
        self, sender_name: str | None = None,
        layout: WorkspaceLayout | None = None,
    ) -> str:
        """Load all bootstrap files from workspace.

        Per-sender overrides: if people/{sender}/{file}.md exists, it
        replaces the root-level file for that sender.
        Per-channel AGENT.md is appended as a layer if present.
        """
        parts = []

        for filename in self.BOOTSTRAP_FILES:
            file_path = self._resolve_bootstrap_path(filename, sender_name, layout=layout)
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        current_role: str = "user",
        channel_name: str | None = None,
        sender_name: str | None = None,
        layout: WorkspaceLayout | None = None,
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call."""
        runtime_ctx = self._build_runtime_context(channel, chat_id, self.timezone, channel_name, sender_name)
        user_content = self._build_user_content(current_message, media)

        # Merge runtime context and user content into a single user message
        # to avoid consecutive same-role messages that some providers reject.
        if isinstance(user_content, str):
            merged = f"{runtime_ctx}\n\n{user_content}"
        else:
            merged = [{"type": "text", "text": runtime_ctx}] + user_content
        messages = [
            {"role": "system", "content": self.build_system_prompt(skill_names, sender_name, channel_name, layout)},
            *history,
        ]
        if messages[-1].get("role") == current_role:
            last = dict(messages[-1])
            last["content"] = self._merge_message_content(last.get("content"), merged)
            messages[-1] = last
            return messages
        messages.append({"role": current_role, "content": merged})
        return messages

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        images = []
        for path in media:
            p = Path(path)
            if not p.is_file():
                continue
            raw = p.read_bytes()
            # Detect real MIME type from magic bytes; fallback to filename guess
            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(raw).decode()
            images.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
                "_meta": {"path": str(p)},
            })

        if not images:
            return text
        return images + [{"type": "text", "text": text}]

    def add_tool_result(
        self, messages: list[dict[str, Any]],
        tool_call_id: str, tool_name: str, result: Any,
    ) -> list[dict[str, Any]]:
        """Add a tool result to the message list."""
        messages.append({"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": result})
        return messages

    def add_assistant_message(
        self, messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
        thinking_blocks: list[dict] | None = None,
    ) -> list[dict[str, Any]]:
        """Add an assistant message to the message list."""
        messages.append(build_assistant_message(
            content,
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
            thinking_blocks=thinking_blocks,
        ))
        return messages
