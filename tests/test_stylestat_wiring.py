"""M4-B1 wiring: cross-episode style stats accumulate + judge injection."""

from __future__ import annotations

import json
from pathlib import Path

from storyforge.core.artifacts import ArtifactStore
from storyforge.core.config import Settings
from storyforge.core.types import Story, StoryConfig, StoryStyle
from storyforge.stylestat import StyleStats, accumulate_style_stats, load_accumulated_stats


def _settings(tmp_path: Path) -> Settings:
    s = Settings(llm__api_key="test-key", workspace_dir=tmp_path / "ws")
    s.knowledge.kb_data_dir = tmp_path / "kb"
    return s


# -- accumulate (cli helper) ---------------------------------------------------


def _write_episode_stats(store: ArtifactStore) -> None:
    stats = StyleStats(
        sentence_lengths=[5, 7, 9],
        avg_sentence_length=7.0,
        scene_openers={"narrative": 2},
        ending_types={"narrative": 2},
        repeated_phrases=["rất là"],
        avg_paragraph_length=6.0,
        total_words=21,
    )
    store.write_model(store.dir("04_story") / "style_stats.json", stats)


def test_accumulate_style_stats_helper(tmp_path: Path) -> None:
    """The cli helper writes episode stats into the universe accumulation dir."""
    from storyforge.cli import _accumulate_style_stats

    settings = _settings(tmp_path)
    store = ArtifactStore(settings.workspace_dir, "proj")
    _write_episode_stats(store)

    universe_dir = Path(settings.knowledge.kb_data_dir) / "test_universe"
    universe_dir.mkdir(parents=True, exist_ok=True)

    _accumulate_style_stats(settings, store, "test_universe")

    stats_dir = universe_dir / "style_stats"
    assert stats_dir.exists()
    files = list(stats_dir.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["avg_sentence_length"] == 7.0


def test_accumulate_style_stats_noop_without_stats_file(tmp_path: Path) -> None:
    """No style_stats.json artifact → nothing written (no crash)."""
    from storyforge.cli import _accumulate_style_stats

    settings = _settings(tmp_path)
    store = ArtifactStore(settings.workspace_dir, "proj")
    universe_dir = Path(settings.knowledge.kb_data_dir) / "u"
    universe_dir.mkdir(parents=True, exist_ok=True)

    _accumulate_style_stats(settings, store, "u")
    assert not (universe_dir / "style_stats").exists()


def test_load_accumulated_stats_renders(tmp_path: Path) -> None:
    """Accumulated stats render into a prompt snippet."""
    universe_dir = tmp_path / "kb" / "u"
    universe_dir.mkdir(parents=True, exist_ok=True)
    accumulate_style_stats(
        universe_dir,
        "ep_001",
        StyleStats(
            avg_sentence_length=7.0,
            scene_openers={"narrative": 2},
            ending_types={},
            avg_paragraph_length=6.0,
            total_words=21,
        ),
    )
    text = load_accumulated_stats(universe_dir)
    assert "ACCUMULATED STYLE STATS" in text
    assert "ep_001" in text


def test_load_accumulated_stats_empty(tmp_path: Path) -> None:
    assert load_accumulated_stats(tmp_path / "missing") == ""


# -- judge injection -----------------------------------------------------------


def test_judge_scene_injects_style_stats(
    tmp_path: Path, scene, monkeypatch
) -> None:
    """The judge prompt carries accumulated style stats when present."""
    import storyforge.eval_story as eval_mod

    captured: dict[str, str] = {}

    class _FakeLLM:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def chat(self, system: str, user: str) -> str:
            captured["user"] = user
            return json.dumps(
                {
                    "scores": [
                        {"dimension": d, "score": 80, "evidence": "trích dẫn."}
                        for d in (
                            "grounding", "consistency", "pacing",
                            "tts_ready", "visual", "hook",
                        )
                    ]
                }
            )

    monkeypatch.setattr(eval_mod, "LLMClient", _FakeLLM)

    settings = _settings(tmp_path)
    story = Story(
        config=StoryConfig(
            title="T",
            genre="test",
            universe="u",
            target_minutes=1,
            language="vi",
            style=StoryStyle(),
        ),
        outline=[scene.beat],
        scenes=[scene],
    )

    from storyforge.eval_story import judge_scene

    evaluation = judge_scene(
        settings, story, scene.scene_id, None, 3, style_stats="[ACCUMULATED] avg=9.0"
    )
    assert "[ACCUMULATED]" in captured["user"]
    assert evaluation.prompt_version == 3


def test_eval_story_loads_accumulated(tmp_path: Path, scene, monkeypatch) -> None:
    """eval_story loads accumulated stats and passes them to judge_scene."""
    import storyforge.eval_story as eval_mod

    seen: list[str] = []

    def _fake_judge(
        settings: Settings,
        story: Story,
        scene_id: str,
        lint: object,
        prompt_version: int,
        style_stats: str = "",
    ) -> object:
        seen.append(style_stats)
        from storyforge.core.types import StoryEval

        return StoryEval(
            prompt_version=1,
            project="T",
            scene_id=scene_id,
            scores=[],
            total=0.0,
            judge_model="fake",
        )

    monkeypatch.setattr(eval_mod, "judge_scene", _fake_judge)

    universe_dir = tmp_path / "kb" / "u"
    universe_dir.mkdir(parents=True, exist_ok=True)
    accumulate_style_stats(
        universe_dir,
        "ep_001",
        StyleStats(avg_sentence_length=9.0, total_words=30),
    )

    settings = _settings(tmp_path)
    store = ArtifactStore(settings.workspace_dir, "proj")
    story = Story(
        config=StoryConfig(
            title="T",
            genre="test",
            universe="u",
            target_minutes=1,
            language="vi",
            style=StoryStyle(),
        ),
        outline=[scene.beat],
        scenes=[scene],
    )
    monkeypatch.setattr(eval_mod, "load_story_artifact", lambda store: (story, None, 3))

    from storyforge.eval_story import eval_story

    eval_story(settings, store)
    assert seen and "ACCUMULATED" in seen[0]
