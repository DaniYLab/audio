"""Pacing / TTS lint — deterministic pre-judge quality gates (M2-D2 spec).

Runs inside the story stage right after each scene is drafted, on the text the
TTS will actually read (normalized form). Cheap regex checks fail early —
before spending an LLM judge or a render.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from storyforge.core.types import StoryScene
from storyforge.textnorm import TextNormalizer

_SENTENCE_SPLIT = re.compile(r"[.!?…]+")
_WEIRD_CHAR = re.compile(r"[^\w\s\u00C0-\u024F\u0300-\u036f.!?…,;:\-\"'()]")
# Repeated word >= 3 consecutive occurrences: "rất rất rất".
_WORD_REPEAT = re.compile(r"\b(\w+)(?:\s+\1){2,}\b")
_DIGIT_LEFT = re.compile(r"\d")

# M2-D2 §2.2 thresholds.
_SENTENCE_MAX_WORDS = 25
_SENTENCE_WARN_WORDS = 20
_SCENE_MIN_WORDS = 40
_SCENE_MAX_WORDS = 350
_EXCERPT_LEN = 120


class LintIssue(BaseModel):
    scene_id: str
    rule: str
    severity: Literal["warn", "fail"]
    excerpt: str = Field(max_length=200)  # ~120 chars around the problem
    suggestion: str | None = None


class LintReport(BaseModel):
    issues: list[LintIssue] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(issue.severity == "fail" for issue in self.issues)


def _excerpt(text: str, start: int, end: int) -> str:
    start = max(0, start - 20)
    end = min(len(text), end + 20)
    return text[start:end][:200]


def _word_count(text: str) -> int:
    return len(text.split())


def lint_scene(scene: StoryScene, normalizer: TextNormalizer | None = None) -> list[LintIssue]:
    """Run all deterministic checks against one scene's narration.

    The checks run on the normalized text (what TTS reads), so a leftover
    digit means the normalizer missed it — which is itself a fail.
    """
    normalizer = normalizer or TextNormalizer()
    normalized = normalizer.normalize(scene.narration_text).normalized
    issues: list[LintIssue] = []

    # sentence_length: split on [.!?…]
    for sentence in _SENTENCE_SPLIT.split(normalized):
        sentence = sentence.strip()
        if not sentence:
            continue
        count = _word_count(sentence)
        if count > _SENTENCE_MAX_WORDS:
            issues.append(
                LintIssue(
                    scene_id=scene.scene_id,
                    rule="sentence_length",
                    severity="fail",
                    excerpt=_excerpt(normalized, 0, len(sentence)),
                    suggestion=f"split into sentences of ≤ {_SENTENCE_MAX_WORDS} words",
                )
            )
        elif count >= _SENTENCE_WARN_WORDS:
            issues.append(
                LintIssue(
                    scene_id=scene.scene_id,
                    rule="sentence_length_warn",
                    severity="warn",
                    excerpt=_excerpt(normalized, 0, len(sentence)),
                    suggestion="consider splitting a long sentence",
                )
            )

    # digit_leftover: digits must not survive normalization.
    if _DIGIT_LEFT.search(normalized):
        issues.append(
            LintIssue(
                scene_id=scene.scene_id,
                rule="digit_leftover",
                severity="fail",
                excerpt=_excerpt(normalized, 0, len(normalized)),
                suggestion="normalizer missed a digit — add a rule",
            )
        )

    # weird_char: emoji / characters outside Vietnamese letters, punct, spaces.
    weird = sorted({c for c in normalized if _WEIRD_CHAR.match(c)})
    if weird:
        issues.append(
            LintIssue(
                scene_id=scene.scene_id,
                rule="weird_char",
                severity="fail",
                excerpt=_excerpt(normalized, 0, len(normalized)),
                suggestion=f"unexpected characters: {' '.join(weird)}",
            )
        )

    # word_repeat: same word 3+ times consecutively.
    for match in _WORD_REPEAT.finditer(normalized):
        issues.append(
            LintIssue(
                scene_id=scene.scene_id,
                rule="word_repeat",
                severity="warn",
                excerpt=_excerpt(normalized, match.start(), match.end()),
                suggestion=f"repeated word '{match.group(1)}'",
            )
        )

    # scene_length: too short or too long for the target.
    count = _word_count(normalized)
    if count < _SCENE_MIN_WORDS or count > _SCENE_MAX_WORDS:
        issues.append(
            LintIssue(
                scene_id=scene.scene_id,
                rule="scene_length",
                severity="warn",
                excerpt=_excerpt(normalized, 0, len(normalized)),
                suggestion=(
                    f"scene is {count} words; target is " f"{_SCENE_MIN_WORDS}-{_SCENE_MAX_WORDS}"
                ),
            )
        )

    return issues


def lint_story(scenes: list[StoryScene], normalizer: TextNormalizer | None = None) -> LintReport:
    """Lint every scene and aggregate into a report."""
    issues = [issue for scene in scenes for issue in lint_scene(scene, normalizer=normalizer)]
    return LintReport(issues=issues)
