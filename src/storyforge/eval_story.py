"""Prompt-eval harness (M2-D4 spec §3, M4-B2 refinement).

``storyforge eval-story --project demo`` reads the story artifact + lint report,
asks the judge model to score each scene against the 6-dimension rubric, and
writes ``evals/story/<date>_v<prompt_version>.json``.

M4-B2: 0-100 scores, verdict derived from 40/70 thresholds, hook evidence
mandatory, tts_ready capped at 40 when lint fails.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from storyforge.core.artifacts import ArtifactStore
from storyforge.core.config import Settings
from storyforge.core.exceptions import StoryGenerationError
from storyforge.core.types import DimensionScore, Story, StoryEval
from storyforge.lint import LintReport
from storyforge.providers.llm import LLMClient, extract_json, fill_prompt, load_prompt_with_meta

_DIMENSIONS = ("grounding", "consistency", "pacing", "tts_ready", "visual", "hook")


def load_story_artifact(store: ArtifactStore) -> tuple[Story, LintReport | None, int]:
    story = store.read_model(store.story_path(), Story)
    lint_path = store.dir("04_story") / "lint_report.json"
    lint = None
    if lint_path.exists():
        lint = LintReport.model_validate_json(lint_path.read_text(encoding="utf-8"))
    meta, _ = load_prompt_with_meta("outline")
    return story, lint, meta.version


def _scene_text_for_judge(story: Story, scene_id: str) -> str:
    for scene in story.scenes:
        if scene.scene_id == scene_id:
            return scene.narration_text
    return ""


def _lint_summary(lint: LintReport | None) -> str:
    if lint is None or not lint.issues:
        return ""
    return "\n".join(f"- [{i.severity}] {i.rule}: {i.excerpt}" for i in lint.issues)


def _score_to_100(raw: float) -> float:
    """M4-B2 AC4: legacy 1-5 -> 0-100 by scaling x20."""
    return raw * 20.0 if raw <= 5.0 else raw


def _verdict_for(score: float) -> Literal["fail", "warn", "pass"]:
    """M4-B2 AC1: verdict DERIVED from score thresholds, never LLM."""
    return "fail" if score < 40 else ("warn" if score < 70 else "pass")


def judge_scene(
    settings: Settings,
    story: Story,
    scene_id: str,
    lint: LintReport | None,
    prompt_version: int,
) -> StoryEval:
    """Score one scene via the judge model (M2-D4 §3.3 + M4-B2)."""
    judge = LLMClient(settings, settings.llm.reviewer_model)
    _, template = load_prompt_with_meta("judge_story")

    user = fill_prompt(
        template,
        {
            "scene": _scene_text_for_judge(story, scene_id),
            "lint_report": _lint_summary(lint),
        },
    )
    response = judge.chat("You are a strict story quality judge.", user)
    data = extract_json(response)

    scores_raw = data.get("scores")
    if not isinstance(scores_raw, list) or not scores_raw:
        raise StoryGenerationError("judge returned no scores", details={"response": response[:500]})

    scores: list[DimensionScore] = []
    for row in scores_raw:
        if not isinstance(row, dict):
            continue
        dim = str(row.get("dimension", ""))
        if dim not in _DIMENSIONS:
            continue
        score = _score_to_100(float(row.get("score", 0.0)))
        evidence = str(row.get("evidence", "")).strip()
        # M4-B2 AC2: hook dimension must quote verbatim text.
        if dim == "hook" and len(evidence) < 4:
            raise StoryGenerationError(
                "hook dimension missing verbatim evidence",
                details={"response": response[:500]},
            )
        scores.append(
            DimensionScore(
                dimension=dim,  # type: ignore[arg-type]
                score=score,
                evidence=evidence,
                verdict=_verdict_for(score),
            )
        )

    # M4-B2 AC3: a failing lint rule caps tts_ready at ≤ 40.
    if lint is not None:
        scene_fails = [i for i in lint.issues if i.scene_id == scene_id and i.severity == "fail"]
        if scene_fails:
            for s in scores:
                if s.dimension == "tts_ready" and s.score > 40:
                    s.score = 40.0
                    s.verdict = _verdict_for(40.0)

    mean = sum(s.score for s in scores) / len(scores) if scores else 0.0
    return StoryEval(
        prompt_version=prompt_version,
        project=story.config.title,
        scene_id=scene_id,
        scores=scores,
        total=mean,
        judge_model=settings.llm.reviewer_model,
    )


def eval_story(
    settings: Settings, store: ArtifactStore, judge_model: str | None = None
) -> list[StoryEval]:
    """Score every scene and write evals/story/<date>_v<version>.json."""
    if judge_model is not None:
        settings.llm.reviewer_model = judge_model

    story, lint, prompt_version = load_story_artifact(store)
    evals = [
        judge_scene(settings, story, scene.scene_id, lint, prompt_version) for scene in story.scenes
    ]

    out_dir = Path(settings.workspace_dir) / store.root.name / "evals" / "story"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    out_path = out_dir / f"{stamp}_v{prompt_version}.json"
    out_path.write_text(
        json.dumps([e.model_dump(mode="json") for e in evals], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return evals
