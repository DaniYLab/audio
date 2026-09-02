"""StyleStatsTracker — deterministic style statistics (M4-B1).

Port from ainovel-cli: cheap regex/counter stats, ZERO LLM. The writer prompt
receives them as ``working_memory.style_stats`` before each scene so it does
not repeat patterns (long sentences back-to-back, identical openers), and the
judge rubric uses them for the ``tts_ready`` / ``visual`` dimensions.

Per-episode stats land in ``04_story/style_stats.json``; cross-episode
accumulation (last 10 episodes) is a Dev 1 ops concern (``data/kb/<universe>/
style_stats/``) — this module only computes per-episode numbers.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, Field

_SENTENCE_SPLIT = re.compile(r"[.!?…]+")
_OPENING_ACTION_VERBS = (
    "bước",
    "chạy",
    "đứng",
    "ngồi",
    "đi",
    "nhìn",
    "mở",
    "đóng",
    "cầm",
    "ném",
    "vào",
    "ra",
    "quay",
    "nghe",
    "gọi",
)
_OPENING_DESCRIPTIVE = (
    "trong",
    "trên",
    "dưới",
    "cạnh",
    "giữa",
    "buổi",
    "trời",
    "căn",
    "ngôi",
    "con đường",
    "mặt trời",
    "bầu trời",
)
_QUOTE_CHARS = ('"', "'", "“", "”", "-", "-", "…")


class StyleStats(BaseModel):
    """Per-episode style statistics (M4-B1 AC1)."""

    sentence_lengths: list[int] = Field(default_factory=list)
    avg_sentence_length: float = 0.0
    scene_openers: dict[str, int] = Field(default_factory=dict)  # dialogue/narrative/...
    ending_types: dict[str, int] = Field(default_factory=dict)  # dialogue/question/...
    repeated_phrases: list[str] = Field(default_factory=list)  # n-grams seen > 3x
    avg_paragraph_length: float = 0.0
    total_words: int = 0


def accumulate_style_stats(
    universe_dir: Path, episode_id: str, stats: StyleStats, keep: int = 10
) -> None:
    """Write per-episode stats and prune to keep the most recent ``keep``
    episodes (T4-DEV2). Prunes the oldest files by mtime."""
    stats_dir = universe_dir / "style_stats"
    stats_dir.mkdir(parents=True, exist_ok=True)
    (stats_dir / f"{episode_id}.json").write_text(stats.model_dump_json(indent=2), encoding="utf-8")
    files = sorted(stats_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in files[keep:]:
        stale.unlink(missing_ok=True)


def load_accumulated_stats(universe_dir: Path, keep: int = 10) -> str:
    """Render accumulated stats of the most recent ``keep`` episodes for the
    judge prompt (T4-DEV2). Empty string when no episodes recorded yet."""
    stats_dir = universe_dir / "style_stats"
    if not stats_dir.exists():
        return ""
    files = sorted(stats_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:keep]
    lines = ["[ACCUMULATED STYLE STATS — previous episodes]"]
    for path in files:
        try:
            stats = StyleStats.model_validate_json(path.read_text(encoding="utf-8"))
            openers = ", ".join(f"{k}={v}" for k, v in sorted(stats.scene_openers.items()))
            lines.append(
                f"- {path.stem}: avg_sentence={stats.avg_sentence_length}, "
                f"openers=[{openers}], total_words={stats.total_words}"
            )
        except Exception:
            lines.append(f"- {path.stem}: (corrupt)")
    return "\n".join(lines)


class StyleStatsTracker:
    """Accumulate style stats across the scenes of one episode."""

    def __init__(self) -> None:
        self._sentences: list[str] = []
        self._paragraphs: list[str] = []
        self._openers: Counter[str] = Counter()
        self._endings: Counter[str] = Counter()
        self._words = 0

    def track(self, narration_text: str) -> None:
        """Record one scene's narration."""
        self._paragraphs.extend(p.strip() for p in narration_text.splitlines() if p.strip())
        sentences = [s.strip() for s in _SENTENCE_SPLIT.split(narration_text) if s.strip()]
        self._sentences.extend(sentences)
        self._words += len(narration_text.split())
        if sentences:
            self._openers[_classify_opener(sentences[0])] += 1
            self._endings[_classify_ending(sentences[-1])] += 1

    def summarize(self) -> StyleStats:
        sentence_lengths = [len(s.split()) for s in self._sentences]
        avg_sentence = sum(sentence_lengths) / len(sentence_lengths) if sentence_lengths else 0.0
        para_lengths = [len(p.split()) for p in self._paragraphs]
        avg_para = sum(para_lengths) / len(para_lengths) if para_lengths else 0.0
        return StyleStats(
            sentence_lengths=sentence_lengths,
            avg_sentence_length=round(avg_sentence, 2),
            scene_openers=dict(self._openers),
            ending_types=dict(self._endings),
            repeated_phrases=_repeated_phrases(self._sentences),
            avg_paragraph_length=round(avg_para, 2),
            total_words=self._words,
        )

    def render(self) -> str:
        """Prompt snippet for ``working_memory.style_stats`` (M4-B1 AC2)."""
        stats = self.summarize()
        opener_summary = (
            ", ".join(f"{kind}={count}" for kind, count in sorted(stats.scene_openers.items()))
            or "none yet"
        )
        ending_summary = (
            ", ".join(f"{kind}={count}" for kind, count in sorted(stats.ending_types.items()))
            or "none yet"
        )
        lines = [
            f"avg_sentence_length: {stats.avg_sentence_length} words",
            f"scene_openers so far: {opener_summary}",
            f"endings so far: {ending_summary}",
            f"avg_paragraph_length: {stats.avg_paragraph_length} words",
        ]
        if stats.repeated_phrases:
            lines.append("repeated_phrases: " + ", ".join(stats.repeated_phrases[:5]))
        return "\n".join(lines)


def _classify_opener(sentence: str) -> str:
    stripped = sentence.lstrip()
    first_word = stripped.split()[0].lower() if stripped.split() else ""
    if any(ch in sentence[:20] for ch in _QUOTE_CHARS):
        return "dialogue"
    if first_word in _OPENING_ACTION_VERBS:
        return "action"
    if any(word in sentence[:40].lower() for word in _OPENING_DESCRIPTIVE):
        return "description"
    return "narrative"


def _classify_ending(sentence: str) -> str:
    if sentence.endswith("?"):
        return "question"
    if sentence.endswith(("!", "…")):
        return "dramatic"
    if any(ch in sentence[-20:] for ch in _QUOTE_CHARS):
        return "dialogue"
    return "narrative"


def _repeated_phrases(sentences: list[str], min_count: int = 3) -> list[str]:
    """2- and 3-word phrases appearing more than ``min_count`` times."""
    counter: Counter[str] = Counter()
    for sentence in sentences:
        words = sentence.lower().split()
        for size in (2, 3):
            for i in range(len(words) - size + 1):
                phrase = " ".join(words[i : i + size])
                counter[phrase] += 1
    return [
        phrase for phrase, count in counter.most_common() if count > min_count and len(phrase) > 3
    ]
