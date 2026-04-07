"""Text-to-speech service for nanobot."""

from pathlib import Path
from typing import Any

from loguru import logger


class TTSService:
    """Service for generating text-to-speech audio."""

    def __init__(
        self,
        enabled: bool = False,
        provider: str = "openai",
        voice: str = "alloy",
        api_key: str = "",
        auto_tts_senders: list[str] | None = None,
    ):
        self.enabled = enabled
        self.provider = provider
        self.voice = voice
        self.api_key = api_key
        self.auto_tts_senders = set(auto_tts_senders or [])
        self._provider_instance: Any = None

    def should_trigger(
        self,
        session_tts: bool = False,
        skill_meta: dict[str, Any] | None = None,
        sender_name: str | None = None,
    ) -> bool:
        """Check if TTS should be triggered for this response."""
        if not self.enabled:
            return False
        if session_tts:
            return True
        if skill_meta and skill_meta.get("tts"):
            return True
        # Auto-TTS for configured sender names (case-insensitive)
        if sender_name and self.auto_tts_senders:
            if sender_name.lower() in (s.lower() for s in self.auto_tts_senders):
                return True
        return False

    async def generate_speech(self, text: str, output_path: Path) -> bool:
        """Generate speech from text and save to output_path."""
        if not self.enabled or not self.api_key:
            return False

        try:
            if self.provider == "openai":
                return await self._generate_openai(text, output_path)
            # Add more providers here
            return False
        except Exception as e:
            logger.warning("TTS generation failed: {}", e)
            return False

    async def _generate_openai(self, text: str, output_path: Path) -> bool:
        """Generate speech using OpenAI TTS API."""
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=self.api_key)
        response = await client.audio.speech.create(
            model="tts-1",
            voice=self.voice,
            input=text,
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        response.stream_to_file(str(output_path))
        return True
