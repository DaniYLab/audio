"""M4 DEV2 unit tests — guard (B3), stylestat (B1), rubric verdict (B2).

Pure logic, no network.
"""

from __future__ import annotations

import pytest

from storyforge.eval_story import _score_to_100, _verdict_for
from storyforge.guard import CheckpointDeltaGuard, GuardError
from storyforge.stylestat import StyleStatsTracker


# --- M4-B3: CheckpointDeltaGuard ----------------------------------------------


def test_guard_digest_is_stable():
    d1 = CheckpointDeltaGuard.digest("a", "b")
    d2 = CheckpointDeltaGuard.digest("a", "b")
    assert d1 == d2
    assert d1 != CheckpointDeltaGuard.digest("a", "c")


def test_guard_rejects_duplicate():
    guard = CheckpointDeltaGuard()
    guard.check(CheckpointDeltaGuard.digest("a", "b"))
    with pytest.raises(GuardError):
        guard.check(CheckpointDeltaGuard.digest("a", "b"))


def test_guard_rejects_baseline_unchanged():
    guard = CheckpointDeltaGuard()
    guard.add_baseline(CheckpointDeltaGuard.digest("a", "b"))
    with pytest.raises(GuardError):
        guard.check(CheckpointDeltaGuard.digest("a", "b"))


def test_guard_accepts_distinct():
    guard = CheckpointDeltaGuard()
    guard.add_baseline(CheckpointDeltaGuard.digest("a", "b"))
    guard.check(CheckpointDeltaGuard.digest("a", "c"))
    guard.check(CheckpointDeltaGuard.digest("b", "c"))


# --- M4-B1: StyleStatsTracker --------------------------------------------------


def test_stylestat_sentence_lengths():
    tracker = StyleStatsTracker()
    tracker.track("Trời mưa. Cô ấy chạy nhanh về nhà.")
    stats = tracker.summarize()
    assert stats.sentence_lengths == [2, 6]
    assert stats.avg_sentence_length == 4.0


def test_stylestat_openers_and_endings():
    tracker = StyleStatsTracker()
    tracker.track('"Anh đi đâu?" cô hỏi.')
    tracker.track("Bước vào phòng, anh nhìn quanh.")
    stats = tracker.summarize()
    assert stats.scene_openers.get("dialogue", 0) >= 1
    assert stats.scene_openers.get("action", 0) >= 1


def test_stylestat_repeated_phrases():
    tracker = StyleStatsTracker()
    for _ in range(4):
        tracker.track("bà ngoại gánh hàng rong bà ngoại gánh hàng")
    stats = tracker.summarize()
    assert any("bà ngoại" in p for p in stats.repeated_phrases)


# --- M4-B2: rubric verdict + backward-compat -----------------------------------


def test_verdict_thresholds():
    assert _verdict_for(30) == "fail"
    assert _verdict_for(39.9) == "fail"
    assert _verdict_for(40) == "warn"
    assert _verdict_for(69) == "warn"
    assert _verdict_for(70) == "pass"
    assert _verdict_for(100) == "pass"


def test_score_backward_compat_1_5_scale():
    assert _score_to_100(4.0) == 80.0
    assert _score_to_100(3.5) == 70.0


def test_score_0_100_untouched():
    assert _score_to_100(72) == 72
    assert _score_to_100(40) == 40
