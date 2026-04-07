"""Session management for conversation history."""

import json
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.config.paths import get_legacy_sessions_dir
from nanobot.utils.helpers import ensure_dir, find_legal_message_start, safe_filename
from nanobot.workspace.layout import WorkspaceLayout


@dataclass
class Session:
    """A conversation session."""

    key: str  # channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0  # Number of messages already consolidated to files

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()

    def get_history(self, max_messages: int = 500) -> list[dict[str, Any]]:
        """Return unconsolidated messages for LLM input, aligned to a legal tool-call boundary."""
        unconsolidated = self.messages[self.last_consolidated:]
        sliced = unconsolidated[-max_messages:]

        # Avoid starting mid-turn when possible.
        for i, message in enumerate(sliced):
            if message.get("role") == "user":
                sliced = sliced[i:]
                break

        # Drop orphan tool results at the front.
        start = find_legal_message_start(sliced)
        if start:
            sliced = sliced[start:]

        out: list[dict[str, Any]] = []
        for message in sliced:
            if "role" not in message:
                continue  # skip internal events (e.g. _type: event)
            entry: dict[str, Any] = {"role": message["role"], "content": message.get("content", "")}
            for key in ("tool_calls", "tool_call_id", "name", "reasoning_content"):
                if key in message:
                    entry[key] = message[key]
            out.append(entry)
        return out

    def clear(self) -> None:
        """Clear all messages and reset session to initial state."""
        self.messages = []
        self.last_consolidated = 0
        self.updated_at = datetime.now()

    def retain_recent_legal_suffix(self, max_messages: int) -> None:
        """Keep a legal recent suffix, mirroring get_history boundary rules."""
        if max_messages <= 0:
            self.clear()
            return
        if len(self.messages) <= max_messages:
            return

        start_idx = max(0, len(self.messages) - max_messages)

        # If the cutoff lands mid-turn, extend backward to the nearest user turn.
        while start_idx > 0 and self.messages[start_idx].get("role") != "user":
            start_idx -= 1

        retained = self.messages[start_idx:]

        # Mirror get_history(): avoid persisting orphan tool results at the front.
        start = find_legal_message_start(retained)
        if start:
            retained = retained[start:]

        dropped = len(self.messages) - len(retained)
        self.messages = retained
        self.last_consolidated = max(0, self.last_consolidated - dropped)
        self.updated_at = datetime.now()


class SessionManager:
    """
    Manages conversation sessions.

    Sessions are stored as JSONL files. When a WorkspaceLayout is provided,
    files use the per-channel hierarchy (discord/{name}/sessions/...);
    otherwise, falls back to the flat workspace/sessions/ directory.
    """

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(self.workspace / "sessions")
        self.legacy_sessions_dir = get_legacy_sessions_dir()
        self._cache: dict[str, Session] = {}

    # --- Path helpers ---

    def _get_session_path(self, key: str) -> Path:
        """Get the file path for a session (flat legacy layout)."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.sessions_dir / f"{safe_key}.jsonl"

    def _get_legacy_session_path(self, key: str) -> Path:
        """Legacy global session path (~/.nanobot/sessions/)."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.legacy_sessions_dir / f"{safe_key}.jsonl"

    @staticmethod
    def _session_key(layout: WorkspaceLayout) -> str:
        return f"{layout.channel}:{layout.chat_id}"

    # --- Core API ---

    def get_or_create(self, key_or_layout) -> Session:
        """
        Get an existing session or create a new one.

        Args:
            key_or_layout: Session key string (legacy) or WorkspaceLayout (new).
        """
        if isinstance(key_or_layout, WorkspaceLayout):
            return self._get_or_create_layout(key_or_layout)
        return self._get_or_create_key(key_or_layout)

    def _get_or_create_key(self, key: str) -> Session:
        """Legacy path: flat sessions/ directory."""
        if key in self._cache:
            return self._cache[key]
        session = self._load_flat(key)
        if session is None:
            session = Session(key=key)
        self._cache[key] = session
        return session

    def _get_or_create_layout(self, layout: WorkspaceLayout) -> Session:
        """New path: per-channel hierarchy."""
        key = self._session_key(layout)
        if key in self._cache:
            return self._cache[key]
        session = self._load_layout(layout)
        if session is None:
            layout.ensure_dirs()
            today = date.today().isoformat()
            seq = layout.next_session_seq(today)
            session = Session(key=key)
            session.metadata["_file_path"] = str(layout.session_path(today, seq))
        self._cache[key] = session
        return session

    def new_session(self, layout: WorkspaceLayout) -> Session:
        """Archive current session (keep file on disk), create fresh session with next seq."""
        key = self._session_key(layout)
        self._cache.pop(key, None)
        layout.ensure_dirs()
        today = date.today().isoformat()
        seq = layout.next_session_seq(today)
        session = Session(key=key)
        session.metadata["_file_path"] = str(layout.session_path(today, seq))
        self._cache[key] = session
        return session

    def current_llm_log_path(self, layout: WorkspaceLayout) -> Path:
        """LLM log path that corresponds to the current session file."""
        today = date.today().isoformat()
        path = layout.current_session_path(today)
        if path:
            seq = int(path.stem.rsplit("_", 1)[-1])
        else:
            seq = layout.next_session_seq(today)
        return layout.llm_log_path(today, seq)

    # --- Load ---

    def _load_flat(self, key: str) -> Session | None:
        """Load from flat sessions/ directory with legacy migration."""
        path = self._get_session_path(key)
        if not path.exists():
            legacy_path = self._get_legacy_session_path(key)
            if legacy_path.exists():
                try:
                    shutil.move(str(legacy_path), str(path))
                    logger.info("Migrated session {} from legacy path", key)
                except Exception:
                    logger.exception("Failed to migrate session {}", key)
        if not path.exists():
            return None
        return self._parse_jsonl(path, key)

    def _load_layout(self, layout: WorkspaceLayout) -> Session | None:
        """Load from per-channel hierarchy. Picks the latest session file for today."""
        today = date.today().isoformat()
        path = layout.current_session_path(today)
        if not path:
            return None
        key = self._session_key(layout)
        session = self._parse_jsonl(path, key)
        if session:
            session.metadata["_file_path"] = str(path)
        return session

    @staticmethod
    def _parse_jsonl(path: Path, key: str) -> Session | None:
        """Parse a JSONL session file."""
        try:
            messages = []
            metadata = {}
            created_at = None
            last_consolidated = 0

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None
                        last_consolidated = data.get("last_consolidated", 0)
                    elif data.get("_type"):
                        continue  # skip event rows and other internal types
                    else:
                        messages.append(data)

            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated,
            )
        except Exception as e:
            logger.warning("Failed to load session {}: {}", key, e)
            return None

    # --- Save ---

    def save(self, session: Session) -> None:
        """Save a session to disk."""
        file_path = session.metadata.get("_file_path")
        path = Path(file_path) if file_path else self._get_session_path(session.key)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Don't persist internal metadata keys
        persisted_meta = {k: v for k, v in session.metadata.items() if not k.startswith("_")}

        with open(path, "w", encoding="utf-8") as f:
            metadata_line = {
                "_type": "metadata",
                "key": session.key,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "metadata": persisted_meta,
                "last_consolidated": session.last_consolidated,
            }
            f.write(json.dumps(metadata_line, ensure_ascii=False) + "\n")
            for msg in session.messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

        self._cache[session.key] = session

    def append_event(self, session: "Session", event: dict) -> None:
        """Append an event line to the session JSONL file without touching messages."""
        file_path = session.metadata.get("_file_path")
        path = Path(file_path) if file_path else self._get_session_path(session.key)
        if path.exists():
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def invalidate(self, key: str) -> None:
        """Remove a session from the in-memory cache."""
        self._cache.pop(key, None)

    def list_sessions(self) -> list[dict[str, Any]]:
        """
        List all sessions.

        Returns:
            List of session info dicts.
        """
        sessions = []

        for path in self.sessions_dir.glob("*.jsonl"):
            try:
                # Read just the metadata line
                with open(path, encoding="utf-8") as f:
                    first_line = f.readline().strip()
                    if first_line:
                        data = json.loads(first_line)
                        if data.get("_type") == "metadata":
                            key = data.get("key") or path.stem.replace("_", ":", 1)
                            sessions.append({
                                "key": key,
                                "created_at": data.get("created_at"),
                                "updated_at": data.get("updated_at"),
                                "path": str(path)
                            })
            except Exception:
                continue

        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)
