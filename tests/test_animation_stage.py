"""M6-W1: animation stage tests — provider wiring + VideoStage consumption."""

from __future__ import annotations

from pathlib import Path

import pytest

from storyforge.core.artifacts import ArtifactStore
from storyforge.core.config import Settings
from storyforge.core.contracts import StageContext
from storyforge.core.types import Illustration, NarrationClip, RunManifest
from storyforge.providers.animation import AnimatedClip


def _ctx(tmp_path: Path) -> StageContext:
    settings = Settings(llm__api_key="test-key", workspace_dir=tmp_path / "ws")
    store = ArtifactStore(settings.workspace_dir, "proj")
    return StageContext(settings, store, RunManifest(project="proj"))


def _clip(scene_id: str, seconds: float = 5.0) -> NarrationClip:
    return NarrationClip(
        scene_id=scene_id,
        audio_path=Path(f"{scene_id}.mp3"),
        duration_seconds=seconds,
        char_count=50,
    )


def _illustration(scene_id: str) -> Illustration:
    return Illustration(
        scene_id=scene_id,
        image_path=Path(f"{scene_id}.png"),
        prompt_hash="h",
    )


class _FakeProvider:
    """Produces a fake clip file per scene (behaves like an API provider)."""

    def __init__(self, *, fail_scene: str | None = None) -> None:
        self.fail_scene = fail_scene
        self.calls: list[str] = []

    def animate(
        self, image_path: str, duration_seconds: float, motion: object, out_path: str
    ) -> AnimatedClip:
        self.calls.append(out_path)
        if self.fail_scene is not None and self.fail_scene in out_path:
            raise RuntimeError("provider down")
        Path(out_path).write_bytes(b"clip")
        return AnimatedClip(
            clip_path=Path(out_path),
            duration_seconds=duration_seconds,
            provider="fal_kling",
            cost_usd=0.01,
        )


def test_animation_stage_produces_clips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import storyforge.stages.animation as anim_mod

    provider = _FakeProvider()
    monkeypatch.setattr(anim_mod, "build_animation_provider", lambda settings: provider)

    ctx = _ctx(tmp_path)
    stage = anim_mod.AnimationStage(
        clips=[_clip("s1"), _clip("s2")],
        illustrations={"s1": _illustration("s1"), "s2": _illustration("s2")},
    )
    result = stage.run(ctx)

    assert set(result) == {"s1", "s2"}
    clip = result["s1"]
    assert clip.clip_path.exists()
    assert clip.provider == "fal_kling"
    assert (ctx.store.dir("06_animation") / "s1.mp4").exists()


def test_animation_stage_provider_failure_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import storyforge.stages.animation as anim_mod

    provider = _FakeProvider(fail_scene="s1")
    monkeypatch.setattr(anim_mod, "build_animation_provider", lambda settings: provider)

    ctx = _ctx(tmp_path)
    stage = anim_mod.AnimationStage(
        clips=[_clip("s1"), _clip("s2")],
        illustrations={"s1": _illustration("s1"), "s2": _illustration("s2")},
    )
    result = stage.run(ctx)
    # s1 fell back (no clip), s2 animated.
    assert "s1" not in result
    assert "s2" in result


def test_animation_skip_without_illustration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import storyforge.stages.animation as anim_mod

    provider = _FakeProvider()
    monkeypatch.setattr(anim_mod, "build_animation_provider", lambda settings: provider)

    ctx = _ctx(tmp_path)
    stage = anim_mod.AnimationStage(
        clips=[_clip("s1"), _clip("missing")],
        illustrations={"s1": _illustration("s1")},
    )
    result = stage.run(ctx)
    assert set(result) == {"s1"}


def test_video_stage_uses_animated_clip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """VideoStage muxes narration onto the pre-rendered clip (no zoompan)."""
    from storyforge.stages.video import VideoStage

    ctx = _ctx(tmp_path)
    clip = _clip("s1")
    (ctx.store.dir("06_animation") / "s1.mp4").write_bytes(b"motion")
    (ctx.store.dir("05_tts") / "s1.mp3").write_bytes(b"audio")
    (ctx.store.dir("06_images") / "s1.png").write_bytes(b"png")

    captured: list[list[str]] = []

    def _fake_run(cmd: list[str], log_path: Path) -> None:
        captured.append(cmd)
        log_path.write_text("ok", encoding="utf-8")
        # The real ffmpeg writes the output file (last positional arg).
        out_path = Path(cmd[-1])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"out")

    stage = VideoStage(
        clips=[clip],
        illustrations=[_illustration("s1")],
        animated={"s1": ctx.store.dir("06_animation") / "s1.mp4"},
    )
    monkeypatch.setattr(stage, "_run_ffmpeg", _fake_run)
    out = stage._render_segment(ctx, clip)
    assert out.exists()
    cmd = captured[0]
    joined = " ".join(cmd)
    assert "06_animation" in joined  # clip input used
    assert "zoompan" not in joined  # no internal Ken Burns


def test_video_stage_kenburns_without_animated_clip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without an animated clip the still-image Ken Burns path is unchanged."""
    from storyforge.stages.video import VideoStage

    ctx = _ctx(tmp_path)
    clip = _clip("s1")
    (ctx.store.dir("05_tts") / "s1.mp3").write_bytes(b"audio")
    (ctx.store.dir("06_images") / "s1.png").write_bytes(b"png")

    captured: list[list[str]] = []

    def _fake_run(cmd: list[str], log_path: Path) -> None:
        captured.append(cmd)
        log_path.write_text("ok", encoding="utf-8")
        # The real ffmpeg writes the output file (last positional arg).
        out_path = Path(cmd[-1])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"out")

    stage = VideoStage(clips=[clip], illustrations=[_illustration("s1")])
    monkeypatch.setattr(stage, "_run_ffmpeg", _fake_run)
    stage._render_segment(ctx, clip)
    joined = " ".join(captured[0])
    assert "zoompan" in joined
    assert "06_animation" not in joined
