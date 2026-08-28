"""Tests for subtitle/SRT generation and image prompt composition — pure."""

from __future__ import annotations

from storyforge.core.types import SubtitleLine
from storyforge.stages.imaging import ImagingStage
from storyforge.stages.video import VideoStage


def test_srt_format():
    lines = [
        SubtitleLine(index=1, start=0.0, end=2.5, text="Xin chào"),
        SubtitleLine(index=2, start=2.5, end=61.25, text="Câu thứ hai"),
    ]
    srt = VideoStage._to_srt(lines)

    assert "1\n00:00:00,000 --> 00:00:02,500\nXin chào" in srt
    assert "2\n00:00:02,500 --> 00:01:01,250\nCâu thứ hai" in srt


def test_prompt_composition_inlines_appearances():
    prompt = ImagingStage._compose_prompt(
        "a rainy market",
        {"Lan": "Vietnamese girl, yellow raincoat"},
        "watercolor illustration",
    )
    assert "Lan: Vietnamese girl, yellow raincoat" in prompt
    assert "watercolor illustration" in prompt
    assert prompt.endswith("a rainy market")
