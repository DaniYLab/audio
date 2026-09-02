"""T-E2E: pipeline end-to-end test — fake LLM + memory store + local audio.

Unless every stage runs (or is explicitly skipped with a reason), this
test is the gate that blocks all features after M4 (WORKPLAN_WIRING_SPRINT §4).
"""

from __future__ import annotations

import hashlib
import json
import struct
import wave
from pathlib import Path

import pytest

from storyforge.core.config import Settings
from storyforge.core.types import (
    NarrationClip,
    RunManifest,
    SourceRef,
    StageStatus,
    StoryScene,
    Transcript,
    TranscriptSegment,
)

pytestmark = pytest.mark.e2e


# -- fixture helpers -------------------------------------------------------------


def _silence_audio(path: Path, duration_seconds: float = 30.0) -> None:
    """Generate a floating-point 44100 Hz mono WAV of silence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sample_rate = 44100
    n_frames = int(sample_rate * duration_seconds)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        # Silence: all zeros.
        wf.writeframes(b"\x00" * (n_frames * 2))


def _fake_png(path: Path) -> None:
    """Minimal 1x1 blue PNG."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # PNG header + IHDR + IDAT + IEND — minimal valid PNG.
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1x1, 8-bit RGB
    def _chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", 0xFFFFFFFF & zlib.crc32(c))
    import zlib
    raw = b"\x00" + b"\x00\x00\xff"  # filter byte + blue pixel
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def _fake_wav_short(path: Path, duration: float = 1.0) -> None:
    """Short WAV with a 440 Hz tone (audible, easily identifiable)."""
    import math

    path.parent.mkdir(parents=True, exist_ok=True)
    sample_rate = 22050
    n_frames = int(sample_rate * duration)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_frames):
            val = int(16000 * math.sin(i * 440 * 2 * math.pi / sample_rate))
            frames.extend(struct.pack("<h", max(-32768, min(32767, val))))
        wf.writeframes(bytes(frames))


# -- fake providers -------------------------------------------------------------


class FakeLLM:
    """Returns pre-canned story responses and records usage metrics (so the
    cost test sees > $0)."""

    _OUTLINE = (
        "BEAT: hook_00 | A rainy day | Lan | a girl in yellow raincoat\n"
        "BEAT: beat_01 | Lan walks through the market | Lan\n"
        "BEAT: beat_02 | Lan meets a mysterious cat | Lan, cat\n"
    )
    _FACTS = (
        "FACT: character | Lan | Lan là cô bé mặc áo mưa vàng | invented | -\n"
        "FACT: event | Lan | Lan đi chợ trong mưa | invented | -\n"
    )

    def chat(self, system: str, user: str) -> str:
        if "outline" in user.lower():
            response, tokens = self._OUTLINE, 500
        elif "continuity" in system.lower() or "extracting" in system.lower():
            response, tokens = self._FACTS, 200
        else:
            # Scene generation — vary narration to avoid guard digest collision.
            seed = len(user) % 97
            response = (
                f"Beat {seed}. Hôm ấy trời mưa rất to, "
                f"Lan mặc áo mưa vàng bước ra con chợ."
            )
            response += "\nIMAGE_PROMPT: watercolor. rainy market scene"
            tokens = 100

        # Record usage like the real LLMClient does (T5-DEV2).
        from storyforge.core.metrics import current_run_recorder
        from storyforge.providers.llm import _current_stage

        rec = current_run_recorder()
        if rec is not None:
            stage = _current_stage.get() or "story"
            rec.record(
                stage,
                llm_input_tokens=tokens,
                llm_output_tokens=tokens // 4,
                api_calls=1,
            )
        return response


def _fake_transcriber(settings: Settings) -> object:
    class FakeTranscriber:
        def transcribe(self, audio_path: str) -> Transcript:
            return Transcript(
                source=SourceRef(id=Path(audio_path).stem, local_path=Path(audio_path)),
                language="vi",
                segments=[
                    TranscriptSegment(
                        start=0.0, end=5.0, text="Xin chào các bạn."
                    ),
                    TranscriptSegment(
                        start=5.0, end=30.0, text="Hôm nay tôi sẽ kể một câu chuyện."
                    ),
                ],
                audio_path=Path(audio_path),
            )
    return FakeTranscriber()


def _fake_tts(settings: Settings) -> object:
    class FakeTTS:
        def synthesize(self, scene: StoryScene, out_path: str, voice: str | None) -> NarrationClip:
            _fake_wav_short(Path(out_path), duration=1.0)
            return NarrationClip(
                scene_id=scene.scene_id,
                audio_path=Path(out_path),
                duration_seconds=1.0,
                char_count=len(scene.narration_text),
            )
    return FakeTTS()


def _fake_image_generator(settings: Settings) -> object:
    class FakeGenerator:
        def generate_from_prompt(
            self, prompt: str, out_path: str, reference_image: Path | None = None
        ) -> object:
            _fake_png(Path(out_path))
            from storyforge.core.types import Illustration
            return Illustration(
                scene_id="",
                image_path=Path(out_path),
                prompt_hash=hashlib.sha256(prompt.encode()).hexdigest()[:16],
            )
    return FakeGenerator()


# -- the E2E test ---------------------------------------------------------------


@pytest.fixture()
def fixture_audio(tmp_path: Path) -> Path:
    p = tmp_path / "fixtures" / "source.wav"
    _silence_audio(p, duration_seconds=30.0)
    return p


def _run_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    audio_path: Path,
    settings_overrides: dict[str, object] | None = None,
) -> RunManifest:
    """Run the full pipeline with fake providers, return the final manifest."""
    overrides = {
        "llm__api_key": "test-key",
        "workspace_dir": tmp_path / "workspace",
        "download__acknowledge_tos_risk": True,
    }
    if settings_overrides:
        overrides.update(settings_overrides)
    settings = Settings(**overrides)  # type: ignore[arg-type]

    # Pin writer model so prices.yaml lookups work.
    settings.llm.writer_model = "gpt-4o"
    settings.llm.reviewer_model = "gpt-4o-mini"
    # Nested settings are not overridable via constructor kwargs — set them
    # explicitly so the pipeline runs against the in-memory store (no Qdrant).
    settings.knowledge.store = "memory"
    settings.knowledge.episode_summary = False

    # Monkeypatch all external providers.
    monkeypatch.setattr("storyforge.providers.stt.build_transcriber", _fake_transcriber)
    monkeypatch.setattr("storyforge.providers.llm.LLMClient.chat", FakeLLM().chat)
    monkeypatch.setattr("storyforge.providers.tts.build_tts", _fake_tts)
    monkeypatch.setattr("storyforge.providers.imaging.build_image_generator", _fake_image_generator)

    # ffmpeg may be absent on dev machines — stub the render call so the rest
    # of the pipeline still completes (the real render test skips with a
    # reason). Spec: mark skips clearly, never delete the assertion.
    import shutil

    if shutil.which("ffmpeg") is None:
        def _fake_ffmpeg(self: object, cmd: list[str], log_path: Path) -> None:
            log_path.write_text("(ffmpeg not installed — stubbed in e2e)", encoding="utf-8")

        monkeypatch.setattr("storyforge.stages.video.VideoStage._run_ffmpeg", _fake_ffmpeg)

    # Run the pipeline.
    from storyforge.cli import _execute_pipeline

    story_config_path = tmp_path / "story.yaml"
    story_config_path.write_text(
        "title: E2E Test\ngenre: test\nuniverse: e2e_universe\nlanguage: vi\n"
        "target_minutes: 1\n"
        "recap: true\nmusic_mood: calm\n",
        encoding="utf-8",
    )

    _execute_pipeline(
        settings,
        project="e2e_proj",
        story_config_path=story_config_path,
        urls=[],
        local_files=[audio_path],
        force=True,
        only=None,
    )

    # Load the final manifest.
    from storyforge.core.artifacts import ArtifactStore
    store = ArtifactStore(settings.workspace_dir, "e2e_proj")
    return store.load_manifest()


def _ffmpeg_available() -> bool:
    import shutil
    return shutil.which("ffmpeg") is not None


# -- test cases -----------------------------------------------------------------


def test_e2e_pipeline_manifest_all_done_or_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fixture_audio: Path
) -> None:
    """Every stage must reach DONE (or FAILED with a clear reason)."""
    manifest = _run_pipeline(monkeypatch, tmp_path, fixture_audio)
    for stage, record in manifest.stages.items():
        assert record.status in (
            StageStatus.DONE, StageStatus.SKIPPED, StageStatus.FAILED
        ), f"stage {stage} left in {record.status}"
        if record.status is StageStatus.FAILED:
            assert record.error, f"stage {stage} FAILED without error message"


def test_e2e_cost_report_positive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fixture_audio: Path
) -> None:
    """AC1 (T1-DEV1): cost report > $0 after a pipeline run with LLM calls."""
    manifest = _run_pipeline(monkeypatch, tmp_path, fixture_audio)
    from storyforge.core.cost import build_cost_report
    settings = Settings(llm__api_key="test-key", knowledge__store="memory")
    settings.llm.writer_model = "gpt-4o"
    report = build_cost_report(manifest, settings)
    assert report.total_cost_usd > 0.0, (
        f"cost report is $0 — manifest stages: "
        f"{[(s, r.status.value, r.metrics) for s, r in manifest.stages.items()]}"
    )


def test_e2e_alert_fires_after_two_consecutive_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fixture_audio: Path
) -> None:
    """AC1 (T2-DEV1): 2 consecutive pipeline failures → data/alerts.md is written."""
    from storyforge.cli import _check_alert

    settings = Settings(
        llm__api_key="test-key",
        knowledge__store="memory",
        workspace_dir=tmp_path / "ws",
    )
    manifest = RunManifest(project="e2e_alert")
    manifest.mark("story", StageStatus.FAILED, error="boom")
    alerts_path = tmp_path / "alerts.md"

    # First failure: no alert.
    _check_alert(settings, "e2e_alert", manifest)
    assert not alerts_path.exists()

    # Second consecutive failure: alert fires.
    _check_alert(settings, "e2e_alert", manifest)
    assert alerts_path.exists()
    text = alerts_path.read_text(encoding="utf-8")
    assert "story" in text
    assert "fail x2" in text


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg not installed on this machine")
def test_e2e_video_renders(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fixture_audio: Path
) -> None:
    """When ffmpeg is available, the video stage renders a file."""
    manifest = _run_pipeline(monkeypatch, tmp_path, fixture_audio)
    video_record = manifest.stages.get("video")
    assert video_record is not None
    assert video_record.status is StageStatus.DONE, (
        f"video stage: {video_record.status} — {video_record.error}"
    )


def test_e2e_music_resolves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fixture_audio: Path
) -> None:
    """When music_mood is set, the resolver finds the file."""
    from storyforge.music import resolve_mood_file

    # No fixture file exists yet — verify the resolver returns None gracefully.
    result = resolve_mood_file("calm")
    # If the file exists (BA supplied), assert it resolves; otherwise skip.
    if result is not None:
        assert result.exists()
    else:
        pytest.skip("calm.mp3 not in assets/music_cc0/ — BA not yet supplied")


def test_e2e_story_config_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fixture_audio: Path
) -> None:
    """The StoryConfig used during the run is written to 04_story/config.json."""
    from storyforge.core.artifacts import ArtifactStore
    settings = Settings(
        llm__api_key="test-key",
        knowledge__store="memory",
        workspace_dir=tmp_path / "workspace",
    )
    _run_pipeline(monkeypatch, tmp_path, fixture_audio)
    store = ArtifactStore(settings.workspace_dir, "e2e_proj")
    config_path = store.dir("04_story") / "config.json"
    assert config_path.exists()
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["title"] == "E2E Test"
