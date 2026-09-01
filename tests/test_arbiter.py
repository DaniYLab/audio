"""M4-B4 arbiter tests — escalation, replay log, conservative fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

from storyforge.core.config import Settings
from storyforge.kb.types import ConflictVerdict, Fact, FactKind, FactOrigin
from storyforge.ledger import build_ledger
from storyforge.ledger.arbiter import (
    ArbiterLedgerStore,
    ArbiterVerdict,
    build_arbiter_ledger,
)


def _fact(fact_id: str, statement: str, episode: str = "ep_001") -> Fact:
    return Fact(
        fact_id=fact_id,
        kind=FactKind.CHARACTER,
        subject="Bà Ngoại",
        statement=statement,
        origin=FactOrigin.INVENTED,
        episode_id=episode,
    )


@pytest.fixture()
def universe(tmp_path: Path) -> Path:
    return tmp_path / "ledgers" / "storyvu"


def test_disabled_passes_through(universe: Path):
    ledger = build_ledger(universe)
    ledger.record_episode("ep_001", [_fact("f0007", "Bà Ngoại còn sống")])
    wrapped = build_arbiter_ledger(
        Settings(llm__api_key="test-key"), universe, ledger
    )  # arbiter_enabled default False
    candidate = _fact("f0041", "Bà Ngoại qua đời mùa đông", episode="ep_015")
    report = wrapped.find_conflicts(candidate)
    assert report.verdict is ConflictVerdict.CONFLICT  # rule-based still decides
    assert not (universe / "meta" / "conflict_verdicts.jsonl").exists()


def test_escalates_uncertain_and_replays(universe: Path):
    ledger = build_ledger(universe)
    ledger.record_episode("ep_003", [_fact("f0007", "Bà Ngoại còn sống")])
    wrapped = ArbiterLedgerStore(
        ledger,
        enabled=True,
        arbiter=lambda cand, existing: ArbiterVerdict("no_conflict", "complementary"),
        verdicts_path=universe / "meta" / "conflict_verdicts.jsonl",
    )
    candidate = _fact("f0041", "Bà Ngoại còn sống ở Hà Nội", episode="ep_015")
    report = wrapped.find_conflicts(candidate)
    assert report.verdict is ConflictVerdict.NO_CONFLICT
    assert report.reason == "complementary"

    lines = (universe / "meta" / "conflict_verdicts.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    import json

    row = json.loads(lines[0])
    assert row["candidate"]["fact_id"] == "f0041"
    assert row["existing"] == ["f0007"]
    assert row["verdict"] == "no_conflict"


def test_arbiter_conflict_overrides_rule_base(universe: Path):
    """Even a rule-decisive NO_CONFLICT is not touched when enabled — only
    uncertain cases reach the arbiter. Here the same-slot case is escalated
    and the arbiter says conflict."""
    ledger = build_ledger(universe)
    ledger.record_episode("ep_003", [_fact("f0007", "Bà Ngoại còn sống")])
    wrapped = ArbiterLedgerStore(
        ledger,
        enabled=True,
        arbiter=lambda cand, existing: ArbiterVerdict("conflict", "contradicts"),
        verdicts_path=universe / "meta" / "conflict_verdicts.jsonl",
    )
    candidate = _fact("f0041", "Bà Ngoại còn sống ở Sài Gòn", episode="ep_015")
    report = wrapped.find_conflicts(candidate)
    assert report.verdict is ConflictVerdict.CONFLICT
    assert [f.fact_id for f in report.conflicts] == ["f0007"]


def test_arbiter_failure_falls_back_conservative(universe: Path):
    ledger = build_ledger(universe)
    ledger.record_episode("ep_003", [_fact("f0007", "Bà Ngoại còn sống")])

    def boom(cand: Fact, existing: list[Fact]) -> ArbiterVerdict:
        raise RuntimeError("llm down")

    wrapped = ArbiterLedgerStore(
        ledger,
        enabled=True,
        arbiter=boom,
        verdicts_path=universe / "meta" / "conflict_verdicts.jsonl",
    )
    candidate = _fact("f0041", "Bà Ngoại còn sống ở Huế", episode="ep_015")
    report = wrapped.find_conflicts(candidate)
    # Conservative fallback (AC3): CONFLICT even though the arbiter crashed.
    assert report.verdict is ConflictVerdict.CONFLICT
    assert "conservative" in report.reason
