"""Text-to-speech providers."""

from __future__ import annotations

import subprocess
from pathlib import Path

from storyforge.core.config import Settings
from storyforge.core.contracts import TextToSpeech
from storyforge.core.exceptions import TTSError
from storyforge.core.retry import retry_external
from storyforge.core.types import NarrationClip, StoryScene


def build_tts(settings: Settings) -> TextToSpeech:
    engine = settings.tts.engine
    if engine == "edge":
        return EdgeTTS(settings)
    if engine == "elevenlabs":
        return ElevenLabsTTS(settings)
    raise TTSError(f"unknown tts engine: {engine}")


def probe_duration(audio_path: str, ffprobe_bin: str = "ffprobe") -> float:
    """Read audio duration in seconds via ffprobe."""
    result = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "quiet",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            audio_path,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise TTSError(f"ffprobe failed for {audio_path}")
    try:
        return float(result.stdout.strip())
    except ValueError as exc:
        raise TTSError(f"unparseable duration for {audio_path}") from exc


class EdgeTTS:
    """Free Microsoft Edge neural voices (unofficial endpoint; no SLA)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def synthesize(self, scene: StoryScene, out_path: str, voice: str | None) -> NarrationClip:
        import edge_tts  # type: ignore[import-not-found]

        cfg = self._settings.tts

        def _call() -> None:
            communicate = edge_tts.Communicate(
                text=scene.narration_text,
                voice=voice or cfg.edge_voice,
                rate=cfg.rate,
                pitch=cfg.pitch,
            )
            import anyio

            anyio.run(communicate.save, out_path)

        retry_external(_call)
        duration = probe_duration(out_path, self._settings.video.ffprobe_bin)
        return NarrationClip(
            scene_id=scene.scene_id,
            audio_path=Path(out_path),
            duration_seconds=duration,
            char_count=len(scene.narration_text),
        )


class ElevenLabsTTS:
    """High-quality narration; Vietnamese supported via multilingual v2/v3."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        key = settings.tts.elevenlabs_api_key.get_secret_value()
        if not key:
            raise TTSError("SF__TTS__ELEVENLABS_API_KEY is required")
        voice_id = settings.tts.elevenlabs_voice_id
        if not voice_id:
            raise TTSError("SF__TTS__ELEVENLABS_VOICE_ID is required")

    def synthesize(self, scene: StoryScene, out_path: str, voice: str | None) -> NarrationClip:
        import httpx

        voice_id = voice or self._settings.tts.elevenlabs_voice_id
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        headers = {
            "xi-api-key": self._settings.tts.elevenlabs_api_key.get_secret_value(),
            "Content-Type": "application/json",
        }
        payload = {
            "text": scene.narration_text,
            "model_id": "eleven_multilingual_v2",
            "output_format": "mp3_44100_128",
        }

        def _call() -> bytes:
            response = httpx.post(url, headers=headers, json=payload, timeout=120.0)
            if response.status_code in (429, 500, 502, 503, 504):
                from storyforge.core.exceptions import ExternalServiceError

                raise ExternalServiceError(
                    f"elevenlabs http {response.status_code}", retryable=True
                )
            if response.status_code != 200:
                from storyforge.core.exceptions import ExternalServiceError

                raise ExternalServiceError(
                    f"elevenlabs http {response.status_code}: {response.text[:300]}",
                    retryable=False,
                )
            return response.content

        audio = retry_external(_call)
        Path(out_path).write_bytes(audio)
        duration = probe_duration(out_path, self._settings.video.ffprobe_bin)
        return NarrationClip(
            scene_id=scene.scene_id,
            audio_path=Path(out_path),
            duration_seconds=duration,
            char_count=len(scene.narration_text),
        )
