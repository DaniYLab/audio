"""M7-W3: Agentic analysis loop — RetentionProposal model + basic skeleton.

When retention is low (analytics ingest M7-V3), the agentic loop proposes
changes to the hook/prompt/pacing. Proposals are NEVER auto-applied — a human
must approve them (M5 web editor).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


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
    created_at: str = ""


class RetentionProposalList(BaseModel):
    proposals: list[RetentionProposal] = Field(default_factory=list)
