"""LLM access: one OpenAI-compatible client covers OpenAI, GLM, DeepSeek,
Gemini-compat gateways and local vLLM — only base_url/model change.

Prompts live in the prompts/ directory as Jinja-less {placeholder} templates;
this module loads and fills them. Never inline prompt text in code.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from storyforge.core.config import Settings
from storyforge.core.exceptions import ExternalServiceError, StoryGenerationError
from storyforge.core.retry import retry_external
from storyforge.core.types import StoryBeat, StoryConfig, StoryScene
from storyforge.kb.brief import (
    render_degraded,
    render_dossiers,
    render_established_facts,
    render_invented_facts,
    render_palette,
    render_theme,
    render_unknown,
)
from storyforge.kb.types import KnowledgeBrief

PROMPTS_DIR = (
    Path(__file__).resolve().parents[2] / "prompts"
    if (Path(__file__).resolve().parents[2] / "prompts").exists()
    else Path("prompts")
)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class PromptMeta:
    """Frontmatter metadata attached to a prompt template (M2-D3 §3.1)."""

    def __init__(
        self,
        version: int = 0,
        changelog: str = "",
        eval_ref: str | None = None,
    ) -> None:
        self.version = version
        self.changelog = changelog
        self.eval_ref = eval_ref


def load_prompt(name: str) -> str:
    """Load a prompt template (frontmatter stripped) as plain text."""
    meta, template = load_prompt_with_meta(name)
    return template


def load_prompt_with_meta(name: str) -> tuple[PromptMeta, str]:
    """Load a prompt template and its frontmatter metadata (M2-D3 §3.1).

    Template files start with ``---`` frontmatter (version/changelog/eval_ref)
    followed by the prompt body. Falls back to a version-0 meta when a
    template predates versioning.
    """
    path = PROMPTS_DIR / f"{name}.txt"
    if not path.exists():
        raise StoryGenerationError(f"prompt template not found: {path}")
    raw = path.read_text(encoding="utf-8")

    match = _FRONTMATTER_RE.match(raw)
    if match is None:
        return PromptMeta(version=0, changelog=""), raw

    import yaml

    data = yaml.safe_load(match.group(1)) or {}
    meta = PromptMeta(
        version=int(data.get("version", 0)),
        changelog=str(data.get("changelog", "")),
        eval_ref=data.get("eval_ref"),
    )
    return meta, raw[match.end() :]


def extract_json(text: str) -> dict[str, Any]:
    """Parse the first JSON object out of an LLM response (M2-D3 §3.2.1).

    LLMs frequently wrap JSON in markdown code fences; this strips them and
    takes the substring from the first ``{`` to the last ``}``.
    """
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise StoryGenerationError("judge response missing JSON", details={"response": text[:500]})
    parsed = json.loads(cleaned[start : end + 1])
    if not isinstance(parsed, dict):
        raise StoryGenerationError(
            "judge response JSON is not an object", details={"response": text[:500]}
        )
    return parsed


def fill_prompt(template: str, values: dict[str, Any]) -> str:
    text = template
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    return text


class LLMClient:
    """Thin async-free chat client over the OpenAI-compatible API."""

    def __init__(self, settings: Settings, model: str) -> None:
        self._settings = settings
        self._model = model
        if not settings.llm.api_key.get_secret_value():
            raise StoryGenerationError("LLM api_key is required (SF__LLM__API_KEY)")

    def chat(self, system: str, user: str) -> str:
        import httpx

        url = f"{self._settings.llm.base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {self._settings.llm.api_key.get_secret_value()}"}
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self._settings.llm.max_output_tokens,
        }

        def _call() -> dict[str, Any]:
            response = httpx.post(
                url,
                json=payload,
                headers=headers,
                timeout=self._settings.llm.timeout_seconds,
            )
            if response.status_code in (429, 500, 502, 503, 504):
                raise ExternalServiceError(f"llm http {response.status_code}", retryable=True)
            if response.status_code != 200:
                raise ExternalServiceError(
                    f"llm http {response.status_code}: {response.text[:500]}",
                    retryable=False,
                )
            return dict(response.json())

        data = retry_external(_call)
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError) as exc:
            raise StoryGenerationError(f"malformed llm response: {data}") from exc


class StoryWriter:
    """Outline-first, scene-by-scene story generation over a KnowledgeBrief."""

    def __init__(self, settings: Settings) -> None:
        self._writer = LLMClient(settings, settings.llm.writer_model)

    def generate_outline(
        self, config: StoryConfig, brief: KnowledgeBrief
    ) -> tuple[list[StoryBeat], str]:
        """Pass 1 — beats from premise + theme/dossiers/established/unknown."""
        template = load_prompt("outline")
        characters = (
            "\n".join(f"- {c.name}: {c.personality}" for c in config.characters)
            or "(no fixed characters)"
        )
        target_words = int(config.target_minutes * 150)

        system = fill_prompt(
            template,
            {
                "language": config.language,
                "genre": config.genre,
                "tone": config.style.tone,
                "grounding": config.grounding.value,
            },
        )
        user = fill_prompt(
            template,
            {
                "title": config.title,
                "premise": config.premise or "(derive from knowledge base)",
                "characters": characters,
                "target_words": target_words,
                "degraded": render_degraded(brief.reason) if brief.degraded else "",
                "theme": render_theme(brief.theme),
                "dossiers": render_dossiers(brief.dossiers),
                "established": render_established_facts(brief.established),
                "invented": render_invented_facts(brief.invented),
                "unknown": render_unknown(brief.unknown_entities),
            },
        )
        response = self._writer.chat(system, user)
        return self._parse_beats(response), user

    def generate_scene(
        self,
        config: StoryConfig,
        beat: StoryBeat,
        brief: KnowledgeBrief,
        *,
        lint_feedback: str | None = None,
        style_stats: str | None = None,
    ) -> tuple[StoryScene, str]:
        """Pass 2 — expand one beat using its scene palette.

        ``lint_feedback`` (optional) appends deterministic lint feedback to the
        prompt so a regenerated scene knows exactly what to fix (M2-D2 §2.3).
        ``style_stats`` (M4-B1) injects per-episode writing statistics so the
        writer avoids repeating patterns.
        """
        template = load_prompt("scene")
        appearance_by_name = {c.name: c.appearance for c in config.characters}
        characters = (
            "; ".join(
                f"{name} ({appearance_by_name.get(name, 'appearance unspecified')})"
                for name in beat.characters
            )
            or "(narrator only)"
        )

        system = fill_prompt(
            template,
            {
                "language": config.language,
                "tone": config.style.tone,
                "art_style": config.style.art_style,
            },
        )
        feedback_section = (
            f"\nLINT FEEDBACK (fix these before writing):\n{lint_feedback}" if lint_feedback else ""
        )
        user = (
            fill_prompt(
                template,
                {
                    "beat_summary": beat.summary,
                    "characters": characters,
                    "degraded": render_degraded(brief.reason) if brief.degraded else "",
                    "palette": render_palette(brief.palette),
                    "established": render_established_facts(brief.established),
                    "style_stats": style_stats or "",
                },
            )
            + feedback_section
        )
        response = self._writer.chat(system, user)
        narration, image_prompt = self._parse_scene_response(response)
        scene = StoryScene(
            scene_id=f"{beat.beat_id}_scene",
            beat=beat,
            narration_text=narration,
            image_prompt=image_prompt,
            facts_used=[p.chunk_id for p in brief.palette],
        )
        return scene, user

    @staticmethod
    def _parse_beats(response: str) -> list[StoryBeat]:
        """Parse 'BEAT: id | summary | characters | image_hint' lines."""
        beats: list[StoryBeat] = []
        for line in response.splitlines():
            line = line.strip()
            if not line.upper().startswith("BEAT:"):
                continue
            body = line[len("BEAT:") :].strip()
            parts = [p.strip() for p in body.split("|")]
            if len(parts) < 2:
                continue
            beat_id, summary = parts[0], parts[1]
            characters = [c for c in (parts[2].split(",") if len(parts) > 2 else []) if c]
            image_hint = parts[3] if len(parts) > 3 else ""
            beats.append(
                StoryBeat(
                    beat_id=beat_id or f"beat_{len(beats):02d}",
                    summary=summary,
                    characters=characters,
                    image_hint=image_hint,
                )
            )
        if not beats:
            raise StoryGenerationError(
                "writer returned no parseable beats", details={"response": response[:1000]}
            )
        return beats

    @staticmethod
    def _parse_scene_response(response: str) -> tuple[str, str]:
        """Scene responses end with an IMAGE_PROMPT: line."""
        narration, _, image_prompt = response.partition("IMAGE_PROMPT:")
        if not image_prompt.strip():
            raise StoryGenerationError(
                "scene response missing IMAGE_PROMPT section",
                details={"response": response[:1000]},
            )
        return narration.strip(), image_prompt.strip()


def build_writer(settings: Settings) -> StoryWriter:
    return StoryWriter(settings)
