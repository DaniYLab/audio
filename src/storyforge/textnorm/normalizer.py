"""TextNormalizer + NormalizedText (M2-D1 spec §1.2-1.3)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from storyforge.textnorm.rules import Rule, build_rules

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_LOANWORDS_PATH = _REPO_ROOT / "config" / "textnorm_loanwords.yaml"


class NormalizedText(BaseModel):
    """Result of one normalization pass — audit trail for TTS debugging."""

    original: str
    normalized: str
    rules_applied: list[str] = Field(default_factory=list)


def _load_loanwords(path: Path | None) -> dict[str, str]:
    candidate = path or DEFAULT_LOANWORDS_PATH
    if not candidate.exists():
        return {}
    data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
    return {str(key): str(value) for key, value in data.items()}


class TextNormalizer:
    """Deterministic rule/regex normalizer. Never calls an LLM or network."""

    def __init__(self, loanwords_path: Path | None = None) -> None:
        self._loanwords = _load_loanwords(loanwords_path)
        self._notes: list[str] = []
        self._rules: list[Rule] = build_rules(self._loanwords, self._notes)

    def normalize(self, text: str) -> NormalizedText:
        self._notes.clear()
        rules_applied: list[str] = []
        current = text
        for rule in self._rules:
            current, count = rule.compiled().subn(rule.apply, current)
            if count:
                rules_applied.append(rule.name)
        rules_applied.extend(self._notes)
        return NormalizedText(
            original=text,
            normalized=current.strip(),
            rules_applied=rules_applied,
        )


def normalize_text(text: str, loanwords_path: Path | None = None) -> NormalizedText:
    return TextNormalizer(loanwords_path).normalize(text)
