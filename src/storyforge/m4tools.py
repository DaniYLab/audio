"""M4 DEV2 tooling — A/B hook (A1) and auto thumbnail (A3).

Self-contained helpers that operate on a finished story artifact; wired to the
CLI as ``storyforge hook-ab`` and ``storyforge thumbnail``.
"""

from __future__ import annotations

import json
from pathlib import Path

from storyforge.core.artifacts import ArtifactStore
from storyforge.core.config import Settings
from storyforge.core.exceptions import StoryGenerationError
from storyforge.core.types import Story, StoryBeat, StoryConfig
from storyforge.providers.llm import LLMClient, StoryWriter, extract_json, fill_prompt, load_prompt

# --- A1: A/B hook -------------------------------------------------------------


def _judge_hook(settings: Settings, hook_text: str) -> float:
    """Score a hook's retention power (0-100) using the judge model."""
    judge = LLMClient(settings, settings.llm.reviewer_model)
    template = load_prompt("judge_story")
    user = fill_prompt(
        template,
        {"scene": hook_text, "lint_report": "", "style_stats": ""},
    )
    response = judge.chat("You are a strict story quality judge.", user)
    data = extract_json(response)
    for row in data.get("scores", []):
        if isinstance(row, dict) and row.get("dimension") == "hook":
            raw = float(row.get("score", 0.0))
            return raw * 20.0 if raw <= 5.0 else raw
    raise StoryGenerationError("judge returned no hook score")


def _hook_system_prompt(config: StoryConfig) -> str:
    """System half of the hook-variant prompt (outline template)."""
    template = load_prompt("outline")
    return fill_prompt(
        template,
        {
            "language": config.language,
            "genre": config.genre,
            "tone": config.style.tone,
            "grounding": config.grounding.value,
        },
    )


def _hook_user_prompt(config: StoryConfig, label: str) -> str:
    """User half asking for ONE cold-open beat line labelled ``label``."""
    template = load_prompt("outline")
    user = fill_prompt(
        template,
        {
            "title": config.title,
            "premise": config.premise or "(derive from knowledge base)",
            "characters": "\n".join(f"- {c.name}: {c.personality}" for c in config.characters)
            or "(no fixed characters)",
            "target_words": int(config.target_minutes * 150),
            "degraded": "",
            "theme": "",
            "dossiers": "",
            "established": "",
            "invented": "",
            "unknown": "",
        },
    )
    user += (
        f"\n\nThis is variant {label} of the cold-open hook. Output ONE beat line "
        "only: BEAT: hook_00 | <one sentence, most tense moment, ≤ 80 words when "
        "expanded> | <characters> | <image hint>"
    )
    return user


def generate_hook_variant(writer: LLMClient, config: StoryConfig, label: str) -> str:
    """Generate one alternative ``hook_00`` BEAT line (label ``a`` or ``b``)."""
    response = writer.chat(_hook_system_prompt(config), _hook_user_prompt(config, label))
    # Extract the BEAT line; fall back to the raw response.
    return next(
        (
            line
            for line in response.splitlines()
            if line.strip().upper().startswith("BEAT:")
        ),
        response,
    )


def _variant_beat(line: str, fallback: StoryBeat) -> StoryBeat:
    """Parse a hook BEAT line; missing fields inherit the fallback beat."""
    parsed = StoryWriter._parse_beats(line)
    beat = parsed[0]
    if not beat.image_hint:
        beat = beat.model_copy(update={"image_hint": fallback.image_hint})
    if not beat.characters:
        beat = beat.model_copy(update={"characters": fallback.characters})
    return beat


def choose_hook_beat(
    settings: Settings, config: StoryConfig, mode: str, original: StoryBeat
) -> StoryBeat | None:
    """Select the hook_00 beat for ``mode`` (M4-A1 AC3).

    - ``manual`` → None (keep the outline's own hook).
    - ``a``/``b`` → generate that variant.
    - ``auto`` → generate both and judge them (higher hook score wins).

    Best-effort: any LLM/parse failure returns None so the pipeline keeps the
    outline's original hook rather than failing the episode.
    """
    if mode == "manual":
        return None
    try:
        writer = LLMClient(settings, settings.llm.writer_model)
        if mode in ("a", "b"):
            return _variant_beat(generate_hook_variant(writer, config, mode), original)
        if mode == "auto":
            variant_a = generate_hook_variant(writer, config, "a")
            variant_b = generate_hook_variant(writer, config, "b")
            score_a = _judge_hook(settings, variant_a)
            score_b = _judge_hook(settings, variant_b)
            return _variant_beat(variant_a if score_a >= score_b else variant_b, original)
    except Exception:
        pass
    return None


def hook_ab(settings: Settings, store: ArtifactStore) -> Path:
    """Generate two alternative hooks, judge both, write hook_ab/ artifacts.

    Returns the path to ``04_story/hook_ab/choice.json``.
    """
    story = store.read_model(store.story_path(), Story)
    writer = LLMClient(settings, settings.llm.writer_model)
    config = story.config

    hooks = {label: generate_hook_variant(writer, config, label) for label in ("a", "b")}
    scores = {label: _judge_hook(settings, text) for label, text in hooks.items()}
    choice = "a" if scores["a"] >= scores["b"] else "b"

    out_dir = store.dir("04_story") / "hook_ab"
    for label, text in hooks.items():
        (out_dir / f"hook_{label}.md").write_text(text, encoding="utf-8")
        (out_dir / f"eval_{label}.json").write_text(
            json.dumps({"score": scores[label], "hook": text}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    choice_path = out_dir / "choice.json"
    choice_path.write_text(
        json.dumps({"choice": choice, "scores": scores}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return choice_path


# --- A3: auto thumbnail -------------------------------------------------------


def _pick_thumbnail_scene(story: Story, settings: Settings) -> tuple[str, str] | None:
    """M4-A3 (T6-DEV2): choose the thumbnail scene.

    Priority: ``StoryConfig.thumbnail_scene`` (per-run) > ``StorySettings.thumbnail_scene``
    (global) > first scene.

    - ``auto`` → first scene (no eval data yet).
    - ``N`` (positive int) → 1-based scene index.
    - ``<scene_id>`` → exact scene id.

    Returns ``(scene_id, beat_hint)`` or None when the story is empty.
    """
    scenes = story.scenes
    if not scenes:
        return None
    override = getattr(story.config, "thumbnail_scene", "auto")
    if override == "auto":
        override = getattr(settings.story, "thumbnail_scene", "auto")
    if isinstance(override, int) or (isinstance(override, str) and override.isdigit()):
        index = int(override)
        if 1 <= index <= len(scenes):
            scene = scenes[index - 1]
            return scene.scene_id, scene.beat.image_hint or scene.image_prompt
    if isinstance(override, str) and override != "auto":
        for scene in scenes:
            if scene.scene_id == override:
                return scene.scene_id, scene.beat.image_hint or scene.image_prompt
    scene = scenes[0]
    return scene.scene_id, scene.beat.image_hint or scene.image_prompt


def auto_thumbnail(settings: Settings, store: ArtifactStore) -> Path | None:
    """Best-effort thumbnail: overlay title on the chosen scene's image.

    Never raises — pipeline continues if anything fails (A3 AC4).
    """
    try:
        story = store.read_model(store.story_path(), Story)
        pick = _pick_thumbnail_scene(story, settings)
        if pick is None:
            return None
        scene_id, _ = pick
        image_dir = store.dir("06_images")
        src = image_dir / f"{scene_id}.png"
        if not src.exists():
            return None
        title = story.config.title
        from PIL import Image, ImageDraw, ImageFont

        img = Image.open(src).convert("RGB").resize((1280, 720))
        draw = ImageDraw.Draw(img)
        font = ImageFont.load_default()
        draw.text((40, 40), title, fill=(255, 255, 255), font=font)
        out = image_dir / "thumbnail.png"
        img.save(out)
        return out
    except Exception:
        return None
