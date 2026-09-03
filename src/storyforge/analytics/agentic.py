"""M7-W3: Agentic analysis loop — RetentionProposal + generation + approve.

When retention is low (analytics ingest M7-V3), the agentic loop proposes
changes to the hook/prompt/pacing. Proposals are NEVER auto-applied — a human
must approve them (M5 web editor / ``analytics approve`` CLI).

Flow::

    retention (per scene, low) + story + scene clips
        -> [analyst LLM, 1 call] or deterministic heuristic fallback
        -> proposals/<project>.jsonl
        -> human approve -> decisions.jsonl (append-only)

The LLM path is only used when ``SF__ANALYTICS__AGENTIC_ENABLED=true`` AND an
API key exists — otherwise the offline heuristic proposes (testable, zero cost).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from storyforge.analytics.ingest import AnalyticsIngestor, RetentionCurve, SceneRetention
from storyforge.core.config import Settings
from storyforge.core.types import NarrationClip
from storyforge.publish.receipt import PublishReceipt


class RetentionProposal(BaseModel):
    """M7-W3 §2.4: one suggested change to improve retention on a low-performing
    scene. Created by the agentic loop, approved by a human."""

    project: str
    scene_id: str
    dimension: str  # "hook" | "pacing" | "grounding" | "visual" | "tts"
    change_kind: str  # "prompt_variant" | "hook_rewrite" | "pacing_cut"
    change: str  # the actual proposed change (e.g. new hook text)
    reason: str  # why this change is expected to help
    expected_impact: str  # e.g. "+5% retention on scene 0"
    created_at: str = Field(default_factory=lambda: datetime.now(tz=UTC).isoformat())


class RetentionProposalList(BaseModel):
    proposals: list[RetentionProposal] = Field(default_factory=list)


def generate_proposals(
    settings: Settings,
    analytics_dir: Path,
    workspace_dir: Path,
    universe_id: str,
    threshold: float | None = None,
) -> list[RetentionProposal]:
    """Scan the warehouse for published videos whose retention is below the
    threshold and propose scene-level changes (M7-W3).

    Best-effort and additive: videos without retention data are skipped; each
    proposal is written to ``<analytics_dir>/proposals/<project>.jsonl``.
    Returns the proposals created this round.
    """
    threshold = threshold if threshold is not None else settings.analytics.low_retention_threshold
    proposals: list[RetentionProposal] = []
    out_dir = analytics_dir / "proposals"
    out_dir.mkdir(parents=True, exist_ok=True)

    ingestor = AnalyticsIngestor(warehouse_dir=analytics_dir)
    for project_dir in _published_projects(universe_id, workspace_dir):
        project = project_dir.name
        receipt = _load_receipt(project_dir)
        if receipt is None or receipt.video_id in ("", "dry-run"):
            continue
        curve = ingestor.load_retention(receipt.video_id)
        if curve is None:
            continue
        clips = _load_clips(project_dir)
        retention = _map_retention(curve, clips)
        low = [s for s in retention if s.avg_view_pct < threshold]
        if not low:
            continue
        worst = min(low, key=lambda s: s.avg_view_pct)
        proposal = _propose_for_scene(settings, project, worst.scene_id, worst.avg_view_pct)
        _append_jsonl(out_dir / f"{project}.jsonl", proposal)
        proposals.append(proposal)
    return proposals


def approve_proposal(
    analytics_dir: Path, project: str, proposal_id: str
) -> RetentionProposal | None:
    """Approve one proposal (append to ``decisions.jsonl``). Returns None when
    the proposal id is unknown (M7-W3: proposals are never auto-applied)."""
    path = analytics_dir / "proposals" / f"{project}.jsonl"
    if not path.exists():
        return None
    for proposal in _read_proposals(path):
        if proposal.created_at == proposal_id:
            decisions_dir = analytics_dir / "decisions"
            decisions_dir.mkdir(parents=True, exist_ok=True)
            with (decisions_dir / f"{project}.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            **proposal.model_dump(mode="json"),
                            "approved_at": datetime.now(tz=UTC).isoformat(),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            return proposal
    return None


def list_proposals(analytics_dir: Path, project: str) -> list[RetentionProposal]:
    path = analytics_dir / "proposals" / f"{project}.jsonl"
    return _read_proposals(path) if path.exists() else []


def _propose_for_scene(
    settings: Settings,
    project: str,
    scene_id: str,
    view_pct: float,
) -> RetentionProposal:
    """One analyst call (when enabled) or a deterministic heuristic fallback."""
    if settings.analytics.agentic_enabled and settings.llm.api_key.get_secret_value():
        try:
            from storyforge.providers.llm import LLMClient, extract_json

            client = LLMClient(settings, settings.llm.reviewer_model)
            prompt = (
                f"Scene '{scene_id}' retains only {view_pct:.0%} of viewers. "
                "Propose ONE concrete change (hook rewrite | pacing cut | "
                "prompt variant) in this exact JSON: "
                '{"dimension": "...", "change_kind": "...", "change": "...", '
                '"reason": "...", "expected_impact": "..."}'
            )
            response = client.chat("You are a story-retention analyst.", prompt)
            data = extract_json(response)
            return RetentionProposal(
                project=project,
                scene_id=scene_id,
                dimension=str(data.get("dimension", "pacing")),
                change_kind=str(data.get("change_kind", "pacing_cut")),
                change=str(data.get("change", "")),
                reason=str(data.get("reason", "")),
                expected_impact=str(data.get("expected_impact", "")),
            )
        except Exception:
            pass  # fall through to the heuristic

    # Deterministic offline fallback — hook scenes get hook rewrites.
    if scene_id.startswith("hook_00") or scene_id.endswith("scene_00"):
        change = "Tighten the cold-open: open on the most concrete sensory moment, ≤ 60 words."
        change_kind = "hook_rewrite"
        dimension = "hook"
    else:
        change = "Cut the scene to its single strongest beat and remove filler sentences."
        change_kind = "pacing_cut"
        dimension = "pacing"
    return RetentionProposal(
        project=project,
        scene_id=scene_id,
        dimension=dimension,
        change_kind=change_kind,
        change=change,
        reason=f"scene retained only {view_pct:.0%} (below threshold)",
        expected_impact="+3-8% retention on this scene",
    )


def _map_retention(curve: RetentionCurve, clips: list[NarrationClip]) -> list[SceneRetention]:
    from storyforge.analytics.ingest import map_retention_to_scenes

    try:
        return map_retention_to_scenes(curve, clips)
    except Exception:
        return []


def _published_projects(universe_id: str, workspace_dir: Path) -> list[Path]:
    if not workspace_dir.exists():
        return []
    out: list[Path] = []
    for project_dir in sorted(p for p in workspace_dir.iterdir() if p.is_dir()):
        receipt = _load_receipt(project_dir)
        if receipt is None or receipt.video_id in ("", "dry-run"):
            continue
        if _project_in_universe(project_dir, universe_id):
            out.append(project_dir)
    return out


def _project_in_universe(project_dir: Path, universe_id: str) -> bool:
    from storyforge.core.types import Story

    story_path = project_dir / "04_story" / "story.json"
    if not story_path.exists():
        return False
    try:
        story = Story.model_validate_json(story_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return False
    return story.config.universe == universe_id


def _load_receipt(project_dir: Path) -> PublishReceipt | None:
    from storyforge.publish.receipt import load_receipt

    return load_receipt(project_dir / "07_video" / "publish.json")


def _load_clips(project_dir: Path) -> list[NarrationClip]:
    tts_dir = project_dir / "05_tts"
    if not tts_dir.exists():
        return []
    clips: list[NarrationClip] = []
    for path in sorted(tts_dir.glob("*.json")):
        try:
            clips.append(NarrationClip.model_validate_json(path.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            continue
    return clips


def _read_proposals(path: Path) -> list[RetentionProposal]:
    out: list[RetentionProposal] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(RetentionProposal.model_validate_json(line))
        except ValueError:
            continue
    return out


def _append_jsonl(path: Path, proposal: RetentionProposal) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(proposal.model_dump_json() + "\n")
