"""M2-Q5: run the 50-sentence lint dataset (tests/fixtures/lint_cases_vi.yaml).

P1 deliverable, executed at P3 as acceptance: every expected substring must
appear in the normalized text and long-sentence cases must produce the
expected lint severity/rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from storyforge.lint import lint_scene
from storyforge.textnorm import TextNormalizer

_FIXTURE = Path(__file__).resolve().parents[0] / "fixtures" / "lint_cases_vi.yaml"


def _cases() -> list[dict[str, object]]:
    data = yaml.safe_load(_FIXTURE.read_text(encoding="utf-8")) or []
    return [c for c in data if isinstance(c, dict)]


def test_dataset_has_50_cases():
    assert len(_cases()) == 50


@pytest.mark.parametrize("case", _cases(), ids=lambda c: str(c.get("id")))
def test_normalization_expected_contains(case: dict[str, object]):
    """Every expected_contains substring must survive normalization."""
    if "expected_contains" not in case:
        pytest.skip("lint-only case")
    normalizer = TextNormalizer()
    normalized = normalizer.normalize(str(case["input"])).normalized
    for expected in case["expected_contains"]:
        assert (
            str(expected) in normalized
        ), f"case {case['id']}: expected {expected!r} in {normalized!r}"


@pytest.mark.parametrize("case", _cases(), ids=lambda c: str(c.get("id")))
def test_lint_severity_matches_expectation(case: dict[str, object]):
    """Long-sentence cases must trigger the expected lint rule/severity."""
    if "expect_rule" not in case:
        pytest.skip("not a lint case")
    from storyforge.core.types import StoryBeat, StoryScene

    scene = StoryScene(
        scene_id=str(case["id"]),
        beat=StoryBeat(beat_id="b0", summary="s"),
        narration_text=str(case["input"]),
        image_prompt="",
    )
    issues = lint_scene(scene, TextNormalizer())
    rule = str(case["expect_rule"])
    severity = str(case["expect_severity"])
    matching = [i for i in issues if i.rule == rule]
    assert matching, f"case {case['id']}: no issue for rule {rule}: {issues}"
    assert all(
        i.severity == severity for i in matching
    ), f"case {case['id']}: expected {severity}, got {[i.severity for i in matching]}"
