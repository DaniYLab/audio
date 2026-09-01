"""TextNormalizer — deterministic pre-TTS text normalization (M2-D1 spec).

Pure rule + regex, never an LLM, no network. Runs between the story stage and
the TTS stage; subtitles keep the original narration text while the synthesized
audio reads the normalized form.
"""

from __future__ import annotations

from storyforge.textnorm.normalizer import NormalizedText, TextNormalizer, normalize_text

__all__ = ["NormalizedText", "TextNormalizer", "normalize_text"]
